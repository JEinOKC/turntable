"""Turntable → Sonos.

Captures the turntable's USB audio, streams it through Icecast, tells Sonos
rooms to play that stream, and feeds a visualizer over a WebSocket.
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
STATIC = Path(__file__).parent / "static"

VIS_RATE = 11025
FPS = 30
FRAME = VIS_RATE // FPS
FFT_SIZE = 1024
BANDS = 48
WAVE_POINTS = 96
SILENCE_DB = -60


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.1.1", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def stream_url():
    return f"http://{lan_ip()}:8000/{MOUNT}"


# ---------- audio capture + analysis ----------

class Audio:
    def __init__(self):
        self.clients = set()
        self.frames = collections.deque()
        self.level_db = -120.0
        self.running = False
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
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "alsa", "-ac", "2", "-ar", "44100", "-i", ALSA_DEVICE,
            "-filter_complex",
            f"[0:a]volume={GAIN_DB}dB,asplit=2[out][vis];"
            f"[vis]pan=mono|c0=0.5*c0+0.5*c1,aresample={VIS_RATE}[mono]",
            "-map", "[out]", "-c:a", "libmp3lame", "-b:a", "320k",
            "-content_type", "audio/mpeg", "-ice_name", "Turntable", "-f", "mp3", icecast,
            "-map", "[mono]", "-f", "s16le", "pipe:1",
        ]

    async def run(self):
        while True:
            proc = await asyncio.create_subprocess_exec(
                *self.command(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            self.running = True
            try:
                while True:
                    chunk = await proc.stdout.readexactly(FRAME * 2)
                    self.analyse(np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768)
            except asyncio.IncompleteReadError:
                pass
            self.running = False
            err = (await proc.stderr.read()).decode(errors="replace").strip()
            await proc.wait()
            print(f"ffmpeg exited ({proc.returncode}): {err[-300:]}", flush=True)
            self.level_db = -120.0
            await asyncio.sleep(3)

    def analyse(self, samples):
        rms = float(np.sqrt(np.mean(samples ** 2))) or 1e-9
        self.level_db = float(20 * np.log10(rms))
        if not self.clients:
            return
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
    return MOUNT in (uri or "")


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
    zone.play_uri("x-rincon-mp3radio://" + stream_url().removeprefix("http://"),
                  title="Turntable", force_radio=True)


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


# ---------- HTTP ----------

routes = web.RouteTableDef()


@routes.get("/")
async def index(request):
    return web.FileResponse(STATIC / "index.html")


@routes.get("/api/status")
async def status(request):
    return web.json_response({
        "capturing": audio.running,
        "signal": audio.level_db > SILENCE_DB,
        "level_db": round(audio.level_db, 1),
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
    app["tasks"] = [asyncio.create_task(audio.run()), asyncio.create_task(audio.broadcast())]


def main():
    app = web.Application()
    app.add_routes(routes)
    app.router.add_static("/static", STATIC)
    app.on_startup.append(start_background)
    web.run_app(app, port=PORT, print=None)


if __name__ == "__main__":
    main()
