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

  // Tables that scroll sideways become a focusable, named region, so keyboard users can scroll them.
  document.querySelectorAll(".table-wrap").forEach(function (wrap) {
    if (wrap.scrollWidth > wrap.clientWidth + 1) {
      wrap.setAttribute("tabindex", "0");
      wrap.setAttribute("role", "region");
      var head = wrap.closest(".card") && wrap.closest(".card").querySelector("h2");
      wrap.setAttribute("aria-label", (head ? head.textContent.trim() + ", " : "") + "table, scrolls sideways");
    }
  });

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
    // Selected requirements as removable chips above the filter, so the mapping is visible without scrolling the list.
    var chips = document.createElement("div");
    chips.className = "req-chips";
    chips.setAttribute("aria-live", "polite");
    picker.insertBefore(chips, filter);
    function render() {
      var checked = picker.querySelectorAll(".req-item input:checked");
      count.textContent = checked.length;
      chips.textContent = "";
      checked.forEach(function (box) {
        var chip = document.createElement("button");
        chip.type = "button";
        chip.className = "req-chip mono";
        chip.textContent = box.value + " \u00d7";
        chip.setAttribute("aria-label", "Remove " + box.value);
        chip.disabled = box.disabled;
        chip.addEventListener("click", function () { box.checked = false; render(); });
        chips.appendChild(chip);
      });
      chips.hidden = checked.length === 0;
    }
    picker.addEventListener("change", render);
    render();
  });

  // Filter forms apply on change; the Filter button stays for use without JavaScript.
  document.querySelectorAll("form[data-autosubmit]").forEach(function (form) {
    form.querySelectorAll("select").forEach(function (sel) {
      sel.addEventListener("change", function () { form.requestSubmit ? form.requestSubmit() : form.submit(); });
    });
    var button = form.querySelector("[data-autosubmit-button]");
    if (button && !form.querySelector("input[type=search]")) { button.hidden = true; }
  });

  // Score buttons: show the meaning of the chosen level under each row.
  document.querySelectorAll("[data-level-hint]").forEach(function (hint) {
    var name = hint.getAttribute("data-level-hint");
    var radios = document.querySelectorAll('input[name="' + name + '"]');
    function show() {
      var picked = document.querySelector('input[name="' + name + '"]:checked');
      hint.textContent = picked ? picked.value + ": " + (picked.getAttribute("data-meaning") || "") : "";
    }
    radios.forEach(function (r) { r.addEventListener("change", show); });
    show();
  });

  // Mobile menu: close it once a destination is chosen.
  var toggle = document.getElementById("nav-toggle");
  if (toggle) {
    document.querySelectorAll(".nav a").forEach(function (a) { a.addEventListener("click", function () { toggle.checked = false; }); });
  }
})();
