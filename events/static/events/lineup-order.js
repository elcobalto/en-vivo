/* Drag-and-drop ordering for lineup rows: the DOM order becomes billing_order 1, 2, 3…
 * Rows only become draggable while the ⠿ handle is held, so inputs and selects work normally. */
(function () {
  "use strict";
  var list = document.getElementById("lineup-rows");
  if (!list) return;
  var dragging = null;

  function renumber() {
    list.querySelectorAll(".lineup-row").forEach(function (row, i) {
      var order = row.querySelector('input[name$="-billing_order"]');
      if (order) order.value = i + 1;
    });
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
    var select = row.querySelector('select[name$="-performer"]');
    if (select) select.focus();
  });

  var nameInput = document.getElementById("new-artist-name");
  var addArtist = document.getElementById("new-artist-add");
  function addNewArtist() {
    var name = nameInput.value.trim();
    if (!name) { nameInput.focus(); return; }
    // Reuse an empty row if there is one, otherwise add a row.
    var row = Array.prototype.slice.call(list.querySelectorAll(".lineup-row")).find(function (r) {
      var sel = r.querySelector('select[name$="-performer"]');
      var hidden = r.querySelector('input[name$="-new_performer"]');
      return sel && !sel.value && hidden && !hidden.value && !r.querySelector('input[name$="-id"]').value;
    }) || addRow();
    row.querySelector('input[name$="-new_performer"]').value = name;
    row.querySelector(".performer-pick").classList.add("d-none");
    var label = row.querySelector(".new-artist-label");
    label.classList.remove("d-none");
    label.querySelector(".fw-semibold").textContent = name;
    nameInput.value = "";
    nameInput.focus();
  }
  if (addArtist) addArtist.addEventListener("click", addNewArtist);
  if (nameInput) nameInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); addNewArtist(); }
  });

  // × on rows that aren't saved yet just removes them from the page.
  list.addEventListener("click", function (e) {
    var remove = e.target.closest(".remove-row");
    if (!remove) return;
    remove.closest(".lineup-row").remove();
    renumber();
  });

  list.closest("form").addEventListener("submit", renumber);
  renumber();
})();
