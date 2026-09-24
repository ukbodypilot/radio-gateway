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
      // Semantic tokens — each key optional; common.css defaults apply if absent.
      if (t.panel_hi)  root.style.setProperty('--t-panel-hi',  t.panel_hi);
      if (t.border_hi) root.style.setProperty('--t-border-hi', t.border_hi);
      if (t.text)      root.style.setProperty('--t-text',      t.text);
      if (t.text_dim)  root.style.setProperty('--t-text-dim',  t.text_dim);
      if (t.text_mute) root.style.setProperty('--t-text-mute', t.text_mute);
      if (t.ok)        root.style.setProperty('--t-ok',        t.ok);
      if (t.warn)      root.style.setProperty('--t-warn',      t.warn);
      if (t.err)       root.style.setProperty('--t-err',       t.err);
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

// Change-detection DOM helpers — skip the write if value is already current.
function setText(el, val) { if (el && el.textContent !== String(val)) el.textContent = String(val); }
function setClass(el, cls) { if (el && el.className !== cls) el.className = cls; }
function setHTML(el, html) { if (el && el.innerHTML !== html) el.innerHTML = html; }

// ── Status-row builders ────────────────────────────────────────────────────
// Shared renderers for the label/value readout grammar (.st-row / .st-item /
// .st-label / .st-val) so pages stop hand-assembling the same spans.
// valueHtml is trusted HTML — the CALLER escapes any remote-supplied string.
function stVal(text, cls) {
  return '<span class="st-val' + (cls ? ' ' + cls : '') + '">' + text + '</span>';
}
function stItem(label, valueHtml) {
  return '<div class="st-item"><span class="st-label">' + label + ':</span>'
       + valueHtml + '</div>';
}
function stRow(items, extraCls) {
  return '<div class="st-row' + (extraCls ? ' ' + extraCls : '') + '">'
       + (Array.isArray(items) ? items.join('') : items) + '</div>';
}
// Common value idioms: yes/no (green when yes) and ON/off where ON is the
// alarming state (PTT, TX — red when keyed, green when idle).
function stYesNo(v) { return stVal(v ? 'Yes' : 'No', v ? 'green' : 'red'); }
function stOnOff(v) { return stVal(v ? 'ON' : 'off', v ? 'red' : 'green'); }

// ── Audio meter physics (RG.vu) ────────────────────────────────────────
// rAF-driven interpolator that gives real VU-meter behavior to any bar.
//
// Why this exists: poll-driven `el.style.width = X%` looks stuttery
// because CSS transitions are direction-blind (same speed up and down)
// and poll rates (250-500ms) are much slower than perceived motion.
//
// What it does: target value is decoupled from displayed value. Each
// frame, current → target using asymmetric envelope (~50ms attack,
// ~500ms decay). Optional peak-hold sliver follows the high-water mark
// and falls gently after ~1s — the detail every hardware meter has.
//
// Opt-in: add class "vu" to the wrap, or attach explicitly via
// RG.vu.set(elementOrId, percent, optionalClass). Existing pages that
// keep using style.width still work (CSS transition gives some smoothing)
// — only opt-ins get the full physics.

