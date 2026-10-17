/* Offline data/display invariants, replay interactions, and SVG visual QA. No browser navigation. */
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const [fragmentPath,d3Path,sharpPath,output]=process.argv.slice(2);
const html=fs.readFileSync(fragmentPath,'utf8');
assert(!/<!doctype|<html[ >]|<body[ >]/i.test(html));
assert(Buffer.byteLength(html)<1_000_000);
const scripts=[...html.matchAll(/<script([^>]*)>([\s\S]*?)<\/script>/g)];
const dataScript=scripts.find(s=>s[1].includes('application/json'));
const elements={},listeners={};let displayWidth=504,intervalCallback=null,savedState=null;
function makeElement(id=''){
  return {id,value:'',textContent:'',hidden:false,open:false,style:{},attributes:{},children:[],events:{},_html:'',
    set innerHTML(value){this._html=value;},get innerHTML(){return this._html;},
    append(child){this.children.push(child);if(this.children.length===1)this.value=child.value;},
    addEventListener(type,fn){this.events[type]=fn;},setAttribute(key,value){this.attributes[key]=value;},getAttribute(key){return this.attributes[key];},
    getBoundingClientRect(){return {left:0,top:0,width:id==='crypto-strength-replay'?1024:displayWidth,height:360};},
    querySelector(selector){
      if(selector.startsWith('#'))return element(selector.slice(1));
      if(selector.startsWith('[data-layer='))return element(selector.includes('relative')?'relative-button':'large-button');
      if(selector==='svg')return element(id+'-svg');
      if(selector==='[data-chart-hit]')return element(id+'-hit');
      throw Error('Unexpected selector '+selector);
    },querySelectorAll(){return [];}};
}
function element(id){
  if(!elements[id]){
    assert(id.endsWith('-button')||id.endsWith('-svg')||id.endsWith('-hit')||html.includes(`id="${id}"`),id);
    elements[id]=makeElement(id);
  }
  return elements[id];
}
element('strength-data').textContent=dataScript[2];
const document={getElementById:element,createElement:()=>makeElement(),documentElement:makeElement()};
const context=vm.createContext({document,ResizeObserver:class{observe(){}},setTimeout,clearTimeout,
  setInterval(fn){intervalCallback=fn;return 1;},clearInterval(){intervalCallback=null;},
  matchMedia:()=>({matches:true}),addEventListener(type,fn){listeners[type]=fn;},
  openai:{setWidgetState(state){savedState=state;return Promise.resolve();}}});
