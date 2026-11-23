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

VIS_RATE = 11025
FPS = 30
FRAME = VIS_RATE // FPS
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
        self.flac_header = None
        self.flac_listeners = set()
        self.window = np.hanning(FFT_SIZE)
        self.ring = np.zeros(FFT_SIZE, dtype=np.float32)
        freqs = np.fft.rfftfreq(FFT_SIZE, 1 / VIS_RATE)
        edges = np.geomspace(40, VIS_RATE / 2, BANDS + 1)
        self.band_bins = [
            np.where((freqs >= lo) & (freqs < hi))[0] for lo, hi in zip(edges[:-1], edges[1:])
        ]
        self.band_bins = [b if len(b) else np.array([np.searchsorted(freqs, lo)])
                          for b, lo in zip(self.band_bins, edges[:-1])]

    def command(self, flac_fd=None):
        icecast = f"icecast://source:{ICECAST_PASSWORD}@localhost:8000/{MOUNT}"
        split = 3 if flac_fd is not None else 2
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "alsa", "-ac", "2", "-ar", "44100", "-i", ALSA_DEVICE,
            "-filter_complex",
            f"[0:a]volume={GAIN_DB}dB,asplit={split}[out][vis]{'[lossless]' if split == 3 else ''};"
            f"[vis]pan=mono|c0=0.5*c0+0.5*c1,aresample={VIS_RATE}[mono]",
            "-map", "[out]", "-c:a", "libmp3lame", "-b:a", "320k",
            "-content_type", "audio/mpeg", "-ice_name", "Turntable", "-f", "mp3", icecast,
            "-map", "[mono]", "-f", "s16le", "pipe:1",
        ]
        if flac_fd is not None:
            cmd += ["-map", "[lossless]", "-c:a", "flac", "-compression_level", "0",
                    "-flush_packets", "1", "-f", "flac", f"pipe:{flac_fd}"]
        return cmd

    def restart(self):
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()

    async def run(self):
        while True:
            flac_r = flac_w = None
            if settings["flac"]:
                flac_r, flac_w = os.pipe()
            proc = self.proc = await asyncio.create_subprocess_exec(
                *self.command(flac_w), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                pass_fds=(flac_w,) if flac_w is not None else ())
            flac_task = None
            if flac_w is not None:
                os.close(flac_w)
                flac_task = asyncio.create_task(self.read_flac(flac_r))
            self.running = True
            try:
                while True:
                    chunk = await proc.stdout.readexactly(FRAME * 2)
                    self.analyse(np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768)
            except asyncio.IncompleteReadError:
                pass
            self.running = False
            if flac_task:
                flac_task.cancel()
            self.flac_header = None
            err = (await proc.stderr.read()).decode(errors="replace").strip()
            await proc.wait()
            print(f"ffmpeg exited ({proc.returncode}): {err[-300:]}", flush=True)
            self.level_db = -120.0
            self.avg_power = 0.0
            await asyncio.sleep(3)

    async def read_flac(self, fd):
        """Split ffmpeg's FLAC output into its header (sent to each new listener) and audio frames."""
        reader = asyncio.StreamReader()
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(fd, "rb", 0))
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
        except (asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        finally:
            transport.close()
            for q in list(self.flac_listeners):
                self.flac_listeners.discard(q)
                q.put_nowait(None)

    def analyse(self, samples):
        power = float(np.mean(samples ** 2))
        self.level_db = float(10 * np.log10(power or 1e-12))
        self.avg_power += (power - self.avg_power) * 0.03  # ~1 second average
        if self.clients:
            self.analyse_vis(samples)

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
    flac_before = settings["flac"]
    for key in ("auto_play", "screen_sleep", "flac"):
        if isinstance(body.get(key), bool):
            settings[key] = body[key]
    save_settings()
    if settings["flac"] != flac_before:
        audio.restart()
    return web.json_response(settings)


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


def main():
    app = web.Application()
    app.add_routes(routes)
    app.router.add_static("/static", STATIC)
    app.on_startup.append(start_background)
    web.run_app(app, port=PORT, print=None)


if __name__ == "__main__":
    main()
