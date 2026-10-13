/* Собаки чата — клиентская логика для OBS Browser Source.
 * Данные приходят через SSE (/api/stream); если SSE недоступен — опрос /api/dogs.
 */
(function () {
  "use strict";

  // Детерминированный выбор "породы" по нику: один чатер = одна собака-порода
  const BREEDS = ["🐶", "🐕", "🦮", "🐕‍🦺", "🐩", "🐺"];
  function hashStr(s) {
    let h = 0;
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return h;
  }
  function breedFor(login) {
    return BREEDS[hashStr(String(login).toLowerCase()) % BREEDS.length];
  }

  const yard = document.getElementById("yard");
  const counter = document.getElementById("counter");
  const statusEl = document.getElementById("status");

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function fmtAgo(sec) {
    if (sec < 60) return sec + " сек назад";
    const m = Math.floor(sec / 60);
    if (m < 60) return m + " мин назад";
    return Math.floor(m / 60) + " ч назад";
  }

  function fillDog(el, d, windowSec) {
    el.className = "dog" + (d.seconds_ago < 300 ? " hot" : "");
    const remain = Math.max(0, Math.min(1, 1 - d.seconds_ago / windowSec));
    const colorStyle = /^#[0-9a-fA-F]{6}$/.test(d.color || "") ? ' style="color:' + d.color + '"' : "";
    el.innerHTML =
      '<div class="breed">' + breedFor(d.login) + "</div>" +
      '<div class="name"' + colorStyle + ">" + escapeHtml(d.name) + "</div>" +
      '<div class="meta">' + d.messages + " сообщ. · " + fmtAgo(d.seconds_ago) + "</div>" +
      '<div class="timer"><i style="width:' + Math.round(remain * 100) + '%"></i></div>';
  }

  function render(data) {
    const windowSec = data.active_window_seconds || 3600;
    const dogs = data.dogs || [];
    const seen = new Set();

    const existing = new Map();
    for (const child of Array.from(yard.children)) {
      if (child.dataset.login) existing.set(child.dataset.login, child);
    }

    dogs.forEach((d) => {
      seen.add(d.login);
      let el = existing.get(d.login);
      if (el) {
        fillDog(el, d, windowSec);
      } else {
        el = document.createElement("div");
        el.dataset.login = d.login;
        fillDog(el, d, windowSec);
      }
      yard.appendChild(el); // append переставляет существующий элемент в конец
    });

    // порядок: самые свежие — первыми (order работает во flex-wrap контейнере)
    Array.from(yard.children).forEach((el) => {
      const idx = dogs.findIndex((d) => d.login === el.dataset.login);
      el.style.order = idx === -1 ? 9999 : idx;
    });

    // ушедшие неактивными — с анимацией исчезновения
    for (const [login, el] of existing) {
      if (!seen.has(login)) {
        el.classList.add("leaving");
        setTimeout(() => el.remove(), 500);
      }
    }

    counter.textContent = dogs.length + " активных 🐾";
    statusEl.textContent = "онлайн · обновлено " + new Date().toLocaleTimeString();
  }

  // --- источник данных ------------------------------------------------------
  let sseOk = false;
  try {
    const es = new EventSource("/api/stream");
    es.onopen = () => { sseOk = true; };
    es.onmessage = (ev) => {
      sseOk = true;
      try { render(JSON.parse(ev.data)); } catch (e) { /* ignore */ }
    };
    es.onerror = () => {
      statusEl.textContent = "переподключение к серверу…";
    };
  } catch (e) {
    poll();
  }

  function poll() {
    fetch("/api/dogs")
      .then((r) => r.json())
      .then(render)
      .catch(() => { statusEl.textContent = "сервер недоступен, повтор через 3с"; })
      .finally(() => setTimeout(poll, 3000));
  }
  // страховка: если SSE не открылся за 4 секунды — включаем опрос
  setTimeout(() => { if (!sseOk) poll(); }, 4000);
})();
