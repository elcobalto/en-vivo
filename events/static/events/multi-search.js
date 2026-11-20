/* Multi-select as a search box: <select multiple data-search-multi> becomes a text box with suggestions
 * plus removable tags for the chosen items. The select stays (hidden) and is what the form sends. */
(function () {
  "use strict";
  document.querySelectorAll("select[multiple][data-search-multi]").forEach(function (select, n) {
    var listId = "multi-search-options-" + n;
    var datalist = document.createElement("datalist");
    datalist.id = listId;
    var byName = {};
    Array.prototype.forEach.call(select.options, function (option) {
      byName[option.text.trim().toLowerCase()] = option;
      datalist.appendChild(new Option(option.text));
    });

    var wrap = document.createElement("div");
    var tags = document.createElement("div");
    tags.className = "d-flex flex-wrap gap-1 mb-1";
    var box = document.createElement("input");
    box.type = "search";
    box.className = "form-control";
    box.setAttribute("list", listId);
    box.setAttribute("autocomplete", "off");
    box.placeholder = select.dataset.placeholder || gettext("Type to search…");
    if (select.id) {
      box.id = select.id + "-search";
      var label = document.querySelector('label[for="' + select.id + '"]');
      if (label) label.setAttribute("for", box.id);
    }
    var hint = document.createElement("div");
    hint.className = "small text-warning d-none";

    function render() {
      tags.innerHTML = "";
      Array.prototype.forEach.call(select.selectedOptions, function (option) {
        var tag = document.createElement("span");
        tag.className = "badge text-bg-secondary d-inline-flex align-items-center gap-1";
        tag.textContent = option.text;
        var remove = document.createElement("button");
        remove.type = "button";
        remove.className = "btn-close btn-close-white";
        remove.style.fontSize = ".55rem";
        remove.setAttribute("aria-label", interpolate(gettext("Remove %s"), [option.text]));
        remove.addEventListener("click", function () { option.selected = false; render(); });
        tag.appendChild(remove);
        tags.appendChild(tag);
      });
    }

    function pick() {
      var text = box.value.trim();
      if (!text) { hint.classList.add("d-none"); return; }
      var option = byName[text.toLowerCase()];
      if (option) {
        option.selected = true;
        box.value = "";
        hint.classList.add("d-none");
        render();
      } else {
        hint.textContent = interpolate(gettext("“%s” isn't in the list."), [text]);
        hint.classList.remove("d-none");
      }
    }
    box.addEventListener("change", pick);
    box.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); pick(); } });

    select.classList.add("d-none");
    select.parentNode.insertBefore(wrap, select);
    wrap.appendChild(tags);
    wrap.appendChild(box);
    wrap.appendChild(hint);
    wrap.appendChild(datalist);
    render();
  });
})();