context.window=context;
new vm.Script(fs.readFileSync(d3Path,'utf8')).runInContext(context);
for(const script of scripts)if(script!==dataScript&&script[2].trim())new vm.Script(script[2],{filename:'strength-preview.js'}).runInContext(context);
const api=context.FlowStrengthPreview;
assert(api && api.getModels().size===4);
assert.equal(savedState,null,'No persistence writes on initial render');
let layouts=0,unchangedMarks=0,relativeStyleChecks=0;
const close=(a,b)=>assert(Math.abs(a-b)<=Math.max(1,Math.abs(a))*1e-10,`${a} != ${b}`);
const metricKeys=['events','markers','buyQty','sellQty','notional'];
for(const windowData of api.data.windows){
  assert.equal(windowData.coins.ZEC.start_ms,windowData.coins.ENA.start_ms);
  for(const coin of ['ZEC','ENA']){
    const w=windowData.coins[coin];
    const dataBefore=JSON.stringify(w);
    for(const width of [320,504,736,1024])for(let step=1;step<=60;step++){
      const before=api.buildChart(w,step,width,'before'),after=api.buildChart(w,step,width,'after');
      assert.equal(before.begin,after.begin);assert.equal(before.end,after.end);
      assert.deepEqual(before.domain,after.domain);
      assert.equal(before.unit,after.unit);assert.equal(before.marks.length,after.marks.length);
      assert(!after.svg.includes('NaN')&&!after.svg.includes('undefined'));
      for(const tier of ['large','relative'])for(const key of metricKeys)assert.equal(before.total[tier][key],after.total[tier][key]);
      for(let i=0;i<before.marks.length;i++){
        const b=before.marks[i],a=after.marks[i];
        for(const key of ['key','x','y','r','qty','notional','count','rank','historySamples'])assert.equal(a[key],b[key]);
        assert.equal(a.r,context.FlowLargeTrades.radius(a.qty,after.unit));
        const f=after.frame;
        assert(a.x-a.r>=f.left-1e-7&&a.x+a.r<=f.right+1e-7,'Horizontal clipping');
        assert(a.y-a.r>=f.top-1e-7&&a.y+a.r<=f.bottom+1e-7,'Vertical clipping');
        if(a.tier==='relative'){assert.deepEqual(a.style,b.style);relativeStyleChecks++;}
        else {
          assert(a.historySamples>=20&&a.rank>=0&&a.rank<=1);
          assert.equal(a.style.highlighted,a.rank>=.8);
          assert(a.style.fillOpacity>0&&a.style.strokeOpacity>0,'Faint marks must remain drawn');
          assert(a.style.fillOpacity<=b.style.fillOpacity&&a.style.strokeOpacity<=b.style.strokeOpacity);
          const r=after.marks.find(m=>m.barIndex===a.barIndex&&m.side===a.side&&m.tier==='relative');
          if(r)assert(Math.abs(r.y-a.y)>=a.r+r.r+4.9999,'Same-bar tier overlap');
        }
        unchangedMarks++;
      }
      for(const [large,relative] of [[false,true],[true,false],[false,false]]){
        const filtered=api.buildChart(w,step,width,'after',large,relative);
        assert.deepEqual(filtered.total,after.total,'Visibility cannot change statistics');
        assert(filtered.marks.every(m=>m.tier==='large'?large:relative));
        for(const m of filtered.marks){const original=after.marks.find(a=>a.key===m.key);assert.equal(m.r,original.r);assert.equal(m.x,original.x);assert.equal(m.y,original.y);}
      }
      layouts++;
    }
    assert.equal(JSON.stringify(w),dataBefore,'Renderer must not mutate source data');
    const total=w.rows.reduce((a,r)=>{a[0]+=r[8]+r[9];a[1]+=r[10]+r[11];a[2]+=r[4]+r[5];a[3]+=r[12]+r[13];return a;},[0,0,0,0]);
    assert.equal(total[0],w.summary.large_events);assert.equal(total[1],w.summary.relative_events);
    close(total[2],w.summary.large_qty);close(total[3],w.summary.large_notional_usdt);
    assert.equal(w.ranks.flat().filter(r=>r!==null&&r>=.8).length,w.summary.highlighted_markers);
  }
}
assert(api.markStyle('large',null,'after').highlighted,'Cold start must preserve old emphasis');
assert(api.markStyle('large',.8,'after').highlighted);
assert(!api.markStyle('large',.799999,'after').highlighted);
let previousOpacity=0;
for(let i=0;i<=100;i++){const style=api.markStyle('large',i/100,'after');assert(style.fillOpacity>=previousOpacity);previousOpacity=style.fillOpacity;}

let interactions=0;
for(let sample=0;sample<api.data.windows.length;sample++){
  element('strength-window').value=String(sample);element('strength-window').events.change();
  for(const step of [1,30,60]){
    element('strength-step').value=String(step);element('strength-step').events.input();element('strength-step').events.change();
    assert.equal(api.getState().step,step);
    assert([...api.getModels().values()].every(m=>m.end===step));
    const totals=element('strength-statistics-body').innerHTML;
    const models=[...api.getModels().values()];assert(models.every(m=>m.begin===models[0].begin));
    for(const layer of ['large','relative']){
      element(layer+'-button').events.click();assert.equal(api.getState()[layer],false);
      assert([...api.getModels().values()].every(m=>m.marks.every(a=>a.tier!==layer)));
      assert.equal(element('strength-statistics-body').innerHTML,totals);
      element(layer+'-button').events.click();assert(api.getState()[layer]);
    }
    const model=api.getModels().get('ZEC-after'),mark=model.marks[0];
    if(mark){element('strength-zec-after-svg-hit').events.click({clientX:mark.x,clientY:mark.y,pointerType:'touch'});assert(element('strength-detail').textContent.includes('ZEC'));}
    interactions++;
  }
  element('strength-prev').events.click();assert.equal(api.getState().step,59);
  element('strength-next').events.click();assert.equal(api.getState().step,60);
}
element('strength-step').value='59';element('strength-step').events.input();element('strength-play').events.click();
assert(intervalCallback);intervalCallback();assert.equal(api.getState().step,60);assert.equal(intervalCallback,null);
element('strength-play').events.click();assert.equal(api.getState().step,1);assert(intervalCallback);
element('strength-play').events.click();assert.equal(intervalCallback,null);
element('strength-statistics').open=true;element('strength-statistics').events.toggle();assert(api.getState().statistics);
const writes=savedState;
listeners['openai:set_globals']({detail:{globals:{widgetState:{modelContent:{preview:'crypto-bar-side-strength'},privateContent:{sample:0,step:30,large:true,relative:false,statistics:true}}}}});
assert.equal(api.getState().step,30);assert(!api.getState().relative);assert.equal(savedState,writes,'State restoration must not write back');

