import copy
import importlib.util
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
AVAILABLE=all(importlib.util.find_spec(name) for name in ("PIL","numpy","cv2","rapidocr_onnxruntime"))
from gkshot.capture.store import CaptureSession, atomic_json
from gkshot.capture.vision import load_profile, overlap, pages_match
from gkshot.capture.windows import CapturePaused


def rows_for(page):
    return [[{"column":col,"signature":[(row*3+col)*10]*24,"point":[0.2+col*0.3,0.38+(row-page*2)*0.1],"box":[0.1,0.35,0.3,0.44]}
             for col in range(3)] for row in range(page*2,page*2+3)]


class FakeGame:
    """Deterministic UI simulator; no actual game input occurs in these tests."""
    def __init__(self,profile,pages=1):
        self.profile,self.pages=profile,pages
        self.state="list";self.page=0;self.selected=0;self.selected_point=None;self.inputs=[];self.card_clicks=0
        self.interrupt_at=None;self.interrupted=False;self.fail_return=False;self.focus=True
    def activate(self):self.focus=True
    def keys(self):return {"pause":self.interrupted}
    def screenshot(self):
        from PIL import Image,ImageDraw
        if not self.focus:raise CapturePaused("lost focus")
        image=Image.new("RGB",(80,160),(210,210,210))
        ImageDraw.Draw(image).rectangle((2,90,76,144),fill={"list":"blue","top":"green","bottom":"purple","popup":"orange"}[self.state])
        image.info.update(state=self.state,page=self.page)
        return image
    def click(self,point):
        if not self.focus:raise CapturePaused("lost focus")
        self.inputs.append(("click",point))
        if point==self.profile["open_detail"]:self.state="top"
        elif point==self.profile["back"]:self.state="list" if not self.fail_return else "top"
        elif point==self.profile["dismiss_tooltip"]:self.state="bottom"
        elif point==self.profile["stage_tab"]:self.state="top"
        elif self.state=="list":self.selected+=1;self.selected_point=point
        else:
            self.state="popup";self.card_clicks+=1
            if self.card_clicks==self.interrupt_at:self.interrupted=True
    def scroll(self,point,notches):
        self.inputs.append(("scroll",point,notches))
        if self.state=="list":self.page=0 if notches>0 else min(self.pages-1,self.page+1)
        else:self.state="top" if notches>0 else "bottom"


