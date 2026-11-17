/*
 * Venue search box: our own venues first, then Google Places suggestions.
 *
 * <div data-venue-search data-mode="select|create|link" ...> (see _venue_search.html)
 *   select: picking a venue fills the hidden <input class="vs-value"> (event form)
 *   create: picking opens the venue (or `data-next`) — used on "New venue"
 *   link:   picking a Google place re-standardizes an existing venue (admins)
 */
(function () {
  "use strict";

  function newSession() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return Date.now().toString(36) + Math.random().toString(36).slice(2);
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  }

  function init(root) {
    var mode = root.dataset.mode || "select";
    var input = root.querySelector(".vs-input");
    var list = root.querySelector(".vs-results");
    var hidden = root.querySelector(".vs-value");
    var chosen = root.querySelector(".vs-chosen");
    var session = newSession();
    var timer = null;
    var seq = 0;
    var active = -1;

    function hide() {
      list.hidden = true;
      active = -1;
    }

    function items() {
      return Array.prototype.slice.call(list.querySelectorAll(".vs-item"));
    }

    function highlight(index) {
      var all = items();
      all.forEach(function (item, i) { item.classList.toggle("active", i === index); });
      active = index;
    }

    function showMessage(text) {
      list.replaceChildren(el("div", "list-group-item small text-body-secondary", text));
      list.hidden = false;
    }

    function setChosen(venue) {
      if (!chosen) return;
      chosen.replaceChildren();
      if (!venue) return;
      chosen.append(el("span", "badge text-bg-success me-2", "✓"));
      chosen.append(el("strong", "", venue.name));
      if (venue.secondary) chosen.append(el("span", "text-body-secondary", " · " + venue.secondary));
      var clear = el("button", "btn btn-link btn-sm text-danger p-0 ms-2", "Remove");
      clear.type = "button";
      clear.addEventListener("click", function () {
        hidden.value = "";
        setChosen(null);
        input.focus();
      });
      chosen.append(clear);
    }

    function finish(venue) {
      session = newSession();
      if (mode === "select") {
        hidden.value = venue.id;
        hidden.dispatchEvent(new Event("change", { bubbles: true }));
        setChosen(venue);
        input.value = "";
        hide();
      } else {
        window.location.href = root.dataset.next || venue.url;
      }
    }

    function pickGoogle(place) {
      if (mode === "link") {
        var form = el("form");
        form.method = "post";
        form.action = root.dataset.linkUrl;
        [["csrfmiddlewaretoken", root.dataset.csrf], ["place_id", place.place_id], ["session", session]]
          .forEach(function (pair) {
            var field = el("input");
            field.type = "hidden";
            field.name = pair[0];
            field.value = pair[1];
            form.append(field);
          });
        document.body.append(form);
        form.submit();
        return;
      }
      showMessage("Adding “" + place.main + "”…");
      var body = new FormData();
      body.append("place_id", place.place_id);
      body.append("session", session);
      fetch(root.dataset.placeUrl, {
        method: "POST",
        body: body,
        headers: { "X-CSRFToken": root.dataset.csrf },
        credentials: "same-origin",
      })
        .then(function (r) { return r.json().then(function (data) { return { ok: r.ok, data: data }; }); })
        .then(function (res) {
          if (!res.ok) throw new Error(res.data.error || "Could not add that venue.");
          finish(res.data);
        })
        .catch(function (err) { showMessage(err.message); });
    }

    function render(data) {
      list.replaceChildren();
      var local = mode === "link" ? [] : data.local || [];
      var google = data.google || [];

      function addHeader(text) {
        list.append(el("div", "list-group-item small text-uppercase text-body-secondary py-1 vs-header", text));
      }

      function addItem(main, secondary, onPick) {
        var button = el("button", "list-group-item list-group-item-action vs-item");
        button.type = "button";
        button.append(el("div", "fw-semibold", main));
        if (secondary) button.append(el("div", "small text-body-secondary", secondary));
        button.addEventListener("click", onPick);
        list.append(button);
      }

      if (local.length) {
        addHeader("On En Vivo");
        local.forEach(function (v) { addItem(v.name, v.secondary, function () { finish(v); }); });
      }
      if (google.length) {
        addHeader("From Google");
        google.forEach(function (p) { addItem(p.main, p.secondary, function () { pickGoogle(p); }); });
      }
      if (!local.length && !google.length) {
        showMessage(data.error || (data.google_enabled ? "No matches." : "No matches. Google search isn't configured, add it by hand below."));
        return;
      }
      if (data.error) list.append(el("div", "list-group-item small text-warning", data.error));
      list.hidden = false;
      active = -1;
    }

    function search() {
      var q = input.value.trim();
      if (q.length < 2) { hide(); return; }
      var mine = ++seq;
      var url = root.dataset.searchUrl + "?" + new URLSearchParams({ q: q, session: session });
      fetch(url, { credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (data) { if (mine === seq) render(data); })
        .catch(function () { if (mine === seq) showMessage("Search failed, try again."); });
    }

    input.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(search, 250);
    });
    input.addEventListener("keydown", function (e) {
      var all = items();
      if (e.key === "ArrowDown" && all.length) { e.preventDefault(); highlight(Math.min(active + 1, all.length - 1)); }
      else if (e.key === "ArrowUp" && all.length) { e.preventDefault(); highlight(Math.max(active - 1, 0)); }
      else if (e.key === "Enter") { e.preventDefault(); if (active >= 0 && all[active]) all[active].click(); }
      else if (e.key === "Escape") { hide(); }
    });
    document.addEventListener("click", function (e) { if (!root.contains(e.target)) hide(); });

    if (chosen && root.dataset.initialName) {
      setChosen({ name: root.dataset.initialName, secondary: root.dataset.initialSecondary || "" });
    }
  }

  document.querySelectorAll("[data-venue-search]").forEach(init);
})();
