#!/usr/bin/env python3
"""Native quest marker overlay for SPT (Escape from Tushonka) - Wayland only.

Transparent, frameless, click-through overlay surface on your game monitor.
Marker data comes from the bundled BepInEx plugin (live WebSocket feed) or
from a latest.json writer such as feed_sim.py.

Why a native layer-shell presenter:
- The overlay is a wlr-layer-shell client (`wayland/wayland_present`), so the
  compositor itself blends the alpha channel: no compositing tricks, no
  per-window clipping and real per-pixel transparency on KDE/GNOME/wlroots.
- Rendering stays in Python/cairo: each marker becomes a small ARGB surface,
  and only the dirty rectangles are piped to the presenter (~6 KB per frame
  instead of a full 14 MB surface at 30 Hz).
- X11 is deliberately NOT supported. Under XWayland the depth-32 visual is
  refused and depth-24 windows cannot carry alpha, which yields a black
  screen instead of an overlay.

Run (venv with pycairo, see README):
  .venv/bin/python3 overlay.py [options]

Options:
  --feed SOURCE    feed source: path to latest.json (feed_sim.py output,
                   file:// also accepted) or a
                   ws://host:9378 URL (QuestMarkersLinux plugin; the feed
                   token from config.json is used for the challenge
                   handshake, never appended to the URL)
  --seconds N      run duration for --feed (default 0 = run until Ctrl-C)
  --hz FLOAT       feed render rate, clamped to 1..300 (default 120)
  --config PATH    JSON feature defaults; default config.json lives NEXT TO
                   overlay.py regardless of launch directory; CLI flags win
  --geometry WxH+X+Y   overlay surface placement (default fullscreen at 0,0)
"""

import argparse
import hashlib
import hmac
import json
import re
import math
import os
import subprocess
import struct
import sys
import threading
import time

import cairo

# Native Wayland presenter: a thin C layer-shell client that owns a
# transparent, click-through surface and blits ARGB frames from stdin.
# XWayland on this setup refuses depth-32 windows and honours no alpha for
# depth-24 ones, so the compositor's own alpha blending is the only way to a
# truly transparent overlay.
PRESENTER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "wayland", "wayland_present")

# ---------------------------------------------------------------------------
# Marker rendering (cairo ImageSurface per box, DOM-like appearance)
# ---------------------------------------------------------------------------

# Visual constants = DOM contract (values verbatim from the verified HUD page):
# pin = 26 px square, border-radius 50% 50% 50% 0, rotate(-45deg) -> teardrop,
# 1 px border rgb(194,173,109), bg rgb(27,28,24); TIP = anchor point.
# tagbox: label 20 px rgb(208,205,189), dist 20 px rgb(145,142,126),
# 1 px border rgb(194,173,109), bg rgb(9,14,11), radius 4 px.
# Font "QuestMarkers" is the game page webfont — extracted live via CDP in
# m1-verify; until then DejaVu Sans is the documented substitute.
KIND_COLORS = {
    # authoritative accents + exfil states from maschine's QuestMarkers 1.4 CSS
    "extract": {"open": (0x5F, 0xD0, 0x68), "locked": (0x9A, 0x9A, 0x8E),
                "countdown": (0xF0, 0xB4, 0x29), "default": (0x5F, 0xD0, 0x68)},
    "zone": (0xC2, 0xAD, 0x6D),      # gold
    "plant": (0x86, 0xB9, 0x6A),    # sage green
    "beacon": (0xB6, 0x99, 0xE0),   # purple
    "flare": (0xE0, 0xA3, 0x5C),    # orange
    "item": (0x8F, 0xC7, 0xF2),    # light blue
    "exit": (0xE0, 0x88, 0x8A),    # pink
    "airdrop": (0x4C, 0xC3, 0xD9), # cyan
    "btr": (0xE0, 0x60, 0x4F),     # red
}
FONT = "Chakra Petch"    # maschine's QuestMarkers webfont (installed font)
PIN_R = 13.0            # 26 px square -> circle radius
TIP_OFF = 18.384        # tip -> circle center for rotated square (13*(1+sqrt2)/2)
PIN_BG = (27, 28, 24)
PIN_BORDER = (194, 173, 109)
BOX_BG = (27, 28, 24)  # live DOM: .tag background-color rgb(27, 28, 24)
LABEL_COLOR = (208, 205, 189)
DIST_COLOR = (145, 142, 126)
LABEL_FONT = 20
DIST_FONT = 20
PAD_X = 10
PAD_Y = 3
GAP_PIN_BOX = 2
BOX_MIN_W = 28          # box at least as wide as the pin
SC = 1.0                # global scale (CLI --scale / config "scale")
EDGE_MAX_DIST = 250     # max px an edge arrow may sit away from the screen edge

# Semi-transparency, tiered. One scalar per marker reaches the renderer: the
# plugin's in-game "Marker Opacity" arrives as the wire field "op", an
# overlay-side "markerAlpha" (config.json) multiplies it. Each layer then keeps
# its own share of what is left:  layer alpha = 1 - (1 - opacity) * ALPHA_WEIGHTS
# The plugin's default from 1.2.2 on is 0.35, so a fresh install ships
# translucent: plate/pin A89, outlines A172, icon A205, distance A242, label
# A255. 1.0 - what a plugin older than 1.2.2 sends, because it sends no "op" at
# all - reproduces the fully opaque look pixel for pixel. Lowering it opens the
# tagbox/pin background up but holds the outlines and text readable over bright
# maps (snow, sand, concrete) - a flat 1:1 fade would wash the text out first.
ALPHA_WEIGHTS = {
    "plate": 1.00,     # tagbox background
    "pin": 1.00,       # pin fill
    "border": 0.50,    # 1 px outlines: separation from the map matters most
    "icon": 0.30,      # kind icon strokes + accent fills
    "label": 0.00,     # quest label text: never transparent
    "dist": 0.08,      # distance text
}

# Config features (keys are English; the overlay reads the plugin's
# config.json when present and the feed may override them per frame via "cfg").
FEATURE_DEFAULTS = {
    "labels": True,          # ShowLabels: marker label text
    "distance": True,        # ShowDistance: meter text next to label / on edges
    "edge": True,            # EdgeArrows: arrows at screen border for off-screen
                             # markers (never a marker in the middle)
    "scale": 1.0,            # MarkerScale: original QuestMarkers size (0.5 = half)
    "farDistance": 300.0,    # FarDistance [m]: beyond this, shrink markers ...
    "farScale": 0.6,         # ... by this factor (1 = off)
    "imperial": False,       # ImperialUnits: display feet / miles instead of m / km
    "smooth": 0.045,         # chord seam-blend time [s] (0 = raw feed);
                             # overlay-side only - not an in-game option
    "markerAlpha": 1.0,      # overlay-side dimmer, multiplied into the plugin's
                             # "Marker Opacity" (0.35 by default from 1.2.2). Left
                             # at 1.0 here so the shipped look is the plugin value
                             # alone; below 1 the tagbox/pin background opens up
                             # further while outlines and text keep their opacity
                             # (ALPHA_WEIGHTS above).
    "edgeSize": 24,          # EdgeArrowSize [px]: arrow long side at scale 1
    "iconScale": 1.0,        # symbol multiplier: pin geometry + kind icon
                             # (text stays readable). 1.0 is the standard size
                             # = 1.7x original pin baked in as ICON_BASE
    "font": "Chakra Petch",  # Font: maschine's bundled font name (upstream's
                             # "Use the bundled font" - set "" for system default)
}


# ---------------------------------------------------------------------------
# Feed hardening (security review MEDIUM/LOW): incoming feed frames are
# treated as hostile input. sanitize_feed() is the single provider-boundary
# choke point for file AND ws feeds; frame_rects re-guards viewport/rect
# counts as defense in depth. All caps are generous - a real plugin feed
# sends <= ~32 targets and never approaches them.
MAX_MARKERS = 128
MAX_EDGES = 64
MAX_LABEL = 160
MAX_KEY = 64
MAX_COORD = 100.0           # nx/ny: beyond 1.1 is already off-screen
MAX_PX = 1e6                # legacy x/y pixel coordinates
MAX_METERS = 1e7
MAX_RECTS = 2048            # matches the presenter C MAXRECTS wire guard
TRACE_MAX_BYTES = 64 << 20  # --trace JSONL disk cap
LOG_ROTATE_BYTES = 8 << 20  # overlay.log startup rotation (-> overlay.log.1)

_san_warn = {"markers": 0, "rects": 0, "viewport": 0}


def _finite(v, default=None):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _mask_url(s, token=""):
    """Keep the feed token out of EVERY log line (overlay.log mirrors stdout)."""
    return s.replace(token, "***") if token else s


_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
_CONFIG_WARNED = set()
_ASPECT_WARNED = False


def _load_json(path):
    """Parse JSON with user-facing errors: a UTF-8 BOM is tolerated, corrupt
    or unreadable content degrades to None (built-in defaults) with a
    one-line message instead of a raw traceback. A half-written config after
    a crash must not stop the overlay from starting. Warn once per path
    (cfg_of re-reads the file per frame - no message spam)."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        if path not in _CONFIG_WARNED:
            _CONFIG_WARNED.add(path)
            msg = getattr(exc, "msg", None) or str(exc)
            print(f"config: {path} is corrupt ({exc.__class__.__name__}: {msg})"
                  " - using built-in defaults")
        return None
    except OSError as exc:
        if path not in _CONFIG_WARNED:
            _CONFIG_WARNED.add(path)
            print(f"config: {path} unreadable ({exc}) - using built-in defaults")
        return None


def _flag(value, default):
    """Strict bool parsing: JSON true/false win; a string must say what it
    means ('no'/'false'/'off'/'0' is False). Python truthiness made the
    string "no" silently KEEP features enabled."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "yes", "on", "1"):
            return True
        if low in ("false", "no", "off", "0", ""):
            return False
        return default
    if isinstance(value, (int, float)) and math.isfinite(value):
        return value != 0
    return default


def _clip_label(raw):
    """MAX_LABEL truncation with a visible '…' so a clipped label is not
    mistaken for a long quest name."""
    s = str(raw or "")
    if len(s) > MAX_LABEL:
        return s[:MAX_LABEL - 1] + "…"
    return s


def sanitize_feed(frame):
    """Provider-boundary hardening for one incoming feed frame.

    v=[0,0] once caused a ZeroDivisionError (whole frame lost -> 'feed lost'
    flicker); hostile iconScale/label lengths/marker counts would allocate
    absurd cairo surfaces. Every field is bounded here, BEFORE smoothing,
    rendering or the presenter wire. The trace tap keeps logging the RAW
    frame (jitter diagnostics); only the consumer view is sanitized.
    """
    if not isinstance(frame, dict):
        return None
    out = dict(frame)
    t = _finite(out.get("t"))
    out["t"] = t if t is not None else time.time()

    v = out.get("v")
    ok = False
    if isinstance(v, (list, tuple)) and len(v) == 2:
        vw, vh = _finite(v[0]), _finite(v[1])
        if (vw is not None and vh is not None
                and 1.0 <= vw <= 16384.0 and 1.0 <= vh <= 16384.0):
            out["v"] = [vw, vh]
            ok = True
    if not ok:
        if v is not None and _san_warn["viewport"] < 3:
            _san_warn["viewport"] += 1
            print("feed frame: degenerate viewport 'v' -> default 2560x1440")
        out["v"] = [2560.0, 1440.0]

    m = out.get("m")
    if not isinstance(m, list):
        m = []
    if len(m) > MAX_MARKERS:
        if _san_warn["markers"] < 3:
            _san_warn["markers"] += 1
            print(f"feed frame: {len(m)} markers > {MAX_MARKERS} -> truncated")
        m = m[:MAX_MARKERS]
    clean = []
    for item in m:
        if not isinstance(item, dict):
            continue
        mm = dict(item)
        for key in ("nx", "ny"):
            val = _finite(mm.get(key))
            if val is not None:
                mm[key] = max(-MAX_COORD, min(MAX_COORD, val))
            elif key in mm:
                mm[key] = None      # non-numeric -> legacy path (x/y)
        for key in ("x", "y"):
            if key in mm:
                mm[key] = max(-MAX_PX, min(MAX_PX, _finite(mm[key], 0.0)))
        d = mm.get("d")
        if isinstance(d, str):
            mm["d"] = d[:32]
        elif isinstance(d, (int, float)):
            mm["d"] = max(0.0, min(MAX_METERS, _finite(d, 0.0)))
        else:
            mm["d"] = None
        mm["l"] = _clip_label(mm.get("l", ""))
        if mm.get("k") is not None:
            mm["k"] = str(mm["k"])[:MAX_KEY]
        s = mm.get("s")
        if isinstance(s, str):
            mm["s"] = s[:MAX_KEY]
        elif isinstance(s, (int, float)):
            mm["s"] = max(-1e6, min(1e6, _finite(s, 0) or 0))
        elif "s" in mm:
            # lists/dicts/objects would land unhashable in the smoothing key
            # tuple (TypeError in every frame) or render as garbage
            mm["s"] = None
        # unclamped scale multipliers would ask for multi-MB cairo surfaces
        # per marker (DoS) - the cfg path clamps, per-marker sc/ic must too
        for key in ("sc", "ic"):
            if key in mm:
                mm[key] = max(0.1, min(8.0, _finite(mm[key], 1.0)))
        mm["op"] = max(0.0, min(1.0, _finite(mm.get("op"), 1.0)))
        if mm.get("b") is not None:
            b = _finite(mm["b"])
            mm["b"] = 0.0 if b is None else max(0.0, min(1.0, b))
        clean.append(mm)
    out["m"] = clean

    e = out.get("e")
    if isinstance(e, list):
        if len(e) > MAX_EDGES:
            e = e[:MAX_EDGES]
        clean_e = []
        for item in e:
            if not isinstance(item, dict):
                continue
            ee = dict(item)
            for key in ("x", "y"):
                val = _finite(ee.get(key))
                if val is not None:
                    ee[key] = max(-MAX_PX, min(MAX_PX, val))
            ee["l"] = _clip_label(ee.get("l", ""))
            ee["op"] = max(0.0, min(1.0, _finite(ee.get("op"), 1.0)))
            # edge geometry comes from an untrusted frame too: w/h/sc without
            # bounds become absurd surface allocations
            for key in ("w", "h"):
                if key in ee:
                    ee[key] = max(1.0, min(4096.0, _finite(ee[key], 24.0)))
            if "sc" in ee:
                ee["sc"] = max(0.1, min(8.0, _finite(ee["sc"], 1.0)))
            clean_e.append(ee)
        out["e"] = clean_e
    else:
        out["e"] = []
    return out


