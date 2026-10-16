// CallScope — общее для всех страниц: кнопка меню на планшете.
(function () {
  "use strict";
  var shell = document.querySelector(".shell");
  var toggle = document.querySelector("[data-menu-toggle]");
  if (!shell || !toggle) return;

  function setOpen(open) {
    shell.classList.toggle("menu-open", open);
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
  }

  toggle.addEventListener("click", function () {
    setOpen(!shell.classList.contains("menu-open"));
  });
  document.querySelectorAll("[data-menu-close]").forEach(function (el) {
    el.addEventListener("click", function () { setOpen(false); });
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") setOpen(false);
  });
})();
