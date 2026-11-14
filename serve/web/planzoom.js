"use strict";
/* Zoom and pan of the plan diagram (serve/web/plans.js). The diagram is an SVG drawn at its layout size inside a scrolling frame, so zooming is the size the SVG is drawn at
   (the viewBox stays) and panning is the frame's scroll; node drag, links and the palette read the screen matrix of the SVG and follow the scale by themselves.

   Mouse: the wheel zooms around the pointer while it is over the diagram (the page does not scroll then; Ctrl + wheel, a trackpad pinch, does the same); the MIDDLE button held pans, the diagram moving with the
   pointer. The cursor is an open hand over the empty background (CSS) and a closed hand while panning. Touch: two fingers pinch-zoom around their middle, one finger on the
   empty background pans; a finger on a step still drags the step. Keyboard: + and - zoom, 0 puts it back to 100%. A bar with minus, the zoom in percent (which also resets) and plus. */
(() => {
  const ZMIN = 0.25, ZMAX = 3, STEP = 1.25, WHEEL = 0.0015, MOVE = 4;
  const clamp = z => Math.min(ZMAX, Math.max(ZMIN, z));
  const mk = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const SKIP = ".pl-node, .pl-pg, .pl-edgehit, .pl-x, .pl-port, .pl-hit";                // what is on the diagram: a press there is not on the empty background

  /* `hooks.svg()` gives the SVG now drawn, `hooks.size()` its layout size {width, height} in units; `hooks.busy()` says a step or a link is being dragged */
  function attach(frame, hooks) {
    let zoom = 1, pan = null;
    const touches = new Map();                                                       // touch pointers down on the empty background or anywhere: id -> {x, y, onStep}
    let pinch = null, swipe = null;
    const bar = mk("div", "pl-zoom"), minus = mk("button", "pl-zb pl-zout", "−"), reset = mk("button", "pl-zb pl-zreset"), plus = mk("button", "pl-zb pl-zin", "+"), fitBtn = mk("button", "pl-zb pl-zfit", "⤢");
    bar.setAttribute("role", "group");
    for (const b of [minus, reset, plus, fitBtn]) b.type = "button";
    bar.append(minus, reset, plus, fitBtn);
    const words = () => ({ in: T("pl.zoom.in"), out: T("pl.zoom.out"), reset: T("pl.zoom.reset"), label: T("pl.zoom.label"), fit: T("pl.zoom.fit") });
    function paintBar() {
      const w = words(), pct = Math.round(zoom * 100);
      bar.setAttribute("aria-label", w.label);
      minus.setAttribute("aria-label", w.out); minus.title = w.out; minus.disabled = zoom <= ZMIN + 1e-6;
      plus.setAttribute("aria-label", w.in); plus.title = w.in; plus.disabled = zoom >= ZMAX - 1e-6;
      reset.textContent = pct + "%"; reset.setAttribute("aria-label", `${w.reset} (${pct}%)`); reset.title = w.reset;
      fitBtn.setAttribute("aria-label", w.fit); fitBtn.title = w.fit;
    }
    /* the SVG at the zoom in force: drawn at its layout size times the zoom */
    function size(svgEl) {
      const s = hooks.size();
      if (!svgEl || !s) return;
      svgEl.setAttribute("width", String(Math.round(s.width * zoom))); svgEl.setAttribute("height", String(Math.round(s.height * zoom)));
    }
    /* the new zoom around a point of the screen (the middle of the frame when none is given): the part of the diagram under it stays under it */
    function set(z, cx, cy) {
      z = clamp(z);
      const svgEl = hooks.svg(), old = zoom;
      if (Math.abs(z - old) < 1e-4) { paintBar(); return zoom; }
      const box = frame.getBoundingClientRect();
      if (cx == null) cx = box.left + box.width / 2;
      if (cy == null) cy = box.top + box.height / 2;
      let ux = 0, uy = 0, ox = 0, oy = 0;
      if (svgEl) {
        const r = svgEl.getBoundingClientRect();
        ux = (cx - r.left) / old; uy = (cy - r.top) / old;                            // the point in layout units
        ox = r.left + frame.scrollLeft; oy = r.top + frame.scrollTop;                  // where the diagram starts, as if not scrolled
      }
      zoom = z;
      size(svgEl);
      if (svgEl) { frame.scrollLeft = ox + ux * z - cx; frame.scrollTop = oy + uy * z - cy; }
      paintBar();
      frame.dispatchEvent(new CustomEvent("plan:zoom", { detail: { zoom } }));
      return zoom;
    }
    /* the zoom at which the whole diagram is in view (never above 100%), from its top left corner */
    function fit(floor) {
      const s = hooks.size();
      if (!s) return zoom;
      const css = getComputedStyle(frame), padX = (parseFloat(css.paddingLeft) || 0) + (parseFloat(css.paddingRight) || 0), padY = (parseFloat(css.paddingTop) || 0) + (parseFloat(css.paddingBottom) || 0);
      const roomW = Math.max(60, frame.clientWidth - padX), roomH = Math.max(80, (frame.clientHeight || Math.min(window.innerHeight * 0.4, 400)) - padY);
      zoom = clamp(Math.max(floor || 0, Math.min(1, roomW / s.width, roomH / s.height)));       // `floor`: a plan that opens is not shrunk past what can be read
      size(hooks.svg());
      frame.scrollLeft = 0; frame.scrollTop = 0;
      paintBar();
      frame.dispatchEvent(new CustomEvent("plan:zoom", { detail: { zoom } }));
      return zoom;
    }
    const by = (f, cx, cy) => set(zoom * f, cx, cy);
    minus.onclick = () => by(1 / STEP); plus.onclick = () => by(STEP); reset.onclick = () => set(1); fitBtn.onclick = () => fit();

    /* the wheel, alone or with Ctrl (a trackpad pinch is the same event): zoom around the pointer, only while it is over the diagram, so the page does not scroll then */
    frame.addEventListener("wheel", e => {
      e.preventDefault();
      const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1;
      by(Math.exp(-e.deltaY * unit * WHEEL), e.clientX, e.clientY);
    }, { passive: false });

    /* the middle button: the diagram follows the pointer */
    const endPan = () => { if (!pan) return; frame.classList.remove("pl-panning"); try { frame.releasePointerCapture(pan.id); } catch (e) { /* already let go */ } pan = null; };
    frame.addEventListener("pointerdown", e => {
      if (e.pointerType === "mouse" && e.button === 1) {
        e.preventDefault();
        pan = { id: e.pointerId, x: e.clientX, y: e.clientY, l: frame.scrollLeft, t: frame.scrollTop };
        try { frame.setPointerCapture(e.pointerId); } catch (err) { /* the pointer is gone */ }
        frame.classList.add("pl-panning");
        return;
      }
      if (e.pointerType === "touch") touchDown(e);
    });
    frame.addEventListener("mousedown", e => { if (e.button === 1) e.preventDefault(); });           // no autoscroll circle
    frame.addEventListener("auxclick", e => { if (e.button === 1) e.preventDefault(); });
    frame.addEventListener("pointermove", e => {
      if (pan && e.pointerId === pan.id) { frame.scrollLeft = pan.l - (e.clientX - pan.x); frame.scrollTop = pan.t - (e.clientY - pan.y); return; }
      if (e.pointerType === "touch") touchMove(e);
    });
    for (const type of ["pointerup", "pointercancel", "lostpointercapture"]) {
      frame.addEventListener(type, e => {
        if (pan && e.pointerId === pan.id) endPan();
        if (e.pointerType === "touch" && type !== "lostpointercapture") touchUp(e);
      });
    }

    /* touch: a finger on the empty background pans, two fingers pinch; a finger that went down on a step is the step's (it drags) */
    function touchDown(e) {
      const onStep = !!(e.target.closest && e.target.closest(SKIP));
      touches.set(e.pointerId, { x: e.clientX, y: e.clientY, onStep });
      if (!onStep) { try { frame.setPointerCapture(e.pointerId); } catch (err) { /* the pointer is gone */ } }          // the end of the touch reaches the frame even when the diagram is redrawn under the finger
      const free = [...touches.values()].filter(t => !t.onStep);
      if (touches.size === 2 && free.length === 2 && !hooks.busy()) {
        const [a, b] = [...touches.values()];
        pinch = { d: Math.hypot(a.x - b.x, a.y - b.y) || 1, z: zoom };
        swipe = null;
        frame.classList.add("pl-panning");
      } else if (touches.size === 1 && free.length === 1) {
        swipe = { id: e.pointerId, x: e.clientX, y: e.clientY, l: frame.scrollLeft, t: frame.scrollTop, moved: false };
      }
    }
    function touchMove(e) {
      const t = touches.get(e.pointerId);
      if (!t) return;
      t.x = e.clientX; t.y = e.clientY;
      if (pinch && touches.size === 2) {
        const [a, b] = [...touches.values()], d = Math.hypot(a.x - b.x, a.y - b.y) || 1;
        set(pinch.z * d / pinch.d, (a.x + b.x) / 2, (a.y + b.y) / 2);
        e.preventDefault();
      } else if (swipe && swipe.id === e.pointerId) {
        const dx = e.clientX - swipe.x, dy = e.clientY - swipe.y;
        if (!swipe.moved && Math.hypot(dx, dy) < MOVE) return;
        swipe.moved = true;
        frame.scrollLeft = swipe.l - dx; frame.scrollTop = swipe.t - dy;
      }
    }
    function touchUp(e) {
      touches.delete(e.pointerId);
      if (swipe && swipe.id === e.pointerId) swipe = null;
      if (touches.size < 2 && pinch) { pinch = null; frame.classList.remove("pl-panning"); }
    }

    /* the keyboard, from a step or the frame that has the focus: + and - zoom, 0 puts it back; a key with Ctrl, Alt or the system key is the browser's */
    frame.addEventListener("keydown", e => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.key === "+" || e.key === "=") { e.preventDefault(); by(STEP); }
      else if (e.key === "-" || e.key === "_") { e.preventDefault(); by(1 / STEP); }
      else if (e.key === "0") { e.preventDefault(); set(1); }
    });
    bar.addEventListener("keydown", e => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.key === "0") { e.preventDefault(); set(1); }
    });
    paintBar();
    return { bar, size, set, by, fit, paint: paintBar, get zoom() { return zoom; }, get panning() { return !!pan || !!pinch; } };
  }
  window.PlanZoom = { attach, ZMIN, ZMAX };
})();
