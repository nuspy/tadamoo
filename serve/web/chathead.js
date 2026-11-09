"use strict";
/* The header of Chat and Code on a narrow screen: the New button and More stay in the row, and More opens the rest as a list
   in the header. Tap and keyboard reach every control; Escape closes the list and focus returns to More. */
(() => {
  const head = document.querySelector(".chathead"), more = document.getElementById("ch-more");
  if (!head || !more) return;
  const narrow = window.matchMedia("(max-width: 860px)");
  function set(open) {
    head.classList.toggle("more-open", open);
    more.setAttribute("aria-expanded", String(open));
  }
  more.addEventListener("click", () => set(!head.classList.contains("more-open")));
  head.addEventListener("keydown", e => {
    if (e.key !== "Escape" || e.defaultPrevented || !head.classList.contains("more-open")) return;
    e.preventDefault();
    set(false);
    more.focus();
  });
  narrow.addEventListener("change", () => set(false));
})();
