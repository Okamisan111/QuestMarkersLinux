# QuestMarkersLinux

System-wide quest-marker overlay for **SPT (Single Player Tarkov)** on
Linux/Wine/Proton: a transparent on-screen HUD showing quest targets,
extracts, airdrops and BTR trains **outside** the game window — heavily
inspired by maschine's [QuestMarkers](https://sp-mod.com/mod/2954/quest-marker)
(MIT; see License & credits). A BepInEx plugin streams live feed frames over a
local WebSocket; a Python overlay renders them and a native Wayland
layer-shell (or X11) presenter puts them on your desktop. Includes
feed-freshness detection, scaling, imperial units, delayed-chord smoothing and
a self-test suite.

## How it works

> **The markers are not drawn inside the game window.** This mod is a bridge: the
> plugin streams the live feed, and a separate **Python overlay window** draws it.
> Pressing the toggle key alone changes nothing you can see unless the overlay
> is running. Full loop while playing:
> ```bash
> # 1) start the overlay BEFORE/WHILE a raid (desktop side, Linux):
> .venv/bin/python3 overlay.py --feed ws://127.0.0.1:9378
> # 2) in-game: press I during the raid (or enable Show in raid automatically)
> ```

1. `plugin/` — BepInEx plugin (shows as **QuestMarkersLinux**, file
   `chaossan-questmarkerslinux.dll`) runs in-game, scans quest/extract/airdrop/BTR targets and
   streams normalized feed frames (v2: `nx/ny/behind` per marker) to the overlay.
2. `overlay.py` — receives frames, renders maschine-style pin markers, tagboxes and
   **edge arrows** (off-screen targets only ever appear as arrows at the screen border,
   never in the middle), then streams dirty rects to the presenter.
3. Presenter — Wayland layer-shell (default, real per-pixel alpha) or X11 window.

## Running

```bash
python3 -m venv .venv
.venv/bin/pip install pycairo websockets   # pycairo needs cairo dev headers + pkg-config
bash wayland/build.sh          # native presenter (gcc, pkg-config, wayland-devel)
.venv/bin/python3 overlay.py --test            # self-test suite
.venv/bin/python3 overlay.py --feed ws://127.0.0.1:9378  # live with the game
```

Install into SPT: `dotnet build plugin/ -p:SptPath=/path/to/your/SPT` (or
export `SPT_PATH` first), then copy
`plugin/bin/Release/chaossan-questmarkerslinux.dll` into
`<SPT>/BepInEx/plugins/`.

`feed_sim.py` generates synthetic raid traffic for developing without the game.

## In-game plugin config (`BepInEx/config/com.chaossan.questmarkerslinux.cfg`)

Sections: **General**, **Display**, **Filter**, **Debug** — all English.

| Key | Default | Purpose |
|---|---|---|
| `General / Enabled` | true | stream the feed |
| `General / Toggle key` | **I** | press to show/hide markers during a raid |
| `General / Toggle key shows` | Quests, Extracts, Airdrops, BTR | which kinds the toggle marks |
| `General / Second key` | *(unbound)* | second hotkey with its own marker list |
| `General / Second key shows` | Extracts | what the second key marks |
| `General / Show in raid automatically` | true | show feed automatically at raid start |
| `General / Feed Port`, `Feed Hz` | 9378, 60 | WebSocket feed endpoint (Hz 1-180, game-frame-aligned) |
| `General / Feed Token` | empty (advanced) | shared feed secret — URI `?token=` + `"tok"` per frame; both sides must match (§ Feed Security) |
| `Display / Show Labels`, `Show Distance` | true | label / distance text |
| `Display / Edge Arrows` | true | off-screen targets as border arrows only |
| `Display / Marker Scale` | 1.0 | original QuestMarkers size (0.5 = half) |
| `Display / Far Distance`, `Far Scale` | 300 m, 0.6 | distant markers drawn smaller |
| `Display / Imperial Units` | false | feet / miles instead of m / km |
| `Display / Edge Arrow Size` | 24 px | arrow size at scale 1 |
| `Display / Icon Scale` | 1.0 | symbol (pin + kind icon) size multiplier (0.2-5.0); advanced |
| `Display / Auto-hide after` | 6 s | peek hides itself again |
| `Filter / Maximum Distance`, `Maximum Quest Markers` | 0 (no cap), 16 | feed filter |
| `Debug / Debug Log` | false | verbose feed/scan logging |

### Advanced settings (Better BepInEx)

