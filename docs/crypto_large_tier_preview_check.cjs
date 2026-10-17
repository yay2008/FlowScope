/* Offline numerical, interaction and SVG checks; no browser navigation. */
const fs=require('node:fs'), path=require('node:path'), vm=require('node:vm'), assert=require('node:assert/strict');
const [fragmentPath,d3Path,sharpPath,output]=process.argv.slice(2);
const html=fs.readFileSync(fragmentPath,'utf8');
const scripts=[...html.matchAll(/<script([^>]*)>([\s\S]*?)<\/script>/g)];
const dataScript=scripts.find(s=>s[1].includes('application/json'));
assert(dataScript);
const elements={};let displayWidth=736,intervalCallback=null,savedState=null;
function makeElement(id=''){
  return {id,value:'',textContent:'',checked:false,hidden:false,style:{},attributes:{},children:[],events:{},_html:'',
    set innerHTML(value){this._html=value;},get innerHTML(){return this._html;},
    append(child){this.children.push(child);if(this.children.length===1)this.value=child.value;},
    replaceChildren(){this.children=[];this.value='';},
    addEventListener(type,fn){this.events[type]=fn;},setAttribute(key,value){this.attributes[key]=value;},
    getAttribute(key){return this.attributes[key];},
    getBoundingClientRect(){return {left:0,top:0,width:displayWidth,height:422};},
    querySelector(selector){
      if(selector.startsWith('#'))return element(selector.slice(1));
      if(selector.startsWith('[data-layer='))return element(selector.includes('relative')?'relative-button':'large-button');
      if(selector==='svg')return element('svg');
      if(selector==='[data-chart-hit]')return element('hit');
      throw Error('Unexpected selector '+selector);
    },querySelectorAll(){return [];}};
}
function element(id){
  if(!elements[id]){
    assert(['relative-button','large-button','svg','hit'].includes(id)||html.includes(`id="${id}"`),id);
    elements[id]=makeElement(id);
  }
  return elements[id];
}
element('tier-data').textContent=dataScript[2];
const document={getElementById:element,createElement:()=>makeElement(),documentElement:makeElement()};
const context=vm.createContext({document,ResizeObserver:class{observe(){}},
  setTimeout,clearTimeout,setInterval(fn){intervalCallback=fn;return 1;},clearInterval(){intervalCallback=null;},
  matchMedia:()=>({matches:true}),addEventListener(){},
  openai:{setWidgetState(state){savedState=state;return Promise.resolve();}}});
