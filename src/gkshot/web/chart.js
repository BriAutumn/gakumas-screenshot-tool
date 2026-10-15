/* Shared SVG chart utilities for the histogram and box plots. */
const number = (x, digits=0) => x == null ? '—' : Number(x).toLocaleString('zh-CN',{maximumFractionDigits:digits});
function svgNode(tag, attrs={}, text){
 const node=document.createElementNS('http://www.w3.org/2000/svg',tag);
 for(const [key,value] of Object.entries(attrs)) node.setAttribute(key,value);
 if(text!=null) node.textContent=text;
 return node;
}
function quantile(sorted,p){
 const i=(sorted.length-1)*p, a=Math.floor(i);
 return sorted[a]+(sorted[Math.ceil(i)]-sorted[a])*(i-a);
}
function drawHistogram(container,scores){
 container.replaceChildren();
 const min=scores.reduce((a,b)=>Math.min(a,b),Infinity), max=scores.reduce((a,b)=>Math.max(a,b),-Infinity);
 const count=45, width=Math.max(1,(max-min)/count);
 const bins=Array(count).fill(0);
 scores.forEach(x=>bins[Math.min(count-1,Math.floor((x-min)/width))]++);
 const peak=Math.max(...bins,1);
 const x0=38,y0=260,w=710,h=232;
 for(let i=0;i<=4;i++){
  const y=y0-h*i/4;
  container.append(svgNode('line',{x1:x0,y1:y,x2:x0+w,y2:y,class:'chart-grid'}),
   svgNode('text',{x:x0-9,y:y+4,'text-anchor':'end'},Math.round(peak*i/4)));
 }
 bins.forEach((n,i)=>{
  const bar=svgNode('rect',{x:x0+i*w/count+1,y:y0-n/peak*h,width:w/count-3,height:n/peak*h,rx:1,class:'hist-bar'});
  bar.append(svgNode('title',{},`${number(min+i*width)}–${number(min+(i+1)*width)} 分：${n} 次`));
  container.append(bar);
 });
 for(let i=0;i<=6;i++) container.append(svgNode('text',{x:x0+w*i/6,y:282,'text-anchor':'middle'},number(min+(Math.max(max-min,1))*i/6)));
}
function boxplot(record,maximum){
 const scores=[...record.simulation.scores].sort((a,b)=>a-b);
 const q1=quantile(scores,.25),median=quantile(scores,.5),q3=quantile(scores,.75);
 const svg=svgNode('svg',{viewBox:'0 0 260 50',class:'boxplot',role:'img','aria-label':`平均 ${number(record.summary.mean)}，中位数 ${number(median)}`});
 const x=value=>5+value/maximum*245;
 for(let i=0;i<5;i++) svg.append(svgNode('line',{x1:5+i*245/4,x2:5+i*245/4,y1:5,y2:45,stroke:'#e9ecf0','stroke-dasharray':'3 3'}));
 svg.append(svgNode('line',{x1:x(scores[0]),x2:x(scores.at(-1)),y1:25,y2:25,stroke:'#f8a319','stroke-width':1.6}));
 for(const value of [scores[0],scores.at(-1)]) svg.append(svgNode('line',{x1:x(value),x2:x(value),y1:17,y2:33,stroke:'#f8a319'}));
 svg.append(svgNode('rect',{x:x(q1),y:13,width:Math.max(1,x(q3)-x(q1)),height:24,fill:'#fff0d3',stroke:'#f8a319','stroke-width':1.5}),
  svgNode('line',{x1:x(median),x2:x(median),y1:13,y2:37,stroke:'#f8a319'}),
  svgNode('circle',{cx:x(record.summary.mean),cy:25,r:3,fill:'white',stroke:'#f8a319','stroke-width':1.5}));
 return svg;
}
function scoreAxis(maximum){
 const svg=svgNode('svg',{viewBox:'0 0 260 28',class:'rank-axis','aria-hidden':'true'});
 for(let i=0;i<=4;i++) svg.append(svgNode('text',{x:5+i*245/4,y:19,'text-anchor':i===0?'start':i===4?'end':'middle'},number(maximum*i/4)));
 return svg;
}
