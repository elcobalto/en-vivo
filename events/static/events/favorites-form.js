// Favourite events form: a festival's two act dropdowns list the acts you saw there (hidden for other events).
// The page sends each dropdown only the picked festival's acts; the rest come from the #favorite-lineups JSON.
function initFavoriteActs(root) {
  var data = root.querySelector("#favorite-lineups");
  var lineups = data ? JSON.parse(data.textContent) : {};
  root.querySelectorAll("[data-favorite-slot]").forEach(function (eventSelect) {
    var box = root.querySelector('[data-favorite-acts="' + eventSelect.dataset.favoriteSlot + '"]');
    var selects = box.querySelectorAll("select");
    function fill() {
      var lineup = lineups[eventSelect.value] || [];
      box.hidden = !lineup.length;
      selects.forEach(function (select, n) {
        var keep = select.value;
        select.replaceChildren(new Option(n === 0 ? gettext("Headliners") : "—", ""));
        lineup.forEach(function (act) { select.add(new Option(act.name, act.id)); });
        select.value = lineup.some(function (act) { return String(act.id) === keep; }) ? keep : "";
      });
    }
    eventSelect.addEventListener("change", fill);
    fill();
  });
}

// On the edit profile page: load the form into the modal when it opens, save it without leaving the page
// (so unsaved profile changes stay), then show the new list.
document.addEventListener("DOMContentLoaded", function () {
  var modal = document.getElementById("favoritesModal");
  if (!modal) return;
  var body = modal.querySelector(".modal-body");
  var headers = {"X-Requested-With": "fetch"};

  function show(html) {
    body.innerHTML = html;
    initFavoriteActs(body);
    var form = body.querySelector("[data-favorites-form]");
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var button = form.querySelector("[type=submit]");
      button.disabled = true;
      fetch(form.action, {method: "POST", body: new FormData(form), headers: headers})
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
        .then(function (result) {
          if (!result.ok) return show(result.form);  // errors: show the form again
          document.getElementById("favorites-summary").outerHTML = result.summary;
          bootstrap.Modal.getOrCreateInstance(modal).hide();
        })
        .catch(function () { form.submit(); });  // fall back to a normal post
    });
  }

  modal.addEventListener("show.bs.modal", function () {
    body.innerHTML = '<div class="text-center text-body-secondary py-4"><div class="spinner-border spinner-border-sm"></div> ' + gettext("Loading your events…") + '</div>';
    fetch(modal.dataset.url, {headers: headers})
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
      .then(show)
      .catch(function () { window.location = modal.dataset.url; });  // the form's own page
  });
});
