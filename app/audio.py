"""Turntable audio: capture and streams.

Kept apart from the control app (server.py) so the control app can be updated and
restarted without interrupting what Sonos is playing. Change this file rarely.

- Captures the turntable's USB audio with ffmpeg.
- Streams 320 kbps MP3 to Icecast (:8000/turntable.mp3).
- Serves lossless FLAC at :8001/turntable.flac while switched on.
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
        self.flac_proc = None
        self.flac_header = None
        self.flac_listeners = set()

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
                    self.feed_flac(chunk)
                    send(self.pcm_listeners, chunk)
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
            "flac": self.flac_header is not None,
        }

    # Lossless stream: a separate encoder fed from the same capture, so it can
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
                send(self.flac_listeners, chunk)
        except asyncio.IncompleteReadError:
            pass
        finally:
            self.flac_header = None
            for q in list(self.flac_listeners):
                close(self.flac_listeners, q)
            await proc.wait()


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


async def stream(request, listeners, content_type, first=b"", sync=None):
    resp = web.StreamResponse(headers={"Content-Type": content_type, "Cache-Control": "no-cache",
                                       "icy-name": "Turntable"})
    await resp.prepare(request)
    if first:
        await resp.write(first)
    q = asyncio.Queue(maxsize=200)
    listeners.add(q)
    try:
        while (chunk := await q.get()) is not None:
            if sync:
                i = chunk.find(sync)
                if i < 0:
                    continue
                chunk, sync = chunk[i:], None
            await resp.write(chunk)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        listeners.discard(q)
    return resp


@routes.get(FLAC_PATH)
async def flac(request):
    if capture.flac_header is None:
        raise web.HTTPServiceUnavailable(text="Lossless stream is off")
    # Join at the next frame boundary (FLAC frame sync code 0xFFF8).
    return await stream(request, capture.flac_listeners, "audio/flac", capture.flac_header, b"\xff\xf8")


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
    if (await request.json()).get("on"):
        await capture.start_flac()
    else:
        capture.stop_flac()
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