window.RG = window.RG || {};
(function() {
  var meters = [];
  var attached = new WeakSet();

  function _findWrap(el) {
    // Any element passed in that has a .vu class (or one of the legacy
    // wrap classes) is treated as the wrap. Otherwise we look for a
    // descendant. Final fallback: the el itself.
    if (!el) return null;
    if (el.classList && el.classList.contains('vu')) return el;
    var inner = el.querySelector('.vu');
    return inner || el;
  }

  function _findFill(wrap) {
    // Permissive: any first descendant whose class contains "-fill" and is
    // not a sparkline bar. Lets the engine work with .bar-fill, .lvl-fill,
    // .link-fill, .mon-level-fill, .d75-meter-fill, .pkt-meter-fill, .kv-meter-fill
    // without enumerating each here.
    var nodes = wrap.querySelectorAll('[class*="-fill"]');
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      if (n.className.indexOf('spark-bar') !== -1) continue;
      return n;
    }
    return null;
  }

  function attach(el) {
    if (!el || attached.has(el)) return null;
    var wrap = _findWrap(el) || el;
    var fill = _findFill(wrap);
    if (!fill) return null;
    // Fill spans the full wrap and carries the gradient; reveal it with
    // clip-path so the gradient stays anchored to the bar's left edge
    // (NOT scaled). At 50% fill you see only the leftmost half of the
    // gradient — entirely green — and amber/red zones become visible
    // only when the fill actually reaches them.
    fill.style.width = '100%';
    fill.style.transition = 'none';
    fill.style.transform = 'none';
    var initialW = (parseFloat(fill.dataset.initialWidth) || 0);
    fill.style.clipPath = 'inset(0 ' + (100 - initialW) + '% 0 0)';
    // Peak marker — auto-created if missing.
    var peak = wrap.querySelector('.vu-peak');
    if (!peak) {
      peak = document.createElement('span');
      peak.className = 'vu-peak';
      wrap.appendChild(peak);
    }
    var m = {
      el: el, wrap: wrap, fill: fill, peak: peak,
      target: initialW, current: initialW, peakLvl: initialW,
      peakHoldUntil: 0,
    };
    meters.push(m);
    attached.add(el);
    return m;
  }

  function set(elOrId, percent, opts) {
    var el = (typeof elOrId === 'string') ? document.getElementById(elOrId) : elOrId;
    if (!el) return;
    var m = meters.find(function(x) { return x.el === el || x.wrap === el || x.fill === el; });
    if (!m) m = attach(el);
    if (!m) return;
    var v = Math.max(0, Math.min(100, +percent || 0));
    m.target = v;
    if (opts && (typeof opts === 'string' || opts.class)) {
      // Preserve whichever fill class was already on the element so the
      // engine works with .bar-fill / .lvl-fill / .vad-fill / .link-fill
      // alike. Append the tone class (speech / gated / rx / tx / etc.).
      var tone = (typeof opts === 'string') ? opts : opts.class;
      var existing = m.fill.className.split(/\s+/);
      var base = existing[0] || 'bar-fill';
      var next = base + ' ' + tone;
      if (m.fill.className !== next) m.fill.className = next;
    }
  }

  var ATTACK = 0.55;     // close 55% of the gap per frame on rise — punchy
  var DECAY  = 0.06;     // close 6% per frame on fall — gentle gravity
  var PEAK_HOLD_MS = 1100;
  var PEAK_FALL_PER_FRAME = 0.7;

  function tick() {
    var now = performance.now();
    for (var i = 0; i < meters.length; i++) {
      var m = meters[i];
      var diff = m.target - m.current;
      if (Math.abs(diff) < 0.05) {
        m.current = m.target;
      } else if (diff > 0) {
        m.current += diff * ATTACK;
      } else {
        m.current += diff * DECAY;
      }
      // Peak hold + fall
      if (m.current > m.peakLvl) {
        m.peakLvl = m.current;
        m.peakHoldUntil = now + PEAK_HOLD_MS;
      } else if (now > m.peakHoldUntil) {
        m.peakLvl -= PEAK_FALL_PER_FRAME;
        if (m.peakLvl < m.current) m.peakLvl = m.current;
      }
      m.fill.style.clipPath = 'inset(0 ' + (100 - m.current) + '% 0 0)';
      // peak visible only when above the current rendered level by >2pt,
      // and above 4% — avoids a stray pixel at idle.
      if (m.peakLvl > 4 && m.peakLvl - m.current > 2) {
        m.peak.style.left = m.peakLvl + '%';
        m.peak.style.opacity = '1';
      } else {
        m.peak.style.opacity = '0';
      }
    }
    requestAnimationFrame(tick);
  }

  function autoAttach() {
    document.querySelectorAll('.vu').forEach(attach);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', autoAttach);
  } else {
    autoAttach();
  }
  requestAnimationFrame(tick);

  window.RG.vu = { set: set, attach: attach };
})();

// ── Soundboard category picker ──────────────────────────────────────────────
// Lives here rather than in a page so /controls and /dashboard/operate share
// one implementation. The dialog is built on demand, so neither page carries
// picker markup.

function _sbEsc(t) {
  return String(t == null ? '' : t).replace(/[&<>"']/g, function (c) {
    return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
  });
}

// dash pages have showToast(); /controls does not, so degrade to alert().
function _sbNotify(msg, level) {
  if (typeof showToast === 'function') showToast(msg, level);
  else alert(msg);
}

