// CallScope — страница звонка: перемотка по клику, подсветка текущей реплики, подтверждения.
(function () {
  "use strict";
  var player = document.getElementById("player");

  // Подтверждение опасных действий: <form data-confirm="Текст вопроса">.
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.getAttribute("data-confirm"))) event.preventDefault();
    });
  });

  if (!player) return;
  var utterances = Array.prototype.slice.call(document.querySelectorAll(".utt[data-start]"));

  function seek(seconds) {
    if (isNaN(seconds)) return;
    player.currentTime = seconds;
    var playing = player.play();
    if (playing && playing.catch) playing.catch(function () {});
  }

  function onActivate(event) {
    var target = event.target.closest("[data-start]");
    if (!target || target.tagName === "AUDIO") return;
    seek(parseFloat(target.getAttribute("data-start")));
  }

  document.addEventListener("click", onActivate);
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Enter" && event.key !== " ") return;
    var target = event.target.closest(".utt[data-start]");
    if (!target) return;
    event.preventDefault();
    seek(parseFloat(target.getAttribute("data-start")));
  });

  // Подсветка реплики, которая звучит сейчас.
  var current = null;
  player.addEventListener("timeupdate", function () {
    var now = player.currentTime;
    var found = null;
    for (var i = 0; i < utterances.length; i++) {
      var start = parseFloat(utterances[i].getAttribute("data-start"));
      if (start <= now + 0.05) found = utterances[i]; else break;
    }
    if (found === current) return;
    if (current) current.classList.remove("now");
    if (found) found.classList.add("now");
    current = found;
  });
})();
