/* Canvas view — live telemetry and execution graph fed by /metrics. */
(function () {
  "use strict";

  const POLL_MS = 2500;
  const FIELDS = [
    "hit_rate", "prompt_ms", "decode_tok_s", "pcie_share",
    "drafts_offered", "drafts_accepted", "prompt_tokens", "output_tokens"
  ];

  class CanvasPanel {
    constructor(rootId) {
      this.root = (typeof document !== "undefined") ? document.getElementById(rootId) : null;
      this.history = [];
      this.maxPoints = 20;
      this.timer = null;
    }

    async fetchMetrics() {
      try {
        const res = await fetch("/metrics", { cache: "no-store" });
        if (!res.ok) return null;
        return await res.json();
      } catch {
        return null;
      }
    }

    resolveValues(snapshot) {
      const out = {};
      if (!snapshot) return out;
      for (const k in snapshot) {
        const obj = snapshot[k];
        if (obj && typeof obj === "object" && !Array.isArray(obj)) {
          for (const f of FIELDS) if (f in obj) out[f] = obj[f];
        } else if (Array.isArray(obj) && obj.length > 0) {
          const newest = obj[obj.length - 1];
          for (const f of FIELDS) if (newest && f in newest) out[f] = newest[f];
        }
      }
      return out;
    }

    async tick() {
      const data = await this.fetchMetrics();
      if (!data) return;

      const vals = this.resolveValues(data);
      this.history.push(vals);
      if (this.history.length > this.maxPoints) this.history.shift();

      if (this.root) {
        const points = this.history
          .map(h => typeof h.decode_tok_s === "number" ? h.decode_tok_s.toFixed(1) : "·")
          .join(" → ");
        this.root.textContent = points || "In attesa di attività...";
      }
    }

    start() {
      this.tick();
      this.timer = setInterval(() => this.tick(), POLL_MS);
    }
  }

  if (typeof window !== "undefined") {
    window.basaltCanvas = { CanvasPanel };
    document.addEventListener("DOMContentLoaded", () => {
      const panel = new CanvasPanel("canvas-telemetry");
      panel.start();
    });
  }
})();
