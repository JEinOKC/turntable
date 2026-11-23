"""Turntable → Sonos.

Captures the turntable's USB audio, streams it (MP3 through Icecast, or
lossless FLAC served here), tells Sonos rooms to play it, feeds a visualizer
over a WebSocket, and starts/stops rooms and the Pi screen with the music.
"""
import asyncio
import collections
import json
import os
import socket
import time
from pathlib import Path

import numpy as np
import soco
from aiohttp import web

ICECAST_PASSWORD = os.environ["ICECAST_SOURCE_PASSWORD"]
ALSA_DEVICE = os.environ.get("ALSA_DEVICE", "hw:CODEC")
GAIN_DB = float(os.environ.get("GAIN_DB", "0"))
# Sonos plays a few seconds behind the live input; visuals wait to match.
VISUAL_DELAY = float(os.environ.get("VISUAL_DELAY", "2.5"))
PORT = int(os.environ.get("PORT", "80"))
MOUNT = "turntable.mp3"
FLAC_PATH = "/turntable.flac"
STATIC = Path(__file__).parent / "static"
SETTINGS_FILE = Path.home() / ".config" / "turntable" / "settings.json"
BACKLIGHT = Path("/sys/class/backlight/10-0045/brightness")

RATE = 44100
VIS_RATE = RATE // 4
FPS = 30
FRAME = RATE // FPS  # stereo samples per analysis frame
FFT_SIZE = 1024
BANDS = 48
WAVE_POINTS = 96
# Above this the turntable counts as playing. Needs checking against a real record.
SIGNAL_DB = -45
SOUND_SECONDS = 2  # sound must last this long before rooms auto-start


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.1.1", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def stream_url():
    if settings["flac"]:
        return f"http://{lan_ip()}{'' if PORT == 80 else f':{PORT}'}{FLAC_PATH}"
    return f"http://{lan_ip()}:8000/{MOUNT}"


def sonos_uri():
    url = stream_url()
    return url if settings["flac"] else "x-rincon-mp3radio://" + url.removeprefix("http://")


# ---------- settings ----------

DEFAULT_SETTINGS = {
    "auto_play": True,
    "auto_rooms": ["Living Room"],
    "screen_sleep": True,
    "idle_minutes": 5,
    "flac": False,
}


def load_settings():
    try:
        return {**DEFAULT_SETTINGS, **json.loads(SETTINGS_FILE.read_text())}
    except (OSError, ValueError):
        return dict(DEFAULT_SETTINGS)


def save_settings():
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2))


settings = load_settings()


# ---------- audio capture + analysis ----------

