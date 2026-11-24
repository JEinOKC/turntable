"""Last.fm scrobbling for songs identified on the turntable.

Uses Last.fm's desktop auth flow: the control page sends you to Last.fm to allow
access once, and the session key it returns is kept on the Pi.
"""
import hashlib
import json
import os
import time
from pathlib import Path

import aiohttp

API = "https://ws.audioscrobbler.com/2.0/"
AUTH_PAGE = "https://www.last.fm/api/auth/"
API_KEY = os.environ.get("LASTFM_API_KEY", "")
API_SECRET = os.environ.get("LASTFM_API_SECRET", "")
SESSION_FILE = Path.home() / ".config" / "turntable" / "lastfm.json"
MIN_PLAY_SECONDS = 30  # Last.fm ignores plays shorter than this

session = {}  # {"key", "name"} once linked
pending_token = None
playing = None  # {"track": {...}, "started": unix time} of the song to scrobble when it ends


def configured():
    return bool(API_KEY and API_SECRET)


def load():
    global session
    try:
        session = json.loads(SESSION_FILE.read_text())
    except (OSError, ValueError):
        session = {}


def status():
    return {"configured": configured(), "user": session.get("name")}


async def call(method, http_method="POST", **params):
    params = {k: str(v) for k, v in params.items() if v is not None}
    params.update(method=method, api_key=API_KEY)
    params["api_sig"] = hashlib.md5(
        ("".join(k + params[k] for k in sorted(params)) + API_SECRET).encode()).hexdigest()
    params["format"] = "json"
    async with aiohttp.ClientSession() as http:
        req = http.post(API, data=params) if http_method == "POST" else http.get(API, params=params)
        async with req as resp:
            data = await resp.json(content_type=None)
    if "error" in data:
        raise RuntimeError(f"Last.fm {method}: {data.get('message')}")
    return data


async def start_auth():
    """Returns the Last.fm page where you allow access."""
    global pending_token
    pending_token = (await call("auth.getToken", "GET"))["token"]
    return f"{AUTH_PAGE}?api_key={API_KEY}&token={pending_token}"


async def finish_auth():
    """Call after allowing access on Last.fm. True once linked."""
    global session, pending_token
    if not pending_token:
        return bool(session)
    try:
        s = (await call("auth.getSession", "GET", token=pending_token))["session"]
    except RuntimeError:
        return False  # not allowed yet
    session = {"key": s["key"], "name": s["name"]}
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps(session))
    SESSION_FILE.chmod(0o600)
    pending_token = None
    return True


def unlink():
    global session
    session = {}
    SESSION_FILE.unlink(missing_ok=True)


async def song_changed(track, started_ago=0):
    """Scrobble the song that just ended (if it played long enough) and announce the new one."""
    global playing
    if not session:
        return
    try:
        await flush()
        if track:
            playing = {"track": track, "started": time.time() - started_ago}
            await call("track.updateNowPlaying", artist=track["artist"], track=track["title"],
                       album=track.get("album"), sk=session["key"])
    except Exception as e:
        print(f"lastfm: {e}", flush=True)


async def flush():
    """Scrobble the current song if it played long enough."""
    global playing
    done, playing = playing, None
    if done and time.time() - done["started"] >= MIN_PLAY_SECONDS:
        t = done["track"]
        await call("track.scrobble", artist=t["artist"], track=t["title"], album=t.get("album"),
                   timestamp=int(done["started"]), sk=session["key"])
        print(f"scrobbled: {t['artist']} - {t['title']}", flush=True)


load()
