"""Exercise dashboard analytics rendering without operating a desktop."""

import shutil
import subprocess
import unittest

import dashboard


class DashboardAnalyticsUiTests(unittest.TestCase):
    def test_missing_samples_process_details_and_stream_stability(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js required for the browser-script check.")
        script = dashboard.HTML.decode().split("<script>", 1)[1].split("</script>", 1)[0]
        harness = r"""
const assert=require('node:assert/strict'),vm=require('node:vm');
class Element{
 constructor(){this.children=[];this.events={};this.attributes={};this.dataset={};this.style={};this.animations=[];this.hidden=false;this.classes=new Set();this.classList={contains:n=>this.classes.has(n),add:n=>this.classes.add(n),remove:n=>this.classes.delete(n)};this.loads=0;this.textContent='';}
 appendChild(child){if(child.parent)child.parent.children=child.parent.children.filter(item=>item!==child);child.parent=this;this.children.push(child);return child;}
 replaceChildren(...children){this.children=[];for(const child of children)this.appendChild(child);}
 remove(){if(this.parent)this.parent.children=this.parent.children.filter(child=>child!==this);}
 removeAttribute(name){delete this.attributes[name];if(name==='src')this.address='';}
 insertBefore(child,before){if(child.parent)child.parent.children=child.parent.children.filter(item=>item!==child);child.pointer=null;child.parent=this;const index=before?this.children.indexOf(before):-1;if(index<0)this.children.push(child);else this.children.splice(index,0,child);}
 get nextSibling(){if(!this.parent)return null;return this.parent.children[this.parent.children.indexOf(this)+1]||null;}
 getBoundingClientRect(){const index=this.parent?this.parent.children.indexOf(this):0,translation=/translate\(([-\d.]+)px,([-\d.]+)px\)/.exec(this.style.transform||''),left=index*150+(translation?Number(translation[1]):0),top=translation?Number(translation[2]):0;return {left,top,width:100,height:200,right:left+100,bottom:top+200};}
 contains(node){return this===node||this.children.some(child=>child.contains(node));}
 closest(selector){let node=this;while(node){if(['button','input','select','textarea','a'].includes(node.tagName)&&selector.includes(node.tagName))return node;if((node.className==='process-details'||node.className==='windows-panel')&&selector.includes('.'+node.className))return node;node=node.parent;}return null;}
 setPointerCapture(id){this.pointer=id;}hasPointerCapture(id){return this.pointer===id;}releasePointerCapture(){this.pointer=null;}
 animate(frames,options){this.animations.push({frames,options});}
 requestFullscreen(){this.fullscreenRequests=(this.fullscreenRequests||0)+1;document.fullscreenElement=this;return Promise.resolve();}
 focus(){this.focused=true;}select(){this.selected=true;}
 setAttribute(n,v){this.attributes[n]=v;}addEventListener(n,f){this.events[n]=f;}
 set src(value){this.address=value;this.loads++;}get src(){return this.address;}
}
const pendingImages=[],elements=new Map(),documentEvents={},document={fullscreenElement:null,fullscreenExits:0,getElementById(id){if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},createElement(tag){const node=new Element();node.tagName=tag;return node;},createElementNS(namespace,tag){const node=new Element();node.tagName=tag;node.namespaceURI=namespace;return node;},addEventListener(name,callback){documentEvents[name]=callback;},exitFullscreen(){this.fullscreenElement=null;this.fullscreenExits++;return Promise.resolve();}};
class Image extends Element{constructor(){super();pendingImages.push(this);}}
let reloads=0;
const storage=new Map(),localStorage={getItem(key){return storage.get(key)||null;},setItem(key,value){storage.set(key,value);}};
let reduceMotion=false;
const context=vm.createContext({document,Image,localStorage,matchMedia:()=>({matches:reduceMotion}),location:{pathname:'/dashboard-token',reload(){reloads++;}},Date,Map,fetch:()=>new Promise(()=>{}),setInterval:()=>{}});
vm.runInContext(require('node:fs').readFileSync(0,'utf8'),context);
const run=code=>vm.runInContext(code,context);
const desktop={id:'one',sequence:5,name:'First desktop',state:'live',url:'http://127.0.0.1:12345/test',windows:[{pid:42,hwnd:10,title:'Main',status:'captured',sha256:'aaa',captured_at:Date.now()/1000},{pid:42,hwnd:11,title:'Other',status:'captured',sha256:'bbb',captured_at:Date.now()/1000}]},other={...desktop,id:'two',sequence:8,name:'Other desktop',windows:[]};
function render(metrics){context.fixture=[{...desktop,metrics},other];run('render(fixture)');}
render(undefined);const card=run('cards.get("one")'),second=run('cards.get("two")'),image=card.image;
assert.equal(card.title.dataset.sequence,'05');assert.equal(second.title.dataset.sequence,'08');
assert.equal(card.renameButton,card.title,'the desktop name itself must be the rename control');assert.equal(card.title.tagName,'button');assert.equal(card.title.className,'desktop-name');assert.ok(!card.node.children[0].children.some(child=>child.className==='rename-button'),'there must be no separate pencil control');
assert.equal(card.metricsFields.cpu.metric.parent.parent,card.node.children[0],'metrics must stay inside the desktop header');
for(const field of Object.values(card.metricsFields)){assert.equal(field.metric.children.length,2);assert.equal(field.metric.children[0].tagName,'svg');assert.equal(field.metric.children[0].attributes['aria-hidden'],'true');assert.equal(field.metric.children[0].children[0].tagName,'path');assert.equal(field.metric.children[1],field.value,'only an icon and value should be visible');}
function renameInput(){return card.node.children[0].children.find(child=>child.className==='desktop-name-input');}
card.renameButton.events.click();let input=renameInput();assert.equal(input.focused,true);input.value='Renamed desktop';input.events.keydown({key:'Enter',preventDefault(){}});assert.equal(card.title.textContent,'Renamed desktop');assert.equal(card.name,'First desktop','renaming must not change the source identity');assert.equal(JSON.parse(storage.get('stealth-desktop-names')).one,'Renamed desktop');assert.equal(run('loadNames().one'),'Renamed desktop');assert.equal(card.title.dataset.sequence,'05');
card.renameButton.events.click();input=renameInput();input.value='Cancelled';input.events.keydown({key:'Escape',preventDefault(){}});assert.equal(card.title.textContent,'Renamed desktop');
card.renameButton.events.click();input=renameInput();input.value='Saved on blur';input.events.blur();assert.equal(card.title.textContent,'Saved on blur');
for(const metric of Object.values(card.metricsFields))assert.equal(metric.value.textContent,'—','missing values must not become zero');
assert.equal(card.details.hidden,true);assert.equal(card.processBody.children.length,0);assert.equal(second.details.hidden,true);
const process={pid:42,name:'python.exe',cpu_percent:null,rss_bytes:536870912,private_bytes:268435456,read_bytes_per_second:1024,write_bytes_per_second:0,threads:8,handles:120,uptime_seconds:3661,windows:[{hwnd:10,title:'Main window'},{hwnd:11,title:'Second window'}]};
const metrics={cpu_percent:3.25,rss_bytes:536870912,private_bytes:268435456,process_count:1,window_count:2,capture_fps:4.95,frame_age_seconds:0.2,processes:[process],observer:{cpu_percent:0.1,rss_bytes:33554432},gpu:{percent:7.3,label:'Busiest GPU engine',available:true}};
render(metrics);assert.equal(card.metricsFields.cpu.value.textContent,'3.3%');assert.equal(card.metricsFields.ram.value.textContent,'512 MiB');assert.equal(card.metricsFields.gpu.value.textContent,'7.3%');assert.equal(card.metricsFields.fps.value.textContent,'5.0');
assert.ok(card.metricsFields.cpu.metric.title.includes('total logical-processor capacity'));assert.ok(card.metricsFields.gpu.metric.title.includes('Busiest GPU engine'));assert.ok(card.metricsFields.gpu.metric.title.includes('7.3%'));assert.equal(card.metricsFields.cpu.metric.attributes['aria-label'],'App CPU: 3.3%');assert.equal(card.metricsFields.fps.metric.attributes['aria-label'],'Capture FPS: 5.0');
assert.equal(card.processBody.children.length,0,'closed details must not create process rows');
card.detailsButton.events.click();assert.equal(card.details.hidden,false);assert.equal(card.detailsButton.attributes['aria-expanded'],'true');
assert.equal(card.processBody.children.length,1,'two windows sharing a process must remain one process row');
let row=card.processRows.get(42);assert.equal(row.name.textContent,'python.exe · 42');assert.equal(row.windows.textContent,'Main window\nSecond window');assert.equal(row.cells[1].textContent,'—');assert.equal(row.cells[3].textContent,'1.0 KiB/s / 0 B/s');assert.equal(row.cells[6].textContent,'1h 1m');
assert.ok(card.processSummary.textContent.includes('1 processes · 2 windows'));assert.ok(card.processSummary.textContent.includes('Observer 0.1% / 32.0 MiB'));
const stable=row.node;render({...metrics,processes:[{...process,cpu_percent:2.0,windows:[{hwnd:11,title:'Renamed window'}]}]});assert.equal(card.processRows.get(42).node,stable);assert.equal(row.windows.textContent,'Renamed window');assert.equal(row.cells[1].textContent,'2.0%');
render({...metrics,processes:[process,process]});assert.equal(card.processBody.children.length,1,'duplicate PIDs must not create duplicate rows');
const many=Array.from({length:180},(_,index)=>({...process,pid:index+100,windows:[]}));
render({...metrics,process_count:many.length,processes:many});assert.equal(card.processBody.children.length,180);assert.equal(card.processRows.has(42),false);
card.detailsButton.events.click();assert.equal(card.processBody.children.length,0);assert.equal(card.processRows.size,0);
for(let count=0;count<5;count++)render({...metrics,process_count:180,processes:many});assert.equal(card.processBody.children.length,0,'polling while details are closed must not grow the table');assert.equal(second.details.hidden,true);
card.detailsButton.events.click();assert.equal(card.processBody.children.length,180);
render({...metrics,processes:[{...process,name:'<img src=x onerror=alert(1)>',windows:[{title:'<script>private title</script>'}]}]});row=card.processRows.get(42);assert.equal(card.processBody.children.length,1);assert.equal(row.name.textContent,'<img src=x onerror=alert(1)> · 42');assert.equal(row.windows.textContent,'<script>private title</script>');
render({...metrics,cpu_percent:0,gpu:{percent:0,available:true},capture_fps:0});assert.equal(card.metricsFields.cpu.value.textContent,'0.0%');assert.equal(card.metricsFields.gpu.value.textContent,'0.0%');assert.equal(card.metricsFields.fps.value.textContent,'0.0');
render({...metrics,cpu_percent:NaN,gpu:{percent:0,available:false},capture_fps:null});assert.equal(card.metricsFields.cpu.value.textContent,'—');assert.equal(card.metricsFields.gpu.value.textContent,'—');assert.equal(card.metricsFields.fps.value.textContent,'—');
assert.equal(card.image,image);assert.equal(card.image.loads,1,'metric polling and detail controls must not reconnect the desktop stream');
card.button.events.click();assert.equal(card.node.classList.contains('expanded'),true);assert.equal(card.node.fullscreenRequests,1);assert.equal(document.fullscreenElement,card.node);assert.equal(card.image.loads,1);assert.equal(card.button.textContent,String.fromCodePoint(0x26f6));
card.button.events.click();assert.equal(document.fullscreenExits,1);documentEvents.fullscreenchange();assert.equal(card.node.classList.contains('expanded'),false);
card.node.requestFullscreen=undefined;card.button.events.click();assert.equal(card.node.classList.contains('expanded'),true,'fullscreen must retain a CSS fallback');documentEvents.keydown({key:'Escape'});assert.equal(card.node.classList.contains('expanded'),false);
const pointer=(target,x,y,id=1)=>({target,button:0,pointerId:id,clientX:x,clientY:y,preventDefault(){}});
card.node.events.pointerdown(pointer(card.title,30,10));assert.equal(run('activeDrag'),null,'interactive controls must not start panel dragging');
card.node.events.pointerdown(pointer(card.node.children[0],30,10));card.node.events.pointermove(pointer(card.node,230,20));assert.equal(card.node.classList.contains('dragging'),true);assert.equal(card.node.hasPointerCapture(1),true,'DOM swap must re-acquire pointer capture');assert.ok(card.node.style.transform.includes('translate'));card.node.events.pointerup(pointer(card.node,230,20));assert.equal(card.node.style.transform,'');assert.equal(card.node.classList.contains('dragging'),false);assert.ok(card.node.animations.length>0,'drop should animate into its snapped grid position');
assert.equal(document.getElementById('grid').children[0],second.node);assert.equal(document.getElementById('grid').children[1],card.node);render(metrics);assert.equal(document.getElementById('grid').children[0],second.node,'polling must preserve local drag order');assert.equal(card.title.dataset.sequence,'05','reordering must not renumber desktop labels');
assert.equal(JSON.parse(storage.get('stealth-desktop-order')).join(','),'two,one');
reduceMotion=true;const animations=card.node.animations.length+second.node.animations.length;card.node.events.pointerdown(pointer(card.node.children[0],180,10,2));card.node.events.pointermove(pointer(card.node,20,20,2));card.node.events.pointerup(pointer(card.node,20,20,2));assert.equal(card.node.animations.length+second.node.animations.length,animations,'reduced-motion preference must disable snap animations');reduceMotion=false;
let metadataCalls=0;context.fetch=()=>{metadataCalls++;return new Promise(()=>{});};run('polling=false');const otherStreamLoads=second.image.loads;card.refreshButton.events.click();assert.equal(card.image.loads,2,'refresh must reconnect only the selected desktop stream');assert.equal(second.image.loads,otherStreamLoads);assert.equal(metadataCalls,1,'refresh must request current metadata');
assert.equal(pendingImages.length,0,'desktop mode must not request individual window images');
document.getElementById('windowsView').events.click();assert.equal(card.surface.hidden,true);assert.equal(card.windowsPanel.hidden,false);assert.equal(card.image.src,'');assert.equal(pendingImages.length,2);
let candidate=pendingImages.shift();assert.ok(candidate.src.includes('/window?hwnd=10&sha256=aaa'));candidate.onload();let firstWindow=card.windowCards.get('42:10');const originalWindowNode=firstWindow.node;assert.equal(firstWindow.hash,'aaa');
candidate=pendingImages.shift();candidate.onerror();assert.equal(card.windowCards.get('42:11').status.textContent,'Unavailable');
render(metrics);assert.equal(pendingImages.length,0,'a failed first image must wait before retrying');
card.windowCards.get('42:11').nextRetry=0;render(metrics);assert.equal(pendingImages.length,1);pendingImages.shift().onload();
render(metrics);assert.equal(pendingImages.length,0);assert.equal(firstWindow.node,originalWindowNode,'window cards must survive health polling');
card.refreshButton.events.click();assert.equal(pendingImages.length,2,'window-mode refresh must request fresh individual captures');assert.equal(firstWindow.image.hidden,false,'manual refresh must retain the previous image until replacement loads');for(const image of pendingImages.splice(0))image.onload();
const updated={...desktop,windows:[{...desktop.windows[0],sha256:'ccc'}]};context.fixture=[{...updated,metrics},other];run('render(fixture)');assert.equal(card.windowCards.size,1);assert.equal(pendingImages.length,1);const late=pendingImages.shift();
document.getElementById('desktopsView').events.click();assert.equal(late.onload,null,'switching modes must cancel pending window loads');assert.equal(card.surface.hidden,false);assert.equal(card.windowsPanel.hidden,true);assert.equal(card.image.loads,3);
render(metrics);assert.equal(pendingImages.length,0,'hidden windows must not make image requests');
document.getElementById('windowsView').events.click();assert.equal(pendingImages.length,1);const removedCandidate=pendingImages.shift(),removedCallback=removedCandidate.onload;
context.fixture=[other];run('render(fixture)');removedCallback();assert.equal(run('cards.has("one")'),false,'removed desktops and late images cannot revive cards');assert.equal(document.getElementById('grid').children.length,1);
context.fixture=[other,{...other,id:'three',name:'Third desktop'}];run('render(fixture)');assert.equal(document.getElementById('grid').children.length,2,'newly discovered desktops must appear');
assert.equal(card.image.loads,3,'window mode must not restart a hidden composed stream');
let payload={build:'first',desktops:[other]};context.fetch=async()=>({ok:true,json:async()=>payload});run('polling=false');
(async()=>{await run('refresh()');assert.equal(reloads,0);payload={...payload,build:'second'};await run('refresh()');assert.equal(reloads,1);await run('refresh()');assert.equal(reloads,1,'a changed build must trigger only one reload');})().catch(error=>{console.error(error);process.exitCode=1;});
console.log('Analytics samples, shared windows, stable process rows, closed details, and live stream stability passed.');
"""
        completed = subprocess.run([node, "-e", harness], input=script, text=True, encoding="utf-8", capture_output=True,
                                   timeout=25, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