def cfg_of(feed=None):
    """Effective feature dict: FEATURE_DEFAULTS <- plugin config.json <- feed cfg."""
    cfg = dict(FEATURE_DEFAULTS)
    try:
        user_cfg = _load_json(_CONFIG_PATH)
        if user_cfg is None:
            raise ValueError("no parseable config")
        if not isinstance(user_cfg, dict):
            raise ValueError("config must be a JSON object")
        for key in ("labels", "distance", "edge", "scale", "farDistance",
                    "farScale", "imperial", "edgeSize", "font",
                    "iconScale", "smooth", "markerAlpha"):
            if key in user_cfg:
                cfg[key] = user_cfg[key]
    except Exception:
        pass
    if feed:
        for key, value in (feed.get("cfg") or {}).items():
            if key in cfg:
                cfg[key] = value
    for key, lo, hi in (("scale", 0.1, 3.0), ("farScale", 0.1, 3.0),
                        ("farDistance", 1.0, 100000.0), ("smooth", 0.0, 3.0),
                        # 0.05 floor: below it every layer would be so thin that
                        # the markers read as noise, and the op <= 0.01 visibility
                        # gate would silently swallow them
                        ("markerAlpha", 0.05, 1.0),
                        # a feed can inject these geometry values directly:
                        # iconScale=1e9 would ask for multi-MB cairo surfaces
                        # per marker, edgeSize likewise - clamp always.
                        ("iconScale", 0.25, 4.0), ("edgeSize", 4.0, 192.0)):
        val = _finite(cfg.get(key))
        cfg[key] = max(lo, min(hi, val)) if val is not None else FEATURE_DEFAULTS[key]
    for key in ("labels", "distance", "edge", "imperial"):
        cfg[key] = _flag(cfg[key], FEATURE_DEFAULTS[key])
    if not isinstance(cfg["font"], str) or len(cfg["font"]) > 128:
        cfg["font"] = FEATURE_DEFAULTS["font"]
    return cfg


def fmt_dist(meters, cfg):
    """Distance text like the original mod: <1000 -> '885m', >=1000 -> '1.1km'
    (imperial: feet / miles, 'ft' always, 'mi' only >= 1 mile)."""
    if meters is None:
        return ""
    meters = float(meters)
    if cfg.get("imperial"):
        feet = meters * 3.28084
        if feet < 5280:
            return f"{int(round(feet))}ft"
        return f"{max(0.1, round(meters / 1609.344, 1))}mi"
    if meters < 1000:
        return f"{int(round(meters))}m"
    return f"{max(0.1, round(meters / 1000.0, 1))}km"


def parse_meters(raw):
    """Feed distance -> meters. Numbers are meters (protocol); strings like
    '123 m' / '1.4 km' are parsed for legacy feeds."""
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw or "").strip().lower().replace(",", ".")
    if not s:
        return None
    try:
        if "km" in s:
            return float(s.replace("km", "").strip()) * 1000.0
        if "m" in s:
            return float(s.replace("m", "").strip())
        return float(s)
    except ValueError:
        return None


# Original mod draw order: farther/higher-priority kinds under, quests on top
Z_ORDER = {"extract": 0, "airdrop": 1, "plant": 2, "item": 3,
           "beacon": 4, "flare": 4, "btr": 5, "exit": 6, "zone": 6, "quest": 6}


_FONT_METRICS = {}


