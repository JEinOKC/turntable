"""Last.fm scrobbling for songs identified on the turntable, for whoever is listening.

Each person links their Last.fm account once (desktop auth flow: the control page
sends them to Last.fm to allow access, and the session key is kept on the Pi).
Someone then picks who's listening on their phone. Nobody is picked by default,
so nothing is scrobbled unless someone chooses to be, and the choice expires when
the listening session ends. Picking late still scrobbles the session's earlier songs.
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
ACCOUNTS_FILE = Path.home() / ".config" / "turntable" / "lastfm.json"
MIN_PLAY_SECONDS = 30  # Last.fm ignores plays shorter than this

accounts = {}  # Last.fm username -> session key
listeners = set()  # usernames scrobbling right now
chosen_at = 0.0  # when listeners last changed
session = []  # songs this listening session: {"track", "started", "ended", "scrobbled": set()}


def configured():
    return bool(API_KEY and API_SECRET)


def load():
    global accounts
    try:
        accounts = json.loads(ACCOUNTS_FILE.read_text()).get("accounts", {})
    except (OSError, ValueError):
        accounts = {}


def save():
    ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACCOUNTS_FILE.write_text(json.dumps({"accounts": accounts}))
    ACCOUNTS_FILE.chmod(0o600)


def status():
    return {"configured": configured(), "accounts": sorted(accounts), "listeners": sorted(listeners)}


async def call(method, http_method="POST", **params):
    params = {k: str(v) for k, v in params.items() if v is not None}
    params.update(method=method, api_key=API_KEY)
    params["api_sig"] = hashlib.md5(
        ("".join(k + params[k] for k in sorted(params)) + API_SECRET).encode()).hexdigest()
    params["format"] = "json"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as http:
        req = http.post(API, data=params) if http_method == "POST" else http.get(API, params=params)
        async with req as resp:
            data = await resp.json(content_type=None)
    if "error" in data:
        raise RuntimeError(f"Last.fm {method}: {data.get('message')}")
    return data


# ---------- linking accounts ----------

async def start_auth():
    """A token and the Last.fm page where its owner allows access."""
    token = (await call("auth.getToken", "GET"))["token"]
    return token, f"{AUTH_PAGE}?api_key={API_KEY}&token={token}"


async def finish_auth(token):
    """The linked username once access was allowed, else None."""
    try:
        s = (await call("auth.getSession", "GET", token=token))["session"]
    except RuntimeError:
        return None  # not allowed yet
    accounts[s["name"]] = s["key"]
    save()
    return s["name"]


def unlink(name):
    accounts.pop(name, None)
    listeners.discard(name)
    save()


# ---------- who's listening ----------

async def set_listeners(names):
    global chosen_at
    names = {n for n in names if n in accounts}
    added = names - listeners
    listeners.clear()
    listeners.update(names)
    chosen_at = time.monotonic()
    for name in added:
        for entry in session:
            if entry["ended"]:
                await scrobble(entry, [name])  # songs played before they picked
        if session and not session[-1]["ended"]:
            await now_playing(session[-1]["track"], [name])


async def end_session():
    """Music stopped for good: finish the last song, then forget who was listening."""
    global chosen_at
    if session and not session[-1]["ended"]:
        session[-1]["ended"] = time.time()
        await scrobble(session[-1], listeners)
    listeners.clear()
    session.clear()
    chosen_at = time.monotonic()


# ---------- songs ----------

async def song_changed(track, started_ago=0):
    """Called when a new song is identified (recorded even if nobody is listening, for late picks)."""
    now = time.time()
    if session and not session[-1]["ended"]:
        session[-1]["ended"] = now - started_ago
        await scrobble(session[-1], listeners)
    session.append({"track": track, "started": now - started_ago, "ended": None, "scrobbled": set()})
    await now_playing(track, listeners)


async def now_playing(track, names):
    for name in names:
        await safe("track.updateNowPlaying", artist=track["artist"], track=track["title"],
                   album=track.get("album"), sk=accounts[name])


async def scrobble(entry, names):
    if entry["ended"] - entry["started"] < MIN_PLAY_SECONDS:
        return
    t = entry["track"]
    for name in set(names) - entry["scrobbled"]:
        if await safe("track.scrobble", artist=t["artist"], track=t["title"], album=t.get("album"),
                      timestamp=int(entry["started"]), sk=accounts[name]):
            entry["scrobbled"].add(name)
            print(f"scrobbled for {name}: {t['artist']} - {t['title']}", flush=True)


async def safe(method, **params):
    try:
        await call(method, **params)
        return True
    except Exception as e:
        print(f"lastfm: {e}", flush=True)
        return False


load()