const sharp=require(sharpPath);
const palettes={light:{background:'#ffffff',foreground:'#202e3b',border:'#dce2e9',green:'#16936e',red:'#d04f61'},
                dark:{background:'#171b21',foreground:'#e2e8ee',border:'#3c454e',green:'#36cea2',red:'#f47a87'}};
function themeSvg(svg,p){
  return svg.replace('<svg ','<svg xmlns="http://www.w3.org/2000/svg" ')
    .replaceAll('var(--foreground)',p.foreground).replaceAll('var(--border)',p.border).replaceAll('var(--green)',p.green).replaceAll('var(--red)',p.red)
    .replaceAll('<text ',`<text fill="${p.foreground}" font-size="12" font-family="Microsoft YaHei, Arial" `);
}
async function snapshot(sample,theme,width,file){
  const w=api.data.windows[sample],p=palettes[theme],wide=width>760,panelWidth=wide?(width-16)/2:width;
  let content='',y=80;
  const text=(x,y,value,size=12)=>`<text x="${x}" y="${y}" fill="${p.foreground}" font-size="${size}" font-family="Microsoft YaHei, Arial">${value}</text>`;
  content+=text(12,25,'ZEC / ENA · 大单显示强弱回放',18);
  content+=text(12,52,w.date+'—'+new Date(w.start_ms+1800000).toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false,hour:'2-digit',minute:'2-digit'})+' · '+w.label);
  for(const coin of ['ZEC','ENA']){
    for(const [i,mode] of ['before','after'].entries()){
      const m=api.buildChart(w.coins[coin],60,panelWidth,mode),x=wide?i*(panelWidth+16):0;
      content+=text(x+12,y,coin+' · '+(mode==='before'?'全部突出':'按强弱显示'),14);
      content+=text(x+12,y+23,`突出 ${m.total.large.highlighted} / 大单 ${m.total.large.markers} 点 · 空心 ${m.total.relative.markers} 点`);
      content+=`<g transform="translate(${x},${y+34})">${themeSvg(m.svg,p)}</g>`;
      if(!wide)y+=418;
    }
    if(wide)y+=434;
  }
  const svg=`<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${y}"><rect width="100%" height="100%" fill="${p.background}"/>${content}</svg>`;
  await sharp(Buffer.from(svg)).png().toFile(path.join(output,file));
}
(async()=>{
  await snapshot(0,'light',1024,'qa-matched-light.png');
  await snapshot(0,'dark',1024,'qa-matched-dark.png');
  await snapshot(1,'light',1024,'qa-dense-light.png');
  await snapshot(1,'dark',1024,'qa-dense-dark.png');
  await snapshot(0,'light',320,'qa-matched-narrow.png');
  const result={layout_pairs:layouts,unchanged_mark_geometry_cases:unchangedMarks,unchanged_relative_style_cases:relativeStyleChecks,
    interaction_cases:interactions,matching_time_axes:true,unchanged_statistics:true,production_radius_unchanged:true,
    weaker_marks_retained:true,monotone_emphasis:true,playback_stops_at_end:true,state_restoration:true,
    svg_snapshots:5,browser_visual_check:false,passed:true};
  fs.writeFileSync(path.join(output,'preview-checks.json'),JSON.stringify(result,null,2));
  console.log(JSON.stringify(result));
})().catch(error=>{console.error(error);process.exitCode=1;});
