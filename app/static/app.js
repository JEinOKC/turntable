const $ = (id) => document.getElementById(id);
const kiosk = new URLSearchParams(location.search).has("kiosk");
if (kiosk) document.body.classList.add("kiosk");

// ---------- controls ----------

let busy = false;

async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

function renderRooms(rooms) {
  const list = $("rooms");
  if (!rooms.length) {
    list.innerHTML = '<li class="empty">No Sonos rooms found.</li>';
    return;
  }
  list.replaceChildren(...rooms.map((r) => {
    const li = document.createElement("li");
    li.className = "room";
    li.innerHTML = `<span class="name"></span>
      <button class="toggle" aria-pressed="${r.on}" aria-label="Play in ${r.name}"></button>
      <input type="range" min="0" max="100" value="${r.volume}" aria-label="${r.name} volume">`;
    li.querySelector(".name").textContent = r.name;
    const toggle = li.querySelector(".toggle");
    toggle.onclick = () => act(toggle, `/api/rooms/${encodeURIComponent(r.name)}`, { on: !r.on });
    const slider = li.querySelector("input");
    slider.onchange = () => act(null, `/api/rooms/${encodeURIComponent(r.name)}`, { volume: +slider.value });
    return li;
  }));
}

async function act(el, path, body) {
  busy = true;
  el?.classList.add("busy");
  try {
    renderRooms(await api(path, body));
  } catch (e) {
    alert(e.message);
    refreshRooms();
  } finally {
    busy = false;
  }
}

async function refreshRooms() {
  if (busy || document.activeElement?.type === "range") return;
  try { renderRooms(await api("/api/rooms")); } catch {}
}

async function refreshStatus() {
  try {
    const s = await api("/api/status");
    const pill = $("signal");
    pill.textContent = !s.capturing ? "Turntable unplugged" : s.signal ? "Live" : "Silent";
    pill.classList.toggle("live", s.capturing && s.signal);
  } catch {}
}

$("stop").onclick = () => act(null, "/api/stop", {});
refreshRooms();
refreshStatus();
setInterval(refreshRooms, 10000);
setInterval(refreshStatus, 2000);

// ---------- visualizer ----------

const canvas = $("canvas");
const ctx = canvas.getContext("2d");
const styles = [record, bars, scope, fireplace];
let style = +(localStorage.vizStyle || 0) % styles.length;
let socket, raf, hideTimer;
let bands = new Float32Array(48), wave = new Float32Array(96), level = 0, angle = 0;
const peaks = new Float32Array(48);

function openViz() {
  $("controls").hidden = true;
  $("viz").hidden = false;
  resize();
  socket = new WebSocket(`ws://${location.host}/ws`);
  socket.onmessage = (m) => {
    const f = JSON.parse(m.data);
    for (let i = 0; i < f.b.length; i++) bands[i] = Math.max(f.b[i], bands[i] * 0.85);
    wave = Float32Array.from(f.w);
    level = Math.max(f.l, level * 0.9);
  };
  showUi();
  raf = requestAnimationFrame(draw);
}

function closeViz() {
  socket?.close();
  cancelAnimationFrame(raf);
  $("viz").hidden = true;
  $("controls").hidden = false;
  refreshRooms();
}

function showUi() {
  $("viz-ui").classList.remove("hide");
  clearTimeout(hideTimer);
  hideTimer = setTimeout(() => $("viz-ui").classList.add("hide"), 3000);
}

