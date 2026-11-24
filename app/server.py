"""Turntable → Sonos.

The control app: tells Sonos rooms to play the turntable, starts/stops rooms and
the Pi screen with the music, identifies songs, feeds the visualizers, and serves
the control page. The audio itself (capture and streams) is audio.py, a separate
service, so this one can restart without interrupting playback.
"""
import asyncio
import collections
import json
import os
import io
import socket
import time
import wave
from pathlib import Path
from urllib.parse import quote

import numpy as np
import aiohttp
import soco
from aiohttp import web
from shazamio import Shazam

import lastfm

ICECAST_PASSWORD = os.environ["ICECAST_SOURCE_PASSWORD"]
AUDIO = f"http://127.0.0.1:{os.environ.get('AUDIO_PORT', '8001')}"  # audio.py
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
# Detection uses the level with the low end removed: a needle left in the run-out groove
# makes rumble around -45 dB but almost nothing above it, while music is full of treble.
# Measured: run-out groove -73 dB. Music: not yet measured.
TREBLE_DB = -55
SOUND_SECONDS = 2  # sound must last this long before rooms auto-start
GAP_SECONDS = 1.0  # a quiet stretch this long between loud parts counts as a new track
IDENTIFY_AFTER = 10  # seconds into a track before asking Shazam
CLIP_SECONDS = 12
RECHECK_SECONDS = 180  # re-identify if no gap was heard (gapless albums)
# Music is loud most of the time; the clicks of a needle left in the run-out groove are not.
ACTIVE_WINDOW = 3  # seconds
ACTIVE_FRACTION = 0.5


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.1.1", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def stream_url():
    if settings["flac"]:
        return f"http://{lan_ip()}:{AUDIO.rsplit(':', 1)[1]}{FLAC_PATH}"
    return f"http://{lan_ip()}:8000/{MOUNT}"


def sonos_uri():
    url = stream_url()
    return url if settings["flac"] else "x-rincon-mp3radio://" + url.removeprefix("http://")


# ---------- settings ----------