function openSoundboardPicker(onSaved) {
  fetch('/soundboard/categories')
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (!d.ok) { _sbNotify('Categories unavailable: ' + (d.error || 'unknown'), 'error'); return; }
      _sbShowPicker(d, onSaved);
    })
    .catch(function (e) { _sbNotify('Categories unavailable: ' + e, 'error'); });
}

function _sbShowPicker(d, onSaved) {
  var old = document.getElementById('sb-picker');
  if (old) old.remove();

  var counts = {};
  d.categories.forEach(function (c) { counts[c.name] = c.count; });

  // A blank filter means "all", so start everything ticked rather than
  // showing an empty picker on a fresh install.
  var rows = d.categories.map(function (c) {
    var on = d.all || d.selected.indexOf(c.name) >= 0;
    return '<label class="sb-item"><input type="checkbox" data-cat="' + _sbEsc(c.name) + '"' +
           (on ? ' checked' : '') + '><span class="sb-name">' + _sbEsc(c.name) +
           '</span><span class="sb-count">' + c.count + '</span></label>';
  }).join('');

  var dlg = document.createElement('dialog');
  dlg.id = 'sb-picker';
  dlg.className = 'sb-dialog';
  dlg.innerHTML =
    '<h3 class="sb-title">Soundboard categories</h3>' +
    '<p class="sb-sub">Refresh draws only from the ticked categories.' +
      (d.max_seconds ? ' Clips longer than ' + d.max_seconds + 's are skipped.' : '') + '</p>' +
    '<div class="sb-bar">' +
      '<button type="button" class="sb-btn" id="sb-all">All</button>' +
      '<button type="button" class="sb-btn" id="sb-none">None</button>' +
      '<span class="sb-tally" id="sb-tally"></span>' +
    '</div>' +
    '<div class="sb-grid">' + rows + '</div>' +
    '<div class="sb-foot">' +
      '<button type="button" class="sb-btn" id="sb-cancel">Cancel</button>' +
      '<button type="button" class="sb-btn sb-primary" id="sb-save">Save</button>' +
    '</div>';
  document.body.appendChild(dlg);

  function boxes() { return dlg.querySelectorAll('input[data-cat]'); }
  function tally() {
    var n = 0, sounds = 0;
    boxes().forEach(function (b) {
      if (b.checked) { n++; sounds += counts[b.dataset.cat] || 0; }
    });
    var el = dlg.querySelector('#sb-tally');
    el.textContent = n + ' of ' + d.categories.length + ' · ' + sounds + ' sounds';
    // Saving zero ticked is stored as "all" server-side (a silent soundboard
    // is worse than an ignored filter) — say so instead of surprising them.
    el.classList.toggle('sb-warn', n === 0);
    if (n === 0) el.textContent = 'Nothing ticked — saves as "all categories"';
  }

  boxes().forEach(function (b) { b.addEventListener('change', tally); });
  dlg.querySelector('#sb-all').onclick = function () {
    boxes().forEach(function (b) { b.checked = true; }); tally();
  };
  dlg.querySelector('#sb-none').onclick = function () {
    boxes().forEach(function (b) { b.checked = false; }); tally();
  };
  dlg.querySelector('#sb-cancel').onclick = function () { dlg.close(); };
  dlg.querySelector('#sb-save').onclick = function () {
    var picked = [];
    boxes().forEach(function (b) { if (b.checked) picked.push(b.dataset.cat); });
    var btn = this;
    btn.disabled = true;
    btn.textContent = 'Saving…';
    fetch('/soundboard/categories', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({categories: picked})
    })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        dlg.close();
        if (!res.ok) { _sbNotify('Save failed: ' + (res.error || 'unknown'), 'error'); return; }
        _sbNotify(res.saved
          ? 'Saved ' + picked.length + ' categories — applies on the next Refresh'
          : 'All categories enabled — applies on the next Refresh');
        if (typeof onSaved === 'function') onSaved(res);
      })
      .catch(function (e) { dlg.close(); _sbNotify('Save failed: ' + e, 'error'); });
  };

  dlg.addEventListener('close', function () { dlg.remove(); });
  tally();
  dlg.showModal();
}

// ── TTS engine selector ─────────────────────────────────────────────────────
// Populates a <select id="tts-engine"> and hot-swaps the backend on change.
// The voice dropdowns repopulate on their own: switching the engine changes
// what /status reports in tts_voices, and the poller already rebuilds when the
// value set changes.

