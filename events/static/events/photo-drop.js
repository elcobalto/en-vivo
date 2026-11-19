/*
 * "Find photo": drag a photo from another tab (e.g. Google Images) onto the page, or paste its link.
 *
 * <div data-photo-drop> holds [data-drop-zone] (with .photo-drop-preview / .photo-drop-label),
 * [data-drop-message], [data-drop-input] (the form's url field) and [data-drop-submit].
 * The whole page accepts drops, so missing the box doesn't make the browser open the image instead.
 * Google/Bing result links (…imgres?imgurl=…) are unwrapped here for the preview; the server does it too.
 */
(function () {
  "use strict";

  var WRAPPED = [["google.", "imgurl"], ["bing.com", "mediaurl"]];

  function parseUrl(text) {
    try { return new URL((text || "").trim()); } catch (e) { return null; }
  }

  // The photo's own address, if `text` is a search-result link that wraps it.
  function unwrap(text) {
    var url = parseUrl(text);
    if (!url) return "";
    for (var i = 0; i < WRAPPED.length; i++) {
      if (url.hostname.indexOf(WRAPPED[i][0]) !== -1) {
        var inner = url.searchParams.get(WRAPPED[i][1]) || "";
        if (/^https?:\/\//i.test(inner)) return inner;
      }
    }
    return "";
  }

  function isSearchPage(text) {
    var url = parseUrl(text);
    if (!url) return false;
    var path = url.pathname.replace(/\/$/, "");
    return WRAPPED.some(function (w) { return url.hostname.indexOf(w[0]) !== -1; }) &&
      ["", "/search", "/imgres", "/images/search"].indexOf(path) !== -1;
  }

  // Every link a drop carries: <img src> and <a href> from the dragged HTML, then the URI list and plain text.
  function droppedLinks(dt) {
    var links = [];
    var html = dt.getData("text/html");
    if (html) {
      var doc = new DOMParser().parseFromString(html, "text/html");
      doc.querySelectorAll("img[src]").forEach(function (img) { links.push(img.getAttribute("src")); });
      doc.querySelectorAll("a[href]").forEach(function (a) { links.push(a.getAttribute("href")); });
    }
    (dt.getData("text/uri-list") || "").split(/\r?\n/).forEach(function (line) {
      if (line && line.charAt(0) !== "#") links.push(line);
    });
    links.push(dt.getData("text/plain"));
    return links.map(function (l) { return (l || "").trim(); }).filter(Boolean);
  }

  // Best photo link from a drop: { url, note } or { error }.
  function pickPhoto(links) {
    for (var i = 0; i < links.length; i++) {
      var inner = unwrap(links[i]);
      if (inner) return { url: inner };
    }
    var thumb = "";
    for (var j = 0; j < links.length; j++) {
      var link = links[j];
      if (!/^https?:\/\//i.test(link) || isSearchPage(link)) continue;
      if (/(^|\.)gstatic\.com$/i.test((parseUrl(link) || {}).hostname || "")) { thumb = thumb || link; continue; }
      return { url: link };
    }
    if (thumb) {
      return { url: thumb, note: "This is Google's small preview. For a sharper photo, click it on Google first and drag the large version." };
    }
    if (links.some(function (l) { return /^data:/i.test(l); })) {
      return { error: "That's Google's small preview, which can't be saved. Click the photo on Google first, then drag the large version." };
    }
    if (links.some(isSearchPage)) {
      return { error: "That's a link to the search results, not the photo. Drag the photo itself." };
    }
    return { error: "That didn't include a photo link. Try dragging the photo itself, or right-click it → “Copy image address”." };
  }

  function init(root) {
    var zone = root.querySelector("[data-drop-zone]");
    var preview = zone.querySelector(".photo-drop-preview");
    var label = zone.querySelector(".photo-drop-label");
    var message = root.querySelector("[data-drop-message]");
    var input = root.querySelector("[data-drop-input]");
    var submit = root.querySelector("[data-drop-submit]");
    var depth = 0;

    function say(text, kind) {
      message.hidden = !text;
      message.textContent = text || "";
      message.className = "small mb-2 " + (kind === "error" ? "text-danger" : "text-body-secondary");
    }

    function show(url, note) {
      input.value = url;
      preview.hidden = false;
      preview.src = url;
      label.textContent = "Drop another photo to replace it";
      submit.textContent = "Use this photo";
      say(note || "");
      submit.focus({ preventScroll: true });
    }

    preview.addEventListener("error", function () {
      if (!preview.getAttribute("src")) return;
      preview.hidden = true;
      say("This photo doesn't load here (its site may block other pages from showing it). Try another one.", "error");
    });

    function hasLinkOrFile(event) {
      var types = Array.prototype.slice.call(event.dataTransfer ? event.dataTransfer.types : []);
      return types.indexOf("text/uri-list") !== -1 || types.indexOf("text/html") !== -1 ||
        types.indexOf("text/plain") !== -1 || types.indexOf("Files") !== -1;
    }

    document.addEventListener("dragenter", function (event) {
      if (!hasLinkOrFile(event)) return;
      depth += 1;
      zone.classList.add("is-dragging");
    });
    document.addEventListener("dragleave", function () {
      depth = Math.max(0, depth - 1);
      if (!depth) zone.classList.remove("is-dragging");
    });
    document.addEventListener("dragover", function (event) {
      if (!hasLinkOrFile(event)) return;
      event.preventDefault();  // allow dropping anywhere instead of the browser opening the image
      event.dataTransfer.dropEffect = "copy";
    });
    document.addEventListener("drop", function (event) {
      depth = 0;
      zone.classList.remove("is-dragging");
      if (event.target.closest && event.target.closest("input[type=search]")) return;  // dropped search words
      event.preventDefault();
      var links = droppedLinks(event.dataTransfer);
      if (!links.length && event.dataTransfer.files.length) {
        say("We only save links to photos, not files. Drag the photo from a web page instead.", "error");
        return;
      }
      var picked = pickPhoto(links);
      if (picked.error) { say(picked.error, "error"); return; }
      show(picked.url, picked.note);
    });

    // A pasted (or typed, once the field is left) Google result link: swap in the photo it points to and preview it.
    function checkInput(event) {
      if (event.type === "input" && (event.inputType || "").indexOf("insertFromPaste") !== 0) return;
      var value = input.value.trim();
      var inner = unwrap(value);
      if (inner) { show(inner); return; }
      if (/^https:\/\/\S+$/i.test(value) && !isSearchPage(value)) {
        preview.hidden = false;
        preview.src = value;
        say("");
      } else if (isSearchPage(value)) {
        preview.hidden = true;
        say("That's a link to the search results, not the photo. Right-click the photo → “Copy image address”.", "error");
      }
    }
    input.addEventListener("input", checkInput);
    input.addEventListener("change", checkInput);
  }

  document.querySelectorAll("[data-photo-drop]").forEach(init);
})();
