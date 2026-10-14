/* Собаки чата — клиент для OBS Browser Source.
 * Данные приходят через SSE (/api/stream); если SSE недоступен — опрос /api/dogs.
 *
 * Композиция:
 *   - собаки бродят ТОЛЬКО влево-вправо по нижнему краю экрана (газона нет);
 *   - справа стоит будка, из неё собаки выходят и в неё уходят;
 *   - модераторы сидят на крыше будки и следят за остальными.
 *
 * Система ассетов (рисует автор, см. ui/assets/README.md):
 *   - сервер отдаёт список файлов из ui/assets через /api/assets;
 *   - клиент определяет, какие PNG уже нарисованы, и переключается на них
 *     автоматически (до этого — эмодзи-заглушки);
 *   - анимация "в пару кадров": idle/walk меняются местами каждые
 *     frame_duration_ms из assets/frame-duration.txt;
 *   - ориентация: файлы с суффиксом -right/-left используются как есть,
 *     если left-версии нет — right-спрайт зеркалится CSS-ом;
 *   - размеры на экране задаются в CSS (--dog-size, --kennel-w),
 *     пропорции PNG подхватываются автоматически.
 */
(function () {
  "use strict";

  const yard = document.getElementById("yard");
  const kennel = document.getElementById("kennel");
  const roost = document.getElementById("mod-roost");
  const metrics = document.getElementById("metrics");
  const assetsInfo = document.getElementById("assets-info");

  // OBS browser source имеет obs-browser в userAgent — скрываем тестовую панель
  if (/obs/i.test(navigator.userAgent)) document.body.classList.add("obs");

  // =========================================================================
  // Реестр ассетов: что уже нарисовано
  // =========================================================================
  const Assets = {
    files: new Set(),        // имена файлов в ui/assets
    frameMs: 250,            // длительность одного кадра, мс
    dogArt: false,           // есть ли хотя бы один dog-sprite
    modArt: false,           // есть ли mod-sprite
    kennelArt: false,        // есть ли kennel.png
    runArt: false,           // отдельный кадр бега (опционально)
    ratio: 1,                // ширина/высота спрайта собаки (из PNG)

    has(name) { return this.files.has(name); },

    async load() {
      try {
        const r = await fetch("/api/assets", { cache: "no-store" });
        const j = await r.json();
        this.files = new Set(j.files || []);
        this.frameMs = j.frame_duration_ms || 250;
      } catch (e) { /* сеть ещё не готова — останутся заглушки */ }

      this.dogArt = this.has("dog-idle-right.png") || this.has("dog-walk-right.png") ||
                    this.has("dog-idle-left.png") || this.has("dog-walk-left.png");
      this.modArt = this.has("mod-idle-right.png") || this.has("mod-idle-left.png") ||
                    this.has("mod-walk-right.png") || this.has("mod-walk-left.png");
      this.kennelArt = this.has("kennel.png");
      this.runArt = this.has("dog-run-right.png") || this.has("dog-run-left.png");

      kennel.classList.toggle("has-art", this.kennelArt);
      renderPlaceholderKennel();
      if (assetsInfo) {
        const parts = [
          this.kennelArt ? "будка✓" : "будка✗",
          this.dogArt ? "собаки✓" : "собаки✗",
          this.modArt ? "модер✓" : "модер✗",
          "кадр " + this.frameMs + "мс",
        ];
        assetsInfo.textContent = parts.join(" · ");
      }
      // узнаём реальные пропорции PNG, чтобы aspect-ratio в CSS совпал
      measureSprite("dog-idle-right.png", (w, h) => {
        this.ratio = w / h;
        document.documentElement.style.setProperty("--sprite-ar", this.ratio.toFixed(4));
      });
      measureSprite("kennel.png", (w, h) => {
        document.documentElement.style.setProperty("--kennel-ar", (w / h).toFixed(4));
      });
      // обновляем уже стоящих на сцене собак/модераторов
      for (const [, d] of dogs) refreshDogSprite(d);
      for (const [, el] of mods) refreshModSprite(el);
    },
  };

  function measureSprite(name, cb) {
    if (!Assets.has(name)) return;
    let img = metrics.querySelector('img[data-name="' + name + '"]');
    if (img && img.naturalWidth) { cb(img.naturalWidth, img.naturalHeight); return; }
    img = document.createElement("img");
    img.dataset.name = name;
    img.onload = () => cb(img.naturalWidth || 1, img.naturalHeight || 1);
    img.src = "/assets/" + name;
    metrics.appendChild(img);
  }

  // CSS-заглушка будки (пока нет kennel.png)
  function renderPlaceholderKennel() {
    let ph = kennel.querySelector(".placeholder");
    if (Assets.kennelArt) { if (ph) ph.remove(); return; }
    if (!ph) {
      ph = document.createElement("div");
      ph.className = "placeholder";
      ph.innerHTML = '<span class="ph-roof"></span><span class="ph-body"></span>' +
                     '<span class="ph-door"></span>';
      kennel.prepend(ph);
    }
  }

  // =========================================================================
  // Выбор файла спрайта: поза + направление, с фолбэками и авто-зеркалированием
  // =========================================================================
  // возвращает {url, flip} или null (тогда рисуем эмодзи-заглушку)
  function spriteFor(prefix, pose, dir) { // prefix: "dog"|"mod"; pose: idle|walk|run
    const other = dir === "left" ? "right" : "left";
    const cands = [
      [prefix + "-" + pose + "-" + dir + ".png", false],   // идеал: нужная сторона
      [prefix + "-" + pose + "-" + other + ".png", true],  // зеркалим существующую
    ];
    if (pose !== "idle") { // walk/run отсутствуют — используем idle
      cands.push([prefix + "-idle-" + dir + ".png", false],
                 [prefix + "-idle-" + other + ".png", true]);
    }
    for (const [f, flip] of cands) {
      if (Assets.has(f)) return { url: "/assets/" + f, flip };
    }
    return null;
  }

  function applySprite(spriteEl, holderEl, prefix, pose, dir, tintCss) {
    const s = spriteFor(prefix, pose, dir);
    if (!s) {
      holderEl.classList.add("placeholder");
      spriteEl.style.backgroundImage = "";
      spriteEl.textContent = breedFor(holderEl.dataset.login || "", prefix === "mod");
      return;
    }
    holderEl.classList.remove("placeholder");
    spriteEl.textContent = "";
    spriteEl.style.backgroundImage = 'url("' + s.url + '")';
    // собственное зеркало только когда файл отражён относительно нужной стороны
    spriteEl.style.transform = s.flip ? "scaleX(-1)" : "";
    if (tintCss) spriteEl.style.setProperty("--tint-rot", tintCss);
  }

  // =========================================================================
  // "Порода" для эмодзи-заглушки: один чатер = одна собака
  // =========================================================================
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

  // тонировка собаки в цвет ника чатера (Toca Boca style).
  // Отключается одной строкой в CSS: удалите правило .tinted.
  function tintFor(colorHex) {
    if (!/^#[0-9a-fA-F]{6}$/.test(colorHex || "")) return null;
    const r = parseInt(colorHex.slice(1, 3), 16) / 255;
    const g = parseInt(colorHex.slice(3, 5), 16) / 255;
    const b = parseInt(colorHex.slice(5, 7), 16) / 255;
    const max = Math.max(r, g, b), min = Math.min(r, g, b);
    let hh = 0;
    if (max !== min) {
      const d = max - min;
      if (max === r) hh = (g - b) / d + (g < b ? 6 : 0);
      else if (max === g) hh = (b - r) / d + 2;
      else hh = (r - g) / d + 4;
      hh *= 60;
    }
    // базовый hue после sepia+saturate ≈ 50° (оранжевый) → крутим до нужного
    return (((hh - 50) % 360 + 360) % 360).toFixed(1) + "deg";
  }

  // =========================================================================
  // Физика выгула: ТОЛЬКО горизонтальное движение по нижней кромке
  // =========================================================================
  const WALK_TICK = 250;       // мс между шагами симуляции
  const dogs = new Map();      // login -> {el, spriteEl, x, target, speed, running, leaving, faceLeft}

  function areaW() { return Math.max(200, yard.clientWidth); }
  function dogW() {
    const sz = parseFloat(getComputedStyle(document.documentElement)
      .getPropertyValue("--dog-size"));
    return (isNaN(sz) ? 110 : sz) * (Assets.ratio || 1);
  }
  function rand(a, b) { return a + Math.random() * (b - a); }

  function spawnX() {
    // выходим из будки: правый край экрана минус ширина будки
    const kw = parseFloat(getComputedStyle(document.documentElement)
      .getPropertyValue("--kennel-w")) || 170;
    return areaW() - kw - rand(0, 40);
  }

  function pickTarget(d) {
    const w = areaW(), dw = dogW();
    d.target = rand(4, Math.max(6, w - dw - 4));
    d.running = Math.random() < 0.18;                   // иногда zoomies
    d.speed = d.running ? rand(140, 260) : rand(35, 80); // px/сек
  }

  function applyPose(d) {
    const el = d.el;
    el.style.left = d.x.toFixed(0) + "px";              // bottom не трогаем — пол фиксирован
    d.faceLeft = d.target < d.x - 1;
    el.classList.toggle("face-left", d.faceLeft);
    el.classList.toggle("running", d.running);
    updateDogFrame(d);
  }

  function tickWalk() {
    const dt = WALK_TICK / 1000;
    for (const [, d] of dogs) {
      if (d.leaving) continue;
      const dx = d.target - d.x;
      const step = d.speed * dt;
      if (Math.abs(dx) <= step) {
        d.x = d.target;
        if (Math.random() < 0.35) pickTarget(d);        // дошла — куда идём дальше
      } else {
        d.x += Math.sign(dx) * step;
      }
      applyPose(d);
    }
  }
  setInterval(tickWalk, WALK_TICK);

  // =========================================================================
  // Анимация в пару кадров: глобальный таймер, все собаки синхронно
  // =========================================================================
  let frameToggle = false;
  function startFrameTimer() {
    // пересоздаём интервал при перезагрузке ассетов (мог измениться frame-duration.txt)
    if (startFrameTimer._t) clearInterval(startFrameTimer._t);
    startFrameTimer._t = setInterval(() => {
      frameToggle = !frameToggle;
      for (const [, d] of dogs) { if (!d.leaving) updateDogFrame(d); }
      for (const [, el] of mods) {
        const st = el._st || {};
        const pose = frameToggle && (Assets.has("mod-walk-right.png") ||
                                     Assets.has("mod-walk-left.png")) ? "walk" : "idle";
        if (st.pose !== pose) {
          st.pose = pose; el._st = st;
          applySprite(el.querySelector(".sprite"), el, "mod", pose, "right", st.tint);
        }
      }
    }, Math.max(60, Assets.frameMs || 250));
  }

  function updateDogFrame(d) {
    const moving = Math.abs(d.target - d.x) > 2;
    const hasWalk = Assets.has("dog-walk-right.png") || Assets.has("dog-walk-left.png");
    let pose;
    if (!moving) pose = "idle";
    else if (!hasWalk) pose = "idle";                  // walk-кадров нет — без дёрганья
    else if (d.running && Assets.runArt) pose = "run";
    else pose = frameToggle ? "walk" : "idle";         // переключение в пару кадров
    const dir = d.faceLeft ? "left" : "right";
    const st = d.st || (d.st = {});
    if (st.pose === pose && st.dir === dir) return;
    st.pose = pose; st.dir = dir;
    applySprite(d.spriteEl, d.el, "dog", pose, dir, st.tint);
  }

  // =========================================================================
  // Карточка обычной собаки: спрайт + подпись (ТОЛЬКО ник)
  // =========================================================================
  function makeDogShell(login) {
    const el = document.createElement("div");
    el.className = "dog";
    el.dataset.login = login;
    el.innerHTML = '<div class="sprite"></div><div class="name"></div>';
    el.style.transitionDuration = ((Assets.frameMs || 250) / 1000).toFixed(2) + "s";
    return el;
  }

  function refreshDogSprite(d) {
    const data = d.data;
    if (!data) return;
    const tint = tintFor(data.color);
    d.el.classList.toggle("tinted", !!tint);
    (d.st = d.st || {}).tint = tint || "";
    updateDogFrame(d);
  }

  function addWalker(login, data) {
    const el = makeDogShell(login);
    const d = {
      el, spriteEl: el.querySelector(".sprite"),
      x: spawnX(), target: 0, speed: 60, running: false,
      leaving: false, faceLeft: true, data, st: {},
    };
    el.querySelector(".name").textContent = data.name || login;
    pickTarget(d);
    applyPose(d);
    refreshDogSprite(d);
    yard.appendChild(el);
    // старт без анимации позиции — чтобы "вышла" из-за будки, а не прилетела
    el.style.transitionDuration = "0s";
    requestAnimationFrame(() => requestAnimationFrame(() => {
      el.style.transitionDuration = ((Assets.frameMs || 250) / 1000).toFixed(2) + "s";
      applyPose(d);
    }));
    dogs.set(login, d);
  }

  function removeWalker(login) {
    const d = dogs.get(login);
    if (!d || d.leaving) return;
    dogs.delete(login);
    d.leaving = true;
    // уходит в будку: идёт за правый край, затем fadeout
    d.target = areaW() + dogW();
    d.running = true; d.speed = 160;
    applyPose(d);
    setTimeout(() => { d.el.style.opacity = "0"; }, 1000);
    setTimeout(() => d.el.remove(), 1800);
  }

  // =========================================================================
  // Модераторы на крыше будки
  // =========================================================================
  const mods = new Map(); // login -> element
  function renderMod(login, data) {
    let el = mods.get(login);
    if (!el) {
      el = document.createElement("div");
      el.className = "mod-dog";
      el.dataset.login = login;
      el.innerHTML = '<div class="sprite"></div><div class="name"></div>';
      mods.set(login, el);
      roost.appendChild(el);
    }
    el.querySelector(".name").textContent = data.name || login;
    const tint = tintFor(data.color);
    el._st = el._st || {};
    el._st.tint = tint || "";
    applySprite(el.querySelector(".sprite"), el, "mod", "idle", "right", tint);
  }
  function refreshModSprite(el) {
    const st = el._st || {};
    applySprite(el.querySelector(".sprite"), el, "mod", st.pose || "idle", "right", st.tint);
  }

  // =========================================================================
  // Основной рендер по данным с сервера
  // =========================================================================
  let lastData = null;
  function render(data) {
    lastData = data;
    const list = data.dogs || [];
    const walkers = [], moderators = [];
    for (const dd of list) (dd.mod ? moderators : walkers).push(dd);

    const curWalk = new Set(walkers.map((d) => d.login));
    for (const dd of walkers) {
      const d = dogs.get(dd.login);
      if (d) {
        d.data = dd;
        const nm = d.el.querySelector(".name");
        if (nm.textContent !== dd.name) nm.textContent = dd.name;
        const tint = tintFor(dd.color);
        d.el.classList.toggle("tinted", !!tint);
        (d.st = d.st || {}).tint = tint || "";
        refreshDogSprite(d);
      } else {
        addWalker(dd.login, dd);
      }
    }
    for (const [login] of dogs) if (!curWalk.has(login)) removeWalker(login);

    const topMods = moderators.slice(0, 5);
    const curMods = new Set(topMods.map((d) => d.login));
    for (const dd of topMods) renderMod(dd.login, dd);
    for (const [login, el] of mods) {
      if (!curMods.has(login)) { el.remove(); mods.delete(login); }
    }
  }

  // =========================================================================
  // Источник данных: SSE, фолбэк — polling
  // =========================================================================
  let sseOk = false;
  try {
    const es = new EventSource("/api/stream");
    es.onopen = () => { sseOk = true; };
    es.onmessage = (ev) => {
      sseOk = true;
      try { render(JSON.parse(ev.data)); } catch (e) { /* ignore */ }
    };
    es.onerror = () => { /* браузер сам переподключится */ };
  } catch (e) { poll(); }

  function poll() {
    fetch("/api/dogs").then((r) => r.json()).then(render)
      .catch(() => {})
      .finally(() => setTimeout(poll, 3000));
  }
  setTimeout(() => { if (!sseOk) poll(); }, 4000);

  // =========================================================================
  // Тестовая панель (видна только в обычном браузере, не в OBS)
  // =========================================================================
  const TEST_NAMES = ["tester_one", "tester_two", "grey_wolf", "lucky", "barsik",
                      "hachiko", "rex", "julia", "charlie", "muhorka"];
  const TEST_COLORS = ["#FF5A5F", "#3FA7FC", "#7ED957", "#C77DFF", "#FFB703", ""];
  let testCounter = 0;
  function fakeDog(mod) {
    const n = "test_" + (++testCounter) + "_" + TEST_NAMES[testCounter % TEST_NAMES.length];
    return {
      login: n.toLowerCase(), name: n, mod: !!mod,
      color: TEST_COLORS[Math.floor(Math.random() * TEST_COLORS.length)],
      messages: 1, seconds_ago: 0, last_seen: Math.floor(Date.now() / 1000),
    };
  }
  function injectTest(mod) {
    if (!lastData) lastData = { dogs: [], active_window_seconds: 3600 };
    lastData.dogs.unshift(fakeDog(mod));
    render(lastData);
  }
  const bAdd = document.getElementById("t-add");
  if (bAdd) {
    bAdd.onclick = () => injectTest(false);
    document.getElementById("t-mod").onclick = () => injectTest(true);
    document.getElementById("t-remove").onclick = () => {
      const victim = [...dogs.keys()].find((k) => k.startsWith("test_")) ||
                     (lastData && lastData.dogs[0] && lastData.dogs[0].login);
      if (victim) {
        lastData.dogs = lastData.dogs.filter((d) => d.login !== victim);
        render(lastData);
      }
    };
    document.getElementById("t-assets").onclick = () =>
      Assets.load().then(startFrameTimer);
  }

  Assets.load().then(startFrameTimer);
})();
