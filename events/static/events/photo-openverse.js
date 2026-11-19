/*
 * "Find photo": Openverse results (openly licensed photos, mostly from Flickr), searched from the browser.
 *
 * Openverse is free and allows browser requests (200 a day per visitor without an account), but a search
 * nobody has made before can take 10–20 seconds, so the page shows Deezer/Commons right away and adds these.
 *
 * <section data-openverse data-query data-artist> holds [data-openverse-status], [data-openverse-grid]
 * and a <template data-candidate-template> with one empty photo card (events/_photo_candidate.html).
 */
(function () {
  "use strict";

  var API = "https://api.openverse.org/v1/images/";
  var BUDGET_MS = 45000;  // stop retrying / trying shorter searches after this long
  var REQUEST_MS = 25000;  // their gateway gives up at ~20s anyway
  var LICENCES = {
    "by": "CC BY", "by-sa": "CC BY-SA", "by-nd": "CC BY-ND", "by-nc": "CC BY-NC",
    "by-nc-sa": "CC BY-NC-SA", "by-nc-nd": "CC BY-NC-ND", "cc0": "CC0", "pdm": "Public domain"
  };

  // Openverse needs every word to match, so "Paul McCartney Chile 2019" often finds nothing:
  // then try without the year, then just the artist. Its words match anywhere on their own, so the
  // artist's name is quoted as a phrase (otherwise "Green Day Chile" finds drawings tagged green/day/chile).
  function attempts(query, artist) {
    query = query.trim();
    artist = (artist || "").trim();
    if (artist && query.indexOf('"') === -1) {
      var at = query.toLowerCase().indexOf(artist.toLowerCase());
      if (at !== -1) query = query.slice(0, at) + '"' + query.slice(at, at + artist.length) + '"' + query.slice(at + artist.length);
    }
    var withoutYear = query.replace(/\b(19|20)\d\d\b/g, " ").replace(/\s+/g, " ").trim();
    var seen = {};
    return [query, withoutYear, artist && '"' + artist + '"'].filter(function (q) {
      var key = q.toLowerCase();
      if (!q || seen[key]) return false;
      seen[key] = true;
      return true;
    });
  }

  function search(q, signal) {
    var url = API + "?" + new URLSearchParams({ q: q, page_size: "12", mature: "false" });
    return fetch(url, { signal: signal, headers: { Accept: "application/json" } }).then(function (resp) {
      if (resp.status === 429) throw new Error("limit");
      if (!resp.ok) throw new Error("busy");
      return resp.json();
    }).then(function (data) { return data.results || []; });
  }

  function credit(item) {
    var licence = LICENCES[item.license] || (item.license || "").toUpperCase();
    if (licence.indexOf("CC ") === 0 && item.license_version) licence += " " + item.license_version;
    return [(item.creator || "").slice(0, 80), licence].filter(Boolean).join(" · ");
  }

  function card(template, item) {
    var node = template.content.firstElementChild.cloneNode(true);
    node.querySelector("[data-c-url]").value = item.url;
    var thumb = node.querySelector("[data-c-thumb]");
    thumb.src = item.thumbnail || item.url;
    thumb.alt = item.title || "";
    node.querySelector("[data-c-source]").textContent = "Openverse";
    var link = node.querySelector("[data-c-credit]");
    var text = node.querySelector("[data-c-credit-text]");
    var who = credit(item);
    if (/^https:\/\//.test(item.foreign_landing_url || "")) {
      link.href = item.foreign_landing_url;
      link.textContent = who || "source";
      link.hidden = false;
      text.hidden = true;
    } else {
      text.textContent = who;
      link.hidden = true;
      text.hidden = false;
    }
    return node;
  }

  function init(root) {
    var status = root.querySelector("[data-openverse-status]");
    var grid = root.querySelector("[data-openverse-grid]");
    var template = root.querySelector("[data-candidate-template]");
    var queries = attempts(root.dataset.query || "", root.dataset.artist || "");
    var started = Date.now();
    var slow = setTimeout(function () {
      status.textContent = "Still looking… Openverse can take a while for a search nobody has made before.";
    }, 5000);

    function finish(text) {
      clearTimeout(slow);
      status.textContent = text;
      status.hidden = !text;
    }

    var failed = false;  // a search timed out or errored (Openverse's gateway timeouts arrive as network errors)

    function tryNext(i, retry) {
      if (i >= queries.length || ((i > 0 || retry) && Date.now() - started > BUDGET_MS)) {
        finish(failed
          ? "Openverse isn't answering right now; reload the page in a moment to try again."
          : "No openly licensed photos found. Try a shorter search, like just the artist's name.");
        return;
      }
      var controller = window.AbortController ? new AbortController() : null;
      var timer = controller && setTimeout(function () { controller.abort(); }, REQUEST_MS);
      search(queries[i], controller && controller.signal).then(function (items) {
        clearTimeout(timer);
        items = items.filter(function (item) { return /^https:\/\//.test(item.url || ""); });  // http is blocked on https pages
        if (!items.length) { tryNext(i + 1); return; }
        failed = false;
        items.forEach(function (item) { grid.appendChild(card(template, item)); });
        var plain = function (q) { return q.replace(/"/g, ""); };
        finish(i ? "Nothing for “" + plain(queries[0]) + "”, so these are for “" + plain(queries[i]) + "”." : "");
      }).catch(function (error) {
        clearTimeout(timer);
        if (error && error.message === "limit") {
          finish("Openverse has reached its free limit for today; try again tomorrow.");
          return;
        }
        failed = true;
        // Openverse often finishes a timed-out search in the background, so the same one again usually
        // comes back quickly from their cache; after a second failure, move on to the shorter search.
        if (retry) tryNext(i + 1); else tryNext(i, true);
      });
    }

    tryNext(0);
  }

  document.querySelectorAll("[data-openverse]").forEach(init);
})();
