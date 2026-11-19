// "Auto" thumbnails: a photo close to the poster's 2:3 shape is stretched to fill it; a wider (or much
// taller) one stays whole on its blurred copy. Loaded in <head> so it hears every photo load, cached or lazy.
(function () {
  var MIN_RATIO = 0.5;   // width ÷ height; 2:3 posters are 0.67
  var MAX_RATIO = 0.85;  // wider than this (about 6:7) would look distorted when stretched, so blur instead

  function decide(img) {
    if (!img.classList || !img.classList.contains("poster-auto") || !img.naturalWidth) return;
    var ratio = img.naturalWidth / img.naturalHeight;
    var poster = img.closest(".poster");
    if (poster) poster.classList.toggle("poster-filled", ratio >= MIN_RATIO && ratio <= MAX_RATIO);
  }

  document.addEventListener("load", function (e) { decide(e.target); }, true);  // load doesn't bubble
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("img.poster-auto").forEach(function (img) { if (img.complete) decide(img); });
  });
})();
