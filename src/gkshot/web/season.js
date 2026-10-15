/* Pure season / stage / URL helpers. No DOM access; loaded before the page scripts. */
function contestSeasons(stages){
 return [...new Set(stages.filter(s=>s.type==='contest'&&Number.isInteger(s.season)&&s.season>0).map(s=>s.season))].sort((a,b)=>b-a);
}
function seasonStages(stages,season){
 return stages.filter(s=>s.type==='contest'&&s.season===season).slice().sort((a,b)=>a.stage-b.stage);
}
function stageLabel(stage){
 if(!stage) return '未选择场地';
 const plans={sense:'感性',logic:'理性',anomaly:'异变',free:'自由'};
 return `S${stage.season} · 第 ${stage.stage} 舞台${plans[stage.plan]?' · '+plans[stage.plan]:''}`;
}
function seasonSummary(stages,season){
 const rows=seasonStages(stages,season);
 return rows.length?`S${season} 第 ${rows.map(r=>r.stage).join(' / ')} 舞台`:`S${season}`;
}
function supportsSeasonRun(stages,season){
 return seasonStages(stages,season).length===3;
}
function memoryNumber(memory){
 return !memory||memory.ordinal==null?'编号未知':`#${memory.ordinal}`;
}
/* Build the upstream GKTools simulator URL from a full composed loadout. */
function simulatorUrl(loadout){
 const params=['vocal','dance','visual','stamina'].map(k=>loadout.parameters[k]).join('-');
 const query=new URLSearchParams({stage:loadout.stage_id,support_bonus:loadout.support_bonus,params,
  items:loadout.item_ids.join('-'),
  cards:loadout.card_ids.map(row=>row.join('-')).join('_'),
  customizations:loadout.customizations.map(row=>row.join('-')).join('_')});
 return 'https://gktools.ris.moe/zh-Hans/simulator?'+query;
}