With [Better BepInEx](https://sp-mod.com/mod/3207/better-bepinex) active,
`Feed Port`, `Feed Token`, `Feed Hz`, `Second key*`, `Auto-hide after`, `Far Distance`,
`Far Scale`, `Edge Arrow Size`, `Icon Scale` and `Debug Log` carry an
`IsAdvanced` flag: they are hidden from the normal config list and only visible
after the user enables Better BepInEx's advanced view. This tagging exists
**only** in Better BepInEx — stock BepInEx has no in-game config UI at all;
there all settings simply live in the plain `.cfg` file, nothing hidden or
missing.

## Overlay config (`config.json`)

`config.json` is intentionally **not shipped** in this repository — it carries
your personal feed token and monitor geometry. On a fresh clone:

```bash
cp config.example.json config.json
openssl rand -hex 16    # use the output as your feedToken
```

and set the same value in the plugin cfg (`General / Feed Token`, advanced
section). Two different random values on the two sides simply means the
overlay cannot connect — they are a shared secret, not individual settings.

**Token changes are never hot.** The plugin reads its cfg once at GAME start,
the overlay reads `config.json` once at OVERLAY start; an edit while either is
running has no effect (the rejected handshake logs `401` overlay-side and
`missing/wrong feed token` in the BepInEx log). Worse, BepInEx rewrites the
cfg from the in-memory config when the game exits — a mid-raid cfg edit can
be silently undone on exit. Correct order: close the game → set both sides to
the same value → start the game → start the overlay.

Feature mirror of the plugin: `labels`, `distance`, `edge`, `scale`,
`farDistance`, `farScale`, `imperial`, `edgeSize`, `iconScale`, `smooth`,
`presenter`, `font`, `hz` (overlay **render** rate, default 120 — the plugin's
**data** rate is `Feed Hz`, default 60; the chord smoother interpolates
between feed samples at render rate).

The font is chosen through fontconfig by name: if `Chakra Petch` is not
installed cairo silently falls back to the system default. Install it (Arch:
AUR `ttf-chakra-petch`; other distros / manual: fonts.google.com, SIL OFL 1.1)
or set `"font": ""` to request the system font on purpose.

The file is read from the directory of `overlay.py` regardless of the launch
directory (`--config PATH` selects another one) — so `feedToken` applies no
matter where the overlay is started from. A UTF-8 BOM is tolerated; a corrupt
or unreadable file prints one warning line and the overlay continues with
built-in defaults. Booleans are strict (`"labels": "no"` now really disables —
before, any non-empty string counted as true), `hz` is clamped to 1..300 and
`presenter` only ever accepts `wayland` or `x11`.

`feedToken` (CLI override `--feed-token`) is the feed secret — see § Feed Security;
it is appended to `ws://` connect URIs as `?token=…` and checked in every frame.

`overlay.py --debug` prints per-frame feed stats (rect count, feed age, effective
rate) every 2 s; `--readback` keeps the pixel-readback verification on.

## Feed Security (loopback WS auth)

The feed server binds to 127.0.0.1 only — but loopback means other local
processes and **any web page opened in your browser** (WebSocket handshakes
are not CORS-gated) could otherwise read the live game feed or evict the
overlay from the server's single client slot. The handshake enforces:

- requests carrying an `Origin` header (browsers always send one, RFC6455
  §4.1) are **rejected**; native clients never send it.
- a shared `Feed Token` (plugin cfg `General / Feed Token`, overlay
  `config.json feedToken`): the overlay connects with `?token=<token>` and
  every frame carries `"tok": "<token>"`; an overlay with a token configured
  drops **every** frame whose `tok` mismatches (redial after 3 failures) —
  that is what protects the client side against a local process that squats
  the port before the plugin binds it. An empty plugin token disables only
  the token check; browser-Origin requests stay rejected either way.
- handshake headers are capped (8 KB per line, 64 KB total).
- incoming frames are hardened VIEWER-side too: one `sanitize_feed` pass at
  the provider boundary guards the viewport (`v=[0,0]` used to raise
  ZeroDivisionError and drop whole frames), caps marker count (128), label
  length (160 chars, clipped labels end in a visible `…`) and every
  coordinate/distance field before any rendering or smoothing; `frame_rects`
  hard-caps the rect list at the presenter's MAXRECTS (2048) and warns about
  merged rects above 1.5 MPixel. Feed-injected feature values are always
  clamped (`iconScale` 0.25-4.0, `edgeSize` 4-192 px, scale 0.1-3.0,
  presenter/font validated; unknown `cfg` keys are dropped). The plugin caps
  its side too: labels are truncated at 120 chars (`…`), a frame is
  byte-budgeted to fit the plugin's 8 KB wire buffer (UTF-8 exact,
  distance-ordered, so the CLOSEST markers are the ones kept), and the
  presenter accepts rect payloads up to 17 MiB —
  enough for a 4K surface, still bounded against a bogus header.
  `--trace` JSONL caps at 64 MiB, `logs/overlay.log` rotates at
  startup (8 MiB -> `.1`, one generation), and the feed token never appears
  in a log line. Batteries: tests/test_feed_auth.py, tests/test_feed_hardening.py.

File-feed mode (`latest.json`, `feed_sim.py`) is a local dev tool and stays
unauthenticated. Set your own token value if you share the machine. This is
also why `config.json` is git-ignored: a committed token is a published
token — the tracked `config.example.json` only carries a placeholder.

