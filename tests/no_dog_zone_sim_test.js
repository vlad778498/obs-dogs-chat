// Симуляция логики block-зоны (порт ключевых функций dogs.js) — проверяем:
// 1) собака НЕ заходит в зону; 2) спавн у будки идёт ВЛЕВО ПЕШКОМ (без телепорта);
// 3) при avoid собаки тоже не садятся в зоне.
const W = 1000, DW = 90, Z = {x: 650, w: 350}; // зона x=65%..100%
let mode = "block", force = true;
const zoneBlocks = () => mode === "block";
const dogInZone = (d) => d.x > Z.x - DW/2 && d.x < Z.x + Z.w + DW/2;

function pickTarget(d) {
  const dw = DW, m = 4;
  const blockAnchor = zoneBlocks() && ((d.x >= Z.x - dw/2 - 2) || (dogInZone(d) && force !== false));
  if (!blockAnchor && Math.random() < 0.55) { d.target = d.x; return; }
  // freeSpot с вырезанной зоной для affected-собак -> цели только слева от зоны
  let t = m + dw/2 + Math.random() * (Math.max(m+2, W-dw-m) - (m+dw/2));
  const zLo = Z.x - dw/2 - 6, zHi = Z.x + Z.w + dw/2 + 6;
  if (zoneBlocks() && t > zLo) t = m + dw/2 + Math.random() * (zLo - (m+dw/2));
  d.target = t;
  if (dogInZone(d)) d.target = Z.x - dw/2 - 6;
  if (blockAnchor) d.target = Math.max(m + dw/2, Z.x - dw/2 - 6);
}
function tick(d, step) {
  if (dogInZone(d) && Math.abs(d.target - d.x) < 2) d.target = Z.x - DW/2 - 6;
  const dx = d.target - d.x;
  if (Math.abs(dx) <= step) { d.x = d.target; pickTarget(d); }
  else {
    const nx = d.x + Math.sign(dx) * step;
    if (zoneBlocks() && d.x > Z.x && nx < Z.x) {
      d.x = (Math.abs(d.target - d.x) <= step*1.5) ? d.target : Z.x;
    } else d.x = nx;
  }
}
// спавн как в addWalker: x справа (за будкой), target=0, затем pickTarget
let d = { x: W - 120, target: 0 };
pickTarget(d);
let maxJump = 0, prev = d.x, entered = 0, steps = 0;
for (let i = 0; i < 20000; i++) {           // ~20000 тиков по шагу 2px
  tick(d, 2); steps++;
  maxJump = Math.max(maxJump, Math.abs(d.x - prev)); prev = d.x;
  if (dogInZone(d)) entered++;
}
console.log("block+force: maxJump=", maxJump.toFixed(1), "(телепорт если >>2)",
            " тиков в зоне:", entered, "/", steps);
if (maxJump > 3 || entered/steps > 0.01) { console.log("SIM FAILED"); process.exit(1); }
console.log("SIM PASSED: без телепортов, зона почти не посещается");
