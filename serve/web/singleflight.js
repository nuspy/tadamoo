"use strict";
/* One run at a time: a call made while the function is still running is not started beside it, it is run once more when the first one ends. */
(function (root) {
  function singleFlight(fn) {
    let running = false, again = null;
    const call = async (...args) => {
      if (running) { again = args; return; }
      running = true;
      try { await fn(...args); }
      finally { running = false; }
      if (again) { const next = again; again = null; await call(...next); }
    };
    return call;
  }
  root.singleFlight = singleFlight;
  if (typeof module !== "undefined" && module.exports) module.exports = singleFlight;
})(typeof window !== "undefined" ? window : globalThis);
