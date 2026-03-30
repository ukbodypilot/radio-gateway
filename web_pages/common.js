/* Common utilities for all gateway pages — loaded from /pages/common.js */

// Theme object — populated from /theme endpoint
var _T = {};

// Fetch theme and apply CSS variables
function loadTheme() {
  return fetch('/theme')
    .then(function(r) { return r.json(); })
    .then(function(t) {
      _T = t;
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

// Auto-load theme on page load
loadTheme();