def _font_metrics(size, bold):
    """Measured (ascent, descent) for FONT at size — box heights budget these
    so descenders never overflow the tagbox background."""
    key = (size, bold)
    got = _FONT_METRICS.get(key)
    if got is None:
        tmp = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
        ctx = cairo.Context(tmp)
        ctx.set_font_size(size)
        ctx.select_font_face(FONT, cairo.FONT_SLANT_NORMAL,
                             cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
        asc, desc = ctx.font_extents()[0], ctx.font_extents()[1]
        got = (asc, desc)
        _FONT_METRICS[key] = got
    return got


def _marker_sizes(m):
    """All marker dimensions scaled by SC * feed 'sc' (0.5 = half size).
    'pin' additionally carries the cfg iconScale symbol multiplier: pin
    geometry and the kind icon scale with it while text sizes do not."""
    ICON_BASE = 1.7  # new standard: 1.0 iconScale = 1.7x original pin size
    sc = SC * float(m.get("sc", 1.0) or 1.0)
    if sc <= 0.05:
        sc = 1.0
    ic = float(m.get("ic", 1.0) or 1.0)
    if not 0.2 <= ic <= 5.0:
        ic = 1.0
    label_font = max(9, int(round(LABEL_FONT * sc)))
    dist_font = max(8, int(round(DIST_FONT * sc)))
    asc_l = _font_metrics(label_font, True)[0]
    desc_d = _font_metrics(dist_font, False)[1]
    return {
        "sc": sc,
        "pin": sc * ic * ICON_BASE,
        "label_font": label_font,
        "dist_font": dist_font,
        "asc_l": asc_l,
        "desc_d": desc_d,
        "pad_x": max(3, int(round(PAD_X * sc))),
        "pad_y": max(1, int(round(PAD_Y * sc))),
        "gap": max(1, int(round(GAP_PIN_BOX * sc))),
        "box_min": max(10, int(round(BOX_MIN_W * sc))),
        "pin_r": PIN_R * sc,
        "tip_off": TIP_OFF * sc,
        "radius": max(2, int(round(4 * sc))),
    }


def kind_color(k, state):
    v = KIND_COLORS.get(k, (0xE8, 0xE8, 0xE8))
    if isinstance(v, dict):  # extract/exit carry exfil state colours
        return v.get(state, v["default"])
    return v


# Pin shape + vector icon per kind: a faithful port of maschine's QuestMarkers
# 1.4 marker sheet (markers.html). Teardrop pins stay the default, but extracts
# and exits ride a square-cornered diamond and the kinds that move during a
# raid (airdrop crate, BTR) ride a circle - each carrying a 24x24 stroke icon
# in the accent colour. Locked extracts/exits turn grey with a dashed outline
# and a dimmed icon; a countdown (leaving soon) turns amber. Path data below
# is verbatim from the sheet, so the glyphs are pixel-faithful, not approximations.
PIN_SHAPE = {"extract": "diamond", "exit": "diamond",
             "airdrop": "circle", "btr": "circle"}
PIN_TOPS = {  # px above the feed point a shape spans at scale 1
    "teardrop": TIP_OFF + PIN_R,
    "diamond": TIP_OFF + PIN_R * math.sqrt(2.0),
    "circle": PIN_R * 2.0,
}

ICONS = {  # ("d", path, fill25) | ("c", cx, cy, r, fill25) | ("r", x, y, w, h, rx, fill25)
    "zone": [("d", "M6 21V3.5", False),
             ("d", "M6 4.5h11l-2.5 3.5L17 11.5H6z", True)],
    "plant": [("d", "M12 3v8.5", False),
              ("d", "M8 8l4 4 4-4", False),
              ("d", "M4 14.5V20h16v-5.5", False)],
    "beacon": [("c", 12.0, 7.0, 2.0, False),
               ("d", "M12 10.5V20M8 20h8", False),
               ("d", "M7.6 11.2a6 6 0 0 1 0 -8.4", False),
               ("d", "M16.4 2.8a6 6 0 0 1 0 8.4", False)],
    "flare": [("c", 12.0, 12.0, 3.2, True),
              ("d", "M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3", False),
              ("d", "M5.4 5.4l2.1 2.1M16.5 16.5l2.1 2.1M18.6 5.4l-2.1 2.1M7.5 16.5l-2.1 2.1", False)],
    "item": [("r", 3.5, 7.5, 17.0, 12.0, 1.5, False),
             ("d", "M9 7.5V6a3 3 0 0 1 6 0v1.5", False),
             ("d", "M3.5 12.5h17", False)],
    "extract": [("d", "M5.5 6l6 6-6 6", False),
                ("d", "M12.5 6l6 6-6 6", False)],
    "airdrop": [("d", "M3.5 10a8.5 6.5 0 0 1 17 0z", True),
                ("d", "M3.5 10l6 6.5M20.5 10l-6 6.5M12 10v6.5", False),
                ("r", 9.0, 16.5, 6.0, 4.5, 0.8, False)],
    "btr": [("d", "M2.5 15.5h19l-1.6-4.2H15.2l-.9-2.3H9.7l-.9 2.3H4.1z", True),
            ("d", "M14.3 9H20.5", False),
            ("c", 6.5, 18.6, 1.7, False),
            ("c", 12.0, 18.6, 1.7, False),
            ("c", 17.5, 18.6, 1.7, False)],
    "exit": [("d", "M13 3.5H5.5v17H13", False),
             ("d", "M10.5 12h9", False),
             ("d", "M16.5 8.5 20 12l-3.5 3.5", False)],
}

_TOK = re.compile(r"([MmHhVvLlAaZz])|(-?\d*\.?\d+)")


def _svg_arc(ctx, x1, y1, rx, ry, large, sweep, x2, y2):
    """SVG elliptical arc (x-axis rotation 0) -> cairo arc under a scale.

    Scaling by 1/rx, 1/ry collapses the centre problem onto a unit circle;
    save/scale/arc/restore maps the unit-circle arc back onto the ellipse,
    which reproduces SVG semantics exactly for the sheet's data (rotation
    is always 0 there, so the rotation terms drop out).
    """
    rx, ry = abs(rx), abs(ry)
    if rx <= 0.0 or ry <= 0.0:
        ctx.line_to(x2, y2)
        return
    sx, sy = x1 / rx, y1 / ry
    ex, ey = x2 / rx, y2 / ry
    chord = math.hypot(ex - sx, ey - sy)
    if chord > 2.0:  # radii too small for the endpoints: grow both equally
        fac = chord / 2.0
        rx, ry = rx * fac, ry * fac
        sx, sy = x1 / rx, y1 / ry
        ex, ey = x2 / rx, y2 / ry
        chord = 2.0
    if chord < 1e-9:
        ctx.line_to(x2, y2)
        return
    mx, my = (sx + ex) / 2.0, (sy + ey) / 2.0
    nx, ny = -(ey - sy) / chord, (ex - sx) / chord
    h = math.sqrt(max(0.0, 1.0 - (chord / 2.0) ** 2))
    best = None
    for sgn in (1.0, -1.0):
        cx, cy = mx + sgn * nx * h, my + sgn * ny * h
        a1 = math.atan2(sy - cy, sx - cx)
        a2 = math.atan2(ey - cy, ex - cx)
        for d in (1.0, -1.0):  # +1 = increasing angle = SVG sweep flag 1
            span = (a2 - a1) % (2.0 * math.pi)
            if d < 0.0:
                span = (a1 - a2) % (2.0 * math.pi)
            if span < 1e-9:
                continue
            if (span > math.pi) != bool(large) or (d > 0.0) != bool(sweep):
                continue
            best = (cx, cy, a1, a2, d)
            break
    if best is None:
        ctx.line_to(x2, y2)
        return
    cx, cy, a1, a2, d = best
    ctx.save()
    ctx.scale(rx, ry)
    ctx.move_to(cx + math.cos(a1), cy + math.sin(a1))
    if d > 0.0:
        ctx.arc(cx, cy, 1.0, a1, a2)
    else:
        ctx.arc_negative(cx, cy, 1.0, a1, a2)
    ctx.restore()


def _svg_path(ctx, d, ox, oy, s):
    """Mini SVG path interpreter: Mm Hh Vv Ll Aa Zz, rotation 0 arcs only -
    the subset the marker sheet uses. 24x24 space mapped by (ox, oy, s)."""
    toks = [t for pair in _TOK.findall(d) for t in pair if t]
    i = 0
    x = y = 0.0
    subx = suby = 0.0

    def num():
        nonlocal i
        v = float(toks[i])
        i += 1
        return v

    cmd = None
    while i < len(toks):
        if toks[i].isalpha():
            cmd = toks[i]
            i += 1
        if cmd is None:
            return
        rel = cmd.islower()
        if cmd in "Mm":
            while i < len(toks) and not toks[i].isalpha():
                a, b = num(), num()
                x, y = (x + a * s if rel else ox + a * s,
                        y + b * s if rel else oy + b * s)
                ctx.move_to(x, y)
                subx, suby = x, y
                cmd = "L" if cmd == "M" else "l"
                break
        elif cmd in "Ll":
            while i < len(toks) and not toks[i].isalpha():
                a, b = num(), num()
                x, y = x + a * s if cmd == "l" else ox + a * s, y + b * s if cmd == "l" else oy + b * s
                ctx.line_to(x, y)
        elif cmd == "H":
            while i < len(toks) and not toks[i].isalpha():
                x = ox + num() * s
                ctx.line_to(x, y)
        elif cmd == "h":
            while i < len(toks) and not toks[i].isalpha():
                x += num() * s
                ctx.line_to(x, y)
        elif cmd == "V":
            while i < len(toks) and not toks[i].isalpha():
                y = oy + num() * s
                ctx.line_to(x, y)
        elif cmd == "v":
            while i < len(toks) and not toks[i].isalpha():
                y += num() * s
                ctx.line_to(x, y)
        elif cmd in "Aa":
            while i < len(toks) and not toks[i].isalpha():
                rx, ry = num() * s, num() * s
                _ = num()  # x-axis rotation: the sheet never uses it
                large, sweep = int(num()), int(num())
                a, b = num(), num()
                nx2 = x + a * s if cmd == "a" else ox + a * s
                ny2 = y + b * s if cmd == "a" else oy + b * s
                _svg_arc(ctx, x, y, rx, ry, large, sweep, nx2, ny2)
                x, y = nx2, ny2
        elif cmd in "Zz":
            ctx.close_path()
            x, y = subx, suby
        else:
            return


def _icon_prim(ctx, pr, ox, oy, s):
    el = pr[0]
    if el == "d":
        _svg_path(ctx, pr[1], ox, oy, s)
    elif el == "c":
        cx, cy, r = ox + pr[1] * s, oy + pr[2] * s, pr[3] * s
        ctx.move_to(cx + r, cy)
        ctx.arc(cx, cy, r, 0.0, 2.0 * math.pi)
    elif el == "r":
        x0, y0 = ox + pr[1] * s, oy + pr[2] * s
        w, h, rad = pr[3] * s, pr[4] * s, pr[5] * s
        if rad < 0.75:
            ctx.move_to(x0, y0)
            ctx.line_to(x0 + w, y0)
            ctx.line_to(x0 + w, y0 + h)
            ctx.line_to(x0, y0 + h)
            ctx.close_path()
        else:
            ctx.move_to(x0 + rad, y0)
            ctx.line_to(x0 + w - rad, y0)
            ctx.arc(x0 + w - rad, y0 + rad, rad, -math.pi / 2.0, 0.0)
            ctx.line_to(x0 + w, y0 + h - rad)
            ctx.arc(x0 + w - rad, y0 + h - rad, rad, 0.0, math.pi / 2.0)
            ctx.line_to(x0 + rad, y0 + h)
            ctx.arc(x0 + rad, y0 + h - rad, rad, math.pi / 2.0, math.pi)
            ctx.line_to(x0, y0 + rad)
            ctx.arc(x0 + rad, y0 + rad, rad, math.pi, 3.0 * math.pi / 2.0)
            ctx.close_path()


def _rounded_poly(ctx, pts, r):
    n = len(pts)
    started = False
    for i in range(n):
        px, py = pts[i]
        ax, ay = pts[i - 1]
        bx, by = pts[(i + 1) % n]
        la = math.hypot(ax - px, ay - py)
        lb = math.hypot(bx - px, by - py)
        rr = min(r, la / 2.0, lb / 2.0)
        ix, iy = px + (ax - px) / la * rr, py + (ay - py) / la * rr
        ox2, oy2 = px + (bx - px) / lb * rr, py + (by - py) / lb * rr
        if not started:
            ctx.move_to(ix, iy)
            started = True
        else:
            ctx.line_to(ix, iy)
        a1 = math.atan2(iy - py, ix - px)
        a2 = math.atan2(oy2 - py, ox2 - px)
        # pycairo arc() is positional-only; take the SHORT way round regardless
        # of polygon winding so convex corners never bulge
        if (a2 - a1) % (2.0 * math.pi) > math.pi:
            ctx.arc_negative(px, py, rr, a1, a2)
        else:
            ctx.arc(px, py, rr, a1, a2)
    ctx.close_path()


def _pin_diamond(ctx, tipx, tipy, sc, color, dash=None, fill_a=1.0, stroke_a=1.0):
    """Extract/exit pin: maschine's 26px square rotated 45 deg about its
    bottom-left corner -> a 4px-rounded diamond whose lower vertex is the
    feed point and whose centre is exactly the teardrop circle centre."""
    h = PIN_R * math.sqrt(2.0) * sc
    cx, cy = float(tipx), tipy - h
    pts = [(cx, tipy), (cx + h, cy), (cx, tipy - 2.0 * h), (cx - h, cy)]
    _rounded_poly(ctx, pts, 4.0 * sc)
    _rgba(ctx, PIN_BG, fill_a)
    ctx.fill_preserve()
    ctx.set_line_width(max(1.0, 1.0 * sc))
    if dash:
        ctx.set_dash(dash, 0.0)
    _rgba(ctx, color, stroke_a)
    ctx.stroke()
    if dash:
        ctx.set_dash([], 0.0)


def _pin_circle(ctx, tipx, tipy, sc, color, dash=None, fill_a=1.0, stroke_a=1.0):
    """Airdrop/BTR pin: a 26px circle whose lower tangent is the feed point
    (maschine seats its rotated square so the circle lands on the marker)."""
    r = PIN_R * sc
    cx, cy = float(tipx), tipy - r
    ctx.move_to(cx + r, cy)
    ctx.arc(cx, cy, r, 0.0, 2.0 * math.pi)
    _rgba(ctx, PIN_BG, fill_a)
    ctx.fill_preserve()
    ctx.set_line_width(max(1.0, 1.0 * sc))
    if dash:
        ctx.set_dash(dash, 0.0)
    _rgba(ctx, color, stroke_a)
    ctx.stroke()
    if dash:
        ctx.set_dash([], 0.0)


def draw_icon(ctx, k, color, cx, cy, sc=1.0, dim=1.0, alpha=1.0):
    """Kind icon inside the pin - maschine's 24x24 stroke set, verbatim path
    data (ICONS above; the sheet's exact glyphs, not approximations). Sheet
    fills (flag pennant, flare core, airdrop canopy, BTR hull) get the .25
    accent fill; strokes are 2px round. Locked state dims to .55. 'alpha'
    multiplies both (semi-transparent markers keep the icon legible because its
    weight in ALPHA_WEIGHTS is well below the plate's)."""
    prims = ICONS.get(k, ICONS["zone"])
    s = 15.0 * sc / 24.0
    ox, oy = cx - 12.0 * s, cy - 12.0 * s
    ctx.set_line_width(max(1.0, 2.0 * s))
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    ctx.set_line_join(cairo.LINE_JOIN_ROUND)
    r, g, b = _f(color)
    for pr in prims:
        _icon_prim(ctx, pr, ox, oy, s)
        if pr[-1]:
            ctx.set_source_rgba(r, g, b, 0.25 * dim * alpha)
            ctx.fill_preserve()
            ctx.set_source_rgba(r, g, b, dim * alpha)
            ctx.stroke()
        else:
            ctx.set_source_rgba(r, g, b, dim * alpha)
            ctx.stroke()

def _rgb(ctx, c):
    ctx.set_source_rgb(c[0] / 255.0, c[1] / 255.0, c[2] / 255.0)


def _rgba(ctx, c, a):
    """Color with an explicit layer alpha (0..1). Clamped: cairo clamps an
    out-of-range alpha silently, which would turn a bug into an invisible or
    half-opaque marker instead of a loud one."""
    ctx.set_source_rgba(c[0] / 255.0, c[1] / 255.0, c[2] / 255.0,
                        max(0.0, min(1.0, a)))


def layer_alpha(opacity, which):
    """Alpha of one marker layer for a given marker opacity.

    layer alpha = 1 - (1 - opacity) * ALPHA_WEIGHTS[which]: at opacity 1.0 every
    layer is fully opaque (byte-identical to the pre-transparency renderer), and
    each layer only gives up as much opacity as its weight allows.
    """
    o = _finite(opacity, 1.0)
    if o is None:
        o = 1.0
    o = max(0.0, min(1.0, o))
    w = ALPHA_WEIGHTS.get(which, 1.0)
    return 1.0 - (1.0 - o) * w


def _f(c):
    """cairo color floats 0..1 (set_source_rgba with 0-255 ints clamps to
    white - a silent bug, not an error)."""
    return tuple(v / 255.0 for v in c)


def _rounded_rect(ctx, x, y, w, h, r):
    ctx.new_path()
    ctx.move_to(x + r, y)
    ctx.line_to(x + w - r, y)
    ctx.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    ctx.line_to(x + w, y + h - r)
    ctx.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    ctx.line_to(x + r, y + h)
    ctx.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    ctx.line_to(x, y + r)
    ctx.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    ctx.close_path()


def teardrop(ctx, tipx, tipy, sc=1.0, border=None, fill_a=1.0, stroke_a=1.0):
    """DOM pin: 26 px square, border-radius 50% 50% 50% 0, rotate(-45deg)
    -> circle r=13 with a sharp tip; tip point is exactly (tipx, tipy).
    Border colored per kind (example.png look), falls back to gold."""
    pr = PIN_R * sc
    ccx, ccy = tipx, tipy - TIP_OFF * sc
    a1 = math.pi / 4
    ctx.new_path()
    ctx.move_to(tipx, tipy)
    ctx.line_to(ccx + pr * math.cos(a1), ccy + pr * math.sin(a1))
    ctx.arc_negative(ccx, ccy, pr, 3 * math.pi / 4, a1)
    ctx.close_path()
    _rgba(ctx, PIN_BG, fill_a)
    ctx.fill_preserve()
    ctx.set_line_width(max(1.0, round(sc)))
    _rgba(ctx, border if border is not None else PIN_BORDER, stroke_a)
    ctx.set_antialias(cairo.ANTIALIAS_NONE)
    ctx.stroke()
    ctx.set_antialias(cairo.ANTIALIAS_DEFAULT)


def render_marker(m):
    """DOM-faithful marker: teardrop pin with kind icon at its circle center,
    tagbox (label + dist) hanging BELOW the tip (original .tagbox sits at
    top:38px under the 31px pin), everything scaled by SC * m['sc'].
    Anchor: the pin TIP sits exactly at feed (x, y) (CSS translate(-50%,-100%)).
    Returns (surf, bx, by); the surface bottom includes the tagbox.
    None if invisible."""
    op = float(m.get("op", 1.0))
    if op <= 0.01:
        return None
    sz = _marker_sizes(m)
    # one opacity in, six layer alphas out (see ALPHA_WEIGHTS)
    plate_a = layer_alpha(op, "plate")
    pin_a = layer_alpha(op, "pin")
    border_a = layer_alpha(op, "border")
    icon_a = layer_alpha(op, "icon")
    label = str(m.get("l", ""))
    dist = str(m.get("d", ""))
    color = kind_color(m.get("k", "item"), m.get("s"))
    lsurf, lw, lbase = _text_surface(label, sz["label_font"], True, op)
    dsurf, dw, dbase = _text_surface(dist, sz["dist_font"], False, op)
    box_w = int(lw) + int(dw) + 2 * sz["pad_x"] + 2
    # box_h budgeted from the real text surfaces: label band + line gap +
    # dist band — descenders can never overflow the tagbox background
    box_h = int(math.ceil(sz["pad_y"] + lbase + sz["dist_font"] + sz["pad_y"]
                          + (dsurf.get_height() - dbase)))
    bw = max(sz["box_min"], box_w)
    bx = int(round(m["x"] - bw / 2.0))
    tipx = int(round(m["x"] - bx))
    _shape = PIN_SHAPE.get(m.get("k", ""), "teardrop")
    tipy = int(round(PIN_TOPS.get(_shape, PIN_TOPS["teardrop"]) * sz["pin"]))
    box_y = tipy + 1 + sz["gap"]
    by = int(round(m["y"] - tipy))
    bh = box_y + box_h
    bw = max(bw, int(math.ceil((2.0 * PIN_R * math.sqrt(2.0) + 4.0) * sz["pin"])))
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, bw, bh)
    sctx = cairo.Context(surf)
    sctx.set_operator(cairo.OPERATOR_SOURCE)
    sctx.set_source_rgba(0, 0, 0, 0)
    sctx.paint()
    sctx.set_operator(cairo.OPERATOR_OVER)
    # tagbox: dark bg, 1 px gold border, rounded corners — hangs BELOW the tip
    off = (bw - box_w) / 2.0
    rad = max(2, int(round(4 * sz["sc"])))
    _rounded_rect(sctx, off, box_y, box_w, box_h, rad)
    _rgba(sctx, BOX_BG, plate_a)
    sctx.fill()
    # crisp 1 px border: inset path by 0.5 with AA off -> exact PIN_BORDER
    # pixels at the box perimeter (AA strokes blend with fill/bg, never exact)
    sctx.set_antialias(cairo.ANTIALIAS_NONE)
    sctx.set_line_width(1.0)
    _rounded_rect(sctx, off + 0.5, box_y + 0.5, box_w - 1.0, box_h - 1.0, rad)
    _rgba(sctx, PIN_BORDER, border_a)
    sctx.stroke()
    sctx.set_antialias(cairo.ANTIALIAS_DEFAULT)
    # label bold + dist, centered as DOM does (pasted text surfaces,
    # identical to the cached path so both layouts match exactly)
    sctx.set_source_surface(lsurf, off + (box_w - lw) / 2.0, box_y + sz["pad_y"])
    sctx.paint()
    sctx.set_source_surface(dsurf, off + (box_w - dw) / 2.0,
                            box_y + sz["pad_y"] + lbase + sz["dist_font"]
                            + sz["pad_y"] - dbase)
    sctx.paint()
    # teardrop pin: tip anchored exactly at feed (x, y)
    locked = m.get("s") == "locked"
    dash = [3.0 * sz["pin"], 3.0 * sz["pin"]] if locked else None
    if _shape == "diamond":
        _pin_diamond(sctx, tipx, tipy, sz["pin"], color, dash, pin_a, border_a)
    elif _shape == "circle":
        _pin_circle(sctx, tipx, tipy, sz["pin"], color, dash, pin_a, border_a)
    else:
        teardrop(sctx, tipx, tipy, sz["pin"], color, pin_a, border_a)
    _cy = tipy - ((PIN_R if _shape == "circle" else TIP_OFF) * sz["pin"])
    draw_icon(sctx, m.get("k", "item"), color, tipx, _cy, sz["pin"],
              0.55 if locked else 1.0, icon_a)
    return surf, bx, by