function initTtsEngineSelect() {
  var sel = document.getElementById('tts-engine');
  if (!sel) return;
  fetch('/tts/engine')
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (!d.ok || !d.engines) return;
      sel.innerHTML = d.engines.map(function (e) {
        // An engine whose package is missing is shown but not selectable —
        // clearer than hiding it and leaving the user wondering where it went.
        return '<option value="' + _sbEsc(e.value) + '"' +
               (e.active ? ' selected' : '') +
               (e.available ? '' : ' disabled') + '>' +
               _sbEsc(e.label) + (e.available ? '' : ' (not installed)') +
               '</option>';
      }).join('');
      sel.dataset.active = d.active || '';
    })
    .catch(function () { /* selector is optional; never break the page */ });

  sel.addEventListener('change', function () {
    var want = sel.value, prev = sel.dataset.active || '';
    sel.disabled = true;
    fetch('/tts/engine', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({engine: want})
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        sel.disabled = false;
        if (!d.ok) {
          // Failed switch leaves the OLD engine live server-side, so put the
          // dropdown back rather than showing a selection that isn't real.
          if (prev) sel.value = prev;
          _sbNotify('Could not switch engine: ' + (d.message || 'unknown'), 'error');
          return;
        }
        sel.dataset.active = d.active || want;
        // Force the voice poller to rebuild on the next tick.
        window._ttsVoiceKey = null;
        _sbNotify('TTS engine: ' + (d.message || d.active));
      })
      .catch(function (e) {
        sel.disabled = false;
        if (prev) sel.value = prev;
        _sbNotify('Could not switch engine: ' + e, 'error');
      });
  });
}

document.addEventListener('DOMContentLoaded', initTtsEngineSelect);

// ── Playback grid ───────────────────────────────────────────────────────────
// Built from /status rather than hardcoded in each page, so PLAYBACK_SLOTS can
// change without editing HTML. Shared by /controls and /dashboard/operate.

function renderPlaybackGrid(s) {
  var grid = document.getElementById('pb-grid');
  if (!grid || !s.files) return;

  // Rebuild only when the SET of slots changes — rebuilding every poll would
  // fight the user's clicks and flicker the labels.
  var keys = Object.keys(s.files).sort(function (a, b) {
    if (a === '0') return 1;          // station ID sorts last
    if (b === '0') return -1;
    return (+a) - (+b);
  });
  var key = keys.join(',');
  if (grid.dataset.slotKey !== key) {
    grid.dataset.slotKey = key;
    grid.innerHTML = keys.map(function (k) {
      var label = k === '0' ? 'ID' : k;
      return '<button class="ctrl-btn" onclick="sendKey(\'' + k + '\')" ' +
             'id="btn-f' + k + '" aria-label="Play slot ' + label + '">' +
             '<span class="pb-key">' + label + '</span>' +
             '<span class="pb-name" id="pb-name-' + k + '"></span></button>';
    }).join('');
  }

  keys.forEach(function (k) {
    var b = document.getElementById('btn-f' + k);
    var n = document.getElementById('pb-name-' + k);
    var f = s.files[k];
    if (b) {
      b.classList.toggle('muted', !!f.playing);
      b.classList.toggle('dim', !f.loaded);
      b.title = f.name || ('Slot ' + k + ' (empty)');
    }
    if (n) n.textContent = f.loaded && f.name ? f.name.replace(/\.[^.]+$/, '').substring(0, 11) : '';
  });
}

// ── Loop button ─────────────────────────────────────────────────────────────
// Sends an explicit start/stop and reflects the SERVER's state on every poll.
// It used to be a blind toggle whose lit state lived only in the browser, so
// anything that stopped the loop server-side (the Stop button, a queued
// announcement, a restart) left the button lit and inverted — the next click
// then started a loop instead of stopping one.

function syncLoopButton(s) {
  var btn = document.getElementById('btn-test-loop');
  if (!btn || typeof s.loop_active === 'undefined') return;
  if (btn.dataset.busy === '1') return;      // don't fight an in-flight click
  var on = !!s.loop_active;
  btn.classList.toggle('muted', on);
  btn.textContent = on ? 'Stop Loop' : 'Loop';
  btn.dataset.looping = on ? '1' : '0';
}

