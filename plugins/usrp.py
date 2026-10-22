"""USRP (DVSwitch) channel plugin — bridges the gateway bus to an AllStarLink
node over the USRP UDP protocol.

Why this exists
---------------
AllStar is not an open relay: you can only inject/extract audio from a node
that is configured to accept you. To talk through a *public* node we don't
control, we stand up our own minimal ASL3 "bridge node" (no radio hardware)
whose ``rxchannel`` is a USRP channel pointed at this gateway, and have it
*connect* to the target node like any other AllStar link. This plugin is the
gateway end of that USRP socket — it looks like any other bus source/sink.

ASL config (rpt.conf on the bridge node):
    rxchannel = USRP/<gateway_ip>:34001:32001
                       └ where ASL SENDS  ┘ └ where ASL LISTENS ┘
So this plugin LISTENS on 34001 (USRP_LISTEN_PORT) and SENDS to the bridge
node's :32001 (USRP_REMOTE_HOST/USRP_REMOTE_PORT). chan_usrp must be enabled
in the node's modules.conf.

Wire format (verified against DVSwitch/USRP_Client pyUC.py):
    32-byte header  = b"USRP" + 7x big-endian int32:
        seq, memory, keyup(=PTT), talkgroup, type, mpxid, reserved
    voice payload   = 160 x int16 LITTLE-endian @ 8 kHz  (320 bytes, 20 ms)
    type: 0=voice, 2=text/metadata, 3=ping

Rate bridging: bus is 48 kHz s16 mono (2400 samples / 50 ms chunk); USRP is
8 kHz (160 samples / 20 ms frame). We resample x6 on RX and /6 on TX with
scipy.signal.resample_poly.

Audio format on the bus everywhere: 48000 Hz, signed 16-bit LE, mono.
One chunk = 2400 samples = 4800 bytes = 50 ms.
"""

from __future__ import annotations

try:
    from log_util import log_exc
except ImportError:   # file used on its own (worker copy, self-test run)
    def log_exc(*_a, **_k):
        pass
import collections
import json
import os
import socket
import threading
import time
from struct import Struct

import numpy as np

try:
    from scipy.signal import resample_poly
except ImportError:  # pragma: no cover - scipy is a hard dep of the gateway
    resample_poly = None

from audio_util import pcm_level

# Plugin contract capability flags
try:
    from plugins._base import (
        CAPABILITY_AUDIO_RX, CAPABILITY_AUDIO_TX,
        CAPABILITY_PTT, CAPABILITY_STATUS,
    )
except Exception:  # standalone self-test import fallback
    CAPABILITY_AUDIO_RX = 'audio_rx'
    CAPABILITY_AUDIO_TX = 'audio_tx'
    CAPABILITY_PTT = 'ptt'
    CAPABILITY_STATUS = 'status'


# ── USRP protocol constants ────────────────────────────────────────
USRP_HEADER = Struct('>4s7i')          # b"USRP" + seq,mem,keyup,tg,type,mpxid,reserved
USRP_HEADER_LEN = 32
USRP_VOICE_SAMPLES = 160               # int16 samples per voice frame @ 8 kHz
USRP_VOICE_BYTES = USRP_VOICE_SAMPLES * 2
USRP_FRAME_MS = 0.020                  # 20 ms per voice frame
USRP_TYPE_VOICE = 0
USRP_TYPE_TEXT = 2
USRP_TYPE_PING = 3

# app_rpt ilink subcommands (issued to the bridge node over AMI). The target
# node is runtime-configurable from the control panel — nothing is baked into
# the node's rpt.conf.
ILINK_DISCONNECT = 1        # disconnect one specified link
ILINK_MONITOR = 2           # connect — receive (monitor) only
ILINK_TRANSCEIVE = 3        # connect — full transceive (talk + listen)
ILINK_DISCONNECT_ALL = 6
ILINK_PERMA_DISCONNECT = 11  # drop a link previously connected as permanent
ILINK_PERMA_MONITOR = 12    # permanent connect — monitor only
ILINK_PERMA_TRANSCEIVE = 13  # permanent connect — full transceive

# We deliberately do NOT use app_rpt's permanent mode (12/13) to hold links up.
#
# "Permanent" is app_rpt's word and it means something narrower than what a
# user wants: the flag lives only in Asterisk's memory, so it does not survive
# an ASL restart or a reboot at all, and while set app_rpt re-dials every ~10s
# for ever with no backoff and no way to tune it. Running it alongside the
# reconciler meant two independent retry loops fighting, only one of which
# could be told to slow down.
#
# What is wanted is PERSISTENCE: a link you asked for is restored after a
# gateway restart, an ASL restart, a reboot, or a three-month absence — and
# stays gone once you disconnect it. That comes from usrp_desired.json on disk
# plus the reconciler below, which owns all retry timing. ILINK_PERMA_DISCONNECT
# is still used defensively to clear flags older builds may have left set.
_CONNECT_MODES = (ILINK_TRANSCEIVE, ILINK_MONITOR)


