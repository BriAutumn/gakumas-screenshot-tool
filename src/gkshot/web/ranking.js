let searchId=null, rankView='total', rankIdol=null, rankOffset=0, rankData=null, rankSelected=new Map(), rankTimer=null, rankRequest=0;
const pairName=r=>`${r.idol_name} · 主${memoryNumber(r.main)} / 副${memoryNumber(r.sub)}`;
async function searchHistory(select){const data=await api('/api/searches');$('#search-history').replaceChildren(...data.searches.map(s=>{const o=el('option',`${stageName(s.config.stage_id)} · ${libraryLabel(s.config.library)} · ${number(s.pairs)}组 · ${s.config.screen_policy==='five-rounds-20-v1'?'五轮初筛':'连续初筛'} · ${s.id.slice(0,6)}`);o.value=s.id;return o;}));if(select)$('#search-history').value=select;else if(!searchId&&data.searches.length)searchId=data.searches[0].id;if(searchId)$('#search-history').value=searchId;}
function resetRank(){rankOffset=0;rankSelected.clear();$('#rank-compare').textContent='对比已选（0/4）';}
async function refreshRank(){clearTimeout(rankTimer);if(!searchId)return;const request=++rankRequest,id=searchId;const q=new URLSearchParams({phase:$('#rank-phase').value,view:rankView,offset:rankOffset});if(rankIdol!==null)q.set('idol',rankIdol);const data=await api(`/api/searches/${id}?${q}`);if(request!==rankRequest||id!==searchId)return;rankData=data;renderRoundStability(data);const running=data.state==='running';$('#search-stop').hidden=!running;$('#search-resume').hidden=running||['complete','round-wait','queued'].includes(data.state);$('#search-start').disabled=running;updateDesktopActions();$('#search-resume').disabled=desktopLibrary!==null && data.config.library!==desktopLibrary;const states={queued:'等待开始','round-wait':'本轮完成，等待其他舞台',running:'运行中',complete:'已完成',cancelled:'已停止',interrupted:'上次运行中断',error:'运行失败','completed-with-errors':'完成，含失败项'};const phase=data.phase==='refine'?'复测':'初筛';$('#search-status').textContent=`${states[data.state]||data.state} · ${phase} ${number(data.completed)} / ${number(data.total)} 组 · 初筛成功 ${number(data.screen_done)} / ${number(data.pairs)} · 复测成功 ${number(data.refine_done)} / ${number(data.selected)}${data.progress?' · 当前组合 '+data.progress.completed+'/'+data.progress.trials+'次':''}${data.error?' · '+data.error:''}`;if(data.screen_round&&data.phase==='screen')$('#search-status').textContent+=` · 第 ${data.screen_round}/5 轮，每组新增20次`;$('#search-progress').max=Math.max(data.total,1);$('#search-progress').value=data.completed;$('#rank-context').textContent=`${stageName(data.config.stage_id)} · 支援 ${(data.config.support_bonus*100).toFixed(2)}% · ${$('#rank-phase').selectedOptions[0].textContent} · ${data.state==='complete'?'已完成榜单':'暂定榜单'} · 共 ${number(data.row_count)} 项${rankIdol!==null?'（已筛选角色）':''}`;$('#rank-context').textContent += ` · 榜单来源：${libraryLabel(data.config.library)}`;$('#rank-context').textContent += data.config.refine_policy==='character-balanced-v2'?' · 角色均衡复测（每角色3–15组）':data.config.refine_policy==='adaptive-character-v1'?' · 旧版分层复测（1–15组）':' · 原版复测规则';$('#rank-all').hidden=rankIdol===null;$('#rank-context').textContent+=' · 历史任务配置（不改变上方新搜索设置）'; if(data.config.group_id){$('#search-stop').hidden=true;$('#search-resume').hidden=true;} renderRank(data.rows);$('#rank-page').textContent=`${data.row_count?Math.floor(rankOffset/50)+1:0} / ${Math.ceil(data.row_count/50)}`;$('#rank-prev').disabled=rankOffset===0;$('#rank-next').disabled=rankOffset+50>=data.row_count;const errors=[...data.rejected.map(r=>`回忆 ${r.ordinal==null?r.memory_id:'#'+r.ordinal}：${r.error}`),...data.errors.map(r=>`${r.phase==='screen'?'初筛':'复测'} ${r.id}：${r.error}`)];$('#search-errors').hidden=!errors.length;$('#search-errors div').replaceChildren(...errors.map(t=>el('p',t)));rankTimer=setTimeout(()=>refreshRank().catch(e=>{notice(e.message);rankTimer=setTimeout(()=>safe(refreshRank)(),3000);}),1500);}
const rankPlots = new Map();
let rankPlotGeneration = 0;

