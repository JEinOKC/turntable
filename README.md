# Turntable

Stream a record player to Sonos from a Raspberry Pi, with a control page for your phone, a touchscreen kiosk, and music visualizers.

- **Rooms**: finds every Sonos room on the network; switch rooms on and off and set their volume.
- **Auto-play**: starts chosen rooms when a record starts playing and stops them after a few minutes of silence.
- **Pi screen**: shows the controls, a visualizer (spinning record, bars, waveform, fireplace), or switches off. You can change it from your phone. It sleeps when the music stops and wakes when it starts.
- **What's playing**: identifies each song with Shazam and shows it on the page, the Pi screen, and optionally Last.fm.
- **Lossless**: optional FLAC stream alongside the 320 kbps MP3, switchable without interrupting playback.

## How it works

```
turntable ──USB audio──▶ ffmpeg ──MP3──▶ Icecast :8000 ──▶ Sonos
                           │
                           └─PCM─▶ server.py ──FLAC──▶ :80/turntable.flac ──▶ Sonos
                                      │
                                      ├─ level detection → auto-play / screen sleep
                                      ├─ spectrum → WebSocket → visualizers
                                      └─ control page + API on :80, Sonos via SoCo
```

Sonos plays a few seconds behind the live input, so the visualizer data is delayed to match.

## Hardware

- Raspberry Pi 3 or newer, with a proper power supply (5V 2.5A for a Pi 3). An undersized one causes slowdowns and Wi-Fi drops.
- A turntable with USB audio output, or any USB audio interface.
- Optional: the official 7" touchscreen.

## Setup

1. Flash **Raspberry Pi OS (64-bit)** with Raspberry Pi Imager. Set the hostname to `turntable` and the user to `pi`, and enable SSH with your public key.
2. Plug the turntable into the Pi over USB.
3. From this repo on your computer, run:

   ```sh
   deploy/push.sh            # or: deploy/push.sh pi@<pi-address>
   ```

   This installs Icecast, ffmpeg and SoCo, generates the Icecast passwords, starts the `turntable` service, and sets the screen to open the control page on login.
4. Open **http://turntable.local** on your phone. In Safari, **Share → Add to Home Screen** installs it as an app.

Re-run `deploy/push.sh` after any change.

## Configuration

`/etc/turntable.env` on the Pi (created by the installer):

| Variable | Default | |
|---|---|---|
| `GAIN_DB` | `0` | Input boost in dB |
| `VISUAL_DELAY` | `2.5` | Seconds to delay the visuals so they match Sonos |
| `ALSA_DEVICE` | `hw:CODEC` | Capture device (`arecord -l` lists them) |
| `LASTFM_API_KEY`, `LASTFM_API_SECRET` | | Optional. From a [Last.fm API account](https://www.last.fm/api/account/create); enables scrobbling. Connect your account from the control page. |

The settings on the control page are saved to `~/.config/turntable/settings.json`, where you can also change `auto_rooms` and `idle_minutes`.

Restart the service after editing either file: `sudo systemctl restart turntable`.

## Screen orientation

If the touchscreen is mounted upside down, add this to `~/.config/kanshi/config` on the Pi:

```
profile {
	output DSI-1 enable mode 800x480 position 0,0 transform 180
}
```