def _gw_root():
    """Gateway root — plugins/ lives one level down."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── shared node address book ─────────────────────────────────────────────
class _NodeBook:
    """Node number → the name YOU gave it, shared by every USRP instance.

    Both panels reach the same AllStar network, so a per-instance list means
    naming 45412 twice and then remembering which panel you named it in. One
    file, one list, both panels — and because both plugin instances live in
    the same process, one in-memory copy behind one lock.

    Entries carry last_used/uses so the dropdown can sort most-recent-first:
    that subsumes the two old per-instance recents files, whose contents are
    migrated in on first load rather than thrown away.
    """

    # Kept in step with the maxlength on the panel's name field and with the
    # 5.5rem/1fr column grid it is rendered into — a longer name would only
    # ellipsis away, so truncate at the source rather than store what cannot
    # be shown.
    MAX_NAME = 30

    def __init__(self, path):
        self.path = path
        self._d = {}
        self._lock = threading.Lock()
        self._loaded = False

    def _load_locked(self):
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(self.path) as f:
                data = json.load(f)
            for node, ent in (data.get('nodes') or {}).items():
                if str(node).isdigit() and isinstance(ent, dict):
                    self._d[str(node)] = {
                        'name': str(ent.get('name', ''))[:self.MAX_NAME],
                        'last_used': float(ent.get('last_used', 0) or 0),
                        'uses': int(ent.get('uses', 0) or 0),
                    }
        except (OSError, ValueError, AttributeError):
            pass
        self._migrate_recents_locked()

    def _migrate_recents_locked(self):
        """Fold the old per-instance recents files in, once, unnamed.

        Losing the history on upgrade would mean retyping node numbers that
        were already known — the exact friction this book exists to remove.
        """
        changed = False
        for fname in ('usrp_recent.json', 'usrp2_recent.json'):
            try:
                with open(os.path.join(_gw_root(), fname)) as f:
                    old = json.load(f)
            except (OSError, ValueError):
                continue
            if not isinstance(old, list):
                continue
            for i, node in enumerate(old):
                node = str(node)
                if node.isdigit() and node not in self._d:
                    # Preserve relative order without inventing timestamps
                    # that would outrank genuinely recent entries.
                    self._d[node] = {'name': '', 'last_used': float(len(old) - i),
                                     'uses': 0}
                    changed = True
        if changed:
            self._save_locked()

    def _save_locked(self):
        try:
            from atomic_json import save_json
            save_json(self.path, {'nodes': self._d}, indent=1)
        except (OSError, ImportError):
            pass

    def all(self):
        """Entries as a list, most recently used first."""
        with self._lock:
            self._load_locked()
            rows = [{'node': n, **e} for n, e in self._d.items()]
        rows.sort(key=lambda r: (-r['last_used'], r['node']))
        return rows

    def name_of(self, node):
        with self._lock:
            self._load_locked()
            ent = self._d.get(str(node))
            return ent['name'] if ent else ''

    def touch(self, node):
        """Record a connect: creates the entry unnamed if it is new."""
        node = str(node).strip()
        if not node.isdigit():
            return
        with self._lock:
            self._load_locked()
            ent = self._d.setdefault(node, {'name': '', 'last_used': 0.0, 'uses': 0})
            ent['last_used'] = time.time()
            ent['uses'] = int(ent.get('uses', 0)) + 1
            self._save_locked()

    def set_name(self, node, name):
        node = str(node).strip()
        if not node.isdigit():
            return False
        name = ' '.join(str(name).split())[:self.MAX_NAME]
        with self._lock:
            self._load_locked()
            ent = self._d.setdefault(node, {'name': '', 'last_used': 0.0, 'uses': 0})
            ent['name'] = name
            self._save_locked()
        return True

    def remove(self, node):
        with self._lock:
            self._load_locked()
            existed = self._d.pop(str(node), None) is not None
            if existed:
                self._save_locked()
        return existed


# One book for the whole process, shared by usrp and usrp2.
NODE_BOOK = _NodeBook(os.path.join(_gw_root(), 'usrp_nodes.json'))


# ── control panel page ─────────────────────────────────────────────
_PANEL_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__LABEL__ — node __NODE__</title>
<style>
  /* This panel is standalone — it does NOT load common.css, so the theme
     vars it references have to be defined here. They were not: every
     `color:var(--t-err)` in this file resolved to nothing and silently
     inherited, so the link-reconnect warning had never once shown a colour.
     Values mirror web_pages/common.css. */
  :root { color-scheme: dark;
          --t-accent:#4fd6e6; --t-ok:#5dc47a;
          --t-warn:#e89d3c;   --t-err:#e04848; }
  body { font-family: system-ui, sans-serif; background:#14171c; color:#e6e9ef;
         margin:0; padding:1.2rem; }
  h1 { font-size:1.1rem; margin:0 0 .2rem; }
  .sub { color:#8b94a3; font-size:.8rem; margin-bottom:1rem; }
  .card { background:#1c2128; border:1px solid #2a313b; border-radius:10px;
          padding:1rem; margin-bottom:1rem; max-width:560px; }
  .row { display:flex; gap:.5rem; align-items:center; flex-wrap:wrap; }
  input[type=text] { background:#0f1216; border:1px solid #2a313b; color:#e6e9ef;
          border-radius:7px; padding:.5rem .6rem; font-size:1rem; width:8rem; }
  button { border:0; border-radius:7px; padding:.5rem .9rem; font-size:.9rem;
          cursor:pointer; color:#fff; }
  .b-conn { background:#2f9e44; } .b-mon { background:#1971c2; }
  .b-dis { background:#5c636e; } .b-all { background:#c92a2a; }
  button:active { transform:translateY(1px); }
  .links { list-style:none; padding:0; margin:.4rem 0 0; }
  .links li { display:flex; justify-content:space-between; align-items:center;
          padding:.4rem .2rem; border-bottom:1px solid #232a33; }
  .pill { display:inline-block; padding:.1rem .5rem; border-radius:999px;
          font-size:.72rem; font-weight:600; }
  .on { background:#2f9e44; } .off { background:#3a424d; color:#9aa3b0; }
  .stat { display:grid; grid-template-columns:auto auto; gap:.2rem 1rem;
          font-size:.82rem; color:#b9c1cd; }
  .stat b { color:#e6e9ef; font-weight:600; }
  .msg { font-size:.78rem; color:#8b94a3; margin-top:.6rem; min-height:1em;
         word-break:break-word; }
  .none { color:#6b7480; font-size:.85rem; }
  .links li small { color:#6b7480; font-size:.72rem; margin-left:.4rem; }
  /* Saved-node / kept-connected rows. The plain .links li above is
     space-between with loose children, so the buttons track the length of
     each label and stagger down the list. These rows are a fixed column
     grid instead: node number, then name, then actions — so the buttons sit
     on the same x no matter what you type into the name field. */
  .links li.arow { display:grid; align-items:center; gap:.5rem;
          grid-template-columns:5.5rem minmax(0,1fr) auto; }
  .arow .nodenum { font-variant-numeric:tabular-nums; color:#b9c1cd; }
  /* Node number doubles as a link to that node's AllStarLink stats page.
     It lives in its own fixed column, so making it the link keeps the row
     alignment intact instead of adding a third thing to the button group. */
  /* Not scoped to .arow: every node number on this page is a stats link,
     including the connected/conference lists and the header. */
  a.nodenum { color:var(--t-accent); text-decoration:none; }
  a.nodenum:hover, a.nodenum:focus { text-decoration:underline; }
  /* Names are capped at 30 chars server-side; ellipsis is the backstop so a
     pasted-in long one can never push the buttons out of line. */
  .arow .nm { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .arow .nm small { margin-left:0; display:block; }
  .arow .acts { display:flex; gap:.35rem; justify-content:flex-end; }
  .arow .acts button { min-width:4.4rem; }
  .links li.indirect { opacity:.6; }
  .links li.indirect span::before { content:'\\21B3 '; color:#6b7480; }
  .grouplbl { color:#8b94a3; font-size:.72rem; text-transform:uppercase;
              letter-spacing:.04em; margin:.6rem 0 .1rem; }
</style></head><body>
<h1>__LABEL__ — node <a class="nodenum" href="https://stats.allstarlink.org/stats/__NODE__" target="_blank" rel="noopener noreferrer" title="AllStarLink stats for this node">__NODE__</a></h1>
<div class="sub" id="amiline">via USRP bridge</div>

<div class="card">
  <div class="row">
    <input type="text" id="node" inputmode="numeric" placeholder="node #">
    <select id="book" title="saved nodes — shared with the other AllStar panel"
            onchange="if(this.value){document.getElementById('node').value=this.value;} this.selectedIndex=0;">
      <option value="">saved ▾</option>
    </select>
    <button class="b-conn" onclick="act('connect','transceive')">Connect</button>
    <button class="b-mon" onclick="act('connect','monitor')">Monitor</button>
    <button class="b-all" onclick="act('disconnect_all')">Disconnect all</button>
  </div>
  <div class="row" style="margin-top:.4rem">
    <input type="text" id="nodename" placeholder="name for this node (max 30)" maxlength="30"
           style="flex:1 1 12rem">
    <button class="b-mon" onclick="saveName()">Save name</button>
  </div>
  <div class="msg" id="msg"></div>
</div>

<div class="card">
  <div class="row" style="justify-content:space-between">
    <strong>Saved nodes</strong>
    <span class="sub">one list, used by both AllStar nodes</span>
  </div>
  <ul class="links" id="booklist"></ul>
</div>

<div class="card" id="relink-card" style="display:none">
  <div class="row" style="justify-content:space-between">
    <strong>Kept connected (persistent)</strong>
    <span class="sub" id="relink-counters"></span>
  </div>
  <ul class="links" id="relinklist"></ul>
</div>

<div class="card">
  <div class="stat">
    <span>RX (incoming COS)</span><b id="rx">—</b>
    <span>TX (keyed)</span><b id="tx">—</b>
  </div>
  <div style="margin-top:.5rem;font-size:.75rem;color:#888;text-transform:uppercase;letter-spacing:.05em">Local link (ASL node → gateway)</div>
  <div class="stat" style="margin-top:.25rem">
    <span>Packets in / out</span><b id="pk">—</b>
    <span>RX queue depth</span><b id="q">—</b>
    <span>RX rate</span><b id="pps">—</b>
    <span>Packet loss</span><b id="loss">—</b>
  </div>
  <div style="margin-top:.5rem;font-size:.75rem;color:#888;text-transform:uppercase;letter-spacing:.05em">Remote node stats (via ASL)</div>
  <div class="stat" style="margin-top:.25rem">
    <span>Node uptime</span><b id="nd-uptime">—</b>
    <span>Keyups today</span><b id="nd-keyups">—</b>
    <span>TX time today</span><b id="nd-txtime">—</b>
    <span>Timeouts</span><b id="nd-timeouts">—</b>
  </div>
  <div id="cache-note" style="font-size:.7rem;color:#6b7480;margin-top:.25rem"></div>
  <div id="nd-links-stats" style="margin-top:.25rem"></div>
</div>

<div class="card">
  <div class="row" style="justify-content:space-between">
    <strong>Connected nodes</strong>
    <span class="sub" id="age"></span>
  </div>
  <ul class="links" id="links"></ul>
</div>

<script>
const $ = id => document.getElementById(id);
function pill(on){ return `<span class="pill ${on?'on':'off'}">${on?'YES':'no'}</span>`; }
async function post(action, extra){
  const body = Object.assign({action}, extra||{});
  const r = await fetch('/usrp/control', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  return r.json();
}
async function act(action, mode){
  const node = $('node').value.trim();
  if(action!=='disconnect_all' && !/^[0-9]+$/.test(node)){ $('msg').textContent='enter a node number'; return; }
  $('msg').textContent='working…';
  const res = await post(action, {node, mode});
  // A connect is only a tick when the link actually came UP. 'established'
  // is absent on non-connect actions, so fall back to res.ok there.
  const good = ('established' in res) ? res.established : res.ok;
  $('msg').textContent = (good?'✓ ':'✗ ') + (res.output||res.error||action);
  $('msg').style.color = good ? '' : 'var(--t-warn)';
  refreshLinks();
}
async function dis(node){ $('msg').textContent='disconnecting '+node+'…';
  const res = await post('disconnect',{node}); $('msg').textContent=(res.ok?'✓ ':'✗ ')+node; refreshLinks(); }
// Cache painting is driven by poll() (every 1.5 s) rather than run once on
// load. As a one-shot it painted whatever the cache held at that instant — so
// opening the page during the ~10 s after a gateway restart showed "not
// current" and then never re-checked, which looked exactly like the original
// slow load it was meant to fix.
let _liveLinks = false;   // true once an AMI-backed refresh has painted
function applyCache(s){
  const note = $('cache-note');
  if(note){
    // Only speak up when the data is NOT current. When it is fresh the
    // existing "updated Ns ago" line already says so.
    const stale = s.links_stale || s.stats_stale;
    note.textContent = stale ? 'node data not current, refreshing' : '';
    note.style.color = stale ? '#c9a227' : '#6b7480';
  }
  if(s.stats && s.stats.ok && !s.stats_stale) renderNodeStats(s.stats);
  // Paint cached links only until the richer AMI view arrives — that one has
  // direct/indirect grouping and disconnect buttons, so it must win.
  if(!_liveLinks && !s.links_stale &&
     ((s.links_direct && s.links_direct.length) || (s.links_indirect && s.links_indirect.length))){
    renderLinks(s.links_direct, s.links_indirect, true);
  }
}

function renderLinks(direct, indirect, cached){
  const ul = $('links'); ul.innerHTML='';
  direct = direct || []; indirect = indirect || [];
  if(!direct.length && !indirect.length){ ul.innerHTML='<li class="none">none connected</li>'; return; }
  const tag = cached ? ' <small>cached</small>' : '';
  // Direct links — these you can disconnect (ilink 1 on your own link).
  if(direct.length){
    const lbl=document.createElement('li'); lbl.className='grouplbl';
    lbl.textContent='Your links'; lbl.style.cssText='border:0;justify-content:flex-start';
    ul.appendChild(lbl);
  }
  for(const d of direct){
    const li=document.createElement('li');
    // CONNECTING is not connected — call it out rather than showing it in
    // the same grey as an established link, which read as "it worked".
    const est=(d.state||'').toUpperCase()==='ESTABLISHED';
    const sc=est?'':'color:var(--t-warn)';
    const sl=est?'':' — not answering';
    li.innerHTML=`<span>${statsLink(d.node)}<small style="${sc}">${(d.dir||'').toLowerCase()} ${(d.state||'').toLowerCase()}${sl}</small>${tag}</span>`;
    const b=document.createElement('button'); b.className='b-dis'; b.textContent='Disconnect';
    b.onclick=()=>dis(d.node); li.appendChild(b); ul.appendChild(li);
  }
  // Conference members reached through a hub — read-only (not your links).
  if(indirect.length){
    const lbl=document.createElement('li'); lbl.className='grouplbl';
    lbl.textContent='In conference (via a hub)'; lbl.style.cssText='border:0;justify-content:flex-start';
    ul.appendChild(lbl);
    for(const n of indirect){
      const li=document.createElement('li'); li.className='indirect';
      li.innerHTML=`<span>${statsLink(n)}</span>`;
      ul.appendChild(li);
    }
  }
}
async function refreshLinks(){
  const res = await post('links');
  if(res && res.ok) _liveLinks = true;
  renderLinks(res && res.direct, res && res.indirect, false);
}

async function poll(){
  try{
    const s = await (await fetch('/usrp/status')).json();
    $('rx').innerHTML = pill(s.rx_keyed);
    $('tx').innerHTML = pill(s.tx_keyed);
    $('pk').textContent = s.pkts_rx + ' / ' + s.pkts_tx;
    $('q').textContent = s.rx_queue;
    $('pps').textContent = s.pps_rx != null ? s.pps_rx + ' pps' : '—';
    $('loss').textContent = s.loss_pct != null ? s.loss_pct + '% loss (' + s.pkts_dropped + ' dropped)' : '—';
    $('loss').style.color = (s.loss_pct > 5) ? 'var(--t-err)' : (s.loss_pct > 1) ? 'var(--t-warn)' : '';
    $('amiline').textContent = 'via USRP bridge — AMI ' +
        (s.ami_ready ? ('ready ('+s.ami+')') : 'NOT configured');
    if(s.links_age!=null) $('age').textContent = 'updated '+s.links_age+'s ago';
    applyCache(s);
    renderBook(s.book);
    renderRelink(s.relink, s.relink_counters);
  }catch(e){}
}
// The saved-node list and the dropdown are the same data. Both rebuild only
// when the signature changes — repainting every 1.5 s would close the select
// under the user's finger and fight anything typed into the name field.
function esc(t){ const d=document.createElement('div'); d.textContent=t==null?'':t; return d.innerHTML; }
function label(e){ return e.name ? e.node+' — '+e.name : e.node; }
// Node number as a link to its AllStarLink stats page. rel=noopener because
// target=_blank otherwise hands the opened page a handle back to this one.
const STATS_URL='https://stats.allstarlink.org/stats/';
function statsLink(node){
  const n=String(node);
  return '<a class="nodenum" href="'+STATS_URL+encodeURIComponent(n)+
         '" target="_blank" rel="noopener noreferrer"'+
         ' title="AllStarLink stats for node '+esc(n)+'">'+esc(n)+'</a>';
}
function renderBook(book){
  if(!Array.isArray(book)) return;
  const sig = book.map(e=>e.node+':'+e.name).join(',');
  const sel=$('book'), list=$('booklist');
  if(sel.dataset.sig===sig) return;
  sel.dataset.sig=sig;
  sel.innerHTML='<option value="">saved ▾</option>'+
    book.map(e=>'<option value="'+esc(e.node)+'">'+esc(label(e))+'</option>').join('');
  // Buttons carry the node in a data attribute and are handled by the
  // delegated listener below. Inline onclick with a quoted argument does not
  // survive this file: _PANEL_HTML is a non-raw Python triple-quoted string,
  // so \' is consumed by Python and the emitted JS fails to parse — taking
  // the whole panel with it, buttons that never worked and no error visible
  // anywhere but the browser console.
  list.innerHTML = book.length
    ? book.map(e=>'<li class="arow">'+statsLink(e.node)+
        '<span class="nm">'+esc(e.name||'—')+'</span><span class="acts">'+
        '<button class="b-mon" data-act="pick" data-node="'+esc(e.node)+'">Use</button>'+
        '<button class="b-all" data-act="forget" data-node="'+esc(e.node)+'">Forget</button>'+
        '</span></li>').join('')
    : '<li><span class="sub">nothing saved yet — connect to a node, then name it</span></li>';
}
function renderRelink(rows, counters){
  const card=$('relink-card');
  if(!Array.isArray(rows) || !rows.length){ card.style.display='none'; return; }
  card.style.display='';
  $('relink-counters').textContent = counters
    ? counters.attempts+' attempts · '+counters.recovered+' relinked'+
      (counters.dormant ? ' · '+counters.dormant+' slow-retrying' : '')
    : '';
  $('relinklist').innerHTML = rows.map(r=>{
    const slow = r.state==='dormant';
    const col = slow ? 'var(--t-err)' : r.state==='retrying' ? 'var(--t-warn)' : '';
    const what = slow ? 'not answering after '+r.fails+' tries — slow retry, still kept'
               : r.state==='retrying' ? 'retrying ('+r.fails+' failed)'
               : 'connected';
    return '<li class="arow">'+statsLink(r.node)+
           '<span class="nm">'+esc(r.name||'—')+
           '<small style="color:'+col+'">'+esc(what)+
           (r.error?' · '+esc(r.error):'')+'</small></span>'+
           '<span class="acts">'+
           '<button class="b-all" data-act="stop" data-node="'+esc(r.node)+'">Stop</button>'+
           '</span></li>';
  }).join('');
}
function pick(n){ $('node').value=n; $('nodename').value=''; }
async function forget(n){
  const r = await post('book_delete',{node:n});
  $('msg').textContent = (r.ok?'✓ forgot ':'✗ ')+n;
  if(r.book) { $('book').dataset.sig=''; renderBook(r.book); }
}
// One delegated listener for every row button in both lists.
document.addEventListener('click', function(ev){
  const b = ev.target.closest('button[data-act]');
  if(!b) return;
  const n = b.dataset.node;
  if(b.dataset.act==='pick') pick(n);
  else if(b.dataset.act==='forget') forget(n);
  else if(b.dataset.act==='stop') dis(n);
});
async function saveName(){
  const node=$('node').value.trim(), name=$('nodename').value.trim();
  if(!/^[0-9]+$/.test(node)){ $('msg').textContent='enter a node number first'; return; }
  const r = await post('book_set',{node, name});
  $('msg').textContent = r.ok ? ('✓ saved '+node+(name?' as "'+name+'"':' (name cleared)')) : ('✗ '+(r.error||''));
  if(r.book) { $('book').dataset.sig=''; renderBook(r.book); }
}
function renderNodeStats(r){
  if(!r || !r.ok) return;
  $('nd-uptime').textContent   = r.uptime || '—';
  $('nd-keyups').textContent   = r.keyups_today || '—';
  $('nd-txtime').textContent   = r.tx_time_today || '—';
  $('nd-timeouts').textContent = r.timeouts || '0';
}
async function pollNodeStats(){
  try{
    renderNodeStats(await post('node_stats'));
  }catch(e){}
  try{
    const r = await post('links');
    if(r.ok && Array.isArray(r.direct) && r.direct.length){
      $('nd-links-stats').innerHTML = '<div style="font-size:.75rem;color:#888;margin-top:.25rem">Direct link reconnects</div>' +
        r.direct.map(d => {
          const warn = d.reconnects > 5 ? 'color:var(--t-err)' : d.reconnects > 0 ? 'color:var(--t-warn)' : '';
          return `<div class="stat" style="margin-top:.15rem"><span>Node ${statsLink(d.node)} (${d.peer})</span><b style="${warn}">${d.reconnects} reconnects — ${d.state}</b></div>`;
        }).join('');
    } else {
      $('nd-links-stats').innerHTML = '';
    }
  }catch(e){}
}
// Paint from the cached snapshot FIRST — /usrp/status is a cheap local read,
// so the panel is populated immediately. The AMI-backed refreshes still run,
// but after the page is already usable rather than blocking it for ~9 s.
// poll() paints from the warm cache on its first pass (cheap, no AMI), so the
// panel is populated before the AMI-backed calls below have returned.
poll();
refreshLinks(); pollNodeStats();
setInterval(poll, 1500);
setInterval(refreshLinks, 6000);
setInterval(pollNodeStats, 15000);
</script>
</body></html>"""

