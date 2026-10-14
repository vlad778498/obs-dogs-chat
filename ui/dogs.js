/* Собаки чата — клиент для OBS Browser Source.
 * Данные приходят через SSE (/api/stream); если SSE недоступен — опрос /api/dogs.
 * Обычные чатеры-собаки бродят по газону у нижнего края экрана,
 * появляются из будки справа и уходят обратно в неё;
 * собаки-модераторы сидят на крыше будки и следят за остальными.
 */
(function () {
  "use strict";

  const yard = document.getElementById("yard");
  const roost = document.getElementById("mod-roost");
  const counter = document.getElementById("counter");
  const statusEl = document.getElementById("status");

  // "порода" по нику: один чатер = одна собака
  const BREEDS = ["🐶", "🐕", "🦮", "🐕‍🦺", "🐩", "🐺"];
  const MOD_BREEDS = ["🐕‍🦺", "🦮", "🐺"]; // служебные — для модераторов
  function hashStr(s) {
    let h = 0;
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return h;
  }
  function breedFor(login, mod) {
    const list = mod ? MOD_BREEDS : BREEDS;
    return list[hashStr(String(login).toLowerCase()) % list.length];
  }

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

  // --- физика выгула -------------------------------------------------------
  // Каждая собака имеет позицию x (px от левого края зоны выгула),
  // смещение по глубине bottom (px) и скорость. Тик раз в WALK_TICK мс:
  // собака идёт к своей цели, иногда меняет её, иногда бежит (zoomies).
  const WALK_TICK = 1200;      // мс между решениями о движении
  const DOG_W = 92;            // ширина карточки (.dog в CSS)
  const dogs = new Map();      // login -> {el, x, bottom, target, speed, running}

  function walkArea() {
    return { w: Math.max(100, yard.clientWidth), h: Math.max(20, yard.clientHeight) };
  }

  function spawnX() {
    // выходим из будки: правый край зоны выгула
    const { w } = walkArea();
    return w - DOG_W - rand(0, 30);
  }

  function rand(a, b) { return a + Math.random() * (b - a); }

  function pickTarget(d) {
    const { w } = walkArea();
    d.target = rand(4, Math.max(5, w - DOG_W - 4));
    d.bottom = rand(0, Math.max(2, walkArea().h - 70)); // глубина "газона"
    d.running = Math.random() < 0.18;                  // иногда.zoomies
    d.speed = d.running ? rand(140, 260) : rand(35, 80); // px/сек
  }

  function applyPose(d) {
    const el = d.el;
    el.style.left = d.x.toFixed(0) + "px";
    el.style.bottom = d.bottom.toFixed(0) + "px";
    const goingLeft = d.target < d.x;
    el.classList.toggle("face-left", goingLeft);
    el.classList.toggle("running", d.running);
    // длительность CSS-перехода зависит от скорости: плавная ходьба/бег
    const dur = Math.min(3.5, Math.max(0.6, WALK_TICK / 1000 * (d.running ? 1.6 : 1)));
    el.style.transitionDuration = dur.toFixed(2) + "s";
  }

  function tickWalk() {
    const dt = WALK_TICK / 1000;
    for (const [, d] of dogs) {
      if (d.leaving) continue;
      const dx = d.target - d.x;
      const step = d.speed * dt;
      if (Math.abs(dx) <= step) {
        d.x = d.target;
        if (Math.random() < 0.7) pickTarget(d); // дошла — выбираем куда дальше
      } else {
        d.x += Math.sign(dx) * step;
      }
      applyPose(d);
    }
  }
  setInterval(tickWalk, WALK_TICK);

  // --- карточка обычной собаки ----------------------------------------------
  function fillDog(el, dogData, windowSec) {
    const colorStyle = /^#[0-9a-fA-F]{6}$/.test(dogData.color || "")
      ? ' style="color:' + dogData.color + '"' : "";
    el.innerHTML =
      '<div class="sprite">' + breedFor(dogData.login, false) + "</div>" +
      '<div class="name"' + colorStyle + ">" + escapeHtml(dogData.name) + "</div>" +
      '<div class="meta">' + dogData.messages + " · " + fmtAgo(dogData.seconds_ago) + "</div>" +
      '<div class="timer"><i style="width:' +
      Math.round(Math.max(0, Math.min(1, 1 - dogData.seconds_ago / windowSec)) * 100) + '%"></i></div>';
    el.classList.toggle("hot", dogData.seconds_ago < 300);
  }

  function addWalker(login, data) {
    const el = document.createElement("div");
    el.className = "dog";
    el.dataset.login = login;
    fillDog(el, data);
    const d = {
      el, x: spawnX(), bottom: rand(0, 40), target: 0, speed: 60,
      running: false, leaving: false,
    };
    fillDog(el, data, 3600);
    pickTarget(d);
    applyPose(d);
    yard.appendChild(el);
    // стартовая позиция без анимации — чтобы "вышла" из-за будки
    el.style.transitionDuration = "0s";
    requestAnimationFrame(() => requestAnimationFrame(() => {
      el.style.transitionDuration = "";
      applyPose(d);
    }));
    dogs.set(login, d);
  }

  function removeWalker(login) {
    const d = dogs.get(login);
    if (!d || d.leaving) return;
    dogs.delete(login);
    d.leaving = true;
    // уходит в будку: идём к правому краю, затем fadeout
    const { w } = walkArea();
    d.target = w + DOG_W;
    d.running = true; d.speed = 160;
    applyPose(d);
    setTimeout(() => d.el.remove(), 2600);
  }

  // --- модераторы на крыше ----------------------------------------------------
  const mods = new Map(); // login -> element
  function renderMod(login, data) {
    let el = mods.get(login);
    if (!el) {
      el = document.createElement("div");
      el.className = "mod-dog";
      mods.set(login, el);
      roost.appendChild(el);
    }
    const colorStyle = /^#[0-9a-fA-F]{6}$/.test(data.color || "")
      ? ' style="color:' + data.color + '"' : "";
    el.innerHTML =
      '<div class="breed">' + breedFor(login, true) + "</div>" +
      '<div class="name"' + colorStyle + ">" + escapeHtml(data.name) + "</div>" +
      '<div class="badge-shield">🛡️</div>';
  }

  // --- основной рендер по данным с сервера -------------------------------------
  function render(data) {
    const windowSec = data.active_window_seconds || 3600;
    const list = data.dogs || [];
    const seen = new Set();

    const walkers = [];
    const moderators = [];
    for (const dd of list) {
      (dd.mod ? moderators : walkers).push(dd);
    }

    // обычные: добавить новых, обновить, удалить ушедших
    const curWalk = new Set(walkers.map((d) => d.login));
    for (const dd of walkers) {
      seen.add(dd.login);
      const d = dogs.get(dd.login);
      if (d) {
        fillDog(d.el, dd, windowSec);
      } else {
        addWalker(dd.login, dd);
        fillDog(dogs.get(dd.login).el, dd, windowSec);
      }
    }
    for (const [login] of dogs) {
      if (!curWalk.has(login)) removeWalker(login);
    }

    // модераторы на крыше (максимум 5, самые свежие)
    const topMods = moderators.slice(0, 5);
    const curMods = new Set(topMods.map((d) => d.login));
    for (const dd of topMods) renderMod(dd.login, dd);
    for (const [login, el] of mods) {
      if (!curMods.has(login)) { el.remove(); mods.delete(login); }
    }

    counter.textContent = list.length + " активных 🐾 · модераторов на будке: " + moderators.length;
    statusEl.textContent = "онлайн · " + new Date().toLocaleTimeString();
  }

  // --- источник данных ---------------------------------------------------------
  let sseOk = false;
  try {
    const es = new EventSource("/api/stream");
    es.onopen = () => { sseOk = true; };
    es.onmessage = (ev) => {
      sseOk = true;
      try { render(JSON.parse(ev.data)); } catch (e) { /* ignore */ }
    };
    es.onerror = () => { statusEl.textContent = "переподключение к серверу…"; };
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
  setTimeout(() => { if (!sseOk) poll(); }, 4000);
})();
