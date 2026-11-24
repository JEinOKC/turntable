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
    const message = { searching: ["Listening…", "Identifying the song"],
      unknown: ["Couldn't identify this song", "Shazam didn't recognise it"],
      offline: ["No song info", "Can't reach Shazam: the internet seems to be down"] }[s.identify];
    $("now").hidden = !s.track && !message;
    $("now").classList.toggle("status", !s.track && !!message);
    if (!s.track && message) [$("np-title").textContent, $("np-artist").textContent] = message;
    showNowPlaying(s.track, message);
    const song = $("viz-song");
    song.hidden = !(s.track && s.song_overlay) || styles[style] === nowPlaying;
    if (s.track) {
      song.querySelector(".song-title").textContent = s.track.title;
      song.querySelector(".song-artist").textContent = s.track.artist;
      const img = song.querySelector(".song-art");
      if (img.getAttribute("src") !== (s.track.art || "")) img.src = s.track.art || "";
      img.hidden = !s.track.art;
    }
    if (s.track) {
      $("np-title").textContent = s.track.title;
      $("np-artist").textContent = [s.track.artist, s.track.album].filter(Boolean).join(" · ");
      if ($("art").getAttribute("src") !== (s.track.art || "")) $("art").src = s.track.art || "";
    }
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
const styles = [record, bars, scope, fireplace, nowPlaying];
let style = +(localStorage.vizStyle || 0) % styles.length;
let socket, raf, hideTimer;
let bands = new Float32Array(48), wave = new Float32Array(96), level = 0, angle = 0;
const peaks = new Float32Array(48);

function openViz() {
  if (!kiosk) localStorage.vizOpen = "1";  // reopen it if the page reloads
  $("controls").hidden = true;
  $("viz").hidden = false;
  resize();
  connectViz();
  showUi();
  raf = requestAnimationFrame(draw);
}

// Phones drop the live feed now and then (sleep, switching apps, Wi-Fi blips); reconnect
// whenever it closes or goes quiet, or the visuals freeze.
let lastFrame = 0;
function connectViz() {
  socket?.close();
  const ws = socket = new WebSocket(`ws://${location.host}/ws`);
  lastFrame = performance.now();
  ws.onmessage = (m) => {
    lastFrame = performance.now();
    const f = JSON.parse(m.data);
    for (let i = 0; i < f.b.length; i++) bands[i] = Math.max(f.b[i], bands[i] * 0.85);
    wave = Float32Array.from(f.w);
    level = Math.max(f.l, level * 0.9);
  };
  ws.onclose = () => {
    if (socket === ws && !$("viz").hidden) setTimeout(() => socket === ws && connectViz(), 1000);
  };
}
setInterval(() => {
  // The server sends ~30 updates a second, so 5 quiet seconds means the feed has died.
  if (!$("viz").hidden && performance.now() - lastFrame > 5000) connectViz();
}, 2500);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && !$("viz").hidden) connectViz();
});

