// Small progressive enhancements. No inline handlers (strict CSP).
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

  // Assessment: ticking "not applicable" clears the radio scores.
  var na = document.querySelector('input[name="not_applicable"]');
  if (na) {
    na.addEventListener("change", function () {
      if (na.checked) {
        document.querySelectorAll('input[name="doc_score"], input[name="impl_score"]').forEach(function (r) { r.checked = false; });
      }
    });
    document.querySelectorAll('input[name="doc_score"], input[name="impl_score"]').forEach(function (r) {
      r.addEventListener("change", function () { na.checked = false; });
    });
  }
})();
