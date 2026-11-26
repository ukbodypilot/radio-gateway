/* rig-apps.js 1.0.0 — the "Apps" menu in a .rig-top bar (canonical copy: radio-gateway web_pages/).
   RigApps.render(links, current): links = [{name, url}] from the app's own config; RigApps.load(jsonUrl, current) for static sites. Empty list = the menu stays
   hidden, so an app used on its own shows nothing. Inside another app's frame (the gateway shell) it also stays
   hidden: that shell already has the navigation. */
window.RigApps = (function () {
  function render(links, current) {
    const box = document.querySelector(".rig-apps");
    if (!box) return;
    const list = (links || []).filter((l) => l && l.name && l.url);
    let framed = false;
    try { framed = window.top !== window.self; } catch (e) { framed = true; }
    if (!list.length || framed) { box.hidden = true; return; }
    box.innerHTML = '<button type="button" aria-haspopup="true" aria-expanded="false">Apps</button><div class="rig-apps-menu" role="menu"></div>';
    const btn = box.firstChild, menu = box.lastChild;
    for (const l of list) {
      const a = document.createElement("a");
      a.href = l.url; a.textContent = l.name; a.setAttribute("role", "menuitem");
      if (current && l.name.toLowerCase() === String(current).toLowerCase()) a.setAttribute("aria-current", "page");
      menu.appendChild(a);
    }
    const set = (open) => { box.classList.toggle("open", open); btn.setAttribute("aria-expanded", String(open)); };
    btn.addEventListener("click", (e) => { e.stopPropagation(); set(!box.classList.contains("open")); });
    document.addEventListener("click", () => set(false));
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") set(false); });
    box.hidden = false;
  }
  // Static sites: read the links from a JSON file ([{name, url}] or {"apps": [...]}); missing file = no menu.
  function load(url, current) {
    fetch(url, { cache: "no-cache" }).then((r) => (r.ok ? r.json() : [])).catch(() => [])
      .then((j) => render(Array.isArray(j) ? j : (j && j.apps) || [], current));
  }
  return { render, load };
})();
