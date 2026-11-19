"use strict";
/* A small right-click menu, reusable anywhere in the GUI: openContextMenu(x, y, items), items = [{label, onClick}]. Closes on
   Escape, a click outside, or choosing an item; the arrow keys move between items, Enter or Space activates one. */
(() => {
  let menu = null;

  function close() {
    if (!menu) return;
    menu.remove();
    menu = null;
    document.removeEventListener("keydown", onKey, true);
    document.removeEventListener("pointerdown", onOutside, true);
  }
  function onKey(e) {
    if (!menu) return;
    const items = [...menu.querySelectorAll("button")];
    const i = items.indexOf(document.activeElement);
    if (e.key === "Escape") { e.preventDefault(); close(); return; }
    if (e.key === "ArrowDown") { e.preventDefault(); (items[i + 1] || items[0]).focus(); }
    if (e.key === "ArrowUp") { e.preventDefault(); (items[i - 1] || items[items.length - 1]).focus(); }
  }
  function onOutside(e) { if (menu && !menu.contains(e.target)) close(); }

  window.openContextMenu = (x, y, items) => {
    close();
    menu = document.createElement("div");
    menu.className = "ctxmenu";
    menu.setAttribute("role", "menu");
    for (const it of items) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "ctxmenu-item";
      b.textContent = it.label;
      b.setAttribute("role", "menuitem");
      b.onclick = () => { close(); it.onClick(); };
      menu.append(b);
    }
    document.body.append(menu);
    const vw = window.innerWidth, vh = window.innerHeight;
    const rect = menu.getBoundingClientRect();
    menu.style.left = Math.max(4, Math.min(x, vw - rect.width - 4)) + "px";
    menu.style.top = Math.max(4, Math.min(y, vh - rect.height - 4)) + "px";
    const first = menu.querySelector("button");
    if (first) first.focus();
    document.addEventListener("keydown", onKey, true);
    setTimeout(() => document.addEventListener("pointerdown", onOutside, true), 0);
  };
  window.closeContextMenu = close;
})();
