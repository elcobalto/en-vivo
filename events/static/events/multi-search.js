/* Multi-select as a search box: <select multiple data-search-multi> becomes a text box with suggestions
 * plus removable tags for the chosen items. The select stays (hidden) and is what the form sends.
 * With data-allow-new, a name that isn't in the list can be added too: it's sent as "new:Name" and the
 * form creates it on save (Enter adds it straight away; leaving the box offers a button). */
(function () {
  "use strict";
  document.querySelectorAll("select[multiple][data-search-multi]").forEach(function (select, n) {
    var listId = "multi-search-options-" + n;
    var allowNew = select.hasAttribute("data-allow-new");
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
        var isNew = option.value.indexOf("new:") === 0;
        tag.className = "badge d-inline-flex align-items-center gap-1 " + (isNew ? "text-bg-success" : "text-bg-secondary");
        tag.textContent = isNew ? interpolate(gettext("%s (new)"), [option.text]) : option.text;
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
      } else if (allowNew) {
        hint.textContent = interpolate(gettext("“%s” isn't on En Vivo yet."), [text]) + " ";
        var add = document.createElement("button");
        add.type = "button";
        add.className = "btn btn-sm btn-link p-0 align-baseline";
        add.textContent = interpolate(gettext("+ Add “%s” as new"), [text]);
        add.addEventListener("click", function () { addNew(text); });
        hint.appendChild(add);
        hint.classList.remove("d-none");
      } else {
        hint.textContent = interpolate(gettext("“%s” isn't in the list."), [text]);
        hint.classList.remove("d-none");
      }
    }
    function addNew(text) {
      var option = new Option(text, "new:" + text, true, true);
      select.appendChild(option);
      byName[text.toLowerCase()] = option;
      box.value = "";
      hint.classList.add("d-none");
      render();
      box.focus();
    }
    box.addEventListener("change", pick);
    box.addEventListener("keydown", function (e) {
      if (e.key !== "Enter") return;
      e.preventDefault();
      var text = box.value.trim();
      if (allowNew && text && !byName[text.toLowerCase()]) addNew(text); else pick();
    });

    select.classList.add("d-none");
    select.parentNode.insertBefore(wrap, select);
    wrap.appendChild(tags);
    wrap.appendChild(box);
    wrap.appendChild(hint);
    wrap.appendChild(datalist);
    render();
  });
})();
