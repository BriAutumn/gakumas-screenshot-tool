import copy,hashlib,json,re,socket,sys,tempfile,shutil
from pathlib import Path
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from gkshot.catalog import Catalog
from gkshot.recognition.catalog_candidates import candidates,compile_card,load_tree,Unsupported
FIXTURES=ROOT/'tests/fixtures/catalog-candidates'

class CatalogCandidateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.catalog=Catalog();cls.tree=load_tree()

    def test_condition_patch_preserves_trigger_body_and_appended_genki(self):
        states={s['token']:s['effects'] for s in candidates(521,self.catalog,self.tree)}
        a=states[''];b=states['88x1'];joint=states['88x1e27x1']
        self.assertEqual(a['actions'][1]['conditions'][0]['right']['value'],15)
        self.assertEqual(b['actions'][1]['conditions'][0]['right']['value'],10)
        self.assertEqual(a['actions'][1]['effects'],b['actions'][1]['effects'])
        self.assertEqual(joint['actions'][0]['actions'][0]['rhs']['value'],2)
        self.assertEqual(joint['actions'][2]['actions'][0]['rhs']['value'],4)
        self.assertEqual(self.tree['cards']['521']['actions'][1]['conditions'][0]['right']['value'],15)

    def test_multilevel_growth_is_total_and_not_cumulative(self):
        cases={r['token']:r for r in compile_card(111,self.catalog,self.tree)}
        self.assertIn('好調6ターン',cases['52x2']['pattern'])
        self.assertNotIn('好調7ターン',cases['52x2']['pattern'])

    def test_generated_words_distinguish_condition_append_and_extra_text(self):
        cases=compile_card(521,self.catalog,self.tree)
        text='夢色リップ+元気+2やる気+5スコア上昇量増加20%好印象が10以上の場合、以降の4ターンの間、ターン開始時、好印象1.1倍元気+4重複不可試験・ステージ中1回'
        def matches(t):return [c['token'] for c in cases if re.fullmatch(c['pattern'],t)]
        self.assertEqual(matches(text),['88x1e27x1'])
        self.assertEqual(matches(text.replace('元気+2','元気+6').replace('元気+4','')),[])
        self.assertEqual(matches(text+'やる気+1'),[])
        self.assertEqual(matches(text.replace('好印象1.1倍','')),[])
        # Both UI wordings denote the same full-power resource, not score.
        stance=compile_card(411,self.catalog,self.tree)
        body='始まりの笑顔+全力値消費1強気2段階目に変更スコア+8消費体力減少2ターン次のターン、スキルカードを引く重複不可'
        self.assertEqual([c['token'] for c in stance if re.fullmatch(c['pattern'],body)],['40x1'])
        self.assertEqual([c['token'] for c in stance if re.fullmatch(c['pattern'],body.replace('全力値消費1','スコア消費1'))],[])

    def test_unknown_growth_or_trigger_cannot_be_ignored(self):
        for field,row in [('effects',{'phase':'prestage','targets':['other'],'actions':[]}),('actions',{'phase':'unknown','actions':[]})]:
            tree=copy.deepcopy(self.tree);tree['cards']['99'][field].append(row)
            with self.assertRaises(Unsupported):compile_card(99,self.catalog,tree)

    def test_usable_condition_and_complete_upgrade_hand_text(self):
        cases=compile_card(517,self.catalog,self.tree)
        text='デイドリーミング+体力消費3元気が30以上の場合、使用可元気+6以降の4ターンの間、ターン開始時、元気の95%分スコア上昇重複不可試験・ステージ中1回'
        self.assertEqual([c['token'] for c in cases if re.fullmatch(c['pattern'],text)],['86x3'])
        for bad in (text.replace('元気が30以上の場合、使用可',''),text.replace('95%','94%')):
            self.assertFalse(any(re.fullmatch(c['pattern'],bad) for c in cases))
        cases=compile_card(87,self.catalog,self.tree)
        text='シュプレヒコール+集中消費2スコア+6好調3ターンスキルカード使用数追加+1手札をすべて試験・ステージ中強化重複不可'
        self.assertEqual([c['token'] for c in cases if re.fullmatch(c['pattern'],text)],['39x1'])

    def test_repeated_scores_cannot_lose_the_repeat_count(self):
        from gkshot.recognition.catalog_candidates import effects
        a={'type':'assignment','lhs':'score','op':'+=','rhs':{'type':'number','value':25}}
        pattern=effects([{'actions':[a,a]}],{})
        self.assertIsNotNone(re.fullmatch(pattern,'スコア+25(2回)'))
        self.assertIsNone(re.fullmatch(pattern,'スコア+25'))

    def test_adjacent_scores_after_stance_retain_count_and_order(self):
        from gkshot.recognition.catalog_candidates import effects
        stance={'type':'call','name':'setStance','target':None,'args':[{'type':'identifier','name':'strength'}]}
        score={'type':'assignment','lhs':'score','op':'+=','rhs':{'type':'number','value':7}}
        pattern=effects([{'actions':[stance,score,score]}],{})
        self.assertIsNotNone(re.fullmatch(pattern,'強気に変更スコア+7(2回)'))
        for text in ('強気に変更スコア+7','強気に変更スコア+7(3回)','スコア+7(2回)強気に変更'):
            self.assertIsNone(re.fullmatch(pattern,text))

    def test_new_effect_units_and_state_condition_are_exact(self):
        from gkshot.recognition.catalog_candidates import action,condition
        def assignment(key,value):
            return {'type':'assignment','lhs':key,'op':'+=','rhs':{'type':'number','value':value}}
        self.assertEqual(action(assignment('fixedStamina',5),{}),'体力回復5')
        self.assertEqual(action(assignment('doubleCostTurns',2),{}),'消費体力増加2ターン')
        condition_tree={'type':'comparison','op':'>=','left':{'type':'identifier','name':'goodConditionTurns'},'right':{'type':'number','value':1}}
        self.assertIsNotNone(re.fullmatch(condition(condition_tree),'好調状態の場合、'))
        condition_tree['right']['value']=2
        self.assertIsNone(re.fullmatch(condition(condition_tree),'好調状態の場合、'))
        buff={'type':'call','name':'setGoodImpressionTurnsEffectBuff','target':None,'args':[{'type':'number','value':.1}]}
        self.assertIsNotNone(re.fullmatch(action(buff,{}),'好印象強化+10%'))
        self.assertIsNone(re.fullmatch(action(buff,{}),'好印象強化+1%'))

    def test_unknown_patch_anchor_and_unsupported_state_refuse_whole_card(self):
        tree=copy.deepcopy(self.tree);tree['customizations']['88']['actions'][0]['anchor']='missing'
        with self.assertRaises(Unsupported):list(candidates(521,self.catalog,tree))
        tree=copy.deepcopy(self.tree);tree['customizations']['27']['actions'][0]['effect']['actions'][0]['lhs']='unknown'
        with self.assertRaises(Unsupported):compile_card(521,self.catalog,tree)

    def test_promoted_rules_are_reproducible_from_catalog(self):
        rules=json.loads((ROOT/'configs/recognition/card-detail-rules.json').read_text(encoding='utf8'))
        promoted=[r for r in rules['rules'] if r.get('generated_from')]
        self.assertTrue({99,167,297}.issubset({r['card_id'] for r in promoted}))
        for r in promoted:self.assertEqual(r['complete_text_cases'],compile_card(r['card_id'],self.catalog,self.tree))

    def test_tampered_derived_tree_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            shutil.copy2(ROOT/'assets/recognition/manifest.json',root/'manifest.json')
            (root/'catalog-effects.json').write_text('{}',encoding='utf8')
            with self.assertRaises(ValueError):load_tree(root)

    def test_raw_images_and_low_confidence_remains_unknown(self):
        from gkshot.recognition.card_detail import CardDetailScanner,rule_fingerprint
        with patch.object(socket.socket,'connect',side_effect=AssertionError('offline only')):
            scanner=CardDetailScanner()
            # Isolate the bare candidate matcher: without glyph evidence the
            # low-confidence line must still be refused after production promotion.
            scanner.profiles['rules']=[r for r in scanner.profiles['rules'] if r['card_id']!=521]
            card=self.catalog.skill_cards[521]
            scanner.profiles['rules'].append(dict(card_id=521,min_line_score=.7,complete_text_cases=compile_card(521,self.catalog,self.tree),
                catalog_fingerprint=rule_fingerprint(card,[self.catalog.customizations[88],self.catalog.customizations[27]])))
            for row in json.loads((FIXTURES/'provenance.json').read_text(encoding='utf8')):
                with self.subTest(ordinal=row['ordinal']):
                    for key in ('tooltip','owner'):
                        self.assertEqual(hashlib.sha256((FIXTURES/row[key]).read_bytes()).hexdigest(),row[key+'_sha256'])
                    d=scanner.scan(FIXTURES/row['tooltip'],expected_card_id=row['card_id'],client_area=True,owner_path=FIXTURES/row['owner'])
                    self.assertEqual(d['token'],row['token'] if row['expected_status']=='matched' else None)
                    self.assertEqual(d['status'],row['expected_status'])
