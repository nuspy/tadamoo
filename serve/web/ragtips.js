"use strict";
/* Knowledge: a suggestion on every control. A button gets the sentence that says what it does (the table below, by the key of its label); a field gets the help written under it, or its
   label. Suggestions are only added where there is none yet, whenever the page is drawn again. On a touch screen a long press shows them and the help text under each field stays on the page. */
(() => {
  const TIPS = {
  "rg.new": "rt.new",
  "rg.create": "rt.create",
  "rg.cancel": "rt.cancel",
  "rg.save": "rt.save",
  "rg.add.path.btn": "rt.addpath",
  "rg.browse": "rt.browse",
  "rg.browse.go": "rt.browsego",
  "rg.browse.up": "rt.browseup",
  "rg.browse.addfiles": "rt.browseadd",
  "rg.browse.follow": "rt.browsefollow",
  "rg.browse.close": "rt.browseclose",
  "rg.doc.open": "rt.docopen",
  "rg.doc.save": "rt.docsave",
  "rg.fig.more": "rt.figmore",
  "rg.fig.close": "rt.figclose",
  "rg.index.again": "rt.indexagain",
  "rg.job.cancel": "rt.jobcancel",
  "rg.try.go": "rt.trygo",
  "rg.sem.test": "rt.semtest",
  "rg.proj.new": "rt.pnew",
  "rg.proj.create": "rt.pcreate",
  "rg.proj.save": "rt.psave",
  "rg.proj.continue": "rt.pcontinue",
  "rg.proj.delete.go": "rt.pdelete",
  "rs.check": "rt.check",
  "rs.update.all": "rt.updateall",
  "rs.what": "rt.what",
  "rs.gone.keep": "rt.gonekeep",
  "rs.source.add": "rt.sourceadd",
  "rs.verify": "rt.verify",
  "rs.tree.again": "rt.treeagain",
  "rs.tree.f.short": "rt.treefreq",
  "rdc.search": "rt.dsearch",
  "rdc.widen": "rt.dwiden",
  "rdc.addsel": "rt.daddsel",
  "rdc.addall": "rt.daddall",
  "rdc.cancel": "rt.dcancel",
  "rd.again": "rt.dbagain",
  "rd.comp.go": "rt.dbcomp",
  "rd.install.local": "rt.dbinstall",
  "rd.planb.go": "rt.dbplanb",
  "rd.reset": "rt.dbreset",
  "ri.usefolder": "rt.imgfolder",
};
  const BY_ID = { "rs-rm-": "rt.remove", "rg-rm-": "rt.remove", "rg-fig-": "rt.docfigs", "rg-open-": "rt.docopen", "rg-save-": "rt.docsave" };   // buttons made one for each document or node
  const MARK = "\u0001";
  const patterns = () => Object.entries(TIPS).map(([label, tip]) => {
    const [pre, post] = T(label, MARK).split(MARK);
    return { pre, post: post === undefined ? null : post, tip };
  });
  const text = e => (e.textContent || "").trim();
  function apply(root) {
    const pats = patterns();
    root.querySelectorAll("button").forEach(b => {
      if (b.title) return;
      const own = text(b);
      const hit = pats.find(p => (p.post === null ? own === p.pre : own.startsWith(p.pre) && own.endsWith(p.post)));
      const byId = Object.keys(BY_ID).find(k => (b.id || "").startsWith(k));
      const where = b.closest(".rg-projects") ? "rt.pickproject" : b.closest(".rg-side") && b.classList.contains("rg-pick") ? "rt.pickarchive" : "";
      const tip = hit ? T(hit.tip) : byId ? T(BY_ID[byId]) : where ? T(where) : (b.getAttribute("aria-label") || own);
      if (tip) b.title = tip;
    });
    root.querySelectorAll("input, select, textarea").forEach(i => {
      if (i.title || i.type === "hidden") return;
      const field = i.closest(".rg-field");
      const help = field && field.querySelector(".rg-help");
      const lab = i.id && document.querySelector(`label[for="${CSS.escape(i.id)}"]`);
      const tip = (help && text(help)) || (lab && text(lab)) || i.getAttribute("aria-label") || i.placeholder || "";
      if (tip) i.title = tip;
    });
  }
  let queued = false;
  function watch() {
    const roots = () => [document.getElementById("rg-root"), ...document.querySelectorAll("dialog.rg-dlg")].filter(Boolean);   // the page and the windows opened over it
    const run = () => { queued = false; roots().forEach(apply); };
    new MutationObserver(() => { if (!queued) { queued = true; setTimeout(run, 30); } }).observe(document.body, { childList: true, subtree: true });
    run();
  }
  window.ragTips = { apply };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", watch); else watch();
})();