function resize() {
  const dpr = devicePixelRatio || 1;
  canvas.width = innerWidth * dpr;
  canvas.height = innerHeight * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

function draw() {
  for (let i = 0; i < bands.length; i++) bands[i] *= 0.97;
  level *= 0.97;
  styles[style](innerWidth, innerHeight);
  raf = requestAnimationFrame(draw);
}

// A spinning record with the spectrum radiating from its edge.
function record(w, h) {
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, w, h);
  const cx = w / 2, cy = h / 2, r = Math.min(w, h) * 0.28;
  const hue = (performance.now() / 80) % 360;
  angle += 0.035;  // ~33⅓ rpm at 60fps

  for (let i = 0; i < bands.length * 2; i++) {
    const v = bands[i < bands.length ? i : bands.length * 2 - 1 - i];
    const a = angle * 0.25 + (i / (bands.length * 2)) * Math.PI * 2;
    const len = 6 + v * r * 0.9;
    ctx.strokeStyle = `hsl(${(hue + i * 4) % 360} 90% ${45 + v * 25}%)`;
    ctx.lineWidth = Math.max(2, (Math.PI * 2 * r) / (bands.length * 2) - 3);
    ctx.beginPath();
    ctx.moveTo(cx + Math.cos(a) * (r + 6), cy + Math.sin(a) * (r + 6));
    ctx.lineTo(cx + Math.cos(a) * (r + 6 + len), cy + Math.sin(a) * (r + 6 + len));
    ctx.stroke();
  }

  ctx.save();
  ctx.translate(cx, cy);
  ctx.rotate(angle);
  ctx.fillStyle = "#0b0b0b";
  ctx.beginPath(); ctx.arc(0, 0, r, 0, Math.PI * 2); ctx.fill();
  ctx.strokeStyle = "rgba(255,255,255,.06)";
  ctx.lineWidth = 1;
  for (let g = r * 0.42; g < r; g += 4) { ctx.beginPath(); ctx.arc(0, 0, g, 0, Math.PI * 2); ctx.stroke(); }
  ctx.strokeStyle = "rgba(255,255,255,.18)";
  ctx.lineWidth = 3;
  ctx.beginPath(); ctx.arc(0, 0, r * 0.8, -0.5, 0.3); ctx.stroke();
  const label = r * (0.33 + level * 0.06);
  ctx.fillStyle = `hsl(${hue} 80% 55%)`;
  ctx.beginPath(); ctx.arc(0, 0, label, 0, Math.PI * 2); ctx.fill();
  ctx.fillStyle = "rgba(0,0,0,.35)";
  ctx.fillRect(-label * 0.6, -label * 0.12, label * 1.2, label * 0.24);
  ctx.fillStyle = "#000";
  ctx.beginPath(); ctx.arc(0, 0, 4, 0, Math.PI * 2); ctx.fill();
  ctx.restore();
}

// Mirrored spectrum bars with falling peak caps.
function bars(w, h) {
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, w, h);
  const n = bands.length, gap = 3, bw = (w - gap * (n + 1)) / n, mid = h / 2;
  for (let i = 0; i < n; i++) {
    const v = bands[i];
    peaks[i] = Math.max(v, peaks[i] - 0.006);
    const bh = v * mid * 0.92, x = gap + i * (bw + gap);
    const grad = ctx.createLinearGradient(0, mid - bh, 0, mid + bh);
    const hue = 280 - (i / n) * 240;
    grad.addColorStop(0, `hsl(${hue} 90% 60%)`);
    grad.addColorStop(0.5, `hsl(${hue} 90% 35%)`);
    grad.addColorStop(1, `hsl(${hue} 90% 60%)`);
    ctx.fillStyle = grad;
    ctx.fillRect(x, mid - bh, bw, bh * 2);
    ctx.fillStyle = "#fff";
    const ph = peaks[i] * mid * 0.92;
    ctx.fillRect(x, mid - ph - 4, bw, 2);
    ctx.fillRect(x, mid + ph + 2, bw, 2);
  }
}

// Glowing oscilloscope line that leaves trails.
function scope(w, h) {
  ctx.fillStyle = "rgba(0,0,0,.18)";
  ctx.fillRect(0, 0, w, h);
  const hue = (performance.now() / 50) % 360, mid = h / 2;
  ctx.lineWidth = 3;
  ctx.strokeStyle = `hsl(${hue} 100% 60%)`;
  ctx.shadowColor = ctx.strokeStyle;
  ctx.shadowBlur = 12;
  ctx.beginPath();
  for (let i = 0; i < wave.length; i++) {
    const x = (i / (wave.length - 1)) * w, y = mid + wave[i] * mid * 0.85;
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  }
  ctx.stroke();
  ctx.shadowBlur = 0;
}

// A log fire whose flames flare with the music (classic "Doom fire" on a small grid, scaled up).
const FIRE_W = 96, FIRE_H = 72;
const fireHeat = new Uint8Array(FIRE_W * FIRE_H);
const fireCanvas = Object.assign(document.createElement("canvas"), { width: FIRE_W, height: FIRE_H });
const fireCtx = fireCanvas.getContext("2d");
const fireImage = fireCtx.createImageData(FIRE_W, FIRE_H);
const firePalette = Array.from({ length: 37 }, (_, i) => {
  const t = i / 36;
  return [Math.min(255, t * 3 * 255), Math.min(255, Math.max(0, t * 3 - 1) * 255), Math.max(0, t * 3 - 2) * 255];
});

