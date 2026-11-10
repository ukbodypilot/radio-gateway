/* Dashboard family — shared helpers for the /dashboard sub-pages.
   Loaded after common.js (needs postJson/createPoller). */

// ── Change-flash bookkeeping ────────────────────────────────────────────────
var _prevStates = {};
function flashCls(key, val) {
  var prev = _prevStates[key]; _prevStates[key] = val;
  return (prev !== undefined && prev !== val) ? ' flash' : '';
}
function flashDotOnChange(el, val) {
  var key = '_dot_' + el.id;
  if (_prevStates[key] !== undefined && _prevStates[key] !== val) flashValue(el);
  _prevStates[key] = val;
}

// Escape untrusted text before innerHTML. Endpoint/worker names and stream
// errors are remote-supplied strings and can contain angle brackets.
function esc(t){ return String(t==null?'':t).replace(/[&<>"']/g, function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }

// ── Toasts ──────────────────────────────────────────────────────────────────
function showToast(msg, level) {
  var c = document.getElementById('toast-container'); if (!c) return;
  var d = document.createElement('div');
  d.className = 'toast ' + (level==='warning'?'warning':level==='error'?'error':'info');
  d.textContent = msg;
  var dismiss = function(){ d.classList.add('fading'); setTimeout(function(){ d.remove(); }, 400); };
  d.onclick = dismiss; c.appendChild(d); setTimeout(dismiss, 8000);
}
// Gateway-pushed notifications ride on /status; every dash page surfaces
// them (only one sub-page is loaded in the shell iframe at a time).
var _lastNotifSeq = 0;
function handleNotifications(s) {
  if (!s.notifications || !s.notifications.length) return;
  for (var i = 0; i < s.notifications.length; i++) {
    var n = s.notifications[i];
    if (n.seq > _lastNotifSeq) { _lastNotifSeq = n.seq; showToast(n.msg, n.level); }
  }
}

// ── Panel tabs (input-mode switchers) ───────────────────────────────────────
// Panes live inside containerId as .panel-pane elements. Markup carries
// role=tablist/tab/tabpanel; this keeps class state and aria-selected in sync.
function switchPanelTab(containerId, paneId, btn) {
  var c = document.getElementById(containerId); if (!c) return;
  c.querySelectorAll('.panel-pane').forEach(function(p){ p.classList.remove('active'); });
  c.querySelectorAll('.panel-tab').forEach(function(b){
    b.classList.remove('active');
    b.setAttribute('aria-selected', 'false');
  });
  var pane = document.getElementById(paneId);
  if (pane) pane.classList.add('active');
  if (btn) { btn.classList.add('active'); btn.setAttribute('aria-selected', 'true'); }
}

// ── /status poll with offline detect + auto-reload ─────────────────────────
// Preferred source is the shell's 1s /status broadcast (postMessage) — one
// poll feeds the meter strip and the page. The page only fetches /status
// itself when no broadcast has arrived within 5s (opened outside the shell,
// or the shell's poll is failing). On 5 consecutive fetch failures the page
// shows "Gateway offline" (via onLost) and probes until the server answers,
// then reloads itself. A broadcast arriving while lost also means the
// gateway is back — reload.
function createStatusPoller(cb, onLost) {
  var busy = false, lost = false, lostCount = 0, lastFeed = 0;
  window.addEventListener('message', function(ev) {
    if (ev.origin !== location.origin) return;
    var d = ev.data;
    if (!d || d.type !== 'rg-status' || !d.status) return;
    lastFeed = Date.now();
    lostCount = 0;
    if (lost) { window.location.reload(); return; }
    cb(d.status);
  });
  function poll() {
    if (Date.now() - lastFeed < 5000) return;   // shell broadcast is feeding us
    if (lost) {
      fetch('/status').then(function(r){ if (r.ok) window.location.reload(); }).catch(function(){});
      return;
    }
    if (busy) return;
    busy = true;
    var ac = new AbortController(); setTimeout(function(){ ac.abort(); }, 10000);
    fetch('/status', {signal: ac.signal})
      .then(function(r){ return r.json(); })
      .then(function(s){ lostCount = 0; cb(s); })
      .catch(function(){
        lostCount++;
        if (lostCount >= 5) { lost = true; if (onLost) onLost(); }
      })
      .finally(function(){ busy = false; });
  }
  setInterval(poll, 2000);
  poll();
}

// ── System bars (Overview) ─────────────────────────────────────────────────
// Color zones live in the bar's CSS background gradient (0-60px green,
// 60-80px amber, 80-100px red). Setting the bar's width reveals only the
// leftmost portion of that gradient, so amber/red appear only when the bar
// has actually filled past 60/80%.
function sysBar(pct, tone) {
  var w = Math.round(Math.min(Math.max(pct, 0), 100));
  var p = pct < 10 ? '  ' + pct : pct < 100 ? ' ' + pct : '' + pct;
  var toneCls = tone ? (' tone-' + tone) : '';
  return '<span class="bar-pct">'+p+'%</span><span class="bar bar-rx'+toneCls+'" style="width:'+w+'px"></span>';
}

// ── Sub-page tabs ───────────────────────────────────────────────────────────
(function() {
  var pages = [['/dashboard','Overview'], ['/dashboard/endpoints','Endpoints'],
               ['/dashboard/services','Services'], ['/dashboard/operate','Operate']];
  var here = location.pathname.replace(/\/$/, '');
  var nav = document.createElement('nav');
  nav.className = 'dash-tabs';
  nav.setAttribute('aria-label', 'Dashboard pages');
  pages.forEach(function(p) {
    var a = document.createElement('a');
    a.href = p[0]; a.textContent = p[1];
    if (p[0] === here) { a.className = 'active'; a.setAttribute('aria-current', 'page'); }
    nav.appendChild(a);
  });
  document.body.insertBefore(nav, document.body.firstChild);
})();
