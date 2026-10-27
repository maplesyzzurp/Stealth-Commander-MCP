"""View approved hidden-desktop observer streams together on a local page."""

import argparse
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import harness as h
import observe
import cached_capture


HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>Desktops</title><style>
:root{color-scheme:dark;font:11.5px/1.35 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#06090f;color:#e4ecf7;--bg-card:#0c121c;--bg-elev:#101927;--bg-Well:#070b11;--border:#1d2a3d;--border-hi:#314866;--accent:#3b82f6;--live:#10b981;--warn:#f59e0b;--err:#ef4444;--muted:#8496ae;--mono:ui-monospace,SFMono-Regular,Consolas,monospace}
*{box-sizing:border-box}body{margin:0;padding:8px 12px 16px;background:radial-gradient(circle at 50% 0,#0e1828 0,#06090f 65%);min-height:100vh}button{font:inherit}
.page-header{position:sticky;top:0;z-index:15;display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin:0 0 8px;padding:5px 10px;background:rgba(12,18,28,.9);backdrop-filter:blur(12px);border:1px solid var(--border);border-radius:7px;box-shadow:0 4px 16px rgba(0,0,0,.35)}
h1{font-size:13px;letter-spacing:.02em;margin:0;font-weight:650;color:#f0f6fc}
.fps-badge{display:inline-flex;align-items:center;gap:5px;padding:2px 7px;border-radius:999px;font:600 10px/1.1 var(--mono);background:rgba(16,185,129,.12);color:#34d399;border:1px solid rgba(16,185,129,.3)}
.fps-badge:before{content:'';width:5px;height:5px;border-radius:50%;background:#10b981;box-shadow:0 0 6px #10b981}
#summary{font:600 10.5px/1.1 var(--mono);color:#9fb1c9;padding:2px 7px;background:var(--bg-elev);border:1px solid var(--border);border-radius:5px}
.toolbar-Cluster{display:flex;align-items:center;flex-wrap:wrap;gap:6px;margin-left:auto}
.views,.ctrl-group{display:inline-flex;align-items:center;gap:2px;padding:2px;border:1px solid var(--border);border-radius:6px;background:var(--bg-Well)}
.ctrl-label{font:600 9px/1 var(--mono);text-transform:uppercase;letter-spacing:.05em;color:var(--muted);padding:0 5px}
.views button,.ctrl-group button{font-size:10.5px;font-weight:500;padding:3px 7px;border-radius:4px;color:#93a6bf;transition:background .12s,color .12s}
.views button[aria-pressed="true"],.ctrl-group button[aria-pressed="true"]{background:#1d3251;color:#fff;box-shadow:inset 0 0 0 1px rgba(96,165,250,.35)}
.fleet-ribbon{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:6px;margin:0 0 8px}
.fleet-cell{background:var(--bg-card);border:1px solid var(--border);border-radius:6px;padding:4px 8px;display:flex;flex-direction:row;align-items:center;justify-content:space-between;gap:6px}
.fleet-label{font-size:9px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);font-weight:600;white-space:nowrap}
.fleet-val{font:600 11px/1.2 var(--mono);color:#e4ecf7;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,480px),1fr));align-items:start;gap:10px}
#grid[data-cols="1"]{grid-template-columns:1fr}
#grid[data-cols="2"]{grid-template-columns:repeat(auto-fit,minmax(min(100%,46%),1fr))}
#grid[data-cols="3"]{grid-template-columns:repeat(auto-fit,minmax(min(100%,31%),1fr))}
#grid[data-cols="4"]{grid-template-columns:repeat(auto-fit,minmax(min(100%,23%),1fr))}
.desktop{min-width:min(100%,280px);width:100%;max-width:100%;min-height:320px;display:flex;flex-direction:column;resize:both;background:var(--bg-card);border:1px solid var(--border);border-radius:8px;overflow:hidden;box-shadow:0 8px 24px rgba(0,0,0,.35);transition:border-color .15s}
.desktop:hover{border-color:var(--border-hi)}
.desktop.spotlight{grid-column:1/-1;border-color:#3b82f6;box-shadow:0 12px 32px rgba(15,23,42,.75),0 0 0 1px rgba(59,130,246,.3)}
.desktop.dragging{position:relative;z-index:8;resize:none;box-shadow:0 18px 40px #000a}
.fullscreen-button{font-size:13px;line-height:1;padding:2px 5px}.refresh-button,.spotlight-button{font-size:12px;line-height:1;padding:2px 5px;color:#8fa3be}
.spotlight-button[aria-pressed="true"]{background:#1d3251;color:#60a5fa}
.desktop-name-input{min-width:80px;flex:1;font:inherit;font-size:11.5px;background:#070b11;color:#e4ecf7;border:1px solid #60a5fa;border-radius:4px;padding:2px 5px;outline:none}
.desktop header{min-height:32px;max-height:60%;flex-shrink:0;overflow:auto;display:flex;align-items:center;flex-wrap:wrap;gap:5px;padding:4px 9px;border-bottom:1px solid var(--border);background:var(--bg-elev);cursor:grab;user-select:none}
.desktop header:not(:has(.process-details:not([hidden]))){touch-action:none}.desktop.dragging header{cursor:grabbing}.desktop-name-input,.process-details{user-select:text;cursor:auto}
.desktop-name{font-size:12px;font-weight:600;text-align:left;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin:0;padding:1px 3px;flex:1;min-width:90px;color:#f0f6fc}.desktop-name:hover{background:rgba(255,255,255,.04);color:#93c5fd}
.desktop-name:before{content:attr(data-sequence);font:600 9.5px/1 var(--mono);color:#60a5fa;background:rgba(59,130,246,.14);border:1px solid rgba(59,130,246,.3);padding:1px 4px;border-radius:3px;margin-right:6px}
.status{font:600 10px/1 var(--mono);color:#8494aa;white-space:nowrap;padding:2px 6px;border-radius:999px;background:rgba(132,148,170,.12);border:1px solid rgba(132,148,170,.25)}
.status:before{content:' ';display:inline-block;width:5px;height:5px;border-radius:50%;background:#8494aa;margin-right:4px;vertical-align:middle}
.desktop[data-state="live"] .status{color:#34d399;background:rgba(16,185,129,.12);border-color:rgba(16,185,129,.3)}.desktop[data-state="live"] .status:before{background:#10b981;box-shadow:0 0 5px #10b981}
.desktop[data-state="stale"] .status{color:#fbbf24;background:rgba(245,158,11,.12);border-color:rgba(245,158,11,.3)}.desktop[data-state="stale"] .status:before{background:#f59e0b}
button{border:0;background:transparent;color:#93a6bf;padding:3px 5px;border-radius:4px;cursor:pointer}button:hover{background:#1e2d42;color:#f0f6fc}button:focus-visible{outline:2px solid #60a5fa;outline-offset:2px}
.analytics{display:inline-flex;align-items:center;flex-wrap:wrap;gap:4px;order:2}
.status,.fullscreen-button,.spotlight-button,.refresh-button{order:3}
.metric{min-width:0;display:inline-flex;align-items:center;gap:3px;padding:1px 5px;border-radius:4px;background:rgba(7,11,17,.65);border:1px solid rgba(29,42,61,.8)}
.metric-icon{display:block;width:11px;height:11px;flex-shrink:0;color:#60a5fa}.metric-value{font:600 10px/1.1 var(--mono);color:#d5e2f2;font-variant-numeric:tabular-nums;white-space:nowrap}
.details-toggle{font:600 10px/1 var(--mono);padding:2px 6px;background:rgba(59,130,246,.12);color:#93c5fd;border:1px solid rgba(59,130,246,.28)}
.process-details{order:4;flex-basis:100%;min-width:0;max-height:240px;overflow:auto;border-top:1px solid var(--border);padding-top:6px;margin-top:3px}.process-summary{color:#94a8c4;font:10px/1.4 var(--mono);margin:0 0 6px}
.process-table{border-collapse:collapse;width:100%;font:10px/1.3 var(--mono);font-variant-numeric:tabular-nums}.process-table th{text-align:left;color:#8496ae;font-weight:600;position:sticky;top:0;background:var(--bg-elev);z-index:1}.process-table th,.process-table td{padding:4px 8px 4px 0;border-bottom:1px solid #192536;white-space:nowrap;vertical-align:top}.process-table th:first-child,.process-table td:first-child{min-width:150px;max-width:300px}.process-name{color:#e2ecf7;font-weight:600}.process-windows{display:block;white-space:pre-line;overflow-wrap:anywhere;color:#8496ae;font-size:9.5px;margin-top:2px;max-width:280px}
.surface{position:relative;aspect-ratio:16/10;flex:1;min-height:200px;background:var(--bg-Well);display:grid;place-items:center;overflow:hidden}
#grid[data-aspect="16:9"] .surface{aspect-ratio:16/9}
#grid[data-aspect="natural"] .surface{aspect-ratio:auto;min-height:300px}
#grid[data-aspect="tall"] .surface{aspect-ratio:4/3;min-height:360px}
.desktop.spotlight .surface{aspect-ratio:auto;min-height:74vh}
.surface img{display:block;width:100%;height:100%;object-fit:contain;min-height:0;min-width:0;image-rendering:-webkit-optimize-contrast}
#grid[data-fit="cover"] .surface img{object-fit:cover}
.empty{position:absolute;color:#6e8098;font:11px var(--mono);pointer-events:none}
.surface-hud{position:absolute;left:7px;right:7px;bottom:6px;display:flex;align-items:center;justify-content:space-between;gap:6px;pointer-events:none;opacity:.82;transition:opacity .15s}
.desktop:hover .surface-hud{opacity:1}
.hud-pill{font:500 9.5px/1.1 var(--mono);color:#c9d8eb;background:rgba(7,11,17,.8);backdrop-filter:blur(5px);border:1px solid rgba(49,72,102,.6);padding:2px 6px;border-radius:4px;max-width:70%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.windows-panel{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,280px),1fr));align-content:start;gap:10px;padding:12px;background:var(--bg-Well);min-height:320px}
.window-card{min-width:0;border:1px solid var(--border);border-radius:8px;overflow:hidden;background:var(--bg-card);cursor:pointer;transition:border-color .12s}
.window-card:hover{border-color:var(--border-hi)}
.window-card.zoomed{grid-column:1/-1;border-color:#3b82f6}
.window-bar{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:8px 10px;background:var(--bg-elev);border-bottom:1px solid var(--border)}
.window-title{margin:0;color:#dce7f5;font-size:11.5px;font-weight:600;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.window-state{font:600 10px/1 var(--mono);color:#8496ae;white-space:nowrap;padding:3px 6px;border-radius:4px;background:rgba(255,255,255,.04)}
.window-image{display:block;width:100%;height:260px;object-fit:contain;background:#05080d;image-rendering:-webkit-optimize-contrast}
.window-card.zoomed .window-image{height:540px}
.window-placeholder{padding:70px 12px;color:#6e7d93;font:11px var(--mono);text-align:center}.windows-empty{grid-column:1/-1;color:#6e7d93;font:12px var(--mono);padding:30px 0;text-align:center}
[hidden]{display:none!important}.expanded{position:fixed;inset:14px;width:auto!important;height:auto!important;min-width:0;min-height:0;max-width:none;z-index:25;box-shadow:0 0 0 30px rgba(6,9,15,.94);resize:none}.desktop:fullscreen{inset:0;border:0;border-radius:0;width:100vw!important;height:100vh!important}.expanded header{flex-shrink:0}.expanded .surface{height:auto;flex:1;min-height:0;aspect-ratio:auto}.windows-panel{flex:1;overflow:auto}.expanded .windows-panel{grid-template-columns:repeat(auto-fit,minmax(min(100%,440px),1fr))}.expanded .window-image{height:400px}
body:has(.expanded){overflow:hidden}@media(max-width:900px){body{padding:12px}#grid{grid-template-columns:1fr}.surface{height:60vh;min-height:260px}.expanded{inset:8px}.analytics{gap:10px}}
</style></head><body><header class="page-header"><h1>Desktops</h1><span id="fpsBadge" class="fps-badge">DWM GPU · LIVE FPS</span><span id="summary" role="status" aria-live="polite">Connecting</span><div class="toolbar-Cluster"><div class="ctrl-group" role="group" aria-label="Grid density"><span class="ctrl-label">Grid</span><button id="colsAuto" type="button" aria-pressed="true">Auto</button><button id="cols1" type="button" aria-pressed="false">1×Theater</button><button id="cols2" type="button" aria-pressed="false">2×Quad</button><button id="cols3" type="button" aria-pressed="false">3×Dense</button></div><div class="ctrl-group" role="group" aria-label="Aspect ratio"><span class="ctrl-label">Canvas</span><button id="aspect1610" type="button" aria-pressed="true">16:10</button><button id="aspect169" type="button" aria-pressed="false">16:9</button><button id="aspectNatural" type="button" aria-pressed="false">Natural</button><button id="aspectTall" type="button" aria-pressed="false">4:3 Tall</button></div><div class="views" role="group" aria-label="View"><button id="desktopsView" type="button" aria-pressed="true">Desktops</button><button id="windowsView" type="button" aria-pressed="false">Windows</button></div></div></header><section class="fleet-ribbon" aria-label="Fleet telemetry"><div class="fleet-cell"><span class="fleet-label">Fleet Status</span><span id="fleetHealth" class="fleet-val">Waiting…</span></div><div class="fleet-cell"><span class="fleet-label">Visible Windows</span><span id="fleetWindows" class="fleet-val">0 windows</span></div><div class="fleet-cell"><span class="fleet-label">Active Processes</span><span id="fleetProcs" class="fleet-val">0 procs</span></div><div class="fleet-cell"><span class="fleet-label">Aggregate CPU</span><span id="fleetCpu" class="fleet-val">—</span></div><div class="fleet-cell"><span class="fleet-label">Aggregate RAM</span><span id="fleetRam" class="fleet-val">—</span></div><div class="fleet-cell"><span class="fleet-label">Live Capture Speed</span><span id="fleetFps" class="fleet-val">Measuring…</span></div></section><main id="grid"></main>
<script>
const base=location.pathname,grid=document.getElementById('grid'),summary=document.getElementById('summary'),fpsBadge=document.getElementById('fpsBadge'),cards=new Map();let polling=false,view='desktops',initialBuild=null,reloading=false,nextSequence=1,activeDrag=null;
const fleetHealth=document.getElementById('fleetHealth'),fleetWindows=document.getElementById('fleetWindows'),fleetProcs=document.getElementById('fleetProcs'),fleetCpu=document.getElementById('fleetCpu'),fleetRam=document.getElementById('fleetRam'),fleetFps=document.getElementById('fleetFps');
function bindGroup(pairs,attr){
  for(const [id,val] of pairs){
    const btn=document.getElementById(id);
    if(!btn)continue;
    btn.addEventListener('click',()=>{
      if(val)grid.dataset[attr]=val;else delete grid.dataset[attr];
      for(const [otherId,otherVal] of pairs){const other=document.getElementById(otherId);if(other)other.setAttribute('aria-pressed',String(otherVal===val));}
    });
  }
}
bindGroup([['colsAuto',''],['cols1','1'],['cols2','2'],['cols3','3']],'cols');
bindGroup([['aspect1610',''],['aspect169','16:9'],['aspectNatural','natural'],['aspectTall','tall']],'aspect');
function loadNames(){const names=Object.create(null);try{const saved=JSON.parse(localStorage.getItem('stealth-desktop-names')||'{}');if(saved&&typeof saved==='object'&&!Array.isArray(saved))for(const [id,name] of Object.entries(saved))if(typeof name==='string'&&name.trim()&&name.length<=80)names[id]=name;}catch(_){}return names;}
const customNames=loadNames();
function labelCard(card){const name=customNames[card.id]||card.name;card.title.textContent=name;card.title.setAttribute('aria-label','Rename desktop '+card.sequence+': '+name);card.image.alt=name+' desktop';card.button.setAttribute('aria-label',(card.node.classList.contains('expanded')?'Exit full screen for ':'Full screen for ')+name);if(card.refreshButton)card.refreshButton.setAttribute('aria-label','Refresh view for '+name);if(card.spotlightButton)card.spotlightButton.setAttribute('aria-label','Spotlight view for '+name);if(card.detailsButton)card.detailsButton.setAttribute('aria-label','Process details for '+name);}
function addRename(card,header){
  const button=card.title;button.type='button';button.className='desktop-name';button.title='Rename desktop';card.renameButton=button;
  button.addEventListener('click',()=>{
    if(card.renaming)return;card.renaming=true;const input=element('input','desktop-name-input');input.type='text';input.maxLength=80;input.value=customNames[card.id]||card.name;input.setAttribute('aria-label','Desktop name');card.title.hidden=true;header.insertBefore(input,card.title);
    const finish=save=>{if(!card.renaming)return;card.renaming=false;if(save){const value=input.value.trim().slice(0,80);if(value)customNames[card.id]=value;else delete customNames[card.id];try{localStorage.setItem('stealth-desktop-names',JSON.stringify(customNames));}catch(_){}}input.remove();card.title.hidden=false;labelCard(card);};
    input.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key==='Escape'){event.preventDefault();finish(event.key==='Enter');}});input.addEventListener('blur',()=>finish(true));input.focus();input.select();
  });
}
function cancelWindowLoad(window){window.generation++;window.pending='';if(window.candidate){window.candidate.onload=null;window.candidate.onerror=null;window.candidate.removeAttribute('src');window.candidate=null;}}
function selectView(next){
  view=next;document.getElementById('desktopsView').setAttribute('aria-pressed',String(next==='desktops'));document.getElementById('windowsView').setAttribute('aria-pressed',String(next==='windows'));
  for(const card of cards.values()){
    card.surface.hidden=next!=='desktops';card.windowsPanel.hidden=next!=='windows';
    if(next==='windows'){card.streamActive=false;card.image.removeAttribute('src');renderWindows(card);}
    else{for(const window of card.windowCards.values())cancelWindowLoad(window);if(card.approvedUrl&&card.sourceAvailable)connect(card,card.approvedUrl);}
  }
}
document.getElementById('desktopsView').addEventListener('click',()=>{if(view!=='desktops')selectView('desktops');});
document.getElementById('windowsView').addEventListener('click',()=>{if(view!=='windows')selectView('windows');});
function expand(card){
  const opening=!card.node.classList.contains('expanded');
  for(const other of cards.values()){other.node.classList.remove('expanded');other.button.setAttribute('aria-expanded','false');other.button.title='Full screen';other.button.setAttribute('aria-label','Full screen for '+other.name);}
  if(opening){card.node.classList.add('expanded');card.button.setAttribute('aria-expanded','true');card.button.title='Exit full screen';card.button.setAttribute('aria-label','Exit full screen for '+card.name);if(card.node.requestFullscreen)try{const request=card.node.requestFullscreen();if(request&&request.catch)request.catch(()=>{});}catch(_){}}
  else if(document.fullscreenElement===card.node&&document.exitFullscreen)try{const exit=document.exitFullscreen();if(exit&&exit.catch)exit.catch(()=>{});}catch(_){}
}
document.addEventListener('fullscreenchange',()=>{for(const card of cards.values()){const active=document.fullscreenElement===card.node;if(active)card.node.classList.add('expanded');else card.node.classList.remove('expanded');card.button.setAttribute('aria-expanded',String(active));card.button.title=active?'Exit full screen':'Full screen';}});
document.addEventListener('keydown',event=>{if(event.key==='Escape')for(const card of cards.values())if(card.node.classList.contains('expanded'))expand(card);});
function loadOrder(){try{const value=JSON.parse(localStorage.getItem('stealth-desktop-order')||'[]');if(Array.isArray(value))return value.filter(id=>typeof id==='string');}catch(_){}return [];}
let savedOrder=loadOrder();
function reducedMotion(){return typeof matchMedia==='function'&&matchMedia('(prefers-reduced-motion: reduce)').matches;}
function animateMove(node,from){if(reducedMotion()||!node.animate)return;const to=node.getBoundingClientRect(),x=from.left-to.left,y=from.top-to.top;if(x||y)node.animate([{transform:'translate('+x+'px,'+y+'px)'},{transform:'translate(0,0)'}],{duration:180,easing:'cubic-bezier(.2,.8,.2,1)'});}
function reorder(change){const before=new Map([...grid.children].map(node=>[node,node.getBoundingClientRect()]));change();for(const [node,rect] of before)if(!activeDrag||node!==activeDrag.card.node)animateMove(node,rect);}
function rememberOrder(){savedOrder=[...grid.children].map(node=>node.dataset.id);try{localStorage.setItem('stealth-desktop-order',JSON.stringify(savedOrder));}catch(_){}}
function restoreOrder(){if(!savedOrder.length||activeDrag)return;const current=[...grid.children],ordered=savedOrder.map(id=>cards.get(id)).filter(Boolean).map(card=>card.node);for(const node of current)if(!ordered.includes(node))ordered.push(node);if(ordered.some((node,index)=>node!==current[index]))for(const node of ordered)grid.appendChild(node);}
function addReorder(card,header){
  const position=event=>{const drag=activeDrag,rect=card.node.getBoundingClientRect();drag.x+=drag.origin.left+event.clientX-drag.startX-rect.left;drag.y+=drag.origin.top+event.clientY-drag.startY-rect.top;card.node.style.transform='translate('+drag.x+'px,'+drag.y+'px)';};
  const move=event=>{
    const drag=activeDrag;if(!drag||drag.card!==card||event.pointerId!==drag.pointer||drag.lastEvent===event)return;drag.lastEvent=event;
    if(!drag.started){if(Math.hypot(event.clientX-drag.startX,event.clientY-drag.startY)<5)return;drag.started=true;card.node.classList.add('dragging');}
    if(event.preventDefault)event.preventDefault();position(event);
    for(const other of cards.values()){
      if(other===card)continue;const bounds=other.node.getBoundingClientRect();if(event.clientX<bounds.left||event.clientX>bounds.right||event.clientY<bounds.top||event.clientY>bounds.bottom)continue;
      const nodes=[...grid.children],forward=nodes.indexOf(card.node)<nodes.indexOf(other.node),sameRow=Math.abs(drag.origin.top-bounds.top)<bounds.height/2,past=sameRow?event.clientX>bounds.left+bounds.width/2:event.clientY>bounds.top+bounds.height/2;
      if(forward===past){reorder(()=>grid.insertBefore(card.node,forward?other.node.nextSibling:other.node));if(card.node.setPointerCapture)try{card.node.setPointerCapture(drag.pointer);}catch(_){}position(event);}break;
    }
  };
  const finish=event=>{const drag=activeDrag;if(!drag||drag.card!==card||(event&&event.pointerId!== undefined&&event.pointerId!==drag.pointer))return;const rect=card.node.getBoundingClientRect();card.node.style.transform='';card.node.classList.remove('dragging');activeDrag=null;if(drag.started){animateMove(card.node,rect);rememberOrder();}if(card.node.hasPointerCapture&&card.node.hasPointerCapture(drag.pointer))try{card.node.releasePointerCapture(drag.pointer);}catch(_){}};
  card.node.addEventListener('pointerdown',event=>{
    if(activeDrag||event.button!==0||event.isPrimary===false||card.node.classList.contains('expanded'))return;
    if(event.target.closest&&event.target.closest('button,input,select,textarea,a,[contenteditable],.process-details,.windows-panel'))return;
    if(event.target!==card.node&&event.target!==card.surface&&!header.contains(event.target))return;
    const bounds=card.node.getBoundingClientRect();if(bounds.right-event.clientX<20&&bounds.bottom-event.clientY<20)return;
    activeDrag={card,pointer:event.pointerId,startX:event.clientX,startY:event.clientY,origin:bounds,x:0,y:0,started:false,lastEvent:null};
    if(card.node.setPointerCapture)try{card.node.setPointerCapture(event.pointerId);}catch(_){}
  });
  card.node.addEventListener('pointermove',move);
  card.node.addEventListener('pointerup',finish);card.node.addEventListener('pointercancel',finish);
  if(typeof addEventListener==='function'){addEventListener('pointermove',move);addEventListener('pointerup',finish);addEventListener('pointercancel',finish);addEventListener('blur',()=>finish());}
}
function addSpotlight(card,header){
  const button=element('button','spotlight-button',String.fromCodePoint(0x25a3));button.type='button';button.title='Spotlight full-width card';button.setAttribute('aria-pressed','false');card.spotlightButton=button;header.appendChild(button);
  button.addEventListener('click',()=>{
    const active=!card.node.classList.contains('spotlight');
    if(active)card.node.classList.add('spotlight');else card.node.classList.remove('spotlight');
    button.setAttribute('aria-pressed',String(active));
  });
}
function addRefresh(card,header){
  const button=element('button','refresh-button',String.fromCodePoint(0x21bb));button.type='button';button.title='Refresh view';card.refreshButton=button;header.appendChild(button);
  button.addEventListener('click',()=>{if(view==='desktops'&&card.approvedUrl&&card.sourceAvailable){card.image.removeAttribute('src');connect(card,card.approvedUrl);}else if(view==='windows'){for(const window of card.windowCards.values()){cancelWindowLoad(window);window.hash=window.hash?'refresh:'+window.hash:'';window.failedHash='';window.nextRetry=0;}renderWindows(card);}refresh();});
}
function connect(card,url){card.url=url;card.streamActive=true;card.failed=false;card.nextRetry=Date.now()+3000;card.image.src=url+'/stream.mjpg';}
function element(tag,className,text){const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=text;return node;}
function known(value){return typeof value==='number'&&Number.isFinite(value)&&value>=0;}
function percent(value){return known(value)?value.toFixed(1)+'%':'—';}
function bytes(value){if(!known(value))return '—';if(value<1024)return Math.round(value)+' B';const scale=value>=1073741824?1073741824:value>=1048576?1048576:1024;return (value/scale).toFixed(value/scale>=100?0:1)+' '+(scale===1073741824?'GiB':scale===1048576?'MiB':'KiB');}
function rate(value){return known(value)?bytes(value)+'/s':'—';}
function uptime(value){if(!known(value))return '—';if(value<60)return Math.floor(value)+'s';if(value<3600)return Math.floor(value/60)+'m';return Math.floor(value/3600)+'h '+Math.floor(value%3600/60)+'m';}
const cpuNote='App CPU as a percentage of total logical-processor capacity. The first sample is unavailable.';
function metricIcon(key){
  if(!document.createElementNS){const icon=element('span','metric-icon',String.fromCodePoint(0x25a1));icon.setAttribute('aria-hidden','true');return icon;}
  const paths={cpu:'M8 8h8v8H8z M5 5h14v14H5z M9 2v3 M15 2v3 M9 19v3 M15 19v3 M2 9h3 M2 15h3 M19 9h3 M19 15h3',ram:'M3 7h18v10H3z M6 10h3v4H6z M12 10h3v4h-3z M18 10v4 M6 17v3 M10 17v3 M14 17v3 M18 17v3',gpu:'M3 6h18v12H3z M7 18v3 M11 18v3 M16 18v3 M3 3v18 M7 10h6v4H7z M17 10v4',fps:'M4 5h16v14H4z M8 10l3 2-3 2z M16 9v6 M2 2h5 M2 2v5 M22 22h-5 M22 22v-5'};
  const icon=document.createElementNS('http://www.w3.org/2000/svg','svg'),path=document.createElementNS('http://www.w3.org/2000/svg','path');
  for(const [name,value] of Object.entries({viewBox:'0 0 24 24',width:'13',height:'13',fill:'none',stroke:'currentColor','stroke-width':'1.6','stroke-linecap':'round','stroke-linejoin':'round','aria-hidden':'true',focusable:'false',class:'metric-icon'}))icon.setAttribute(name,value);
  path.setAttribute('d',paths[key]);icon.appendChild(path);return icon;
}
function addAnalytics(card,header){
  const bar=element('div','analytics');card.metricsFields={};
  for(const [key,label,title] of [['cpu','APP CPU',cpuNote],['ram','RAM','Resident memory of application processes.'],['gpu','GPU','GPU engine utilization.'],['fps','CAPTURE FPS','New captured layouts per second; independent of stream delivery rate.']]){
    const metric=element('div','metric'),value=element('span','metric-value','—');metric.title=title;metric.setAttribute('role','img');metric.setAttribute('aria-label',label+': unavailable');metric.appendChild(metricIcon(key));metric.appendChild(value);bar.appendChild(metric);card.metricsFields[key]={metric,value};
  }
  card.detailsButton=element('button','details-toggle','Details');card.detailsButton.type='button';card.detailsButton.setAttribute('aria-expanded','false');bar.appendChild(card.detailsButton);header.appendChild(bar);
  card.details=element('section','process-details');card.details.hidden=true;card.processSummary=element('p','process-summary');card.details.appendChild(card.processSummary);
  const table=element('table','process-table'),thead=element('thead'),heading=element('tr');
  for(const label of ['Process','CPU','RAM','Read / write','Threads','Handles','Uptime']){const cell=element('th','',label);cell.setAttribute('scope','col');if(label==='CPU')cell.title=cpuNote;heading.appendChild(cell);}
  thead.appendChild(heading);table.appendChild(thead);card.processBody=element('tbody');table.appendChild(card.processBody);card.details.appendChild(table);header.appendChild(card.details);card.processRows=new Map();card.latestMetrics={};
  card.detailsButton.addEventListener('click',()=>{card.details.hidden=!card.details.hidden;card.detailsButton.setAttribute('aria-expanded',String(!card.details.hidden));card.detailsButton.textContent=card.details.hidden?'Details':'Hide details';if(card.details.hidden){card.processBody.replaceChildren();card.processRows.clear();}else renderProcesses(card);});
}
function renderProcesses(card){
  const metrics=card.latestMetrics,processes=Array.isArray(metrics.processes)?metrics.processes:[],active=new Set();
  const observer=metrics.observer||{},count=known(metrics.process_count)?metrics.process_count:Array.isArray(metrics.processes)?processes.length:'—';
  card.processSummary.textContent=count+' processes · '+(known(metrics.window_count)?metrics.window_count:'—')+' windows · Frame '+(known(metrics.frame_age_seconds)?metrics.frame_age_seconds.toFixed(1)+'s':'—')+' · Observer '+(observer.name?observer.name+' ':'')+(known(observer.pid)?'#'+observer.pid+' ':'')+percent(observer.cpu_percent)+' / '+bytes(observer.rss_bytes)+(known(observer.uptime_seconds)?' · '+uptime(observer.uptime_seconds):'');
  for(const process of processes){
    if(!Number.isInteger(process.pid)||process.pid<=0||active.has(process.pid))continue;active.add(process.pid);
    let row=card.processRows.get(process.pid);
    if(!row){const node=element('tr'),cells=[];for(let i=0;i<7;i++){const cell=element('td');cells.push(cell);node.appendChild(cell);}const name=element('span','process-name'),windows=element('span','process-windows');cells[0].appendChild(name);cells[0].appendChild(windows);row={node,cells,name,windows};card.processRows.set(process.pid,row);card.processBody.appendChild(node);}
    row.name.textContent=(process.name||'Process')+' · '+process.pid;
    row.windows.textContent=(Array.isArray(process.windows)?process.windows:[]).map(window=>window.title||'Untitled window').join(String.fromCharCode(10));
    row.cells[1].textContent=percent(process.cpu_percent);row.cells[1].title=cpuNote;
    row.cells[2].textContent=bytes(process.rss_bytes);row.cells[2].title='Private memory: '+bytes(process.private_bytes);
    row.cells[3].textContent=rate(process.read_bytes_per_second)+' / '+rate(process.write_bytes_per_second);
    row.cells[4].textContent=known(process.threads)?String(process.threads):'—';row.cells[5].textContent=known(process.handles)?String(process.handles):'—';row.cells[6].textContent=uptime(process.uptime_seconds);
  }
  for(const [pid,row] of card.processRows){if(!active.has(pid)){row.node.remove();card.processRows.delete(pid);}}
}
function renderMetrics(card,metrics){
  card.latestMetrics=metrics||{};const data=card.latestMetrics,gpu=data.gpu||{};
  card.metricsFields.cpu.value.textContent=percent(data.cpu_percent);card.metricsFields.ram.value.textContent=bytes(data.rss_bytes);
  card.metricsFields.gpu.value.textContent=gpu.available===true?percent(gpu.percent):'—';
  card.metricsFields.fps.value.textContent=known(data.capture_fps)?data.capture_fps.toFixed(1):'—';
  for(const [key,label,note] of [['cpu','App CPU',cpuNote],['ram','RAM','Resident memory. Private: '+bytes(data.private_bytes)],['gpu','GPU',gpu.label||'GPU data unavailable.'],['fps','Capture FPS','New captured layouts per second; independent of stream delivery rate.']]){const field=card.metricsFields[key],description=label+': '+field.value.textContent;field.metric.title=description+'. '+note;field.metric.setAttribute('aria-label',description);}
  if(!card.details.hidden)renderProcesses(card);
}
function renderWindows(card){
  const active=new Set();
  for(const item of card.latestWindows){
    const key=item.pid+':'+item.hwnd;active.add(key);let window=card.windowCards.get(key);
    if(!window){
      const node=element('article','window-card'),bar=element('div','window-bar'),title=element('h3','window-title'),status=element('span','window-state'),image=element('img','window-image'),placeholder=element('div','window-placeholder','Waiting for image');
      image.hidden=true;bar.appendChild(title);bar.appendChild(status);node.appendChild(bar);node.appendChild(image);node.appendChild(placeholder);card.windowsPanel.appendChild(node);
      node.addEventListener('click',()=>{if(node.classList.contains('zoomed'))node.classList.remove('zoomed');else node.classList.add('zoomed');});
      window={node,title,status,image,placeholder,hash:'',pending:'',generation:0,failedHash:'',nextRetry:0,candidate:null};card.windowCards.set(key,window);
    }
    window.title.textContent=item.title||'Untitled window';window.title.title=window.title.textContent+' · PID '+item.pid+' · HWND '+item.hwnd;window.image.alt=window.title.textContent;
    const stale=typeof item.captured_at==='number'&&Date.now()/1000-item.captured_at>3;
    window.status.textContent=!card.sourceAvailable?'Stale':item.status==='minimized'?'Minimized':item.status!=='captured'||window.failedHash===item.sha256?'Unavailable':stale?'Stale':window.hash===item.sha256?'Live':'Updating';
    window.placeholder.textContent=item.status==='minimized'?'Minimized':'Image unavailable';window.placeholder.hidden=!!window.hash;
    if(view==='windows'&&card.sourceAvailable&&item.status==='captured'&&item.sha256&&window.hash!==item.sha256&&window.pending!==item.sha256&&(window.failedHash!==item.sha256||Date.now()>=window.nextRetry)){
      cancelWindowLoad(window);const generation=window.generation,url=card.approvedUrl,candidate=new Image();window.pending=item.sha256;window.candidate=candidate;
      const current=()=>view==='windows'&&cards.get(card.id)===card&&card.windowCards.get(key)===window&&window.generation===generation&&card.approvedUrl===url;
      candidate.onload=()=>{if(!current())return;window.image.src=candidate.src;window.image.hidden=false;window.hash=item.sha256;window.pending='';window.failedHash='';window.status.textContent=typeof item.captured_at==='number'&&Date.now()/1000-item.captured_at>3?'Stale':'Live';window.placeholder.hidden=true;window.candidate=null;};
      candidate.onerror=()=>{if(!current())return;window.pending='';window.failedHash=item.sha256;window.nextRetry=Date.now()+3000;window.status.textContent='Unavailable';window.candidate=null;};
      candidate.src=url+'/window?hwnd='+encodeURIComponent(item.hwnd)+'&sha256='+encodeURIComponent(item.sha256);
    }
  }
  for(const [key,window] of card.windowCards){if(!active.has(key)){cancelWindowLoad(window);window.node.remove();card.windowCards.delete(key);}}
  card.windowsEmpty.hidden=card.latestWindows.length!==0;
}
function updateFleet(items){
  let live=0,stale=0,unavail=0,winTotal=0,procTotal=0,cpuSum=0,cpuKnown=false,ramSum=0,ramKnown=false,fpsSum=0,fpsMax=0,fpsCount=0;
  for(const item of items){
    if(item.state==='live')live++;else if(item.state==='stale')stale++;else unavail++;
    if(Array.isArray(item.windows))winTotal+=item.windows.length;
    const m=item.metrics;
    if(m){
      if(known(m.process_count))procTotal+=m.process_count;else if(Array.isArray(m.processes))procTotal+=m.processes.length;
      if(known(m.cpu_percent)){cpuSum+=m.cpu_percent;cpuKnown=true;}
      if(known(m.rss_bytes)){ramSum+=m.rss_bytes;ramKnown=true;}
      if(known(m.capture_fps)){fpsSum+=m.capture_fps;fpsMax=Math.max(fpsMax,m.capture_fps);fpsCount++;}
    }
  }
  const fpsAvg=fpsCount?fpsSum/fpsCount:0;
  if(fleetHealth)fleetHealth.textContent=live+' live · '+stale+' stale'+(unavail?(' · '+unavail+' down'):'');
  if(fleetWindows)fleetWindows.textContent=winTotal+' window'+(winTotal===1?'':'s');
  if(fleetProcs)fleetProcs.textContent=procTotal+' proc'+(procTotal===1?'':'s');
  if(fleetCpu)fleetCpu.textContent=cpuKnown?cpuSum.toFixed(1)+'%':'—';
  if(fleetRam)fleetRam.textContent=ramKnown?bytes(ramSum):'—';
  if(fleetFps)fleetFps.textContent=fpsCount?(fpsCount>1?fpsAvg.toFixed(1)+' avg · '+fpsMax.toFixed(1)+' peak FPS':fpsAvg.toFixed(1)+' FPS live'):'Measuring…';
  if(fpsBadge)fpsBadge.textContent=fpsCount?'DWM GPU · '+fpsAvg.toFixed(1)+' FPS LIVE · 4:4:4 AUTO-SCALE':'DWM GPU · 60 FPS AUTO-SCALE';
}
function render(items){
  const active=new Set(items.map(item=>item.id));
  for(const [id,card] of cards){if(!active.has(id)){if(activeDrag&&activeDrag.card===card)activeDrag=null;card.streamActive=false;card.image.removeAttribute('src');for(const window of card.windowCards.values())cancelWindowLoad(window);card.node.remove();cards.delete(id);}}
  for(const item of items){
    let card=cards.get(item.id);
    if(!card){
      const node=document.createElement('section'),header=document.createElement('header'),title=document.createElement('button'),status=document.createElement('span'),button=document.createElement('button'),surface=document.createElement('div'),image=document.createElement('img'),empty=document.createElement('span');
      const hud=element('div','surface-hud'),hudLeft=element('span','hud-pill',''),hudRight=element('span','hud-pill','DWM GPU · Measuring…');hud.appendChild(hudLeft);hud.appendChild(hudRight);
      node.className='desktop';node.dataset.id=item.id;image.draggable=false;status.className='status';surface.className='surface';empty.className='empty';empty.textContent='Waiting for desktop';button.type='button';button.className='fullscreen-button';button.textContent=String.fromCodePoint(0x26f6);button.title='Full screen';button.setAttribute('aria-expanded','false');
      header.appendChild(title);header.appendChild(status);header.appendChild(button);surface.appendChild(image);surface.appendChild(empty);surface.appendChild(hud);node.appendChild(header);node.appendChild(surface);grid.appendChild(node);
      const windowsPanel=element('div','windows-panel'),windowsEmpty=element('div','windows-empty','No visible windows');windowsPanel.hidden=view!=='windows';windowsPanel.appendChild(windowsEmpty);node.appendChild(windowsPanel);surface.hidden=view!=='desktops';
      const sequence=Number.isInteger(item.sequence)&&item.sequence>0?item.sequence:nextSequence;nextSequence=Math.max(nextSequence,sequence+1);
      card={id:item.id,name:item.name,sequence,node,title,status,button,image,empty,surface,hudLeft,hudRight,windowsPanel,windowsEmpty,windowCards:new Map(),latestWindows:[],approvedUrl:'',sourceAvailable:false,streamActive:false,url:'',failed:false,nextRetry:0};cards.set(item.id,card);
      addReorder(card,header);
      addSpotlight(card,header);
      addRefresh(card,header);
      addRename(card,header);
      addAnalytics(card,header);
      button.addEventListener('click',()=>expand(card));
      image.onload=()=>{card.empty.hidden=true;};
      image.onerror=()=>{if(view!=='desktops'||!card.streamActive)return;card.failed=true;card.nextRetry=Date.now()+3000;card.node.dataset.state='unavailable';card.status.textContent='Unavailable';};
    }
    if(Number.isInteger(item.sequence)&&item.sequence>0)card.sequence=item.sequence;
    card.name=item.name;card.title.dataset.sequence=String(card.sequence).padStart(2,'0');labelCard(card);renderMetrics(card,item.metrics);
    card.node.dataset.state=item.state;card.status.textContent=item.state==='live'?'Live':item.state==='stale'?'Stale':'Unavailable';
    card.sourceAvailable=!!item.url;if(item.url)card.approvedUrl=item.url;if(Array.isArray(item.windows))card.latestWindows=item.windows;
    const topWin=card.latestWindows[0];
    card.hudLeft.textContent=card.latestWindows.length+' win'+(topWin&&topWin.title?' · '+topWin.title:'');
    const m=item.metrics||{};
    card.hudRight.textContent='DWM GPU 4:4:4 · '+(known(m.capture_fps)?m.capture_fps.toFixed(1)+' FPS live':'Measuring FPS')+(known(m.frame_age_seconds)?' · '+m.frame_age_seconds.toFixed(1)+'s age':'');
    if(view==='desktops'&&item.url&&(!card.streamActive||card.url!==item.url||(card.failed&&Date.now()>=card.nextRetry)))connect(card,item.url);
    if(view==='windows')renderWindows(card);
    if(view==='desktops'&&card.failed){card.node.dataset.state='unavailable';card.status.textContent='Unavailable';}
  }
  restoreOrder();
  updateFleet(items);
  summary.textContent=items.length+' desktop'+(items.length===1?'':'s');
}
async function refresh(){
  if(polling)return;polling=true;
  try{const response=await fetch(base+'/meta',{cache:'no-store',credentials:'same-origin'});if(!response.ok)throw Error();const data=await response.json();if(typeof data.build==='string'){if(initialBuild===null)initialBuild=data.build;else if(data.build!==initialBuild){if(!reloading){reloading=true;location.reload();}return;}}render(data.desktops);}
  catch(_){summary.textContent='Connection unavailable';for(const card of cards.values()){card.node.dataset.state='stale';card.status.textContent='Stale';}}
  finally{polling=false;}
}
refresh();setInterval(refresh,1000);
</script></body></html>""".encode("utf-8")


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _pid(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def watch_config(root):
    root = Path(root).resolve(strict=True)
    config = observe._read_json_file(root / "watch.json", root)
    if (not isinstance(config, dict) or config.get("schema") != "desktop-watch-v1"
            or Path(config.get("watch", "")).resolve() != root):
        raise ValueError("Invalid observer directory.")
    parsed = urlsplit(config.get("url", ""))
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username is not None
            or parsed.password is not None or not parsed.port or parsed.query or parsed.fragment
            or parsed.netloc != f"127.0.0.1:{parsed.port}"
            or not re.fullmatch(r"/[A-Za-z0-9_-]{32,128}", parsed.path)):
        raise ValueError("Expected a token-protected loopback observer URL.")
    source = config.get("source_identity")
    if (not isinstance(source, list) or len(source) != 4 or not isinstance(source[0], str)
            or not source[0].startswith(("PyCharmHarness_", "StealthREA_"))
            or not _pid(source[1]) or not _finite(source[2]) or not isinstance(source[3], str)
            or not Path(source[3]).is_absolute()
            or not _pid(config.get("server_pid")) or not _finite(config.get("server_created"))):
        raise ValueError("Invalid observer process identity.")
    return root, cached_capture.approved_config(config)


def check_processes(config):
    desktop, pid, created, executable = config["source_identity"]
    h.owned_process({"pid": pid, "process_created": created, "ide_executable": executable})
    server = h.psutil.Process(config["server_pid"])
    if abs(server.create_time() - config["server_created"]) > .01:
        raise ValueError("Observer process identity changed.")
    # Both observers and the dashboard use the selected harness interpreter.
    if Path(server.exe()).resolve() != Path(sys.executable).resolve():
        raise ValueError("Observer interpreter does not match.")
    port = urlsplit(config["url"]).port
    connections = getattr(server, "net_connections", None) or server.connections
    if not any(connection.status == h.psutil.CONN_LISTEN and connection.laddr.ip == "127.0.0.1"
               and connection.laddr.port == port for connection in connections(kind="tcp")):
        raise ValueError("Observer process does not own the stream listener.")
    if config.get("worker_created") is not None:
        worker = h.psutil.Process(config["worker_pid"])
        if (worker.create_time() != config["worker_created"] or not worker.is_running()
                or Path(worker.exe()).resolve() != Path(sys.executable).resolve()):
            raise ValueError("Observer worker identity changed.")


def approve_watch(path, name):
    root, config = watch_config(path)
    check_processes(config)
    if not name or len(name) > 100 or any(not char.isprintable() for char in name):
        raise ValueError("Use a short printable desktop name.")
    return {"root": root, "config": config, "name": name,
            "id": hashlib.sha256(str(root).encode()).hexdigest()[:16]}


_WORKER_CACHE = {}


def resource_status(root, config, layout, sampler, gpu):
    sample = sampler.sample(root, config, layout)
    windows = observe._public_workspace(layout)["windows"]
    processes = sample["processes"]
    worker_pids = []
    server_pid = config.get("server_pid")
    if _pid(server_pid):
        cache_key = (server_pid, config.get("server_created"))
        cached = _WORKER_CACHE.get(cache_key)
        now_mono = time.monotonic()
        if cached and now_mono - cached[0] < 5.0:
            worker_pids = cached[1]
        else:
            try:
                worker_pids = [child.pid for child in h.psutil.Process(server_pid).children() if _pid(child.pid)]
            except Exception:
                worker_pids = []
            _WORKER_CACHE[cache_key] = (now_mono, worker_pids)
    gpu_pids = [row["pid"] for row in processes]
    for wpid in worker_pids:
        if wpid not in gpu_pids:
            gpu_pids.append(wpid)
    readings = gpu.snapshot(gpu_pids) if gpu else {}
    engines = {}
    measured = 0
    for row in processes:
        row["windows"] = [{"hwnd": w["hwnd"], "title": w.get("title", "")}
                          for w in windows if w["pid"] == row["pid"]]
        reading = readings.get(row["pid"], {})
        row["gpu_percent"] = reading.get("gpu_percent")
        row["gpu_status"] = reading.get("gpu_status", "unavailable")
        current = reading.get("gpu_engines")
        if isinstance(current, dict) and current:
            measured += 1
            for engine, value in current.items():
                engines[engine] = min(100.0, engines.get(engine, 0.0) + value)
    obs_gpu_pct = None
    obs_gpu_status = "unavailable"
    for wpid in worker_pids:
        wreading = readings.get(wpid, {})
        wengines = wreading.get("gpu_engines")
        if isinstance(wengines, dict) and wengines:
            measured += 1
            for engine, value in wengines.items():
                engines[engine] = min(100.0, engines.get(engine, 0.0) + value)
        wpct = wreading.get("gpu_percent")
        if wpct is not None and (obs_gpu_pct is None or wpct > obs_gpu_pct):
            obs_gpu_pct = wpct
            obs_gpu_status = wreading.get("gpu_status", "ok")
    if isinstance(sample.get("observer"), dict) and worker_pids:
        sample["observer"]["gpu_percent"] = obs_gpu_pct
        sample["observer"]["gpu_status"] = obs_gpu_status
    result = dict(sample["apps"])
    result.update(processes=processes, observer=sample["observer"],
                  window_count=sample["capture"]["window_count"],
                  capture_fps=sample["capture"]["fps"],
                  frame_age_seconds=sample["capture"]["frame_age_seconds"],
                  gpu={"percent": max(engines.values()) if engines else None,
                       "available": bool(engines), "measured_processes": measured,
                       "process_count": len(processes),
                       "label": "Busiest measured GPU engine; shared processes may include other work"},
                  scope=sample["apps"]["scope"])
    return result


def desktop_status(source, sampler=None, gpu=None, capture_cache=None):
    result = {"id": source["id"], "name": source["name"], "state": "unavailable"}
    if "sequence" in source:
        result["sequence"] = source["sequence"]
    try:
        root, config = watch_config(source["root"])
        if root != source["root"] or config != source["config"]:
            return result
        check_processes(config)
        state = observe._read_json_file(root / "browser-session.json", root)
        desktop, pid, _, _ = config["source_identity"]
        if (not isinstance(state, dict) or state.get("schema") != "rea-browser-v1" or state.get("desktop") != desktop
                or state.get("pid") != pid or Path(state.get("session", "")).resolve() != root):
            return result
        if state.get("status") != "running":
            return result
        layout = observe._workspace(root)
        heartbeat = state.get("heartbeat")
        stale = (not _finite(heartbeat) or time.time() - heartbeat > 25
                 or time.time() - layout["captured_at"] > 3
                 or state.get("error") or state.get("capture_error"))
        result.update(state="stale" if stale else "live", url=config["url"],
                      windows=observe._public_workspace(layout)["windows"])
        result["capture_refs"] = capture_cache.publish(source) if capture_cache is not None and not stale else []
        if sampler is not None:
            try:
                result["metrics"] = resource_status(root, config, layout, sampler, gpu)
            except (OSError, ValueError, KeyError, TypeError, RuntimeError, h.psutil.Error):
                result["metrics"] = None
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, h.psutil.Error):
        pass
    return result


def make_handler(sources, token, sampler=None, gpu=None, registry=None):
    capture_cache = cached_capture.ObservationCache()
    approved = {source["root"]: source for source in sources}
    sequence = {source["config"]["source_identity"][0]: index + 1
                for index, source in enumerate(sorted(sources, key=lambda s: s["config"]["source_identity"][2]))}
    source_lock = threading.Lock()
    build = hashlib.sha256(HTML).hexdigest()[:16]

    def current_sources():
        if registry is None:
            entries = [{"root": source["root"], "name": source["name"]} for source in sources]
        else:
            entries = registry.snapshot()
        result = []
        with source_lock:
            for entry in entries[:16]:
                root = Path(entry["root"]).resolve()
                source = approved.get(root)
                if source is None:
                    try:
                        source = approve_watch(root, entry["name"])
                    except (OSError, ValueError, KeyError, TypeError, RuntimeError, h.psutil.Error):
                        continue
                    approved[root] = source
                desktop = source["config"]["source_identity"][0]
                if desktop not in sequence:
                    sequence[desktop] = max(sequence.values(), default=0) + 1
                source["sequence"] = sequence[desktop]
                result.append(source)
        return sorted(result, key=lambda s: s["sequence"])

    class Handler(BaseHTTPRequestHandler):
        server_version = "LocalDesktopDashboard"
        sys_version = ""

        def log_message(self, _format, *args):
            pass

        def _send(self, code, data, content_type="application/json; charset=utf-8"):
            body = data if isinstance(data, bytes) else json.dumps(data, separators=(",", ":")).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; img-src http://127.0.0.1:*; connect-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if (not ipaddress.ip_address(self.client_address[0]).is_loopback
                    or self.headers.get_all("Host", []) != [f"127.0.0.1:{self.server.server_port}"]):
                self._send(403, {"error": "forbidden"})
            elif self.path == "/" + token:
                self._send(200, HTML, "text/html; charset=utf-8")
            elif self.path == "/" + token + "/meta":
                self._send(200, {"build": build, "desktops": [
                    desktop_status(source, sampler, gpu, capture_cache) for source in current_sources()]})
            elif urlsplit(self.path).path == "/" + token + "/capture":
                self._capture()
            else:
                self._send(404, {"error": "not found"})

        def _capture(self):
            origin = f"http://127.0.0.1:{self.server.server_port}"
            if (self.headers.get("Origin") not in (None, origin) or self.headers.get("Sec-Fetch-Site") == "cross-site"
                    or self.headers.get("Cf-Connecting-Ip") or self.headers.get("X-Forwarded-For")):
                self._send(403, {"ok": False, "error": "Cross-origin capture export is forbidden."})
                return
            try:
                query = parse_qs(urlsplit(self.path).query, strict_parsing=True, keep_blank_values=True, max_num_fields=4)
                keys = set(query)
                if (keys not in ({"desktop_id", "hwnd"}, {"desktop_id", "hwnd", "sha256", "capture_sequence"})
                        or any(len(values) != 1 for values in query.values())
                        or re.fullmatch(r"[0-9a-f]{16}", query["desktop_id"][0]) is None
                        or re.fullmatch(r"[1-9][0-9]{0,18}", query["hwnd"][0]) is None):
                    raise ValueError("Invalid frame reference")
                source = next((entry for entry in current_sources() if entry["id"] == query["desktop_id"][0]), None)
                if source is None:
                    raise cached_capture.Unavailable("The approved desktop is unavailable.")
                sequence = query.get("capture_sequence", [None])[0]
                if sequence is not None and re.fullmatch(r"[1-9][0-9]{0,18}", sequence) is None:
                    raise ValueError("Invalid capture sequence")
                result = capture_cache.export(source, int(query["hwnd"][0]), query.get("sha256", [None])[0],
                                              int(sequence) if sequence is not None else None, check_processes)
                self._send(200, result)
            except cached_capture.Unavailable:
                self._send(409, {"ok": False, "error": "The published frame is unavailable, changed, protected or expired. Read a current observation reference."})
            except Exception:
                self._send(400, {"ok": False, "error": "Observability export could not be verified."})

        def do_POST(self):
            self._send(405, {"error": "read only"})

        do_PUT = do_DELETE = do_PATCH = do_POST

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", type=Path, action="append", required=True)
    parser.add_argument("--name", action="append", default=[])
    parser.add_argument("--status-file", type=Path, required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--discover-root", type=Path,
                        help="Runtime directory for automatic observation of new harness desktops.")
    parser.add_argument("--scan-root", type=Path, action="append", default=[],
                        help="Reuse existing observer directories under this runtime directory.")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535 or len(args.watch) > 16:
        parser.error("Use a valid port and at most 16 desktops.")
    if args.name and len(args.name) != len(args.watch):
        parser.error("Supply one --name for every --watch, or omit all names.")
    h.dependencies(False)
    h.native_modules()
    names = args.name or [f"Desktop {index + 1}" for index in range(len(args.watch))]
    sources = [approve_watch(path, name) for path, name in zip(args.watch, names)]
    if len({source["root"] for source in sources}) != len(sources):
        parser.error("Each observer directory must be unique.")
    from metrics import DesktopMetricsSampler
    from gpu_metrics import GpuSampler
    sampler, gpu = DesktopMetricsSampler(), GpuSampler()
    registry = None
    if args.discover_root is not None:
        from desktop_registry import DesktopRegistry
        registry = DesktopRegistry(sources, args.discover_root, scan_roots=args.scan_root)
        registry.start()
    token = secrets.token_urlsafe(32)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(sources, token, sampler, gpu, registry))
    server.daemon_threads = True
    status_file = args.status_file.resolve()
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status = {"schema": "desktop-dashboard-v1", "url": f"http://127.0.0.1:{server.server_port}/{token}",
              "pid": os.getpid(), "created": h.psutil.Process().create_time(), "status": "running"}
    h.write_json(status_file, status)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if registry:
            registry.close()
        gpu.close()
        h.write_json(status_file, {**status, "status": "stopped"})


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(json.dumps({"ok": False, "error": "Dashboard startup failed; check observer directories and dependencies."}), file=sys.stderr)
        raise SystemExit(1)
