let searchId=null, rankView='total', rankIdol=null, rankOffset=0, rankData=null, rankSelected=new Map(), rankTimer=null, rankRequest=0;
const memoryNumber=m=>m.ordinal==null?'编号未知':`#${m.ordinal}`;
const pairName=r=>`${r.idol_name} · 主${memoryNumber(r.main)} / 副${memoryNumber(r.sub)}`;
function searchConfig(){const l=readEditor();return {library:$('#search-library').value,stage_id:l.stage_id,support_bonus:l.support_bonus,seed:Number($('#seed').value)};}
async function searchHistory(select){const data=await api('/api/searches');$('#search-history').replaceChildren(...data.searches.map(s=>{const o=el('option',`${stageName(s.config.stage_id)} · ${s.config.library.split('/processing/')[1]?.replace('/memory-library','')} · ${number(s.pairs)}组 · ${s.config.screen_policy==='five-rounds-20-v1'?'五轮初筛':'连续初筛'} · ${s.id.slice(0,6)}`);o.value=s.id;return o;}));if(select)$('#search-history').value=select;else if(!searchId&&data.searches.length)searchId=data.searches[0].id;if(searchId)$('#search-history').value=searchId;}
function resetRank(){rankOffset=0;rankSelected.clear();$('#rank-compare').textContent='对比已选（0/4）';}
async function refreshRank(){clearTimeout(rankTimer);if(!searchId)return;const request=++rankRequest,id=searchId;const q=new URLSearchParams({phase:$('#rank-phase').value,view:rankView,offset:rankOffset});if(rankIdol!==null)q.set('idol',rankIdol);const data=await api(`/api/searches/${id}?${q}`);if(request!==rankRequest||id!==searchId)return;rankData=data;renderRoundStability(data);const running=data.state==='running';$('#search-stop').hidden=!running;$('#search-resume').hidden=running||['complete','round-wait','queued'].includes(data.state);$('#search-start').disabled=running;const states={queued:'等待开始','round-wait':'本轮完成，等待其他舞台',running:'运行中',complete:'已完成',cancelled:'已停止',interrupted:'上次运行中断',error:'运行失败','completed-with-errors':'完成，含失败项'};const phase=data.phase==='refine'?'复测':'初筛';$('#search-status').textContent=`${states[data.state]||data.state} · ${phase} ${number(data.completed)} / ${number(data.total)} 组 · 初筛成功 ${number(data.screen_done)} / ${number(data.pairs)} · 复测成功 ${number(data.refine_done)} / ${number(data.selected)}${data.progress?' · 当前组合 '+data.progress.completed+'/'+data.progress.trials+'次':''}${data.error?' · '+data.error:''}`;if(data.screen_round&&data.phase==='screen')$('#search-status').textContent+=` · 第 ${data.screen_round}/5 轮，每组新增20次`;$('#search-progress').max=Math.max(data.total,1);$('#search-progress').value=data.completed;$('#rank-context').textContent=`${stageName(data.config.stage_id)} · 支援 ${(data.config.support_bonus*100).toFixed(2)}% · ${$('#rank-phase').selectedOptions[0].textContent} · ${data.state==='complete'?'已完成榜单':'暂定榜单'} · 共 ${number(data.row_count)} 项${rankIdol!==null?'（已筛选角色）':''}`;$('#rank-context').textContent += data.config.refine_policy==='character-balanced-v2'?' · 角色均衡复测（每角色3–15组）':data.config.refine_policy==='adaptive-character-v1'?' · 旧版分层复测（1–15组）':' · 原版复测规则';$('#rank-all').hidden=rankIdol===null;renderRank(data.rows);$('#rank-page').textContent=`${data.row_count?Math.floor(rankOffset/50)+1:0} / ${Math.ceil(data.row_count/50)}`;$('#rank-prev').disabled=rankOffset===0;$('#rank-next').disabled=rankOffset+50>=data.row_count;const errors=[...data.rejected.map(r=>`回忆 ${r.ordinal==null?r.memory_id:'#'+r.ordinal}：${r.error}`),...data.errors.map(r=>`${r.phase==='screen'?'初筛':'复测'} ${r.id}：${r.error}`)];$('#search-errors').hidden=!errors.length;$('#search-errors div').replaceChildren(...errors.map(t=>el('p',t)));if(running||data.state==='round-wait')rankTimer=setTimeout(()=>refreshRank().catch(e=>{notice(e.message);rankTimer=setTimeout(()=>safe(refreshRank)(),3000);}),1500);}
const rankPlots = new Map();
let rankPlotGeneration = 0;
function scoreAxis(maximum){
 const svg=svgNode('svg',{viewBox:'0 0 260 28',class:'rank-axis','aria-hidden':'true'});
 for(let i=0;i<=4;i++)svg.append(svgNode('text',{x:5+i*245/4,y:19,'text-anchor':i===0?'start':i===4?'end':'middle'},number(maximum*i/4)));
 return svg;
}
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
  caption.title=`采集批次 ${r.main.session_id||'未知'}\n主 ${r.main.id}\n副 ${r.sub.id}`;
  if(rankView==='characters'){const expand=el('button','查看该角色组合','text-button');expand.onclick=safe(async()=>{rankIdol=r.idol_id;rankView='total';rankOffset=0;setRankTabs();await refreshRank();});caption.append(expand);}
  identity.append(caption);name.append(identity);row.append(name);
  const distribution=el('td',null,'rank-distribution');distribution.append(el('small','加载分布…'));plotCells.push(distribution);row.append(distribution);
  const score=el('td');score.append(el('strong',number(r.summary.mean,1)),el('small','中位 '+number(r.summary.median)),el('small',`${number(r.summary.trials)} 次`));row.append(score);
  const gap=el('td',r.gap?'-'+number(r.gap,1):'第一名');if(r.close&&r.rank>1)gap.append(el('small','接近第一','rank-close'));gap.append(el('small','标准差 '+number(r.summary.sample_stddev,1)),el('small','标准误 '+number(r.summary.standard_error,1)));row.append(gap);
  const actions=el('td',null,'rank-actions');
  const load=el('button','载入');load.onclick=safe(()=>{setLoadout(r.loadout);$('#memory-provenance').textContent=pairName(r);$('#candidate-name').value=pairName(r);notice(`已载入 ${pairName(r)}`);$('.editor').scrollIntoView({behavior:'smooth',block:'start'});});
  const view=el('button','直方图');view.onclick=safe(async()=>{const record=await rankRecord(r);renderResult(record);$('.results').scrollIntoView({behavior:'smooth',block:'start'});});
  const save=el('button','☆ 保存');save.onclick=safe(async()=>{const record=await rankRecord(r);if(!history.some(h=>h.id===record.id)){history.unshift({...record,saved:true});persist();renderHistory();}notice('已保存到模拟结果的“已保存”列表。');});
  const cover=el('button','回忆封面');cover.onclick=safe(()=>showMemoryCovers(r,rankData.config.library));actions.append(load,view,save,cover);row.append(actions);table.append(row);
 }
 const legend=el('p','本页统一分数刻度 · 箱体：25%–75%分位 · 箱内竖线：中位数 · 圆点：均分 · 横线：最低–最高分','rank-chart-legend');
 area.replaceChildren(legend,table);
 fillRankPlots(rows,plotCells,maximum,generation,searchId,$('#rank-phase').value);
}
async function rankRecord(row){const phase=$('#rank-phase').value;const record=await api(`/api/searches/${searchId}/${phase}/${row.id}?trials=${row.summary.trials}`);return {...record,name:pairName(row),id:`${searchId}-${phase}-${row.id}`,created:new Date().toISOString(),saved:false,memories:{main:row.main,sub:row.sub},search_id:searchId};}
function setRankTabs(){$('#rank-total').classList.toggle('selected',rankView==='total');$('#rank-characters').classList.toggle('selected',rankView==='characters');}
function compareRank(){if(rankSelected.size<2){notice('请选择2至4个同阶段编成。');return;}const rows=[...rankSelected.values()],table=el('table');for(const [label,fn] of [['角色',r=>r.idol_name],['主回忆',r=>memoryNumber(r.main)],['副回忆',r=>memoryNumber(r.sub)],['平均分',r=>number(r.summary.mean,2)],['中位数',r=>number(r.summary.median)],['单次标准差',r=>number(r.summary.sample_stddev,2)],['标准误',r=>number(r.summary.standard_error,2)],['次数',r=>number(r.summary.trials)],['属性（Vo / Da / Vi / 体力）',r=>Object.values(r.loadout.parameters).join(' / ')]]){const line=el('tr');line.append(el('th',label));rows.forEach(r=>line.append(el('td',fn(r))));table.append(line);}for(const group of [0,1]){const line=el('tr');line.append(el('th',group?'副卡组':'主卡组'));for(const r of rows){const cell=el('td'),cards=el('div',null,'rank-cards');r.loadout.card_ids[group].forEach((id,i)=>{if(id){const wrap=el('span');wrap.append(slotImage('cards',id));const custom=r.loadout.customizations[group][i];if(custom)wrap.append(el('small',custom));cards.append(wrap);}});cell.append(cards);line.append(cell);}table.append(line);}$('#rank-comparison-content').replaceChildren(table);$('#rank-comparison').showModal();}
async function initRanking(){
 $('#preview-memories').onclick=safe(async()=>{const main=memories.find(m=>m.id===$('#main-memory').value),sub=memories.find(m=>m.id===$('#sub-memory').value);if(!main||!sub)throw Error('请先选择主副回忆');await showMemoryCovers({main,sub,idol_name:'记忆库'},$('#library').value);});
 $('#search-library').replaceChildren(...[...$('#library').options].map(o=>o.cloneNode(true)));
 $('#search-preview').onclick=safe(async()=>{$('#search-estimate').textContent='正在读取记忆库与统计组合…';const d=await api('/api/search/preview',searchConfig());$('#search-estimate').textContent=`本场 ${number(d.pairs)} 个有序主副组合 · ${d.characters} 个角色 · ${d.rejected.length} 份记录校验未通过。全部先跑100次，再自动复测领先和接近的组合。`;});
 $('#search-start').onclick=safe(async()=>{const button=$('#search-start');button.disabled=true;try{const data=await api('/api/search/start',searchConfig());searchId=data.search_id;rankIdol=null;resetRank();await searchHistory(searchId);await refreshRank();}finally{button.disabled=rankData?.state==='running';}});
 $('#search-history').onchange=safe(async()=>{searchId=$('#search-history').value;rankIdol=null;resetRank();await refreshRank();});
 $('#search-stop').onclick=safe(async()=>{await api('/api/search/cancel',{search_id:searchId});await refreshRank();});
 $('#search-resume').onclick=safe(async()=>{await api('/api/search/resume',{search_id:searchId});await refreshRank();});
 for(const [id,view] of [['rank-total','total'],['rank-characters','characters']])$('#'+id).onclick=safe(async()=>{rankView=view;rankIdol=null;rankOffset=0;setRankTabs();await refreshRank();});
 $('#rank-phase').onchange=safe(async()=>{resetRank();await refreshRank();});$('#rank-all').onclick=safe(async()=>{rankIdol=null;rankOffset=0;await refreshRank();});
 $('#rank-prev').onclick=safe(async()=>{rankOffset=Math.max(0,rankOffset-50);await refreshRank();});$('#rank-next').onclick=safe(async()=>{rankOffset+=50;await refreshRank();});$('#rank-compare').onclick=compareRank;
 $('#rank-export').onclick=safe(async()=>{if(!searchId)return;let rows=[],offset=0;do{const q=new URLSearchParams({phase:$('#rank-phase').value,view:rankView,offset});if(rankIdol!==null)q.set('idol',rankIdol);const d=await api(`/api/searches/${searchId}?${q}`);rows.push(...d.rows);offset+=50;if(offset>=d.row_count)break;}while(true);const csv=[['排名','角色','主编号','副编号','均分','中位数','标准差','标准误','次数','场地','支援加成','主记忆ID','副记忆ID'],...rows.map(r=>[r.rank,r.idol_name,r.main.ordinal,r.sub.ordinal,r.summary.mean,r.summary.median,r.summary.sample_stddev,r.summary.standard_error,r.summary.trials,r.loadout.stage_id,r.loadout.support_bonus,r.main.id,r.sub.id])].map(row=>row.map(v=>'"'+String(v??'').replaceAll('"','""')+'"').join(',')).join('\n');download('ranking.csv','\ufeff'+csv,'text/csv;charset=utf-8');});
 await searchHistory();if(searchId)await refreshRank();
 await refreshSeasonNav();
}
if(window.gkReady)safe(initRanking)();
else window.addEventListener('gk-ready',safe(initRanking),{once:true});