class FakeVision:
    def __init__(self,game):self.game=game;self.changed=False
    def is_list(self,image):return image.info["state"]=="list"
    def is_detail(self,image):return image.info["state"] in ("top","bottom","popup")
    def is_detail_page(self,image):return self.is_detail(image)
    def is_loading(self,image):return image.info.get("loading",False)
    def at_detail_edge(self,image,bottom=False):return image.info["state"]==("bottom" if bottom else "top")
    def selection_matches(self,image,tile):return self.game.selected_point==tile["point"]
    def at_list_start(self,image):return image.info["page"]==0
    def at_list_end(self,image):return image.info["page"]==self.game.pages-1
    def production_only(self,image,tile):return False
    def details_enabled(self,image):return True
    def matches_list_page(self,image,rows,tolerance):return pages_match(rows,self.list_rows(image),tolerance)
    def list_rows(self,image):
        rows=rows_for(image.info["page"])
        if self.changed:rows[0][0]["signature"]=[255]*24
        return rows
    def cards(self,image):
        return [{"slot":i+1,"point":[0.15+0.15*(i%4),0.65+0.12*(i//4)]} for i in range(6)] if image.info["state"]=="bottom" else []
    def tooltip(self,image):return {"check":"bounded-panel-with-text"} if image.info["state"]=="popup" else None
    def customization_badge(self,image,card):return {"state":"unknown","rule":"test"}


@unittest.skipUnless(AVAILABLE,"Use the project virtual environment")
class CaptureTests(unittest.TestCase):
    def test_restart_in_detail_or_tooltip_returns_automatically_then_resumes(self):
        for state in ('top','bottom','popup'):
            with self.subTest(state=state),tempfile.TemporaryDirectory() as directory:
                session,game,vision,collector=self.make(directory)
                collector.run(1)
                game.state=state;game.inputs.clear()
                collector.run(1)
                self.assertIsNone(session.data['last_error'])
                self.assertEqual(len(session.data['entries']),2)
                clicks=[a[1] for a in game.inputs if a[0]=='click']
                self.assertEqual(clicks[0],game.profile['dismiss_tooltip'] if state=='popup' else game.profile['back'])
                self.assertTrue(all(e['status']=='captured' for e in session.data['entries']))

    def test_temporary_capture_failure_reenters_once_and_imports_only_successful_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory)
            original=vision.cards
            vision.cards=lambda image:[] if session.data['entries'][0]['attempt']==1 else original(image)
            collector.run(1)
            entry=session.data['entries'][0]
            self.assertEqual(entry['attempt'],2)
            self.assertEqual(entry['status'],'captured')
            self.assertEqual(entry['attempts'][0]['outcome'],'retry')
            batch=json.loads((session.root/'batch.json').read_text(encoding='utf8'))
            old_paths={r['path'] for r in entry['images'] if r['metadata'].get('attempt')==1}
            self.assertFalse(old_paths.intersection(batch['memories'][0]['details']))
            self.assertTrue(any(r['role']=='diagnostic-retry' for r in entry['images']))

    def test_permanent_capture_failure_is_skipped_after_two_tries_and_next_memory_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory)
            original=vision.cards
            vision.cards=lambda image:[] if len(session.data['entries'])==1 else original(image)
            collector.run(2)
            self.assertIsNone(session.data['last_error'])
            self.assertEqual([e['status'] for e in session.data['entries']],['skipped-incomplete','captured'])
            self.assertEqual([e['attempt'] for e in session.data['entries']],[2,1])
            skipped=json.loads((session.root/'skipped-captures.json').read_text(encoding='utf8'))
            self.assertEqual([e['ordinal'] for e in skipped['memories']],[1])

    def test_navigation_accidentally_opens_selected_memory_then_returns_and_turns_page(self):
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory,pages=2)
            previous=rows_for(0);game.selected_point=previous[-1][-1]['point']
            restored=[False];original=game.click
            def click(point):
                if point==game.selected_point and not restored[0]:
                    game.inputs.append(('click',point));game.state='top'
                else:
                    original(point)
                    if point==game.profile['back']:restored[0]=True
            game.click=click
            vision.selected_list_cell=lambda image,rows:((2 if game.page==0 else 0,2) if restored[0] else None)
            _,rows,skip=collector.next_page(game.screenshot(),previous)
            self.assertEqual(game.page,1)
            self.assertEqual(skip,1)
            self.assertEqual(session.data['entries'],[])
            self.assertEqual(sum(a==('click',game.profile['back']) for a in game.inputs),1)

    def test_auto_return_is_bounded_and_never_backs_out_of_unknown_page(self):
        for unknown in (False,True):
            with self.subTest(unknown=unknown),tempfile.TemporaryDirectory() as directory:
                session,game,vision,collector=self.make(directory)
                game.state='top';game.fail_return=True
                if unknown:vision.is_detail_page=lambda image:False
                with self.assertRaises(CapturePaused):collector.return_to_list(game.screenshot())
                self.assertEqual(sum(a==('click',game.profile['back']) for a in game.inputs),0 if unknown else 2)

    def test_restart_resumes_later_page_without_rewind_and_keeps_ids(self):
        from gkshot.capture.runner import Collector
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory,pages=3)
            collector.run(10)
            self.assertEqual(game.page,1)
            old_ids=[e['capture_id'] for e in session.data['entries']]
            reloaded=CaptureSession(session.root)
            self.assertEqual(reloaded.data['checkpoint']['page_index'],1)
            resumed=Collector(game,vision,reloaded,sleep=lambda _:None)
            resumed.rewind=Mock(side_effect=AssertionError('must not rewind'))
            game.inputs.clear()
            self.assertEqual(resumed.run(1),'paused')
            self.assertIsNone(reloaded.data['last_error'])
            self.assertEqual([e['capture_id'] for e in reloaded.data['entries'][:10]],old_ids)
            self.assertEqual(len(reloaded.data['entries']),11)
            self.assertFalse(any(a[0]=='scroll' and a[1]==game.profile['list_scroll'] for a in game.inputs))
            self.assertEqual(reloaded.data['entries'][-1]['cell_index'],4)
            self.assertEqual(reloaded.data['checkpoint']['cell_index'],5)
            self.assertIn('定位断点',(session.root/'capture.log').read_text(encoding='utf8'))

    def test_explicit_top_replay_remains_available(self):
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory,pages=2)
            collector.run(10)
            collector.rewind=Mock(wraps=collector.rewind)
            collector.run(1,from_top=True)
            collector.rewind.assert_called_once()
            self.assertEqual(len(session.data['entries']),11)

    def test_interrupted_turn_recovers_nearby_saved_page_before_creating_successor(self):
        from gkshot.capture.runner import Collector
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory,pages=2)
            collector.run(9)
            old_ids=[e['capture_id'] for e in session.data['entries']]
            game.page=1  # wheel moved, process stopped before new page was saved
            reloaded=CaptureSession(session.root)
            resumed=Collector(game,vision,reloaded,sleep=lambda _:None)
            resumed.rewind=Mock(side_effect=AssertionError('must not rewind'))
            game.inputs.clear()
            resumed.run(1)
            self.assertIsNone(reloaded.data['last_error'])
            self.assertEqual([e['capture_id'] for e in reloaded.data['entries'][:9]],old_ids)
            self.assertEqual(len(reloaded.data['entries']),10)
            self.assertEqual(len(reloaded.data['pages']),2)
            self.assertEqual(game.inputs[0],('scroll',game.profile['list_scroll'],.5))
            self.assertEqual(reloaded.data['entries'][-1]['page_index'],1)

    def test_unmatched_checkpoint_search_is_local_and_keeps_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory,pages=2)
            collector.run(1)
            checkpoint=copy.deepcopy(session.data['checkpoint'])
            vision.changed=True
            collector.rewind=Mock(side_effect=AssertionError('must not rewind'))
            game.inputs.clear()
            self.assertEqual(collector.run(1),'paused')
            self.assertIn('断点附近',session.data['last_error'])
            self.assertEqual(session.data['checkpoint'],checkpoint)
            self.assertEqual(len(session.data['entries']),1)
            self.assertEqual(len(game.inputs),12)
            self.assertTrue(all(a[0]=='scroll' and abs(a[2])==.5 for a in game.inputs))

    def test_checkpoint_distinguishes_shared_rows_from_identical_owned_copies(self):
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory)
            rows=rows_for(0)
            session.data['pages']=[{'rows':rows[:2],'skip_rows':0},{'rows':rows[1:],'skip_rows':1}]
            vision.matches_list_page=lambda im,rs,tol:any(pages_match(rs,rows[j:j+len(rs)],tol) for j in range(len(rows)-len(rs)+1))
            for pi,positions in [(0,range(6)),(1,range(3,6))]:
                for cell in positions:session.entry(pi,cell).update(status='captured',selection_verified=True)
            self.assertEqual(collector.locate_saved_page(game.screenshot(),rows)[0],1)
            session.data['entries'][0]['status']='pending'
            self.assertEqual(collector.locate_saved_page(game.screenshot(),rows)[0],0)
            session.data['pages']=[{'rows':rows,'skip_rows':0},{'rows':copy.deepcopy(rows),'skip_rows':0}]
            self.assertIsNone(collector.locate_saved_page(game.screenshot(),rows))

    def test_resume_marks_last_usable_tile_even_if_self_match_is_unambiguous(self):
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory,pages=3)
            previous=copy.deepcopy(rows_for(0)[:2])
            for i,row in enumerate(previous):
                for tile in row:tile['signature']=[i*10]*24
            self.assertEqual(overlap(previous,previous,9),2)
            marked=[False]
            def rows(image):
                result=copy.deepcopy(previous)
                for i,row in enumerate(result):
                    for tile in row:tile['signature']=[80 if image.info['page']>=2 and i else 5]*24
                return result
            vision.list_rows=rows
            vision.matches_list_page=lambda im,rs,tol:im.info['page']==0
            vision.selected_list_cell=lambda im,rs:((1 if im.info['page']<2 else 0,1) if marked[0] else None)
            vision.production_only=lambda im,tile:tile['column']==2
            original=game.click
            def click(point):
                original(point);marked[0]=True
            game.click=click
            def scroll(point,notches):
                game.inputs.append(('scroll',point,notches));game.page+=1
            game.scroll=scroll
            image,new_rows,skip=collector.next_page(game.screenshot(),previous)
            self.assertEqual(game.selected_point,previous[-1][1]['point'])
            self.assertEqual(game.selected,1)
            self.assertEqual(game.card_clicks,0)
            self.assertEqual(game.page,2)
            self.assertEqual(skip,1)
            reloaded=CaptureSession(session.root)
            record=next(r for r in reloaded.data['navigation_images'] if r['role']=='list-navigation-anchor')
            self.assertEqual(record['metadata']['anchor'],[1,1])
            self.assertTrue(reloaded.source(record).is_file())

    def test_similar_rows_wait_for_new_row_and_keep_copies_separate(self):
        for initially_selected in ('last','first','offscreen'):
            with self.subTest(initially_selected=initially_selected),tempfile.TemporaryDirectory() as directory:
                session,game,vision,collector=self.make(directory,pages=3)
                marked=[initially_selected=='last']
                def rows(image):
                    result=copy.deepcopy(rows_for(0)[:2])
                    for i,row in enumerate(result):
                        for tile in row:
                            tile['signature']=[0]*24
                            tile['owned_row']=i+(image.info['page']>=2)
                    return result
                vision.list_rows=rows
                vision.selected_list_cell=lambda im,rs: ((1 if im.info['page']<2 else 0,2) if marked[0]
                                                         else ((0,2) if initially_selected=='first' else None))
                vision.matches_list_page=lambda im,rs,tol: im.info['page']==0
                original=game.click
                def click(point):
                    original(point);marked[0]=True
                game.click=click
                def scroll(point,notches):
                    game.inputs.append(('scroll',point,notches));game.page+=1
                game.scroll=scroll
                before=game.screenshot()
                image,new_rows,skip=collector.next_page(before,rows(before))
                self.assertEqual(game.page,2)
                self.assertEqual(skip,1)
                self.assertEqual([r[0]['owned_row'] for r in new_rows],[1,2])
                self.assertEqual(game.selected,0 if initially_selected=='last' else 1)
                self.assertEqual(game.card_clicks,0)

    def test_ambiguous_overlap_enters_bounded_recovery_instead_of_raising_immediately(self):
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory)
            rows=copy.deepcopy(rows_for(0)[:2])
            for row in rows:
                for tile in row:tile['signature']=[0]*24
            vision.list_rows=lambda image:copy.deepcopy(rows)
            vision.matches_list_page=lambda *args:False
            with self.assertRaisesRegex(CapturePaused,'自动回滚重试'):
                collector.next_page(game.screenshot(),rows)
            self.assertEqual(len(game.inputs),5)
            self.assertTrue(all(a[0]=='scroll' for a in game.inputs))

    def test_overlap_recovery_backtracks_then_preserves_exact_owned_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory,pages=3)
            def scroll(point,notches):
                game.inputs.append(('scroll',point,notches))
                game.page=2 if notches<0 else 1
            game.scroll=scroll
            image,rows,skip=collector.next_page(game.screenshot(),rows_for(0))
            self.assertEqual(image.info['page'],1)
            self.assertEqual(rows,rows_for(1))
            self.assertEqual(skip,1)
            self.assertEqual([a[2] for a in game.inputs],[-1,.5])
            self.assertEqual(game.selected,0)
            self.assertTrue(list((session.root/'raw/navigation').glob('diagnostic-overlap-*.png')))

    def test_overlap_recovery_approaches_new_rows_in_quarter_notches(self):
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory,pages=3)
            quarters=[]
            def scroll(point,notches):
                game.inputs.append(('scroll',point,notches))
                if notches==-1:game.page=2
                elif notches>0:game.page=0
                else:
                    quarters.append(notches)
                    if len(quarters)>=2:game.page=1
            game.scroll=scroll
            image,rows,skip=collector.next_page(game.screenshot(),rows_for(0))
            self.assertEqual(quarters,[-.25,-.25])
            self.assertEqual(rows,rows_for(1))
            self.assertEqual(skip,1)

    def test_recovery_stops_forward_search_when_tail_is_lost_again(self):
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory,pages=3)
            def scroll(point,notches):
                game.inputs.append(('scroll',point,notches))
                game.page=0 if notches>0 else 2
            game.scroll=scroll
            with self.assertRaisesRegex(CapturePaused,'自动回滚重试'):
                collector.next_page(game.screenshot(),rows_for(0))
            self.assertEqual([a[2] for a in game.inputs],[-1,.5,-.25])
            self.assertEqual(session.data['entries'],[])
            records=list((session.root/'raw/navigation').glob('overlap-search-*.png'))
            self.assertTrue(records)

    def test_overlap_recovery_is_bounded_and_interruptible(self):
        for pause in (False,True):
            with self.subTest(pause=pause),tempfile.TemporaryDirectory() as directory:
                session,game,vision,collector=self.make(directory,pages=3)
                def scroll(point,notches):
                    game.inputs.append(('scroll',point,notches));game.page=2
                    if pause and notches>0:game.interrupted=True
                game.scroll=scroll
                with self.assertRaises(CapturePaused):collector.next_page(game.screenshot(),rows_for(0))
                self.assertEqual(len(game.inputs),2 if pause else 5)
                if not pause:
                    self.assertEqual([a[2] for a in game.inputs],[-1,.5,.5,.5,.5])
                self.assertEqual(game.selected,0)
                self.assertEqual(session.data['entries'],[])

    def test_legal_short_memories_capture_actual_card_count(self):
        for count in range(1,7):
            with self.subTest(count=count),tempfile.TemporaryDirectory() as directory:
                session,game,vision,collector=self.make(directory)
                cards=vision.cards
                vision.cards=lambda image:cards(image)[:count]
                collector.run(1)
                entry=session.data['entries'][0]
                self.assertEqual(entry['status'],'captured')
                self.assertEqual(game.card_clicks,count)
                self.assertEqual(entry['attempts'][-1]['card_count'],count)
                overview=next(r for r in entry['images'] if r['role']=='overview-cards')
                self.assertEqual(overview['metadata']['card_count'],count)
                self.assertEqual([d['slot'] for d in overview['metadata']['detail_selection']],list(range(1,count+1)))

    def test_incomplete_grid_continues_across_pages_and_resume_only_retries_on_request(self):
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory,pages=2)
            cards=vision.cards
            vision.cards=lambda image: [] if game.selected==1 else cards(image)
            messages=[];collector.notify=messages.append
            self.assertEqual(collector.run(100),'complete')
            self.assertEqual(len(session.data['entries']),15)
            first=session.data['entries'][0]
            self.assertEqual(first['status'],'skipped-incomplete')
            self.assertEqual(len(first['attempts'][0]['detail_scrolls']),1)
            self.assertTrue(all(e['status']=='captured' for e in session.data['entries'][1:]))
            self.assertTrue(any('跳过编号：1' in m for m in messages))
            batch=json.loads((session.root/'batch.json').read_text(encoding='utf8'))
            self.assertEqual(len(batch['memories']),14)
            skipped=json.loads((session.root/'skipped-captures.json').read_text(encoding='utf8'))
            self.assertEqual([e['ordinal'] for e in skipped['memories']],[1])
            self.assertTrue(skipped['memories'][0]['images'])
            old_images=copy.deepcopy(first['images'])
            collector.run(100)
            self.assertEqual(first['attempt'],2)
            vision.cards=cards
            collector.run(1,retry_issues=True,from_top=True)
            self.assertEqual(first['status'],'captured')
            self.assertEqual(first['attempt'],3)
            for record in old_images:self.assertTrue(session.source(record).exists())
            self.assertEqual(json.loads((session.root/'skipped-captures.json').read_text(encoding='utf8'))['memories'],[])

    def test_failed_recovery_never_advances_or_exports_uncertain_attempt(self):
        for mode in ('return-failed','list-changed','pause','focus','unknown-page'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory:
                session,game,vision,collector=self.make(directory)
                def missing(image):
                    if mode=='return-failed':game.fail_return=True
                    elif mode=='list-changed':vision.changed=True
                    elif mode=='pause':game.interrupted=True
                    elif mode=='focus':game.focus=False
                    elif mode=='unknown-page':vision.is_detail_page=lambda image:False
                    return []
                vision.cards=missing
                self.assertEqual(collector.run(10),'paused')
                self.assertEqual(len(session.data['entries']),1)
                self.assertEqual(session.data['entries'][0]['status'],'capturing')
                self.assertEqual(json.loads((session.root/'batch.json').read_text(encoding='utf8'))['memories'],[])
                if mode in ('pause','focus','unknown-page'):
                    self.assertNotIn(('click',game.profile['back']),game.inputs)

    def test_unconfirmed_selection_is_skipped_on_resume_without_opening_wrong_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory)
            selection=vision.selection_matches
            vision.selection_matches=lambda image,tile:False
            collector.run(1)
            self.assertEqual(session.data['entries'][0]['status'],'skipped-incomplete')
            self.assertNotIn(('click',game.profile['open_detail']),game.inputs)
            vision.selection_matches=selection
            collector.run(1)
            self.assertEqual([e['status'] for e in session.data['entries']],['skipped-incomplete','captured'])
            self.assertEqual(session.data['entries'][0]['attempt'],2)

    def test_slow_loading_list_is_not_ready_and_open_is_clicked_once(self):
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory)
            original_click,original_shot=game.click,game.screenshot
            remaining=[0]
            def click(point):
                original_click(point)
                if point==game.profile['open_detail']:
                    remaining[0]=40  # eight seconds of unchanged list/loading
                    game.state='list'
            def shot():
                if remaining[0]:
                    remaining[0]-=1
                    image=original_shot();image.info['loading']=True
                    if remaining[0]==0:game.state='top'
                    return image
                return original_shot()
            game.click,game.screenshot=click,shot
            collector.run(1)
            self.assertEqual(session.data['entries'][0]['status'],'captured')
            self.assertEqual(sum(a==('click',game.profile['open_detail']) for a in game.inputs),1)

    def test_open_timeout_preserves_progress_and_does_not_click_stage_blindly(self):
        for loading in (False,True):
            with self.subTest(loading=loading),tempfile.TemporaryDirectory() as directory:
                session,game,vision,collector=self.make(directory)
                original=game.click
                def click(point):
                    if point==game.profile['open_detail']:
                        game.inputs.append(('click',point))
                        vision.is_loading=lambda image:loading
                    else:original(point)
                game.click=click
                collector.run(1)
                entry=session.data['entries'][0]
                self.assertEqual(entry['status'],'capturing' if loading else 'skipped-incomplete')
                if loading:self.assertIn('LOADING',session.data['last_error'])
                else:self.assertIsNone(session.data['last_error'])
                self.assertFalse(any(r['role']=='overview-top' for r in entry['images']))
                self.assertNotIn(('click',game.profile['stage_tab']),game.inputs)
                self.assertEqual(sum(a==('click',game.profile['open_detail']) for a in game.inputs),1 if loading else 2)

    def test_loading_wait_remains_interruptible(self):
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory)
            calls=[]
            def loading(image):
                calls.append(1)
                if len(calls)==3:game.interrupted=True
                return True
            vision.is_loading=loading
            with self.assertRaises(CapturePaused):collector.stable('detail',ready=vision.is_detail_page,timeout=30)
            self.assertLessEqual(len(calls),3)
            self.assertEqual(game.inputs,[])

    def test_produce_tab_switches_only_after_detail_page_is_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory)
            original_click=game.click
            stage=[False]
            original_detail=vision.is_detail
            vision.is_detail_page=original_detail
            vision.is_detail=lambda image:stage[0] and original_detail(image)
            def click(point):
                original_click(point)
                if point==game.profile['stage_tab']:stage[0]=True
            game.click=click
            collector.run(1)
            self.assertEqual(session.data['entries'][0]['status'],'captured')
            self.assertEqual(sum(a==('click',game.profile['stage_tab']) for a in game.inputs),1)

    def test_resume_relocates_only_saved_rows_without_losing_cell_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory)
            saved=rows_for(0)[:2]
            session.data['pages']=[{'rows':copy.deepcopy(saved),'skip_rows':1}]
            session.entry(0,3).update(status='captured',selection_verified=True)
            current=rows_for(0)
            for row in current:
                for tile in row:tile['point'][1]+=0.03
            vision.list_rows=lambda image:copy.deepcopy(current)
            vision.matches_list_page=lambda image,rows,tolerance: (
                pages_match(rows,current[:len(rows)],tolerance)
                and rows[0][0]['point']==current[0][0]['point'])
            def capture(entry,tile,image,rows):
                self.assertEqual(len(rows),2)
                self.assertEqual(tile['point'],current[1][1]['point'])
                entry.update(status='captured',selection_verified=True)
                return image
            collector.capture_memory=capture
            collector.run(1)
            self.assertEqual([(e['page_index'],e['cell_index']) for e in session.data['entries']],[(0,3),(0,4)])
            self.assertEqual(session.data['pages'][0]['rows'],saved)

    def test_resume_uses_verified_anchors_when_last_contour_row_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory)
            rows=rows_for(0)
            session.data["pages"]=[{"rows":copy.deepcopy(rows),"skip_rows":0}]
            for i in range(7):
                entry=session.entry(0,i)
                entry.update(status="captured",selection_verified=True)
            vision.list_rows=lambda image:rows[:2]
            vision.matches_list_page=lambda image,saved,tolerance:pages_match(saved,rows,tolerance)
            collector.run(1)
            self.assertEqual(len(session.data["entries"]),8)
            self.assertEqual(session.data["entries"][-1]["cell_index"],7)
            self.assertEqual(session.data["entries"][-1]["status"],"captured")

    def test_detail_burst_settles_once_and_stops_between_pulses(self):
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory)
            game.state="top"
            original=game.scroll
            pulses=[]
            def clamped(point,notches):
                pulses.append(notches)
                if len(pulses)>=4:original(point,notches)
            game.scroll=clamped
            with patch.object(collector,"stable",wraps=collector.stable) as stable:
                image=collector.scroll_detail(-3,4)
            self.assertEqual(image.info["state"],"bottom")
            self.assertEqual(pulses,[-3]*4)
            stable.assert_called_once_with("detail")
            pulses.clear()
            def interrupt(point,notches):
                pulses.append(notches);game.interrupted=True
            game.scroll=interrupt
            with self.assertRaises(CapturePaused):collector.scroll_detail(-3,4)
            self.assertEqual(pulses,[-3])

    def test_detail_endpoints_skip_redundant_top_and_bottom_scrolls(self):
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory)
            collector.run(1)
            scrolls=session.data["entries"][0]["attempts"][-1]["detail_scrolls"]
            self.assertEqual(len(scrolls),1)
            self.assertEqual(scrolls[0]["pulses"],4)
            self.assertLess(scrolls[0]["notches_per_pulse"],0)
    def test_missing_row_requires_regular_surrounding_anchors(self):
        from gkshot.capture.vision import complete_internal_rows
        self.assertEqual(complete_internal_rows([668,1048],191),[668,858,1048])
        self.assertEqual(complete_internal_rows([668,858,1048],191),[668,858,1048])
        self.assertEqual(complete_internal_rows([668],191),[668])
        self.assertEqual(complete_internal_rows([668,1000],191),[668,1000])

    def test_processing_counts_failures_and_finds_latest_nonempty_result(self):
        from gkshot.capture.process import processing_summary,latest_result_library
        self.assertIn("没有可处理",processing_summary([]))
        self.assertIn("成功入库 0 份",processing_summary([{"status":"failed"}]))
        self.assertIn("失败 1 份",processing_summary([{"status":"failed"}]))
        self.assertIn("成功入库 2 份",processing_summary([{"status":"processed"},{"status":"processed-with-issues"}]))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name,results in (("001",[{"memory_id":"test","status":"processed"}]),("002",[{"status":"failed"}]),("003",[])):
                run=root/"session"/"processing"/name
                (run/"memory-library").mkdir(parents=True)
                (run/"memory-library/library.sqlite3").touch()
                atomic_json(run/"results.json",{"results":results})
            self.assertEqual(latest_result_library(root),root/"session/processing/001/memory-library")

    def make(self,directory,pages=1):
        from gkshot.capture.runner import Collector
        session=CaptureSession.create(load_profile(),directory)
        game=FakeGame(session.data["profile"],pages)
        vision=FakeVision(game)
        collector=Collector(game,vision,session,sleep=lambda _:None)
        return session,game,vision,collector

    def test_one_memory_archives_six_slots_and_resumes_without_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory)
            collector.run(1)
            entry=session.data["entries"][0]
            self.assertEqual(entry["status"],"captured")
            self.assertEqual(sorted(r["slot"] for r in entry["images"] if r["role"]=="card-tooltip"),list(range(1,7)))
            self.assertEqual(len([r for r in entry["images"] if r["role"]=="card-before"]),6)
            hashes=[r["sha256"] for r in entry["images"]]
            saved=CaptureSession(session.root)
            self.assertEqual(saved.data["entries"][0]["capture_id"],entry["capture_id"])
            collector.run(1)
            self.assertEqual(len(session.data["entries"]),2)
            self.assertEqual([r["sha256"] for r in entry["images"]],hashes)
            batch=json.loads((session.root/"batch.json").read_text(encoding="utf-8"))
            self.assertEqual(len(batch["memories"]),2)
            self.assertEqual(len(batch["memories"][0]["card_details"]),6)
            self.assertEqual(game.state,"list")

    def test_already_selected_tile_is_not_clicked_again(self):
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory)
            game.selected_point=rows_for(0)[0][0]["point"]
            collector.run(1)
            self.assertEqual(session.data["entries"][0]["status"],"captured")
            self.assertEqual(game.selected,0)

    def test_production_only_skipped_and_unknown_disabled_button_not_clicked(self):
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory)
            vision.production_only=lambda image,tile:tile["column"]<2
            original_click=game.click
            def click(point):
                if point in [t["point"] for t in rows_for(0)[0][:2]]:
                    self.fail("Production-only tiles must never be clicked")
                original_click(point)
            game.click=click
            collector.run(1)
            self.assertEqual([e["status"] for e in session.data["entries"]],
                             ["skipped-production-only","skipped-production-only","captured"])
            self.assertEqual(sum(action==("click",game.profile["open_detail"]) for action in game.inputs),1)
            for entry in session.data["entries"][:2]:
                self.assertFalse(entry.get("selection_verified"))
                self.assertEqual([r["role"] for r in entry["images"]],["production-only-thumbnail"])
            collector.run(1)
            self.assertEqual([e["attempt"] for e in session.data["entries"][:2]],[1,1])
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory)
            vision.details_enabled=lambda image:False
            collector.run(1)
            self.assertFalse(any(action==("click",game.profile["open_detail"]) for action in game.inputs))
            self.assertEqual(session.data['entries'][0]['status'],'skipped-incomplete')
            self.assertIn("没有点击禁用按钮",session.data['entries'][0]['issues'][0]['message'])

    def test_wrong_selection_never_opens_detail_and_stale_checkpoint_is_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory)
            vision.selection_matches=lambda image,tile:False
            collector.run(1)
            self.assertEqual(game.card_clicks,0)
            self.assertFalse(any(action==("click",game.profile["open_detail"]) for action in game.inputs))
            self.assertEqual(session.data['entries'][0]['status'],'skipped-incomplete')
            self.assertIn("选中框",session.data['entries'][0]['issues'][0]['message'])
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory)
            collector.run(1)
            entry=session.data["entries"][0]
            entry.pop("selection_verified")
            game.selected_point=None
            session.save()
            collector.run(1)
            self.assertEqual(len(session.data["entries"]),1)
            self.assertEqual(entry["attempt"],2)
            self.assertTrue(entry["selection_verified"])

    def test_diagnostic_attaches_to_current_attempt_not_stale_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory)
            first=session.entry(0,0)
            stale=session.entry(0,2)
            stale["status"]="capturing"
            vision.selection_matches=lambda image,tile:False
            collector.run(1)
            self.assertTrue(any(r["role"]=="diagnostic-incomplete" for r in first["images"]))
            self.assertEqual(stale["images"],[])

    def test_interruption_retains_partial_files_and_restarts_same_owned_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory)
            game.interrupt_at=3
            collector.run(1)
            entry=session.data["entries"][0]
            self.assertEqual(entry["status"],"capturing")
            old=list(entry["images"])
            self.assertGreater(len(old),3)
            game.interrupted=False;game.interrupt_at=None;game.state="list"
            collector.run(1)
            self.assertEqual(len(session.data["entries"]),1)
            self.assertEqual(entry["attempt"],2)
            self.assertEqual(entry["status"],"captured")
            for image in old:self.assertTrue(session.source(image).exists())
            batch=json.loads((session.root/"batch.json").read_text(encoding="utf-8"))
            self.assertEqual(len(batch["memories"][0]["card_details"]),6)

    def test_selective_details_capture_custom_and_unknown_and_allow_all_override(self):
        for all_details in (False, True):
            with self.subTest(all_details=all_details), tempfile.TemporaryDirectory() as directory:
                session,game,vision,collector=self.make(directory)
                collector.all_details=all_details
                vision.customization_badge=lambda image,card:{"state":{5:"present",6:"unknown"}.get(card["slot"],"absent"),"rule":"test"}
                collector.run(1)
                entry=session.data["entries"][0]
                self.assertEqual(entry["status"],"captured")
                self.assertEqual(game.card_clicks,6 if all_details else 2)
                overview=next(r for r in entry["images"] if r["role"]=="overview-cards")
                self.assertEqual(len(overview["metadata"]["detail_selection"]),6)
                self.assertEqual(entry["attempts"][-1]["detail_policy"],"all" if all_details else "custom-or-uncertain")

    def test_popup_header_occlusion_allowed_but_restored_identity_change_not_bound(self):
        from PIL import ImageDraw
        for changed_after in (False,True):
            with self.subTest(changed_after=changed_after), tempfile.TemporaryDirectory() as directory:
                session,game,vision,collector=self.make(directory)
                original=game.screenshot
                changed_attempt=[None]
                def screenshot():
                    image=original()
                    attempt=session.data['entries'][0]['attempt'] if session.data['entries'] else 0
                    if game.state=='popup':changed_attempt[0]=attempt
                    if game.state=="popup" or (changed_after and changed_attempt[0]==attempt):
                        ImageDraw.Draw(image).rectangle((0,55,45,72),fill="black")
                    return image
                game.screenshot=screenshot
                collector.run(1)
                entry=session.data["entries"][0]
                bound=[r for r in entry["images"] if r["role"]=="card-tooltip"]
                if changed_after:
                    self.assertEqual(entry["status"],"skipped-incomplete")
                    self.assertEqual(bound,[])
                    self.assertTrue(any(r["role"]=="pending-tooltip" for r in entry["images"]))
                    self.assertEqual(json.loads((session.root/"batch.json").read_text(encoding="utf-8"))["memories"],[])
                else:
                    self.assertEqual(entry["status"],"captured")
                    self.assertEqual(len(bound),6)
                    self.assertTrue(all(r["metadata"]["binding_check"] for r in bound))

    def test_page_overlap_visits_owned_positions_once(self):
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory,pages=2)
            self.assertEqual(collector.run(100),"complete")
            self.assertEqual(len(session.data["entries"]),15)  # 9 + 6, three positions overlap
            self.assertEqual(len({e["capture_id"] for e in session.data["entries"]}),15)
            self.assertTrue(all(e["status"]=="captured" for e in session.data["entries"]))
            self.assertEqual(session.data["pages"][1]["skip_rows"],1)

    def test_changed_list_and_failed_return_pause_before_next_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory)
            collector.run(1)
            previous=len(game.inputs)
            vision.changed=True
            collector.run(1)
            self.assertIn("续采校验失败",session.data["last_error"])
            self.assertFalse(any(action[0]=="click" for action in game.inputs[previous:]))
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory)
            game.fail_return=True
            collector.run(2)
            self.assertEqual(len(session.data["entries"]),1)
            self.assertEqual(session.data["entries"][0]["status"],"capturing")

    def test_missing_tooltip_retains_issue_without_claiming_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory)
            vision.tooltip=lambda image:None
            collector.run(1)
            entry=session.data["entries"][0]
            self.assertEqual(entry["status"],"needs-recapture")
            self.assertEqual(len(entry["issues"]),6)
            self.assertEqual(len([r for r in entry["images"] if r["role"]=="diagnostic-tooltip"]),12)

    def test_ambiguous_overlap_and_source_tampering_rejected(self):
        row=rows_for(0)[0]
        with self.assertRaises(ValueError):overlap([row,row],[row,row],9)
        with tempfile.TemporaryDirectory() as directory:
            session,game,vision,collector=self.make(directory)
            collector.run(1)
            image=session.data["entries"][0]["images"][0]
            session.source(image).write_bytes(b"corrupt")
            with self.assertRaises(ValueError):session.source(image)
            bad=dict(image,path="../outside.png")
            with self.assertRaises(ValueError):session.source(bad)

    def test_foreground_guard_prevents_native_input(self):
        import ctypes
        from ctypes import wintypes as W
        from gkshot.capture.windows import WindowsBackend
        backend=WindowsBackend.__new__(WindowsBackend)
        backend.window={"hwnd":123,"pid":456,"title":"gakumas"};backend.geometry=None
        user=Mock()
        user.GetWindowThreadProcessId.side_effect=lambda h,p:setattr(ctypes.cast(p,ctypes.POINTER(W.DWORD)).contents,"value",456)
        user.GetWindowTextW.side_effect=lambda h,t,n:setattr(t,"value","gakumas")
        user.IsWindow.return_value=True;user.IsIconic.return_value=False;user.GetForegroundWindow.return_value=999
        backend.user=user
        with self.assertRaises(CapturePaused):backend.click([0.5,0.5])
        user.SetCursorPos.assert_not_called();user.SendInput.assert_not_called()


