// CallScope — «Настройки»: редактор чек-листа, ход переоценки, проверка шаблона имени.
(function () {
  "use strict";

  // --- переоценка: ход «N из M» обновляется сам --------------------------------------

  var rescore = document.querySelector("[data-rescore]");
  if (rescore) {
    var msg = rescore.querySelector("[data-rescore-msg]");
    var btn = rescore.querySelector("[data-rescore-btn]");
    var bar = rescore.querySelector("[data-rescore-bar]");
    var form = rescore.querySelector("form");
    var timer = null;

    var show = function (data) {
      msg.textContent = data.message;
      var running = data.state === "running";
      btn.disabled = running || data.state === "unavailable";
      if (bar) {
        bar.hidden = !(running && data.total);
        var fill = bar.querySelector("i");
        if (fill && data.total) fill.style.width = Math.round(100 * data.done / data.total) + "%";
      }
      if (running && !timer) timer = setInterval(poll, 1500);
      if (!running && timer) { clearInterval(timer); timer = null; }
    };

    var poll = function () {
      fetch("/api/rescore", { headers: { Accept: "application/json" }, credentials: "same-origin" })
        .then(function (r) {
          if (r.status === 401) { window.location.href = "/login?next=" + encodeURIComponent("/settings/checklist"); return null; }
          return r.json();
        })
        .then(function (data) { if (data) show(data); })
        .catch(function () { /* сеть мигнула — следующий опрос поправит */ });
    };

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      btn.disabled = true;
      fetch("/settings/rescore", { method: "POST", headers: { Accept: "application/json" }, credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(show)
        .catch(function () { msg.textContent = "Не удалось запустить переоценку. Попробуйте ещё раз."; btn.disabled = false; });
    });

    if (rescore.getAttribute("data-state") === "running") { poll(); }
  }

  // --- редактор чек-листа -------------------------------------------------------------

  var editor = document.querySelector("[data-checklist-form]");
  if (editor) {
    var list = editor.querySelector("[data-items]");
    var emptyNote = editor.querySelector("[data-items-empty]");
    var dirty = editor.querySelector("[data-dirty]");
    var template = document.getElementById("item-template");

    var renumber = function () {
      var cards = list.querySelectorAll("[data-item]");
      cards.forEach(function (card, i) {
        card.querySelector("[data-num]").textContent = String(i + 1);
        card.querySelector("[data-move='-1']").disabled = i === 0;
        card.querySelector("[data-move='1']").disabled = i === cards.length - 1;
      });
      emptyNote.hidden = cards.length > 0;
    };
    var markDirty = function () { if (dirty) dirty.hidden = false; };

    editor.addEventListener("input", function (event) {
      markDirty();
      if (event.target.matches("[data-title]")) {
        var card = event.target.closest("[data-item]");
        card.querySelector("[data-name]").textContent = event.target.value.trim() || "Новый пункт";
      }
    });
    editor.addEventListener("change", markDirty);

    editor.addEventListener("click", function (event) {
      var card = event.target.closest("[data-item]");
      var move = event.target.closest("[data-move]");
      if (move && card) {
        var step = Number(move.getAttribute("data-move"));
        var target = step < 0 ? card.previousElementSibling : card.nextElementSibling;
        if (target) list.insertBefore(card, step < 0 ? target : target.nextElementSibling);
        renumber(); markDirty(); move.focus();
        return;
      }
      if (event.target.closest("[data-remove]") && card) {
        var name = card.querySelector("[data-name]").textContent;
        if (window.confirm("Удалить пункт «" + name + "»? Ручные правки по нему исчезнут после переоценки.")) {
          card.remove(); renumber(); markDirty();
        }
        return;
      }
      if (event.target.closest("[data-add]")) {
        var fresh = template.content.firstElementChild.cloneNode(true);
        list.appendChild(fresh);
        renumber(); markDirty();
        fresh.scrollIntoView({ behavior: "smooth", block: "center" });
        fresh.querySelector("[data-title]").focus({ preventScroll: true });
      }
    });

    window.addEventListener("beforeunload", function (event) {
      if (dirty && !dirty.hidden && !editor.dataset.submitting) { event.preventDefault(); event.returnValue = ""; }
    });
    editor.addEventListener("submit", function () { editor.dataset.submitting = "1"; });

    renumber();
    if (dirty && editor.querySelector("[data-form-error]")) dirty.hidden = false;
    var firstError = editor.querySelector(".field-error");
    if (firstError) firstError.scrollIntoView({ block: "center" });
  }

  // --- шаблон имени файла: проверка на лету ------------------------------------------

  var fname = document.querySelector("[data-filename-form]");
  if (fname) {
    var result = fname.querySelector("[data-check-result]");
    var pending = null;
    var run = function () {
      fetch("/settings/filename/check", { method: "POST", body: new URLSearchParams(new FormData(fname)), credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          result.textContent = data.message;
          result.className = "check-result" + (data.matched === true ? " ok" : data.matched === false ? " no" : "");
        })
        .catch(function () { /* без проверки на лету работает кнопка «Проверить» */ });
    };
    fname.addEventListener("input", function () { clearTimeout(pending); pending = setTimeout(run, 300); });
    fname.querySelector("[data-check-btn]").addEventListener("click", function (event) {
      event.preventDefault(); run();
    });
  }
})();