function fireplace(w, h) {
  // Feed the bottom row: a steady base fire plus heat from the music, bass in the middle.
  // Slow-moving waves across the row split the fire into separate tongues.
  const t = performance.now() / 1000;
  for (let x = 0; x < FIRE_W; x++) {
    const edge = Math.abs(x / FIRE_W - 0.5) * 2;
    const band = bands[Math.floor(edge * (bands.length * 0.6))];
    const tongue = 0.5 + 0.5 * Math.sin(x * 0.35 + t * 2.3) * Math.sin(x * 0.13 - t * 1.1);
    const heat = edge > 0.7 ? 0 : (14 + tongue * 14 + band * 10) * (1 - edge * 0.6);
    fireHeat[(FIRE_H - 1) * FIRE_W + x] = Math.min(36, heat);
  }
  // Let heat rise, cool and flicker; louder music cools slower so flames reach higher.
  const cooling = 1.6 - level * 0.25;
  for (let y = 1; y < FIRE_H; y++) {
    for (let x = 0; x < FIRE_W; x++) {
      const drift = (Math.random() * 3) | 0;
      const dst = (y - 1) * FIRE_W + Math.min(FIRE_W - 1, Math.max(0, x - drift + 1));
      fireHeat[dst] = Math.max(0, fireHeat[y * FIRE_W + x] - ((Math.random() * cooling) | 0));
    }
  }
  const px = fireImage.data;
  for (let i = 0; i < fireHeat.length; i++) {
    const [r, g, b] = firePalette[fireHeat[i] | 0];
    px[i * 4] = r; px[i * 4 + 1] = g; px[i * 4 + 2] = b; px[i * 4 + 3] = 255;
  }
  fireCtx.putImageData(fireImage, 0, 0);

  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, w, h);
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = "high";
  const fh = h * 0.9;
  ctx.drawImage(fireCanvas, 0, h - fh - h * 0.06, w, fh);

  // Logs and glowing embers in front of the flames.
  const logH = h * 0.09, base = h - logH * 0.6;
  ctx.fillStyle = `rgba(255, ${90 + level * 80}, 20, ${0.35 + level * 0.4})`;
  ctx.beginPath(); ctx.ellipse(w / 2, base + logH * 0.2, w * 0.32, logH * 0.5, 0, 0, Math.PI * 2); ctx.fill();
  for (const [cx, tilt] of [[0.42, 0.05], [0.58, -0.05]]) {
    ctx.save();
    ctx.translate(w * cx, base);
    ctx.rotate(tilt);
    const grad = ctx.createLinearGradient(0, -logH / 2, 0, logH / 2);
    grad.addColorStop(0, "#5a3a22"); grad.addColorStop(1, "#24160c");
    ctx.fillStyle = grad;
    ctx.beginPath(); ctx.roundRect(-w * 0.22, -logH / 2, w * 0.44, logH, logH / 2); ctx.fill();
    ctx.fillStyle = "#8a6a4a";
    ctx.beginPath(); ctx.ellipse(w * (tilt > 0 ? -0.22 : 0.22), 0, logH * 0.3, logH / 2, 0, 0, Math.PI * 2); ctx.fill();
    ctx.restore();
  }
}

function setStyle(n) {
  style = n % styles.length;
  localStorage.vizStyle = style;
  ctx.clearRect(0, 0, innerWidth, innerHeight);
}

$("visualize").onclick = () => { openViz(); tellScreen("viz"); };
$("viz-close").onclick = () => { closeViz(); tellScreen("controls"); };
$("viz-next").onclick = () => { setStyle(style + 1); showUi(); tellScreen("viz"); };

// ---------- Pi screen (set from any phone; the kiosk follows) ----------

let screenState = null;

function tellScreen(mode, s = style) {
  if (kiosk) api("/api/screen", { mode, style: s }).then((st) => (screenState = st)).catch(() => {});
}

function applyScreen(st) {
  const vizOpen = !$("viz").hidden;
  if (st.mode === "viz") {
    if (st.style !== style) setStyle(st.style);
    if (!vizOpen) openViz();
  } else if (vizOpen) {
    closeViz();
  }
  $("dark").hidden = st.mode !== "off";
}

async function followScreen() {
  try {
    const st = await api("/api/screen");
    if (!screenState || st.mode !== screenState.mode || st.style !== screenState.style) applyScreen(st);
    screenState = st;
  } catch {}
}

function markScreen(st) {
  for (const b of document.querySelectorAll("#screen button")) {
    const match = b.dataset.mode === st.mode && (st.mode !== "viz" || +b.dataset.style === st.style);
    b.classList.toggle("current", match);
  }
}

if (kiosk) {
  $("dark").onclick = () => { $("dark").hidden = true; tellScreen("controls"); };
  followScreen();
  setInterval(followScreen, 1000);
} else {
  for (const b of document.querySelectorAll("#screen button")) {
    b.onclick = async () => {
      const body = { mode: b.dataset.mode };
      if (b.dataset.style) body.style = +b.dataset.style;
      try { markScreen(await api("/api/screen", body)); } catch {}
    };
  }
  const poll = () => api("/api/screen").then(markScreen).catch(() => {});
  poll();
  setInterval(poll, 5000);
}
canvas.onclick = showUi;
addEventListener("resize", () => { if (!$("viz").hidden) resize(); });
