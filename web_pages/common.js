/* Common utilities for all gateway pages — loaded from /pages/common.js */

// ── Theme ──────────────────────────────────────────────────────────────────

var _T = {};

function loadTheme() {
  return fetch('/theme')
    .then(function(r) { return r.json(); })
    .then(function(t) {
      _T = t;
      // Alias for shell.html compat (uses camelCase)
      _T.btnBorder = t.btn_border;
      var root = document.documentElement;
      root.style.setProperty('--t-bg', t.bg);
      root.style.setProperty('--t-panel', t.panel);
      root.style.setProperty('--t-border', t.border);
      root.style.setProperty('--t-accent', t.accent);
      root.style.setProperty('--t-btn', t.btn);
      root.style.setProperty('--t-btn-border', t.btn_border);
      root.style.setProperty('--t-btn-hover', t.btn_hover);
      root.style.setProperty('--t-btn-active', t.btn_active_bg);
      root.style.setProperty('--t-checkbox', t.checkbox);
      if (t.gateway_name) {
        document.title = t.gateway_name + ' - ' + document.title;
      }
    })
    .catch(function(e) {
      console.warn('Failed to load theme:', e);
    });
}

loadTheme();


// ── Fetch helpers ──────────────────────────────────────────────────────────

function postJson(url, data) {
  return fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data)
  }).then(function(r) { return r.json(); });
}

function getJson(url) {
  return fetch(url).then(function(r) { return r.json(); });
}


// ── Polling ────────────────────────────────────────────────────────────────

function createPoller(url, intervalMs, callback, opts) {
  opts = opts || {};
  var timeoutMs = opts.timeout || 10000;
  var busy = false;
  var timer = null;

  function poll() {
    if (busy) return;
    busy = true;
    var ac = new AbortController();
    var to = setTimeout(function() { ac.abort(); }, timeoutMs);
    fetch(url, {signal: ac.signal})
      .then(function(r) { return r.json(); })
      .then(function(data) { callback(data); })
      .catch(function() {})
      .finally(function() { clearTimeout(to); busy = false; });
  }

  timer = setInterval(poll, intervalMs);
  poll();

  return {
    stop: function() { if (timer) { clearInterval(timer); timer = null; } },
    poll: poll
  };
}


// ── Common actions ─────────────────────────────────────────────────────────

function sendKey(k) {
  postJson('/key', {key: k});
  if (document.activeElement) document.activeElement.blur();
}

function openTmux() {
  postJson('/open_tmux', {});
}


// ── Formatting ─────────────────────────────────────────────────────────────

function fmtSecs(s) {
  if (s === null || s === undefined) return '--';
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm ' + Math.floor(s % 60) + 's';
  return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
}

function fmtTimestamp(ts) {
  if (!ts) return '--';
  try { return new Date(ts).toLocaleTimeString(); }
  catch(e) { return typeof ts === 'string' ? ts.slice(11, 19) : '--'; }
}

function fmtDuration(s) {
  if (!s || isNaN(s)) return '0:00';
  var m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return m + ':' + (sec < 10 ? '0' : '') + sec;
}

function fmtBytes(b) {
  if (b >= 1048576) return (b / 1048576).toFixed(1) + ' MB/s';
  if (b >= 1024) return (b / 1024).toFixed(1) + ' KB/s';
  return b + ' B/s';
}


// ── Motion ─────────────────────────────────────────────────────────────────

// Restart the CSS cell-flash animation on an element — use sparingly for
// "this value just changed" moments. Pair with setFlash() for set-and-flash.
function flashValue(el) {
  if (!el) return;
  el.classList.remove('flash');
  void el.offsetWidth;   // force reflow so animation restarts
  el.classList.add('flash');
}

// Set text content and flash iff the value actually changed.
// Drop-in replacement for `el.textContent = x` on live-updating cells.
function setFlash(el, text) {
  if (!el) return;
  var v = (text === null || text === undefined) ? '' : String(text);
  if (el.textContent === v) return;
  el.textContent = v;
  flashValue(el);
}