DEFAULT_SETTINGS = {
    "auto_play": True,
    "auto_rooms": ["Living Room"],
    "screen_sleep": True,
    "song_overlay": True,
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
        self.treble_db = -120.0
        self.avg_treble = 0.0
        self.running = False
        self.last_sound = 0.0
        self.loud_frames = collections.deque(maxlen=ACTIVE_WINDOW * FPS)
        self.recent = collections.deque(maxlen=CLIP_SECONDS * FPS)
        self.gaps = 0  # quiet gaps seen between tracks
        self.quiet_run = 0
        self.loud_run = 0
        self.window = np.hanning(FFT_SIZE)
        self.ring = np.zeros(FFT_SIZE, dtype=np.float32)
        freqs = np.fft.rfftfreq(FFT_SIZE, 1 / VIS_RATE)
        edges = np.geomspace(40, VIS_RATE / 2, BANDS + 1)
        self.band_bins = [
            np.where((freqs >= lo) & (freqs < hi))[0] for lo, hi in zip(edges[:-1], edges[1:])
        ]
        self.band_bins = [b if len(b) else np.array([np.searchsorted(freqs, lo)])
                          for b, lo in zip(self.band_bins, edges[:-1])]

    async def run(self):
        """Read the turntable audio from the audio service, reconnecting if it restarts."""
        dropped = None  # rooms that were playing when the audio service went away
        while True:
            try:
                async with aiohttp.ClientSession() as http:
                    async with http.get(f"{AUDIO}/pcm", timeout=aiohttp.ClientTimeout(sock_read=10)) as resp:
                        self.running = True
                        if dropped:
                            asyncio.create_task(reconnect_rooms(dropped))
                        elif settings["flac"]:  # the audio service may have restarted without it
                            asyncio.create_task(self.start_flac())
                        dropped = None
                        while True:
                            chunk = await resp.content.readexactly(FRAME * 4)
                            self.recent.append(chunk)
                            stereo = np.frombuffer(chunk, dtype=np.int16).astype(np.float32).reshape(-1, 2) / 32768
                            self.analyse(stereo.mean(axis=1))
            except (aiohttp.ClientError, asyncio.IncompleteReadError, asyncio.TimeoutError) as e:
                print(f"audio service: {e.__class__.__name__}", flush=True)
            if self.running and dropped is None:
                # Sonos stops the moment its stream drops, so use the last known list.
                dropped = list(playing_rooms)
            self.running = False
            self.level_db = -120.0
            self.avg_power = 0.0
            await asyncio.sleep(2)

    def clip_wav(self):
        """The last few seconds of audio as a mono 16 kHz WAV, for recognition."""
        pcm = np.frombuffer(b"".join(self.recent), dtype=np.int16).reshape(-1, 2).mean(axis=1)
        pcm = pcm[: len(pcm) // 11 * 11].reshape(-1, 11).mean(axis=1)  # 44.1k -> ~16k
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE // 11)
            w.writeframes(pcm.astype(np.int16).tobytes())
        return buf.getvalue()

    async def start_flac(self, timeout=10):
        return (await audio_service("POST", "/flac", {"on": True})).get("flac", False)

    def stop_flac(self):
        asyncio.create_task(audio_service("POST", "/flac", {"on": False}))

    def analyse(self, samples):
        power = float(np.mean(samples ** 2))
        self.level_db = float(10 * np.log10(power or 1e-12))
        self.avg_power += (power - self.avg_power) * 0.03  # ~1 second average
        treble = float(np.mean(np.diff(samples) ** 2))
        self.treble_db = float(10 * np.log10(treble or 1e-12))
        self.avg_treble += (treble - self.avg_treble) * 0.03
        loud = self.treble_db > TREBLE_DB
        self.loud_frames.append(loud)
        # Track gaps: a pop or crackle is too short to count as sound (needs ~100 ms).
        if loud:
            self.loud_run += 1
            if self.loud_run >= 3:
                if self.quiet_run >= GAP_SECONDS * FPS:
                    self.gaps += 1
                self.quiet_run = 0
        else:
            self.loud_run = 0
            self.quiet_run += 1
        if self.clients:
            # Average groups of 4 samples: 44.1 kHz down to the visualizer's 11 kHz.
            self.analyse_vis(samples[: len(samples) // 4 * 4].reshape(-1, 4).mean(axis=1))

    @property
    def active_fraction(self):
        return sum(self.loud_frames) / self.loud_frames.maxlen

    @property
    def playing(self):
        """Music, as opposed to silence or a needle clicking in the run-out groove."""
        return self.active_fraction >= ACTIVE_FRACTION

    @property
    def avg_db(self):
        return float(10 * np.log10(self.avg_power or 1e-12))

    def analyse_vis(self, samples):
        self.ring = np.concatenate([self.ring[len(samples):], samples])
        mag = np.abs(np.fft.rfft(self.ring * self.window)) / (FFT_SIZE / 4)
        db = 20 * np.log10(np.array([mag[b].max() for b in self.band_bins]) + 1e-9)
        bands = np.clip((db + 70) / 70, 0, 1)
        recent = self.ring[-FFT_SIZE // 2:]
        wave = recent[np.linspace(0, len(recent) - 1, WAVE_POINTS).astype(int)]  # the page scales it
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


async def audio_service(method, path, body=None):
    try:
        async with aiohttp.ClientSession() as http:
            async with http.request(method, AUDIO + path, json=body,
                                    timeout=aiohttp.ClientTimeout(total=15)) as resp:
                return await resp.json()
    except Exception as e:
        print(f"audio service {path}: {e.__class__.__name__}", flush=True)
        return {}


# ---------- Sonos ----------

def is_turntable(uri):
    return MOUNT in (uri or "") or FLAC_PATH in (uri or "")


def room_state(zone):
    """None if the speaker can't be reached (e.g. a Roam that's switched off or away)."""
    try:
        coord = zone.group.coordinator
        info = coord.get_current_transport_info()["current_transport_state"]
        uri = coord.get_current_track_info().get("uri", "")
        return {
            "name": zone.player_name,
            "on": info in ("PLAYING", "TRANSITIONING") and is_turntable(uri),
            "volume": zone.volume,
        }
    except Exception as e:
        print(f"sonos {zone.ip_address} unreachable: {e.__class__.__name__}", flush=True)
        return None


def is_on(zone):
    return bool((room_state(zone) or {}).get("on"))


def discover():
    """Reachable Sonos rooms, by name. Speakers that are off or away are left out."""
    named = []
    for z in soco.discover(timeout=3) or set():
        try:
            named.append((z.player_name, z))
        except Exception:
            print(f"sonos {z.ip_address} unreachable", flush=True)
    return [z for _, z in sorted(named, key=lambda nz: nz[0])]


def find(name):
    for z in discover():
        if z.player_name == name:
            return z
    raise web.HTTPNotFound(text=f"No Sonos room called {name}")


def list_rooms():
    return [st for st in map(room_state, discover()) if st]


def turn_on(name):
    zone = find(name)
    for other in discover():
        if other is zone or other.player_name == name:
            continue
        coord = other.group.coordinator
        if coord.player_name == other.player_name and is_on(other):
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
        if is_on(z):
            z.group.coordinator.stop()


def playing_coordinators():
    return [z for z in discover() if is_on(z) and z.group.coordinator.player_name == z.player_name]


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


# ---------- what's playing (Shazam) ----------

track = None  # {"title", "artist", "album", "art"} of the song on the turntable
# Where identifying the current song stands: None (no music), "searching", "found" or "unknown".
id_state = None
shazam = None


async def identify():
    global track, shazam
    shazam = shazam or Shazam()
    try:
        result = await shazam.recognize(audio.clip_wav())
    except Exception as e:
        print(f"shazam: {e}", flush=True)
        return False
    found = result.get("track")
    if not found:
        return False
    album = next((m.get("text") for sec in found.get("sections", []) for m in sec.get("metadata", [])
                  if m.get("title") == "Album"), None)
    new = {"title": found.get("title"), "artist": found.get("subtitle"), "album": album,
           "art": found.get("images", {}).get("coverart")}
    if new != track:
        track = new
        print(f"now playing: {new['artist']} - {new['title']}", flush=True)
        await tell_icecast(f"{new['artist']} - {new['title']}")
        await lastfm.song_changed(new, started_ago=IDENTIFY_AFTER)
    return True


async def tell_icecast(song):
    """Show the song in Sonos's now playing (MP3 stream only)."""
    url = f"http://localhost:8000/admin/metadata?mount=/{MOUNT}&mode=updinfo&song={quote(song)}"
    try:
        async with aiohttp.ClientSession() as session:
            await session.get(url, auth=aiohttp.BasicAuth("source", ICECAST_PASSWORD))
    except Exception as e:
        print(f"icecast metadata: {e}", flush=True)


async def follow_tracks():
    """Identify the song a few seconds into each track; vinyl's gaps between tracks mark the changes."""
    global track, id_state
    gaps = audio.gaps
    due = None  # when to next ask Shazam
    tries = 0
    last_id = 0
    recheck = False  # a routine re-check of a song already identified
    while True:
        await asyncio.sleep(1)
        now = time.monotonic()
        playing = audio.playing
        if not playing:
            if now - audio.last_sound > settings["idle_minutes"] * 60:
                track = id_state = None
            continue
        new_track = audio.gaps != gaps or id_state is None
        if new_track or due is None and now - last_id > RECHECK_SECONDS:
            gaps = audio.gaps
            due, tries, recheck = now + IDENTIFY_AFTER, 0, not new_track
            if new_track:
                id_state = "searching"
        if due and now >= due:
            last_id = now
            tries += 1
            matched = await identify()
            if matched:
                id_state, due = "found", None
            elif tries < 2:
                due = now + 20  # one retry later in the track
            else:
                due = None
                if not recheck:  # a new song Shazam doesn't know: don't keep showing the last one
                    track, id_state = None, "unknown"


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
        if audio.playing:
            quiet_since = audio.last_sound = now
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
            await lastfm.end_session()
            if settings["auto_play"]:
                try:
                    await blocking(stop_all)
                except Exception as e:
                    print(f"auto-stop: {e}", flush=True)

        # Whoever picked themselves stops scrobbling after a quiet spell, even if no music came.
        if (lastfm.listeners and not playing
                and now - quiet_since >= idle and time.monotonic() - lastfm.chosen_at >= idle):
            await lastfm.end_session()

        if (settings["screen_sleep"] and screen["mode"] != "off"
                and now - quiet_since >= idle and now - screen_changed >= idle):
            slept_from = screen["mode"]
            set_screen("off")


async def stream_ready():
    """Make sure the lossless stream has started before pointing Sonos at it."""
    if settings["flac"] and not await audio.start_flac(timeout=30):
        raise web.HTTPServiceUnavailable(text="The lossless stream isn't ready yet.")


playing_rooms = []  # rooms playing the turntable, refreshed every few seconds


async def watch_rooms():
    """Keep playing_rooms current, so the rooms can be put back if the audio service restarts."""
    while True:
        if audio.running:
            try:
                playing_rooms[:] = [z.player_name for z in await blocking(playing_coordinators)]
            except Exception as e:
                print(f"watching rooms: {e}", flush=True)
        await asyncio.sleep(15)


async def reconnect_rooms(names):
    """Safety net: after the audio service restarts, put back the rooms that were playing."""
    await asyncio.sleep(3)  # let the streams come back up
    try:
        await stream_ready()
    except web.HTTPException:
        print("reconnect: lossless stream not ready", flush=True)
        return
    for name in names:
        try:
            await blocking(turn_on_if_off, name)
            print(f"reconnected {name}", flush=True)
        except Exception as e:
            print(f"reconnect {name}: {e}", flush=True)


def turn_on_if_off(name):
    zone = find(name)
    if not is_on(zone):
        turn_on(name)


# ---------- HTTP ----------

routes = web.RouteTableDef()


@routes.get("/")
async def index(request):
    # Always fetch the page fresh, so phones pick up updates instead of a cached old version.
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@routes.get("/api/status")
async def status(request):
    return web.json_response({
        "capturing": audio.running,
        "signal": audio.playing,
        "active": round(audio.active_fraction, 2),
        "treble_db": round(float(10 * np.log10(audio.avg_treble or 1e-12)), 1),
        "level_db": round(audio.avg_db, 1),
        **{k: v for k, v in (await audio_service("GET", "/health")).items() if k != "flac"},
        "track": track,
        "identify": id_state,
        "song_overlay": settings["song_overlay"],
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
    for key in ("auto_play", "screen_sleep", "song_overlay"):
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
        if not all(await blocking(lambda: [is_on(z) for z in playing])):
            settings["flac"] = False
            await blocking(repoint, playing)
            audio.stop_flac()
            return "Sonos wouldn't play the lossless stream, so it's back on MP3."
    if not on:
        audio.stop_flac()
    return None


@routes.get("/api/lastfm")
async def lastfm_status(request):
    return web.json_response(lastfm.status())


@routes.post("/api/lastfm/connect")
async def lastfm_connect(request):
    if not lastfm.configured():
        raise web.HTTPServiceUnavailable(text="Last.fm keys aren't set up on the Pi.")
    token, url = await lastfm.start_auth()
    return web.json_response({"token": token, "url": url})


@routes.post("/api/lastfm/finish")
async def lastfm_finish(request):
    name = await lastfm.finish_auth((await request.json())["token"])
    return web.json_response({**lastfm.status(), "linked": name})


@routes.post("/api/lastfm/listeners")
async def lastfm_listeners(request):
    await lastfm.set_listeners((await request.json())["names"])
    return web.json_response(lastfm.status())


@routes.post("/api/lastfm/unlink")
async def lastfm_unlink(request):
    lastfm.unlink((await request.json())["name"])
    return web.json_response(lastfm.status())


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
    app["tasks"] = [asyncio.create_task(t) for t in (audio.run(), audio.broadcast(), follow_music(), follow_tracks(), watch_rooms())]


def main():
    app = web.Application()
    app.add_routes(routes)
    app.router.add_static("/static", STATIC)
    app.on_startup.append(start_background)
    # Short shutdown: open Sonos streams would otherwise hold a restart for a minute.
    web.run_app(app, port=PORT, print=None, shutdown_timeout=3)


if __name__ == "__main__":
    main()
