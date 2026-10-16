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
 *   - размеры на экране задаются в CSS (--dog-size — высота собаки, --kennel-h — высота будки),
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
  // Индикатор живости (только вне OBS). Исторический баг «собаки появляются
  // лишь минуту, потом молчат» почти всегда был не в IRC, а в мёртвом SSE:
  // браузер/OBS показывал последний снимок. Теперь прямо на странице видно,
  // когда последний раз пришли ДАННЫЕ: зелёная точка = данные идут, красная =
  // соединения нет/данные не обновляются. В OBS-источнике индикатор скрыт.
  // =========================================================================
  let liveDot = null;
  try {
    liveDot = document.createElement("div");
    liveDot.id = "live-dot";
    liveDot.title = "Индикатор получения данных с сервера";
    Object.assign(liveDot.style, {
      position: "fixed", top: "8px", left: "8px", width: "14px", height: "14px",
      borderRadius: "50%", background: "#e33", zIndex: "9999",
      boxShadow: "0 0 6px rgba(0,0,0,.5)", transition: "background .3s",
    });
    document.body.appendChild(liveDot);
    function setLive(ok) {
      if (!liveDot || document.body.classList.contains("obs")) return;
      liveDot.style.background = ok ? "#2ecc40" : "#e33";
    }
    window.__setDogsLive = setLive;
  } catch (e) { /* украшение — не критично */ }

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
    kennelRatio: 1.5,        // ширина/высота kennel.png (замер JS)
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
      // кадры только другой стороны — переупаковываем их в цикл с правильной
      // нумерацией и flip=1. Важно: если у автора есть walk-0-left, но нет
      // walk-0-right, кадр 0 не должен выпадать из цикла (старый код терял
      // его, и при ходьбе вправо анимация начиналась с шага 2).
      const mirror = [];
      for (let i = startIdx; i <= 8; i++) {
        const exact = b + "-" + pose + "-" + i + "-" + other + ".png";
        if (this.has(exact)) { mirror[i] = "/assets/" + exact + "?flip=1"; continue; }
        const loose = this._looseFrame(b, pose, i, other);
        if (loose) { mirror[i] = loose + "?flip=1"; continue; }
        if (i === 0) {
          const legacy = b + "-" + pose + "-" + other + ".png";
          if (this.has(legacy)) mirror[i] = "/assets/" + legacy + "?flip=1";
        }
      }
      for (const u of mirror) if (u) urls.push(u);
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
    // policy="strict-pose" (используется движком при ходьбе): если у позы нет
    // собственных кадров ни у породы, ни у базовой dog-* — вернуть пустой
    // список. idle НЕ подмешивается в walk-цикл никогда.
    framesFor(breed, pose, dir, strict, policy) {
      const key = breed + "|" + pose + "|" + dir + (strict ? "|s" : "") + (policy === "strict-pose" ? "|p" : "");
      if (this._frameCache.has(key)) return this._frameCache.get(key);
      const b = String(breed).toLowerCase(), d = String(dir).toLowerCase();
      let urls = this._poseFrames(b, pose, d, 0);
      if (!strict && policy !== "strict-pose") {
        if (!urls.length && pose !== "idle") urls = this._poseFrames(b, "idle", d, 0);
      }
      if (!urls.length && b !== "dog") {
        urls = this._poseFrames("dog", pose, d, 0);
        if (!strict && policy !== "strict-pose" && !urls.length && pose !== "idle")
          urls = this._poseFrames("dog", "idle", d, 0);
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
        this.kennelRatio = w / h;   // ширина будки на экране = --kennel-h * kennelRatio
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
  // Конфиг отображения: config/display.json на сервере -> GET /api/config.
  // Применяется НА ЛЕТУ (опрос раз в 5 c): правка файла + обновить browser
  // source в OBS (или просто подождать ~5 c) — без перезапуска сервиса.
  // =========================================================================
  const DisplayCfg = {
    name_font_px: 14, name_color: "#ffffff", name_shadow: true,
    name_gap_px: -14, name_max_len: 0,
    tint_by_name_color: true,
    dog_scale: 1.0, kennel_scale: 1.0,
    min_dog_gap: 8,
    crowd_auto_scale: true, crowd_scale_min: 0.6, crowd_dogs_for_min: 18,
    ground_offset_px: 0, show_mods_on_roof: true,
    no_dog_zone_pct: null,   // зона, где собакам нельзя останавливаться и толпиться;
                             // {x,y,w,h} в % от размеров сцены или null = выкл.
    no_dog_zone_mode: "avoid", // "avoid" — не садятся, но транзитом проходить могут;
                               // "block" — вообще не входят в зону (обходят её)
    no_dog_zone_force: true,   // block: если собака уже внутри зоны (зона появилась
                               // на лету / окно OBS сузилось) — мягко вывести её влево
    no_dog_zone_show: false, // показывать ли контур зоны (для настройки в браузере;
                             // для OBS оставьте false — плашка полностью прозрачна)
    crowdFactor: 1,          // текущий авто-коэффициент толпы (см. applyDisplayCfg)
    displayName(name) {      // ник с учётом name_max_len
      const n = String(name == null ? "" : name);
      const m = this.name_max_len | 0;
      return (m > 0 && n.length > m) ? n.slice(0, m) + "…" : n;
    },
  };

  function cssNumPx(v, fallback) {
    const root = document.documentElement;
    const cur = parseFloat(getComputedStyle(root).getPropertyValue(v));
    return isNaN(cur) ? fallback : cur;
  }

  // Безопасное число: NaN/null/строка -> def; законные 0 и отрицательные — ок.
  function num2(v, def) {
    const n = Number(v);
    return Number.isFinite(n) ? n : def;
  }

  function applyDisplayCfg(c) {
    Object.assign(DisplayCfg, c || {});
    // Защита от «дырявых» значений в display.json: null/NaN/строки приводим
    // к безопасным числам. ВАЖНО: проверяем через Number.isFinite, а не через
    // `x || дефолт` — иначе законное значение 0 (например dog_scale: 0)
    // молча подменялось бы дефолтом и конфиг «не применялся».
    const num = (v, def, lo, hi) => {
      const n = Number(v);
      return Number.isFinite(n) ? Math.max(lo, Math.min(hi, n)) : def;
    };
    DisplayCfg.dog_scale       = num(DisplayCfg.dog_scale, 1.0, 0.2, 5);
    DisplayCfg.kennel_scale    = num(DisplayCfg.kennel_scale, 1.0, 0.2, 5);
    DisplayCfg.name_font_px    = num(DisplayCfg.name_font_px, 14, 6, 200);
    DisplayCfg.ground_offset_px= num(DisplayCfg.ground_offset_px, 0, -500, 500);
    DisplayCfg.crowd_scale_min = num(DisplayCfg.crowd_scale_min, 0.6, 0.2, 1);
    DisplayCfg.crowd_dogs_for_min = num(DisplayCfg.crowd_dogs_for_min, 18, 1, 10000);
    if (!/^#[0-9a-fA-F]{3,8}$/.test(String(DisplayCfg.name_color || "")))
      DisplayCfg.name_color = "#ffffff";
    const root = document.documentElement;
    // масштаб будки
    root.style.setProperty("--kennel-h",
      (cssNumPx("--kennel-base", 150) * DisplayCfg.kennel_scale) + "px");
    // тонировка собак в цвет ника
    const scene = document.getElementById("scene");
    if (scene) scene.classList.toggle("no-tint", !DisplayCfg.tint_by_name_color);
    // ник: размер/цвет/тень/зазор
    root.style.setProperty("--name-font", DisplayCfg.name_font_px + "px");
    root.style.setProperty("--name-color", DisplayCfg.name_color);
    root.style.setProperty("--name-gap", num(DisplayCfg.name_gap_px, -14, -200, 200) + "px");
    if (scene) scene.classList.toggle("no-name-shadow", !DisplayCfg.name_shadow);
    // пол
    root.style.setProperty("--ground", DisplayCfg.ground_offset_px + "px");
    // модераторы на крыше
    if (roost && roost.parentElement)
      roost.parentElement.style.display = DisplayCfg.show_mods_on_roof ? "" : "none";
    // пересобрать подписи (name_max_len мог измениться)
    for (const [, d] of dogs) {
      const nm = d.el.querySelector(".name");
      if (nm && d.data) nm.textContent = DisplayCfg.displayName(d.data.name || d.el.dataset.login);
    }
    for (const [login, el] of mods) {
      const nm = el.querySelector(".name");
      if (nm) nm.textContent = DisplayCfg.displayName(login);
    }
    updateNoDogZone();
    updateCrowdScale();
    // новые границы clampDog актуальны и при старом масштабе
    for (const [, d] of dogs) if (!d.leaving) {
      const before = d.x; clampDog(d);
      if (Math.abs(before - d.x) > 0.5) reposition(d); else applyPose(d);
    }
  }

  // =========================================================================
  // no_dog_zone_pct — «запретная зона»: плашка (алерт/виджет), которую собаки
  // не должны загораживать надолго. Задается в config/display.json как
  // {x, y, w, h} в ПРОЦЕНТАХ от размеров сцены (y — сверху вниз). Собаки:
  //  - не выбирают цель внутри зоны (freeSpot вырезает её из интервалов);
  //  - не останавливаются в ней: если собака уже внутри (зона появилась на
  //    лету / окно OBS сузилось) — ей назначается ближайшая выходная точка.
  // Режим no_dog_zone_mode:
  //  "avoid" (по умолчанию) — то, что выше: сквозь зону можно ПРОХОДИТЬ
  //          транзитом по пути к цели, но нельзя сидеть/толпиться;
  //  "block" — собакам нельзя даже проходить: зона вырезается из целей,
  //          а если путь лежит через неё (например, спавн справа от широкой
  //          зоны на пол-экрана) — цель принудительно ставится СЛЕВА от
  //          зоны, и собака уходит туда ходить. no_dog_zone_force=true при
  //          этом мягко выводит застрявших внутри (телепорт к левому краю).
  // no_dog_zone_show=true рисует пунктирный контур — удобно для настройки в
  // обычном браузере; в OBS держите false (плашка и так полностью прозрачна).
  // =========================================================================
  let NoDogZonePx = null;         // зона в пикселях или null
  function updateNoDogZone() {
    const z = DisplayCfg.no_dog_zone_pct;
    const el = document.getElementById("nodogzone");
    if (!z || typeof z !== "object" || !Number.isFinite(+z.w) || +z.w <= 0 || +z.h <= 0) {
      NoDogZonePx = null;
      if (el) el.style.display = "none";
      return;
    }
    const W = areaW(), H = Math.max(120, yard.clientHeight);
    const zx = (Math.max(0, Math.min(100, +z.x || 0)) / 100) * W;
    const zw = (Math.max(0, Math.min(100, +z.w)) / 100) * W;
    // y=0 — самый низ «пола» (там, где стоят собаки), выше — больше.
    // Так зону рисовать интуитивно: плашка над нижней кромкой = y:0..h:N.
    const zh = (Math.max(0, Math.min(100, +z.h)) / 100) * H;
    const zy = (Math.max(0, Math.min(100, +z.y || 0)) / 100) * H;   // низ зоны
    NoDogZonePx = { x: zx, w: zw, h: zh, yTop: H - zy - zh };       // yTop — верх зоны от верха yard
    if (el) {
      // el живёт ВНУТРИ #yard — координаты совпадают с NoDogZonePx 1:1
      el.style.display = DisplayCfg.no_dog_zone_show ? "block" : "none";
      el.style.left = zx.toFixed(0) + "px";
      el.style.width = zw.toFixed(0) + "px";
      el.style.height = zh.toFixed(0) + "px";
      el.style.bottom = zy.toFixed(0) + "px";
      el.style.top = "auto";
    }
  }

  // Вертикальный диапазон собаки [topY, botY] при позиции центра d.x.
  function dogVerticalSpan(d) {
    const H = Math.max(120, yard.clientHeight);
    const bottom = parseFloat(getComputedStyle(d.el).bottom) || 0;   // учтён --ground
    const dh = d.el.offsetHeight || dogW();
    const top = H - bottom - dh;
    return [top, top + dh];
  }

  // Пересекает ли собаку запретная зона по обеим осям.
  function dogInZone(d) {
    const z = NoDogZonePx;
    if (!z) return false;
    const dw = dogW();
    const [t, b] = dogVerticalSpan(d);
    return (d.x + dw / 2 > z.x) && (d.x - dw / 2 < z.x + z.w) &&
           (b > z.yTop) && (t < z.yTop + z.h);
  }

  // Ближайшая точка вне зоны по горизонтали (для «выдворения» застрявших).
  function zoneEscapeX(d) {
    const z = NoDogZonePx, w = areaW(), dw = dogW();
    const m = Math.max(4, nameOverhang(d));
    const leftFree = z.x - dw / 2 - 6;            // сразу слева от зоны
    const rightFree = z.x + z.w + dw / 2 + 6;     // сразу справа
    const lo = m + dw / 2, hi = w - m - dw / 2;
    const cand = [];
    if (leftFree >= lo) cand.push(leftFree);
    if (rightFree <= hi && !zoneBlocks()) cand.push(rightFree); // в block-режиме
    if (!cand.length) return lo;                  // зона перекрывает всю ширину
    let best = cand[0], bd = Math.abs(cand[0] - d.x);
    for (const c of cand.slice(1)) { const dd = Math.abs(c - d.x); if (dd < bd) { best = c; bd = dd; } }
    return best;
  }

  // Режим "block": зона непроходима — нельзя ни сидеть в ней, ни проходить
  // транзитом. "avoid" (по умолчанию): транзит разрешён, запрещены только
  // остановки/цели внутри зоны.
  function zoneBlocks() {
    return !!NoDogZonePx && String(DisplayCfg.no_dog_zone_mode || "avoid") === "block";
  }

  // Пересекает ли собака зону по ВЕРТИКАЛИ (в отличие от dogInZone — без
  // требования по горизонтали). Собаки живут у «пола», поэтому при y=0..h:N
  // это почти всегда true; если зону подняли высоко — собаки под ней ходят
  // свободно даже в режиме block.
  function dogVerticalOverlap(z) {
    if (!z) return false;
    const H = Math.max(120, yard.clientHeight);
    const bottom = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--ground")) || 0;
    const dh = (cssNumPx("--dog-size", 90) * 1.35) + 34;   // собака + ник сверху
    const botY = H - bottom;                                // низ собаки от верха yard
    return botY > z.yTop && (botY - dh) < z.yTop + z.h;
  }

  // Влияет ли зона на данную собаку: в block — если пересекается по вертикали
  // (сквозь зону ходить нельзя); в avoid — только если реально попадает в неё.
  function zoneAffects(d) {
    if (!NoDogZonePx) return false;
    return zoneBlocks() ? dogVerticalOverlap(NoDogZonePx) : dogInZone(d);
  }

  let lastCfgJson = "";
  let cfgTries = 0;        // сколько раз не удалось загрузить конфиг (для диагностики)
  function fetchDisplayCfg() {
    fetch("/api/config", { cache: "no-store" })
      .then((r) => {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then((c) => {
        if (!c || typeof c !== "object") return;
        cfgTries = 0;
        const j = JSON.stringify(c);
        if (j !== lastCfgJson) { lastCfgJson = j; applyDisplayCfg(c); }
      })
      .catch((e) => {
        // конфиг опционален — живём на предыдущих/дефолтных значениях,
        // но пробуем перезагрузиться чаще (1 c), пока не дойдёт
        cfgTries++;
        setTimeout(fetchDisplayCfg, 1000);
      });
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
  // Ширина ника с учётом transform:translate(-50%): ник выступает за карточку
  // собаки примерно на половину своей ширины в каждую сторону.
  function nameOverhang(d) {
    const nm = d.el.querySelector(".name");
    if (!nm) return 0;
    return Math.max(0, (nm.offsetWidth - dogW()) / 2 + 4);
  }
  // Держим собаку ПОЛНОСТЬЮ внутри экрана вместе с ником — иначе длинный ник
  // залезает за край, а OBS Browser Source трактовал такое переполнение как
  // появление полос прокрутки (страница «съезжала»).
  function clampDog(d) {
    const w = areaW(), dw = dogW();
    const m = Math.max(4, nameOverhang(d));
    const max = Math.max(4, w - dw - m);
    d.x = Math.min(Math.max(d.x, m), max);
    d.target = Math.min(Math.max(d.target, m), max);
  }
  // Пересчёт позиции без CSS-анимации left: иначе «телепорт» в clampDog
  // превращался в плавное скольжение на 0.25 c и собаки «плыли» назад.
  function reposition(d) {
    const el = d.el;
    el.style.transitionDuration = "0s";
    applyPose(d);
    void el.offsetWidth;                                // форсируем layout, чтобы transitionDuration подействовал до следующего left
    requestAnimationFrame(() => requestAnimationFrame(() => {
      el.style.transitionDuration = ((Assets.frameMs || 250) / 1000).toFixed(2) + "s";
    }));
  }
  function rand(a, b) { return a + Math.random() * (b - a); }

  // =========================================================================
  // Толпа: собаки не должны проходить ДРУГ ЧЕРЕЗ ДРУГА и залезать на будку.
  // 1) min_dog_gap — дистанция расталкивания (config/display.json);
  // 2) crowd_auto_scale — при большом числе собак масштаб плавно уменьшается;
  // 3) reserved[] — занятые интервалы: будка (постоянно) и уходящие в неё
  //    собаки (временно). Новые цели выбираются только в свободных интервалах.
  // =========================================================================
  const reserved = [];
  let crowdCur = 1;        // текущий применённый множитель масштаба собак

  function updateCrowdScale() {
    const c = DisplayCfg;
    const n = dogs.size;
    let target = num2(c.dog_scale, 1.0);   // Number.isFinite — законный 0 не подменяем дефолтом
    if (c.crowd_auto_scale && n > (c.crowd_dogs_for_min || 18)) {
      const over = n - (c.crowd_dogs_for_min || 18);
      target *= Math.max(Math.min(1, c.crowd_scale_min || 0.6), 1 - over * 0.03);
    }
    // плавный разгон к целевому масштабу — без рывков при смене числа собак
    crowdCur = Math.abs(crowdCur - target) < 0.01 ? target : crowdCur + (target - crowdCur) * 0.15;
    document.documentElement.style.setProperty("--dog-size",
      (cssNumPx("--dog-base", 90) * crowdCur).toFixed(1) + "px");
  }

  // Пересчёт занятых интервалов перед выбором целей.
  function buildReserved(dw) {
    reserved.length = 0;
    const kh = cssNumPx("--kennel-h", 150);
    const kw = kh * (Assets.kennelRatio || 1.5);
    const kLeft = areaW() - kw;
    reserved.push([Math.max(0, kLeft - dw * 0.6), areaW()]);   // будка
    for (const [, o] of dogs) {                                 // уходящие в будку
      if (o.leaving) reserved.push([o.x - dw / 2, o.x + dw * 1.2]);
    }
  }

  // Случайная точка из незанятых интервалов [lo, hi].
  // Запретная зона (no_dog_zone_pct) вырезается из интервалов, если собака
  // вертикально с ней пересекается — цели внутри зоны не бывает.
  function freeSpot(lo, hi, d) {
    if (hi <= lo) return lo;
    const ivs = [[lo, hi]];
    const allReserved = reserved.slice();
    if (d && zoneAffects(d)) {
      // avoid: зону вырезаем, только если собака реально попадает в неё;
      // block: зона непроходима для всех собак, пересекающих её по вертикали,
      //      — цели справа от зоны для них недосягаемы (см. якорь в pickTarget)
      allReserved.push([NoDogZonePx.x - dogW() / 2 - 6, NoDogZonePx.x + NoDogZonePx.w + dogW() / 2 + 6]);
    }
    for (const [a, b] of allReserved) {
      const out = [];
      for (const [x, y] of ivs) {
        if (b <= x || a >= y) { out.push([x, y]); continue; }
        if (a > x) out.push([x, Math.min(a, y)]);
        if (b < y) out.push([Math.max(b, x), y]);
      }
      ivs.length = 0;
      for (const iv of out) if (iv[1] > iv[0]) ivs.push(iv);
      if (!ivs.length) break;
    }
    if (!ivs.length) return lo;
    const lens = ivs.map(([x, y]) => y - x);
    let r = Math.random() * lens.reduce((s, v) => s + v, 0);
    for (let i = 0; i < ivs.length; i++) {
      if (r <= lens[i]) return ivs[i][0] + r;
      r -= lens[i];
    }
    return ivs[ivs.length - 1][1];
  }

  function spawnX() {
    // выходим из-за будки: ширина будки = её высота * пропорции PNG (замер JS)
    const kh = cssNumPx("--kennel-h", 150);
    const kw = Math.round(kh * (Assets.kennelRatio || 1.5));
    return areaW() - kw - rand(0, 40);
  }

  // Паузы в idle: дошедшая до цели собака иногда «задумывается» на 2–6 сек.
  // Частота и длительность — эти константы.
  const PAUSE_CHANCE = 0.55;   // вероятность паузы при выборе новой цели
  const PAUSE_MIN_MS = 2000;
  const PAUSE_MAX_MS = 6000;

  function pickTarget(d) {
    const w = areaW(), dw = dogW();
    if (Math.random() < PAUSE_CHANCE) {
      // пауза: цель = текущая позиция → moving=false → idle-цикл;
      // через случайное время собака снова выберет цель
      d.target = d.x;
      d.pausedUntil = Date.now() + rand(PAUSE_MIN_MS, PAUSE_MAX_MS);
    } else {
      // границы цели учитывают свес ника (clampDog), иначе длинный ник на
      // правом краю залезет за экран — OBS рисовал полосы прокрутки
      const m = Math.max(4, nameOverhang(d));
      buildReserved(dw);
      // не заводим гуляющую собаку в будку и в хвост уходящей в будку;
      // внутри запретной зоны цель тоже не выбирается (см. freeSpot)
      d.target = freeSpot(m, Math.max(m + 2, w - dw - m), d);
      if (NoDogZonePx && dogInZone(d)) d.target = zoneEscapeX(d); // страховка
      // Режим "block": зона непроходима даже транзитом. Если собака стоит
      // справа от зоны или ВНУТРИ неё (спавн у будки при зоне на пол-экрана;
      // зону включили/расширили на лету) — обычная цель слева была бы
      // недостижима без прохода сквозь зону, поэтому ставим ей «якорь» сразу
      // за левым краем зоны: она уходит влево и ходит уже там.
      // no_dog_zone_force=false — якорь ставится только для собак ПРАВЕЕ зоны
      // (спавн), а застрявших внутри не телепортируем (ждут новую цель).
      if (zoneBlocks() && dogVerticalOverlap(NoDogZonePx)) {
        const leftFree = NoDogZonePx.x - dw / 2 - 6;
        const rightEdge = NoDogZonePx.x + NoDogZonePx.w + dw / 2 + 6;
        if (leftFree >= m + dw / 2) {
          const atOrRightOfZone = d.x >= NoDogZonePx.x - dw / 2 - 2;
          const insideZone = d.x > NoDogZonePx.x - dw / 2 && d.x < rightEdge - dw / 2;
          if (atOrRightOfZone || (insideZone && DisplayCfg.no_dog_zone_force)) {
            d.target = leftFree;
          }
        } else {
          // зона перекрывает всю ширину — держим у левого края экрана
          d.target = m + dw / 2;
        }
      }
      d.pausedUntil = 0;
    }
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
    // сначала сдвиг всех, затем расталкивание — собаки не проходят друг
    // через друга (min_dog_gap из config/display.json, 0 = выключить)
    for (const [, d] of dogs) {
      if (d.leaving) continue;
      // запретная зона: если собака остановилась/зависла внутри плашки —
      // выгоняем к ближайшей точке вне зоны (транзитом пройти можно)
      if (NoDogZonePx && dogInZone(d) && Math.abs(d.target - d.x) < 2) {
        d.target = zoneEscapeX(d);
        d.pausedUntil = 0;
      }
      const dx = d.target - d.x;
      const step = d.speed * dt;
      if (Math.abs(dx) <= step) {
        d.x = d.target;
        // после паузы в idle — снова выбираем цель; без пауз dog-idle всё
        // равно виден: цель достигается, собака стоит до следующего тика
        if (!d.pausedUntil || Date.now() >= d.pausedUntil) {
          d.pausedUntil = 0;
          pickTarget(d);                                // дошла — куда идём дальше
        }
      } else {
        d.x += Math.sign(dx) * step;
      }
      clampDog(d);                                    // ник не должен вылезать за край экрана
    }
    separateDogs();
    for (const [, d] of dogs) if (!d.leaving) applyPose(d);
  }

  // Расталкивание парных пересечений (несколько проходов для плотной толпы).
  function separateDogs() {
    const gapPx = DisplayCfg.min_dog_gap;
    if (!(gapPx > 0)) return;
    const dw = dogW(), minSep = dw + gapPx;
    const arr = [...dogs.values()].filter((d) => !d.leaving)
              .sort((a, b) => a.x - b.x);
    for (let pass = 0; pass < 3; pass++) {
      let moved = false;
      for (let i = 0; i + 1 < arr.length; i++) {
        const a = arr[i], b = arr[i + 1];
        const need = minSep - (b.x - a.x);
        if (need > 0.5) {
          const half = need / 2;
          a.x -= half; b.x += half;
          moved = true;
        }
      }
      for (const d of arr) clampDog(d);               // после расталкивания — снова в кадре
      if (!moved) break;
    }
    // запретная зона: расталкивание могло столкнуть собак под плашку или
    // оставить толпу внутри неё — выгоняем каждую к ближайшему краю зоны
    if (NoDogZonePx && (!zoneBlocks() || DisplayCfg.no_dog_zone_force !== false)) {
      for (const d of arr) {
        if (dogInZone(d)) {
          const before = d.x;
          d.x = zoneEscapeX(d);
          clampDog(d);
          // цель тоже уводим из зоны, иначе собака «сядет» ровно на плашку
          if (d.target !== undefined && Math.abs(d.target - d.x) < dogW()) d.target = d.x;
          else d.target = zoneEscapeX(d);
          if (Math.abs(before - d.x) > 0.5) reposition(d);
        }
      }
    }
  }
  setInterval(tickWalk, WALK_TICK);
  // плавный «разгон» масштаба толпы + пересборка границ при смене числа собак
  setInterval(updateCrowdScale, 400);

  // При сужении окна (изменение размера Browser Source в OBS) собаки могли
  // остаться за правым краем — пересчитываем границы для всех сразу.
  window.addEventListener("resize", () => {
    for (const [, d] of dogs) if (!d.leaving) {
      const before = d.x;
      clampDog(d);
      if (Math.abs(before - d.x) > 0.5) reposition(d); // телепорт без «плытия»
      else applyPose(d);
    }
  });

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
      leaving: false, faceLeft: true, data, st: {}, pausedUntil: 0,
    };
    // В режиме block спавн не должен оказываться внутри запретной зоны:
    // сразу выводим собаку к левому краю зоны (она «выходит» и уходит влево).
    if (zoneBlocks() && dogVerticalOverlap(NoDogZonePx) &&
        d.x > NoDogZonePx.x - dogW() / 2 && DisplayCfg.no_dog_zone_force !== false) {
      const lf = NoDogZonePx.x - dogW() / 2 - 6;
      d.x = Math.max(areaW() * 0.05, lf);
    }
    el.querySelector(".name").textContent = DisplayCfg.displayName(data.name || login);
    pickTarget(d);
    clampDog(d);                                      // ник не вылезает за край с первой позиции
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
    // уходит в будку: идёт за правый край, затем fadeout.
    // #yard теперь с overflow:hidden — собака аккуратно обрезается на границе
    // зоны (раньше уходила «за экран» и создавала переполнение страницы).
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
    el.querySelector(".name").textContent = DisplayCfg.displayName(data.name || login);
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
        const wantName = DisplayCfg.displayName(dd.name || dd.login);
        if (nm.textContent !== wantName) {
          nm.textContent = wantName;
          clampDog(d);                                // ник сменился/вырос — не выпускаем за край
        }
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
  // Источник данных: SSE + параллельный polling-страховка.
  // Почему так: в OBS (CEF) EventSource иногда «зависает» — соединение
  // формально открыто, но данные перестают приходить (например, после
  // перезапуска сервиса или обрыва на несколько минут). Раньше UI молча
  // оставался с последним снимком. Теперь, если от SSE нет сообщений дольше
  // SSE_STALL_MS, включаем опрос /api/dogs каждые POLL_MS, пока поток не
  // оживёт; сообщения SSE всегда приоритетны.
  // =========================================================================
  let lastMsgAt = performance.now();
  let sseBroken = false;
  let pollTimer = null;
  let warnedStall = false;
  try {
    const es = new EventSource("/api/stream");
    es.onmessage = (ev) => {
      lastMsgAt = performance.now();
      sseBroken = false;
      warnedStall = false;
      stopPolling();
      if (window.__setDogsLive) window.__setDogsLive(true);
      try { render(JSON.parse(ev.data)); } catch (e) { /* ignore */ }
    };
    es.onerror = () => { /* браузер сам переподключится; страховка ниже */ };
  } catch (e) { sseBroken = true; }

  function pollOnce() {
    fetch("/api/dogs", { cache: "no-store" })
      .then((r) => r.json())
      .then((d) => {
        if (window.__setDogsLive) window.__setDogsLive(true);
        render(d);
      })
      .catch(() => {});
  }
  function startPolling() {
    if (pollTimer) return;
    pollOnce();
    pollTimer = setInterval(pollOnce, POLL_MS);
    if (!warnedStall) {
      warnedStall = true;
      console.warn("[dogs] SSE молчит дольше " + SSE_STALL_MS / 1000 +
        " c — включён резервный опрос /api/dogs каждые " + POLL_MS / 1000 + " c");
    }
  }
  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  const SSE_STALL_MS = 15000; // нет сообщений SSE 15 сек -> считаем поток мёртвым
  const POLL_MS = 3000;       // частота страховочного опроса
  setInterval(() => {
    if (performance.now() - lastMsgAt > SSE_STALL_MS || sseBroken) startPolling();
    // Индикатор: данные не обновлялись дольше 20 c — точка краснеет.
    if (window.__setDogsLive && performance.now() - lastMsgAt > 20000) {
      window.__setDogsLive(false);
    }
  }, 2000);
  setTimeout(pollOnce, 1500); // первый снимок получим сразу, не дожидаясь SSE

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

  // конфиг отображения: первый запрос сразу + опрос раз в 5 c (правка
  // config/display.json подхватывается без перезапуска сервиса)
  fetchDisplayCfg();
  setInterval(fetchDisplayCfg, 5000);

  Assets.load().then(startFrameTimer);
})();
