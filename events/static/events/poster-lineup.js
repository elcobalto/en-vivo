/* "Sort lineup from poster": reads the event's poster in the browser (Tesseract.js OCR, free) and
 * reorders the acts already in the lineup in reading order: top to bottom, then left to right. Acts on the
 * first line where any act appears become headliners, the rest support. Acts it can't find keep their place.
 * Nothing is saved until Save. */
(function () {
  "use strict";
  var button = document.getElementById("poster-sort");
  var list = document.getElementById("lineup-rows");
  if (!button || !list) return;
  var status = document.getElementById("poster-sort-status");
  var CREDITS = ["author", "director", "choreographer", "music", "lyrics", "book"];
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

  // Where a name is on the poster: consecutive words on one line whose letters match closely.
  // Returns {line, left, top} of the closest match (ties: the one higher up), or null.
  function findOnPoster(name, lines) {
    var wanted = norm(name);
    var size = name.split(/\s+/).filter(Boolean).length || 1;
    var best = null;
    lines.forEach(function (line, lineIndex) {
      var words = line.words.filter(function (w) { return norm(w.text); });
      for (var start = 0; start < words.length; start++) {
        for (var n = Math.max(1, size - 1); n <= size + 1 && start + n <= words.length; n++) {
          var chunk = words.slice(start, start + n);
          var score = similar(norm(chunk.map(function (w) { return w.text; }).join("")), wanted);
          if (score < (wanted.length <= 4 ? 0.99 : 0.8)) continue;
          var spot = {
            score: score,
            line: lineIndex,
            top: Math.min.apply(null, chunk.map(function (w) { return w.bbox.y0; })),
            left: Math.min.apply(null, chunk.map(function (w) { return w.bbox.x0; })),
          };
          if (!best || spot.score > best.score || (spot.score === best.score && spot.top < best.top)) best = spot;
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
      script.onerror = function () { reject(new Error(gettext("Couldn't load the poster reader."))); };
      document.head.appendChild(script);
    });
  }

  // The poster as a canvas: first through En Vivo (most sites don't let pages read their images),
  // then straight from the photo's site if that site allows it. Errors say which step failed.
  function loadPoster() {
    return fetch(button.dataset.image, { credentials: "same-origin" }).then(function (resp) {
      if (resp.ok) return resp.blob().then(function (blob) { return blobToCanvas(blob); });
      return resp.text().then(function (reason) {
        var code = interpolate(gettext("error %s"), [resp.status]);
        var why = (reason || "").length < 200 ? reason : code;
        return direct().catch(function () {
          throw new Error(interpolate(gettext("Couldn't get the photo: %s Try saving a copy of the poster on another site and using that link."),
                                      [why || code]));
        });
      });
    });
  }

  function direct() {
    return new Promise(function (resolve, reject) {
      var img = new Image();
      img.crossOrigin = "anonymous";
      img.onload = function () { try { resolve(toCanvas(img, img.naturalWidth, img.naturalHeight)); } catch (e) { reject(e); } };
      img.onerror = reject;
      img.src = button.dataset.direct;
    });
  }

  function blobToCanvas(blob) {
    return createImageBitmap(blob).then(function (bitmap) { return toCanvas(bitmap, bitmap.width, bitmap.height); },
      function () { throw new Error(gettext("The photo couldn't be opened as an image.")); });
  }

  function toCanvas(source, width, height) {
    // Small posters read better a bit larger; huge ones are scaled down to keep it quick.
    var scale = Math.min(3, Math.max(1, 1600 / Math.max(width, height)), 4000 / Math.max(width, height));
    var canvas = document.createElement("canvas");
    canvas.width = Math.round(width * scale);
    canvas.height = Math.round(height * scale);
    canvas.getContext("2d").drawImage(source, 0, 0, canvas.width, canvas.height);
    canvas.getContext("2d").getImageData(0, 0, 1, 1); // throws now if the browser won't let us read it
    return canvas;
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
    if (acts.length < 2) { say(gettext("Add at least two acts to the lineup first.")); return; }
    button.disabled = true;
    var worker, poster;
    say(gettext("Getting the photo…"));
    loadPoster()
      .then(function (canvas) {
        poster = canvas;
        say(gettext("Loading the poster reader… (the first time takes a few seconds)"));
        return loadTesseract();
      })
      .then(function (Tesseract) {
        return Tesseract.createWorker("eng").catch(function () { throw new Error(gettext("The poster reader couldn't start. Check your connection and try again.")); });
      })
      .then(function (w) {
        worker = w;
        say(gettext("Reading the poster…"));
        return worker.recognize(poster, {}, { blocks: true });
      })
      .then(function (result) {
        var lines = linesOf(result.data);
        var found = [], missing = [];
        acts.forEach(function (row, index) {
          var spot = findOnPoster(rowName(row), lines);
          if (spot) found.push({ row: row, index: index, line: spot.line, top: spot.top, left: spot.left });
          else missing.push(rowName(row));
        });
        if (!found.length) { say(gettext("Couldn't read any of these names on the poster. The lineup wasn't changed.")); return; }

        // Found acts fill the slots they held, in poster order; acts not found stay where they are.
        var slots = found.map(function (f) { return f.index; }).sort(function (a, b) { return a - b; });
        // Reading order: top to bottom, then left to right on the same line.
        var ranked = found.slice().sort(function (a, b) {
          return a.line === b.line ? a.left - b.left : a.top - b.top || a.left - b.left;
        });
        // Lines from OCR are in reading order too; acts on the first line with any act are the headliners.
        var firstLine = ranked[0].line;
        var isHeadliner = function (f) { return f.line === firstLine; };
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
          role.value = isHeadliner(f) ? "headliner" : "support";
        });
        if (window.enVivoLineup) window.enVivoLineup.renumber();

        var heads = ranked.filter(isHeadliner).length;
        say(interpolate(ngettext("Sorted %s act top to bottom", "Sorted %s acts top to bottom", found.length), [found.length]) +
            " (" + interpolate(ngettext("%s headliner", "%s headliners", heads), [heads]) + ")." +
            (missing.length ? " " + interpolate(gettext("Not found on the poster, left in place: %s."), [missing.join(", ")]) : "") +
            " " + gettext("Check it, then Save."));
      })
      .catch(function (err) {
        if (window.console) console.error("Poster sort failed:", err);
        say((err && err.message) || (typeof err === "string" ? err : gettext("Couldn't read the poster.")));
      })
      .then(function () {
        button.disabled = false;
        if (worker) worker.terminate();
      });
  });
})();
