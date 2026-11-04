"use strict";
/* The Canary tab: the page of the Openweight Canary (results by level with search and filters, conversations as chats, catalogs,
   settings, start and stop) in a frame of this window. The page is served by this server under /canary/; the strip under the tab
   chooses its section, and the key and the colours of this window are handed to it. */
(function () {
  const root = document.getElementById("view-canary");
  if (!root) return;
  let frame = null, section = "results";

  // the window's own colours decide, whatever way it chose them
  const dark = () => {
    const c = getComputedStyle(document.body).backgroundColor.match(/\d+/g) || [255, 255, 255];
    return (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) < 128;
  };
  const tell = msg => { if (frame && frame.contentWindow) frame.contentWindow.postMessage({ canary: msg }, location.origin); };

  function open() {
    if (frame) return;
    frame = document.createElement("iframe");
    frame.title = T("tab.canary");
    frame.src = "/canary/?embed=1&section=" + section;
    frame.addEventListener("load", () => tell({ key: typeof apiKey === "string" ? apiKey : "", theme: dark() ? "dark" : "light", section }));
    root.append(frame);
  }

  document.addEventListener("tadamoo:view", e => { if (e.detail === "canary") open(); });
  document.addEventListener("tadamoo:sub", e => {
    if (typeof e.detail !== "string" || !e.detail.startsWith("canary:")) return;
    section = e.detail.slice(7);
    tell({ section });
  });
  if (typeof viewOn === "string" && viewOn === "canary") open();
})();
