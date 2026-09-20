"""Finite-state raw collection with conservative navigation and replayable checkpoints."""
import copy
import time
from threading import Event

from gkshot.capture.store import now
from gkshot.capture.vision import signature, distance, pages_match, overlap, validate_profile, relocate_page, AmbiguousOverlap
from gkshot.capture.windows import CapturePaused


class MemoryCaptureIncomplete(CapturePaused):
    """A local capture failure; continue only after recovering the same list page."""


class CaptureWaitTimeout(CapturePaused):
    """A bounded UI wait expired, rather than a user/focus/input interruption."""


class Collector:
    def __init__(self, backend, vision, session, *, notify=None, pause=None, stop=None, sleep=time.sleep, all_details=False):
        self.backend, self.vision, self.session = backend, vision, session
        self.profile = validate_profile(session.data["profile"])
        def report(message):
            self.session.log(message)
            if notify:notify(message)
        self.notify = report
        self.pause, self.stop = pause or Event(), stop or Event()
        self.sleep = sleep
        self.all_details = all_details
        self.active_entry = None
        self.active_page_index = None

    def checkpoint(self, page_index, cell_index=None, phase='list'):
        self.active_page_index=page_index
        self.session.data['checkpoint']={'page_index':page_index,'cell_index':cell_index,
                                         'phase':phase,'updated_at':now()}
        self.session.save()

    def page_finished(self, index, *, retry_issues=False):
        page=self.session.data['pages'][index]
        done={'captured','skipped-production-only'}
        if not retry_issues:done.update(('needs-recapture','skipped-incomplete'))
        entries={(e['page_index'],e['cell_index']):e for e in self.session.data['entries']}
        cell=0
        for ri,row in enumerate(page['rows']):
            for tile in row:
                if ri>=page['skip_rows'] and entries.get((index,cell),{}).get('status') not in done:
                    return False
                cell+=1
        return True

    def saved_rows_match(self, image, saved, current):
        tolerance=self.profile['list_match_tolerance']
        if len(saved)!=len(current) or not current:return False
        matched=pages_match(saved,current,tolerance)
        if not matched and hasattr(self.vision,'page_overlap'):
            matched=self.vision.page_overlap(image,saved,current,tolerance,full_page=True)==len(saved)
        return matched and self.vision.matches_list_page(image,current,tolerance)

    def locate_saved_page(self, image, rows, *, retry_issues=False):
        """Find complete saved row sequences at the current scroll position.

        Overlapping saved pages can describe the same physical position. Their
        global row origins must agree; identical art at different positions is
        ambiguous and must not be silently assigned to the latest checkpoint.
        """
        pages=self.session.data['pages']; tolerance=self.profile['list_match_tolerance']
        candidates=[]; origin=0
        for index,page in enumerate(pages):
            if index:origin+=len(pages[index-1]['rows'])-page['skip_rows']
            for start in range(len(rows)-len(page['rows'])+1):
                matched=rows[start:start+len(page['rows'])]
                if self.saved_rows_match(image,page['rows'],matched):
                    candidates.append((origin-start,index,matched))
            # Detection can miss an edge contour while the old coordinates
            # still verify every saved tile. Keep that established fallback.
            if 0<len(rows)<len(page['rows']):
                for offset in range(len(page['rows'])-len(rows)+1):
                    if (pages_match(page['rows'][offset:offset+len(rows)],rows,tolerance)
                            and self.vision.matches_list_page(image,page['rows'],tolerance)):
                        candidates.append((origin+offset,index,page['rows']))
        if not candidates or len({c[0] for c in candidates})!=1:
            return None
        unfinished=[c for c in candidates if not self.page_finished(c[1],retry_issues=retry_issues)]
        chosen=min(unfinished,key=lambda c:c[1]) if unfinished else max(candidates,key=lambda c:c[1])
        return chosen[1],copy.deepcopy(chosen[2])

    def resume_position(self, *, from_top=False, retry_issues=False):
        pages=self.session.data['pages']
        if from_top or not pages:
            image=self.rewind()
            return image,0,self.vision.list_rows(image),0
        self.notify('正在从当前画面定位断点；不会自动回到列表顶部……')
        image=self.return_to_list(self.stable('list'))
        # Search a small neighbourhood only. A failed local check does not
        # silently trigger a full replay through hundreds of owned memories.
        moves=[.5]*4+[-.5]*8
        for step in range(len(moves)+1):
            self.check()
            rows=self.vision.list_rows(image)
            found=self.locate_saved_page(image,rows,retry_issues=retry_issues)
            if found:
                index,matched=found
                self.notify(f'已定位到第 {index+1} 个保存页面，直接续采；已完成条目自动略过。')
                return image,index,matched,pages[index]['skip_rows']
            # After an interrupted page turn, first recover a complete saved
            # page nearby. A partial/ambiguous tail alone cannot create IDs.
            if step==len(moves):break
            if step==0:self.notify('当前画面未完整露出保存页，正在断点附近小范围定位……')
            image=self.scroll(self.profile['list_scroll'],moves[step],'list')
            self.require_list(image)
        raise CapturePaused('续采校验失败：断点附近未找到唯一对应页面，进度已保留。请回到暂停附近的列表位置，或勾选「从顶部重新核对」；不会自动从头遍历。')

    def check(self):
        keys = self.backend.keys()
        if self.stop.is_set() or keys.get("stop"):
            raise CapturePaused("已停止；原图与进度已保留，可以续采。")
        if self.pause.is_set() or keys.get("pause"):
            raise CapturePaused("已暂停；原图与进度已保留。")

    def wait(self, seconds):
        # Frequent checks make F8/F9 responsive even while waiting for game animation.
        for _ in range(max(1, round(seconds/0.05))):
            self.check()
            self.sleep(0.05)

    def stable(self, kind, *, ready=None, timeout=None):
        region = self.profile["list_region"] if kind == "list" else self.profile["detail_region"]
        previous, count = None, 0
        timeout = timeout or self.profile["stable_timeout"]
        started = time.monotonic()
        loading = False
        for _ in range(round(timeout/0.2)):
            if time.monotonic()-started >= timeout:
                break
            self.check()
            image = self.backend.screenshot()
            loading = self.vision.is_loading(image)
            if loading or (ready is not None and not ready(image)):
                previous, count = None, 0
                self.wait(0.2)
                continue
            current = signature(image, region, (96, 64))
            count = count+1 if previous is not None and distance(previous, current) < 1.5 else 0
            if count >= 2:
                return image
            previous = current
            self.wait(0.2)
        if ready is not None:
            if loading:raise CapturePaused('游戏仍在 LOADING，已保存进度；等待加载完成后续采。')
            raise CaptureWaitTimeout(f"等待记忆详情超时（{timeout} 秒），本次未确认打开详情。")
        if loading:
            raise CapturePaused("游戏仍在 LOADING，已保存进度；加载完成后可继续原会话。")
        raise CaptureWaitTimeout("画面在等待时间内未稳定。")

    def click(self, point, kind, *, ready=None, timeout=None):
        self.check()
        self.backend.click(point)
        self.wait(self.profile["settle_seconds"])
        if ready is not None:
            return self.stable(kind,ready=ready,timeout=timeout)
        return self.stable(kind)

    def scroll(self, point, notches, kind):
        self.check()
        self.backend.scroll(point, notches)
        self.wait(self.profile["settle_seconds"])
        return self.stable(kind)

    def scroll_detail(self, notches, pulses=1):
        # Several separate wheel events work even if the game caps each event's
        # delta. Wait for settling once per burst rather than after every pulse.
        started = time.perf_counter()
        for i in range(pulses):
            self.check()
            self.backend.scroll(self.profile["detail_scroll"], notches)
            if i+1 < pulses:
                self.wait(0.05)
        self.wait(0.15)
        image = self.stable("detail")
        if self.active_entry is not None:
            self.active_entry["attempts"][-1].setdefault("detail_scrolls", []).append({
                "notches_per_pulse": notches, "pulses": pulses,
                "seconds": round(time.perf_counter()-started, 3)})
        return image

    def require_list(self, image):
        if not self.vision.is_list(image):
            if self.vision.is_detail(image):
                raise CapturePaused("当前仍在记忆详情页。请点底部双箭头返回 memory 列表，再续采；不需要新建会话。")
            raise CapturePaused("未识别到 memory 列表的缩略图和『詳細を見る』按钮，已保存诊断图。请返回独立 memory 列表后续采。")

    def require_detail(self, image):
        if not self.vision.is_detail(image):
            raise MemoryCaptureIncomplete("未确认处于记忆的『ステージ』详情页。")

    def return_to_list(self, image, rows=None):
        """Return only from a recognized memory detail, then verify the list."""
        dismissed=False
        for attempt in range(3):
            self.check()
            if self.vision.is_loading(image):
                image=self.stable('detail')
            if self.vision.is_list(image):
                if rows is not None and not self.vision.matches_list_page(image,rows,self.profile['list_match_tolerance']):
                    raise CapturePaused('自动返回后无法对应原列表，进度和原图已保留。')
                return image
            if attempt==2:break
            if not self.vision.is_detail_page(image):
                raise CapturePaused('无法确认当前页面，未执行返回；原图和进度已保留。')
            if not dismissed and self.vision.tooltip(image):
                image=self.click(self.profile['dismiss_tooltip'],'detail')
                dismissed=True
            check_return=getattr(self.vision,'can_return_to_list',self.vision.is_detail_page)
            if not check_return(image):
                raise CapturePaused('未确认记忆详情的返回入口，未继续点击；原图和进度已保留。')
            self.session.image(image,'automatic-return',entry=self.active_entry,
                               metadata={'attempt':self.active_entry['attempt'] if self.active_entry else None,
                                         'return_try':attempt+1,'page_index':self.active_page_index})
            self.notify(f'检测到记忆详情，正在自动返回列表（{attempt+1}/2）……')
            image=self.click(self.profile['back'],'list')
        raise CapturePaused('已尝试自动返回列表两次，但仍未回到列表；原图和进度已保留。')

    def rewind(self):
        image = self.return_to_list(self.stable("list"))
        self.notify("正在回到列表顶部；续采时会核对已经经过的页面……")
        unchanged = 0
        for _ in range(self.profile["max_scroll_steps"]):
            before = signature(image, self.profile["list_region"], (96,64))
            image = self.scroll(self.profile["list_scroll"], 6, "list")
            self.require_list(image)
            unchanged = unchanged+1 if distance(before, signature(image,self.profile["list_region"],(96,64))) < 1.5 else 0
            if unchanged >= 2:
                if self.vision.at_list_start(image):
                    return image
                raise CapturePaused("列表没有继续向上滚动，但未确认滚动条在顶部；请检查标定，避免从中间漏采。")
        raise CapturePaused("未能确认列表顶部，请调整列表滚动点后重新开始。")

    def next_page(self, image, previous):
        tolerance = self.profile["list_match_tolerance"]
        selection = getattr(self.vision,'selected_list_cell',None) or (lambda image,rows:None)
        anchor = selection(image,previous)
        if (anchor is None or anchor[0]!=len(previous)-1) and callable(getattr(self.vision,'selected_list_cell',None)):
            # A self-match before scrolling cannot predict ambiguity after a
            # different contour landing. This also applies during resume, when
            # skipping captured entries leaves the old selection offscreen.
            self.require_list(image)
            if not self.vision.matches_list_page(image,previous,tolerance):
                raise CapturePaused('建立翻页定位标记前，列表位置改变；已保留进度。')
            for tile in reversed(previous[-1]):
                if self.vision.production_only(image,tile):
                    continue
                image = self.click(tile['point'],'list')
                image = self.return_to_list(image,previous)
                if not self.vision.matches_list_page(image,previous,tolerance):
                    raise CapturePaused('建立翻页定位标记时，列表位置改变；已保留进度。')
                anchor = selection(image,previous)
                if anchor == (len(previous)-1,tile['column']):
                    self.session.image(image,'list-navigation-anchor',metadata={'anchor':anchor})
                    self.notify('已用末行选中框定位，继续翻页。')
                    break
        unchanged = 0
        for _ in range(18):
            before = signature(image,self.profile["list_region"],(96,64))
            image = self.scroll(self.profile["list_scroll"], -1, "list")
            if not self.vision.is_list(image):image=self.return_to_list(image,previous)
            self.require_list(image)
            unchanged = unchanged+1 if distance(before,signature(image,self.profile["list_region"],(96,64))) < 1.5 else 0
            rows = self.vision.list_rows(image)
            current_anchor = selection(image,rows)
            anchor_moved = anchor is not None and current_anchor is not None and anchor!=current_anchor
            if rows and (anchor_moved or not self.vision.matches_list_page(image, previous, tolerance)):
                try:
                    count = self.page_overlap(image,previous,rows,anchor,current_anchor)
                except AmbiguousOverlap:
                    return self.recover_page_overlap(image,previous,anchor=anchor)
                if count and len(rows) > count:
                    return image, rows, count
                if not count:
                    return self.recover_page_overlap(image,previous,anchor=anchor)
            if unchanged >= 3:
                if self.vision.at_list_end(image):
                    return image, None, 0
                raise CapturePaused("列表滚动没有继续，但尚未确认滚动条到底；请检查标定。已保存进度。")
        raise CapturePaused("未能定位下一批完整缩略图，已暂停。")

    def page_overlap(self, image, previous, rows, anchor=None, current_anchor=None):
        if hasattr(self.vision,'page_overlap'):
            return self.vision.page_overlap(image,previous,rows,self.profile['list_match_tolerance'],
                                            anchor=anchor,current_anchor=current_anchor)
        return overlap(previous,rows,self.profile['list_match_tolerance'],anchor=anchor,current_anchor=current_anchor)

    def recover_page_overlap(self, image, previous, *, anchor=None):
        """Search locally with small wheel deltas; never guess missing row IDs."""
        self.session.image(image,'diagnostic-overlap',metadata={'reason':'overlap-not-found','page_index':self.active_page_index,'anchor':anchor})
        self.notify('翻页暂未对上重叠行，正在小幅回滚重新定位……')

        def inspect(image):
            self.require_list(image)
            rows = self.vision.list_rows(image)
            selection = getattr(self.vision,'selected_list_cell',None) or (lambda image,rows:None)
            try:
                count = self.page_overlap(image,previous,rows,anchor,selection(image,rows)) if rows else 0
            except AmbiguousOverlap:
                count = 0  # Try another landing instead of stopping at first ambiguity.
            self.session.image(image,'overlap-search',metadata={'rows':len(rows),'overlap':count,
                                                               'anchor':anchor,'current_anchor':selection(image,rows)})
            return rows,count

        # First re-read a stable frame, then backtrack at most two wheel notches.
        image = self.stable('list')
        for step in range(5):
            rows,count = inspect(image)
            if count and len(rows)>count:
                self.notify('已重新确认重叠行，继续采集。')
                return image,rows,count
            if count:
                break  # Previous tail is visible; approach new rows slowly.
            if step<4:
                image = self.scroll(self.profile['list_scroll'],.5,'list')
        # Only approach forward after re-establishing the old tail. Some games
        # treat fractional wheel deltas as full steps; blind retries run away.
        for _ in range(8 if count else 0):
            image = self.scroll(self.profile['list_scroll'],-.25,'list')
            rows,count = inspect(image)
            if count and len(rows)>count:
                self.notify('已重新确认重叠行，继续采集。')
                return image,rows,count
            if not count:
                break
        raise CapturePaused('翻页已自动回滚重试，仍无法唯一确认重叠行；原图和进度已保留。请检查列表排序、筛选或遮挡后续采。')

    def same_header(self, image, header):
        if distance(signature(image,self.profile["header_region"]), header) > 8:
            raise MemoryCaptureIncomplete("关闭弹框后的头像/姓名与本份记忆不一致，或页头仍被遮挡。")

    def skip_incomplete(self, entry, rows, error):
        # Never catch native input/focus failures or user pause/stop as a skip.
        # Unverified images stay archived but this attempt is excluded from import.
        self.check()
        image = self.backend.screenshot()
        self.session.image(image, "diagnostic-incomplete", entry=entry,
                           metadata={"attempt": entry["attempt"], "reason": str(error)})
        entry["issues"].append({"reason": "capture-incomplete", "message": str(error)})
        self.session.save()
        image = self.return_to_list(self.stable('detail'),rows)
        entry["status"] = "skipped-incomplete"
        entry["attempts"][-1].update(finished_at=now(), outcome="skipped-incomplete", reason=str(error))
        self.session.event("running", f"第 {entry['ordinal']} 份跳过待补：{error}")
        self.session.export_manifest()
        self.notify(f"第 {entry['ordinal']} 份跳过待补：{error} 原图已保存，继续下一份。")
        return image

    def capture_with_retry(self, entry, tile, image, rows):
        for retry in range(2):
            try:
                return self.capture_memory(entry,tile,image,rows)
            except (MemoryCaptureIncomplete,CaptureWaitTimeout) as error:
                if retry==1:return self.skip_incomplete(entry,rows,error)
                self.check()
                self.session.image(self.backend.screenshot(),'diagnostic-retry',entry=entry,
                                   metadata={'attempt':entry['attempt'],'reason':str(error)})
                entry['attempts'][-1].update(finished_at=now(),outcome='retry',reason=str(error))
                self.session.save()
                image=self.return_to_list(self.stable('detail'),rows)
                self.notify(f"第 {entry['ordinal']} 份本次未采全：{error} 已回到原列表，重试一次（2/2）。")

    def completion_message(self, prefix):
        entries = self.session.data["entries"]
        skipped = [str(e["ordinal"]) for e in entries if e["status"] == "skipped-incomplete"]
        pending = sum(e["status"] == "needs-recapture" for e in entries)
        suffix = f" 会话累计跳过待补 {len(skipped)} 份，缺少部分详情 {pending} 份。"
        if skipped:
            suffix += " 跳过编号：" + "、".join(skipped[:30]) + ("等" if len(skipped)>30 else "") + "；完整记录见 skipped-captures.json。"
        return prefix + suffix

    def capture_memory(self, entry, tile, list_image, rows):
        self.active_entry = entry
        self.require_list(list_image)
        entry["attempt"] += 1
        entry["issues"] = []
        entry["status"] = "capturing"
        entry["attempts"].append({"attempt": entry["attempt"], "started_at": now(),
                                  "detail_policy": "all" if self.all_details else "custom-or-uncertain"})
        self.session.save()
        metadata = {"attempt": entry["attempt"]}
        # Production-only thumbnails cannot be selected in the Stage list.
        # Identify the target tile before touching it; the selected banner may
        # still describe an entirely different (usable) memory.
        if self.vision.production_only(list_image, tile):
            entry.pop("selection_verified", None)
            self.session.image(list_image, "production-only-thumbnail", entry=entry,
                               metadata={**metadata, "tile_box": tile["box"]})
            entry["status"] = "skipped-production-only"
            entry["attempts"][-1]["finished_at"] = now()
            self.session.save()
            self.notify(f"第 {entry['ordinal']} 份为制作专用，已跳过，继续下一份。")
            return list_image
        self.notify(f"正在采集第 {entry['ordinal']} 份：选中记忆……")
        selected = list_image
        for _ in range(3):
            # Clicking an already-selected tile may open it directly in the game.
            if self.vision.selection_matches(selected,tile):break
            selected = self.click(tile["point"], "list")
            selected = self.return_to_list(selected,rows)
            if not self.vision.matches_list_page(selected, rows, self.profile["list_match_tolerance"]):
                raise CapturePaused("选择记忆时列表位置改变，已暂停，未绑定截图。")
        if not self.vision.selection_matches(selected,tile):
            raise MemoryCaptureIncomplete("点击后橙色选中框未落在目标记忆上，未绑定截图。")
        entry["selection_verified"] = True
        if not self.vision.matches_list_page(selected, rows, self.profile["list_match_tolerance"]):
            raise CapturePaused("选中记忆后列表位置改变，已暂停。")
        self.session.image(selected, "list-selected", entry=entry, metadata=metadata)
        if not self.vision.details_enabled(selected):
            raise MemoryCaptureIncomplete("当前记忆的详情按钮不可用，且未读清『プロデュース専用』；已保留选中截图，没有点击禁用按钮。")
        opened = self.click(self.profile["open_detail"], "detail",
                            ready=self.vision.is_detail_page,timeout=30)
        # First-time detail may be on the Produce tab; explicitly choose Stage.
        if not self.vision.is_detail(opened):
            opened = self.click(self.profile["stage_tab"], "detail",ready=self.vision.is_detail,timeout=30)
        self.require_detail(opened)
        # Scroll only the lower detail pane, not the banner or global page.
        unchanged = 0
        for _ in range(15):
            if self.vision.at_detail_edge(opened):
                break
            previous = signature(opened,self.profile["detail_region"],(96,64))
            opened = self.scroll_detail(3, pulses=4)
            self.require_detail(opened)
            unchanged = unchanged+1 if distance(previous,signature(opened,self.profile["detail_region"],(96,64))) < 1.5 else 0
            if unchanged >= 2:
                break
        else:
            raise MemoryCaptureIncomplete("详情未能回到顶部，请检查详情滚动点。")
        header = signature(opened,self.profile["header_region"])
        self.session.image(opened, "overview-top", entry=entry, metadata=metadata)
        overview = opened
        unchanged = 0
        for _ in range(6):
            previous = signature(overview,self.profile["detail_region"],(96,64))
            # This screenshot is the bottom of the detail pane. Overscrolling is
            # intentional: use large movements until the complete grid stops moving.
            overview = self.scroll_detail(-3, pulses=4)
            self.require_detail(overview)
            self.same_header(overview, header)
            cards = self.vision.cards(overview)
            unchanged = unchanged+1 if distance(previous,signature(overview,self.profile["detail_region"],(96,64))) < 1.5 else 0
            at_bottom = self.vision.at_detail_edge(overview, bottom=True)
            if 1 <= len(cards) <= 6 and (at_bottom or unchanged):
                break
            if at_bottom or unchanged >= 2:
                self.session.image(overview, "diagnostic-cards", entry=entry, metadata=metadata)
                raise MemoryCaptureIncomplete("已到详情底部或画面不再移动，但未确认完整卡组布局（卡片可能被遮挡或识别不全）。")
        else:
            self.session.image(overview, "diagnostic-cards", entry=entry, metadata=metadata)
            raise MemoryCaptureIncomplete("详情未滚到底或未确认完整卡组，已保存画面。")
        decisions = [{"slot": card["slot"], **self.vision.customization_badge(overview, card)} for card in cards]
        entry['attempts'][-1]['card_count'] = len(cards)
        self.session.image(overview, "overview-cards", entry=entry, metadata={**metadata, "card_count":len(cards), "detail_selection": decisions})
        baseline = signature(overview,self.profile["detail_region"],(96,64))
        for card, decision in zip(cards, decisions):
            if not self.all_details and decision["state"] == "absent":
                self.notify(f"第 {entry['ordinal']} 份：卡片 {card['slot']} 未见自定义角标，保留总览、跳过弹框。")
                continue
            self.notify(f"第 {entry['ordinal']} 份：采集卡片 {card['slot']} / {len(cards)} 的详情……")
            success = False
            for attempt in range(self.profile["tooltip_retries"]):
                self.check()
                current = self.backend.screenshot()
                self.require_detail(current)
                self.same_header(current, header)
                if distance(signature(current,self.profile["detail_region"],(96,64)), baseline) > 3:
                    raise MemoryCaptureIncomplete("打开下一张卡之前，总览位置发生变化。")
                self.session.image(current,"card-before",entry=entry,slot=card["slot"],metadata={**metadata,"try":attempt+1,"point":card["point"]})
                popup = self.click(card["point"], "detail")
                # A legitimate tooltip can cover the entire portrait/name. Bind only
                # after dismissing it and verifying BOTH identity and card positions.
                panel = self.vision.tooltip(popup)
                record = self.session.image(popup, "pending-tooltip" if panel else "diagnostic-tooltip", entry=entry, slot=card["slot"],
                                   metadata={**metadata, "try": attempt+1, "panel": panel, "selection": decision})
                # Dismiss only via an explicitly calibrated blank location.
                restored = self.click(self.profile["dismiss_tooltip"], "detail")
                self.require_detail(restored)
                self.same_header(restored, header)
                if distance(signature(restored,self.profile["detail_region"],(96,64)), baseline) > 3:
                    raise MemoryCaptureIncomplete("详情框未关闭或卡组位置改变，请检查关闭详情的空白点。")
                if panel:
                    record["role"] = "card-tooltip"
                    record["metadata"]["binding_check"] = "same-header-and-overview-before-and-after"
                    self.session.save()
                    success = True
                    break
            if not success:
                entry["issues"].append({"slot": card["slot"], "reason": "tooltip-not-confirmed"})
                self.session.save()
        returned = self.return_to_list(self.backend.screenshot(),rows)
        entry["status"] = "needs-recapture" if entry["issues"] else "captured"
        entry["attempts"][-1]["finished_at"] = now()
        self.session.save()
        self.session.export_manifest()
        return returned

    def run(self, limit=1, *, retry_issues=False, foreground_ready=False, from_top=False):
        """Resume from the current saved neighbourhood; full replay is explicit."""
        if type(limit) is not int or not 1 <= limit <= 2000:
            raise ValueError("本批数量需要在 1 至 2000 之间。")
        done = 0
        self.active_entry = None
        self.session.data["last_error"] = None
        self.session.event("running", "开始采集；原图先保存，识别稍后执行。")
        self.notify('开始采集；日志、原图和断点均保存在当前会话目录。')
        try:
            if foreground_ready:
                # The UI already requested foreground activation before its countdown.
                # If the user changed apps meanwhile, pause instead of stealing focus.
                self.backend.verify()
            else:
                self.backend.activate()
            image,page_index,rows,skip_rows = self.resume_position(from_top=from_top,retry_issues=retry_issues)
            while True:
                self.check()
                if not rows:
                    raise CapturePaused("未找到完整 memory 缩略图行，请标定列表区域。")
                if page_index < len(self.session.data["pages"]):
                    saved = self.session.data["pages"][page_index]
                    if not self.vision.matches_list_page(image, saved["rows"], self.profile["list_match_tolerance"]):
                        relocated = relocate_page(saved["rows"],rows,self.profile["list_match_tolerance"])
                        if relocated is None and self.saved_rows_match(image,saved['rows'],rows):
                            relocated=copy.deepcopy(rows)
                        if relocated is None or not self.vision.matches_list_page(image,relocated,self.profile["list_match_tolerance"]):
                            raise CapturePaused("续采校验失败：无法完整且唯一地对应旧列表；可能顺序、筛选或内容改变。原图和进度已保留。")
                        rows = relocated
                        self.notify("已核对旧页面的全部回忆，仅滚动落点有偏移；按当前位置继续。")
                    else:
                        rows = copy.deepcopy(saved["rows"])
                    skip_rows = saved["skip_rows"]
                else:
                    raw = self.session.image(image,"list-page")
                    self.session.data["pages"].append({"rows": copy.deepcopy(rows), "skip_rows": skip_rows, "source": raw})
                    self.session.save()
                self.checkpoint(page_index)
                cell_index = 0
                for row_index, row in enumerate(rows):
                    for tile in row:
                        current_cell = cell_index
                        cell_index += 1
                        if row_index < skip_rows:
                            continue
                        entry = self.session.entry(page_index,current_cell)
                        skip = entry["status"] in ("captured","skipped-production-only") or (entry["status"] in ("needs-recapture", "skipped-incomplete") and not retry_issues)
                        if entry["status"] == "skipped-production-only":
                            # Page anchors were checked above. Reconfirm the label
                            # in this current tile, without requiring selection.
                            skip = self.vision.production_only(image, tile)
                        elif skip and entry["status"] != "skipped-incomplete" and not entry.get("selection_verified"):
                            from PIL import Image
                            saved_selection=next((r for r in reversed(entry["images"]) if r["role"]=="list-selected" and r["metadata"].get("attempt")==entry["attempt"]),None)
                            if saved_selection:
                                with Image.open(self.session.source(saved_selection)) as previous:
                                    skip=self.vision.selection_matches(previous,tile)
                            else:skip=False
                            if skip:
                                entry["selection_verified"]=True
                                self.session.save()
                            else:self.notify(f"第 {entry['ordinal']} 份旧截图未选中目标格，保留原图并重新采集此位置。")
                        if skip:
                            continue
                        self.checkpoint(page_index,current_cell,'memory')
                        image = self.capture_with_retry(entry,tile,image,rows)
                        self.active_entry = None
                        if entry["status"] != "skipped-production-only":
                            done += 1
                        if done >= limit:
                            self.checkpoint(page_index,current_cell+1)
                            message = self.completion_message(f"本批已尝试 {done} 份。保持列表筛选和排序不变即可续采。")
                            self.session.event("paused",message)
                            self.notify(message)
                            return self.session.data["status"]
                self.checkpoint(page_index,phase='turn-page')
                image, new_rows, skip_rows = self.next_page(image,rows)
                if new_rows is None:
                    message = self.completion_message("确认滚动条到底，当前列表遍历结束。")
                    self.session.event("complete", message)
                    self.notify(message)
                    return "complete"
                rows, page_index = new_rows, page_index+1
        except (CapturePaused, ValueError, OSError) as error:
            # Keep the last observable game state for later rule/layout improvements.
            try:
                active = self.active_entry
                self.session.image(self.backend.screenshot(), "diagnostic-stop", entry=active,
                                   metadata={"attempt": active["attempt"] if active else None, "reason": str(error),
                                             'page_index':self.active_page_index})
            except Exception:
                pass  # A lost/occluded window must not fall back to capturing the desktop.
            self.session.data["last_error"] = str(error)
            self.session.event("paused",str(error))
            self.notify(str(error))
            return "paused"
        except Exception as error:
            self.session.data["last_error"] = str(error)
            self.session.event("error",str(error))
            raise
        finally:
            self.session.export_manifest()
