// CallScope — страница «Загрузка»: отправка по одному файлу (не больше 3 сразу)
// и список очереди, который обновляется каждые 2 секунды.
(function () {
  "use strict";
  var MAX_PARALLEL = 3;
  var POLL_MS = 2000;
  var EXTENSIONS = [".mp3", ".wav", ".ogg", ".m4a"];
  var MAX_BYTES = 200 * 1024 * 1024;

  var dropzone = document.getElementById("dropzone");
  var input = document.getElementById("file-input");
  var sending = document.getElementById("sending");
  var sendList = document.getElementById("send-list");
  var queueBody = document.getElementById("queue-body");
  var queueEmpty = document.getElementById("queue-empty");
  var queueCounts = document.getElementById("queue-counts");

  var waiting = [];
  var active = 0;

  function toLogin() {
    window.location.href = "/login?next=" + encodeURIComponent("/upload");
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  // --- отправка ---------------------------------------------------------------------

  function addRow(file) {
    sending.hidden = false;
    var li = el("li");
    li.appendChild(el("span", "name", file.name));
    var state = el("span", "state muted", "ждёт отправки");
    li.appendChild(state);
    sendList.insertBefore(li, sendList.firstChild);
    return state;
  }

  function precheck(file) {
    var name = file.name.toLowerCase();
    var ok = EXTENSIONS.some(function (ext) { return name.endsWith(ext); });
    if (!ok) return "не тот формат — подходят mp3, wav, ogg, m4a";
    if (file.size === 0) return "файл пустой";
    if (file.size > MAX_BYTES) return "файл больше 200 МБ";
    return null;
  }

  function enqueue(files) {
    Array.prototype.forEach.call(files, function (file) {
      var state = addRow(file);
      var problem = precheck(file);
      if (problem) {
        state.className = "state fail";
        state.textContent = "отклонён: " + problem;
        return;
      }
      waiting.push({ file: file, state: state });
    });
    pump();
  }

  function pump() {
    while (active < MAX_PARALLEL && waiting.length) {
      send(waiting.shift());
    }
  }

  function finish(job, cls, text, link) {
    job.state.className = "state " + cls;
    job.state.textContent = text;
    if (link) {
      job.state.appendChild(document.createTextNode(" "));
      var a = el("a", "", "открыть");
      a.href = link;
      job.state.appendChild(a);
    }
  }

  function send(job) {
    active += 1;
    var form = new FormData();
    form.append("file", job.file, job.file.name);
    var xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/upload");
    xhr.upload.onprogress = function (event) {
      if (event.lengthComputable) {
        job.state.className = "state muted";
        job.state.textContent = "отправка " + Math.round((event.loaded / event.total) * 100) + "%";
      }
    };
    xhr.onload = function () {
      if (xhr.status === 401) { toLogin(); return; }
      var data = null;
      try { data = JSON.parse(xhr.responseText); } catch (e) { data = null; }
      if (xhr.status >= 200 && xhr.status < 300 && data) {
        if (data.duplicate) finish(job, "dup", "уже загружена —", data.url);
        else finish(job, "ok", "принята, в очереди");
        refreshQueue();
      } else {
        var reason = data && data.message ? data.message : "сервер не принял файл";
        finish(job, "fail", "отклонён: " + reason.replace(job.file.name + ": ", ""));
      }
    };
    xhr.onerror = function () {
      finish(job, "fail", "не отправлен: нет связи с панелью");
    };
    xhr.onloadend = function () {
      active -= 1;
      pump();
    };
    job.state.textContent = "отправка 0%";
    xhr.send(form);
  }

  input.addEventListener("change", function () {
    enqueue(input.files);
    input.value = "";
  });
  dropzone.addEventListener("keydown", function (event) {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      input.click();
    }
  });
  ["dragenter", "dragover"].forEach(function (type) {
    dropzone.addEventListener(type, function (event) {
      event.preventDefault();
      dropzone.classList.add("over");
    });
  });
  ["dragleave", "drop"].forEach(function (type) {
    dropzone.addEventListener(type, function (event) {
      event.preventDefault();
      dropzone.classList.remove("over");
    });
  });
  dropzone.addEventListener("drop", function (event) {
    if (event.dataTransfer && event.dataTransfer.files) enqueue(event.dataTransfer.files);
  });
  // Файл, брошенный мимо рамки, браузер не должен открывать вместо панели.
  window.addEventListener("dragover", function (event) { event.preventDefault(); });
  window.addEventListener("drop", function (event) { event.preventDefault(); });

  // --- очередь ----------------------------------------------------------------------

  function statusCell(item) {
    var td = el("td");
    td.appendChild(el("span", "st " + item.status, item.status_text));
    if (item.status === "processing") {
      var bar = el("div", "bar");
      var fill = el("i");
      fill.style.width = item.progress + "%";
      bar.appendChild(fill);
      td.appendChild(bar);
    }
    if (item.status === "done" && item.score !== null) {
      td.appendChild(document.createTextNode(" "));
      td.appendChild(el("span", "score " + (item.grade || "na"), item.score_text));
    }
    if (item.status === "error" && item.error) {
      td.appendChild(el("div", "reason", item.error));
    }
    return td;
  }

  function actionCell(item) {
    var td = el("td");
    if (item.status === "done") {
      var a = el("a", "", "Открыть");
      a.href = item.url;
      td.appendChild(a);
    } else if (item.status === "error") {
      var button = el("button", "button small", "Повторить");
      button.type = "button";
      button.addEventListener("click", function () { retry(item.id, button); });
      td.appendChild(button);
    }
    return td;
  }

  function render(data) {
    queueBody.textContent = "";
    var items = data.items || [];
    queueEmpty.hidden = items.length > 0;
    queueBody.parentNode.hidden = items.length === 0;
    items.forEach(function (item) {
      var tr = el("tr");
      tr.appendChild(el("td", "", item.file_name));
      tr.appendChild(el("td", "", item.manager));
      tr.appendChild(statusCell(item));
      tr.appendChild(actionCell(item));
      queueBody.appendChild(tr);
    });
    var c = data.counts || {};
    queueCounts.textContent = items.length
      ? "в очереди " + (c.queued || 0) + " · обрабатывается " + (c.processing || 0) +
        " · готово " + (c.done || 0) + " · ошибок " + (c.error || 0)
      : "";
  }

  function refreshQueue() {
    return fetch("/api/queue", { credentials: "same-origin", cache: "no-store" })
      .then(function (response) {
        if (response.status === 401) { toLogin(); return null; }
        return response.ok ? response.json() : null;
      })
      .then(function (data) { if (data) render(data); })
      .catch(function () { /* сеть моргнула — следующий опрос через 2 с */ });
  }

  function retry(id, button) {
    button.disabled = true;
    fetch("/api/calls/" + id + "/retry", { method: "POST", credentials: "same-origin" })
      .then(function (response) {
        if (response.status === 401) { toLogin(); return; }
        refreshQueue();
      })
      .catch(function () { button.disabled = false; });
  }

  refreshQueue();
  setInterval(function () {
    if (!document.hidden) refreshQueue();
  }, POLL_MS);
})();