async function fillRankPlots(rows, cells, maximum, generation, id, phase){
 let next=0;
 async function worker(){
  while(next<rows.length && generation===rankPlotGeneration){
   const i=next++,row=rows[i],key=`${id}/${phase}/${row.id}/${row.summary.trials}`;
   try{
    let record=rankPlots.get(key);
    if(!record){
     record=await api(`/api/searches/${id}/${phase}/${row.id}?trials=${row.summary.trials}`);
     rankPlots.set(key,record);
     if(rankPlots.size>300)rankPlots.delete(rankPlots.keys().next().value);
    }
    if(generation!==rankPlotGeneration)return;
    const plot=boxplot(record,maximum);
    const sorted=[...record.simulation.scores].sort((a,b)=>a-b);
    plot.append(svgNode('title',{},`最低 ${number(sorted[0])} · 25%分位 ${number(quantile(sorted,.25))} · 中位数 ${number(row.summary.median)} · 75%分位 ${number(quantile(sorted,.75))} · 最高 ${number(sorted.at(-1))} · 均分 ${number(row.summary.mean)}`));
    cells[i].replaceChildren(plot);
   }catch(error){if(generation===rankPlotGeneration)cells[i].replaceChildren(el('small','分布暂不可用：'+error.message));}
  }
 }
 await Promise.all(Array.from({length:4},worker));
}
function renderRank(rows){
 const generation=++rankPlotGeneration,area=$('#rank-table');
 if(!rows.length){area.replaceChildren(el('p',$('#rank-phase').value==='refine'?'复测尚无结果。可切换到100次初筛榜查看进度。':'等待第一个组合完成。','empty-history'));return;}
 const maximum=Math.ceil(Math.max(1,...rows.map(r=>r.summary.max))/10000)*10000;
 const table=el('table',null,'rank-visual-table'),head=el('tr'),axis=el('th',null,'rank-distribution');
 head.append(el('th','选择'),el('th','排名 / 角色 / 主副回忆'));
 axis.append(el('span','分数分布'),scoreAxis(maximum));head.append(axis);
 for(const name of ['均分 / 中位数','差距 / 波动','操作'])head.append(el('th',name));table.append(head);
 const plotCells=[];
 for(const r of rows){
  const row=el('tr'),choose=el('td'),check=el('input');check.type='checkbox';check.checked=rankSelected.has(r.id);check.setAttribute('aria-label','选择 '+pairName(r));
  check.onchange=()=>{if(check.checked&&rankSelected.size>=4){check.checked=false;notice('最多选择4个编成对比。');return;}if(check.checked)rankSelected.set(r.id,r);else rankSelected.delete(r.id);$('#rank-compare').textContent=`对比已选（${rankSelected.size}/4）`;};choose.append(check);row.append(choose);
  const name=el('td'),identity=el('div',null,'rank-identity'),caption=el('div');
  const card=r.loadout.card_ids[0].find(Boolean);if(card)identity.append(slotImage('cards',card));
  caption.append(el('strong',`${r.rank}. ${r.idol_name}`),el('small',`主 ${memoryNumber(r.main)} · 副 ${memoryNumber(r.sub)}`));
  const allocation=rankData?.allocation?.[r.idol_id];if(allocation)caption.append(el('small',`${rankData.phase==='screen'?'预计复测':'复测'} ${allocation.groups} 组 · ${{competitive:'有竞争力',distant:'较远',weak:'明显落后',baseline:'保底名额'}[allocation.tier]}${allocation.promoted?'（补测）':''}`));
  caption.title=`来源 ${r.main.source==='native-export'?'原生导出':r.main.session_id||'未知'}\n主 ${r.main.id}\n副 ${r.sub.id}`;
  if(rankView==='characters'){const expand=el('button','查看该角色组合','text-button');expand.onclick=safe(async()=>{rankIdol=r.idol_id;rankView='total';rankOffset=0;setRankTabs();await refreshRank();});caption.append(expand);}
  identity.append(caption);name.append(identity);row.append(name);
  const distribution=el('td',null,'rank-distribution');distribution.append(el('small','加载分布…'));plotCells.push(distribution);row.append(distribution);
  const score=el('td');score.append(el('strong',number(r.summary.mean,1)),el('small','中位 '+number(r.summary.median)),el('small',`${number(r.summary.trials)} 次`));row.append(score);
  const gap=el('td',r.gap?'-'+number(r.gap,1):'第一名');if(r.close&&r.rank>1)gap.append(el('small','接近第一','rank-close'));gap.append(el('small','标准差 '+number(r.summary.sample_stddev,1)),el('small','标准误 '+number(r.summary.standard_error,1)));row.append(gap);
  const actions=el('td',null,'rank-actions');
  const view=el('button','直方图');view.onclick=safe(async()=>{const record=await rankRecord(r);renderResult(record);});
  const save=el('button','☆ 保存');save.onclick=safe(async()=>{const record=await rankRecord(r);if(!history.some(h=>h.id===record.id)){history.unshift({...record,saved:true});persist();renderHistory();}notice('已保存到模拟结果的“已保存”列表。');});
  const cover=el('button','回忆封面');cover.onclick=safe(()=>showMemoryCovers(r,rankData.config.library));actions.append(view,save,cover);row.append(actions);table.append(row);
 }
 const legend=el('p','本页统一分数刻度 · 箱体：25%–75%分位 · 箱内竖线：中位数 · 圆点：均分 · 横线：最低–最高分','rank-chart-legend');
 area.replaceChildren(legend,table);
 fillRankPlots(rows,plotCells,maximum,generation,searchId,$('#rank-phase').value);
}
async function rankRecord(row){const phase=$('#rank-phase').value;const record=await api(`/api/searches/${searchId}/${phase}/${row.id}?trials=${row.summary.trials}`);return {...record,name:pairName(row),id:`${searchId}-${phase}-${row.id}`,created:new Date().toISOString(),saved:false,memories:{main:row.main,sub:row.sub},search_id:searchId};}
function setRankTabs(){$('#rank-total').classList.toggle('selected',rankView==='total');$('#rank-characters').classList.toggle('selected',rankView==='characters');}
function compareRank(){if(rankSelected.size<2){notice('请选择2至4个同阶段编成。');return;}const rows=[...rankSelected.values()],table=el('table');for(const [label,fn] of [['角色',r=>r.idol_name],['主回忆',r=>memoryNumber(r.main)],['副回忆',r=>memoryNumber(r.sub)],['平均分',r=>number(r.summary.mean,2)],['中位数',r=>number(r.summary.median)],['单次标准差',r=>number(r.summary.sample_stddev,2)],['标准误',r=>number(r.summary.standard_error,2)],['次数',r=>number(r.summary.trials)],['属性（Vo / Da / Vi / 体力）',r=>parameterText(r.loadout.parameters)]]){const line=el('tr');line.append(el('th',label));rows.forEach(r=>line.append(el('td',fn(r))));table.append(line);}for(const group of [0,1]){const line=el('tr');line.append(el('th',group?'副卡组':'主卡组'));for(const r of rows){const cell=el('td'),cards=el('div',null,'rank-cards');r.loadout.card_ids[group].forEach((id,i)=>{if(id){const wrap=el('span');wrap.append(slotImage('cards',id));const custom=r.loadout.customizations[group][i];if(custom)wrap.append(el('small',custom));cards.append(wrap);}});cell.append(cards);line.append(cell);}table.append(line);}$('#rank-comparison-content').replaceChildren(table);$('#rank-comparison').showModal();}
async function initRanking(){
 initSeasonControls();
 $('#preview-memories').onclick=safe(async()=>{const main=memories.find(m=>m.id===$('#main-memory').value),sub=memories.find(m=>m.id===$('#sub-memory').value);if(!main||!sub)throw Error('请先选择主副回忆');await showMemoryCovers({main,sub,idol_name:'记忆库'},$('#library').value);});
 $('#search-library').replaceChildren(...[...$('#library').options].map(o=>o.cloneNode(true)));
 await initLibraryHandoff();
 $('#season-select').onchange=safe(async()=>{chooseSeason();await refreshSeasonNav();await selectCurrentStage();});
 $('#stage').onchange=safe(async()=>{updateStartContext();await refreshSeasonNav();await selectCurrentStage();});
 $('#search-preview').onclick=safe(async()=>{$('#search-estimate').textContent='正在读取记忆库与统计组合…';const d=await api('/api/search/preview',searchConfig());$('#search-estimate').textContent=`本场 ${number(d.pairs)} 个有序主副组合 · ${d.characters} 个角色 · ${d.rejected.length} 份记录校验未通过。全部先跑100次，再自动复测领先和接近的组合。`;});
 $('#search-start').onclick=safe(async()=>{const button=$('#search-start');launching=true;updateDesktopActions();try{const data=await api('/api/search/start',searchConfig());groupId=null;groupData=null;searchId=data.search_id;rankIdol=null;resetRank();await searchHistory(searchId);await refreshRank();await refreshSeasonNav();}finally{launching=false;updateDesktopActions();}});
 $('#search-history').onchange=safe(async()=>{searchId=$('#search-history').value;rankIdol=null;resetRank();await refreshRank();});
 $('#search-stop').onclick=safe(async()=>{requireDesktopLibrary(rankData?.config.library);await api('/api/search/cancel',{search_id:searchId,library:$('#search-library').value});await refreshRank();});
 $('#search-resume').onclick=safe(async()=>{requireDesktopLibrary(rankData?.config.library);await api('/api/search/resume',{search_id:searchId,library:$('#search-library').value});await refreshRank();});
 for(const [id,view] of [['rank-total','total'],['rank-characters','characters']])$('#'+id).onclick=safe(async()=>{rankView=view;rankIdol=null;rankOffset=0;setRankTabs();await refreshRank();});
 $('#rank-phase').onchange=safe(async()=>{resetRank();await refreshRank();});$('#rank-all').onclick=safe(async()=>{rankIdol=null;rankOffset=0;await refreshRank();});
 $('#rank-prev').onclick=safe(async()=>{rankOffset=Math.max(0,rankOffset-50);await refreshRank();});$('#rank-next').onclick=safe(async()=>{rankOffset+=50;await refreshRank();});$('#rank-compare').onclick=compareRank;
 $('#rank-export').onclick=safe(async()=>{if(!searchId)return;let rows=[],offset=0;do{const q=new URLSearchParams({phase:$('#rank-phase').value,view:rankView,offset});if(rankIdol!==null)q.set('idol',rankIdol);const d=await api(`/api/searches/${searchId}?${q}`);rows.push(...d.rows);offset+=50;if(offset>=d.row_count)break;}while(true);const csv=[['排名','角色','主编号','副编号','均分','中位数','标准差','标准误','次数','场地','支援加成','主记忆ID','副记忆ID'],...rows.map(r=>[r.rank,r.idol_name,r.main.ordinal,r.sub.ordinal,r.summary.mean,r.summary.median,r.summary.sample_stddev,r.summary.standard_error,r.summary.trials,r.loadout.stage_id,r.loadout.support_bonus,r.main.id,r.sub.id])].map(row=>row.map(v=>'"'+String(v??'').replaceAll('"','""')+'"').join(',')).join('\n');download('ranking.csv','\ufeff'+csv,'text/csv;charset=utf-8');});
 await searchHistory();if(searchId)await refreshRank();
 await refreshSeasonNav();
 await selectCurrentStage();
}
if(window.gkReady)safe(initRanking)();
else window.addEventListener('gk-ready',safe(initRanking),{once:true});

