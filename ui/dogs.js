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
 *   - покадровая анимация: для позы ищутся файлы с номером кадра
 *     (<breed>-walk-0-right.png, -1-, -2- ... до 8), меняются каждые
 *     frame_duration_ms из assets/frame-duration.txt; если кадров нет —
 *     используется безымянный файл (старая двухкадровая схема);
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
    _list: [],               // отсортированный список файлов (для поиска кадров)
    _frameCache: new Map(),  // "breed|pose|dir" -> массив URL кадров
    drawnBreeds: new Set(),  // породы, для которых в assets есть свои PNG

    has(name) { return this.files.has(name); },

    // Кадры одной породы/позы: сначала точное имя "<b>-<pose>-<N>-<dir>.png",
    // затем свободный поиск (префикс "<b>-<pose>", N-й кадр, нужная сторона в
    // имени — husky_walk_2_left.png тоже найдётся).
    _poseFrames(b, pose, dir, startIdx) {
      const urls = [];
      const other = dir === "left" ? "right" : "left";
      for (let i = startIdx; i <= 8; i++) {
        const exact = b + "-" + pose + "-" + i + "-" + dir + ".png";
        if (this.has(exact)) { urls.push("/assets/" + exact); continue; }
        const loose = this._looseFrame(b, pose, i, dir);
        if (loose) { urls.push(loose); continue; }
        // старый формат без номера кадра: "<b>-<pose>-<dir>.png"
        if (i === 0) {
          const legacy = b + "-" + pose + "-" + dir + ".png";
          if (this.has(legacy)) urls.push("/assets/" + legacy);
        }
      }
      if (urls.length) return urls;
      // нужной стороны нет — берём другую (applySprite отразит)
      for (let i = startIdx; i <= 8; i++) {
        const exact = b + "-" + pose + "-" + i + "-" + other + ".png";
        if (this.has(exact)) { urls.push("/assets/" + exact + "?flip=1"); continue; }
        const loose = this._looseFrame(b, pose, i, other);
        if (loose) { urls.push(loose + "?flip=1"); continue; }
        if (i === 0) {
          const legacy = b + "-" + pose + "-" + other + ".png";
          if (this.has(legacy)) urls.push("/assets/" + legacy + "?flip=1");
        }
      }
      return urls;
    },

    // Свободный поиск i-го кадра позы: файл начинается с "<b>-<pose>" или
    // "<b>_<pose>", содержит номер кадра и указанную сторону в имени.
    // Файлы старого формата (без номера) здесь не подходят — их ловит legacy.
    _looseFrame(b, pose, idx, dir) {
      const rxNum = new RegExp("[-_]" + idx + "(?:\\.png|[-_])");
      for (const f of this._list) {
        const fl = f.toLowerCase();
        if (!(fl.startsWith(b + "-" + pose) || fl.startsWith(b + "_" + pose))) continue;
        if (!fl.endsWith(".png")) continue;
        if (!rxNum.test(fl)) continue;
        if (!fl.includes(dir)) continue;
        return "/assets/" + f;
      }
      return null;
    },

    // Есть ли у позы собственные кадры (у породы или хотя бы у базовой
    // собаки dog-*). Используется, чтобы при ходьбе НЕ подставлять idle:
    // если walk-кадров нет — собака идёт своим idle-циклом, а не «телепает»
    // между двумя позами.
    hasPoseFrames(breed, pose, dir) {
      return this.framesFor(breed, pose, dir, true).length > 0;
    },

    // Кадры анимации для breed+pose+dir (N = 0..8). Если у породы своих
    // кадров нет — фолбэк к базовой собаке dog-* ("generic"), чтобы
    // нарисованная базовая анимация работала для всех пород без PNG.
    // strict=true: только кадры самой позы, БЕЗ подстановки idle-фолбэка
    // (нужно движку, чтобы отличать «есть walk» от «walk нет, идём в idle»).
    framesFor(breed, pose, dir, strict) {
      const key = breed + "|" + pose + "|" + dir + (strict ? "|s" : "");
      if (this._frameCache.has(key)) return this._frameCache.get(key);
      const b = String(breed).toLowerCase(), d = String(dir).toLowerCase();
      let urls = this._poseFrames(b, pose, d, 0);
      if (!strict) {
        if (!urls.length && pose !== "idle") urls = this._poseFrames(b, "idle", d, 0);
      }
      if (!urls.length && b !== "dog") {
        urls = this._poseFrames("dog", pose, d, 0);
        if (!strict && !urls.length && pose !== "idle") urls = this._poseFrames("dog", "idle", d, 0);
      }
      this._frameCache.set(key, urls);
      return urls;
    },

    async load() {
      try {
        const r = await fetch("/api/assets", { cache: "no-store" });
        const j = await r.json();
        this.files = new Set(j.files || []);
        this._list = [...this.files].sort();
        this._frameCache.clear();
        this.frameMs = j.frame_duration_ms || 250;
      } catch (e) { /* сеть ещё не готова — останутся заглушки */ }

      const pngs = [...this.files].filter((f) => f.endsWith(".png"));
      // "нарисовано" = любой PNG с префиксом dog-/mod- (любая порода тоже годится)
      this.dogArt = pngs.some((f) => /^(dog|labrador|shepherd|husky|poodle|corgi|beagle|bulldog|dalmatian|greyhound|pug)-/.test(f));
      this.modArt = pngs.some((f) => /^mod-/.test(f));
      this.kennelArt = this.has("kennel.png");
      this.runArt = pngs.some((f) => /-run(-\d)?-(left|right)\.png$/.test(f));

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
      measureSprite(firstDogPng(), (w, h) => {
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

  // первая доступная png собаки — для замера пропорций спрайта
  function firstDogPng() {
    const order = ["dog-idle-0-right.png", "dog-idle-right.png",
                   "dog-walk-0-right.png", "dog-walk-right.png"];
    for (const n of order) if (Assets.has(n)) return n;
    for (const n of [...Assets.files].sort()) {
      if (n.endsWith(".png") && !/^kennel/.test(n) && /-(idle|walk|run|sleep)/.test(n)) return n;
    }
    return "dog-idle-right.png";
  }

  function measureSprite(name, cb) {
    if (!name || !Assets.has(name)) return;
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
  // Породы собак: один чатер = одна порода (детерминированно по нику)
  // =========================================================================
  // Порода = префикс имени PNG-файла: <breed>-idle-right.png и т.д.
  // Список легко расширять — добавьте строку и нарисуйте файлы с таким
  // префиксом; движок сам начнёт их подхватывать.
  const BREEDS = {
    labrador:  "labrador",   // 🦮 ретривер
    shepherd:  "shepherd",   // 🐕‍🦺 овчарка
    husky:     "husky",      // 🐺 хаски
    poodle:    "poodle",     // 🐩 пудель
    corgi:     "corgi",
    beagle:    "beagle",
    bulldog:   "bulldog",
    dalmatian: "dalmatian",
    greyhound: "greyhound",
    pug:       "pug",
  };
  const MOD_BREEDS = ["shepherd", "labrador", "husky"]; // служебные на крышу
  const FALLBACK_EMOJI = ["🐶", "🐕", "🦮", "🐕‍🦺", "🐩", "🐺"];

  function hashStr(s) {
    let h = 0;
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return h;
  }
  // Есть ли у породы собственные PNG в assets? Если нет — заводим её как
  // "generic": движок сам фолбэкнется на базовую собаку dog-* (или emoji).
  // Порода считается нарисованной, если есть файл "<breed>-..." или любой
  // файл с именем породы (нестандартные имена автора тоже годятся).
  function isBreedDrawn(breed) {
    if (Assets.drawnBreeds.has(breed)) return true;
    const b = String(breed).toLowerCase();
    return [...Assets.files].some((f) => f.toLowerCase().includes(b));
  }
  // login -> имя породы (стабильно между перезагрузками страницы/сервера)
  function breedNameFor(login, mod) {
    const key = String(login).toLowerCase();
    const names = mod ? MOD_BREEDS : Object.keys(BREEDS);
    const pick = names[hashStr(key) % names.length];
    return isBreedDrawn(pick) ? pick : "generic";
  }
  // login -> emoji заглушки (пока PNG этой породы не нарисованы)
  function breedEmoji(login, mod) {
    const key = String(login).toLowerCase();
    if (mod) return "🐕‍🦺";
    return FALLBACK_EMOJI[hashStr(key) % FALLBACK_EMOJI.length];
  }

  // =========================================================================
  // Выбор файла спрайта: порода + поза + направление, с фолбэками
  // =========================================================================
  // Порядок поиска (первое совпадение выигрывает):
  //   1. <breed>-<pose>-<dir>.png        точный файл нужной стороны
  //   2. <breed>-<pose>-<other>.png      зеркалим существующую сторону
  //   3. <breed>-idle-...                позы нет — берём idle породы
  //   4. dog-<pose>-<dir>.png            базовый универсальный спрайт
  //   5. любой файл содержащий "<breed>" (нестандартные имена автора)
  // Если ничего нет -> null -> показываем emoji-заглушку породы.
  function findLooseBreedFile(breed, dir) {
    const b = breed.toLowerCase();
    const other = dir === "left" ? "right" : "left";
    // совпадение по началу имени файла (husky-walk-left.png, husky_walk_left.png и т.п.)
    const files = [...Assets.files].filter((f) => f.toLowerCase().startsWith(b + "-") ||
                                                 f.toLowerCase().startsWith(b + "_"));
    const hit = files.find((f) => f.toLowerCase().includes(dir)) ||
                files.find((f) => f.toLowerCase().includes(other));
    if (!hit) return null;
    return { file: hit, flip: !hit.toLowerCase().includes(dir) };
  }

  function spriteFor(breed, pose, dir) {
    const b = BREEDS[breed] || breed || "dog";
    const other = dir === "left" ? "right" : "left";
    const chain = [];
    // файлы породы
    chain.push([b + "-" + pose + "-" + dir + ".png", false]);
    chain.push([b + "-" + pose + "-" + other + ".png", true]);
    if (pose !== "idle") {
      chain.push([b + "-idle-" + dir + ".png", false]);
      chain.push([b + "-idle-" + other + ".png", true]);
    }
    for (const [f, flip] of chain) {
      if (Assets.has(f)) return { url: "/assets/" + f, flip };
    }
    // свободный поиск файлов породы ДО базового dog-*
    const loose = findLooseBreedFile(b, dir);
    if (loose) return { url: "/assets/" + loose.file, flip: loose.flip };
    // базовый universal-спрайт dog-*
    const base = [
      ["dog-" + pose + "-" + dir + ".png", false],
      ["dog-" + pose + "-" + other + ".png", true],
      ["dog-" + pose + "-0-" + dir + ".png", false],   // 1-кадровая схема
      ["dog-" + pose + "-0-" + other + ".png", true],
    ];
    if (pose !== "idle") {
      base.push(["dog-idle-" + dir + ".png", false]);
      base.push(["dog-idle-" + other + ".png", true]);
      base.push(["dog-idle-0-" + dir + ".png", false]);
      base.push(["dog-idle-0-" + other + ".png", true]);
    }
    for (const [f, flip] of base) {
      if (Assets.has(f)) return { url: "/assets/" + f, flip };
    }
    return null; // -> emoji-заглушка
  }

  // spriteEl.dataset.sprite хранит "breed|pose|dir" — по нему выбираем кадр,
  // чтобы при смене собаки не сбрасывать счётчик анимации.
  function applySprite(spriteEl, holderEl, breed, pose, dir, tintCss) {
    const frames = Assets.framesFor(breed, pose, dir);
    let s;
    if (frames.length) {
      s = { url: frames[0], flip: /\?flip=1$/.test(frames[0]) };
    } else {
      s = spriteFor(breed, pose, dir);
    }
    if (!s) {
      holderEl.classList.add("placeholder");
      spriteEl.style.backgroundImage = "";
      spriteEl.textContent = breedEmoji(holderEl.dataset.login || "",
                                        holderEl.classList.contains("mod-dog"));
      return;
    }
    holderEl.classList.remove("placeholder");
    spriteEl.textContent = "";
    spriteEl.dataset.sprite = breed + "|" + pose + "|" + dir;
    spriteEl.dataset.frames = JSON.stringify(frames);
    spriteEl.style.backgroundImage = 'url("' + s.url + '")';
    // собственное зеркало только когда файл отражён относительно нужной стороны
    spriteEl.style.transform = s.flip ? "scaleX(-1)" : "";
    if (tintCss) spriteEl.style.setProperty("--tint-rot", tintCss);
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
  // Покадровая анимация: глобальный счётчик кадров, все собаки синхронно.
  // Для позы с нарисованными кадрами (0..N) проигрывается вся цепочка этой
  // позы. При ходьбе используются ТОЛЬКО walk-кадры — idle в цикл шага не
  // подмешивается. Старая двухкадровая схема (walk/idle без номеров) тоже
  // листается как цикл из двух кадров той же позы.
  // =========================================================================
  let globalFrame = 0;       // номер текущего кадра многокадровой анимации
  function startFrameTimer() {
    // пересоздаём интервал при перезагрузке ассетов (мог измениться frame-duration.txt)
    if (startFrameTimer._t) clearInterval(startFrameTimer._t);
    startFrameTimer._t = setInterval(() => {
      globalFrame++;
      for (const [, d] of dogs) { if (!d.leaving) updateDogFrame(d); }
      for (const [, el] of mods) {
        const st = el._st || {};
        // модератор на будке всегда в idle-цикле (если есть mod-idle-кадры),
        // walk ему не подмешиваем
        advanceFrames(el.querySelector(".sprite"));
      }
    }, Math.max(60, Assets.frameMs || 250));
  }

  // Продвинуть спрайт к следующему кадру его текущей анимации
  function advanceFrames(spriteEl) {
    if (!spriteEl) return;
    let frames = [];
    try { frames = JSON.parse(spriteEl.dataset.frames || "[]"); } catch (e) {}
    if (frames.length < 2) return;                    // одиночный кадр — нечего листать
    const idx = globalFrame % frames.length;
    const url = frames[idx];
    spriteEl.style.backgroundImage = 'url("' + url + '")';
    spriteEl.style.transform = /\?flip=1$/.test(url) ? "scaleX(-1)" : "";
  }

  function updateDogFrame(d) {
    const moving = Math.abs(d.target - d.x) > 2;
    const breed = d.st && d.st.breed ? d.st.breed : "mutt";
    const dirNow = d.faceLeft ? "left" : "right";
    // есть ли собственные walk-кадры у этой породы или хотя бы у базовой dog-*
    const hasWalk = Assets.hasPoseFrames(breed, "walk", dirNow);
    let pose;
    if (!moving) pose = "idle";
    else if (!hasWalk) pose = "idle";                  // walk-кадров нет — идём своим idle-циклом
    else if (d.running && Assets.runArt && Assets.hasPoseFrames(breed, "run", dirNow)) pose = "run";
    else pose = "walk";                                // при ходьбе ТОЛЬКО walk-кадры, без idle
    const dir = dirNow;
    const st = d.st || (d.st = {});
    const key = st.breed + "|" + pose + "|" + dir;
    if (st.pose === pose && st.dir === dir && d.spriteEl.dataset.sprite === key) {
      advanceFrames(d.spriteEl);                       // поза та же — листаем кадры
      return;
    }
    st.pose = pose; st.dir = dir;
    applySprite(d.spriteEl, d.el, st.breed || "mutt", pose, dir, st.tint);
    advanceFrames(d.spriteEl);                         // встать в общий поток анимации
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
    const st = (d.st = d.st || {});
    st.tint = tint || "";
    st.breed = breedNameFor(d.el.dataset.login, false); // порода привязана к нику
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
    el._st.breed = breedNameFor(login, true); // служебная порода для крышы
    applySprite(el.querySelector(".sprite"), el, el._st.breed, "idle", "right", tint);
  }
  function refreshModSprite(el) {
    const st = el._st || {};
    applySprite(el.querySelector(".sprite"), el, st.breed || "shepherd",
                st.pose || "idle", "right", st.tint);
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
