// Progressive enhancements. No inline handlers (strict CSP); every page works without this file.
(function () {
  "use strict";

  // Confirm before destructive submits: <button data-confirm="...">
  document.addEventListener("click", function (ev) {
    var el = ev.target.closest("[data-confirm]");
    if (el && !window.confirm(el.getAttribute("data-confirm"))) {
      ev.preventDefault();
      ev.stopImmediatePropagation();
    }
  }, true);

  // Risk page: changing the sector reloads the page with that sector's default matrix.
  var sector = document.getElementById("sector-select");
  if (sector) {
    sector.addEventListener("change", function () {
      var base = sector.getAttribute("data-reload") || "/risk?sector=";
      window.location.assign(base + encodeURIComponent(sector.value));
    });
  }

  // Assessment: ticking "not applicable" clears the scores, and picking a score clears it.
  var na = document.querySelector('input[name="not_applicable"]');
  if (na) {
    var radios = document.querySelectorAll('input[name="doc_score"], input[name="impl_score"]');
    na.addEventListener("change", function () {
      if (na.checked) { radios.forEach(function (r) { r.checked = false; }); }
    });
    radios.forEach(function (r) { r.addEventListener("change", function () { na.checked = false; }); });
  }

  // Requirement picker: filter as you type, keep a live count of the selection.
  document.querySelectorAll("[data-req-picker]").forEach(function (picker) {
    var filter = picker.querySelector(".req-filter");
    var count = picker.querySelector("[data-req-count]");
    var items = picker.querySelectorAll(".req-item");
    var groups = picker.querySelectorAll(".req-group");
    filter.addEventListener("input", function () {
      var q = filter.value.trim().toLowerCase();
      items.forEach(function (it) { it.hidden = q !== "" && it.getAttribute("data-text").indexOf(q) === -1; });
      groups.forEach(function (g) { g.hidden = !g.querySelector(".req-item:not([hidden])"); });
    });
    filter.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); } });
    picker.addEventListener("change", function () {
      count.textContent = picker.querySelectorAll(".req-item input:checked").length;
    });
  });

  // Mobile menu: close it once a destination is chosen.
  var toggle = document.getElementById("nav-toggle");
  if (toggle) {
    document.querySelectorAll(".nav a").forEach(function (a) { a.addEventListener("click", function () { toggle.checked = false; }); });
  }
})();