## Running without a terminal (background mode)

The plugin lives inside the Windows game process (Wine) and cannot spawn Linux
programs — and it does not need to. The overlay is a single Python script;
start it directly, no wrapper script involved:

```bash
cd /path/to/questmarkerslinux

# one-time setup (local .venv, nothing system-wide, no root, no service)
python3 -m venv .venv
.venv/bin/pip install websockets pillow

# foreground run (Ctrl+C quits). SPT must be running; the overlay waits
# for the feed and shows markers automatically when a raid starts.
.venv/bin/python3 overlay.py
```

Detached — no terminal open at all:

```bash
mkdir -p logs
nohup .venv/bin/python3 overlay.py >> logs/overlay.log 2>&1 &
```

Check, watch, stop:

```bash
pgrep -f overlay.py            # status (empty = not running)
tail -f logs/overlay.log       # live log
pkill -f "python3 overlay.py"  # stop
```


Then just launch SPT, press **I** in a raid - markers appear without ever
touching a terminal.

## Notes

- Feed frames older than **1 second** trigger an automatic overlay clear —
  a crashed game can never leave frozen markers on your screen.
- The feed WebSocket answers client pings (RFC6455 pong). Before this the
  python `websockets` keepalive killed the connection every ~20-60 s mid-raid
  ("keepalive ping timeout") and the overlay showed a 2 s marker gap per event.
- Smoothing is a delayed-chord interpolator, not an EMA: the overlay always
  displays the position on the chord between the last two REAL feed
  measurements, evaluated at render time, so the display trails the newest
  measurement by one measurement interval (~33 ms at 30 Hz - constant and
  invisible) while moving with perfectly constant velocity at display rate.
  Why: the game feeds positions at game-FPS cadence, which is a staircase;
  an EMA re-accelerates toward every stair step and micro-stops before the
  next one - visible zappeln for slow/closing markers (measured on real
  Shoreline data: display-velocity pulses up to 3.5x true speed at 26-56 m).
  The chord model removes those pulses (median 0.23-0.81 -> 0.14-0.20 of
  true speed) and close-range parallax lag (34 px -> 11 px at 4 m). Seam
  blending (tunable overlay-side only, config.json `smooth` or `--smooth`,
  default 0.045 s - deliberately not an in-game option) softens velocity
  changes at chord boundaries: higher = softer direction changes, 0 = raw
  interpolation; at speed the blend is capped so it never trails more than
  ~6 px.
- Markers behind the player never appear as mirrored markers on screen:
  they become edge arrows (arrow at the nearest border side) or, with
  `Edge Arrows` disabled, draw nothing at all. Markers re-entering the view
  start fresh at their real position - no cross-screen interpolation from
  behind-camera projections. The display is always on a real chord: no extrapolation, a
  stationary marker is rock-steady, reversals overshoot <= ~3 px, and feed
  gaps > 300 ms snap instead of crawling. Battery: tests/test_smooth.py
  (includes a staircase test that proves the old EMA stutter and the fix).
- Overlapping markers are composited in Python (`merge_overlapping`): the
  presenter memcpy-copies dirty rects without blending, so before this,
  semi-transparent tagbox footprints of overlapping markers occluded each
  other as rectangular patches. Clustered rects are pixel-disjoint now.
- Display changes are followed live: the presenter reports its surface size
  on every KWin mode/scale change (`presenter: configured WxH` in the log)
  and the normalized feed coordinates are remapped onto the real surface —
  markers stay correctly placed under fractional scaling (e.g. 150 %,
  2560x1440 feed on a 1707x960 surface) instead of breaking after the first
  frame. A ROTATED display stays best-effort: an aspect guard prints one
  honest warning and markers are clipped to the surface rather than skewed.
  The x11 presenter probes depth-32 capability (XWayland advertises the
  visual but refuses the window) and falls back to the ShapeClip path with a
  clear message; a protocol-v2 feed is auto-converted for it, which fixes
  the old `KeyError: 'x'` crash on the first frame. A died presenter
  respawns (5 tries per death, at most 50 deaths per run before the overlay
  gives up with an explicit log line).
- `t` fields in feed JSON are epoch **milliseconds**.
- Distances format like the original: `885m` / `1.1km` (metric) or `4921ft`
  below 1609 m and `1.2mi` from there (imperial; 1 mi = 5280 ft).

## License & credits

MIT (see `LICENSE`). The marker design, icon set and visual language are
adapted from **maschine's QuestMarkers** (MIT,
<https://sp-mod.com/mod/2954/quest-marker>) — thank you, maschine, for the
mod and for blessing this port. Text rendering uses the system font
**Chakra Petch** (SIL Open Font License 1.1, fonts.google.com); this
repository does not ship the font itself.

This is an unofficial community port for Linux/Proton (SPT) play. It is not
affiliated with or endorsed by Battlestate Games.