EDGE_DIST_FACTOR = 0.7  # distance box font = 70 % of marker dist font (DOM edge)


def edge_dist_size(sc):
    return max(8, int(round(DIST_FONT * EDGE_DIST_FACTOR * SC * (sc if sc > 0.05 else 1.0))))


def make_edge_surface(e, W, H):
    """Arrow in kind color with gold border, pointing outward toward an
    off-screen marker, plus a small distance box under the arrow (example.png).
    e['sd'] ('L','R','T','B') gives the side; without it a quadrant heuristic
    is used (legacy feeds). Everything scaled by SC * e['sc'].
    Returns (surf, rx, ry): the surface can be wider than the arrow rect
    (distance box centered) and taller (box below); (rx, ry) is its anchor."""
    w, h = int(e["w"]), int(e["h"])
    color = kind_color(e.get("k", "item"), e.get("s"))
    sc = SC * float(e.get("sc", 1.0) or 1.0)
    if sc <= 0.05:
        sc = 1.0
    op = _finite(e.get("op"), 1.0)
    op = 1.0 if op is None else max(0.0, min(1.0, op))
    arrow_a = layer_alpha(op, "border")   # arrows stay solid-ish: a see-through
    border_a = layer_alpha(op, "border")  # border marker is hard to spot
    plate_a = layer_alpha(op, "plate")
    dist_a = layer_alpha(op, "dist")
    dist = str(e.get("d", "") or "")
    side = e.get("sd")
    box_w = box_h = 0
    dsz = 0
    if dist:
        dsz = edge_dist_size(sc)
        tmp = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
        tctx = cairo.Context(tmp)
        tctx.set_font_size(dsz)
        tctx.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        dw = int(tctx.text_extents(dist).width)
        px = max(3, int(round(PAD_X * sc)))
        py = max(1, int(round(PAD_Y * sc)))
        box_w = dw + 2 * px + 2
        box_h = dsz + 2 * py
    gap = max(2, int(round(GAP_PIN_BOX * sc))) if dist else 0
    surf_w = max(w, box_w)
    surf_h = h + gap + box_h
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, surf_w, surf_h)
    ctx = cairo.Context(surf)
    ctx.set_operator(cairo.OPERATOR_SOURCE)
    ctx.set_source_rgba(0, 0, 0, 0)
    ctx.paint()
    ctx.set_operator(cairo.OPERATOR_OVER)
    ax = (surf_w - w) / 2.0     # arrow horizontally centered in the surface
    cx = e["x"] + w / 2.0
    cy = e["y"] + h / 2.0
    if side is None:
        side = ("L" if cx < W * 0.25 else "R" if cx > W * 0.75
                else "B" if cy > H * 0.75 else "T")
    if side == "L":
        pts = [(ax, h / 2.0), (ax + w - 1, 0), (ax + w - 1, h - 1)]
    elif side == "R":
        pts = [(ax + w - 1, h / 2.0), (ax, 0), (ax, h - 1)]
    elif side == "B":
        pts = [(ax + w / 2.0, h - 1), (ax, 0), (ax + w - 1, 0)]
    else:  # 'T'
        pts = [(ax + w / 2.0, 0), (ax, h - 1), (ax + w - 1, h - 1)]
    ctx.move_to(*pts[0])
    ctx.line_to(*pts[1])
    ctx.line_to(*pts[2])
    ctx.close_path()
    _rgba(ctx, color, arrow_a)
    ctx.fill_preserve()
    ctx.set_line_width(1.0)
    ctx.set_antialias(cairo.ANTIALIAS_NONE)
    _rgba(ctx, PIN_BORDER, border_a)
    ctx.stroke()
    ctx.set_antialias(cairo.ANTIALIAS_DEFAULT)
    if dist:
        bxx = (surf_w - box_w) / 2.0
        byy = h + gap
        rad = max(2, int(round(4 * sc)))
        _rounded_rect(ctx, bxx, byy, box_w, box_h, rad)
        _rgba(ctx, BOX_BG, plate_a)
        ctx.fill()
        ctx.set_antialias(cairo.ANTIALIAS_NONE)
        ctx.set_line_width(1.0)
        _rounded_rect(ctx, bxx + 0.5, byy + 0.5, box_w - 1.0, box_h - 1.0, rad)
        _rgba(ctx, PIN_BORDER, border_a)
        ctx.stroke()
        ctx.set_antialias(cairo.ANTIALIAS_DEFAULT)
        ctx.set_font_size(dsz)
        ctx.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        dt = ctx.text_extents(dist)
        _rgba(ctx, DIST_COLOR, dist_a)
        ctx.move_to(bxx + (box_w - dt.width) / 2.0,
                    byy + max(1, int(round(PAD_Y * sc))) + ctx.font_extents()[0])
        ctx.show_text(dist)
    rx = int(e["x"]) - (surf_w - w) // 2
    ry = int(e["y"])
    return surf, rx, ry


def parse_geometry(s):
    import re
    m = re.match(r"^(\d+)x(\d+)([+-]\d+)([+-]\d+)$", s)
    if not m:
        sys.exit(f"bad geometry: {s}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))


def read_feed(path):
    """Read latest.json; None on missing file AND on stale content: the feed
    writer touches the file every frame, so content older than 1 s means the
    game/plugin stopped — treated as feed gone (clear overlay after 1 s)."""
    try:
        with open(path) as f:
            data = json.load(f)
        if time.time() - os.path.getmtime(path) > 1.0:
            return None
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def smooth_pos(state, key, x, y, tau):
    """Delayed chord interpolation (fixes the stutter on slow-moving markers).

    The plugin samples in the game's frame loop: at 30 Hz game FPS the feed
    is a staircase even though the true motion is perfectly smooth (proven
    with --trace on real Shoreline data: raw projection steps <=1 px, zero
    presence gaps, cadence 33 ms). An EMA re-accelerates towards each stair
    step and micro-stops before the next one: DISPLAY velocity pulses
    (measured 0.47-0.65 median, up to 3.5x true speed at 26-56 m) - the
    visible zapping/zappeln.

    This smoother instead interpolates the LAST completed measurement chord
    in real time: the display position trails the newest measurement by
    exactly one measurement interval (a constant, invisible 33 ms at 30 Hz),
    but moves with constant continuous velocity at 120 Hz render rate -
    no micro-stops, no rubberbanding, no velocity extrapolation: the
    displayed position is always on a chord between two REAL measurements
    (a stationary marker is rock-steady by construction).
    Long feed gaps (>300 ms) snap instead of crawling. tau > 0 adds a small
    (<=20 ms) seam blend softening velocity changes at chord boundaries;
    tau <= 0 is pure interpolation.
    """
    now = time.monotonic()
    prev = state.get(key)
    if prev is None:
        # [m1x, m1y, t1, m2x, m2y, t2, disp_x, disp_y, t_render_last]
        state[key] = [x, y, now, x, y, now, x, y, now]
        return x, y
    m1x, m1y, t1, m2x, m2y, t2, dx, dy, tprev = prev
    if x != m2x or y != m2y:
        if now - t2 > 0.30:
            # feed gap: snap the whole smoother to the new position instead
            # of crawling across the gap or interpolating a degenerate chord
            state[key] = [x, y, now, x, y, now, x, y, now]
            return x, y
        m1x, m1y, t1 = m2x, m2y, t2
        m2x, m2y, t2 = x, y, now
    dtn = max(t2 - t1, 1e-4)
    u = (now - t1) / dtn - 1.0               # delayed by one measurement step
    if u < 0.0:
        u = 0.0
    elif u > 1.0:
        u = 1.0
    tx = m1x + (m2x - m1x) * u
    ty = m1y + (m2y - m1y) * u
    if tau > 0.0:
        # tau (config 'Smoothing') IS the seam-blend time: it softens the
        # velocity change at chord boundaries; at speed it is capped so the
        # blend never trails the chord by more than ~6 px. tau = 0 -> pure
        # interpolation. (A hard 20 ms cap used to shadow the configured
        # value completely -> the config knob did nothing.)
        vchord = math.hypot(m2x - m1x, m2y - m1y) / dtn
        tb = min(tau, 6.0 / max(vchord, 1e-6))
        a = 1.0 - math.exp(-max(now - tprev, 1e-4) / tb)
        dx += (tx - dx) * a
        dy += (ty - dy) * a
    else:
        dx, dy = tx, ty
    state[key] = [m1x, m1y, t1, m2x, m2y, t2, dx, dy, now]
    return dx, dy
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "logs", "overlay.log")


