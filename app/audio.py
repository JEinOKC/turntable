"""Turntable audio: capture and streams.

Kept apart from the control app (server.py) so the control app can be updated and
restarted without interrupting what Sonos is playing. Change this file rarely.

- Captures the turntable's USB audio with ffmpeg.
- Streams 320 kbps MP3 to Icecast (:8000/turntable.mp3).
- Serves lossless FLAC at :8001/turntable.flac while switched on. Each listener gets
  its own encoder started when it connects: Sonos goes silent on a shared FLAC stream
  that was already running long before it joined.
- Hands the raw audio to the control app at :8001/pcm (local only), for levels,
  song recognition and the visualizers.
"""
import asyncio
import os
import time

from aiohttp import web

ICECAST_PASSWORD = os.environ["ICECAST_SOURCE_PASSWORD"]
ALSA_DEVICE = os.environ.get("ALSA_DEVICE", "hw:CODEC")
GAIN_DB = float(os.environ.get("GAIN_DB", "0"))
PORT = int(os.environ.get("AUDIO_PORT", "8001"))
MOUNT = "turntable.mp3"
FLAC_PATH = "/turntable.flac"
RATE = 44100
CHUNK = RATE // 30 * 4  # 1/30 s of 16-bit stereo


class Capture:
    def __init__(self):
        self.running = False
        self.xruns = 0
        self.captured = 0  # stereo samples read since capture started
        self.started = None
        self.pcm_listeners = set()
        self.flac_on = False
        self.flac_listeners = set()  # each a queue of raw audio for that listener's encoder

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
            proc = await asyncio.create_subprocess_exec(
                *self.command(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            self.running = True
            self.captured, self.started = 0, time.monotonic()
            log_task = asyncio.create_task(self.read_log(proc))
            try:
                while True:
                    chunk = await proc.stdout.readexactly(CHUNK)
                    self.captured += CHUNK // 4
                    send(self.pcm_listeners, chunk)
                    send(self.flac_listeners, chunk)
            except asyncio.IncompleteReadError:
                pass
            self.running = False
            await proc.wait()
            await log_task
            print(f"ffmpeg exited ({proc.returncode})", flush=True)
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

    def health(self):
        elapsed = time.monotonic() - self.started if self.started else 0
        return {
            "capturing": self.running,
            "xruns": self.xruns,
            "capture_seconds": round(elapsed, 1),
            "missing_seconds": round(elapsed - self.captured / RATE, 2),
            "flac": self.flac_on,
        }

    def set_flac(self, on):
        self.flac_on = on
        if not on:
            for q in list(self.flac_listeners):
                close(self.flac_listeners, q)


def send(listeners, chunk):
    """Queue a chunk for each listener; drop listeners that have fallen too far behind."""
    for q in list(listeners):
        if q.full():
            close(listeners, q)
        else:
            q.put_nowait(chunk)


def close(listeners, q):
    """End a listener's stream (making room for the end marker if its queue is full)."""
    listeners.discard(q)
    if q.full():
        q.get_nowait()
    q.put_nowait(None)


capture = Capture()
routes = web.RouteTableDef()


async def stream(request, listeners, content_type):
    resp = web.StreamResponse(headers={"Content-Type": content_type, "Cache-Control": "no-cache"})
    await resp.prepare(request)
    q = asyncio.Queue(maxsize=200)
    listeners.add(q)
    try:
        while (chunk := await q.get()) is not None:
            await resp.write(chunk)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        listeners.discard(q)
    return resp


@routes.get(FLAC_PATH)
async def flac(request):
    """A fresh FLAC encoder for this listener, fed live from the capture."""
    if not capture.flac_on:
        raise web.HTTPServiceUnavailable(text="Lossless stream is off")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "s16le", "-ar", str(RATE), "-ac", "2", "-i", "pipe:0",
        "-c:a", "flac", "-compression_level", "0", "-flush_packets", "1", "-f", "flac", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    q = asyncio.Queue(maxsize=200)
    capture.flac_listeners.add(q)

    async def feed():
        try:
            while (chunk := await q.get()) is not None:
                proc.stdin.write(chunk)
                await proc.stdin.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            proc.stdin.close()

    feeder = asyncio.create_task(feed())
    resp = web.StreamResponse(headers={"Content-Type": "audio/flac", "Cache-Control": "no-cache",
                                       "icy-name": "Turntable"})
    try:
        await resp.prepare(request)
        while chunk := await proc.stdout.read(65536):
            await resp.write(chunk)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        capture.flac_listeners.discard(q)
        feeder.cancel()
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
    return resp


@routes.get("/pcm")
async def pcm(request):
    """Raw 44.1 kHz 16-bit stereo for the control app, in whole 1/30 s chunks."""
    if request.remote not in ("127.0.0.1", "::1"):
        raise web.HTTPForbidden()
    return await stream(request, capture.pcm_listeners, "application/octet-stream")


@routes.get("/health")
async def health(request):
    return web.json_response(capture.health())


@routes.post("/flac")
async def set_flac(request):
    capture.set_flac(bool((await request.json()).get("on")))
    return web.json_response(capture.health())


async def start(app):
    app["capture"] = asyncio.create_task(capture.run())


def main():
    app = web.Application()
    app.add_routes(routes)
    app.on_startup.append(start)
    web.run_app(app, port=PORT, print=None, shutdown_timeout=3)


if __name__ == "__main__":
    main()