@unittest.skipUnless(AVAILABLE,"Use the project virtual environment")
class CaptureFixtureTests(unittest.TestCase):
    def test_orange_art_is_not_a_selection_and_actual_stopped_detail_can_return(self):
        from PIL import Image
        folder=ROOT/'tests/fixtures/capture'
        rows=json.loads((folder/'list-orange-art-anchors.json').read_text(encoding='utf8'))
        with Image.open(folder/'list-orange-art-selected.png') as image:
            self.assertFalse(self.vision.selection_matches(image,rows[0][2]))
            self.assertEqual(self.vision.selected_list_cell(image,rows),(1,2))
            self.assertFalse(self.vision.can_return_to_list(image))
        with Image.open(folder/'navigation-opened-detail.png') as image:
            self.assertTrue(self.vision.can_return_to_list(image))
        self.assertFalse(self.vision.can_return_to_list(Image.new('RGB',(1080,1920),'gray')))

    def test_missing_edge_row_is_recovered_and_checkpoint_locates_real_late_page(self):
        from PIL import Image,ImageDraw
        from gkshot.capture.runner import Collector
        folder=ROOT/'tests/fixtures/capture'
        saved=json.loads((folder/'list-checkpoint-pages.json').read_text(encoding='utf8'))
        with Image.open(folder/'list-missing-edge-before.png') as before,Image.open(folder/'list-missing-edge-row.png') as image,tempfile.TemporaryDirectory() as directory:
            previous=saved[42]['rows']
            for im in (before,image):
                rows=self.vision.list_rows(im)
                self.assertEqual(len(rows),2)  # no invented clipped row at the top
                self.assertEqual(self.vision.selected_list_cell(im,rows),(1,1))
                self.assertEqual(self.vision.page_overlap(im,previous,rows,9,anchor=(1,1),current_anchor=(1,1)),2)
            session=CaptureSession.create(load_profile(),directory)
            session.data['pages']=copy.deepcopy(saved)
            backend=Mock();backend.keys.return_value={}
            collector=Collector(backend,self.vision,session,sleep=lambda _:None)
            collector.stable=Mock(return_value=image)
            collector.scroll=Mock(side_effect=AssertionError('no navigation needed'))
            collector.rewind=Mock(side_effect=AssertionError('must not rewind'))
            _,index,rows,skip=collector.resume_position()
            self.assertEqual(index,42)
            self.assertEqual(skip,saved[42]['skip_rows'])
            # Grid pitch alone is insufficient evidence for a missing row.
            blank=image.copy();ImageDraw.Draw(blank).rectangle((0,948,1040,1140),fill='white')
            self.assertEqual(len(self.vision.list_rows(blank)),1)

    def test_resume_real_unmarked_blue_rows_skips_captured_entries_and_reaches_next_saved_page(self):
        from PIL import Image
        from gkshot.capture.runner import Collector
        folder=ROOT/'tests/fixtures/capture'
        saved=json.loads((folder/'list-resume-unmarked-anchors.json').read_text(encoding='utf8'))
        with Image.open(folder/'list-resume-unmarked.png') as before, Image.open(folder/'list-similar-before.png') as marked, Image.open(folder/'list-similar-after.png') as intermediate, Image.open(folder/'list-similar-next-page.png') as after, tempfile.TemporaryDirectory() as directory:
            previous=saved[0]['rows']
            self.assertEqual(overlap(previous,previous,9),2)  # old preflight saw no ambiguity
            self.assertIsNone(self.vision.selected_list_cell(before,previous))
            self.assertTrue(self.vision.matches_list_page(before,previous,9))
            with self.assertRaises(ValueError):
                self.vision.page_overlap(intermediate,previous,self.vision.list_rows(intermediate),9)
            session=CaptureSession.create(load_profile(),directory)
            session.data['pages']=copy.deepcopy(saved)
            for pi,page in enumerate(saved):
                for row_index,row in enumerate(page['rows']):
                    if row_index<page['skip_rows']:continue
                    for tile in row:
                        entry=session.entry(pi,row_index*3+tile['column'])
                        entry.update(status='captured',selection_verified=True)
            original_entries=copy.deepcopy(session.data['entries'])
            session.save()
            backend=Mock();backend.keys.return_value={}
            collector=Collector(backend,self.vision,session,sleep=lambda _:None)
            collector.rewind=Mock(return_value=before)
            collector.stable=Mock(return_value=before)
            collector.click=Mock(return_value=marked)
            collector.scroll=Mock(side_effect=[intermediate,after])
            collector.capture_memory=Mock(side_effect=AssertionError('already captured'))
            collector.recover_page_overlap=Mock(side_effect=AssertionError('anchor should resolve overlap'))
            actual_next_page=collector.next_page
            traversed=[]
            def next_page(image,rows):
                traversed.append(rows)
                if len(traversed)==1:return actual_next_page(image,rows)
                return image,None,0  # End the bounded replay after the next saved page.
            collector.next_page=next_page
            self.assertEqual(collector.run(1),'complete')
            self.assertEqual(len(traversed),2)
            self.assertEqual(session.data['entries'],original_entries)
            collector.click.assert_called_once_with(previous[-1][-1]['point'],'list')
            self.assertEqual(collector.scroll.call_count,2)
            collector.capture_memory.assert_not_called()
            collector.recover_page_overlap.assert_not_called()

    def test_real_contour_jitter_matches_existing_rows_without_widening_threshold(self):
        from PIL import Image
        folder=ROOT/'tests/fixtures/capture'
        previous=json.loads((folder/'list-contour-anchors.json').read_text(encoding='utf8'))
        with Image.open(folder/'list-contour-before.png') as before, Image.open(folder/'list-contour-jitter.png') as image:
            rows=self.vision.list_rows(image)
            anchor=self.vision.selected_list_cell(before,previous)
            current_anchor=self.vision.selected_list_cell(image,rows)
            self.assertEqual(anchor,(2,1))
            self.assertEqual(current_anchor,(1,1))
            self.assertEqual(overlap(previous,rows,9),0)  # original defect
            self.assertEqual(self.vision.page_overlap(image,previous,rows,9,anchor=anchor,current_anchor=current_anchor),2)
            self.assertEqual(self.vision.page_overlap(image,previous,rows,9,anchor=anchor,current_anchor=(1,2)),0)
            changed=copy.deepcopy(previous)
            changed[-1][0]['signature']=[255]*1152
            self.assertEqual(self.vision.page_overlap(image,changed,rows,9),0)
        with Image.open(folder/'list-recovery-too-far.png') as image:
            self.assertEqual(self.vision.page_overlap(image,previous,self.vision.list_rows(image),9),0)

    def test_contour_jitter_does_not_start_recovery_before_new_rows_appear(self):
        from PIL import Image
        from gkshot.capture.runner import Collector
        folder=ROOT/'tests/fixtures/capture'
        previous=json.loads((folder/'list-contour-anchors.json').read_text(encoding='utf8'))
        with Image.open(folder/'list-contour-before.png') as before, Image.open(folder/'list-contour-jitter.png') as after, tempfile.TemporaryDirectory() as directory:
            rows=self.vision.list_rows(after)
            # Replay the real failing landing, then simulate a complete new row.
            # The original two rows must be skipped, never collected a second time.
            expanded=copy.deepcopy(rows)+[copy.deepcopy(previous[0])]
            vision=copy.copy(self.vision)
            vision.is_list=lambda image:True
            vision.list_rows=Mock(side_effect=[rows,expanded])
            vision.selected_list_cell=Mock(side_effect=[(2,1),(1,1),(1,1)])
            session=CaptureSession.create(load_profile(),directory)
            collector=Collector(Mock(),vision,session,sleep=lambda _:None)
            collector.scroll=Mock(return_value=after)
            collector.recover_page_overlap=Mock(side_effect=AssertionError('should keep scrolling'))
            _,found,skip=collector.next_page(before,previous)
            self.assertEqual(collector.scroll.call_count,2)
            collector.recover_page_overlap.assert_not_called()
            self.assertEqual(skip,2)
            self.assertEqual(found,expanded)

    def test_similar_real_rows_resolve_with_persistent_selection_anchor(self):
        from PIL import Image
        folder=ROOT/'tests/fixtures/capture'
        previous=json.loads((folder/'list-similar-anchors.json').read_text(encoding='utf8'))
        with Image.open(folder/'list-similar-before.png') as before,Image.open(folder/'list-similar-after.png') as after:
            current=self.vision.list_rows(after)
            with self.assertRaises(ValueError):overlap(previous,current,9)
            anchor=self.vision.selected_list_cell(before,previous)
            moved=self.vision.selected_list_cell(after,current)
            self.assertEqual(anchor,(1,2))
            self.assertEqual(moved,(1,2))
            self.assertEqual(overlap(previous,current,9,anchor=anchor,current_anchor=moved),2)
            self.assertEqual(self.vision.page_overlap(after,previous,current,9,anchor=anchor,current_anchor=moved),2)
            with self.assertRaises(ValueError):self.vision.page_overlap(after,previous,current,9)
            self.assertEqual(overlap(previous,current,9,anchor=anchor,current_anchor=(0,2)),1)
            self.assertEqual(overlap(previous,current,9,anchor=anchor,current_anchor=(1,1)),0)

    def test_clipped_list_artwork_inner_frame_does_not_break_overlap(self):
        from PIL import Image
        from gkshot.capture.vision import relocate_page
        folder=ROOT/'tests/fixtures/capture'
        anchors=json.loads((folder/'list-inner-frame-anchors.json').read_text(encoding='utf8'))
        with Image.open(folder/'list-inner-frame-overlap.png') as image:
            rows=self.vision.list_rows(image)
            self.assertEqual(len(rows),2)
            self.assertGreater(rows[0][0]['box'][1],.43)
            self.assertEqual(overlap(anchors['previous'],rows,9),1)
            moved=relocate_page(anchors['next'],rows,9)
            self.assertIsNotNone(moved)
            self.assertTrue(self.vision.matches_list_page(image,moved,9))
            self.assertEqual(overlap(anchors['previous'],list(reversed(rows)),9),0)

    def test_actual_five_card_memory_is_a_complete_legal_grid(self):
        from PIL import Image
        with Image.open(ROOT/'tests/fixtures/capture/five-card-memory.png') as image:
            self.assertTrue(self.vision.is_detail_page(image))
            self.assertTrue(self.vision.at_detail_edge(image,bottom=True))
            for width in (image.width,540):
                resized=image.resize((width,round(image.height*width/image.width)))
                cards=self.vision.cards(resized)
                self.assertEqual([c['slot'] for c in cards],list(range(1,6)))
                self.assertTrue(all(c['card_count']==5 for c in cards))

    def test_empty_card_slots_require_heading_bottom_and_blank_space(self):
        from PIL import Image,ImageDraw
        from gkshot.recognition.card_frames import complete_card_grid
        columns=(77,313,549,784)
        heading=lambda im:{'text':'スキルカード','score':.99}
        for count in range(1,6):
            image=Image.new('RGB',(1080,1920),'white')
            boxes=[(columns[i%4],1200+275*(i//4),215,215) for i in range(count)]
            draw=ImageDraw.Draw(image)
            for x,y,w,h in boxes:draw.rectangle((x,y,x+w-1,y+h-1),fill='#444')
            grid=complete_card_grid(image,boxes,at_bottom=True,read_heading=heading)
            self.assertEqual(grid['count'],count)
            self.assertEqual(grid['empty_slots'],list(range(count,6)))
            self.assertIsNone(complete_card_grid(image,boxes,at_bottom=False,read_heading=heading))
            self.assertIsNone(complete_card_grid(image,boxes,at_bottom=True,read_heading=lambda im:[]))
            # A visible card that the detector missed must not become an empty slot.
            x,y=columns[count%4],1200+275*(count//4)
            draw.rectangle((x,y,x+214,y+214),fill='#555')
            self.assertIsNone(complete_card_grid(image,boxes,at_bottom=True,read_heading=heading))

    def test_five_card_offline_import_records_empty_sixth_slot_with_evidence(self):
        from gkshot.memory_library import assess_automatic
        report=self.scanner.scan([ROOT/'tests/fixtures/capture/five-card-memory.png'],client_area=True)
        self.assertEqual(len(report['pages'][0]['cards']),5)
        self.assertEqual(report['draft']['card_ids'][5],0)
        self.assertEqual(report['draft']['customizations'][5],'')
        self.assertNotIn('card_ids.5',report['missing_fields'])
        assess_automatic(report,self.scanner.catalog)
        self.assertNotIn('card_ids.5',report['automatic_check']['unresolved'])
        self.assertNotIn('customizations.5',report['automatic_check']['unresolved'])
        # An explicit zero without visual evidence cannot automatically pass.
        report['pages'][0]['card_grid']=None
        assess_automatic(report,self.scanner.catalog)
        self.assertIn('card_ids.5',report['automatic_check']['unresolved'])
        self.assertIn('customizations.5',report['automatic_check']['unresolved'])

    def test_dark_card_art_is_a_complete_six_card_grid(self):
        from PIL import Image,ImageDraw
        path=ROOT/'tests/fixtures/capture/dark-art-six-cards.png'
        with Image.open(path) as source:
            for width in (source.width,540,405):
                image=source.resize((width,round(source.height*width/source.width)))
                cards=self.vision.cards(image)
                self.assertEqual([c['slot'] for c in cards],list(range(1,7)),width)
                self.assertTrue(self.vision.at_detail_edge(image,bottom=True))
                self.assertTrue(.15<cards[4]['point'][0]<.2)
                self.assertTrue(.80<cards[4]['point'][1]<.85)
            image=self.vision.normalized(source)
        for mode in ('empty','blank-frame','clipped'):
            changed=image.copy();draw=ImageDraw.Draw(changed)
            if mode=='clipped':draw.rectangle((70,1640,300,1700),fill='white')
            else:
                draw.rectangle((70,1465,300,1700),fill='white')
                if mode=='blank-frame':draw.rounded_rectangle((77,1471,294,1688),radius=18,fill='#ddd',outline='#555',width=4)
            self.assertEqual(self.vision.cards(changed),[],mode)

    def test_offline_recognition_uses_same_dark_card_frame_evidence(self):
        path=ROOT/'tests/fixtures/capture/dark-art-six-cards.png'
        page=self.scanner.page(path,client_area=True)
        self.assertEqual([c['slot'] for c in page['cards']],list(range(6)))
        self.assertEqual(page['cards'][4]['id'],773)
        self.assertEqual(page['cards'][4]['label_read']['text'],'エンターテイナー')

    def test_actual_loading_overlay_is_not_stable_or_a_detail_page(self):
        from PIL import Image
        folder=ROOT/'tests/fixtures/capture'
        with Image.open(folder/'list-opening-loading.png') as image:
            self.assertTrue(self.vision.is_loading(image))
            self.assertTrue(self.vision.is_list(image))
            self.assertFalse(self.vision.is_detail_page(image))
        for image in self.images.values():self.assertFalse(self.vision.is_loading(image))
        self.assertTrue(self.vision.is_detail_page(self.images['detail-top.png']))

    def test_actual_resume_drift_relocates_complete_sequence_and_rejects_changes(self):
        from PIL import Image
        from gkshot.capture.vision import relocate_page
        folder=ROOT/'tests/fixtures/capture'
        saved=json.loads((folder/'list-resume-anchors.json').read_text(encoding='utf8'))
        with Image.open(folder/'list-resume-shifted.png') as image:
            current=self.vision.list_rows(image)
            self.assertFalse(self.vision.matches_list_page(image,saved,9))
            fixed=relocate_page(saved,current,9)
            self.assertEqual(len(fixed),2)
            self.assertEqual(len(current),3)
            self.assertTrue(self.vision.matches_list_page(image,fixed,9))
            self.assertLess(fixed[0][0]['box'][1],saved[0][0]['box'][1])
            self.assertIsNone(relocate_page(saved,current[1:],9))
            self.assertIsNone(relocate_page(saved,current[:2]*2,9))
            changed=copy.deepcopy(current)
            changed[0][0]['signature']=changed[1][0]['signature']
            self.assertIsNone(relocate_page(saved,changed,9))
            self.assertIsNone(relocate_page(saved,list(reversed(current)),9))
    @classmethod
    def setUpClass(cls):
        from gkshot.recognition.memory import MemoryScanner
        from gkshot.capture.vision import CaptureVision
        cls.scanner=MemoryScanner();cls.vision=CaptureVision(load_profile())
        cls.fixture=ROOT/"tests/fixtures/memory-library"
        cls.images={name:cls.scanner.normalize(cls.fixture/name)[0] for name in ("list.png","detail-top.png","detail-bottom.png","card-detail-bishitto.png")}

    def test_partial_top_row_does_not_break_pagination_overlap(self):
        from PIL import Image
        from gkshot.capture.runner import Collector
        v=self.vision
        folder=ROOT/"tests/fixtures/capture"
        with Image.open(folder/"list-before-partial-scroll.png") as before, Image.open(folder/"list-partial-top-row.png") as after:
            previous=v.list_rows(before)
            current=v.list_rows(after)
            self.assertEqual([len(row) for row in previous],[3,3])
            self.assertEqual([len(row) for row in current],[3,3])
            self.assertEqual(overlap(previous,current,9),1)
            self.assertGreater(current[0][0]["box"][1],0.43)
            with tempfile.TemporaryDirectory() as directory:
                session=CaptureSession.create(load_profile(),directory)
                collector=Collector(Mock(),v,session,sleep=lambda _:None)
                # These historical layout fixtures selected different memories
                # between shots; they are not a continuous selection track.
                collector.vision=copy.copy(v)
                collector.vision.selected_list_cell=None
                collector.scroll=Mock(return_value=after)
                _,rows,skip=collector.next_page(before,previous)
                self.assertEqual(skip,1)
                self.assertEqual(len(rows)-skip,1)
                # An actual gap/replacement still cannot claim overlap.
                wrong=copy.deepcopy(previous)
                for i,row in enumerate(wrong):
                    for tile in row:tile["signature"]=[i*255]*len(tile["signature"])
                collector.backend.keys.return_value={}
                collector.stable=Mock(return_value=after)
                with self.assertRaisesRegex(CapturePaused,"重叠行"):
                    collector.next_page(before,wrong)
                self.assertEqual(collector.scroll.call_count,6)  # good page + failed scroll + four rollback moves

    def test_green_art_and_cost_fringe_do_not_trigger_detail_collection(self):
        from PIL import Image
        v=self.vision
        folder=ROOT/"tests/fixtures/capture"
        for name in ("overview-green-art.png","overview-green-fringe.png"):
            with Image.open(folder/name) as source:
                for width in (source.width,540):
                    image=source.resize((width,round(source.height*width/source.width)))
                    cards=v.cards(image)
                    decisions=[v.customization_badge(image,c) for c in cards]
                    self.assertEqual([d["state"] for d in decisions],
                                     ["absent","absent","absent","present","absent","absent"],name)
                    self.assertGreater(decisions[3]["green_ring_ratio"],0.7)

    def test_anchored_page_matching_covers_missing_edge_row_and_rejects_changes(self):
        from PIL import Image,ImageChops
        v=self.vision
        folder=ROOT/"tests/fixtures/capture"
        with Image.open(folder/"list-before-middle-selection.png") as before, Image.open(folder/"list-last-row-selected.png") as after:
            rows=v.list_rows(before)
            self.assertTrue(v.matches_list_page(after,rows,9))
            self.assertTrue(v.selection_matches(after,rows[2][1]))
            changed=copy.deepcopy(rows)
            changed[2][1]["signature"]=changed[1][1]["signature"]
            self.assertFalse(v.matches_list_page(after,changed,9))
            self.assertFalse(v.matches_list_page(ImageChops.offset(after,0,40),rows,9))
            changed=copy.deepcopy(rows);changed[0][0]["box"]=[-1,0,1,1]
            self.assertFalse(v.matches_list_page(after,changed,9))
            self.assertFalse(v.matches_list_page(after,[],9))

    def test_detail_scrollbar_endpoints_on_real_images(self):
        v=self.vision
        top=self.images["detail-top.png"]
        bottom=self.images["detail-bottom.png"]
        self.assertTrue(v.at_detail_edge(top))
        self.assertFalse(v.at_detail_edge(top,bottom=True))
        self.assertTrue(v.at_detail_edge(bottom,bottom=True))
        self.assertFalse(v.at_detail_edge(bottom))

    def test_middle_selection_preserves_all_rows_and_resume_matching(self):
        from PIL import Image
        v=self.vision
        folder=ROOT/"tests/fixtures/capture"
        with Image.open(folder/"list-before-middle-selection.png") as before, Image.open(folder/"list-middle-selected.png") as after:
            saved_rows=v.list_rows(before)
            current_rows=v.list_rows(after)
            self.assertEqual([len(row) for row in current_rows],[3,3,3])
            self.assertTrue(pages_match(saved_rows,current_rows,9))
            self.assertTrue(pages_match(current_rows,saved_rows,9))
            self.assertTrue(v.selection_matches(after,saved_rows[1][1]))
            self.assertEqual([i for i,tile in enumerate(t for row in current_rows for t in row)
                              if v.selection_matches(after,tile)],[4])
            # Recovering a border must not accept reordered or missing memories.
            swapped=copy.deepcopy(current_rows)
            swapped[1][0]["signature"],swapped[1][1]["signature"]=swapped[1][1]["signature"],swapped[1][0]["signature"]
            self.assertFalse(pages_match(saved_rows,swapped,9))
            self.assertFalse(pages_match(saved_rows,current_rows[:1]+current_rows[2:],9))

    def test_real_capture_badge_selection_across_window_sizes(self):
        from PIL import Image
        image=Image.open(ROOT/"tests/fixtures/capture/overview.png").convert("RGB")
        for source in (image,self.images["detail-bottom.png"]):
            for width in (source.width,540,405):
                scaled=source.resize((width,round(source.height*width/source.width)))
                cards=self.vision.cards(scaled)
                self.assertEqual(len(cards),6)
                decisions=[self.vision.customization_badge(scaled,c)["state"] for c in cards]
                if width>=540:
                    self.assertEqual(decisions,["absent"]*4+["present","absent"])
                else:
                    self.assertEqual(decisions,["unknown"]*6)
        cards=self.vision.cards(image)
        grey=image.convert("L").convert("RGB")
        self.assertTrue(all(self.vision.customization_badge(grey,c)["state"]=="unknown" for c in cards))

    def test_real_header_covering_popup_is_detected_without_binding_early(self):
        from PIL import Image
        from gkshot.capture.runner import Collector
        from gkshot.capture.vision import signature
        overview=Image.open(ROOT/"tests/fixtures/capture/overview.png")
        popup=Image.open(ROOT/"tests/fixtures/capture/header-occluded.png")
        with tempfile.TemporaryDirectory() as directory:
            session=CaptureSession.create(load_profile(),directory)
            collector=Collector(Mock(),self.vision,session)
            header=signature(overview,session.data["profile"]["header_region"])
            with self.assertRaises(CapturePaused):collector.same_header(popup,header)
            self.assertIsNotNone(self.vision.tooltip(popup))
            collector.same_header(overview,header)

    def test_real_fixture_layout_and_offline_popup_detection(self):
        v=self.vision
        image=self.images["list.png"]
        self.assertTrue(v.is_list(image));self.assertTrue(v.at_list_start(image));self.assertFalse(v.at_list_end(image))
        rows=v.list_rows(image)
        self.assertEqual([len(row) for row in rows],[3,3,3])
        with patch.object(socket.socket,"connect",side_effect=AssertionError("Network forbidden")):
            self.assertTrue(v.production_only(image,rows[0][0]))
            self.assertTrue(v.production_only(image,rows[0][1]))
            self.assertFalse(v.production_only(image,rows[0][2]))
            popup=v.tooltip(self.images["card-detail-bishitto.png"])
        self.assertIsNotNone(popup)
        self.assertTrue(v.is_detail(self.images["detail-top.png"]))
        self.assertEqual(v.cards(self.images["detail-top.png"]),[])
        self.assertEqual([c["slot"] for c in v.cards(self.images["detail-bottom.png"])],list(range(1,7)))

    def test_disabled_details_button_is_still_list_and_selection_is_verified(self):
        from PIL import Image,ImageDraw
        v=self.vision
        disabled=Image.open(ROOT/"tests/fixtures/capture/list-disabled.png").convert("RGB")
        self.assertLess(v.orange(disabled,v.profile["list_button_region"]),0.12)
        with patch.object(socket.socket,"connect",side_effect=AssertionError("Network forbidden")):
            self.assertTrue(v.is_list(disabled))
        self.assertFalse(v.is_detail(disabled))
        tiles=[t for row in v.list_rows(disabled) for t in row]
        self.assertEqual([i for i,t in enumerate(tiles) if v.selection_matches(disabled,t)],[0])
        enabled=self.images["list.png"]
        tiles=[t for row in v.list_rows(enabled) for t in row]
        self.assertEqual([i for i,t in enumerate(tiles) if v.selection_matches(enabled,t)],[2])
        self.assertFalse(v.is_list(self.images["detail-top.png"]))
        fake=Image.new("RGB",disabled.size,"orange")
        self.assertFalse(v.is_list(fake))

    def test_production_only_small_kana_ocr_on_real_selected_thumbnail(self):
        from PIL import Image
        v=self.vision
        image=Image.open(ROOT/"tests/fixtures/capture/production-only-selected.png").convert("RGB")
        tiles=[t for row in v.list_rows(image) for t in row]
        self.assertTrue(v.selection_matches(image,tiles[0]))
        self.assertFalse(v.details_enabled(image))
        with patch.object(socket.socket,"connect",side_effect=AssertionError("Network forbidden")):
            self.assertEqual([v.production_only(image,t) for t in tiles[:3]],[True,True,False])
        for text,score in (("専用",0.99),("プロデユース専用",0.5),("プロデュース振り返り",0.99)):
            with patch.object(v,"read",return_value=[{"text":text,"score":score}]):
                self.assertFalse(v.production_only(image,tiles[0]))

    def test_capture_can_be_reprocessed_without_rewriting_raw_files(self):
        from gkshot.capture.process import process_session
        from gkshot.recognition.card_detail import CardDetailScanner
        with tempfile.TemporaryDirectory() as directory:
            session=CaptureSession.create(load_profile(),directory)
            entry=session.entry(0,0);entry["attempt"]=1
            for name,role,slot in (("detail-top.png","overview-top",None),("detail-bottom.png","overview-cards",None),("card-detail-bishitto.png","card-tooltip",5)):
                session.image(self.images[name],role,entry=entry,slot=slot,metadata={"attempt":1})
            # Older collectors unnecessarily opened ordinary cards. A stale
            # tooltip must not create an error for a clear absent counter.
            session.image(self.images["card-detail-bishitto.png"],"card-tooltip",entry=entry,slot=6,metadata={"attempt":1})
            entry["status"]="needs-recapture";session.save()
            original=session.path.read_bytes()
            scanner=CardDetailScanner(self.scanner)
            result=process_session(session.root,memory_scanner=self.scanner,detail_scanner=scanner)
            self.assertEqual(result["results"][0]["status"],"processed")
            self.assertIn({"slot":6,"detail_status":"skipped-no-customization"},result["results"][0]["card_details"])
            from gkshot.memory_library import MemoryLibrary
            records=MemoryLibrary(result["library_directory"]).list()
            self.assertEqual(records[0]["report"]["draft"]["customizations"][4],"106x1")
            self.assertEqual(records[0]["report"]["draft"]["customizations"],["", "", "", "", "106x1", ""])
            self.assertEqual(records[0]["status"],"auto-resolved")
            self.assertEqual(session.path.read_bytes(),original)
            # A new processing run is an independent result set, not another owned copy in the same library.
            again=process_session(session.root,memory_scanner=self.scanner,detail_scanner=scanner)
            self.assertNotEqual(result["library_directory"],again["library_directory"])
            self.assertEqual(len(MemoryLibrary(result["library_directory"]).list()),1)
            for image in entry["images"]:self.assertTrue(session.source(image).exists())
            with patch.object(scanner,"scan",return_value={"card_id":None,"status":"needs-detail"}):
                unclear=process_session(session.root,memory_scanner=self.scanner,detail_scanner=scanner)
            self.assertEqual(unclear["results"][0]["status"],"processed-with-issues")
            self.assertIn("卡名未可靠识别",unclear["results"][0]["errors"][0]["error"])

    def test_monochrome_thumbnail_is_not_treated_as_an_empty_slot(self):
        image=self.images["list.png"].convert("L").convert("RGB")
        self.assertEqual([len(row) for row in self.vision.list_rows(image)],[3,3,3])

    def test_capture_window_without_game_or_network(self):
        import tkinter as tk
        from gkshot.capture.gui import CaptureWindow
        root=tk.Tk();root.withdraw()
        try:
            with patch("gkshot.capture.gui.list_game_windows",return_value=[]):
                window=CaptureWindow(root)
                self.assertFalse(window.busy)
                self.assertEqual(window.windows,[])
                self.assertIn("未找到",window.log.get("1.0","end"))
        finally:root.destroy()