class _Tee:
    """Mirror stdout into logs/overlay.log so 'Show log window' terminals
    always have content regardless of how the overlay was started."""

    def __init__(self, stream, path):
        self.stream = stream
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # rotation with ONE generation at startup: overlay.log must not
            # grow without bound across runs (security review LOW finding)
            try:
                if os.path.getsize(path) > LOG_ROTATE_BYTES:
                    os.replace(path, path + ".1")
            except OSError:
                pass
            self.fh = open(path, "a", buffering=1, encoding="utf-8")
        except Exception:
            self.fh = None

    def write(self, s):
        try:
            self.stream.write(s)
        except Exception:
            pass
        if self.fh is not None:
            try:
                self.fh.write(s)
            except Exception:
                pass

    def flush(self):
        for fh in (self.stream, self.fh):
            if fh is None:
                continue
            try:
                fh.flush()
            except Exception:
                pass


_EDGE_CACHE = {}
_TEXT_CACHE = {}
_BOX_CACHE = {}
_PIN_CACHE = {}


def _text_surface(text, size, bold, opacity=1.0):
    """Rendered text surface with cache: glyphs rasterize once per string.
    Returns (surf, advance, ascent); baseline sits at y=ascent in the surf.

    'opacity' maps through layer_alpha (bold text is always the quest label, so
    the tier follows 'label'/'dist'): quest labels stay fully readable at any
    opacity, distance text gives up only a little. The rounded alpha is part of
    the cache key - a changed opacity can never hand back a surface that was
    rendered for another one."""
    a = round(layer_alpha(opacity, "label" if bold else "dist"), 3)
    key = (text, size, bold, a)
    hit = _TEXT_CACHE.get(key)
    if hit is not None:
        return hit
    if len(_TEXT_CACHE) > 2000:
        _TEXT_CACHE.clear()
    probe = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1))
    probe.select_font_face(FONT, cairo.FONT_SLANT_NORMAL,
                           cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
    probe.set_font_size(size)
    ext = probe.text_extents(text)
    asc, desk = probe.font_extents()[0], probe.font_extents()[2]
    w = max(1, int(math.ceil(ext.x_advance)))
    h = max(1, int(math.ceil(asc + desk + ext.y_bearing + 2)))
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    sctx = cairo.Context(surf)
    sctx.set_operator(cairo.OPERATOR_SOURCE)
    sctx.set_source_rgba(0, 0, 0, 0)
    sctx.paint()
    sctx.set_operator(cairo.OPERATOR_OVER)
    sctx.select_font_face(FONT, cairo.FONT_SLANT_NORMAL,
                          cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
    sctx.set_font_size(size)
    _rgba(sctx, LABEL_COLOR if bold else DIST_COLOR, a)
    sctx.move_to(0, asc)
    sctx.show_text(text)
    hit = (surf, ext.width, asc)
    _TEXT_CACHE[key] = hit
    return hit


def _box_bg(box_w, box_h, radius=4, opacity=1.0):
    """Tagbox background (rounded rect + crisp 1 px kind-color border), cached
    per (width, height, radius, alpha buckets) — same math as render_marker.

    The border keeps a much higher alpha than the plate (ALPHA_WEIGHTS): a
    see-through tagbox is only readable while its outline still separates it
    from the map behind it."""
    plate_a = round(layer_alpha(opacity, "plate"), 3)
    border_a = round(layer_alpha(opacity, "border"), 3)
    key = (box_w, box_h, radius, plate_a, border_a)
    surf = _BOX_CACHE.get(key)
    if surf is None:
        if len(_BOX_CACHE) > 300:
            _BOX_CACHE.clear()
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, box_w, box_h)
        sctx = cairo.Context(surf)
        sctx.set_operator(cairo.OPERATOR_SOURCE)
        sctx.set_source_rgba(0, 0, 0, 0)
        sctx.paint()
        sctx.set_operator(cairo.OPERATOR_OVER)
        _rounded_rect(sctx, 0.0, 0.0, box_w, box_h, radius)
        _rgba(sctx, BOX_BG, plate_a)
        sctx.fill()
        sctx.set_antialias(cairo.ANTIALIAS_NONE)
        sctx.set_line_width(1.0)
        _rounded_rect(sctx, 0.5, 0.5, box_w - 1.0, box_h - 1.0, 4)
        _rgba(sctx, PIN_BORDER, border_a)
        sctx.stroke()
        sctx.set_antialias(cairo.ANTIALIAS_DEFAULT)
        _BOX_CACHE[key] = surf
    return surf


def _pin_surf(kind, sprite, tipx, tipy, bw, msz=None, opacity=1.0):
    """Pin (kind-colored outline + icon), cached per (kind, sprite, tip offset,
    width, scale bucket, alpha buckets); tip painted at (tipx, tipy) inside the
    surface. Opacity is quantised into the key so cache hits survive a static
    opacity but never return a surface built for a different one."""
    msz = msz or _marker_sizes({})
    sc = msz["sc"]
    pin = msz.get("pin", sc)
    pin_a = round(layer_alpha(opacity, "pin"), 3)
    border_a = round(layer_alpha(opacity, "border"), 3)
    icon_a = round(layer_alpha(opacity, "icon"), 3)
    key = (kind, sprite, tipx, tipy, bw, round(sc, 3), round(pin, 3),
           pin_a, border_a, icon_a)
    surf = _PIN_CACHE.get(key)
    if surf is None:
        if len(_PIN_CACHE) > 400:
            _PIN_CACHE.clear()
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, bw, tipy + 1)
        sctx = cairo.Context(surf)
        sctx.set_operator(cairo.OPERATOR_SOURCE)
        sctx.set_source_rgba(0, 0, 0, 0)
        sctx.paint()
        sctx.set_operator(cairo.OPERATOR_OVER)
        color = kind_color(kind, sprite)
        shape = PIN_SHAPE.get(kind, "teardrop")
        dash = [3.0 * pin, 3.0 * pin] if sprite == "locked" else None
        if shape == "diamond":
            _pin_diamond(sctx, tipx, tipy, pin, color, dash, pin_a, border_a)
        elif shape == "circle":
            _pin_circle(sctx, tipx, tipy, pin, color, dash, pin_a, border_a)
        else:
            teardrop(sctx, tipx, tipy, pin, color, pin_a, border_a)
        cy = tipy - (PIN_R * pin if shape == "circle" else TIP_OFF * pin)
        draw_icon(sctx, kind, color, tipx, cy, pin,
                  0.55 if sprite == "locked" else 1.0, icon_a)
        _PIN_CACHE[key] = surf
    return surf


def render_marker_cached(m):
    """Re-composite one marker from cached parts; returns (surf, rect_x, rect_y).

    Sub-pixel anchoring: the presenter's rect grid is integer-aligned, so the
    fractional part of the position is rendered INSIDE the surface as a
    fractional content offset - rect origin = floor(anchor) - 1, content drawn
    at (frac + 1). The perceived tip lands exactly on the float anchor.
    Without this, a marker crawling at 0.5 px/frame renders as a 1 px
    dwell/jump staircase: that quantization ripple becomes visible zapping on
    near markers (parallax velocity grows as 1/distance) and is invisible far
    away. Text glyphs, box background and pin+icon stay cached
    (_text_surface/_box_bg/_pin_surf); only the tiny final compositing runs
    per frame, and the tip slot is now fixed (bw // 2), which makes the pin
    cache hit even while the marker moves.
    """
    x, y = m["x"], m["y"]
    op = float(m.get("op", 1.0))
    if op <= 0.01:
        return None, 0, 0
    label = str(m.get("l", ""))
    dist = str(m.get("d", ""))
    kind = m.get("k", "item")
    sprite = m.get("s")
    msz = _marker_sizes(m)
    lsurf, lw, lbase = _text_surface(label, msz["label_font"], True, op)
    dsurf, dw, dbase = _text_surface(dist, msz["dist_font"], False, op)
    box_w = int(lw) + int(dw) + 2 * msz["pad_x"] + 2
    box_h = int(math.ceil(msz["pad_y"] + lbase + msz["dist_font"] + msz["pad_y"]
                          + (dsurf.get_height() - dbase)))
    bw = max(msz["box_min"], box_w,
             int(math.ceil((2.0 * PIN_R * math.sqrt(2.0) + 4.0) * msz["pin"]))) + 2
    tipx = bw // 2
    tipy = int(round(PIN_TOPS.get(PIN_SHAPE.get(m["k"], "teardrop"),
                                  PIN_TOPS["teardrop"]) * msz["pin"]))
    box_y = tipy + 1 + msz["gap"]
    bh = box_y + box_h + 2
    ax, ay = x - tipx, y - tipy
    rx, ry = int(math.floor(ax)) - 1, int(math.floor(ay)) - 1
    fx, fy = ax - rx - 1.0, ay - ry - 1.0
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, bw, bh)
    sctx = cairo.Context(surf)
    sctx.set_operator(cairo.OPERATOR_SOURCE)
    sctx.set_source_rgba(0, 0, 0, 0)
    sctx.paint()
    sctx.set_operator(cairo.OPERATOR_OVER)
    sctx.set_source_surface(_pin_surf(kind, sprite, tipx, tipy, bw, msz, op),
                            1.0 + fx, 1.0 + fy)
    sctx.paint()
    off = (bw - box_w) / 2.0
    sctx.set_source_surface(_box_bg(box_w, box_h, msz["radius"], op),
                            off + 1.0 + fx, 1.0 + fy + box_y)
    sctx.paint()
    sctx.set_source_surface(lsurf, off + (box_w - lw) / 2.0 + 1.0 + fx,
                            1.0 + fy + box_y + msz["pad_y"])
    sctx.paint()
    sctx.set_source_surface(dsurf, off + (box_w - dw) / 2.0 + 1.0 + fx,
                            1.0 + fy + box_y + msz["pad_y"] + lbase
                            + msz["dist_font"] + msz["pad_y"] - dbase)
    sctx.paint()
    return surf, rx, ry


def make_edge_surface_cached(e, W, H):
    """make_edge_surface with cache: arrow geometry depends only on
    (kind, state, distance text, size, side, viewport, opacity)."""
    key = (e.get("k", "item"), e.get("s"), e.get("sd"), str(e.get("d", "")),
           int(e["w"]), int(e["h"]), round(float(e.get("op", 1.0)), 3), W, H)
    surf = _EDGE_CACHE.get(key)
    if surf is None:
        if len(_EDGE_CACHE) > 300:
            _EDGE_CACHE.clear()
        surf = make_edge_surface(e, W, H)
        _EDGE_CACHE[key] = surf
    return surf


def _edge_arrow(ed, cfg, vw, vh):
    """Edge arrow derived from an off-screen marker (never a marker in the
    middle of the screen): clamped away from the border, kind-colored, with
    distance text like the original mod."""
    sc = SC * cfg["scale"]
    if (cfg["farScale"] < 1.0 and ed["meters"] is not None
            and ed["meters"] > cfg["farDistance"]):
        sc *= cfg["farScale"]
    nx, ny = float(ed.get("nx", 0.5)), float(ed.get("ny", 0.5))
    ax, ay = nx * vw, ny * vh
    if nx < 0.0:
        side = "L"
    elif nx > 1.0:
        side = "R"
    elif ny < 0.0:
        side = "T"
    elif ny > 1.0:
        side = "B"
    elif abs(nx - 0.5) >= abs(ny - 0.5):
        side = "L" if nx < 0.5 else "R"
    else:
        side = "T" if ny < 0.5 else "B"
    w = max(12, int(round(cfg["edgeSize"] * sc)))
    h = max(10, int(round(w * 5.0 / 6.0)))
    margin = max(10, min(EDGE_MAX_DIST, int(round(EDGE_MAX_DIST * sc))))
    if side in ("L", "R"):
        cx = margin if side == "L" else vw - margin
        cy = min(max(ay, h), vh - h)
    else:
        cx = min(max(ax, w), vw - w)
        cy = margin if side == "T" else vh - margin
    e = {"k": ed.get("k", "item"), "s": ed.get("s"),
         "x": int(round(cx - w / 2.0)), "y": int(round(cy - h / 2.0)),
         "w": w, "h": h, "op": float(ed.get("op", 1.0)), "sd": side,
         "d": fmt_dist(ed["meters"], cfg) if cfg["distance"] else ""}
    return make_edge_surface(e, vw, vh)


_ERRS = [0]  # shared render-error counter (first 5 logged, rest suppressed)


def _render_failed(where, exc, count):
    """One broken marker/frame must never kill the whole feed (user: 'feeder
    crash under certain conditions'): skip the bad unit, log the first few."""
    if count[0] <= 5:
        print(f"[warn] {where} render failed: {exc!r}"
              + (" (more suppressed)" if count[0] == 5 else ""))
    count[0] += 1