function closeViz() {
  if (!kiosk) delete localStorage.vizOpen;
  const ws = socket;
  socket = null;
  ws?.close();
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

// Size from the visualizer itself, not the window: on iPhones the two differ as Safari's toolbar
// shows and hides, which left old drawings uncleared along the bottom.
let viewW = 0, viewH = 0;

function resize() {
  viewW = $("viz").clientWidth;
  viewH = $("viz").clientHeight;
  const portrait = viewH > viewW;
  $("np-view").classList.toggle("portrait", portrait);
  $("np-view").style.setProperty("--np-scale", portrait ? Math.min(viewW / 480, viewH / 800)
    : Math.min(viewW / 800, viewH / 480));
  const dpr = devicePixelRatio || 1;
  canvas.width = viewW * dpr;
  canvas.height = viewH * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

// The Pi's desktop flips the upside-down screen in software, a full processor core at ~20
// frames a second, so the Pi's screen gets fewer frames. Phones draw at their own rate.
const FRAME_MS = kiosk ? 1000 / 12 : 0;
let lastDraw = 0;

function draw(now) {
  raf = requestAnimationFrame(draw);
  if (now - lastDraw < FRAME_MS) return;
  lastDraw = now;
  $("np-view").hidden = styles[style] !== nowPlaying;
  for (let i = 0; i < bands.length; i++) bands[i] *= 0.97;
  level *= 0.97;
  if ($("viz").clientWidth !== viewW || $("viz").clientHeight !== viewH) resize();
  const np = styles[style] === nowPlaying;
  $("canvas").hidden = np;  // Now Playing draws into its own small canvas
  if (!np && styles[style] !== scope) {  // the waveform fades its trails, so it keeps the last frame
    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.restore();
  }
  styles[style](viewW, viewH);
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

// Glowing oscilloscope line that leaves trails. Scales itself to the music so it neither
// flatlines in quiet passages nor clips in loud ones.
let wavePeak = 0.2;
function scope(w, h) {
  ctx.fillStyle = "rgba(0,0,0,.25)";
  ctx.fillRect(0, 0, w, h);
  let peak = 0;
  for (const v of wave) peak = Math.max(peak, Math.abs(v));
  wavePeak += (Math.max(peak, 0.05) - wavePeak) * 0.05;
  const hue = (performance.now() / 50) % 360, mid = h / 2, scale = (h * 0.35) / wavePeak;
  ctx.lineWidth = 3;
  ctx.lineJoin = "round";
  ctx.strokeStyle = `hsl(${hue} 100% 60%)`;
  ctx.shadowColor = ctx.strokeStyle;
  ctx.shadowBlur = 12;
  ctx.beginPath();
  const step = w / (wave.length - 1), y = (i) => mid + Math.max(-1.3, Math.min(1.3, wave[i] * scale / (h * 0.35))) * h * 0.35;
  ctx.moveTo(0, y(0));
  for (let i = 1; i < wave.length - 1; i++) {
    // Curve through the midpoints for a smooth line instead of sharp zigzags.
    ctx.quadraticCurveTo(i * step, y(i), (i + 0.5) * step, (y(i) + y(i + 1)) / 2);
  }
  ctx.lineTo(w, y(wave.length - 1));
  ctx.stroke();
  ctx.shadowBlur = 0;
}

// The identified song as a record sleeve with its record spinning out of it (layout in CSS);
// the canvas only adds a row of bars along the bottom.
const npRecord = document.querySelector(".np-record");
const npBars = $("np-bars"), npCtx = npBars.getContext("2d");
function nowPlaying(w) {
  // Spin the record here (33⅓ rpm) rather than with a CSS animation, so it keeps to the frame cap.
  npRecord.style.transform = `rotate(${((performance.now() / 1800) % 1) * 360}deg)`;
  const dpr = devicePixelRatio || 1, bh = 46;
  if (npBars.width !== Math.round(w * dpr)) {
    npBars.width = Math.round(w * dpr);
    npBars.height = bh * dpr;
    npCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  npCtx.clearRect(0, 0, w, bh);
  const n = bands.length, gap = 3, bw = (w - gap * (n + 1)) / n;
  npCtx.fillStyle = "rgba(232, 163, 61, .65)";
  for (let i = 0; i < n; i++) {
    const h = Math.max(2, bands[i] * bh);
    npCtx.fillRect(gap + i * (bw + gap), bh - h, bw, h);
  }
}

let shownTrack = "";
// Same layout whether or not the song was identified: placeholder cover and a status line until it is.
function showNowPlaying(track, message) {
  const key = JSON.stringify([track || null, message || null]);
  if (key === shownTrack) return;
  shownTrack = key;
  const view = $("np-view");
  for (const el of view.querySelectorAll(".np-art")) {
    el.style.backgroundImage = track?.art ? `url("${track.art}")` : "";
  }
  const [title, artist, album] = track ? [track.title, track.artist, track.album || ""]
    : message ? [...message, ""] : ["Waiting for a record", "", ""];
  view.querySelector(".np-title").textContent = title;
  view.querySelector(".np-artist").textContent = artist;
  view.querySelector(".np-album").textContent = album;
}

// Soft rolling flames, after github.com/peinador/fireplace: layered noise drifting upward,
// faded with height and coloured through a fire palette. Louder music makes taller, brighter flames.
const FIRE_W = 64, FIRE_H = 40;
const fireCanvas = Object.assign(document.createElement("canvas"), { width: FIRE_W, height: FIRE_H });
const fireCtx = fireCanvas.getContext("2d");
const fireImage = fireCtx.createImageData(FIRE_W, FIRE_H);
const FIRE_STOPS = [[0, 0, 0, 0], [0.25, 60, 4, 0], [0.45, 160, 20, 0], [0.65, 232, 90, 0],
  [0.82, 255, 160, 20], [1, 255, 236, 170]];
const firePalette = Array.from({ length: 256 }, (_, i) => {
  const t = i / 255;
  const k = FIRE_STOPS.findIndex(([at]) => at >= t);
  const [a0, ...c0] = FIRE_STOPS[Math.max(0, k - 1)], [a1, ...c1] = FIRE_STOPS[k];
  const f = a1 === a0 ? 0 : (t - a0) / (a1 - a0);
  return c0.map((v, j) => Math.round(v + (c1[j] - v) * f));
});

// 2D gradient noise on a repeating grid (Perlin's smoothstep interpolation).
const NOISE = 64;
const noiseAngles = Float32Array.from({ length: NOISE * NOISE }, () => Math.random() * Math.PI * 2);
function noise2(x, y) {
  const x0 = Math.floor(x), y0 = Math.floor(y), fx = x - x0, fy = y - y0;
  const dot = (ix, iy, dx, dy) => {
    const a = noiseAngles[((iy % NOISE + NOISE) % NOISE) * NOISE + ((ix % NOISE + NOISE) % NOISE)];
    return Math.cos(a) * dx + Math.sin(a) * dy;
  };
  const sx = fx * fx * fx * (fx * (fx * 6 - 15) + 10), sy = fy * fy * fy * (fy * (fy * 6 - 15) + 10);
  const n0 = dot(x0, y0, fx, fy) + sx * (dot(x0 + 1, y0, fx - 1, fy) - dot(x0, y0, fx, fy));
  const n1 = dot(x0, y0 + 1, fx, fy - 1) + sx * (dot(x0 + 1, y0 + 1, fx - 1, fy - 1) - dot(x0, y0 + 1, fx, fy - 1));
  return n0 + sy * (n1 - n0);
}

let fireHeight = 0.6;
function fireplace(w, h) {
  const t = performance.now() / 1000;
  const bass = (bands[0] + bands[1] + bands[2] + bands[3]) / 4;
  fireHeight += (0.55 + level * 0.35 + bass * 0.15 - fireHeight) * 0.08;  // ease toward the music
  const px = fireImage.data;
  for (let y = 0; y < FIRE_H; y++) {
    const up = 1 - y / (FIRE_H - 1);  // 0 at the bottom, 1 at the top
    const fade = Math.max(0, 1 - (up / fireHeight) ** 2);  // quadratic fall-off with height
    for (let x = 0; x < FIRE_W; x++) {
      const edge = Math.abs(x / (FIRE_W - 1) - 0.5) * 2;
      const nx = x / 7, ny = y / 14 + t * 1.6;  // stretched tall, drifting upward
      const n = noise2(nx, ny) + 0.5 * noise2(nx * 2, ny * 2) + 0.25 * noise2(nx * 4 + t, ny * 4);
      const bed = (1 - up) ** 3 * 0.35;  // a hot glowing bed along the bottom
      const v = Math.min(1, Math.max(0, ((n * 0.9 + 0.55) * fade + bed) * (1 - edge ** 3) * 1.25));
      const [r, g, b] = firePalette[(v * 255) | 0];
      const i = (y * FIRE_W + x) * 4;
      px[i] = r; px[i + 1] = g; px[i + 2] = b; px[i + 3] = 255;
    }
  }
  fireCtx.putImageData(fireImage, 0, 0);
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, w, h);
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(fireCanvas, 0, 0, w, h);
}

function setStyle(n) {
  style = n % styles.length;
  localStorage.vizStyle = style;
  ctx.clearRect(0, 0, viewW, viewH);
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
    const st = await api("/api/screen?kiosk");
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

// ---------- settings ----------

function renderSettings(st) {
  for (const t of document.querySelectorAll("#settings .toggle")) {
    t.setAttribute("aria-pressed", st[t.dataset.key]);
    t.classList.remove("busy");
  }
  $("auto-label").textContent = `Auto-play ${st.auto_rooms.join(" & ")} when a record starts`;
}

if (!kiosk) {
  for (const t of document.querySelectorAll("#settings .toggle")) {
    t.onclick = async () => {
      t.classList.add("busy");
      const on = t.getAttribute("aria-pressed") !== "true";
      try {
        const st = await api("/api/settings", { [t.dataset.key]: on });
        renderSettings(st);
        if (st.error) alert(st.error);
      } catch (e) { alert(e.message); }
    };
  }
  api("/api/settings").then(renderSettings).catch(() => {});
}

// ---------- Last.fm ----------

let lastfmState = null;

function renderLastfm(st) {
  lastfmState = st;
  $("lastfm").hidden = !st.configured;
  $("lastfm-accounts").textContent = st.accounts.length ? st.accounts.join(", ") : "None linked";
  $("listening").hidden = !st.configured || !st.accounts.length;
  $("listeners").replaceChildren(...[...st.accounts, null].map((name) => {
    const b = document.createElement("button");
    b.textContent = name ?? "Nobody";
    const on = name ? st.listeners.includes(name) : !st.listeners.length;
    b.setAttribute("aria-pressed", on);
    b.onclick = () => {
      const names = name === null ? []
        : on ? st.listeners.filter((n) => n !== name) : [...st.listeners, name];
      api("/api/lastfm/listeners", { names }).then(renderLastfm).catch((e) => alert(e.message));
    };
    return b;
  }));
}

if (!kiosk) {
  $("lastfm-connect").onclick = async () => {
    // Open the tab now (inside the tap) so the phone doesn't block it, then point it at Last.fm.
    const tab = window.open("", "_blank");
    let token;
    try {
      const auth = await api("/api/lastfm/connect", {});
      token = auth.token;
      tab.location = auth.url;
    } catch (e) {
      tab?.close();
      return alert(e.message);
    }
    // Wait for them to allow access on Last.fm.
    for (let i = 0; i < 60; i++) {
      await new Promise((r) => setTimeout(r, 3000));
      const st = await api("/api/lastfm/finish", { token }).catch(() => null);
      if (st?.linked) return renderLastfm(st);
    }
  };
  const pollLastfm = () => api("/api/lastfm").then(renderLastfm).catch(() => {});
  pollLastfm();
  setInterval(pollLastfm, 5000);
}

// Come back to the visualizer if it was open when the page last closed or reloaded.
if (!kiosk && localStorage.vizOpen) openViz();