async function showMemoryCovers(row,library){
 const content=$('#memory-cover-content');content.replaceChildren(el('p','正在读取回忆详情…'));$('#memory-cover-title').textContent=pairName(row);$('#memory-cover').showModal();
 const previews=await Promise.allSettled([row.main,row.sub].map(m=>api('/api/memory-preview?'+new URLSearchParams({library,memory:m.id}))));
 content.replaceChildren();previews.forEach((result,i)=>{const box=el('section'),memory=i?row.sub:row.main;box.append(el('h3',`${i?'副':'主'}回忆 ${memoryNumber(memory)}`));if(result.status==='rejected'){box.append(el('p','预览不可用：'+result.reason.message));}else if(result.value.source==='native-export'){renderNativeMemory(box,result.value);}else{const image=el('img');image.src=result.value.image;image.alt=`回忆 ${memoryNumber(memory)} 的原始采集封面`;image.className='memory-cover-image';const full=el('button','展开完整列表截图');full.onclick=()=>{const expanded=image.classList.toggle('full');full.textContent=expanded?'收起列表截图':'展开完整列表截图';};box.append(image,full);}content.append(box);});
}

function renderNativeMemory(box,data){
 const draft=data.draft,idol=catalog.p_idols.find(p=>p.id===draft.p_idol_id);
 box.append(el('p','原生导出 · '+(idol?.title||draft.p_idol_id)));
 if(data.image){
  const image=el('img');image.alt=data.photo_kind==='preset'?'游戏内置回忆封面':'玩家拍摄的回忆封面';image.className='memory-cover-image native-photo'+(data.photo_kind==='preset'?' preset-photo':'');
  image.onerror=()=>{image.replaceWith(el('p','原始照片无法显示。请在游戏中打开回忆一览加载照片后重试。','muted'));};
  image.src=data.image;
  box.append(image);
 }else if(data.photo_message){box.append(el('p',data.photo_message,'muted'));}
 box.append(el('p','Vo / Da / Vi / 体力：'+parameterText(draft.parameters)));
 for(const [kind,ids] of [['items',draft.item_ids],['cards',draft.card_ids]]){
  const list=el('div',null,'rank-cards'+(kind==='cards'?' native-memory-cards':''));
  ids.forEach((id,index)=>{if(!id)return;const entry=el('span');entry.append(slotImage(kind,id));
   entry.append(el('small',(kind==='cards'?cardById(id):itemById(id))?.name||String(id)));
   const token=kind==='cards'?draft.customizations[index]:'';
   if(token){const names=token.split('e').map(part=>{const [effect,count]=part.split('x');const option=catalog.customizations.find(c=>c.id===Number(effect));return (option?.name||effect)+' ×'+count;});entry.append(el('small',names.join(' / ')));}
   list.append(entry);});box.append(list);
 }
 box.append(el('small','导出副本 ID：'+data.memory_id));
}