def frame_rects(feed, smooth=0.0, state=None, fresh=True, view=None):
    """One feed frame as dirty rects (x, y, surface) — no full-frame canvas.

    Feed protocol v2: positions normalized (nx/ny, 0..1; they may exceed the
    viewport, b=1 => behind the player), distances in meters. Legacy feeds
    (x/y pixels, string distances) keep working. Off-screen markers never
    render in the middle of the screen — cfg["edge"] turns them into edge
    arrows derived here (not fed from the game side). cfg controls features:
    scale (cfg["scale"], beyond cfg["farDistance"] also cfg["farScale"]),
    label/distance text, meters/km vs feet/miles (cfg["imperial"]). Rects
    are ordered like the original mod: extracts at the bottom, quests on top.

    The presenter blits these rects into its shm buffer and clears exactly the
    rects committed two frames ago, so only marker pixels travel through the
    pipe (~2 MB/frame instead of 14 MB at 2560x1440).
    """
    cfg = cfg_of(feed)
    fc = feed.get("cfg") or {}
    if "smooth" in fc:
        # in-game 'Smoothing' config applies live every frame
        smooth = float(cfg["smooth"])
    try:
        vw, vh = feed.get("v", [2560, 1440])
        vw, vh = float(vw), float(vh)
        if not (math.isfinite(vw) and math.isfinite(vh) and vw >= 1.0 and vh >= 1.0):
            raise ValueError
    except (TypeError, ValueError):
        vw, vh = 2560.0, 1440.0   # defense in depth (sanitize already guards)
    if view is not None:
        # nx/ny are fractions of the VIEWPORT; when KWin fractional scaling
        # resizes the layer surface (2560x1440 feed -> 1707x960 surface),
        # mapping them onto the real surface keeps markers aligned. Only do
        # it for same-aspect resizes (the scaling case); a rotated display
        # would need a rotation transform we deliberately do not fake.
        sw, sh = float(view[0]), float(view[1])
        if sw >= 1.0 and sh >= 1.0:
            if abs(sw / sh - vw / vh) <= 0.05 * max(vw / vh, 1.0):
                vw, vh = sw, sh
            else:
                global _ASPECT_WARNED
                if not _ASPECT_WARNED:
                    _ASPECT_WARNED = True
                    print(f"[warn] display aspect {sw:.0f}x{sh:.0f} differs from game view "
                          f"{vw:.0f}x{vh:.0f} (rotated display?) - marker mapping is best-effort")
    placed = []
    edges = []
    for m in feed.get("m", []):
        if len(placed) >= MAX_RECTS:
            break
        # Marker opacity: the wire value ("op" = the plugin's in-game Marker
        # Opacity, default 1.0) times the overlay-side cfg. The renderer turns
        # this single scalar into per-layer alphas (ALPHA_WEIGHTS).
        op = _finite(m.get("op"), 1.0) * _finite(cfg["markerAlpha"], 1.0)
        op = max(0.0, min(1.0, op))
        if op <= 0.01:
            continue
        meters = parse_meters(m.get("d"))
        nx, ny = m.get("nx"), m.get("ny")
        if nx is None or ny is None:
            nx, ny = float(m.get("x", 0)) / vw, float(m.get("y", 0)) / vh
        else:
            nx, ny = float(nx), float(ny)
        behind = m.get("b")
        if behind is None:
            behind = 1 if (nx < 0.0 or nx > 1.0 or ny < 0.0 or ny > 1.0) else 0
        key = (m.get("k"), m.get("l"), m.get("s"))
        # Off-screen / behind handling BEFORE any smoothing: the plugin
        # mirrors behind-camera projections (b=1), and side-plane NaN
        # fallbacks can emit wildly out-of-range b=0 coordinates. Neither
        # may ever enter the centered marker path: a mirrored coordinate
        # drawn mid-screen reads as a marker "in front, mirrored" (#3), and
        # smoothing mirrored coords into a real front position produced
        # cross-screen chord shots on behind->front transitions (#2).
        # Within 10 % overflow a marker is still legitimately clipped at the
        # screen border (faithful DOM behavior); beyond that it is off-screen.
        far_off = (nx < -0.1 or nx > 1.1 or ny < -0.1 or ny > 1.1)
        if int(behind) or far_off:
            if state is not None:
                # re-entry into view starts fresh at the real position
                state.pop(key, None)
            if cfg["edge"]:
                edges.append({"k": m.get("k"), "s": m.get("s"), "nx": nx,
                              "ny": ny, "meters": meters, "op": op})
            continue
        x, y = nx * vw, ny * vh
        if smooth > 0.0 and state is not None:
            x, y = smooth_pos(state, key, x, y, smooth)
        sc = cfg["scale"]
        if (cfg["farScale"] < 1.0 and meters is not None
                and meters > cfg["farDistance"]):
            sc *= cfg["farScale"]
        md = {"k": m.get("k"), "s": m.get("s"), "x": x, "y": y, "op": op,
              "sc": sc, "ic": cfg["iconScale"],
              "l": str(m.get("l", "")) if cfg["labels"] else "",
              "d": fmt_dist(meters, cfg) if cfg["distance"] else ""}
        try:
            surf, rx, ry = render_marker_cached(md)
        except Exception as exc:
            _render_failed("marker", exc, _ERRS)
            continue
        if surf is None:
            continue
        # anchor: the exact float tip (x, y) is realized INSIDE
        # render_marker_cached (int rect origin + fractional content offset);
        # frame_rects only orders and passes rects on
        placed.append((Z_ORDER.get(m.get("k"), 3),
                       -(meters if meters is not None else 1e12), rx, ry, surf))
    if state is not None:
        live = {(m.get("k"), m.get("l"), m.get("s")) for m in feed.get("m", [])}
        for key in list(state):
            if key not in live:
                del state[key]
    # draw order: kind Z first (extracts bottom, quests top), and within a Z
    # level far markers first so the CLOSEST marker wins visibility on overlap
    rects = [p[2:] for p in sorted(placed, key=lambda p: (p[0], p[1]))]
    for ed in edges:
        try:
            surf, rx, ry = _edge_arrow(ed, cfg, vw, vh)
            rects.append((rx, ry, surf))
        except Exception as exc:
            _render_failed("edge arrow", exc, _ERRS)
    for e in feed.get("e", []):
        if len(rects) >= MAX_RECTS:
            break
        if e.get("op", 1.0) <= 0:
            continue
        try:
            surf = make_edge_surface_cached(e, vw, vh)
            rects.append((int(e["x"]), int(e["y"]), surf))
        except Exception as exc:
            _render_failed("edge", exc, _ERRS)
    merged = merge_overlapping(rects)
    if len(merged) > MAX_RECTS:
        if _san_warn["rects"] < 3:
            _san_warn["rects"] += 1
            print(f"frame_rects: {len(merged)} rects > {MAX_RECTS} -> truncated")
        merged = merged[:MAX_RECTS]
    # The presenter accepts rect payloads only up to the surface itself
    # (anything above was injected, never legitimately rendered). Split
    # oversized merged clusters into row-aligned surface slices instead of
    # only warning about them - the old 'break' merely ended the warning
    # loop while the oversized rect still went out and desynced the pipe.
    cap_px = max(1, int(vw) * int(vh))
    total_cap = 2 * cap_px  # sum over all rects of one frame (presenter cap)
    final = []
    for (mx, my, msurf) in merged:
        w, h = msurf.get_width(), msurf.get_height()
        if w * h > 1_500_000:
            if _san_warn.get("bigrect", 0) < 3:
                _san_warn["bigrect"] = _san_warn.get("bigrect", 0) + 1
                print(f"frame_rects: merged rect {mx},{my} {w}x{h}"
                      " > 1.5 MPixel - chained cluster, split for presenter")
        pieces = ([(mx, my, msurf)] if w * h <= cap_px
                  else split_surface(mx, my, msurf, cap_px))
        for piece in pieces:
            pw, ph = piece[2].get_width(), piece[2].get_height()
            if pw * ph > total_cap:
                if _san_warn.get("bigrect", 0) < 3:
                    _san_warn["bigrect"] = _san_warn.get("bigrect", 0) + 1
                    print("frame_rects: frame rect total exceeds presenter cap"
                          " - farther rects dropped")
                break
            total_cap -= pw * ph
            final.append(piece)
    return final