class Audio:
    def __init__(self):
        self.clients = set()
        self.frames = collections.deque()
        self.level_db = -120.0
        self.avg_power = 0.0
        self.running = False
        self.proc = None
        self.flac_proc = None
        self.flac_header = None
        self.flac_listeners = set()
        self.xruns = 0
        self.captured = 0  # stereo samples read since capture started
        self.capture_started = None
        self.window = np.hanning(FFT_SIZE)
        self.ring = np.zeros(FFT_SIZE, dtype=np.float32)
        freqs = np.fft.rfftfreq(FFT_SIZE, 1 / VIS_RATE)
        edges = np.geomspace(40, VIS_RATE / 2, BANDS + 1)
        self.band_bins = [
            np.where((freqs >= lo) & (freqs < hi))[0] for lo, hi in zip(edges[:-1], edges[1:])
        ]
        self.band_bins = [b if len(b) else np.array([np.searchsorted(freqs, lo)])
                          for b, lo in zip(self.band_bins, edges[:-1])]

    def command(self):
        icecast = f"icecast://source:{ICECAST_PASSWORD}@localhost:8000/{MOUNT}"
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-f", "alsa", "-ac", "2", "-ar", str(RATE), "-i", ALSA_DEVICE,
            "-filter_complex", f"[0:a]volume={GAIN_DB}dB,asplit=2[mp3][pcm]",
            "-map", "[mp3]", "-c:a", "libmp3lame", "-b:a", "320k",
            "-content_type", "audio/mpeg", "-ice_name", "Turntable", "-f", "mp3", icecast,
            "-map", "[pcm]", "-f", "s16le", "pipe:1",
        ]

    async def run(self):
        while True:
            proc = self.proc = await asyncio.create_subprocess_exec(
                *self.command(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            self.running = True
            self.captured, self.capture_started = 0, time.monotonic()
            log_task = asyncio.create_task(self.read_log(proc))
            try:
                while True:
                    chunk = await proc.stdout.readexactly(FRAME * 4)
                    self.captured += FRAME
                    self.feed_flac(chunk)
                    stereo = np.frombuffer(chunk, dtype=np.int16).astype(np.float32).reshape(-1, 2) / 32768
                    self.analyse(stereo.mean(axis=1))
            except asyncio.IncompleteReadError:
                pass
            self.running = False
            await proc.wait()
            await log_task
            print(f"ffmpeg exited ({proc.returncode})", flush=True)
            self.level_db = -120.0
            self.avg_power = 0.0
            await asyncio.sleep(3)

    async def read_log(self, proc):
        """Keep ffmpeg's stderr drained; count capture overruns (dropped audio)."""
        while line := await proc.stderr.readline():
            text = line.decode(errors="replace").strip()
            if "dts to muxer" in text:
                continue
            if "xrun" in text.lower():
                self.xruns += 1
            print(f"ffmpeg: {text}", flush=True)

    def capture_health(self):
        if not self.capture_started:
            return {}
        elapsed = time.monotonic() - self.capture_started
        return {"xruns": self.xruns, "capture_seconds": round(elapsed, 1),
                "missing_seconds": round(elapsed - self.captured / RATE, 2)}

    # The lossless stream is a separate encoder fed from the same capture, so it can
    # start and stop without interrupting the MP3 stream.

    async def start_flac(self, timeout=10):
        if not self.flac_proc:
            self.flac_proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "s16le", "-ar", str(RATE), "-ac", "2", "-i", "pipe:0",
                "-c:a", "flac", "-compression_level", "0", "-flush_packets", "1", "-f", "flac", "pipe:1",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
            asyncio.create_task(self.read_flac(self.flac_proc))
        deadline = time.monotonic() + timeout
        while self.flac_header is None and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        return self.flac_header is not None

    def stop_flac(self):
        if self.flac_proc:
            self.flac_proc.stdin.close()
            self.flac_proc = None

    def feed_flac(self, chunk):
        proc = self.flac_proc
        if proc and not proc.stdin.is_closing():
            if proc.stdin.transport.get_write_buffer_size() > RATE * 4 * 5:
                print("flac encoder fell behind; stopping it", flush=True)
                self.stop_flac()
            else:
                proc.stdin.write(chunk)

    async def read_flac(self, proc):
        """Split the encoder's output into its header (sent to each new listener) and audio frames."""
        reader = proc.stdout
        try:
            header = await reader.readexactly(4)  # "fLaC"
            last = False
            while not last:
                block = await reader.readexactly(4)
                last = bool(block[0] & 0x80)
                header += block + await reader.readexactly(int.from_bytes(block[1:], "big"))
            self.flac_header = header
            while chunk := await reader.read(65536):
                for q in list(self.flac_listeners):
                    if q.full():
                        self.flac_listeners.discard(q)
                        q.put_nowait(None)
                    else:
                        q.put_nowait(chunk)
        except asyncio.IncompleteReadError:
            pass
        finally:
            self.flac_header = None
            for q in list(self.flac_listeners):
                self.flac_listeners.discard(q)
                q.put_nowait(None)
            await proc.wait()

    def analyse(self, samples):
        power = float(np.mean(samples ** 2))
        self.level_db = float(10 * np.log10(power or 1e-12))
        self.avg_power += (power - self.avg_power) * 0.03  # ~1 second average
        if self.clients:
            # Average groups of 4 samples: 44.1 kHz down to the visualizer's 11 kHz.
            self.analyse_vis(samples[: len(samples) // 4 * 4].reshape(-1, 4).mean(axis=1))

    @property
    def avg_db(self):
        return float(10 * np.log10(self.avg_power or 1e-12))

    def analyse_vis(self, samples):
        self.ring = np.concatenate([self.ring[len(samples):], samples])
        mag = np.abs(np.fft.rfft(self.ring * self.window)) / (FFT_SIZE / 4)
        db = 20 * np.log10(np.array([mag[b].max() for b in self.band_bins]) + 1e-9)
        bands = np.clip((db + 70) / 70, 0, 1)
        recent = self.ring[-FFT_SIZE // 2:]
        wave = np.clip(recent[np.linspace(0, len(recent) - 1, WAVE_POINTS).astype(int)] * 4, -1, 1)
        level = min(max((self.level_db + 60) / 60, 0), 1)
        self.frames.append((time.monotonic(), json.dumps({
            "b": [round(x, 3) for x in bands.tolist()],
            "w": [round(x, 3) for x in wave.tolist()],
            "l": round(level, 3),
        })))

    async def broadcast(self):
        while True:
            await asyncio.sleep(1 / FPS)
            due = time.monotonic() - VISUAL_DELAY
            frame = None
            while self.frames and self.frames[0][0] <= due:
                frame = self.frames.popleft()[1]
            if frame is None:
                continue
            for ws in list(self.clients):
                try:
                    await ws.send_str(frame)
                except Exception:
                    self.clients.discard(ws)


audio = Audio()


# ---------- Sonos ----------

def is_turntable(uri):
    return MOUNT in (uri or "") or FLAC_PATH in (uri or "")


def room_state(zone):
    group = zone.group
    coord = group.coordinator
    try:
        info = coord.get_current_transport_info()["current_transport_state"]
        uri = coord.get_current_track_info().get("uri", "")
    except Exception:
        info, uri = "UNKNOWN", ""
    return {
        "name": zone.player_name,
        "on": info in ("PLAYING", "TRANSITIONING") and is_turntable(uri),
        "volume": zone.volume,
    }


def discover():
    zones = soco.discover(timeout=3) or set()
    return sorted(zones, key=lambda z: z.player_name)


def find(name):
    for z in discover():
        if z.player_name == name:
            return z
    raise web.HTTPNotFound(text=f"No Sonos room called {name}")


def list_rooms():
    return [room_state(z) for z in discover()]


def turn_on(name):
    zone = find(name)
    for other in discover():
        if other is zone or other.player_name == name:
            continue
        coord = other.group.coordinator
        if coord.player_name == other.player_name and room_state(other)["on"]:
            zone.join(coord)
            return
    if zone.group.coordinator.player_name != zone.player_name:
        zone.unjoin()
    zone.play_uri(sonos_uri(), title="Turntable", force_radio=not settings["flac"])


def turn_off(name):
    zone = find(name)
    if len(zone.group.members) > 1:
        zone.unjoin()
    else:
        zone.stop()


def stop_all():
    for z in discover():
        if room_state(z)["on"]:
            z.group.coordinator.stop()


def playing_coordinators():
    return [z for z in discover() if z.group.coordinator.player_name == z.player_name and room_state(z)["on"]]


def repoint(zones):
    for z in zones:
        z.play_uri(sonos_uri(), title="Turntable", force_radio=not settings["flac"])


def set_volume(name, volume):
    find(name).volume = max(0, min(100, int(volume)))


async def blocking(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


# ---------- Pi screen ----------

# What the Pi's own screen shows: the controls, a visualizer style, or nothing.
screen = {"mode": "controls", "style": 0}
screen_changed = time.monotonic()


def set_screen(mode=None, style=None):
    global screen_changed
    screen_changed = time.monotonic()
    if mode in ("controls", "viz", "off"):
        screen["mode"] = mode
    if isinstance(style, int) and style >= 0:
        screen["style"] = style
    try:
        BACKLIGHT.write_text("0" if screen["mode"] == "off" else "255")
    except OSError as e:
        print(f"backlight: {e}", flush=True)


# ---------- follow the music ----------

async def follow_music():
    """Start the auto rooms when a record starts; after a quiet spell stop them and sleep the screen."""
    playing = False
    sound_since = None
    quiet_since = time.monotonic()
    slept_from = None  # screen mode to restore when the music comes back
    while True:
        await asyncio.sleep(1)
        now = time.monotonic()
        idle = settings["idle_minutes"] * 60
        if audio.avg_db > SIGNAL_DB:
            quiet_since = now
            sound_since = sound_since or now
        else:
            sound_since = None

        if not playing and sound_since and now - sound_since >= SOUND_SECONDS:
            playing = True
            if slept_from and screen["mode"] == "off":
                set_screen(slept_from)
            slept_from = None
            if settings["auto_play"]:
                for name in settings["auto_rooms"]:
                    try:
                        await stream_ready()
                        await blocking(turn_on_if_off, name)
                    except Exception as e:
                        print(f"auto-play {name}: {e}", flush=True)
        elif playing and now - quiet_since >= idle:
            playing = False
            if settings["auto_play"]:
                try:
                    await blocking(stop_all)
                except Exception as e:
                    print(f"auto-stop: {e}", flush=True)

        if (settings["screen_sleep"] and screen["mode"] != "off"
                and now - quiet_since >= idle and now - screen_changed >= idle):
            slept_from = screen["mode"]
            set_screen("off")


async def stream_ready():
    """Make sure the lossless stream has started before pointing Sonos at it."""
    if settings["flac"] and not await audio.start_flac(timeout=30):
        raise web.HTTPServiceUnavailable(text="The lossless stream isn't ready yet.")


def turn_on_if_off(name):
    zone = find(name)
    if not room_state(zone)["on"]:
        turn_on(name)


# ---------- HTTP ----------

routes = web.RouteTableDef()


@routes.get("/")
async def index(request):
    return web.FileResponse(STATIC / "index.html")


@routes.get("/api/status")
async def status(request):
    return web.json_response({
        "capturing": audio.running,
        "signal": audio.avg_db > SIGNAL_DB,
        "level_db": round(audio.avg_db, 1),
        **audio.capture_health(),
        "stream": stream_url(),
    })


@routes.get("/api/rooms")
async def rooms(request):
    return web.json_response(await blocking(list_rooms))


@routes.post("/api/rooms/{name}")
async def room(request):
    body = await request.json()
    name = request.match_info["name"]
    if "volume" in body:
        await blocking(set_volume, name, body["volume"])
    if "on" in body:
        if body["on"]:
            await stream_ready()
        await blocking(turn_on if body["on"] else turn_off, name)
    return web.json_response(await blocking(list_rooms))


@routes.post("/api/stop")
async def stop(request):
    await blocking(stop_all)
    return web.json_response(await blocking(list_rooms))


@routes.get("/api/screen")
async def get_screen(request):
    return web.json_response(screen)


@routes.post("/api/screen")
async def post_screen(request):
    body = await request.json()
    set_screen(body.get("mode"), body.get("style"))
    return web.json_response(screen)


@routes.get("/api/settings")
async def get_settings(request):
    return web.json_response(settings)


@routes.post("/api/settings")
async def post_settings(request):
    body = await request.json()
    for key in ("auto_play", "screen_sleep"):
        if isinstance(body.get(key), bool):
            settings[key] = body[key]
    if isinstance(body.get("flac"), bool) and body["flac"] != settings["flac"]:
        error = await set_flac(body["flac"])
        if error:
            return web.json_response({**settings, "error": error})
    save_settings()
    return web.json_response(settings)


async def set_flac(on):
    """Switch formats; rooms already playing the turntable move over with a short rebuffer."""
    if on and not await audio.start_flac():
        audio.stop_flac()
        return "The lossless stream didn't start."
    settings["flac"] = on
    playing = await blocking(playing_coordinators)
    await blocking(repoint, playing)
    if on and playing:
        await asyncio.sleep(8)
        if not all(await blocking(lambda: [room_state(z)["on"] for z in playing])):
            settings["flac"] = False
            await blocking(repoint, playing)
            audio.stop_flac()
            return "Sonos wouldn't play the lossless stream, so it's back on MP3."
    if not on:
        audio.stop_flac()
    return None


@routes.get(FLAC_PATH)
async def flac_stream(request):
    if not settings["flac"] or audio.flac_header is None:
        raise web.HTTPServiceUnavailable(text="Lossless stream is off")
    resp = web.StreamResponse(headers={"Content-Type": "audio/flac", "Cache-Control": "no-cache",
                                       "icy-name": "Turntable"})
    await resp.prepare(request)
    await resp.write(audio.flac_header)
    q = asyncio.Queue(maxsize=200)
    audio.flac_listeners.add(q)
    synced = False
    try:
        while (chunk := await q.get()) is not None:
            if not synced:
                # Join at the next frame boundary (FLAC frame sync code 0xFFF8).
                i = chunk.find(b"\xff\xf8")
                if i < 0:
                    continue
                chunk, synced = chunk[i:], True
            await resp.write(chunk)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        audio.flac_listeners.discard(q)
    return resp


@routes.get("/ws")
async def ws(request):
    sock = web.WebSocketResponse(heartbeat=20)
    await sock.prepare(request)
    audio.clients.add(sock)
    try:
        async for _ in sock:
            pass
    finally:
        audio.clients.discard(sock)
    return sock


async def start_background(app):
    app["tasks"] = [asyncio.create_task(t) for t in (audio.run(), audio.broadcast(), follow_music())]
    if settings["flac"]:
        app["tasks"].append(asyncio.create_task(audio.start_flac(timeout=60)))


def main():
    app = web.Application()
    app.add_routes(routes)
    app.router.add_static("/static", STATIC)
    app.on_startup.append(start_background)
    # Short shutdown: open Sonos streams would otherwise hold a restart for a minute.
    web.run_app(app, port=PORT, print=None, shutdown_timeout=3)


if __name__ == "__main__":
    main()
