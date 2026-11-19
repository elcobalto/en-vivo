/* "Sort lineup from poster": reads the event's poster in the browser (Tesseract.js OCR, free) and
 * reorders the acts already in the lineup by how big their names are printed. Bigger = higher on the bill;
 * the biggest tier becomes headliners. Acts it can't find keep their place. Nothing is saved until Save. */
(function () {
  "use strict";
  var button = document.getElementById("poster-sort");
  var list = document.getElementById("lineup-rows");
  if (!button || !list) return;
  var status = document.getElementById("poster-sort-status");
  var CREDITS = ["author", "director"];
  var SORTABLE_ROLES = ["headliner", "support"]; // other roles (cast, host, guest…) keep their role

  function say(text) { status.textContent = text; }

  function norm(text) {
    return (text || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, "");
  }

  function distance(a, b) { // Levenshtein
    var prev = [], cur, i, j;
    for (j = 0; j <= b.length; j++) prev[j] = j;
    for (i = 1; i <= a.length; i++) {
      cur = [i];
      for (j = 1; j <= b.length; j++) {
        cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
      }
      prev = cur;
    }
    return prev[b.length];
  }

  function similar(a, b) {
    if (!a || !b) return 0;
    return 1 - distance(a, b) / Math.max(a.length, b.length);
  }

  function rowName(row) {
    var typed = row.querySelector('input[name$="-new_artist"]');
    if (typed && typed.value) return typed.value;
    var select = row.querySelector('select[name$="-artist"]');
    if (!select || !select.value) return "";
    return select.options[select.selectedIndex].text;
  }

  function rowRole(row) {
    var role = row.querySelector('select[name$="-role"]');
    return role ? role.value : "";
  }

  function isDeleted(row) {
    var del = row.querySelector('input[name$="-DELETE"]');
    return del && del.checked;
  }

  // Best place on the poster for a name: consecutive words on one line whose letters match closely.
  // Returns {height, top} of the biggest match, or null.
  function findOnPoster(name, lines) {
    var wanted = norm(name);
    var size = name.split(/\s+/).filter(Boolean).length || 1;
    var best = null;
    lines.forEach(function (line) {
      var words = line.words.filter(function (w) { return norm(w.text); });
      for (var start = 0; start < words.length; start++) {
        for (var n = Math.max(1, size - 1); n <= size + 1 && start + n <= words.length; n++) {
          var chunk = words.slice(start, start + n);
          var text = norm(chunk.map(function (w) { return w.text; }).join(""));
          if (similar(text, wanted) < (wanted.length <= 4 ? 0.99 : 0.8)) continue;
          var heights = chunk.map(function (w) { return w.bbox.y1 - w.bbox.y0; }).sort(function (a, b) { return a - b; });
          var height = heights[Math.floor(heights.length / 2)];
          var top = Math.min.apply(null, chunk.map(function (w) { return w.bbox.y0; }));
          var left = Math.min.apply(null, chunk.map(function (w) { return w.bbox.x0; }));
          if (!best || height > best.height) best = { height: height, top: top, left: left };
        }
      }
    });
    return best;
  }

  function loadTesseract() {
    if (window.Tesseract) return Promise.resolve(window.Tesseract);
    return new Promise(function (resolve, reject) {
      var script = document.createElement("script");
      script.src = "https://cdn.jsdelivr.net/npm/tesseract.js@5.1.1/dist/tesseract.min.js";
      script.onload = function () { resolve(window.Tesseract); };
      script.onerror = function () { reject(new Error("Couldn't load the poster reader.")); };
      document.head.appendChild(script);
    });
  }

  function linesOf(data) {
    var lines = [];
    (data.blocks || []).forEach(function (block) {
      (block.paragraphs || []).forEach(function (p) {
        (p.lines || []).forEach(function (line) { lines.push(line); });
      });
    });
    return lines.length ? lines : (data.lines || []);
  }

  button.addEventListener("click", function () {
    var acts = Array.prototype.slice.call(list.querySelectorAll(".lineup-row")).filter(function (row) {
      return rowName(row) && !isDeleted(row) && CREDITS.indexOf(rowRole(row)) === -1;
    });
    if (acts.length < 2) { say("Add at least two acts to the lineup first."); return; }
    button.disabled = true;
    say("Loading the poster reader… (the first time takes a few seconds)");
    var worker;
    loadTesseract()
      .then(function (Tesseract) { return Tesseract.createWorker("eng"); })
      .then(function (w) {
        worker = w;
        say("Reading the poster…");
        return worker.recognize(button.dataset.image, {}, { blocks: true });
      })
      .then(function (result) {
        var lines = linesOf(result.data);
        var found = [], missing = [];
        acts.forEach(function (row, index) {
          var spot = findOnPoster(rowName(row), lines);
          if (spot) found.push({ row: row, index: index, height: spot.height, top: spot.top, left: spot.left });
          else missing.push(rowName(row));
        });
        if (!found.length) { say("Couldn't read any of these names on the poster. The lineup wasn't changed."); return; }

        // Found acts fill the slots they held, biggest name first; acts not found stay where they are.
        var slots = found.map(function (f) { return f.index; }).sort(function (a, b) { return a - b; });
        // Bigger first; same size: higher on the poster, then left to right (small height wobble ignored).
        var ranked = found.slice().sort(function (a, b) {
          if (Math.abs(b.height - a.height) > Math.max(a.height, b.height) * 0.1) return b.height - a.height;
          return Math.abs(a.top - b.top) > Math.min(a.height, b.height) / 2 ? a.top - b.top : a.left - b.left;
        });
        var biggest = Math.max.apply(null, found.map(function (f) { return f.height; }));
        var order = acts.slice();
        ranked.forEach(function (f, i) { order[slots[i]] = f.row; });
        // Rebuild the whole list: act rows take each other's places; credits and empty rows don't move.
        var next = 0;
        Array.prototype.slice.call(list.querySelectorAll(".lineup-row")).map(function (row) {
          return acts.indexOf(row) === -1 ? row : order[next++];
        }).forEach(function (row) { list.appendChild(row); });

        ranked.forEach(function (f) {
          var role = f.row.querySelector('select[name$="-role"]');
          if (!role || SORTABLE_ROLES.indexOf(role.value) === -1) return;
          role.value = f.height >= biggest * 0.75 ? "headliner" : "support";
        });
        if (window.enVivoLineup) window.enVivoLineup.renumber();

        var heads = ranked.filter(function (f) { return f.height >= biggest * 0.75; }).length;
        say("Sorted " + found.length + " act" + (found.length === 1 ? "" : "s") + " by name size (" + heads +
            " headliner" + (heads === 1 ? "" : "s") + ")." +
            (missing.length ? " Not found on the poster, left in place: " + missing.join(", ") + "." : "") +
            " Check it, then Save.");
      })
      .catch(function (err) { say((err && err.message) || "Couldn't read the poster."); })
      .then(function () {
        button.disabled = false;
        if (worker) worker.terminate();
      });
  });
})();