async function showMemoryCovers(row,library){
 const content=$('#memory-cover-content');content.replaceChildren(el('p','正在读取并核对原始采集截图…'));$('#memory-cover-title').textContent=pairName(row);$('#memory-cover').showModal();
 const previews=await Promise.allSettled([row.main,row.sub].map(m=>api('/api/memory-preview?'+new URLSearchParams({library,memory:m.id}))));
 content.replaceChildren();previews.forEach((result,i)=>{const box=el('section'),memory=i?row.sub:row.main;box.append(el('h3',`${i?'副':'主'}回忆 ${memoryNumber(memory)}`));if(result.status==='rejected'){box.append(el('p','预览不可用：'+result.reason.message));}else{const image=el('img');image.src=result.value.image;image.alt=`回忆 ${memoryNumber(memory)} 的原始采集封面`;image.className='memory-cover-image';const full=el('button','展开完整列表截图');full.onclick=()=>{const expanded=image.classList.toggle('full');full.textContent=expanded?'收起列表截图':'展开完整列表截图';};box.append(image,full);}content.append(box);});
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


let seasonTimer=null;
async function refreshSeasonNav(){
 clearTimeout(seasonTimer);
 try{
  const data=await api('/api/season-progress'),area=$('#season-stages');
  const current=data.stages.find(s=>s.search_id===searchId);
  if(current&&rankData&&current.state!==rankData.state)await refreshRank();
  for(const [index,stage] of data.stages.entries()){
   let button=$('#season-stage-'+stage.stage_id);
   if(!button){button=el('button');button.id='season-stage-'+stage.stage_id;area.append(button);}
   const labels={running:'运行中','round-wait':'等待轮转',pending:'等待开始',queued:'等待开始',cancelled:'已暂停',error:'失败，可重试',interrupted:'待续跑',complete:'已完成','completed-with-errors':'完成，含错误'};
   button.replaceChildren(el('strong',`舞台 ${index+1}`),el('small',`${labels[stage.state]||stage.state} · 初筛 ${stage.trials}/100`));
   if(stage.state==='running')button.lastChild.textContent+=` · ${stage.completed}/${stage.total}组`;
   button.classList.toggle('running',stage.state==='running');button.setAttribute('aria-current',String(searchId===stage.search_id));
   button.disabled=!stage.search_id;
   button.onclick=safe(async()=>{
    searchId=stage.search_id;rankIdol=null;resetRank();
    $('#rank-phase').value=stage.phase==='refine'?'refine':'screen';
    await searchHistory(searchId);await refreshRank();
    for(const b of area.children)b.setAttribute('aria-current',String(b===button));
    $('#search-section').scrollIntoView({behavior:'smooth',block:'start'});
   });
  }
 }catch(error){$('#season-nav').title='舞台进度暂不可用：'+error.message;}
 finally{seasonTimer=setTimeout(refreshSeasonNav,3000);}
}
