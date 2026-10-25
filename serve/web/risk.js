"use strict";
/* A setting that raises a risk says so, in a highlighted label wherever it is on, and asks twice when it is set: first that the risk is understood,
   then that the message just read was read carefully and the risk understood. Nothing else: the person decides. Text is put in the page as text.

     riskLabel("what can happen")  -> an element to put beside the setting
     riskConfirm(["what can happen", ...]) -> a promise: true when both questions were answered yes */
(() => {
  const h = (tag, cls, ...kids) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    kids.flat().forEach(k => { if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k))); });
    return e;
  };

  window.riskLabel = text => h("p", "risk-label", h("b", "", T("rk.label")), h("span", "", text));

  window.riskConfirm = lines => new Promise(resolve => {
    const dlg = h("dialog", "risk-dialog");
    dlg.setAttribute("aria-labelledby", "rk-title");
    let answered = false;
    const finish = value => { if (answered) return; answered = true; dlg.close(); dlg.remove(); resolve(value); };
    const page = (title, button) => {
      const heading = h("h2", "", title); heading.id = "rk-title";
      const yes = h("button", "btn primary", button); yes.type = "button";
      const no = h("button", "btn ghost", T("rk.cancel")); no.type = "button";
      no.onclick = () => finish(false);
      return { heading, yes, no, body: h("div", "risk-body", [].concat(lines).map(l => riskLabel(l))) };
    };
    const second = () => {
      const p = page(T("rk.second"), T("rk.confirm2"));
      p.yes.onclick = () => finish(true);
      dlg.replaceChildren(h("div", "risk-box", p.heading, p.body, h("div", "risk-actions", p.no, p.yes)));
      p.no.focus();
    };
    const first = page(T("rk.title"), T("rk.understand"));
    first.yes.onclick = second;
    dlg.replaceChildren(h("div", "risk-box", first.heading, first.body, h("div", "risk-actions", first.no, first.yes)));
    dlg.addEventListener("cancel", () => finish(false));
    document.body.append(dlg);
    dlg.showModal();
    first.no.focus();
  });
})();