# Bus side
BUS_RATE = 48000
USRP_RATE = 8000
RATE_RATIO = BUS_RATE // USRP_RATE     # 6
BUS_CHUNK_SAMPLES = 2400               # 50 ms @ 48 kHz
BUS_CHUNK_BYTES = BUS_CHUNK_SAMPLES * 2

# Tuning
RX_QUEUE_MAX = 6                       # ~300 ms of 48 kHz chunks before dropping oldest
RX_RESAMPLE_PAD = 32                   # 8 kHz input samples of filter context carried
TX_RESAMPLE_PAD = 32 * RATE_RATIO      # same context, expressed in 48 kHz input samples
                                       # across frames so the upsampler doesn't click
                                       # at every 20 ms boundary (~8 ms added latency)
TX_BUF_MAX_8K = USRP_RATE             # cap TX backlog at ~1 s of 8 kHz audio
TX_HANG_S = 0.20                       # auto-unkey if no put_audio for this long


class UsrpPlugin:
    PLUGIN_ID = 'usrp'
    PLUGIN_NAME = 'AllStar 1 (USRP)'   # numbered to match /routing and the nav
    CAPABILITIES = {
        CAPABILITY_AUDIO_RX,
        CAPABILITY_AUDIO_TX,
        CAPABILITY_PTT,
        CAPABILITY_STATUS,
    }

    def __init__(self):
        # `name` is required: the bus routing system keys slots, level
        # metering, and active-source tracking on source.name (AudioSource
        # provides it; plugins must too, or sync_listen_bus throws).
        self.name = self.PLUGIN_ID
        self.enabled = False
        self.muted = False
        self.tx_muted = False
        self.volume = 1.0
        self.audio_level = 0
        # Routing/UI metadata read via getattr by bus_manager + the routing UI.
        self.priority = 5            # network source priority (lower = higher)
        self.duck = True             # duck under higher-priority sources on a bus
        self.ptt_control = True     # RX audio does not itself key a TX radio
        self.audio_boost = 1.0       # RX gain (routing slider, 100 = 1.0)
        self.tx_audio_boost = 1.0    # TX gain (independent of RX)
        self.tx_audio_level = 0      # TX meter, updated by the bus on send

        self._config = None
        self._gateway = None

        self.remote_host = '127.0.0.1'
        self.remote_port = 32001       # bridge node's USRP listen port
        self.listen_port = 34001       # we listen here (ASL's first port)

        # AMI control of the bridge node (runtime connect/disconnect)
        self.node = '0'                # our local node number (e.g. 68397)
        self.ami_host = '127.0.0.1'
        self.ami_port = 5038
        self.ami_user = ''
        self.ami_secret = ''
        self._last_ctrl = ''           # last control result text (for the panel)
        self._links_cache = []         # last-known connected nodes (panel display)
        self._links_direct = []        # rich rows: node/peer/dir/state/reconnects
        self._links_indirect = []      # conference members reached via a hub
        self._links_cache_mono = 0.0
        # When `rpt lstats` last SUCCEEDED. The reconciler needs this rather
        # than _poll_step's return: _ami_command reports a dead AMI as
        # (False, msg) instead of raising, so the poll looks successful and
        # would have the reconciler chasing links against a stale list.
        self._lstats_ok_mono = 0.0
        # Node stats (rpt stats) cached alongside links. Both are refreshed by
        # _ami_poll_loop so the panel can paint immediately instead of waiting
        # on three serial AMI round trips at 3 s each.
        self._stats_cache = {}
        self._stats_cache_mono = 0.0
        self._ami_thread = None
        self._poll_err = {}            # what -> {n, last, msg} for rate-limited logging
        # Browser monitor tap. Filled in the RX path, NOT by a second
        # get_audio() call — that pops _rx_queue and would steal audio from the
        # bus. Filling it here also means the tap works even if this node is
        # not wired to any bus.
        self.pcm_tap = False
        self._tap_queue = collections.deque(maxlen=4)
        self._recent = []              # last 10 connected node numbers (dropdown)
        self._recent_path = None       # JSON persistence path (set in setup)
        # Desired link set — what SHOULD be connected on THIS node, plus the
        # reconciler's per-target backoff state. Per-instance (AS1 and AS2
        # hold independent links) even though the address book is shared.
        self._desired = {}             # node -> {'mode': int}
        self._desired_path = None
        self._relink = {}              # node -> {fails,next,pending,state,error}
        self.relink_attempts = 0       # instrumentation: a counter stuck at 0
        self.relink_recovered = 0      # means the reconciler never ran
        self.relink_dormant = 0

        self._sock = None
        self._stop = threading.Event()
        self._rx_thread = None
        self._tx_thread = None

        # RX: 48 kHz accumulator feeding a bounded chunk queue
        self._rx48k = np.zeros(0, dtype=np.int16)
        # 8 kHz input buffer for the continuous resampler, primed with PAD zeros
        # that act as "already-emitted history" so the bookkeeping is uniform
        # from the first frame.
        self._rx_in8k = np.zeros(RX_RESAMPLE_PAD, dtype=np.float32)
        # Same trick on the way out — see put_audio.
        self._tx_in48k = np.zeros(TX_RESAMPLE_PAD, dtype=np.float32)
        self._rx_queue = collections.deque(maxlen=RX_QUEUE_MAX)
        self._rx_queue_primed = False

        # TX: 8 kHz backlog drained by the paced sender thread
        self._tx8k = collections.deque()          # holds np.int16 arrays
        self._tx_lock = threading.Lock()
        self._tx_keyed = False
        self._last_tx_mono = 0.0
        self._sender_keyed = False                # sender thread's view of key state

        # Counters / status
        self._seq = 0
        self.pkts_rx = 0
        self.pkts_tx = 0
        self.pkts_dropped = 0                     # sequence gaps (lost UDP packets)
        self.rx_keyed = False                     # last incoming COS (remote keyup)
        self._last_rx_mono = 0.0
        self._rx_seq_last = None                  # last seen RX seq (None = first pkt)
        # Rolling pps window: list of monotonic timestamps for received voice pkts
        self._pps_window = collections.deque()
        self._PPS_WINDOW_SEC = 5.0

    # ── lifecycle ──────────────────────────────────────────────────
    def setup(self, config, gateway=None):
        # Config arrives as the gateway Config object, not a dict (mirrors
        # the other in-tree plugins, which bail on a dict).
        if isinstance(config, dict):
            return False
        if resample_poly is None:
            print("  [USRP] scipy.signal.resample_poly unavailable — cannot start")
            return False

        self._config = config
        self._gateway = gateway
        self.remote_host = str(getattr(config, 'USRP_REMOTE_HOST', '127.0.0.1'))
        self.remote_port = int(getattr(config, 'USRP_REMOTE_PORT', 32001))
        self.listen_port = int(getattr(config, 'USRP_LISTEN_PORT', 34001))
        # AMI control surface (defaults: AMI on the same host as the USRP socket)
        self.node = str(getattr(config, 'USRP_NODE', '0'))
        self.ami_host = str(getattr(config, 'USRP_AMI_HOST', '')) or self.remote_host
        self.ami_port = int(getattr(config, 'USRP_AMI_PORT', 5038))
        self.ami_user = str(getattr(config, 'USRP_AMI_USER', ''))
        self.ami_secret = str(getattr(config, 'USRP_AMI_SECRET', ''))
        self._recent_path = os.path.join(_gw_root(), 'usrp_recent.json')
        self._recent = self._load_recent()
        self._desired_path = os.path.join(_gw_root(), 'usrp_desired.json')
        self._desired = self._load_desired()

        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(('', self.listen_port))
            self._sock.settimeout(0.5)
        except OSError as e:
            print(f"  [USRP] bind on :{self.listen_port} failed: {e}")
            return False

        self._stop.clear()
        self._rx_thread = threading.Thread(target=self._rx_loop, name='usrp-rx', daemon=True)
        self._tx_thread = threading.Thread(target=self._tx_loop, name='usrp-tx', daemon=True)
        self._rx_thread.start()
        self._tx_thread.start()
        self._ami_thread = threading.Thread(target=self._ami_poll_loop,
                                            name=f'{self.PLUGIN_ID}-ami', daemon=True)
        self._ami_thread.start()
        self.enabled = True
        print(f"  [USRP] up: listen :{self.listen_port}  →  "
              f"{self.remote_host}:{self.remote_port}")
        return True

    def cleanup(self):
        self._stop.set()
        for t in (self._rx_thread, self._tx_thread, self._ami_thread):
            if t and t.is_alive():
                t.join(timeout=1.5)
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self.enabled = False

    # ── RX path: USRP UDP → 48 kHz bus chunks ──────────────────────
    def _rx_loop(self):
        while not self._stop.is_set():
            try:
                data, _ = self._sock.recvfrom(2048)
            except socket.timeout:
                # No traffic: decay COS and level so status reflects an idle link.
                if self.rx_keyed and (time.monotonic() - self._last_rx_mono) > 0.5:
                    self.rx_keyed = False
                self.audio_level = max(0, int(self.audio_level * 0.7))
                continue
            except OSError:
                break

            if len(data) < USRP_HEADER_LEN or data[:4] != b'USRP':
                continue
            _, seq, _mem, keyup, _tg, ptype, _mpx, _rsv = USRP_HEADER.unpack(
                data[:USRP_HEADER_LEN])
            self.pkts_rx += 1
            self.rx_keyed = bool(keyup)
            now = time.monotonic()
            self._last_rx_mono = now

            # Sequence-gap loss detection (32-bit wraparound safe)
            if self._rx_seq_last is not None:
                gap = (seq - self._rx_seq_last - 1) & 0xFFFFFFFF
                if 0 < gap < 1000:            # ignore huge jumps (reconnects)
                    self.pkts_dropped += gap
            self._rx_seq_last = seq

            if ptype != USRP_TYPE_VOICE:
                continue  # ignore text/ping for now

            # Rolling pps window (voice packets only)
            self._pps_window.append(now)
            cutoff = now - self._PPS_WINDOW_SEC
            while self._pps_window and self._pps_window[0] < cutoff:
                self._pps_window.popleft()
            payload = data[USRP_HEADER_LEN:USRP_HEADER_LEN + USRP_VOICE_BYTES]
            if len(payload) < 2:
                continue
            s8k = np.frombuffer(payload, dtype='<i2')
            self._feed_rx(s8k)

    def _feed_rx(self, s8k):
        """Continuous (click-free) upsample 8 kHz -> 48 kHz, emitting bus chunks.

        resample_poly is stateless, so resampling each 20 ms UDP frame on its own
        leaves the anti-alias FIR's edge transients at every frame boundary —
        audible crackle. Instead we keep PAD input samples of context on each
        side: the leading PAD is already-emitted history, and we hold back the
        trailing PAD (which has no look-ahead yet) until the next frame. Both
        edges of every emitted block then sit on real filter context.
        """
        PAD = RX_RESAMPLE_PAD
        self._rx_in8k = np.concatenate((self._rx_in8k, s8k.astype(np.float32)))
        n = len(self._rx_in8k)
        if n < 3 * PAD:
            return                              # not enough context yet
        y = resample_poly(self._rx_in8k, RATE_RATIO, 1)
        # Skip the leading PAD (history, already emitted) and trailing PAD
        # (held back for next call's look-ahead). Steady state: 160 in -> 960 out.
        out = y[PAD * RATE_RATIO:(n - PAD) * RATE_RATIO]
        self._rx_in8k = self._rx_in8k[-2 * PAD:]    # PAD history + PAD held-back
        up = np.clip(out, -32768, 32767).astype(np.int16)
        self._rx48k = np.concatenate((self._rx48k, up))
        while len(self._rx48k) >= BUS_CHUNK_SAMPLES:
            chunk = self._rx48k[:BUS_CHUNK_SAMPLES]
            self._rx48k = self._rx48k[BUS_CHUNK_SAMPLES:]
            chunk_bytes = chunk.tobytes()
            self._rx_queue.append(chunk_bytes)  # deque drops oldest at maxlen
            if self.pcm_tap:
                self._tap_queue.append(chunk_bytes)
            self.audio_level = pcm_level(chunk_bytes, self.audio_level)

    def take_tap(self):
        """One tapped RX chunk for the browser PCM monitor, or None.

        Separate from get_audio() so monitoring never competes with the bus for
        the same queue.
        """
        try:
            return self._tap_queue.popleft()
        except IndexError:
            return None

    def get_audio(self, chunk_size=None):
        """Return one 48 kHz bus chunk of RX audio, or (None, False)."""
        if not self.enabled or self.muted:
            return None, False

        # First consumer read flushes anything that piled up before the bus
        # started ticking (house pattern, mirrors TH-9800).
        if not self._rx_queue_primed:
            self._rx_queue_primed = True
            while len(self._rx_queue) > 1:
                self._rx_queue.popleft()

        try:
            data = self._rx_queue.popleft()
        except IndexError:
            return None, False

        return data, True

    # ── TX path: 48 kHz bus chunks → USRP UDP ──────────────────────
    def put_audio(self, pcm):
        """Accept one 48 kHz bus chunk, downsample to 8 kHz, queue for send."""
        if not self.enabled or self.tx_muted or not pcm:
            return
        s48 = np.frombuffer(pcm, dtype='<i2')
        if s48.size == 0:
            return
        f48 = s48.astype(np.float32)
        if self.tx_audio_boost != 1.0:
            f48 = f48 * self.tx_audio_boost

        # Continuous (click-free) downsample, mirroring _feed_rx. resample_poly
        # is stateless, so downsampling each 50 ms bus chunk on its own left the
        # anti-alias FIR's edge transients at every chunk boundary — 20 clicks a
        # second, heard as "sparkles" on the AllStar side while the same audio
        # was clean at the PCM sink. RX was written this way from the start; TX
        # was not. Keep PAD input samples of context each side: the leading PAD
        # is already-emitted history, the trailing PAD is held back until the
        # next chunk supplies its look-ahead.
        PAD = TX_RESAMPLE_PAD
        self._tx_in48k = np.concatenate((self._tx_in48k, f48))
        n = len(self._tx_in48k)
        self._tx_keyed = True
        self._last_tx_mono = time.monotonic()
        try:
            self.tx_audio_level = pcm_level(pcm, self.tx_audio_level)
        except Exception:
            log_exc()
        if n < 3 * PAD:
            return                              # not enough context yet
        y = resample_poly(self._tx_in48k, 1, RATE_RATIO)
        edge = PAD // RATE_RATIO                # PAD in OUTPUT (8 kHz) samples
        out = y[edge:len(y) - edge]
        self._tx_in48k = self._tx_in48k[-2 * PAD:]   # PAD history + PAD held back
        down = np.clip(out, -32768, 32767).astype(np.int16)
        with self._tx_lock:
            self._tx8k.append(down)
            # Bound backlog so a stalled link can't build unbounded latency.
            total = sum(a.size for a in self._tx8k)
            while total > TX_BUF_MAX_8K and len(self._tx8k) > 1:
                total -= self._tx8k.popleft().size

    def on_ptt_change(self, state):
        """Bus tells us when this sink is keyed. Promptly key/unkey USRP."""
        self._tx_keyed = bool(state)
        if state:
            self._last_tx_mono = time.monotonic()

    def _pull_tx_frame(self):
        """Pull exactly 160 samples of 8 kHz audio, zero-padding on underrun."""
        out = np.zeros(USRP_VOICE_SAMPLES, dtype=np.int16)
        filled = 0
        with self._tx_lock:
            while filled < USRP_VOICE_SAMPLES and self._tx8k:
                head = self._tx8k[0]
                take = min(USRP_VOICE_SAMPLES - filled, head.size)
                out[filled:filled + take] = head[:take]
                if take == head.size:
                    self._tx8k.popleft()
                else:
                    self._tx8k[0] = head[take:]
                filled += take
        return out

    def _send_frame(self, samples_i16, keyup):
        hdr = USRP_HEADER.pack(b'USRP', self._seq & 0xFFFFFFFF, 0,
                               1 if keyup else 0, 0, USRP_TYPE_VOICE, 0, 0)
        self._seq += 1
        try:
            self._sock.sendto(hdr + samples_i16.astype('<i2').tobytes(),
                              (self.remote_host, self.remote_port))
            self.pkts_tx += 1
        except OSError:
            pass

    def _tx_loop(self):
        """Paced 20 ms sender. Emits voice frames while keyed; sends a single
        keyup=0 trailer on unkey so the node drops the link cleanly."""
        next_t = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            # Effective key state: explicit key OR recent put_audio, with hang.
            keyed = self._tx_keyed and (now - self._last_tx_mono) < TX_HANG_S

            if keyed:
                self._sender_keyed = True
                self._send_frame(self._pull_tx_frame(), keyup=True)
            else:
                if self._sender_keyed:
                    # Falling edge: flush whatever's left, then an unkey trailer.
                    self._sender_keyed = False
                    self._tx_keyed = False
                    with self._tx_lock:
                        self._tx8k.clear()
                    # Drop stale filter context, otherwise the next key-up
                    # starts by emitting the tail of the previous transmission.
                    self._tx_in48k = np.zeros(TX_RESAMPLE_PAD, dtype=np.float32)
                    self._send_frame(np.zeros(USRP_VOICE_SAMPLES, dtype=np.int16),
                                     keyup=False)
                # Decay the TX meter when idle. put_audio sets tx_audio_level on
                # each chunk, but nothing else decays it for an external plugin
                # (the bus only decays the built-in radios), so without this the
                # panel/shell TX bar freezes at the last level. Mirrors the RX
                # decay in get_audio.
                if self.tx_audio_level:
                    self.tx_audio_level = max(0, int(self.tx_audio_level * 0.8))

            next_t += USRP_FRAME_MS
            sleep = next_t - time.monotonic()
            if sleep > 0:
                time.sleep(sleep)
            else:
                next_t = time.monotonic()  # we fell behind; resync cadence

    # ── AMI control of the bridge node ─────────────────────────────
    # All AMI I/O is short-lived (connect → login → command → logoff) with a
    # hard timeout, and is only ever called from control handlers — never from
    # the audio RX/TX threads — so a wedged node socket can't stall audio.
    def _ami_command(self, command, timeout=3.0):
        """Run one Asterisk CLI command over AMI. Returns (ok, output_text)."""
        if not self.ami_user:
            return False, 'AMI not configured (set USRP_AMI_USER/SECRET)'
        sock = None
        try:
            sock = socket.create_connection((self.ami_host, self.ami_port), timeout)
            sock.settimeout(timeout)
            # Send Login immediately; the greeting banner and the login
            # response then arrive together and end in a blank line.
            login = (f'Action: Login\r\nUsername: {self.ami_user}\r\n'
                     f'Secret: {self.ami_secret}\r\n\r\n')
            sock.sendall(login.encode())
            buf = self._ami_drain(sock, timeout)
            if 'Success' not in buf:
                return False, f'AMI login failed: {buf.strip()[:200]}'
            sock.sendall((f'Action: Command\r\nCommand: {command}\r\n\r\n').encode())
            out = self._ami_drain(sock, timeout, until='--END COMMAND--')
            try:
                sock.sendall(b'Action: Logoff\r\n\r\n')
            except OSError:
                pass
            # AMI wraps each CLI output line as "Output: <line>". Strip that
            # envelope so downstream line parsers (e.g. rpt lstats) see the raw
            # command output, not "Output:" as the first token of every line.
            olines = [l[7:].lstrip(' ') for l in out.splitlines()
                      if l.startswith('Output:')]
            return True, ('\n'.join(olines) if olines else out)
        except (OSError, socket.timeout) as e:
            return False, f'AMI error: {e}'
        finally:
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass

    @staticmethod
    def _ami_drain(sock, timeout, until=None):
        """Read AMI output until an idle gap, a blank-line terminator, or
        ``until`` marker. Bounded by the socket timeout."""
        chunks = []
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data = sock.recv(4096)
            except socket.timeout:
                break
            if not data:
                break
            chunks.append(data.decode(errors='replace'))
            joined = ''.join(chunks)
            if until and until in joined:
                break
            if until is None and joined.endswith('\r\n\r\n'):
                break
        return ''.join(chunks)

    def _load_recent(self):
        """Load the recent-nodes list from disk (best-effort)."""
        try:
            with open(self._recent_path) as f:
                data = json.load(f)
            if isinstance(data, list):
                return [str(x) for x in data if str(x).isdigit()][:10]
        except (OSError, ValueError):
            pass
        return []

    def _save_recent(self):
        if not self._recent_path:
            return
        try:
            from atomic_json import save_json
            save_json(self._recent_path, self._recent, indent=None)
        except OSError:
            pass

    def _remember(self, node):
        """Push a node to the front of the recent list (dedup, cap 10, persist)."""
        node = str(node).strip()
        if not node.isdigit():
            return
        if node in self._recent:
            self._recent.remove(node)
        self._recent.insert(0, node)
        del self._recent[10:]
        self._save_recent()
        NODE_BOOK.touch(node)          # shared across AS1/AS2

    # ── desired link set + reconciler ───────────────────────────────────
    def _load_desired(self):
        try:
            with open(self._desired_path) as f:
                data = json.load(f)
            out = {}
            for node, ent in (data or {}).items():
                if str(node).isdigit():
                    mode = int((ent or {}).get('mode', ILINK_TRANSCEIVE))
                    out[str(node)] = {'mode': mode if mode in _CONNECT_MODES
                                      else ILINK_TRANSCEIVE}
            return out
        except (OSError, ValueError, AttributeError, TypeError):
            return {}

    def _save_desired(self):
        if not self._desired_path:
            return
        try:
            from atomic_json import save_json
            save_json(self._desired_path, self._desired, indent=1)
        except (OSError, ImportError):
            pass

    def _relink_state(self, node):
        return self._relink.setdefault(str(node), {
            'fails': 0, 'next': 0.0, 'pending': False,
            'state': 'ok', 'error': '', 'last_try': 0.0,
        })

    def _reconcile_links(self):
        """Re-establish desired links that have gone away, with backoff.

        Runs once per AMI poll, immediately after _links_direct was refreshed,
        so it reconciles against evidence rather than against what we believe
        we asked for.

        Two rules matter more than the mechanism:

        * An attempt is verified on a LATER poll, never by the AMI command's
          own return. `rpt cmd ... ilink` returns ok because Asterisk accepted
          the command, not because the link came up — trusting that is how
          remediation reports success while nothing actually reconnects.
        * Retries back off exponentially and then SLOW DOWN — they never stop.
          A node off the air for a week must not be dialled every 10 seconds,
          but abandoning it outright would break the promise that a link you
          asked for comes back: after RELINK_MAX_FAILS the target goes dormant,
          is said out loud once, and is retried hourly for as long as it stays
          in the desired set. Only Disconnect removes it.
        """
        if not self._desired or not self.ami_user:
            return
        now = time.monotonic()
        # Require a link list read successfully within the last couple of
        # polls. Without this an AMI outage looks like "every link is down"
        # and would burn the whole retry budget on the wrong problem.
        if not self._lstats_ok_mono or now - self._lstats_ok_mono > self.AMI_POLL_SEC * 2.5:
            return
        # ESTABLISHED only. A link stuck in CONNECTING is present in lstats but
        # is NOT connected — counting mere presence made a node that dials
        # forever and never answers read as healthy (state ok, 0 fails), which
        # is precisely the case that needs reporting.
        actual = {str(r.get('node')) for r in (self._links_direct or [])
                  if str(r.get('state', '')).upper() == 'ESTABLISHED'}

        for node, want in list(self._desired.items()):
            st = self._relink_state(node)

            if node in actual:
                if st['state'] != 'ok' or st['fails']:
                    self.relink_recovered += 1
                    print(f"  [{self.PLUGIN_ID}] node {node} relinked "
                          f"(after {st['fails']} failed attempt(s))")
                st.update(fails=0, next=0.0, pending=False, state='ok', error='')
                continue

            if now < st['next']:
                continue

            # The window for the previous attempt has elapsed and the link is
            # still absent — that attempt failed.
            if st['pending']:
                st['pending'] = False
                st['fails'] += 1
                if (st['fails'] >= self.RELINK_MAX_FAILS
                        and st['state'] != 'dormant'):
                    st['state'] = 'dormant'
                    self.relink_dormant += 1
                    print(f"  [{self.PLUGIN_ID}] ⚠ node {node} not answering after "
                          f"{st['fails']} attempts"
                          f"{' — ' + st['error'] if st['error'] else ''}. Backing off "
                          f"to one retry an hour; it stays in the persistent set "
                          f"and will relink by itself when it comes back.")

            if st['state'] == 'dormant':
                delay = self.RELINK_DORMANT_SEC
            else:
                delay = min(self.RELINK_BACKOFF_START * (2 ** st['fails']),
                            self.RELINK_BACKOFF_MAX)
            mode = want.get('mode', ILINK_TRANSCEIVE)
            if mode not in _CONNECT_MODES:
                mode = ILINK_TRANSCEIVE
            self.relink_attempts += 1
            ok, out = self._ami_command(f'rpt cmd {self.node} ilink {mode} {node}')
            st.update(pending=True, last_try=now, next=now + delay,
                      state='dormant' if st['state'] == 'dormant' else 'retrying',
                      error='' if ok else str(out)[:120])
            _where = ('hourly retry' if st['state'] == 'dormant'
                      else f"attempt {st['fails'] + 1}/{self.RELINK_MAX_FAILS}")
            print(f"  [{self.PLUGIN_ID}] node {node} missing — reconnect {_where}"
                  f"{'' if ok else ' (AMI error: ' + str(out)[:80] + ')'}; "
                  f"next check in {int(delay)}s")

    def relink_report(self):
        """Per-target reconciler state for the panel and get_status()."""
        rows = []
        for node in self._desired:
            st = self._relink.get(node, {})
            rows.append({
                'node': node,
                'name': NODE_BOOK.name_of(node),
                'mode': self._desired[node].get('mode', ILINK_TRANSCEIVE),
                'state': st.get('state', 'ok'),
                'fails': st.get('fails', 0),
                'error': st.get('error', ''),
            })
        rows.sort(key=lambda r: r['node'])
        return rows

    def clear_dormant(self, node):
        """A manual connect resets a dormant target to full retry speed."""
        st = self._relink.get(str(node))
        if st:
            st.update(fails=0, next=0.0, pending=False, state='ok', error='')

    def connect_node(self, target, mode=ILINK_TRANSCEIVE, persistent=True):
        """Link the bridge node to ``target``.

        Persistent by default: the target joins the desired set on disk, so the
        link is restored after a gateway restart, an ASL restart, a reboot, or
        any outage at the far end — and stays gone once you disconnect it.
        Pass persistent=False for a one-off link that is not remembered.
        """
        target = str(target).strip()
        if not target.isdigit():
            return {'ok': False, 'error': f'invalid node: {target!r}'}
        wire_mode = mode if mode in _CONNECT_MODES else ILINK_TRANSCEIVE
        self._remember(target)
        if persistent:
            self._desired[target] = {'mode': wire_mode}
            self._save_desired()
            self.clear_dormant(target)      # a manual connect resets backoff
        ok, out = self._ami_command(
            f'rpt cmd {self.node} ilink {wire_mode} {target}')
        if not ok:
            self._last_ctrl = f"connect {target}: {out}"
            return {'ok': False, 'node': target, 'mode': wire_mode,
                    'persistent': persistent, 'state': 'error',
                    'output': out or 'AMI command failed'}

        # The AMI command returning ok means Asterisk ACCEPTED it, not that the
        # link came up — reporting that as success is why a node that silently
        # refuses us looked identical to one that connected. Verify.
        state = self._link_state(target)
        if state == 'ESTABLISHED':
            msg = f'connected to {target}'
        elif state:
            msg = (f'{target}: dialling, no answer yet ({state.lower()}). The far '
                   f'end is reachable but has not accepted — it may be closed to '
                   f'node {self.node}, or off the air. Retrying with backoff.')
        else:
            msg = (f'{target}: no link was created. Check the node number is '
                   f'right and reachable.')
        self._last_ctrl = msg
        return {'ok': ok, 'node': target, 'mode': wire_mode,
                'persistent': persistent, 'state': state or 'none',
                'established': state == 'ESTABLISHED',
                'output': msg}

    def _link_state(self, target, settle=1.5):
        """CONNECT STATE for one link from a fresh lstats, or None if absent.

        `settle` gives the IAX exchange a moment to get past the initial
        CONNECTING before we judge it — without it every connect would report
        "dialling" even when it lands immediately.
        """
        target = str(target)
        if settle:
            self._stop.wait(settle)
        ok, out = self._ami_command(f'rpt lstats {self.node}')
        if not ok:
            return None
        for line in out.splitlines():
            p = line.split()
            if len(p) >= 6 and p[0] == target:
                return p[-1].upper()
        return None

    def disconnect_node(self, target):
        """Drop a link and stop wanting it — the opposite of persistent.

        Order matters: leave the desired set FIRST, or the reconciler races the
        disconnect and puts the link straight back. ilink 11 is issued as well
        to clear any app_rpt permanent flag an older build may have left set;
        without it that flag would keep re-dialling behind our back.
        """
        target = str(target).strip()
        if not target.isdigit():
            return {'ok': False, 'error': f'invalid node: {target!r}'}
        self._desired.pop(target, None)
        self._relink.pop(target, None)
        self._save_desired()
        ok_p, out_p = self._ami_command(
            f'rpt cmd {self.node} ilink {ILINK_PERMA_DISCONNECT} {target}')
        ok, out = self._ami_command(
            f'rpt cmd {self.node} ilink {ILINK_DISCONNECT} {target}')
        self._last_ctrl = f"disconnect {target}: {'ok' if (ok or ok_p) else out}"
        return {'ok': ok or ok_p, 'node': target, 'output': out or out_p}

    def disconnect_all(self):
        """Drop every link and empty the persistent set.

        ilink 6 drops the calls; clearing the desired set is what stops the
        reconciler putting them back. The per-node ilink 11 clears any app_rpt
        permanent flag an older build may have left set, which would otherwise
        keep re-dialling after everything else had let go.
        """
        for target in list(self._desired):
            self._ami_command(
                f'rpt cmd {self.node} ilink {ILINK_PERMA_DISCONNECT} {target}')
        self._desired.clear()
        self._relink.clear()
        self._save_desired()
        ok, out = self._ami_command(
            f'rpt cmd {self.node} ilink {ILINK_DISCONNECT_ALL} 0')
        self._last_ctrl = f"disconnect all: {'ok' if ok else out}"
        return {'ok': ok, 'output': out}

    def link_status(self):
        """Return our DIRECT links (which we can disconnect) and the wider
        conference topology (nodes reachable only *through* a hub we linked to,
        which we cannot disconnect ourselves).

        `rpt lstats <node>` lists only our own links (NODE PEER RECONNECTS
        DIRECTION CONNECT_TIME CONNECT_STATE). `rpt nodes <node>` lists the
        whole connected mesh. The difference = indirect/conference members.
        """
        import re
        # Direct links — these are what `ilink 1 <node>` can actually drop.
        ok_l, out_l = self._ami_command(f'rpt lstats {self.node}')
        direct = []
        if ok_l:
            for line in out_l.splitlines():
                p = line.split()
                if len(p) >= 4 and p[0].isdigit() and p[0] != self.node:
                    # NODE PEER RECONNECTS DIRECTION CONNECT_TIME CONNECT_STATE
                    direct.append({'node': p[0], 'peer': p[1] if len(p) >= 2 else '',
                                   'reconnects': int(p[2]) if len(p) >= 3 and p[2].isdigit() else 0,
                                   'dir': p[3],
                                   'ctime': p[4] if len(p) >= 5 else '',
                                   'state': p[-1] if len(p) >= 6 else ''})

            def _uptime_key(d):
                # Connect time is most-significant-unit-first (…:MM:SS:ms);
                # smallest uptime = most recently connected = top of the list.
                try:
                    return tuple(int(x) for x in d['ctime'].split(':'))
                except ValueError:
                    return (10 ** 9,)
            direct.sort(key=_uptime_key)
        direct_nodes = [d['node'] for d in direct]
        # Full mesh — everyone reachable; subtract our direct links to get the
        # conference-only (indirect) members shown read-only in the UI.
        ok_n, out_n = self._ami_command(f'rpt nodes {self.node}')
        indirect = []
        if ok_n:
            for m in re.findall(r'(?<!\d)\d{4,7}(?!\d)', out_n):
                if m != self.node and m not in direct_nodes and m not in indirect:
                    indirect.append(m)
            indirect.sort(key=int)   # tidy numeric order for the read-only list
        if ok_l or ok_n:
            # Cache direct AND indirect. Caching only direct_nodes meant the
            # "Your links" group painted instantly from cache while "In
            # conference" waited on the live AMI round trip — the panel filled
            # in two visible stages.
            self._links_cache = direct_nodes
            self._links_direct = direct
            self._links_indirect = indirect
            self._links_cache_mono = time.monotonic()
        if ok_l:
            self._lstats_ok_mono = time.monotonic()
        return {'ok': ok_l or ok_n, 'direct': direct, 'indirect': indirect,
                'nodes': direct_nodes, 'raw': out_n}

    def node_stats(self):
        """Pull rpt stats for this node — keyups, TX time, uptime."""
        import re as _re
        ok, out = self._ami_command(f'rpt stats {self.node}')
        if not ok:
            return {'ok': False, 'error': out}
        # (cached below once parsed)
        def _val(key):
            m = _re.search(r'%s\.+:\s*(.+)' % _re.escape(key), out)
            return m.group(1).strip() if m else ''
        stats = {
            'ok': True,
            'uptime':        _val('Uptime'),
            'keyups_today':  _val('Keyups today'),
            'keyups_total':  _val('Keyups since system initialization'),
            'tx_time_today': _val('TX time today'),
            'tx_time_total': _val('TX time since system initialization'),
            'timeouts':      _val('Time outs since system initialization'),
            'kerchunks_today': _val('Kerchunks today'),
        }
        self._stats_cache = stats
        self._stats_cache_mono = time.monotonic()
        return stats

    # ── control surface ────────────────────────────────────────────
    def execute(self, cmd):
        if not isinstance(cmd, dict):
            return {'ok': False, 'error': 'cmd must be a dict'}
        c = cmd.get('cmd')
        if c == 'status':
            return {'ok': True, **self.get_status()}
        if c == 'ptt':
            self.on_ptt_change(bool(cmd.get('state')))
            return {'ok': True, 'tx_keyed': self._tx_keyed}
        if c == 'mute':
            self.muted = bool(cmd.get('state'))
            return {'ok': True, 'muted': self.muted}
        if c == 'connect':
            mode = ILINK_MONITOR if cmd.get('mode') == 'monitor' else ILINK_TRANSCEIVE
            return {'ok': True, **self.connect_node(cmd.get('node'), mode)}
        if c == 'disconnect':
            return {'ok': True, **self.disconnect_node(cmd.get('node'))}
        if c == 'disconnect_all':
            return {'ok': True, **self.disconnect_all()}
        if c == 'links':
            return {'ok': True, **self.link_status()}
        return {'ok': False, 'error': f'unknown cmd: {c}'}

    # ── background AMI poller ──────────────────────────────────────
    # Ages are reported so the UI can distinguish "current" from "last known".
    # Reconciler pacing. Backoff doubles from START to MAX for the first
    # MAX_FAILS attempts (~2.5 h), then the target goes DORMANT and is retried
    # once an hour indefinitely — never abandoned. Dormant is the honest
    # middle ground: it stops the hammering and says so, but a node that comes
    # back next week (or next month) is still picked up without anyone having
    # to remember to click Connect.
    RELINK_BACKOFF_START = 30.0
    RELINK_BACKOFF_MAX = 1800.0
    RELINK_MAX_FAILS = 10
    RELINK_DORMANT_SEC = 3600.0

    AMI_POLL_SEC = 10.0        # how often to refresh links + node stats
    AMI_STALE_SEC = 45.0       # beyond this the cache is not presented as fact
    AMI_ERR_LOG_SEC = 60.0     # min seconds between repeats of the same poll error

    def _ami_poll_loop(self):
        """Keep links + node stats warm so the panel paints instantly.

        Without this the page made three serial AMI calls on load (rpt lstats,
        rpt nodes, rpt stats), each bounded at 3 s — up to ~9 s of staring at an
        empty panel. Polling here is bounded by the same timeouts and runs on
        its own thread, so a wedged AMI never blocks a page load.
        """
        while not self._stop.is_set():
            if self.ami_user:
                links_ok = self._poll_step('links', self.link_status)
                self._poll_step('stats', self.node_stats)    # -> _stats_cache
                # Only reconcile against a link list we actually just read. A
                # failed poll leaves _links_direct stale, and reconciling
                # against stale data would either miss a drop or "reconnect"
                # links that are already up.
                if links_ok:
                    try:
                        self._reconcile_links()
                    except Exception as e:
                        print(f"  [{self.PLUGIN_ID}] reconcile failed: {e}")
            self._stop.wait(self.AMI_POLL_SEC)

    def _poll_step(self, what, fn):
        """Run one poll, logging failures at a bounded rate.

        An unconditional print here writes a line every AMI_POLL_SEC per plugin
        instance — ~12k lines a day per node during a sustained outage, which
        buries everything else in the journal. So: log the first failure
        immediately (you want to know), then at most once a minute while it
        persists, with a count of what was suppressed, and one line when it
        recovers. Silence is not an option either — a poll that fails quietly
        just looks like a slow panel.
        """
        try:
            fn()
        except Exception as e:
            st = self._poll_err.setdefault(what, {'n': 0, 'last': 0.0, 'msg': ''})
            st['n'] += 1
            st['msg'] = str(e)
            now = time.monotonic()
            # Gate on when we last REPORTED, not on the failure count. Testing
            # `n == 1` and then resetting n to 0 made every failure look like
            # the first one, so nothing was suppressed at all.
            never_reported = st['last'] == 0.0
            if never_reported or now - st['last'] >= self.AMI_ERR_LOG_SEC:
                extra = f" (x{st['n']} since last report)" if st['n'] > 1 else ''
                print(f"  [{self.PLUGIN_ID}] {what} poll failed: {e}{extra}")
                st['last'] = now
                st['n'] = 0
            return False
        if self._poll_err.pop(what, None) is not None:
            print(f"  [{self.PLUGIN_ID}] {what} poll recovered")
        return True

    def _cache_age(self, mono):
        return round(time.monotonic() - mono, 1) if mono else None

    def get_status(self):
        with self._tx_lock:
            tx_buf = sum(a.size for a in self._tx8k)
        return {
            'plugin': self.PLUGIN_ID,
            'enabled': self.enabled,
            'remote': f'{self.remote_host}:{self.remote_port}',
            'listen_port': self.listen_port,
            'tx_keyed': self._sender_keyed,
            'rx_keyed': self.rx_keyed,          # remote COS (someone talking on AllStar)
            'rx_queue': len(self._rx_queue),
            'tx_buf_8k': tx_buf,
            'pkts_rx': self.pkts_rx,
            'pkts_tx': self.pkts_tx,
            'pkts_dropped': self.pkts_dropped,
            'pps_rx': round(len(self._pps_window) / self._PPS_WINDOW_SEC, 1),
            'loss_pct': round(self.pkts_dropped * 100 / max(1, self.pkts_rx + self.pkts_dropped), 1),
            'audio_level': self.audio_level,
            'node': self.node,
            'ami': f'{self.ami_host}:{self.ami_port}',
            'ami_ready': bool(self.ami_user),
            'last_ctrl': self._last_ctrl,
            'pcm_tap': self.pcm_tap,
            'links': list(self._links_cache),
            'links_direct': list(self._links_direct),
            'links_indirect': list(self._links_indirect),
            'links_age': self._cache_age(self._links_cache_mono),
            'stats': dict(self._stats_cache),
            'stats_age': self._cache_age(self._stats_cache_mono),
            # The UI must never present a warm cache as live. True means "this
            # is the last known value, not the current one".
            'links_stale': (self._cache_age(self._links_cache_mono) is None
                            or self._cache_age(self._links_cache_mono) > self.AMI_STALE_SEC),
            'stats_stale': (self._cache_age(self._stats_cache_mono) is None
                            or self._cache_age(self._stats_cache_mono) > self.AMI_STALE_SEC),
            'recent': list(self._recent),
            # Shared address book (both panels) + this node's desired links.
            'book': NODE_BOOK.all(),
            'relink': self.relink_report(),
            'relink_counters': {
                'attempts': self.relink_attempts,
                'recovered': self.relink_recovered,
                'dormant': self.relink_dormant,
            },
        }

    # ── web control panel (web_routes hook) ────────────────────────
    def web_routes(self):
        """Routes registered on the gateway HTTP server. Handler signature is
        ``handler(request_handler, parent)``; closures keep the plugin ref."""
        return [
            ('/usrp', self._http_panel),
            ('/usrp/status', self._http_status),
            ('/usrp/control', self._http_control),
            ('/usrp/nodes', self._http_nodes),
        ]

    def _http_nodes(self, req, parent):
        """GET /usrp/nodes — list all loaded USRP plugin instances.

        Returns [{id, node, name, status_url, control_url, panel_url}] so the
        dashboard can discover instances without hardcoding their IDs.
        """
        gw = parent.gateway if parent else None
        ext = getattr(gw, '_external_plugins', {}) if gw else {}
        nodes = []
        for pid, plg in sorted(ext.items()):
            if not pid.startswith('usrp'):
                continue
            base = f'/{pid}'
            nodes.append({
                'id': pid,
                'node': getattr(plg, 'node', '0'),
                'name': getattr(plg, 'PLUGIN_NAME', pid),
                'enabled': getattr(plg, 'enabled', False),
                'panel_url': base,
                'status_url': f'{base}/status',
                'control_url': f'{base}/control',
            })
        self._send_json(req, nodes)

    @staticmethod
    def _send_json(req, obj, code=200):
        body = json.dumps(obj).encode()
        req.send_response(code)
        req.send_header('Content-Type', 'application/json')
        req.send_header('Content-Length', str(len(body)))
        req.end_headers()
        req.wfile.write(body)

    def _http_status(self, req, parent):
        """Cheap status poll — no AMI call (audio counters + cached links)."""
        self._send_json(req, self.get_status())

    def _http_control(self, req, parent):
        """POST control actions. Body: form-encoded or JSON {action, node, mode}."""
        if req.command != 'POST':
            return self._send_json(req, {'ok': False, 'error': 'POST only'}, 405)
        try:
            length = int(req.headers.get('Content-Length', 0))
        except (TypeError, ValueError):
            length = 0
        raw = req.rfile.read(length).decode(errors='replace') if length else ''
        action, node, mode = '', '', 'transceive'
        on = ''                                   # used by pcm_tap
        name = ''                                 # used by book_set
        if raw.strip().startswith('{'):
            try:
                d = json.loads(raw)
                action = str(d.get('action', ''))
                node = str(d.get('node', ''))
                mode = str(d.get('mode', 'transceive'))
                on = str(d.get('on', ''))
                name = str(d.get('name', ''))
            except ValueError:
                pass
        else:
            import urllib.parse
            q = urllib.parse.parse_qs(raw)
            action = (q.get('action') or [''])[0]
            node = (q.get('node') or [''])[0]
            mode = (q.get('mode') or ['transceive'])[0]
            on = (q.get('on') or [''])[0]
            name = (q.get('name') or [''])[0]

        if action == 'connect':
            m = ILINK_MONITOR if mode == 'monitor' else ILINK_TRANSCEIVE
            res = self.connect_node(node, m)
            self.link_status()                      # refresh the panel's list
        elif action == 'disconnect':
            res = self.disconnect_node(node)
            self.link_status()
        elif action == 'disconnect_all':
            res = self.disconnect_all()
            self.link_status()
        elif action == 'links':
            res = self.link_status()
        elif action == 'node_stats':
            res = self.node_stats()
        elif action == 'book_set':
            # Naming writes to the SHARED book, so a node named on AS1 is
            # named on AS2 too — the whole point of one address book.
            res = ({'ok': True, 'node': node, 'name': name, 'book': NODE_BOOK.all()}
                   if NODE_BOOK.set_name(node, name)
                   else {'ok': False, 'error': f'invalid node: {node!r}'})
        elif action == 'book_delete':
            res = {'ok': NODE_BOOK.remove(node), 'node': node, 'book': NODE_BOOK.all()}
        elif action == 'pcm_tap':
            # Monitor this node in the browser PCM stream. Deliberately does
            # NOT touch routing — a listen button should not rewrite the graph.
            want = str(on).lower() not in ('0', 'false', 'off', '')
            self.pcm_tap = want
            if not want:
                self._tap_queue.clear()
            else:
                # Exclusive: PCM main, AS1 or AS2 — never two nodes at once.
                # Enabling one clears the others so the stream carries a single
                # identifiable source.
                gw = getattr(self, '_gateway', None)   # set in setup(); NOT self.gateway
                for _pid, _plg in (getattr(gw, '_external_plugins', {}) or {}).items():
                    if _plg is not self and getattr(_plg, 'pcm_tap', False):
                        _plg.pcm_tap = False
                        try:
                            _plg._tap_queue.clear()
                        except Exception:
                            pass
            res = {'ok': True, 'pcm_tap': self.pcm_tap}
        else:
            return self._send_json(req, {'ok': False, 'error': f'bad action: {action}'}, 400)
        self._send_json(req, res)

    def _panel_rewrites(self):
        """Extra (find, replace) pairs applied to the panel. Subclasses that
        serve the template on a different path override THIS, not the whole
        handler."""
        return ()

    def _render_panel(self):
        """Panel HTML with every placeholder substituted.

        Both instances share one template, so the label has to come from the
        plugin — otherwise every panel reads identically apart from a node
        number you cannot map back to the routing page. Centralised because the
        usrp2 subclass used to copy this substitution chain into its own
        handler, so a placeholder added here rendered literally on that
        instance ("__LABEL__ — node 683971").
        """
        html = (_PANEL_HTML
                .replace('__NODE__', self.node)
                .replace('__LABEL__', getattr(self, 'PLUGIN_NAME', 'AllStar (USRP)')))
        for find, repl in self._panel_rewrites():
            html = html.replace(find, repl)
        return html

    def _http_panel(self, req, parent):
        body = self._render_panel().encode()
        req.send_response(200)
        req.send_header('Content-Type', 'text/html; charset=utf-8')
        req.send_header('Content-Length', str(len(body)))
        req.end_headers()
        req.wfile.write(body)


