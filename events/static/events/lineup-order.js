/* Drag-and-drop ordering for lineup rows: the DOM order becomes billing_order 1, 2, 3…
 * Rows only become draggable while the ⠿ handle is held, so inputs and selects work normally. */
(function () {
  "use strict";
  var list = document.getElementById("lineup-rows");
  if (!list) return;
  var dragging = null;

  var CREDITS = ["author", "director"];

  function renumber() {
    var rows = list.querySelectorAll(".lineup-row");
    rows.forEach(function (row, i) {
      var order = row.querySelector('input[name$="-billing_order"]');
      if (order) order.value = i + 1;
    });
    // The first act on the bill (not a credit like author/director) is the headliner.
    for (var i = 0; i < rows.length; i++) {
      var role = rows[i].querySelector('select[name$="-role"]');
      var filled = rows[i].querySelector('select[name$="-artist"]');
      var named = rows[i].querySelector('input[name$="-new_artist"]');
      var hasAct = (filled && filled.value) || (named && named.value);
      if (!hasAct || (role && CREDITS.indexOf(role.value) !== -1)) continue;
      if (role) role.value = "headliner";
      break;
    }
  }

  list.addEventListener("mousedown", function (e) {
    var handle = e.target.closest(".drag-handle");
    if (handle) handle.closest(".lineup-row").setAttribute("draggable", "true");
  });
  list.addEventListener("dragstart", function (e) {
    dragging = e.target.closest(".lineup-row");
    if (!dragging) return;
    dragging.classList.add("opacity-50");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", "");
  });
  list.addEventListener("dragover", function (e) {
    if (!dragging) return;
    e.preventDefault();
    var target = e.target.closest(".lineup-row");
    if (!target || target === dragging) return;
    var box = target.getBoundingClientRect();
    list.insertBefore(dragging, e.clientY > box.top + box.height / 2 ? target.nextSibling : target);
  });
  list.addEventListener("dragend", function () {
    if (dragging) {
      dragging.classList.remove("opacity-50");
      dragging.removeAttribute("draggable");
    }
    dragging = null;
    renumber();
  });

  // Keyboard: focus the handle and use ↑/↓ to move the row.
  list.addEventListener("keydown", function (e) {
    var handle = e.target.closest(".drag-handle");
    if (!handle || (e.key !== "ArrowUp" && e.key !== "ArrowDown")) return;
    e.preventDefault();
    var row = handle.closest(".lineup-row");
    var sibling = e.key === "ArrowUp" ? row.previousElementSibling : row.nextElementSibling;
    if (sibling) list.insertBefore(row, e.key === "ArrowUp" ? sibling : sibling.nextSibling);
    handle.focus();
    renumber();
  });

  // "+ Add another act" and the single "Add a new artist" box: new rows on the same page.
  var template = document.getElementById("lineup-row-template");
  var prefix = list.dataset.prefix;
  var total = document.querySelector('input[name="' + prefix + '-TOTAL_FORMS"]');

  function addRow() {
    var index = parseInt(total.value, 10);
    var holder = document.createElement("div");
    holder.innerHTML = template.innerHTML.replace(/__prefix__/g, index);
    var row = holder.firstElementChild;
    list.appendChild(row);
    total.value = index + 1;
    renumber();
    return row;
  }

  var addAct = document.getElementById("add-act");
  if (addAct) addAct.addEventListener("click", function () {
    var row = addRow();
    setupSearch(row);
    var search = row.querySelector(".artist-search") || row.querySelector('select[name$="-artist"]');
    if (search) search.focus();
  });

  var nameInput = document.getElementById("new-artist-name");
  var addArtist = document.getElementById("new-artist-add");
  function addNewArtist() {
    var name = nameInput.value.trim();
    if (!name) { nameInput.focus(); return; }
    // Reuse an empty row if there is one, otherwise add a row.
    var row = Array.prototype.slice.call(list.querySelectorAll(".lineup-row")).find(function (r) {
      var sel = r.querySelector('select[name$="-artist"]');
      var hidden = r.querySelector('input[name$="-new_artist"]');
      return sel && !sel.value && hidden && !hidden.value && !r.querySelector('input[name$="-id"]').value;
    }) || addRow();
    row.querySelector('input[name$="-new_artist"]').value = name;
    row.querySelector(".artist-pick").classList.add("d-none");
    var label = row.querySelector(".new-artist-label");
    label.classList.remove("d-none");
    label.querySelector(".fw-semibold").textContent = name;
    nameInput.value = "";
    nameInput.focus();
    renumber();
  }
  if (addArtist) addArtist.addEventListener("click", addNewArtist);
  if (nameInput) nameInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); addNewArtist(); }
  });

  // × on rows that aren't saved yet just removes them from the page.
  list.addEventListener("change", function (e) {
    if (e.target.matches('select[name$="-artist"]')) renumber();
  });
  list.addEventListener("click", function (e) {
    var remove = e.target.closest(".remove-row");
    if (!remove) return;
    remove.closest(".lineup-row").remove();
    renumber();
  });

  // Artist/tour pickers arrive with only their current choice; fill the full list (sent once) on first use.
  var optionLists = {};
  function fill(select) {
    var source = select.dataset.options;
    if (!source || select.dataset.filled) return;
    if (!optionLists[source]) {
      var holder = document.getElementById(source);
      optionLists[source] = holder ? JSON.parse(holder.textContent) : [];
    }
    var current = select.value;
    var empty = select.options.length ? select.options[0].cloneNode(true) : new Option("", "");
    var fragment = document.createDocumentFragment();
    fragment.appendChild(empty);
    optionLists[source].forEach(function (pair) {
      fragment.appendChild(new Option(pair[1], pair[0], false, String(pair[0]) === current));
    });
    select.innerHTML = "";
    select.appendChild(fragment);
    select.value = current;
    select.dataset.filled = "1";
  }
  ["mousedown", "focusin", "touchstart"].forEach(function (type) {
    list.addEventListener(type, function (e) {
      if (e.target.matches && e.target.matches("select[data-options]")) fill(e.target);
    });
  });

  // Artist search: a text box with suggestions replaces each row's long artist dropdown (kept hidden for the
  // form). Picking a known name selects that artist; a name that doesn't exist becomes a new artist on save.
  var artistData = null, byName = {};
  function artists() {
    if (artistData) return artistData;
    var holder = document.getElementById("artist-options");
    artistData = holder ? JSON.parse(holder.textContent) : [];
    var counts = {};
    artistData.forEach(function (pair) { var k = pair[1].toLowerCase(); counts[k] = (counts[k] || 0) + 1; });
    var datalist = document.createElement("datalist");
    datalist.id = "artist-search-options";
    artistData.forEach(function (pair) {
      // Two artists with the same name get their number added so either can be picked.
      var label = counts[pair[1].toLowerCase()] > 1 ? pair[1] + " (#" + pair[0] + ")" : pair[1];
      byName[label.toLowerCase()] = pair;
      var option = document.createElement("option");
      option.value = label;
      datalist.appendChild(option);
    });
    document.body.appendChild(datalist);
    return artistData;
  }

  function setupSearch(row) {
    var select = row.querySelector('select[name$="-artist"]');
    var typed = row.querySelector('input[name$="-new_artist"]');
    if (!select || row.querySelector(".artist-search")) return;
    var box = document.createElement("input");
    box.type = "search";
    box.className = "form-control form-control-sm artist-search";
    box.setAttribute("list", "artist-search-options");
    box.setAttribute("autocomplete", "off");
    box.placeholder = gettext("Search artists…");
    box.setAttribute("aria-label", gettext("Artist"));
    box.value = select.value ? select.options[select.selectedIndex].text : (typed ? typed.value : "");
    select.classList.add("d-none");
    select.parentNode.insertBefore(box, select);
    var note = document.createElement("div");
    note.className = "small text-success artist-new-note" + (typed && typed.value && !select.value ? "" : " d-none");
    note.textContent = gettext("New artist: created when you save.");
    box.parentNode.appendChild(note);
    // Rows that already show the "new artist" label from the old box switch to the search box instead.
    var pick = row.querySelector(".artist-pick"), label = row.querySelector(".new-artist-label");
    if (pick) pick.classList.remove("d-none");
    if (label) label.classList.add("d-none");

    box.addEventListener("focus", artists, { once: true });
    box.addEventListener("change", function () {
      artists();
      var text = box.value.trim(), match = byName[text.toLowerCase()];
      if (match) {
        if (!select.querySelector('option[value="' + match[0] + '"]')) select.appendChild(new Option(match[1], match[0]));
        select.value = String(match[0]);
        if (typed) typed.value = "";
        box.value = match[1];
        note.classList.add("d-none");
      } else {
        select.value = "";
        if (typed) typed.value = text;
        note.classList.toggle("d-none", !text);
      }
      renumber();
    });
  }
  list.querySelectorAll(".lineup-row").forEach(setupSearch);
  new MutationObserver(function () { list.querySelectorAll(".lineup-row").forEach(setupSearch); })
    .observe(list, { childList: true });

  // "Type support acts in bulk": one row per typed name (one per line, or split by • · * ;), as Support.
  var bulkText = document.getElementById("bulk-acts-text");
  var bulkAdd = document.getElementById("bulk-acts-add");
  var bulkStatus = document.getElementById("bulk-acts-status");
  function rowName(row) {
    var box = row.querySelector(".artist-search");
    var typed = row.querySelector('input[name$="-new_artist"]');
    return ((box && box.value) || (typed && typed.value) || "").trim().toLowerCase();
  }
  function emptyRow() {
    return Array.prototype.slice.call(list.querySelectorAll(".lineup-row")).find(function (r) {
      return !rowName(r) && !r.querySelector('input[name$="-id"]').value;
    });
  }
  if (bulkAdd) bulkAdd.addEventListener("click", function () {
    var names = bulkText.value.split(/[\n\r•·*;]+/).map(function (n) { return n.trim(); }).filter(Boolean);
    var have = {};
    list.querySelectorAll(".lineup-row").forEach(function (r) { var n = rowName(r); if (n) have[n] = true; });
    var added = 0, skipped = 0;
    names.forEach(function (name) {
      if (have[name.toLowerCase()]) { skipped++; return; }
      have[name.toLowerCase()] = true;
      var row = emptyRow() || addRow();
      setupSearch(row);
      var box = row.querySelector(".artist-search");
      box.value = name;
      box.dispatchEvent(new Event("change"));  // picks a known artist or marks a new one
      var role = row.querySelector('select[name$="-role"]');
      if (role) role.value = "support";
      added++;
    });
    renumber();  // with no acts before, the first one typed heads the bill
    bulkStatus.textContent = (skipped
      ? interpolate(ngettext("%(added)s act added, %(skipped)s already in the lineup. Save to keep them.",
                             "%(added)s acts added, %(skipped)s already in the lineup. Save to keep them.", added),
                    {added: added, skipped: skipped}, true)
      : interpolate(ngettext("%(added)s act added. Save to keep them.", "%(added)s acts added. Save to keep them.", added),
                    {added: added}, true));
    if (added) bulkText.value = "";
  });

  list.closest("form").addEventListener("submit", renumber);
  renumber();
  // Other scripts (the poster sorter) reorder rows and then call this.
  window.enVivoLineup = { renumber: renumber };
})();