function renderRoundStability(data){
 const area=$('#round-stability');area.replaceChildren();
 if(data.config.screen_policy!=='five-rounds-20-v1')return;
 area.append(el('p','分轮初筛：每轮全库新增20次，跑满5轮；本轮未结束时，榜单保持上一轮完整结果。','muted'));
 const table=el('table'),head=el('tr');
 for(const text of ['累计次数','前30保留','前30平均位移','角色最佳变化','复测名单变化','已复测前30覆盖'])head.append(el('th',text));table.append(head);
 for(const h of data.round_history||[]){const row=el('tr'),c=h.changes;for(const text of [h.trials+'次',c?`${c.top_retained}/${c.top_count}`:'基线',c?number(c.top_mean_rank_shift,1)+'名':'—',c?`${c.character_best_changed}/${c.character_count}`:'—',c?`新增${c.finalists_added} / 移出${c.finalists_removed}`:'—',h.verified_top_count!=null?`${h.verified_top_covered}/${h.verified_top_count}`:'待复测'])row.append(el('td',text));table.append(row);}
 area.append(table,el('small','覆盖率只对最终已复测的候选计算，不能证明被排除的组合中没有强组。'));for(const error of data.round_errors||[])area.append(el('p',error.id+'：'+error.error));
}



let seasonTimer=null,seasonRequest=0,stageProgress=[];
const stateLabel=s=>({running:'运行中',paused:'已暂停','round-wait':'等待轮转',pending:'未开始',queued:'等待开始',cancelled:'已暂停',error:'失败',interrupted:'待续跑',complete:'已完成','completed-with-errors':'完成，含失败项'}[s]||s);
async function selectCurrentStage(){
 const row=stageProgress.find(s=>s.stage_id===Number($('#stage').value));
 searchId=row?.search_id||null;rankIdol=null;resetRank();
 if(searchId){$('#rank-phase').value=row.phase==='refine'?'refine':'screen';await searchHistory(searchId);await refreshRank();}
 else{clearTimeout(rankTimer);rankRequest++;rankData=null;$('#rank-table').replaceChildren(el('p','当前舞台尚无搜索，选择上方入口开始。','empty-history'));$('#rank-context').textContent=stageName(Number($('#stage').value));$('#search-status').textContent='尚未开始';$('#search-progress').value=0;$('#round-stability').replaceChildren();$('#search-stop').hidden=true;$('#search-resume').hidden=true;$('#rank-page').textContent='0 / 0';}
}
async function refreshSeasonNav(){
 clearTimeout(seasonTimer);const request=++seasonRequest,season=Number($('#season-select').value),library=$('#search-library').value;
 try{
  const groups=await api('/api/season-runs'), searches=await api('/api/searches');
  if(request!==seasonRequest)return;
  seasonBusy=groups.groups.some(g=>g.state==='running')||searches.searches.some(s=>s.state==='running');
  const matching=groups.groups.filter(g=>g.config.season===season&&g.config.library===library);
  if(groupId&&!matching.some(g=>g.id===groupId))groupId=null;
  groupData=matching.find(g=>g.id===groupId)||null;
  const latest=el('option','各舞台最新任务');latest.value='';
  $('#season-history').replaceChildren(latest,...matching.map(g=>{const o=el('option','S'+g.config.season+' · '+stateLabel(g.state)+' · '+g.id.slice(0,6));o.value=g.id;return o;}));
  if(groupId)$('#season-history').value=groupId;
  $('#season-status').textContent=groupData?'S'+groupData.config.season+' · '+stateLabel(groupData.state)+' · 支援 '+groupData.config.support_bonus*100+'% · '+groupData.stages.map(s=>'舞台'+s.stage+' '+stateLabel(s.state)+' / 初筛'+s.trials+'次').join('；')+(groupData.error?' · '+groupData.error:''):'尚未创建本赛季三场任务';
  $('#season-pause').hidden=groupData?.state!=='running';$('#season-resume').hidden=!groupData||['running','complete'].includes(groupData.state);$('#season-resume').disabled=seasonBusy||!desktopAvailable;
  const q=new URLSearchParams({season});if(library)q.set('library',library);if(groupId)q.set('group',groupId);
  const data=await api('/api/season-progress?'+q);if(request!==seasonRequest)return;stageProgress=data.stages;
  const area=$('#season-stages');area.replaceChildren(...data.stages.map(stage=>{
   const button=el('button');button.id='season-stage-'+stage.stage_id;
   button.append(el('strong','舞台 '+stage.stage),el('small',stateLabel(stage.state)+' · 初筛 '+stage.trials+'/100'+(stage.state==='running'?' · '+stage.completed+'/'+stage.total+'组':'')));
   button.classList.toggle('running',stage.state==='running');button.setAttribute('aria-current',String(Number($('#stage').value)===stage.stage_id));
   button.onclick=safe(async()=>{$('#stage').value=stage.stage_id;updateStartContext();await selectCurrentStage();for(const b of area.children)b.setAttribute('aria-current',String(b===button));$('#search-section').scrollIntoView({behavior:'smooth',block:'start'});});return button;
  }));
  updateDesktopActions();
 }catch(e){notice('进度刷新失败：'+e.message);}
 finally{if(request===seasonRequest)seasonTimer=setTimeout(refreshSeasonNav,2500);}
}
async function startSeasonRun(){
 const c=searchConfig(),season=Number($('#season-select').value);launching=true;updateDesktopActions();
 try{const data=await api('/api/season-runs/start',{...c,season,stages:seasonStages(catalog.stages,season).map(s=>s.id)});groupId=data.group_id;await refreshSeasonNav();await selectCurrentStage();}
 finally{launching=false;updateDesktopActions();}
}
function initSeasonControls(){
 $('#season-start').onclick=safe(startSeasonRun);
 $('#season-history').onchange=safe(async()=>{groupId=$('#season-history').value;await refreshSeasonNav();await selectCurrentStage();});
 $('#season-pause').onclick=safe(async()=>{requireDesktopLibrary(groupData?.config.library);await api('/api/season-runs/pause',{group_id:groupId,library:$('#search-library').value});await refreshSeasonNav();});
 $('#season-resume').onclick=safe(async()=>{requireDesktopLibrary(groupData?.config.library);await api('/api/season-runs/resume',{group_id:groupId,library:$('#search-library').value});await refreshSeasonNav();});
}
