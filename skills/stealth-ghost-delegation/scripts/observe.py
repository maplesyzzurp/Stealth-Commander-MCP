"""Serve the latest private-desktop capture to a local browser, read-only."""

import argparse
import ctypes
import hashlib
import http.server
import ipaddress
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import parse_qs, urlsplit

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_CAPTURE_BYTES = 32 * 1024 * 1024
MAX_RENDER_PIXELS = 12_000_000
MAX_RENDER_DIMENSION = 2560
STREAM_FPS = 60
CAPTURE_METHODS = ("PrintWindow", "D3D11")
MAX_WINDOWS = 32
MAX_COORDINATE = 100_000
MAX_DIMENSION = 32_768
STATUSES = {"starting", "running", "stopped", "failed", "cleanup_failed"}
HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>Hidden desktop observer</title>
<style>
:root{color-scheme:dark;--bg:#070b10;--panel:#0d141f;--panel-alt:#111b29;--border:#233246;--border-hi:#3b82f6;--text:#e6edf7;--muted:#8fa3bf;--ok:#22c55e;--warn:#f59e0b;--err:#ef4444;--accent:#38bdf8;--mono:ui-monospace,SFMono-Regular,Consolas,monospace}
*{box-sizing:border-box}
body{font:12px/1.35 system-ui,-apple-system,Segoe UI,sans-serif;margin:0;padding:0;color:var(--text);background:var(--bg)}
main{max-width:100%;margin:0 auto;padding:.45rem .7rem 1rem}
.top-deck{position:sticky;top:0;z-index:20;display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:.45rem .75rem;padding:.38rem .65rem;margin:-.45rem -.7rem .5rem;background:rgba(7,11,16,.92);backdrop-filter:blur(10px);border-bottom:1px solid var(--border)}
.brand-row{display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;min-width:0}
h1{font-size:.9rem;font-weight:700;letter-spacing:.01em;margin:0;white-space:nowrap}
h2{font-size:.84rem;font-weight:600;margin:.15rem 0}
h3{font-size:.8rem;font-weight:600;margin:0}
.fps-pill{font:700 .66rem/1 var(--mono);padding:.2rem .42rem;border-radius:999px;background:rgba(34,197,94,.14);color:#4ade80;border:1px solid rgba(34,197,94,.38);letter-spacing:.04em}
#state{margin:0;padding:.15rem .45rem;border-radius:4px;background:var(--panel-alt);border:1px solid var(--border);font-weight:600;font-size:.74rem;color:var(--text)}
#details{margin:0;color:var(--muted);font:500 .7rem/1.25 var(--mono);overflow-wrap:anywhere}
.telemetry-ribbon{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:.35rem;margin:0 0 .45rem}
.t-cell{background:var(--panel);border:1px solid var(--border);border-radius:5px;padding:.25rem .48rem;min-width:0;display:flex;align-items:center;justify-content:space-between;gap:.4rem}
.t-label{font-size:.62rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);white-space:nowrap}
.t-val{font:600 .74rem/1.2 var(--mono);color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-variant-numeric:tabular-nums}
.toolbar-row{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:.4rem;margin:.38rem 0;padding:.3rem .5rem;background:var(--panel);border:1px solid var(--border);border-radius:6px}
.view-options{display:flex;gap:.25rem;flex-wrap:wrap;align-items:center;margin:0}
.ctrl-sep{width:1px;height:1rem;background:var(--border);margin:0 .15rem}
button{font:600 .7rem/1.15 system-ui,sans-serif;color:var(--text);background:#162233;border:1px solid #2e4159;border-radius:4px;padding:.24rem .5rem;cursor:pointer;transition:background .12s,border-color .12s}
button:hover{background:#1f3047;border-color:#4b678a}
button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
button[aria-pressed="true"]{background:#1d3a5f;border-color:var(--accent);color:#fff;box-shadow:inset 0 0 0 1px rgba(56,189,248,.25)}
[hidden]{display:none!important}
.subnote{color:var(--muted);font-size:.78rem;margin:.2rem 0 .5rem}
#workspaceInfo{margin:0;font:600 .8rem/1.3 var(--mono);color:var(--accent)}
#zOrderStrip{display:flex;gap:.38rem;overflow-x:auto;padding:.35rem 0 .45rem;margin:0 0 .45rem;scrollbar-width:thin}
.z-chip{display:inline-flex;align-items:center;gap:.38rem;white-space:nowrap;font:500 .74rem/1.1 var(--mono);padding:.28rem .52rem;border-radius:5px;background:var(--panel);border:1px solid var(--border);color:var(--text)}
.z-chip[data-state="stale"]{border-color:var(--warn);color:#fcd34d}
.z-chip[data-state="unavailable"]{border-color:var(--err);color:#fca5a5}
#windowList{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,440px),1fr));gap:.75rem;list-style:none;padding:0;margin:0}
#workspace[data-cols="1"] #windowList{grid-template-columns:1fr}
#workspace[data-cols="2"] #windowList{grid-template-columns:repeat(auto-fit,minmax(min(100%,640px),1fr))}
#workspace[data-cols="dense"] #windowList{grid-template-columns:repeat(auto-fit,minmax(min(100%,310px),1fr));gap:.5rem}
.window-card{min-width:0;overflow:hidden;border:1px solid var(--border);border-radius:7px;background:var(--panel);display:flex;flex-direction:column;transition:border-color .15s}
.window-card:hover{border-color:#3d5675}
.window-card header{padding:.52rem .7rem;display:flex;align-items:center;justify-content:space-between;gap:.6rem;background:var(--panel-alt);border-bottom:1px solid var(--border)}
.window-card h3{font-size:.88rem;font-weight:600;margin:0;overflow-wrap:anywhere}
.window-status{color:var(--muted);font:500 .74rem/1.25 var(--mono);margin:.2rem 0 0;font-variant-numeric:tabular-nums}
.window-card .window-image{width:100%;height:clamp(260px,46vh,680px);max-height:none;object-fit:contain;border:0;background:repeating-conic-gradient(#090e15 0% 25%,#0c131d 0% 50%) 50%/16px 16px;image-rendering:-webkit-optimize-contrast}
#workspace[data-scale="theater"] .window-card .window-image{height:clamp(420px,76vh,1080px)}
#workspace[data-scale="actual"] .window-card .window-image{width:auto;height:auto;max-width:none;max-height:none;object-fit:none}
.window-card[data-state="current"]{border-color:#264638}
.window-card[data-state="stale"]{border-color:var(--warn)}
.window-card[data-state="unavailable"]{border-color:var(--err)}
.window-empty{padding:1rem;color:var(--muted);font-size:.82rem}
.window-card .window-empty{min-height:160px;margin:0;display:grid;place-items:center}
img{display:block;max-width:100%;max-height:calc(100vh - 130px);width:auto;height:auto;object-fit:contain;border:1px solid var(--border);background:#090e15;image-rendering:-webkit-optimize-contrast}
.frame{min-height:4rem;margin:0}
#sceneViewport{width:100%;max-height:76vh;overflow:auto;display:flex;justify-content:center;align-items:center;background:repeating-conic-gradient(#080d14 0% 25%,#0b121c 0% 50%) 50%/18px 18px;border:1px solid var(--border);border-radius:7px;padding:4px}
#liveScene{display:block;width:auto;height:auto;max-width:100%;max-height:75vh;object-fit:contain;border:0;image-rendering:-webkit-optimize-contrast}
#workspace[data-scale="theater"] #sceneViewport{max-height:88vh}
#workspace[data-scale="theater"] #liveScene{max-height:87vh}
#workspace[data-scale="actual"] #sceneViewport{max-height:88vh;justify-content:flex-start;align-items:flex-start}
#workspace[data-scale="actual"] #liveScene{max-width:none;max-height:none;object-fit:none}
#detail{margin-top:.85rem;padding:.7rem;border:1px solid var(--border-hi);border-radius:7px;background:var(--panel)}
#detail img{max-height:85vh;width:auto;margin-top:.4rem}
#viewNote{color:var(--muted);font:500 .75rem/1.35 var(--mono);margin:.75rem 0 0;padding-top:.5rem;border-top:1px solid var(--border)}
</style></head><body><main>
<div class="top-deck">
  <div class="brand-row">
    <h1>Hidden desktop observer</h1>
    <span class="fps-pill" id="fpsBadge">Capture speed · measuring</span>
    <p id="state" role="status" aria-live="polite">Connecting to the local capture stream…</p>
  </div>
  <p id="details"></p>
</div>
<div class="telemetry-ribbon" aria-label="Observer telemetry">
  <div class="t-cell"><span class="t-label">Session Status</span><span class="t-val" id="tStatus">starting</span></div>
  <div class="t-cell"><span class="t-label">Active Windows</span><span class="t-val" id="tWindows">0</span></div>
  <div class="t-cell"><span class="t-label">Workspace Bounds</span><span class="t-val" id="tBounds">--</span></div>
  <div class="t-cell"><span class="t-label">Capture Freshness</span><span class="t-val" id="tAge">--</span></div>
  <div class="t-cell"><span class="t-label">Live Capture Speed</span><span class="t-val" id="tCadence">Measuring…</span></div>
</div>
<section id="single"><p class="frame"><img id="frame" alt="Latest captured window; refreshed about once per second" hidden></p></section>
<section id="workspace" data-scale="fit" data-cols="auto" hidden>
  <div class="toolbar-row">
    <div class="view-options" role="group" aria-label="Observer view">
      <button id="panelsView" type="button" aria-pressed="true">Window panels</button>
      <button id="layoutView" type="button" aria-pressed="false">Desktop layout stream (target up to 60 FPS)</button>
      <span class="ctrl-sep" aria-hidden="true"></span>
      <button id="scaleFitBtn" type="button" aria-pressed="true">Fit</button>
      <button id="scaleTheaterBtn" type="button" aria-pressed="false">Theater</button>
      <button id="scaleActualBtn" type="button" aria-pressed="false">1:1 Pixel</button>
      <span class="ctrl-sep" aria-hidden="true"></span>
      <button id="colsAutoBtn" type="button" aria-pressed="true">Auto Grid</button>
      <button id="colsWideBtn" type="button" aria-pressed="false">Full Width</button>
      <button id="colsDenseBtn" type="button" aria-pressed="false">Dense Matrix</button>
    </div>
    <p id="workspaceInfo"></p>
  </div>
  <p class="subnote">Each window has its own live panel. New windows appear automatically. Viewing does not move or control the applications. Some GPU or protected surfaces may be unavailable.</p>
  <div id="zOrderStrip" aria-label="Window Z-order inspector"></div>
  <div id="panels"><p id="emptyWindows" class="window-empty" hidden>No visible application windows. Waiting for a window to open.</p><ul id="windowList"></ul></div>
  <section id="layoutViewContent" hidden><h3>Reconstructed window layout</h3><p class="subnote">Live 4:4:4 JPEG stream · target ceiling of 60 FPS with resource scaling · captured windows placed over a dark background in exact Z-order. Actual speed depends on the applications and host. Native desktop video and some rendering surfaces are unavailable.</p><div id="sceneViewport"><img id="liveScene" alt="Live reconstructed view of captured windows" hidden></div></section>
  <section id="detail" hidden><h2 id="detailTitle"></h2><p id="detailStatus" class="window-status"></p><img id="detailImage" alt="Selected captured window"></section>
</section>
<p id="viewNote">View only. This page cannot control the session.</p>
</main><script>
const base=location.pathname, state=document.getElementById('state'), details=document.getElementById('details');
const single=document.getElementById('single'), frame=document.getElementById('frame'), workspace=document.getElementById('workspace');
const liveScene=document.getElementById('liveScene'), workspaceInfo=document.getElementById('workspaceInfo'), windowList=document.getElementById('windowList');
const panels=document.getElementById('panels'), panelsView=document.getElementById('panelsView'), layoutView=document.getElementById('layoutView'), layoutViewContent=document.getElementById('layoutViewContent');
const detail=document.getElementById('detail'), detailTitle=document.getElementById('detailTitle'), detailStatus=document.getElementById('detailStatus'), detailImage=document.getElementById('detailImage');
const tStatus=document.getElementById('tStatus'), tWindows=document.getElementById('tWindows'), tBounds=document.getElementById('tBounds'), tAge=document.getElementById('tAge'), tCadence=document.getElementById('tCadence');
const zOrderStrip=document.getElementById('zOrderStrip');
const scaleFitBtn=document.getElementById('scaleFitBtn'), scaleTheaterBtn=document.getElementById('scaleTheaterBtn'), scaleActualBtn=document.getElementById('scaleActualBtn');
const colsAutoBtn=document.getElementById('colsAutoBtn'), colsWideBtn=document.getElementById('colsWideBtn'), colsDenseBtn=document.getElementById('colsDenseBtn');
const fpsBadge=document.getElementById('fpsBadge');
let singleHash='', singleLoading='', currentLayout=null, selectedKey=null, polling=false, detailGeneration=0, detailHash='', view='panels', scaleMode='fit', colsMode='auto', lastCaptureSample=null, liveCaptureFps=null;
const cards=new Map();
function captureMethodLabel(method){
  return method==='D3D11'?'D3D11':method==='PrintWindow'?'PrintWindow':'Backend unknown';
}
function captureBackendSummary(singleMethod){
  if(!currentLayout)return captureMethodLabel(singleMethod);
  const methods=[...new Set(currentLayout.windows.filter(item=>item.status==='captured').map(item=>captureMethodLabel(item.capture_method)))];
  return methods.length?methods.join(' + '):'No captured windows';
}
function updateFpsTelemetry(singleMethod){
  const measured=Number.isFinite(liveCaptureFps)&&liveCaptureFps>=0;
  const fpsTxt=measured?liveCaptureFps.toFixed(1)+' FPS measured':'Measuring capture speed';
  fpsBadge.textContent=captureBackendSummary(singleMethod)+' · '+fpsTxt;
  fpsBadge.title='Capture target ceiling: 60 FPS. Actual speed depends on the applications and host.';
  tCadence.textContent=(measured?fpsTxt+' · ':'')+(view==='layout'?'live 4:4:4 MJPEG':'panel view');
}
function selectView(next){
  view=next;panels.hidden=next!=='panels';layoutViewContent.hidden=next!=='layout';
  panelsView.setAttribute('aria-pressed',String(next==='panels'));layoutView.setAttribute('aria-pressed',String(next==='layout'));
  updateFpsTelemetry();
  if(next==='layout'){liveScene.src=base+'/stream.mjpg';liveScene.hidden=false;}
  else{liveScene.removeAttribute('src');liveScene.hidden=true;}
  updateViewNote();
}
function selectScale(next){
  scaleMode=next;workspace.dataset.scale=next;
  scaleFitBtn.setAttribute('aria-pressed',String(next==='fit'));
  scaleTheaterBtn.setAttribute('aria-pressed',String(next==='theater'));
  scaleActualBtn.setAttribute('aria-pressed',String(next==='actual'));
}
function selectCols(next){
  colsMode=next;workspace.dataset.cols=next;
  colsAutoBtn.setAttribute('aria-pressed',String(next==='auto'));
  colsWideBtn.setAttribute('aria-pressed',String(next==='1'));
  colsDenseBtn.setAttribute('aria-pressed',String(next==='dense'));
}
function updateViewNote(){document.getElementById('viewNote').textContent=(view==='panels'?'Window panels refresh about once per second.':'Live JPEG stream with a target ceiling of 60 frames per second.')+' Actual capture speed depends on the applications and host. This page cannot control the session.';}
panelsView.addEventListener('click',()=>{if(view!=='panels')selectView('panels');});
layoutView.addEventListener('click',()=>{if(view!=='layout')selectView('layout');});
scaleFitBtn.addEventListener('click',()=>selectScale('fit'));
scaleTheaterBtn.addEventListener('click',()=>selectScale('theater'));
scaleActualBtn.addEventListener('click',()=>selectScale('actual'));
colsAutoBtn.addEventListener('click',()=>selectCols('auto'));
colsWideBtn.addEventListener('click',()=>selectCols('1'));
colsDenseBtn.addEventListener('click',()=>selectCols('dense'));
function windowKey(item){return item.pid+':'+item.hwnd;}
function formatGeom(item){
  if(!Array.isArray(item.rect)||item.rect.length!==4)return '';
  const w=Math.max(0,Math.round(item.rect[2]-item.rect[0])),h=Math.max(0,Math.round(item.rect[3]-item.rect[1]));
  return ' · '+w+'×'+h+' @ ('+Math.round(item.rect[0])+','+Math.round(item.rect[1])+')';
}
function loadSingle(hash){
  const candidate=new Image(); singleLoading=hash;
  candidate.onload=()=>{if(singleLoading===hash){frame.src=candidate.src;frame.hidden=false;singleHash=hash;singleLoading='';}};
  candidate.onerror=()=>{if(singleLoading===hash)singleLoading='';};
  candidate.src=base+'/frame?sha256='+encodeURIComponent(hash);
}
function loadWindowImage(img,hwnd,hash,onerror,isCurrent,onload){
  const candidate=new Image();
  candidate.onload=()=>{if(isCurrent&&!isCurrent())return;img.src=candidate.src;img.hidden=false;if(onload)onload();};
  candidate.onerror=onerror||(()=>{});
  candidate.src=base+'/window?hwnd='+encodeURIComponent(hwnd)+'&sha256='+encodeURIComponent(hash);
}
function singleMessage(m){
  if(m.status==='stopped'&&m.session_error)return 'Session stopped after an error; inspect its local logs.';
  if(m.capture_error)return 'Window capture failed; inspect its local logs.';
  if(m.session_error)return 'Session reported an error; inspect its local logs.';
  if(m.worker_stale)return 'Session worker is stale; inspect its local logs.';
  if(m.stale&&m.available)return 'Capture is stale; waiting for a fresh frame.';
  if(m.status==='stopped')return 'Session stopped.';
  if(m.status==='failed'||m.status==='cleanup_failed')return 'Session reported an error.';
  return m.message||'Waiting for a capture.';
}
function workspaceMessage(m,w){
  if(m.status==='stopped'&&m.session_error)return 'Session stopped after an error; inspect its local logs.';
  if(m.capture_error)return 'Window capture failed; inspect its local logs.';
  if(m.session_error)return 'Session reported an error; inspect its local logs.';
  if(m.worker_stale)return 'Session worker is stale; inspect its local logs.';
  if(m.status==='stopped')return 'Session stopped.';
  if(m.status==='failed'||m.status==='cleanup_failed')return 'Session reported an error.';
  if(m.process_alive===false)return 'Session process is no longer running.';
  const age=Math.max(0,Date.now()/1000-w.captured_at);
  if(age>3)return 'Workspace capture is stale; waiting for a fresh layout.';
  if(!w.complete)return 'Workspace capture is incomplete; some window images may be unavailable.';
  return 'Workspace refreshed at '+new Date(w.captured_at*1000).toLocaleTimeString()+'.';
}
function showDetail(item){
  const key=windowKey(item), changed=selectedKey!==key;
  if(!changed&&detailHash===item.sha256&&item.status==='captured'){detailStatus.textContent='Captured window image · '+captureMethodLabel(item.capture_method)+'.';return;}
  const generation=++detailGeneration;
  selectedKey=key;detailHash='';detail.hidden=false;detailTitle.textContent=item.title||'Untitled window';
  if(changed){detailImage.removeAttribute('src');detailImage.hidden=true;}
  detailStatus.textContent=item.status==='captured'?'Loading captured window…':(item.status==='minimized'?'Minimized window; last image retained.':'Window image unavailable; last image retained.');
  if(item.status==='captured'&&item.sha256){
    loadWindowImage(detailImage,item.hwnd,item.sha256,
      ()=>{if(generation===detailGeneration)detailStatus.textContent='Window image unavailable.';},
      ()=>generation===detailGeneration,
      ()=>{if(generation===detailGeneration){detailHash=item.sha256;detailStatus.textContent='Captured window image · '+captureMethodLabel(item.capture_method)+'.';}});
  }
}
function cardStatus(card,item,stale){
  stale=stale||(typeof item.captured_at==='number'&&Date.now()/1000-item.captured_at>3);
  const unavailable=item.status!=='captured'||card.error;
  card.node.dataset.state=unavailable?'unavailable':stale?'stale':'current';
  const zTxt=typeof item.z_index==='number'?'Z#'+(item.z_index+1)+' · ':'';
  card.status.textContent=zTxt+'PID '+item.pid+' · HWND '+item.hwnd+formatGeom(item)+' · '+(item.status==='minimized'?'Minimized; last image retained':unavailable?'Image unavailable; retrying':stale?'Stale capture; last image retained':card.hash===item.sha256?'Current capture':'Loading capture…')+(item.status==='captured'?' · '+captureMethodLabel(item.capture_method):'');
  card.empty.hidden=!!card.hash;
  card.empty.textContent=item.status==='minimized'?'Window is minimized.':unavailable?'No window image available.':'Waiting for the first capture…';
}
function renderCard(item,stale){
  const key=windowKey(item);let card=cards.get(key);
  if(!card){
    const node=document.createElement('li'),header=document.createElement('header'),label=document.createElement('div'),title=document.createElement('h3'),status=document.createElement('p'),button=document.createElement('button'),image=document.createElement('img'),empty=document.createElement('p');
    node.className='window-card';status.className='window-status';image.className='window-image';image.hidden=true;empty.className='window-empty';button.type='button';button.textContent='Inspect';
    label.appendChild(title);label.appendChild(status);header.appendChild(label);header.appendChild(button);node.appendChild(header);node.appendChild(image);node.appendChild(empty);windowList.appendChild(node);
    card={node,title,status,image,empty,item,hash:'',pending:'',error:false,generation:0};cards.set(key,card);
    button.addEventListener('click',()=>showDetail(card.item));
  }
  card.item=item;card.title.textContent=item.title||'Untitled window';card.image.alt='Captured window: '+(item.title||'Untitled window');
  if(item.status!=='captured'){card.generation++;card.pending='';}
  if(item.status==='captured'&&item.sha256&&card.hash!==item.sha256&&card.pending!==item.sha256){
    const generation=++card.generation;card.pending=item.sha256;card.error=false;
    const current=()=>cards.get(key)===card&&generation===card.generation;
    loadWindowImage(card.image,item.hwnd,item.sha256,
      ()=>{if(current()){card.pending='';card.error=true;cardStatus(card,card.item,Date.now()/1000-currentLayout.captured_at>3);}},current,
      ()=>{card.hash=item.sha256;card.pending='';card.error=false;cardStatus(card,card.item,Date.now()/1000-currentLayout.captured_at>3);});
  }
  cardStatus(card,item,stale);
}
function renderZOrderStrip(w,stale){
  while(zOrderStrip.children.length)zOrderStrip.children[0].remove();
  for(let i=0;i<w.windows.length;i++){
    const item=w.windows[i],chip=document.createElement('button');
    chip.type='button';chip.className='z-chip';
    chip.dataset.state=item.status!=='captured'?'unavailable':stale?'stale':'current';
    const z=typeof item.z_index==='number'?item.z_index+1:i+1;
    chip.textContent='Z#'+z+' '+(item.title||'Untitled').slice(0,38)+' (PID '+item.pid+')';
    chip.addEventListener('click',()=>showDetail(item));
    zOrderStrip.appendChild(chip);
  }
}
function renderWorkspace(w){
  currentLayout=w;
  if(typeof w.capture_sequence==='number'&&typeof w.capture_monotonic==='number'){
    if(lastCaptureSample&&w.capture_sequence>lastCaptureSample.seq&&w.capture_monotonic>lastCaptureSample.mono){
      liveCaptureFps=(w.capture_sequence-lastCaptureSample.seq)/(w.capture_monotonic-lastCaptureSample.mono);
    }
    lastCaptureSample={seq:w.capture_sequence,mono:w.capture_monotonic};
  }
  updateFpsTelemetry();
  const age=Math.max(0,Date.now()/1000-w.captured_at);
  const stale=age>3,active=new Set(w.windows.map(windowKey));
  workspaceInfo.textContent=w.windows.length+' window(s) · '+(stale?'stale capture':w.complete?'capture current':'some images unavailable')+
    (w.omitted_count?' · '+w.omitted_count+' additional window(s) not shown':'');
  tWindows.textContent=w.windows.length+(w.omitted_count?' (+'+w.omitted_count+' omitted)':'');
  if(Array.isArray(w.bounds)&&w.bounds.length===4){
    tBounds.textContent=Math.round(w.bounds[2]-w.bounds[0])+'×'+Math.round(w.bounds[3]-w.bounds[1]);
  }
  tAge.textContent=age.toFixed(1)+'s ago';
  renderZOrderStrip(w,stale);
  for(const [key,card] of cards){if(!active.has(key)){card.generation++;card.node.remove();cards.delete(key);}}
  for(const item of w.windows)renderCard(item,stale);
  document.getElementById('emptyWindows').hidden=w.windows.length!==0;
  if(selectedKey!==null){
    const selected=w.windows.find(item=>windowKey(item)===selectedKey);
    if(selected)showDetail(selected);else{selectedKey=null;detailGeneration++;detailHash='';detail.hidden=true;detailImage.removeAttribute('src');}
  }
}
function markCardsStale(){
  for(const card of cards.values())cardStatus(card,card.item,true);
}
async function refresh(){
  if(polling)return; polling=true;
  try{
    const mr=await fetch(base+'/meta',{cache:'no-store',credentials:'same-origin'});
    if(!mr.ok)throw new Error('Local viewer unavailable.');
    const m=await mr.json();
    details.textContent=[m.status,m.pid?('PID '+m.pid):'',m.available?captureMethodLabel(m.capture_method):''].filter(Boolean).join(' · ');
    tStatus.textContent=[m.status,m.pid?('PID '+m.pid):''].filter(Boolean).join(' · ');
    const wr=await fetch(base+'/workspace',{cache:'no-store',credentials:'same-origin'});
    if(wr.status===404){workspace.hidden=true;single.hidden=false;state.textContent=singleMessage(m);
      currentLayout=null;liveCaptureFps=null;lastCaptureSample=null;updateFpsTelemetry(m.capture_method);
      document.getElementById('viewNote').textContent='Periodic window captures. This page cannot control the session.';
      if(m.available&&m.sha256&&m.sha256!==singleHash&&m.sha256!==singleLoading)loadSingle(m.sha256);
      return;
    }
    single.hidden=true;workspace.hidden=false;
    updateViewNote();
    if(!wr.ok){markCardsStale();state.textContent='Workspace manifest is invalid or unavailable; the last layout is preserved.';return;}
    const w=await wr.json();renderWorkspace(w);if(m.session_error||m.capture_error||m.worker_stale||m.process_alive===false||['stopped','failed','cleanup_failed'].includes(m.status))markCardsStale();state.textContent=workspaceMessage(m,w);
  }catch(_){markCardsStale();state.textContent='Local capture status is unavailable; last images retained.';}
  finally{polling=false;}
}
refresh();setInterval(refresh,1000);
</script></body></html>""".encode("utf-8")


def _inside(path, parent):
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _read_json_file(path, root, limit=65536):
    for attempt in range(4):
        try:
            resolved = path.resolve(strict=True)
            if not _inside(resolved, root) or not resolved.is_file() or resolved.stat().st_size > limit:
                raise ValueError("Invalid session metadata.")
            return json.loads(resolved.read_text(encoding="utf-8"))
        except PermissionError:
            if attempt == 3:
                raise
            time.sleep(0.002)


def _session_data(root):
    state = _read_json_file(root / "browser-session.json", root)
    if not isinstance(state, dict) or state.get("schema") != "rea-browser-v1":
        raise ValueError("Invalid session metadata.")
    if not isinstance(state.get("desktop"), str) or not state["desktop"]:
        raise ValueError("Invalid session metadata.")
    status = state.get("status")
    if not isinstance(status, str):
        raise ValueError("Invalid session metadata.")
    status = status if status in STATUSES else "unknown"
    pid = state.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        pid = None
    session_error = bool(state.get("error"))
    capture_error = bool(state.get("capture_error"))
    worker_fields = "worker_pid" in state or "heartbeat" in state
    worker_pid = state.get("worker_pid")
    if isinstance(worker_pid, bool) or not isinstance(worker_pid, int) or worker_pid <= 0:
        worker_pid = None
    worker_alive = _process_alive(worker_pid) if worker_pid is not None else (False if worker_fields else None)
    heartbeat = state.get("heartbeat")
    heartbeat_age = None
    if isinstance(heartbeat, (int, float)) and not isinstance(heartbeat, bool) and math.isfinite(heartbeat):
        heartbeat_age = max(0.0, time.time() - heartbeat)
    worker_stale = bool(worker_fields and status == "running" and
                        (worker_alive is False or (heartbeat_age is not None and heartbeat_age > 25.0)))
    return status, pid, session_error, capture_error, heartbeat_age, worker_alive, worker_stale


def _process_alive(pid):
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
        open_process.restype = ctypes.c_void_p
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
        get_exit_code.restype = ctypes.c_int
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (ctypes.c_void_p,)
        close_handle.restype = ctypes.c_int
        handle = open_process(0x1000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION; no terminate rights
        if not handle:
            return ctypes.get_last_error() not in (87, 1168)  # unknown/access denied errs on the side of alive
        try:
            code = ctypes.c_ulong()
            if not get_exit_code(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            close_handle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _capture(root):
    record = _read_json_file(root / "captures" / "latest.json", root)
    image = _capture_record(root, record)
    return {"sha256": record["sha256"], "captured_at": float(record["captured_at"]),
            "capture_method": record["method"], "image": image}


def _capture_path(root, record, strict=True):
    captures = (root / "captures").resolve(strict=True)
    if not _inside(captures, root) or not captures.is_dir():
        raise ValueError("Capture unavailable.")
    raw_path = record.get("path")
    if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
        raise ValueError("Capture unavailable.")
    path = Path(raw_path).resolve(strict=strict)
    if not _inside(path, captures) or path.suffix.lower() != ".png":
        raise ValueError("Capture unavailable.")
    return path


def _capture_record(root, record):
    if not isinstance(record, dict) or record.get("method") not in CAPTURE_METHODS:
        raise ValueError("Capture unavailable.")
    expected = record.get("sha256")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("Capture unavailable.")
    captured_at = record.get("captured_at")
    if isinstance(captured_at, bool) or not isinstance(captured_at, (int, float)) or not math.isfinite(captured_at):
        raise ValueError("Capture unavailable.")
    path = _capture_path(root, record)
    if not path.is_file():
        raise ValueError("Capture unavailable.")
    if path.stat().st_size > MAX_CAPTURE_BYTES:
        raise ValueError("Capture unavailable.")
    image = path.read_bytes()
    if not image.startswith(PNG_SIGNATURE) or hashlib.sha256(image).hexdigest() != expected:
        raise ValueError("Capture unavailable.")
    return image


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _rect(value):
    if not isinstance(value, list) or len(value) != 4 or not all(_number(part) for part in value):
        raise ValueError("Invalid workspace geometry.")
    left, top, right, bottom = value
    if any(abs(part) > MAX_COORDINATE for part in value):
        raise ValueError("Invalid workspace geometry.")
    if right <= left or bottom <= top or right - left > MAX_DIMENSION or bottom - top > MAX_DIMENSION:
        raise ValueError("Invalid workspace geometry.")
    return [float(part) for part in value]


def _workspace(root):
    captures = (root / "captures").resolve(strict=True)
    if not _inside(captures, root) or not captures.is_dir():
        raise ValueError("Invalid workspace paths.")
    raw = _read_json_file(root / "captures" / "desktop.json", root)
    if not isinstance(raw, dict) or raw.get("schema") != "desktop-layout-v1":
        raise ValueError("Invalid workspace manifest.")
    captured_at = raw.get("captured_at")
    if not _number(captured_at):
        raise ValueError("Invalid workspace manifest.")
    bounds = _rect(raw.get("bounds"))
    if bounds[2] - bounds[0] > MAX_DIMENSION or bounds[3] - bounds[1] > MAX_DIMENSION:
        raise ValueError("Invalid workspace geometry.")
    if not isinstance(raw.get("complete"), bool):
        raise ValueError("Invalid workspace manifest.")
    windows = raw.get("windows")
    if not isinstance(windows, list) or len(windows) > MAX_WINDOWS:
        raise ValueError("Invalid workspace manifest.")
    omitted_count = raw.get("omitted_count", 0)
    if isinstance(omitted_count, bool) or not isinstance(omitted_count, int) or omitted_count < 0:
        raise ValueError("Invalid workspace manifest.")
    sanitized = []
    seen_hwnds, seen_z = set(), set()
    for item in windows:
        if not isinstance(item, dict):
            raise ValueError("Invalid workspace window.")
        hwnd, pid, z_index = item.get("hwnd"), item.get("pid"), item.get("z_index")
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in (hwnd, pid)):
            raise ValueError("Invalid workspace window.")
        if isinstance(z_index, bool) or not isinstance(z_index, int) or not 0 <= z_index < MAX_WINDOWS:
            raise ValueError("Invalid workspace window.")
        if hwnd in seen_hwnds or z_index in seen_z:
            raise ValueError("Invalid workspace ordering.")
        seen_hwnds.add(hwnd)
        seen_z.add(z_index)
        title, status = item.get("title"), item.get("status")
        if not isinstance(title, str) or status not in {"captured", "minimized", "unavailable"}:
            raise ValueError("Invalid workspace window.")
        rect = _rect(item.get("rect"))
        if rect[0] < bounds[0] or rect[1] < bounds[1] or rect[2] > bounds[2] or rect[3] > bounds[3]:
            raise ValueError("Invalid workspace geometry.")
        safe_title = "".join(char for char in title if char.isprintable())[:500]
        entry = {"hwnd": hwnd, "pid": pid, "title": safe_title, "rect": rect,
                 "z_index": z_index, "status": status}
        if status == "captured":
            record = item.get("capture")
            if not isinstance(record, dict) or record.get("method") not in CAPTURE_METHODS:
                raise ValueError("Invalid workspace capture.")
            expected = record.get("sha256")
            captured_time = record.get("captured_at")
            if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected) or not _number(captured_time):
                raise ValueError("Invalid workspace capture.")
            capture_path = _capture_path(root, record, strict=False)
            if not capture_path.exists():
                entry["status"] = "unavailable"
                entry["message"] = "Image file is missing."
            else:
                entry["capture"] = {"path": str(capture_path), "sha256": expected,
                                    "captured_at": float(captured_time), "method": record["method"]}
        elif "capture" in item:
            raise ValueError("Unexpected workspace capture.")
        sanitized.append(entry)
    sanitized.sort(key=lambda item: item["z_index"])
    result = {"captured_at": float(captured_at), "bounds": bounds, "complete": raw["complete"],
            "omitted_count": omitted_count,
            "windows": sanitized}
    sequence, monotonic = raw.get("capture_sequence"), raw.get("capture_monotonic")
    if sequence is not None or monotonic is not None:
        if (isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1
                or not _number(monotonic) or monotonic <= 0):
            raise ValueError("Invalid capture timing.")
        result.update(capture_sequence=sequence, capture_monotonic=float(monotonic))
    return result


def _public_workspace(layout):
    extra = {k: layout[k] for k in ("capture_sequence", "capture_monotonic") if k in layout}
    return {"schema": "desktop-layout-v1", "captured_at": layout["captured_at"],
            "bounds": layout["bounds"], "complete": layout["complete"],
            "omitted_count": layout["omitted_count"], **extra,
            "windows": [{k: v for k, v in item.items() if k != "capture"} |
                        ({"sha256": item["capture"]["sha256"],
                          "captured_at": item["capture"]["captured_at"],
                          "capture_method": item["capture"]["method"]} if item.get("capture") else {})
                        for item in layout["windows"]]}


def _render_workspace_jpeg(root, layout, window_cache=None):
    from PIL import Image, ImageDraw

    left, top, right, bottom = layout["bounds"]
    width, height = int(right - left), int(bottom - top)
    scale = min(1.0, MAX_RENDER_DIMENSION / max(width, height),
                math.sqrt(MAX_RENDER_PIXELS / (width * height)))
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    stale = max(0.0, time.time() - layout["captured_at"]) > 3.0
    visible = [item for item in reversed(layout["windows"]) if item["status"] != "minimized"]

    def _load_window_frame(item, target_size):
        cache_key = (item["capture"]["sha256"], target_size)
        if window_cache is not None and cache_key in window_cache:
            return window_cache[cache_key]
        try:
            raw_img = Image.open(io.BytesIO(_capture_record(root, item["capture"])))
            if raw_img.mode != "RGB":
                raw_img = raw_img.convert("RGB")
            else:
                raw_img.load()
            frame = raw_img if raw_img.size == target_size else raw_img.resize(target_size, Image.Resampling.BICUBIC)
            if window_cache is not None:
                if len(window_cache) >= 64:
                    window_cache.pop(next(iter(window_cache)))
                window_cache[cache_key] = frame
            return frame
        except (OSError, ValueError, KeyError):
            return None

    if len(visible) == 1 and not stale and visible[0]["status"] == "captured":
        only = visible[0]
        x1, y1, x2, y2 = only["rect"]
        box = (max(0, round((x1 - left) * scale)), max(0, round((y1 - top) * scale)),
               min(size[0], round((x2 - left) * scale)), min(size[1], round((y2 - top) * scale)))
        if box == (0, 0, size[0], size[1]):
            direct_frame = _load_window_frame(only, size)
            if direct_frame is not None:
                output = io.BytesIO()
                direct_frame.save(output, format="JPEG", quality=92, subsampling=0)
                return output.getvalue(), stale

    scene = Image.new("RGB", size, (20, 30, 43))
    draw = ImageDraw.Draw(scene)
    # z_index 0 is foremost; paint back-to-front, including retained stale captures.
    for item in visible:
        x1, y1, x2, y2 = item["rect"]
        box = (round((x1 - left) * scale), round((y1 - top) * scale),
               round((x2 - left) * scale), round((y2 - top) * scale))
        box = (max(0, box[0]), max(0, box[1]), min(size[0], box[2]), min(size[1], box[3]))
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        frame = _load_window_frame(item, (box[2] - box[0], box[3] - box[1])) if item["status"] == "captured" else None
        if frame is not None:
            scene.paste(frame, box[:2])
        else:
            draw.rectangle(box, fill=(32, 43, 59), outline=(100, 119, 145))
            draw.text((box[0] + 6, box[1] + 6), "Window image unavailable", fill=(220, 225, 235))
    if stale:
        draw.rectangle((0, 0, min(size[0], 310), 24), fill=(30, 26, 20))
        draw.text((8, 6), "Stale capture - last image retained", fill=(225, 190, 128))
    output = io.BytesIO()
    scene.save(output, format="JPEG", quality=92, subsampling=0)
    return output.getvalue(), stale


def make_handler(root, token):
    frame_cache = {}
    window_cache = {}
    frame_lock = threading.Lock()

    def layout_key(layout):
        stale = max(0.0, time.time() - layout["captured_at"]) > 3.0
        return (tuple(layout["bounds"]), stale,
                tuple((item["hwnd"], tuple(item["rect"]), item["status"],
                       item.get("capture", {}).get("sha256")) for item in layout["windows"]))

    def stream_frame(layout, key=None):
        if key is None:
            key = layout_key(layout)
        with frame_lock:
            if frame_cache.get("key") != key:
                frame_cache["value"] = _render_workspace_jpeg(root, layout, window_cache)
                frame_cache["key"] = key
            return frame_cache["value"]

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "LocalCaptureViewer"
        sys_version = ""

        def log_message(self, _format, *args):
            pass

        def _send(self, code, body, content_type):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; img-src 'self'; connect-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, data):
            self._send(code, json.dumps(data, separators=(",", ":")).encode(), "application/json; charset=utf-8")

        def do_GET(self):
            try:
                peer = ipaddress.ip_address(self.client_address[0])
            except ValueError:
                self._json(403, {"error": "forbidden"})
                return
            host = self.headers.get_all("Host", [])
            expected_host = f"127.0.0.1:{self.server.server_port}"
            if not peer.is_loopback or host != [expected_host]:
                self._json(403, {"error": "forbidden"})
                return

            parsed = urlsplit(self.path)
            base = "/" + token
            if parsed.path == base:
                self._send(200, HTML, "text/html; charset=utf-8")
            elif parsed.path == base + "/meta" and not parsed.query:
                self._metadata()
            elif parsed.path == base + "/workspace" and not parsed.query:
                self._workspace()
            elif parsed.path == base + "/stream.mjpg" and not parsed.query:
                self._stream()
            elif parsed.path == base + "/frame":
                self._frame(parse_qs(parsed.query, keep_blank_values=True))
            elif parsed.path == base + "/window":
                self._window(parse_qs(parsed.query, keep_blank_values=True))
            else:
                self._json(404, {"error": "not found"})

        def _metadata(self):
            try:
                status, pid, session_error, capture_error, heartbeat_age, worker_alive, worker_stale = _session_data(root)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                status, pid, session_error, capture_error = "error", None, False, False
                heartbeat_age, worker_alive, worker_stale = None, None, False
            health = {"session_error": session_error, "capture_error": capture_error}
            if heartbeat_age is not None:
                health["worker_heartbeat_age_seconds"] = round(heartbeat_age, 2)
            if worker_alive is not None:
                health["worker_alive"] = worker_alive
                health["worker_stale"] = worker_stale
            process_alive = _process_alive(pid) if pid is not None else None
            if process_alive is not None:
                health["process_alive"] = process_alive
            try:
                capture = _capture(root)
                age = max(0.0, time.time() - capture["captured_at"])
                process_gone = status == "running" and process_alive is False
                stale = (age > 3.0 or process_gone or worker_stale or session_error or capture_error or
                         status != "running")
                if status == "stopped" and session_error:
                    message = "Session stopped after an error; inspect its local logs."
                elif capture_error:
                    message = "Window capture failed; inspect its local logs."
                elif session_error:
                    message = "Session reported an error; inspect its local logs."
                elif worker_stale:
                    message = "Session worker is stale; inspect its local logs."
                elif process_gone:
                    message = "Session process is no longer running."
                else:
                    message = "Capture is stale." if stale else "Capture is current."
                self._json(200, {"title": "Hidden desktop observer", "status": status, "pid": pid,
                                 "captured_at": capture["captured_at"], "age_seconds": round(age, 2),
                                 "sha256": capture["sha256"], "capture_method": capture["capture_method"],
                                 "available": True, "stale": stale,
                                 "message": message, **health})
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                self._json(200, {"title": "Hidden desktop observer", "status": status, "pid": pid,
                                 "available": False, "stale": True, "message": "Waiting for a valid capture.",
                                 **health})

        def _workspace(self):
            path = root / "captures" / "desktop.json"
            if not path.exists() and not path.is_symlink():
                self._json(404, {"error": "workspace manifest absent"})
                return
            try:
                layout = _workspace(root)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                self._json(422, {"error": "workspace manifest unavailable"})
                return
            self._json(200, _public_workspace(layout))

        def _stream(self):
            try:
                _workspace(root)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                self._json(503, {"error": "workspace reconstruction unavailable"})
                return
            boundary = "codexframe"
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=" + boundary)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Stream-Description", "Live reconstructed window images; not native desktop video")
            self.end_headers()
            sequence = 0
            interval = 1.0 / STREAM_FPS
            idle_keepalive = 0.25
            poll_step = 0.004
            last_key = None
            last_sent = 0.0
            winmm = getattr(ctypes, "windll", None)
            winmm = getattr(winmm, "winmm", None) if winmm is not None else None
            if winmm is not None:
                try:
                    winmm.timeBeginPeriod(1)
                except Exception:
                    winmm = None
            next_deadline = time.monotonic()
            try:
                while not getattr(self.server, "_shutdown_request", False):
                    layout = _workspace(root)
                    key = layout_key(layout)
                    now = time.monotonic()
                    if sequence > 0 and key == last_key and (now - last_sent) < idle_keepalive:
                        time.sleep(poll_step)
                        continue
                    image, stale = stream_frame(layout, key)
                    sequence += 1
                    part = (f"--{boundary}\r\nContent-Type: image/jpeg\r\n"
                            f"Content-Length: {len(image)}\r\nX-Sequence: {sequence}\r\n"
                            f"X-Captured-At: {layout['captured_at']:.6f}\r\n"
                            f"X-Frame-State: {'stale' if stale else 'current'}\r\n\r\n").encode("ascii")
                    self.wfile.write(part + image + b"\r\n")
                    self.wfile.flush()
                    last_key = key
                    last_sent = time.monotonic()
                    next_deadline = max(now + interval, next_deadline + interval)
                    if next_deadline > last_sent:
                        time.sleep(next_deadline - last_sent)
            except (BrokenPipeError, ConnectionResetError, OSError, ValueError, TypeError, json.JSONDecodeError):
                self.close_connection = True
                return
            finally:
                if winmm is not None:
                    try:
                        winmm.timeEndPeriod(1)
                    except Exception:
                        pass

        def _frame(self, query):
            requested = query.get("sha256", [])
            if len(requested) != 1 or not re.fullmatch(r"[0-9a-f]{64}", requested[0]):
                self._json(400, {"error": "invalid frame request"})
                return
            try:
                capture = _capture(root)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                self._json(404, {"error": "capture unavailable"})
                return
            if capture["sha256"] != requested[0]:
                self._json(409, {"error": "capture changed"})
                return
            self._send(200, capture["image"], "image/png")

        def _window(self, query):
            handles, hashes = query.get("hwnd", []), query.get("sha256", [])
            if set(query) != {"hwnd", "sha256"} or len(handles) != 1 or len(hashes) != 1:
                self._json(400, {"error": "invalid window request"})
                return
            if not re.fullmatch(r"[1-9][0-9]{0,9}", handles[0]) or not re.fullmatch(r"[0-9a-f]{64}", hashes[0]):
                self._json(400, {"error": "invalid window request"})
                return
            try:
                layout = _workspace(root)
                hwnd = int(handles[0])
                item = next((entry for entry in layout["windows"]
                             if entry["hwnd"] == hwnd and entry["status"] == "captured" and
                             entry.get("capture", {}).get("sha256") == hashes[0]), None)
                if item is None:
                    self._json(404, {"error": "window capture unavailable"})
                    return
                image = _capture_record(root, item["capture"])
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                self._json(404, {"error": "window capture unavailable"})
                return
            self._send(200, image, "image/png")

        def do_POST(self):
            self._json(405, {"error": "method not allowed"})

        def do_PUT(self):
            self._json(405, {"error": "method not allowed"})

        def do_DELETE(self):
            self._json(405, {"error": "method not allowed"})

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, type=Path)
    args = parser.parse_args(argv)
    root = args.session.expanduser().resolve(strict=True)
    if not root.is_dir():
        parser.error("--session must be a directory")
    _session_data(root)
    token = secrets.token_urlsafe(32)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_handler(root, token))
    server.daemon_threads = True
    print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}/{token}"}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