# ── standalone loopback self-test ──────────────────────────────────
# Verifies both audio directions without an ASL node:
#   python3 plugins/usrp.py
# It binds a fake "bridge node" socket, sends synthetic USRP voice frames at
# the plugin (RX), and captures the frames the plugin sends back (TX).
if __name__ == '__main__':
    import sys

    class _Cfg:
        USRP_REMOTE_HOST = '127.0.0.1'
        USRP_REMOTE_PORT = 41001        # fake node listens here
        USRP_LISTEN_PORT = 41000        # plugin listens here
        USRP_NODE = '68397'
        USRP_AMI_HOST = '127.0.0.1'
        USRP_AMI_PORT = 45038           # fake AMI server
        USRP_AMI_USER = 'admin'
        USRP_AMI_SECRET = 'secret'

    # Fake node socket (the "ASL" end)
    node = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    node.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    node.bind(('', _Cfg.USRP_REMOTE_PORT))
    node.settimeout(0.5)

    p = UsrpPlugin()
    assert p.setup(_Cfg()), "setup failed"

    # --- RX test: stream 25 voice frames (500 ms) of a 1 kHz tone @ 8 kHz,
    # draining the bus chunk queue as we go (mirrors real bus ticking, so the
    # shallow queue + prime-flush don't discard a one-shot burst). ---
    t = np.arange(USRP_VOICE_SAMPLES)
    tone = (np.sin(2 * np.pi * 1000 * t / USRP_RATE) * 8000).astype('<i2')
    rx_chunks, lvl = 0, 0
    for i in range(25):
        hdr = USRP_HEADER.pack(b'USRP', i, 0, 1, 0, USRP_TYPE_VOICE, 0, 0)
        node.sendto(hdr + tone.tobytes(), ('127.0.0.1', _Cfg.USRP_LISTEN_PORT))
        time.sleep(0.02)
        d, _ = p.get_audio()             # bus would call this ~every tick
        if d is not None:
            rx_chunks += 1
            lvl = p.audio_level
    time.sleep(0.1)
    while True:                          # drain the tail
        d, _ = p.get_audio()
        if d is None:
            break
        rx_chunks += 1
        lvl = p.audio_level
    # 25 frames * 960 samples = 24000 @ 48k = 10 full 2400-sample chunks
    print(f"RX: received {rx_chunks} bus chunks (expect ~10), level={lvl}, "
          f"pkts_rx={p.pkts_rx}")
    assert rx_chunks >= 9, "RX chunk count too low"
    assert lvl > 0, "RX level should be non-zero for a tone"

    # --- TX test: key up, push 200 ms of bus audio, confirm USRP frames out ---
    bus_tone = (np.sin(2 * np.pi * 1000 * np.arange(BUS_CHUNK_SAMPLES) / BUS_RATE)
                * 8000).astype('<i2').tobytes()
    p.on_ptt_change(True)
    for _ in range(4):                  # 4 * 50 ms = 200 ms
        p.put_audio(bus_tone)
        time.sleep(0.05)
    p.on_ptt_change(False)
    time.sleep(0.1)
    got, keyups, unkey_seen = 0, 0, False
    while True:
        try:
            d, _ = node.recvfrom(2048)
        except socket.timeout:
            break
        if d[:4] != b'USRP':
            continue
        _, _, _, ku, _, ty, _, _ = USRP_HEADER.unpack(d[:USRP_HEADER_LEN])
        got += 1
        if ku:
            keyups += 1
        else:
            unkey_seen = True
    print(f"TX: node received {got} USRP frames ({keyups} keyed), "
          f"unkey trailer seen={unkey_seen}, pkts_tx={p.pkts_tx}")
    assert keyups >= 8, "expected ~10 keyed voice frames for 200 ms"
    assert unkey_seen, "expected a keyup=0 trailer on unkey"

    # --- AMI control test: a REALISTIC persistent fake AMI server that wraps
    # CLI output in "Output: " lines (as Asterisk actually does — the prefix
    # that broke the lstats parser) and answers per-command. Confirms the
    # connect command AND the link_status direct/conference split + sort. ---
    captured = {'cmds': []}
    _ami_stop = threading.Event()

    def _ami_body_lines(cmd):
        if 'ilink' in cmd:
            return ['Output: ok']
        if 'lstats' in cmd:   # two direct links, different uptimes (sort test)
            return ['Output: NODE  PEER  RECONNECTS  DIRECTION  CONNECT TIME  CONNECT STATE',
                    'Output: ----  ----  ----------  ---------  ------------  -------------',
                    'Output: 55553 1.2.3.4 0 OUT 00:10:01:165 ESTABLISHED',
                    'Output: 41836 5.6.7.8 0 OUT 00:00:11:020 ESTABLISHED']
        if 'nodes' in cmd:    # mesh = direct (55553,41836) + conference (1002,546012)
            return ['Output: ', 'Output: T55553, T41836, T1002, T546012', 'Output: ']
        return ['Output: ']

    def _fake_ami():
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(('127.0.0.1', _Cfg.USRP_AMI_PORT))
        srv.listen(5)
        srv.settimeout(0.3)
        while not _ami_stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            try:
                conn.settimeout(2.0)
                conn.sendall(b'Asterisk Call Manager/8.0.0\r\n')
                data = b''
                while b'\r\n\r\n' not in data:
                    data += conn.recv(1024)
                conn.sendall(b'Response: Success\r\n\r\n')
                data = b''
                while b'\r\n\r\n' not in data:
                    data += conn.recv(1024)
                cmd = ''
                for line in data.decode().splitlines():
                    if line.startswith('Command:'):
                        cmd = line.split(':', 1)[1].strip()
                captured['cmds'].append(cmd)
                resp = 'Response: Follows\r\n' + \
                       ''.join(l + '\r\n' for l in _ami_body_lines(cmd)) + \
                       '--END COMMAND--\r\n\r\n'
                conn.sendall(resp.encode())
            except OSError:
                pass
            finally:
                conn.close()
        srv.close()

    ami_srv = threading.Thread(target=_fake_ami, daemon=True)
    ami_srv.start()
    time.sleep(0.1)

    res = p.connect_node('12345', mode=ILINK_TRANSCEIVE)
    assert res['ok'], f"connect_node failed: {res}"
    assert 'rpt cmd 68397 ilink 3 12345' in captured['cmds'], \
        f"wrong rpt command: {captured['cmds']!r}"

    ls = p.link_status()
    dn = [d['node'] for d in ls['direct']]
    print(f"AMI: connect ok; link_status direct={dn} indirect={ls['indirect']}")
    assert dn == ['41836', '55553'], f"direct not newest-first: {dn}"   # 41836 newer
    assert ls['indirect'] == ['1002', '546012'], f"bad conference list: {ls['indirect']}"
    assert ls['direct'][0]['state'] == 'ESTABLISHED', "lstats state not parsed"

    _ami_stop.set()
    ami_srv.join(timeout=2.0)
    p.cleanup()
    node.close()
    print("\n✓ USRP plugin self-test passed "
          "(RX + TX + PTT + AMI connect + link_status direct/conf/sort)")
    sys.exit(0)
