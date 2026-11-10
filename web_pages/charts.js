/* Small chart kit for the gateway pages — loaded from /pages/charts.js.
   No dependencies. Everything hangs off window.RGC.

     RGC.prom(query, startSec, endSec, stepSec)  -> Promise<[{metric, points:[[ms, v]]}]>
     RGC.rolling(maxPoints)                      -> {push(v), points()}   (history since page load)
     RGC.spark(el, points, opts)                 line + soft area, hover crosshair
     RGC.strip(el, cfg)                          one-row timeline (activity / up-down / status)
     RGC.bars(el, bins, opts)                    histogram, hover + optional click
     RGC.timeAxis(el, startMs, endMs)            tick labels under a strip or bar chart

   Conventions: one series per chart, colour from the theme tokens, every
   chart has a hover readout, and state is never colour-only (the readout
   and the row label always say it in words). */
(function () {
  'use strict';
  var RGC = window.RGC = {};

  // ── helpers ────────────────────────────────────────────────────────────
  RGC.css = function (name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || '#888';
  };
  function p2(n) { return String(n).padStart(2, '0'); }
  var MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  RGC.fmtClock = function (ms, secs) {
    var d = new Date(ms);
    return p2(d.getHours()) + ':' + p2(d.getMinutes()) + (secs ? ':' + p2(d.getSeconds()) : '');
  };
  RGC.fmtDay = function (ms) { var d = new Date(ms); return MON[d.getMonth()] + ' ' + p2(d.getDate()); };
  RGC.fmtStamp = function (ms) { return RGC.fmtDay(ms) + ' ' + RGC.fmtClock(ms); };
  RGC.esc = function (t) {
    return String(t == null ? '' : t).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  };

  // ── tooltip (one shared element) ───────────────────────────────────────
  var _tip = null;
  function tipEl() {
    if (_tip) return _tip;
    _tip = document.createElement('div');
    _tip.className = 'rgc-tip';
    _tip.setAttribute('role', 'status');
    document.body.appendChild(_tip);
    return _tip;
  }
  RGC.tip = {
    show: function (html, clientX, clientY) {
      var t = tipEl();
      t.innerHTML = html;
      t.style.display = 'block';
      var w = t.offsetWidth, h = t.offsetHeight;
      var x = clientX + 12, y = clientY - h - 10;
      if (x + w > window.innerWidth - 4) x = clientX - w - 12;
      if (x < 4) x = 4;
      if (y < 4) y = clientY + 14;
      t.style.left = x + 'px';
      t.style.top = y + 'px';
    },
    hide: function () { if (_tip) _tip.style.display = 'none'; }
  };

  // ── data sources ───────────────────────────────────────────────────────
  // Prometheus range query through the gateway's own /prometheus/ proxy.
  // Resolves to [] on any failure so callers can fall back without a catch.
  RGC.prom = function (query, startSec, endSec, stepSec) {
    var url = '/prometheus/api/v1/query_range?query=' + encodeURIComponent(query)
      + '&start=' + Math.floor(startSec) + '&end=' + Math.floor(endSec) + '&step=' + Math.max(1, Math.round(stepSec));
    return fetch(url)
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        var res = (j && j.data && j.data.result) || [];
        return res.map(function (r) {
          var pts = [];
          for (var i = 0; i < r.values.length; i++) {
            var v = parseFloat(r.values[i][1]);
            if (isFinite(v)) pts.push([r.values[i][0] * 1000, v]);
          }
          return { metric: r.metric || {}, points: pts };
        });
      })
      .catch(function () { return []; });
  };

  // History the browser collects itself — used where the gateway keeps none.
  RGC.rolling = function (maxPoints) {
    var pts = [];
    return {
      push: function (v) {
        if (!isFinite(v)) return;
        pts.push([Date.now(), v]);
        if (pts.length > maxPoints) pts.shift();
      },
      points: function () { return pts; }
    };
  };

  // ── sparkline ──────────────────────────────────────────────────────────
  // points: [[ms, v], ...]   opts: {fmt, color, min, max, minSpan}
  // The element keeps its own state, so calling spark() again just redraws.
  RGC.spark = function (el, points, opts) {
    opts = opts || {};
    if (!el) return;
    var st = el._rgc;
    if (!st) {
      st = el._rgc = {};
      el.classList.add('rgc-spark');
      el.innerHTML = '<svg viewBox="0 0 300 40" preserveAspectRatio="none" aria-hidden="true"></svg>'
        + '<span class="rgc-cross"></span><span class="rgc-dot"></span>';
      st.svg = el.firstChild; st.cross = el.children[1]; st.dot = el.children[2];
      var move = function (ev) {
        var pts = st.points;
        if (!pts || pts.length < 2) return;
        var r = el.getBoundingClientRect();
        var f = Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width));
        var t = st.t0 + f * (st.t1 - st.t0), best = 0, bd = Infinity;
        for (var i = 0; i < pts.length; i++) {
          var d = Math.abs(pts[i][0] - t);
          if (d < bd) { bd = d; best = i; }
        }
        var px = (pts[best][0] - st.t0) / (st.t1 - st.t0) * r.width;
        var py = st.y(pts[best][1]) / 40 * r.height;
        st.cross.style.display = st.dot.style.display = 'block';
        st.cross.style.left = px + 'px';
        st.dot.style.left = px + 'px'; st.dot.style.top = py + 'px';
        RGC.tip.show('<b>' + RGC.esc(st.fmt(pts[best][1])) + '</b><span>' + RGC.fmtClock(pts[best][0], true) + '</span>',
          r.left + px, r.top + py);
      };
      var leave = function () { st.cross.style.display = st.dot.style.display = 'none'; RGC.tip.hide(); };
      el.addEventListener('pointermove', move);
      el.addEventListener('pointerdown', move);
      el.addEventListener('pointerleave', leave);
      el.addEventListener('pointercancel', leave);
    }
    st.points = points;
    st.fmt = opts.fmt || function (v) { return String(Math.round(v * 10) / 10); };
    el.style.color = opts.color || 'var(--t-accent)';
    if (!points || points.length < 2) {
      st.svg.innerHTML = '';
      el.classList.add('rgc-empty');
      return;
    }
    el.classList.remove('rgc-empty');
    var lo = Infinity, hi = -Infinity, i;
    for (i = 0; i < points.length; i++) { lo = Math.min(lo, points[i][1]); hi = Math.max(hi, points[i][1]); }
    if (opts.min !== undefined) lo = Math.min(lo, opts.min);
    if (opts.max !== undefined) hi = Math.max(hi, opts.max);
    // A flat series draws mid-height instead of collapsing onto an edge.
    var span = Math.max(hi - lo, opts.minSpan || 0);
    if (span <= 0) span = Math.abs(hi) || 1;
    if (hi - lo < span) { var mid = (hi + lo) / 2; lo = mid - span / 2; hi = mid + span / 2; if (opts.min !== undefined && lo < opts.min) { hi += opts.min - lo; lo = opts.min; } }
    st.t0 = points[0][0]; st.t1 = points[points.length - 1][0];
    if (st.t1 <= st.t0) st.t1 = st.t0 + 1;
    st.y = function (v) { return 37 - (v - lo) / (hi - lo) * 34; };
    var line = [];
    for (i = 0; i < points.length; i++) {
      line.push(((points[i][0] - st.t0) / (st.t1 - st.t0) * 300).toFixed(1) + ',' + st.y(points[i][1]).toFixed(1));
    }
    st.svg.innerHTML = '<polygon points="0,40 ' + line.join(' ') + ' 300,40" fill="currentColor" opacity="0.12"/>'
      + '<polyline points="' + line.join(' ') + '" fill="none" stroke="currentColor" stroke-width="1.5"'
      + ' stroke-linejoin="round" vector-effect="non-scaling-stroke"/>';
  };

  // ── strip (one-row timeline) ───────────────────────────────────────────
  // cfg: { start, end (ms), bins: [...], paint: function(bin) -> {color, alpha} | null,
  //        describe: function(bin) -> text }
  // Bins are equal-width slices of start..end; null/undefined bin = no data.
  RGC.strip = function (el, cfg) {
    if (!el) return;
    var st = el._rgc;
    if (!st) {
      st = el._rgc = {};
      el.classList.add('rgc-strip');
      el.innerHTML = '<canvas></canvas><span class="rgc-cross"></span>';
      st.cv = el.firstChild; st.cross = el.children[1];
      var move = function (ev) {
        var c = st.cfg; if (!c || !c.bins.length) return;
        var r = el.getBoundingClientRect();
        var f = Math.min(0.99999, Math.max(0, (ev.clientX - r.left) / r.width));
        var i = Math.floor(f * c.bins.length);
        var w = (c.end - c.start) / c.bins.length, t0 = c.start + i * w;
        st.cross.style.display = 'block';
        st.cross.style.left = (f * r.width) + 'px';
        var txt = c.bins[i] == null ? 'no data' : c.describe(c.bins[i]);
        RGC.tip.show('<b>' + RGC.esc(txt) + '</b><span>' + RGC.fmtClock(t0) + '–' + RGC.fmtClock(t0 + w) + '</span>',
          ev.clientX, r.top);
      };
      var leave = function () { st.cross.style.display = 'none'; RGC.tip.hide(); };
      el.addEventListener('pointermove', move);
      el.addEventListener('pointerdown', move);
      el.addEventListener('pointerleave', leave);
      el.addEventListener('pointercancel', leave);
      if (window.ResizeObserver) new ResizeObserver(function () { draw(st, el); }).observe(el);
    }
    st.cfg = cfg;
    draw(st, el);
  };
  function draw(st, el) {
    var c = st.cfg; if (!c) return;
    var r = el.getBoundingClientRect();
    if (!r.width) return;
    var dpr = window.devicePixelRatio || 1;
    var W = Math.round(r.width * dpr), H = Math.round(r.height * dpr);
    if (st.cv.width !== W || st.cv.height !== H) { st.cv.width = W; st.cv.height = H; }
    var g = st.cv.getContext('2d');
    g.clearRect(0, 0, W, H);
    var n = c.bins.length, bw = W / n, cache = {};
    for (var i = 0; i < n; i++) {
      if (c.bins[i] == null) continue;
      var p = c.paint(c.bins[i]);
      if (!p) continue;
      g.globalAlpha = p.alpha === undefined ? 1 : p.alpha;
      g.fillStyle = cache[p.color] || (cache[p.color] = p.color.charAt(0) === '-' ? RGC.css(p.color) : p.color);
      var x0 = Math.floor(i * bw), x1 = Math.max(x0 + 1, Math.floor((i + 1) * bw));
      g.fillRect(x0, 0, x1 - x0, H);
    }
    g.globalAlpha = 1;
  }

  // ── histogram ──────────────────────────────────────────────────────────
  // bins: [{v, label, sub, sel}]   opts: {color, onClick(bin, i), fmt}
  RGC.bars = function (el, bins, opts) {
    opts = opts || {};
    if (!el) return;
    el.classList.add('rgc-bars');
    var max = 0, i;
    for (i = 0; i < bins.length; i++) max = Math.max(max, bins[i].v || 0);
    var h = '';
    for (i = 0; i < bins.length; i++) {
      var pct = max ? (bins[i].v || 0) / max * 100 : 0;
      h += '<span class="rgc-bar' + (bins[i].sel ? ' sel' : '') + (opts.onClick ? ' click' : '') + '" data-i="' + i + '">'
        + '<i style="height:' + (bins[i].v ? Math.max(pct, 3) : 0).toFixed(1) + '%"></i></span>';
    }
    el.innerHTML = h;
    el.style.color = opts.color || 'var(--t-accent)';
    if (el._rgcBound) { el._rgcBins = bins; el._rgcOpts = opts; return; }
    el._rgcBound = true; el._rgcBins = bins; el._rgcOpts = opts;
    var at = function (ev) {
      var b = ev.target.closest('.rgc-bar');
      return b ? parseInt(b.getAttribute('data-i'), 10) : -1;
    };
    var move = function (ev) {
      var i = at(ev); if (i < 0) { RGC.tip.hide(); return; }
      var b = el._rgcBins[i], f = el._rgcOpts.fmt || function (v) { return String(v); };
      var r = ev.target.closest('.rgc-bar').getBoundingClientRect();
      RGC.tip.show('<b>' + RGC.esc(f(b.v || 0)) + '</b><span>' + RGC.esc(b.label || '') + '</span>', r.left + r.width / 2, el.getBoundingClientRect().top);
    };
    el.addEventListener('pointermove', move);
    el.addEventListener('pointerdown', move);
    el.addEventListener('pointerleave', RGC.tip.hide);
    el.addEventListener('click', function (ev) {
      var i = at(ev);
      if (i >= 0 && el._rgcOpts.onClick) el._rgcOpts.onClick(el._rgcBins[i], i);
    });
  };

  // ── loop-recorder activity ─────────────────────────────────────────────
  // Resolves to one entry per recorded bus: {id, name, freq, bins, busyPct}.
  // bins[k] = loudest stored peak (0-100) in that slice, undefined = nothing
  // recorded. Used by the SDR page; history depth = the recorder's retention.
  RGC.loopActivity = function (startMs, endMs, nBins) {
    var binMs = (endMs - startMs) / nBins;
    return fetch('/loop/buses').then(function (r) { return r.json(); }).then(function (buses) {
      return Promise.all((buses || []).map(function (b) {
        return fetch('/loop/waveform?bus=' + encodeURIComponent(b.id) + '&start=' + (startMs / 1000) + '&end=' + (endMs / 1000))
          .then(function (r) { return r.json(); }).then(function (w) {
            var bins = new Array(nBins), busy = 0, total = 0, res = (w.resolution || 1) * 1000, pk = w.peaks || [];
            for (var i = 0; i < pk.length; i++) {
              var k = Math.floor((w.start * 1000 + i * res - startMs) / binMs);
              if (k < 0 || k >= nBins) continue;
              if (bins[k] === undefined || pk[i] > bins[k]) bins[k] = pk[i];
              total++; if (pk[i] > 0) busy++;
            }
            return { id: b.id, name: b.name || b.id, freq: b.freq || '', bins: bins, busyPct: total ? busy / total * 100 : null };
          }).catch(function () { return null; });
      }));
    }).then(function (rows) { return rows.filter(Boolean); }).catch(function () { return []; });
  };

  // ── time axis ──────────────────────────────────────────────────────────
  RGC.timeAxis = function (el, startMs, endMs) {
    if (!el) return;
    el.classList.add('rgc-axis');
    var span = endMs - startMs, H = 3600000;
    var step = span <= 2 * H ? H / 4 : span <= 8 * H ? H : span <= 30 * H ? 4 * H : 24 * H;
    var h = '', d = new Date(startMs);
    // Snap to local wall-clock multiples of the step.
    d.setMinutes(0, 0, 0);
    if (step >= H) d.setHours(Math.floor(d.getHours() / (step / H)) * (step / H));
    for (var t = d.getTime(); t <= endMs; t += step) {
      if (t < startMs) continue;
      var f = (t - startMs) / span * 100;
      if (f > 97) continue;
      h += '<span style="left:' + f.toFixed(2) + '%">' + (step >= 24 * H ? RGC.fmtDay(t) : RGC.fmtClock(t)) + '</span>';
    }
    el.innerHTML = h;
  };
})();