def split_surface(mx, my, surf, cap_px):
    """Slice a merged surface into row-aligned sub-surfaces of at most
    cap_px pixels each (pure row copies, no scaling, no recompression)."""
    w, h = surf.get_width(), surf.get_height()
    stride = surf.get_stride()
    data = surf.get_data()
    rows = max(1, cap_px // max(1, w))
    pieces = []
    off = 0
    while off < h:
        hh = min(rows, h - off)
        sub = cairo.ImageSurface.create_for_data(
            bytearray(data[off * stride:(off + hh) * stride]),
            cairo.FORMAT_ARGB32, w, hh, stride)
        pieces.append((mx, my + off, sub))
        off += hh
    return pieces


def merge_overlapping(rects):
    """Cluster rects that genuinely overlap and composite each cluster into one
    surface.

    The presenter memcpy-copies rects - it never blends - so two overlapping
    semi-transparent marker surfaces would each paste its whole rectangle
    footprint and occlude the other's interior with rectangular translucent
    patches (the visible artifact on dense marker clusters). Compositing once
    on the Python side with correct OVER ordering makes the frame's rects
    pairwise non-overlapping; the memcpy is then pixel-exact. Non-overlapping
    markers (the common case) pass through untouched."""
    n = len(rects)
    if n < 2:
        return list(rects)
    box = [(x, y, x + s.get_width(), y + s.get_height())
           for x, y, s in rects]
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    merged = False
    for i in range(n):
        for j in range(i + 1, n):
            a, b = box[i], box[j]
            if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                ra, rb = find(i), find(j)
                if ra != rb:
                    parent[rb] = ra
                    merged = True
    if not merged:
        return list(rects)
    comps = {}
    for i in range(n):
        comps.setdefault(find(i), []).append(i)
    out = []
    for members in comps.values():
        if len(members) == 1:
            out.append(rects[members[0]])
            continue
        x0 = min(box[i][0] for i in members)
        y0 = min(box[i][1] for i in members)
        x1 = max(box[i][2] for i in members)
        y1 = max(box[i][3] for i in members)
        canvas = cairo.ImageSurface(cairo.FORMAT_ARGB32, x1 - x0, y1 - y0)
        ctx = cairo.Context(canvas)
        for i in members:  # original draw order: far first, closest last
            x, y, surf = rects[i]
            ctx.save()
            ctx.set_source_surface(surf, x - x0, y - y0)
            ctx.paint()
            ctx.restore()
        out.append((x0, y0, canvas))
    # canvases of different components may still bbox-overlap although their
    # members do not -> merge again (each pass strictly reduces the count)
    return merge_overlapping(out) if len(out) < n else out


def encode_frame(rects):
    """Wire format for the presenter: 'F' + uint32 n + n rects, each as
    (i32 x, y, w, h, stride + raw ARGB rows). 'C' = clear frame."""
    parts = [b"F", struct.pack("<I", len(rects))]
    for x, y, surf in rects:
        stride = surf.get_stride()
        parts.append(struct.pack("<5i", x, y, surf.get_width(),
                                 surf.get_height(), stride))
        parts.append(surf.get_data())
    return b"".join(parts)


def make_feed_provider(path, trace_path=None, feed_token=""):
    """Returns a callable that yields the newest feed dict (None = feed gone).

    File mode: poll latest.json with the read_feed freshness rule.
    ws:// mode: a daemon thread runs an asyncio pump; the newest frame is
    kept in state and treated as gone when the writer is silent > 1 s.
    trace_path: log EVERY arriving raw frame (arrival wall time + feed time
    + all markers) as JSON lines - the ground-truth tap for diagnosing feed
    jitter (presence gaps, alternating projections, send cadence).
    feed_token (ws:// only, protocol v4): the server first sends
    {"c":"chal","n":<nonce>,"p":<HMAC-SHA256(key=token,msg="server|"+n)>}; the
    overlay VERIFIES p (proves the real plugin - a process squatting the port
    cannot forge it) and answers {"c":"auth","r":<HMAC over "client|"+n>}, a
    separately derived value (proves it knows the token; echoing the server's
    own p back does not authenticate). The token itself is never sent. Every
    frame additionally carries a "tok" stamp = HMAC-SHA256(token, its "t"
    timestamp), compared in constant time; a missing or mismatching stamp
    drops the frame, so a frame cannot be replayed under another timestamp.
    Without a token there is no challenge and any local process dictating the
    port also dictates the display (--insecure is the only way there). File
    feeds (dev tools) are local-only and not authenticated.
    Token changes: the plugin reads its cfg at GAME start, the overlay reads
    config.json at OVERLAY start - change a token with the game closed, then
    start the game first, then the overlay (never the other way around).
    """
    trace_fh = None
    trace_state = {"bytes": 0, "noted": False}
    if trace_path:
        os.makedirs(os.path.dirname(trace_path) or ".", exist_ok=True)
        trace_fh = open(trace_path, "w", buffering=1)
        print(f"feed trace -> {trace_path} (cap {TRACE_MAX_BYTES >> 20} MiB)")

    def trace(raw):
        if trace_fh is None:
            return
        try:
            if trace_state["bytes"] >= TRACE_MAX_BYTES:
                if not trace_state["noted"]:
                    trace_state["noted"] = True
                    print("feed trace: size cap reached, tracing stopped")
                return
            d = raw if isinstance(raw, dict) else json.loads(raw)
            line = json.dumps(
                {"lt": round(time.time(), 4), "t": d.get("t"),
                 "m": d.get("m")}, separators=(",", ":")) + "\n"
            trace_state["bytes"] += len(line)
            trace_fh.write(line)
        except Exception:
            pass

    if not path.startswith("ws://"):
        last_trace = [0.0]

        def read_traced():
            feed = read_feed(path)
            now = time.monotonic()
            if feed is not None and now - last_trace[0] > 0.02:
                last_trace[0] = now
                trace(feed)
            return sanitize_feed(feed) if feed is not None else None
        return read_traced
    import threading
    state = {"feed": None, "ts": 0.0}

    def pump():
        import asyncio
        import websockets

        async def run():
            while True:
                try:
                    # close_timeout is mandatory: websockets' default (None) hangs the
                    # closing handshake forever when the server keeps pushing frames
                    # (observed: pump froze after the unauthenticated-server break).
                    async with websockets.connect(path, max_size = 1 << 20,
                                                  close_timeout=1.0) as ws:
                        if feed_token:
                            # Protocol v3: the server presents itself FIRST with an
                            # HMAC over a fresh nonce. Only if that verifies does the
                            # overlay answer with its own proof - the token itself is
                            # never sent in either direction. A squatter on the port
                            # cannot present the proof and is cut off before it
                            # injects a single frame.
                            chal = json.loads(await asyncio.wait_for(
                                ws.recv(), timeout=10.0))
                            if not (isinstance(chal, dict) and chal.get("c") == "chal"
                                    and isinstance(chal.get("n"), str)
                                    and isinstance(chal.get("p"), str)):
                                raise RuntimeError(
                                    "server sent no token challenge - not a "
                                    "QuestMarkersLinux 1.1+ plugin feed (or a process "
                                    "that cannot prove the feed token)")
                            expect = hmac.new(feed_token.encode("utf-8"),
                                              ("server|" + chal["n"]).encode("utf-8"),
                                              hashlib.sha256).hexdigest()
                            if not hmac.compare_digest(expect, chal["p"]):
                                raise RuntimeError(
                                    "server could NOT prove knowledge of the feed "
                                    "token - refusing the feed (plugin Feed Token vs. "
                                    "config.json feedToken: same value? game was "
                                    "restarted after the token change?)")
                            # Domain-separated answer: echoing chal["p"] back would
                            # authenticate ANY local process that can connect.
                            answer = hmac.new(feed_token.encode("utf-8"),
                                              ("client|" + chal["n"]).encode("utf-8"),
                                              hashlib.sha256).hexdigest()
                            await ws.send(json.dumps({"c": "auth", "r": answer},
                                                     separators=(",", ":")))
                            print(f"ws feed authenticated by challenge: "
                                  f"{_mask_url(path, feed_token)}")
                        else:
                            print(f"ws feed UNAUTHENTICATED (no feed token): "
                                  f"{_mask_url(path, feed_token)} - only acceptable "
                                  "against a plugin with an empty Feed Token")
                        auth_fails = 0
                        bad_frames = 0
                        async for msg in ws:
                            try:
                                frame = json.loads(msg)
                            except json.JSONDecodeError:
                                # Malformed frames used to be dropped in total
                                # silence: the socket stayed up and every frame
                                # was discarded, so markers froze on screen
                                # until the 1 s staleness rule cleared them.
                                # Name it once, then every ~10 s at 60 Hz.
                                bad_frames += 1
                                if bad_frames == 1 or bad_frames % 600 == 0:
                                    print(f"ws feed: malformed frame ignored "
                                          f"({len(msg)} bytes, not JSON, "
                                          f"{bad_frames} so far) - the plugin "
                                          f"is sending incomplete frames; markers "
                                          f"stay frozen until a valid frame or "
                                          f"the 1 s timeout")
                                continue
                            tok = frame.get("tok") if isinstance(frame, dict) else None
                            want = None
                            if feed_token:
                                ts = frame.get("t") if isinstance(frame, dict) else None
                                if isinstance(ts, int) and not isinstance(ts, bool):
                                    # Frame stamp = HMAC over the frame's own
                                    # timestamp. Fail CLOSED: a frame without a
                                    # verifiable stamp is rejected, not trusted.
                                    want = hmac.new(
                                        feed_token.encode("utf-8"),
                                        str(ts).encode("utf-8"),
                                        hashlib.sha256).hexdigest()
                            if feed_token and (want is None or not isinstance(tok, str)
                                               or not hmac.compare_digest(want, tok)):
                                auth_fails += 1
                                if auth_fails == 1:
                                    print("ws feed: frame tok stamp missing/mismatched "
                                          "REJECTED (token drift between plugin cfg and "
                                          "config.json, or the plugin is older than 1.1: "
                                          "token changes take effect only after "
                                          "restarting the GAME first, then the overlay)")
                                if auth_fails >= 3:
                                    print("ws feed: server redial after token-mismatched frames")
                                    await asyncio.sleep(2.0)
                                    break
                                continue
                            state["feed"] = sanitize_feed(frame)
                            state["ts"] = time.time()
                            trace(frame)
                except Exception as e:
                    print(f"ws feed: {_mask_url(str(e), feed_token)} (retry in 2 s)")
                    time.sleep(2.0)
        threading.Thread(target=lambda: asyncio.run(run()), daemon=True).start()

    def get():
        if state["feed"] is None:
            return None
        if time.time() - state["ts"] > 1.0:
            return None
        return state["feed"]

    pump()
    return get


# ---------------------------------------------------------------------------
# Which monitor? (multi-monitor setups pick the screen, the overlay follows)
# ---------------------------------------------------------------------------

MONITOR_AUTO = ("", "auto", "default")   # specs that mean "you decide"


def _int_field(value, fallback):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _text_field(value, cap):
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:cap]   # collapse tabs/newlines a monitor name could carry


def probe_monitors(presenter=None, runner=None):
    """Ask the presenter for the connected monitors (one dict per wl_output).

    The presenter enumerates the compositor's outputs in registry order and
    the same order is what "#index" means later, so a probe and the surface
    agree. An empty list is a legitimate answer: no Wayland session, a
    presenter built before this feature, or a compositor that lists nothing.
    """
    path = presenter or PRESENTER
    run = runner if runner is not None else subprocess.run
    if not os.path.exists(path):
        return []
    try:
        done = run([path, "--list"], capture_output=True, timeout=8)
    except Exception as e:
        print(f"monitor probe failed: {e}")
        return []
    monitors = []
    for i, raw in enumerate((done.stdout or b"").decode(errors="replace").splitlines()):
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        monitors.append({
            "index": _int_field(row.get("index"), i),
            "name": _text_field(row.get("name"), 128),
            "description": _text_field(row.get("description"), 256),
            "x": _int_field(row.get("x"), 0),
            "y": _int_field(row.get("y"), 0),
            "width": max(0, _int_field(row.get("width"), 0)),
            "height": max(0, _int_field(row.get("height"), 0)),
            "scale": max(1, _int_field(row.get("scale"), 1)),
            "refresh_mhz": max(0, _int_field(row.get("refresh_mhz"), 0)),
        })
    return monitors


def monitor_label(m):
    """One human-readable line per monitor, as shown on the prompt."""
    name = (m.get("name") or m.get("description") or "unnamed output").strip()
    w, h = m.get("width") or 0, m.get("height") or 0
    size = f"{w}x{h}" if w and h else "size unknown"
    parts = [name, f"{size}+{m.get('x', 0)}+{m.get('y', 0)}"]
    if m.get("scale", 1) != 1:
        parts.append(f"scale {m['scale']}")
    # 179960 mHz is sold as 180 Hz; printing 179 would make a user doubt the list
    hz = round((m.get("refresh_mhz") or 0) / 1000.0)
    if hz:
        parts.append(f"{hz} Hz")
    return "  ".join(parts)


def monitor_choice(m):
    """What the user (and the presenter argv) should use for this monitor.

    A real wl_output name is the durable pick: connector names survive a
    replug and a rearranged layout, x offsets do not. Compositors that send no
    name at all (KWin does not send one) are picked by their probe index.
    """
    name = (m.get("name") or "").strip()
    return name if name else f"#{m.get('index', 0)}"