function toggleTestLoop(btn) {
  btn = btn || document.getElementById('btn-test-loop');
  if (!btn) return;
  // Say what we mean rather than toggling blind.
  var action = btn.dataset.looping === '1' ? 'stop' : 'start';
  btn.dataset.busy = '1';
  fetch('/testloop', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: action})
  })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      btn.dataset.busy = '';
      btn.dataset.looping = d.looping ? '1' : '0';
      btn.classList.toggle('muted', !!d.looping);
      btn.textContent = d.looping ? 'Stop Loop' : 'Loop';
      if (d.error) _sbNotify(d.error, 'error');
    })
    .catch(function (e) {
      btn.dataset.busy = '';
      _sbNotify('Loop failed: ' + e, 'error');
    });
}

// ── Background music ────────────────────────────────────────────────────────
// BGM plays through the playback source, so it lands wherever the "System
// Sounds" node is wired in /routing. Buttons reflect SERVER state on every
// poll — same lesson as the Loop button, which used to keep its lit state only
// in the browser and end up inverted.

function renderBgm(s) {
  var row = document.getElementById('bgm-row');
  if (!row || !s.bgm) return;

  var key = s.bgm.map(function (b) { return b.slot + ':' + b.file; }).join(',');
  if (row.dataset.bgmKey !== key) {
    row.dataset.bgmKey = key;
    row.innerHTML = s.bgm.map(function (b) {
      return '<button class="ctrl-btn" id="bgm-' + b.slot + '" ' +
             'onclick="toggleBgm(' + b.slot + ')" ' +
             'aria-label="Background music ' + b.slot + '">' +
             '<span class="pb-key">BGM ' + b.slot + '</span>' +
             '<span class="pb-name" id="bgm-name-' + b.slot + '"></span></button>';
    }).join('');
  }

  s.bgm.forEach(function (b) {
    var btn = document.getElementById('bgm-' + b.slot);
    var nm = document.getElementById('bgm-name-' + b.slot);
    if (btn) {
      if (btn.dataset.busy === '1') return;
      btn.classList.toggle('muted', !!b.playing);
      btn.classList.toggle('dim', !b.available);
      btn.title = b.available
        ? (b.file + (b.playing
            ? ' — playing' + (b.remaining != null ? ', stops in ' + b.remaining + 's' : '') +
              ' (click to stop)'
            : ' — click to loop'))
        : (b.file + ' not found in the audio directory');
    }
    if (nm) nm.textContent = b.file.replace(/\.[^.]+$/, '').substring(0, 11);
  });
}

function toggleBgm(slot) {
  var btn = document.getElementById('bgm-' + slot);
  if (btn) btn.dataset.busy = '1';
  fetch('/bgm', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({slot: slot, action: 'toggle'})
  })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (btn) btn.dataset.busy = '';
      if (!d.ok && d.error) _sbNotify(d.error, 'error');
      // Let the next /status poll paint the result — one source of truth.
    })
    .catch(function (e) {
      if (btn) btn.dataset.busy = '';
      _sbNotify('BGM failed: ' + e, 'error');
    });
}

// ── Announcer (repeating message over the BGM bed) ──────────────────────────
// Text persists server-side (~/.config/radio-gateway/announcer.json), so it
// survives restarts and is shared by every page.

function openAnnouncer() {
  fetch('/announcer')
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (!d.ok && d.error) { _sbNotify(d.error, 'error'); return; }
      _annDialog(d);
    })
    .catch(function (e) { _sbNotify('Announcer unavailable: ' + e, 'error'); });
}