context.window=context;
new vm.Script(fs.readFileSync(d3Path,'utf8')).runInContext(context);
for(const script of scripts){
  if(script===dataScript||!script[2].trim())continue;
  new vm.Script(script[2],{filename:'preview-script.js'}).runInContext(context);
}
const api=context.FlowTierPreview;
assert(api && api.getModel().marks.length>0);
let layouts=0,radiusCases=0,coexistingChecks=0;
for(const windows of Object.values(api.data.views))for(const sample of windows){
  for(const width of [320,736,1024]){
    const steps=width===736?Array.from({length:60},(_,i)=>i+1):[1,30,60];
    for(const step of steps){
      const model=api.buildChart(sample,step,width,true,true);
      const originalOnly=api.buildChart(sample,step,width,true,false);
      assert(!model.svg.includes('NaN')&&!model.svg.includes('undefined'));
      assert.equal(model.total.large.markers,model.marks.filter(m=>m.tier==='large').length);
      assert.equal(model.total.relative.markers,model.marks.filter(m=>m.tier==='relative').length);
      for(const mark of model.marks){
        assert.equal(mark.r,context.FlowLargeTrades.radius(mark.qty,model.unit));radiusCases++;
        const f=model.frame;
        assert(mark.x-mark.r>=f.left-1e-7 && mark.x+mark.r<=f.right+1e-7,'Horizontal clip');
        assert(mark.y-mark.r>=f.top-1e-7 && mark.y+mark.r<=f.bottom+1e-7,'Vertical clip');
        if(mark.tier==='large'){
          const unchanged=originalOnly.marks.find(m=>m.key===mark.key);
          assert(unchanged);assert.equal(mark.r,unchanged.r);assert.equal(mark.x,unchanged.x);assert.equal(mark.y,unchanged.y);
          const relative=model.marks.find(m=>m.barIndex===mark.barIndex&&m.side===mark.side&&m.tier==='relative');
          if(relative){assert(Math.abs(relative.y-mark.y)>=mark.r+relative.r+4.9999);coexistingChecks++;}
        }
      }
      layouts++;
    }
  }
}
let interactionCases=0;
for(const name of Object.keys(api.data.views)){
  element('tier-view').value=name;element('tier-view').events.change();
  for(const sample of ['0','1']){
    element('tier-window').value=sample;element('tier-window').events.change();
    element('tier-step').value='30';element('tier-step').events.input();element('tier-step').events.change();
    assert.equal(api.getState().step,30);
    element('relative-button').events.click();assert(!api.getState().relative);
    assert(api.getModel().marks.every(m=>m.tier==='large'));
    element('relative-button').events.click();assert(api.getState().relative);
    element('tier-prev').events.click();assert.equal(api.getState().step,29);
    element('tier-next').events.click();assert.equal(api.getState().step,30);
    interactionCases++;
  }
}
element('tier-step').value='59';element('tier-step').events.input();
element('tier-play').events.click();assert(intervalCallback);
intervalCallback();assert.equal(api.getState().step,60);assert.equal(intervalCallback,null);
assert(savedState && savedState.privateContent.step===60);
const sharp=require(sharpPath);
const palettes={light:{background:'#ffffff',foreground:'#202e3b',border:'#dce2e9',green:'#16936e',red:'#d04f61'},
                dark:{background:'#171b21',foreground:'#e2e8ee',border:'#3c454e',green:'#36cea2',red:'#f47a87'}};
async function snapshot(view,sample,width,theme,file){
  const w=api.data.views[view][sample],m=api.buildChart(w,60,width,true,true),p=palettes[theme];
  let svg=m.svg.replace('<svg ','<svg xmlns="http://www.w3.org/2000/svg" ');
  svg=svg.replaceAll('var(--foreground)',p.foreground).replaceAll('var(--border)',p.border).replaceAll('var(--green)',p.green).replaceAll('var(--red)',p.red);
  svg=svg.replaceAll('<text ','<text fill="'+p.foreground+'" font-size="12" font-family="Microsoft YaHei, Arial" ');
  const label=`${view} ${w.date} · ${width}px`;
  const wrapped=`<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="480"><rect width="100%" height="100%" fill="${p.background}"/><text x="12" y="22" fill="${p.foreground}" font-size="14" font-family="Microsoft YaHei, Arial">${label}</text><g transform="translate(0,44)">${svg}</g></svg>`;
  await sharp(Buffer.from(wrapped)).png().toFile(path.join(output,file));
}
(async()=>{
  await snapshot('AGG.BTC',0,736,'light','qa-btc-typical-light.png');
  await snapshot('AGG.BTC',1,736,'dark','qa-btc-dense-dark.png');
  await snapshot('AGG.ENA',1,320,'light','qa-ena-dense-narrow.png');
  await snapshot('AGG.ZEC',1,736,'light','qa-zec-dense-light.png');
  const result={offline_layout_cases:layouts,radius_cases:radiusCases,coexisting_non_overlap_cases:coexistingChecks,
    interaction_cases:interactionCases,playback_stops_at_end:true,large_marker_geometry_unchanged_by_toggle:true,
    svg_snapshots:4,browser_visual_check:false,passed:true};
  fs.writeFileSync(path.join(output,'preview-checks.json'),JSON.stringify(result,null,2));
  console.log(JSON.stringify(result));
})().catch(error=>{console.error(error);process.exitCode=1;});