def resolve_monitor(monitors, spec):
    """Match one spec ("DP-1", "1"/"#1", "1280,0", a description fragment).

    Returns the monitor dict, or a human-readable 'what went wrong' string -
    the caller exits on that instead of guessing a screen.
    """
    spec = str(spec).strip()
    if not spec:
        return "empty monitor spec"
    bare = spec[1:] if spec.startswith("#") else spec

    if bare.isdigit():                      # "1" and "#1" mean the same pick
        for m in monitors:
            if m["index"] == int(bare):
                return m
        return (f"no monitor {spec} (known: "
                + ", ".join(monitor_choice(m) for m in monitors) + ")")

    pos = re.match(r"^(-?\d+),(-?\d+)$", spec)
    if pos:
        x, y = int(pos.group(1)), int(pos.group(2))
        hits = [m for m in monitors if m["x"] == x and m["y"] == y]
        if len(hits) == 1:
            return hits[0]
        if hits:
            return (f"position {spec} matches {len(hits)} monitors - use the "
                    "number instead")
        return f"no monitor at position {spec}"

    low = spec.lower()
    hits = [m for m in monitors if m["name"].lower() == low]
    if not hits:
        hits = [m for m in monitors if m["description"].lower() == low]
    if not hits:
        hits = [m for m in monitors
                if low in m["name"].lower() or low in m["description"].lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        return (f"no monitor matching {spec!r} (known: "
                + ", ".join(monitor_choice(m) for m in monitors) + ")")
    return (f"{spec!r} matches {len(hits)} monitors: "
            + ", ".join(monitor_choice(m) for m in hits))


def choose_monitor(monitors, wanted=None, interactive=False, prompt=None,
                   say=print, attempts=3):
    """Decide which monitor the overlay belongs on.

    Returns {"monitor": dict|None, "selector": str|None, "error": str|None,
    "asked": bool}. monitor None means "no pick, let the compositor decide".
    error is only ever set for an explicit spec that matches nothing: drawing
    on the wrong screen because of a typo is worse than refusing to start.
    """
    ask = prompt if prompt is not None else input
    out = {"monitor": None, "selector": None, "error": None, "asked": False}

    def take(m, why):
        out["monitor"] = m
        out["selector"] = monitor_choice(m)
        say(f"monitor: {monitor_label(m)} ({why})")
        return out

    if not monitors:
        say("monitor: the compositor gave no monitor list - using the "
            "compositor default output")
        return out

    spec = "" if wanted is None else str(wanted).strip()
    if spec.lower() in MONITOR_AUTO:
        spec = ""

    if spec:
        picked = resolve_monitor(monitors, spec)
        if isinstance(picked, dict):
            return take(picked, f"selected by {spec!r}")
        out["error"] = picked
        say(f"monitor: {picked}")
        return out

    if len(monitors) == 1:
        return take(monitors[0], "the only monitor")

    if not interactive:
        say(f"{len(monitors)} monitors detected, no monitor asked for: using the "
            "compositor default output. Pin one with --monitor "
            f"{monitor_choice(monitors[0])} (or \"monitor\": "
            f"\"{monitor_choice(monitors[0])}\" in config.json) - "
            + " | ".join(monitor_label(m) for m in monitors))
        return out

    say(f"{len(monitors)} monitors detected:")
    for m in monitors:
        say(f"  [{monitor_choice(m)}] {monitor_label(m)}")
    out["asked"] = True
    for _ in range(max(1, attempts)):
        try:
            answer = ask("Which monitor should the overlay use? "
                         "[number, name or x,y - Enter = first one]: ")
        except (EOFError, KeyboardInterrupt):
            say("monitor: no answer - using the compositor default output")
            return out
        answer = (answer or "").strip()
        if not answer:
            return take(monitors[0], "answered with Enter")
        picked = resolve_monitor(monitors, answer)
        if isinstance(picked, dict):
            return take(picked, "chosen on the prompt")
        say(f"  not a monitor: {picked}")
    say(f"monitor: no valid answer after {attempts} tries - using the "
        "compositor default output")
    return out


def run_wayland_feed(path, seconds, hz, smooth, W, H, out_x, debug=False,
                     trace=None, feed_token="", monitor_selector=None):
    """Feed ARGB frames to the native Wayland layer-shell presenter.

    KWin blends the alpha channel of a layer-shell surface, so this is the
    only path that yields real per-pixel transparency: the compositor owns
    the surface, and only dirty rectangles travel over the pipe.
    """
    if not os.path.exists(PRESENTER):
        print(f"missing presenter binary: {PRESENTER}")
        print("build it with: bash wayland/build.sh")
        return 2
    proc = None
    surface_size = [None]  # type: list  # live layer surface (w, h), updated on reconfigure

    def spawn():
        # Fourth argument is the monitor selector ("DP-1", "#1", "1280,0").
        # Without one the presenter keeps its legacy x-offset behaviour.
        argv = [PRESENTER, str(W), str(H), str(out_x)]
        if monitor_selector:
            argv.append(str(monitor_selector))
        p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        assert p.stdout is not None

        def note(text):
            # relay + parse one presenter status line
            print(f"presenter: {text}")
            m = re.match(r"configured (\d+)x(\d+)", text)
            if m:
                surface_size[0] = (int(m.group(1)), int(m.group(2)))

        # The compositor's configure callback can fire (and print
        # "configured WxH") BEFORE main prints the "ready" handshake line -
        # scan lines until ready arrives, ingesting any configure report
        # seen on the way instead of tripping over it.
        ready = False
        for _ in range(8):
            line = p.stdout.readline().decode(errors="replace").strip()
            if not line:
                break
            if line.startswith("ready"):
                print(f"presenter: {line}")
                ready = True
                break
            note(line)
        if not ready:
            p.kill()
            return None

        def drain():
            # Presenter reports live surface resizes on its stdout
            # ("configured WxH" after every KWin mode/scale/rotation change).
            # Draining keeps the pipe from ever filling AND gives frame_rects
            # the true surface size, so markers keep their coordinates right
            # when fractional scaling changes the surface (e.g. 1707x960
            # surface against a 2560x1440 game viewport).
            for raw in iter(p.stdout.readline, b""):
                text = raw.decode(errors="replace").strip()
                if text:
                    note(text)
        threading.Thread(target=drain, daemon=True).start()
        return p

    proc = spawn()
    if proc is None:
        return 1

    respawn_total = 0

    def respawn():
        """The compositor can destroy our layer-shell client (e.g. output
        layout changes, KWin reload) and the presenter exits with it. One
        bounded respawn chain keeps the overlay alive for the rest of the raid."""
        nonlocal proc, respawn_total
        cur = proc
        if cur is None:
            return False
        respawn_total += 1
        if respawn_total > 50:
            print("presenter keeps dying - giving up")
            return False
        if respawn_total > 3:  # back off so a hostile compositor can't spin us
            time.sleep(min(2.0, 0.25 * respawn_total))
        old_rc = cur.poll()
        if old_rc is None:  # presenter still alive but pipe broken - don't stack two
            cur.kill()
        if cur.stdin is not None:
            try:
                cur.stdin.close()
            except (BrokenPipeError, ValueError, OSError):
                pass
        for attempt in range(1, 6):
            np = spawn()
            if np is not None:
                proc = np
                print(f"presenter died (rc={old_rc}) - respawned "
                      f"(spawn try {attempt}/5, death {respawn_total}/50)")
                return True
            time.sleep(1.0)
        return False
    get_feed = make_feed_provider(path, trace, feed_token)
    t0 = time.time()
    frames = 0
    nrects = 0
    sm_state = {}
    had_feed = False
    miss_since = 0.0
    dbg_last = 0.0
    while True:
        now = time.time()
        if seconds and now - t0 >= seconds:
            break
        feed = get_feed()
        if feed is None:
            if had_feed and now - miss_since >= 1.0:
                try:
                    proc.stdin.write(b"C")  # clear frame: presenter zeroes its dirty rects
                    proc.stdin.flush()
                except (BrokenPipeError, ValueError):
                    if not respawn():
                        break
                had_feed = False
                print(f"[feed f{frames}] feed lost -> overlay cleared (wayland)")
            if not had_feed:
                miss_since = now
            time.sleep(1.0 / hz)
            continue
        if not had_feed:
            had_feed = True
            miss_since = now
        try:
            rects = frame_rects(feed, smooth, sm_state, view=surface_size[0])
        except Exception as exc:
            _render_failed("frame", exc, _ERRS)
            time.sleep(1.0 / hz)
            continue
        try:
            proc.stdin.write(encode_frame(rects))
            proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            if not respawn():
                print("presenter closed the pipe")
                break
        frames += 1
        nrects += len(rects)
        if debug and now - dbg_last >= 2.0:
            dbg_last = now
            t_ms = float(feed.get("t", now * 1000.0))
            print(f"[debug f{frames}] rects={len(rects)} feed_age="
                  f"{(now * 1000.0 - t_ms) / 1000.0:.2f}s rate~="
                  f"{frames / max(now - t0, 0.1):.1f}Hz")
        time.sleep(max(0.0, 1.0 / hz - (time.time() - now)))
    try:
        proc.stdin.close()
    except (BrokenPipeError, ValueError):
        pass
    proc.wait(timeout=5)
    print(f"wayland feed done: {frames} frames ({nrects} rects), {_mask_url(path, feed_token)}")
    return 0


def main():
    p = argparse.ArgumentParser(
        description="SPT quest marker overlay (Wayland layer-shell presenter)")
    p.add_argument("--config", default=None,
                   help="JSON defaults (default: config.json next to overlay.py, "
                        "NOT next to the launch directory); explicit CLI flags override them")
    p.add_argument("--feed", default=None)
    p.add_argument("--seconds", type=float, default=None)
    p.add_argument("--hz", type=float, default=None)
    p.add_argument("--geometry", default=None)
    p.add_argument("--debug", action="store_true",
                   help="Log feed stats (rects, feed age, rate) to stderr every 2 s.")
    p.add_argument("--trace", default=None, metavar="JSONL",
                   help="Record every arriving raw feed frame to JSONL "
                        "(diagnoses feed jitter: presence gaps, projection steps).")
    p.add_argument("--feed-token", default=None, metavar="TOKEN",
                   help="Shared feed secret (default: config.json feedToken). "
                        "ws:// feeds authenticate via challenge/response - the "
                        "token itself is never sent; both sides prove they know "
                        "it (protects against spoofed servers AND port squatters).")
    p.add_argument("--insecure", action="store_true",
                   help="Allow a ws:// feed WITHOUT a feed token (no challenge - "
                        "any local process could inject frames). Only for testing "
                        "against a plugin whose Feed Token is empty.")
    p.add_argument("--smooth", type=float, default=None,
                   help="EMA smoothing time constant in seconds "
                        "(0 = off, larger = smoother, no steady-state lag)")
    p.add_argument("--monitor", default=None, metavar="MONITOR",
                   help="Which screen to draw on: probe index ('1' or '#1'), "
                        "wl_output name ('DP-1'), a description fragment "
                        "('ULTRAGEAR') or a layout position ('1280,0'). With "
                        "several monitors and no --monitor, the overlay asks "
                        "once; without a terminal it uses the compositor "
                        "default. overlay.py --list-monitors prints them.")
    p.add_argument("--list-monitors", action="store_true",
                   help="Print what the compositor reports (one line per "
                        "monitor, with the value to pass as --monitor) and "
                        "exit.")
    args = p.parse_args()
    # config.json supplies defaults; explicit CLI flags win over it. The
    # default path is anchored to overlay.py's OWN directory (launch CWD
    # used to decide whether feedToken was found at all - launching from
    # elsewhere silently disabled feed authentication).
    global _CONFIG_PATH
    if args.config:
        _CONFIG_PATH = os.path.abspath(os.path.expanduser(args.config))
    cfg = _load_json(_CONFIG_PATH)
    if cfg is None:
        cfg = {}
    if not isinstance(cfg, dict):
        print(f"config: {_CONFIG_PATH} is not a JSON object - using built-in defaults")
        cfg = {}
    args.geometry = args.geometry if args.geometry is not None else cfg.get("geometry")
    args.feed = args.feed if args.feed is not None else cfg.get("feed")
    args.seconds = _finite(args.seconds if args.seconds is not None else cfg.get("seconds", 0.0), 0.0)
    hz_val = _finite(args.hz if args.hz is not None else cfg.get("hz", 120.0), 120.0)
    if not (1.0 <= hz_val <= 300.0):
        clamped = max(1.0, min(300.0, hz_val))
        print(f"hz {hz_val} out of usable range 1..300 - clamped to {clamped}")
        hz_val = clamped
    args.hz = hz_val
    args.smooth = _finite(args.smooth if args.smooth is not None else cfg.get("smooth", 0.6), 0.6)
    args.monitor = args.monitor if args.monitor is not None else cfg.get("monitor")
    args.feed_token = args.feed_token if args.feed_token is not None else cfg.get("feedToken", "")
    if args.list_monitors:
        found = probe_monitors()
        if not found:
            print("no monitor reported: no Wayland session, or a presenter "
                  "binary built before this feature (bash wayland/build.sh)")
            return 1
        for m in found:
            print(f"[{monitor_choice(m)}] {monitor_label(m)}")
        return 0
    if args.feed and args.feed.startswith("file://"):
        args.feed = args.feed[len("file://"):]  # accepted scheme == plain path
    if args.feed and args.feed.startswith("ws://"):
        try:
            import websockets  # noqa: F401  preflight: file feeds stay websockets-free
        except ImportError:
            print("websockets module required for ws:// feeds: .venv/bin/pip install websockets")
            return 2
        # A ws:// feed without a token is refused by default: without the
        # challenge ANY process on this machine that accepts the connection
        # could dictate what is displayed. The token is NEVER appended to the
        # URL (it would leak into logs and ps output) - the provider runs the
        # challenge handshake with it instead.
        if not args.feed_token and not args.insecure:
            print("refusing an unauthenticated ws:// feed: set feedToken "
                  "(config.json / --feed-token, matching the plugin's Feed "
                  "Token) or pass --insecure to test against a plugin with an "
                  "empty Feed Token.")
            return 2
    global FONT
    font = cfg.get("font", FEATURE_DEFAULTS["font"])
    if isinstance(font, str) and len(font) <= 128:
        FONT = font  # "" stays as-is: empty string = system default face (README)
    else:
        FONT = FEATURE_DEFAULTS["font"]

    if not args.feed:
        print("nothing to do (use --feed)")
        return 2
    sys.stdout = _Tee(sys.stdout, LOG_PATH)

    # Which screen? Ask the compositor once. A monitor the user named wins;
    # several screens without a named one ask the user (a terminal), or fall
    # back to the compositor default (a launcher, a service, CI) instead of
    # blocking on a prompt nobody can answer there.
    monitors = probe_monitors()
    picked = choose_monitor(monitors, args.monitor,
                            interactive=sys.stdin.isatty())
    if picked["error"]:
        print("pick a real monitor: --monitor <number|name|x,y> "
              "(overlay.py --list-monitors prints what is available)")
        return 2
    mon = picked["monitor"]

    if args.geometry:
        W, H, X, _ = parse_geometry(args.geometry)
    elif mon and mon["width"] > 0 and mon["height"] > 0:
        # the chosen screen's own size: the old hardcoded 2560x1440 frame
        # overshot a 1920x1080 second monitor by the whole difference
        W, H, X = mon["width"], mon["height"], mon["x"]
    else:
        W, H, X, _ = 2560, 1440, 0, 0  # primary display, fullscreen (single-monitor default)
    sel = picked["selector"]
    print(f"presenter: wayland layer-shell {W}x{H}, "
          + (f"monitor {sel}" if sel else f"output x={X}"))
    return run_wayland_feed(args.feed, args.seconds, args.hz, args.smooth,
                            W, H, X, debug=args.debug, trace=args.trace,
                            feed_token=args.feed_token, monitor_selector=sel)

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("interrupted, exiting")
        sys.exit(0)