function _annDialog(d) {
  var old = document.getElementById('ann-dlg');
  if (old) old.remove();
  var beds = d.beds && d.beds.length ? d.beds
           : [{slot: 1, file: 'bgm1'}, {slot: 2, file: 'bgm2'}, {slot: 3, file: 'bgm3'}];

  var voices = d.voices_available || [];
  var vopts = function (sel) {
    // Blank entry = "use the engine default", so a bed can opt out of a
    // specific voice without clearing its message.
    return '<option value="">— engine default —</option>' + voices.map(function (v) {
      return '<option value="' + _sbEsc(v.value) + '"' +
             (String(v.value) === String(sel) ? ' selected' : '') + '>' +
             _sbEsc(v.label) + '</option>';
    }).join('');
  };

  var rows = beds.map(function (b) {
    var txt = (d.messages && d.messages[String(b.slot)]) || '';
    var vc = (d.voices && d.voices[String(b.slot)]) || '';
    var now = d.playing_slot === b.slot ? ' <span class="ann-live">playing</span>' : '';
    return '<div class="ann-row">' +
             '<span class="ann-lbl">BGM ' + b.slot +
               '<span class="ann-file">' + _sbEsc(b.file || '') + '</span>' + now + '</span>' +
             '<textarea class="ctrl-input ann-msg" data-slot="' + b.slot + '" rows="2" ' +
               'maxlength="500" placeholder="Message spoken over this bed">' +
               _sbEsc(txt) + '</textarea>' +
             '<select class="ctrl-input ann-voice" data-slot="' + b.slot + '" ' +
               'aria-label="Voice for bed ' + b.slot + '">' + vopts(vc) + '</select>' +
           '</div>';
  }).join('');

  var dlg = document.createElement('dialog');
  dlg.id = 'ann-dlg';
  dlg.className = 'sb-dialog';
  dlg.innerHTML =
    '<h3 class="sb-title">Bed messages</h3>' +
    '<p class="sb-sub">Each bed has its own message and voice, spoken over it at ' +
      'a fixed interval while the bed plays. The music ducks but stays audible. ' +
      'Routed via the <strong>Announcer</strong> node in /routing.' +
      (d.tts_backend ? ' Voices shown are for the active <strong>' +
        _sbEsc(d.tts_backend) + '</strong> engine.' : '') + '</p>' +
    '<div class="ann-list">' + rows + '</div>' +
    '<div class="sb-bar" style="margin-top:var(--s-2)">' +
      '<label class="sb-sub" style="margin:0">Every</label>' +
      '<input id="ann-interval" class="ctrl-input" type="number" min="2" max="3600" ' +
        'step="1" style="width:5em">' +
      '<label class="sb-sub" style="margin:0">seconds</label>' +
      '<label class="sb-sub" style="margin:0 0 0 auto">' +
        '<input id="ann-enabled" type="checkbox"> Enabled</label>' +
    '</div>' +
    '<div class="sb-bar">' +
      '<label class="sb-sub" style="margin:0">Stop bed after</label>' +
      '<input id="ann-maxsecs" class="ctrl-input" type="number" min="0" max="86400" ' +
        'step="10" style="width:6em">' +
      '<label class="sb-sub" style="margin:0">seconds (0 = never)</label>' +
    '</div>' +
    '<div class="sb-foot">' +
      '<button type="button" class="sb-btn" id="ann-cancel">Cancel</button>' +
      '<button type="button" class="sb-btn sb-primary" id="ann-save">Save</button>' +
    '</div>';
  document.body.appendChild(dlg);

  dlg.querySelector('#ann-interval').value = d.interval || 10;
  // 0 is a real value (no cap), so don't fall through to a default on falsy.
  dlg.querySelector('#ann-maxsecs').value =
    (d.max_seconds === undefined || d.max_seconds === null) ? 120 : d.max_seconds;
  dlg.querySelector('#ann-enabled').checked = !!d.enabled;
  dlg.querySelector('#ann-cancel').onclick = function () { dlg.close(); };
  dlg.querySelector('#ann-save').onclick = function () {
    var msgs = {}, vcs = {};
    dlg.querySelectorAll('textarea.ann-msg').forEach(function (t) {
      msgs[t.dataset.slot] = t.value;
    });
    dlg.querySelectorAll('select.ann-voice').forEach(function (v) {
      vcs[v.dataset.slot] = v.value;
    });
    var btn = this;
    btn.disabled = true;
    btn.textContent = 'Saving…';
    fetch('/announcer', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        messages: msgs,
        voices: vcs,
        interval: parseFloat(dlg.querySelector('#ann-interval').value) || 10,
        max_seconds: Math.max(0, parseFloat(dlg.querySelector('#ann-maxsecs').value) || 0),
        enabled: dlg.querySelector('#ann-enabled').checked
      })
    })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        dlg.close();
        if (!res.ok) _sbNotify('Saved, but TTS failed: ' + (res.error || 'unknown'), 'error');
        else if (!res.enabled) _sbNotify('Messages saved (announcer off)');
        else if (res.playing_slot) _sbNotify('Announcing bed ' + res.playing_slot +
                                             ' every ' + res.interval + 's');
        else _sbNotify('Saved — starts when you play a bed');
      })
      .catch(function (e) { dlg.close(); _sbNotify('Save failed: ' + e, 'error'); });
  };
  dlg.addEventListener('close', function () { dlg.remove(); });
  dlg.showModal();
}
