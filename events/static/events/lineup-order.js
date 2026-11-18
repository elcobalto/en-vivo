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

  list.closest("form").addEventListener("submit", renumber);
  renumber();
})();
