"""Feed-hardening battery (security-review MEDIUM/LOW findings).

Covers the provider-boundary sanitize (viewport guard, marker/label/coord/
distance caps, per-marker sc/ic + edge w/h/sc DoS clamps, unhashable state
drop), cfg clamps for feed-injected geometry values, frame_rects defense in
depth (hostile v, MAX_RECTS wire cap, surface-cap splitting of oversized
merged clusters, per-frame payload total cap), --trace size cap, token
masking in log lines and overlay.log startup rotation.

Run: .venv/bin/python3 tests/test_feed_hardening.py
"""
import json
import math
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import overlay  # noqa: E402

checks = []
tmp = tempfile.mkdtemp(prefix="qmdl_hardening_")


def check(name, passed, detail=""):
    checks.append(bool(passed))
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))


# 1) cfg_of clamps hostile feed-injected cfg
cfg = overlay.cfg_of({"cfg": {"iconScale": 1e9, "edgeSize": 1e9,
                              "farDistance": 1e12, "scale": "abc",
                              "smooth": -5,
                              "font": "x" * 5000, "evilKey": 1}})
check("iconScale clamped", 0.25 <= cfg["iconScale"] <= 4.0, str(cfg["iconScale"]))
check("edgeSize clamped", 4.0 <= cfg["edgeSize"] <= 192.0, str(cfg["edgeSize"]))
check("farDistance upper clamp", 1.0 <= cfg["farDistance"] <= 100000.0)
check("non-numeric scale -> default", cfg["scale"] == overlay.FEATURE_DEFAULTS["scale"])
check("negative smooth -> 0", cfg["smooth"] == 0.0)
check("removed presenter key stays gone", "presenter" not in cfg)
check("oversized font rejected", cfg["font"] == overlay.FEATURE_DEFAULTS["font"])
check("feed cfg cannot inject unknown keys", "evilKey" not in cfg)
check("removed showLog feature stays gone", "showLog" not in cfg)

# 2) sanitize_feed bounds a hostile frame
hostile = {
    "t": "not-a-time",
    "v": [0, 0],
    "m": ([{"k": "Quest", "l": "L" * 5000, "nx": 1e18, "ny": -1e18,
            "d": 1e30, "op": "x", "b": 2, "s": "s" * 500}] * 4000),
    "e": [{"x": 1e9, "y": float("nan"), "op": 5, "l": "E" * 4000}],
}
san = overlay.sanitize_feed(hostile)
assert san is not None
check("viewport v=[0,0] -> default", san["v"] == [2560.0, 1440.0])
check("markers capped", len(san["m"]) <= overlay.MAX_MARKERS, f"{len(san['m'])}")
check("labels capped", all(len(m["l"]) <= overlay.MAX_LABEL for m in san["m"]))
check("coords clamped", all(abs(m["nx"]) <= overlay.MAX_COORD and abs(m["ny"]) <= overlay.MAX_COORD
                            for m in san["m"]))
check("distances clamped", all(0.0 <= m["d"] <= overlay.MAX_METERS for m in san["m"]))
check("op numeric 0..1", all(isinstance(m["op"], float) and 0.0 <= m["op"] <= 1.0 for m in san["m"]))
check("b clamped to <=1", all(m["b"] <= 1.0 for m in san["m"]))
check("keys/states capped", all(len(str(m.get("k", ""))) <= overlay.MAX_KEY
                                and len(str(m.get("s", ""))) <= overlay.MAX_KEY for m in san["m"]))
check("t numeric", isinstance(san["t"], float))
check("edge list sanitized", len(san["e"]) == 1 and math.isfinite(san["e"][0]["x"])
      and san["e"][0]["op"] <= 1.0)

# 2b) DoS fields that used to pass through the sanitize unclamped
hostile2 = {"t": 1, "v": [2560, 1440],
            "m": [{"k": "Q", "l": "x", "nx": 0.5, "ny": 0.5, "d": 5,
                   "sc": 1e9, "ic": -50, "s": ["evil", "list"]}],
            "e": [{"x": 10, "y": 10, "w": 1e9, "h": 1e9, "sc": 1e9, "op": 1}]}
san2 = overlay.sanitize_feed(hostile2)
assert san2 is not None
check("marker sc/ic clamped to sane multipliers",
      0.1 <= san2["m"][0]["sc"] <= 8.0 and 0.1 <= san2["m"][0]["ic"] <= 8.0,
      f"sc={san2['m'][0]['sc']} ic={san2['m'][0]['ic']}")
check("non-string state dropped (unhashable key-tuple crash class)",
      san2["m"][0]["s"] is None, repr(san2["m"][0]["s"]))
check("edge w/h/sc clamped",
      1.0 <= san2["e"][0]["w"] <= 4096.0 and 1.0 <= san2["e"][0]["h"] <= 4096.0
      and 0.1 <= san2["e"][0]["sc"] <= 8.0)

# 3) regression class: v=[0,0] used to ZeroDivisionError and lose whole frames
legacy = {"t": time.time(), "v": [0, 0],
          "m": [{"k": "Extract", "l": "Factory Exit", "x": 900, "y": 400, "d": 50}]}
rects = overlay.frame_rects(overlay.sanitize_feed(legacy), 0.0, {})
check("v=[0,0] legacy feed renders (no ZeroDivisionError)", len(rects) > 0, f"{len(rects)} rects")

# frame_rects defense in depth: hostile v straight into frame_rects
rects2 = overlay.frame_rects({"t": time.time(), "v": [0, 0],
                              "m": [{"k": "Quest", "l": "Q", "nx": 0.5, "ny": 0.5, "d": 10}]},
                             0.0, {})
check("frame_rects guards hostile v itself", isinstance(rects2, list)
      and len(rects2) == 1, f"{len(rects2)} rects (expected exactly 1 rendered marker)")

# 3b) oversized merged clusters are SPLIT, not merely warned about
big = overlay.cairo.ImageSurface(overlay.cairo.FORMAT_ARGB32, 3000, 2000)
cap = 2560 * 1440
pieces = overlay.split_surface(100, 200, big, cap)
check("split_surface: pieces within cap, rows fully covered, offsets continue",
      all(p[2].get_width() * p[2].get_height() <= cap for p in pieces)
      and sum(p[2].get_height() for p in pieces) == 2000
      and pieces[0][1] == 200
      and pieces[-1][1] + pieces[-1][2].get_height() == 200 + 2000,
      f"{len(pieces)} pieces")

# 4) rect hard cap: 3000 markers bypassing the provider cap
feed = {"t": time.time(), "v": [2560, 1440],
        "m": [{"k": "Quest", "l": f"Q{i}", "nx": 0.1 + (i % 80) / 100,
               "ny": 0.1 + (i % 70) / 100, "d": 10 + i} for i in range(3000)]}
rects3 = overlay.frame_rects(feed, 0.0, {})
check("rect count hard-capped at MAX_RECTS", 0 < len(rects3) <= overlay.MAX_RECTS, f"{len(rects3)}")
areas3 = [r[2].get_width() * r[2].get_height() for r in rects3]
check("every rect within surface cap and frame total within presenter cap",
      all(a <= 2560 * 1440 for a in areas3) and sum(areas3) <= 2 * 2560 * 1440,
      f"total {sum(areas3)} px (cap {2 * 2560 * 1440})")

# 5) file provider hands out sanitized frames (hostile latest.json)
feed_path = os.path.join(tmp, "latest.json")
get_file = overlay.make_feed_provider(feed_path)
with open(feed_path, "w") as f:
    json.dump({"t": 1, "v": [0, 0],
               "m": [{"k": "Q", "l": "y" * 4000, "nx": 1e18, "ny": 0.5, "d": 1e30}]}, f)
os.utime(feed_path, None)
frame = None
for _ in range(10):
    frame = get_file()
    if frame:
        break
    time.sleep(0.05)
assert frame is not None
check("file provider sanitizes at the boundary",
      frame["v"] == [2560.0, 1440.0]
      and len(frame["m"][0]["l"]) <= overlay.MAX_LABEL
      and abs(frame["m"][0]["nx"]) <= overlay.MAX_COORD)

# 6) --trace size cap (raw frame is still traced, writes stop at the cap)
trace_path = os.path.join(tmp, "trace.jsonl")
old_cap = overlay.TRACE_MAX_BYTES
overlay.TRACE_MAX_BYTES = 800
try:
    # small diagnostic frames so several lines are needed to hit the cap
    with open(feed_path, "w") as f:
        json.dump({"t": 1, "v": [2560, 1440],
                   "m": [{"k": "Q", "l": "T", "nx": 0.5, "ny": 0.5, "d": 5}]}, f)
    get_trace = overlay.make_feed_provider(feed_path, trace_path)
    for _ in range(30):
        os.utime(feed_path, None)
        get_trace()
        time.sleep(0.05)
    size = os.path.getsize(trace_path)
    check("trace JSONL respects size cap", 800 < size < 1200, f"{size} bytes (cap 800)")
finally:
    overlay.TRACE_MAX_BYTES = old_cap

# 7) token never reaches the log line (masking helper)
masked = overlay._mask_url("ws feed connected: ws://127.0.0.1:9378/?token=SECRET", "SECRET")
check("connect log masks the feed token", "SECRET" not in masked and "***" in masked)

# 8) overlay.log startup rotation (one generation)
class _Stream:
    def write(self, s):
        pass

    def flush(self):
        pass


big = os.path.join(tmp, "overlay.log")
with open(big, "w") as f:
    f.write("x" * (overlay.LOG_ROTATE_BYTES + 4096))
tee = overlay._Tee(_Stream(), big)
tee.write("fresh line\n")
assert tee.fh is not None
tee.fh.close()
check("oversized overlay.log rotates to .1",
      os.path.exists(big + ".1")
      and os.path.getsize(big + ".1") > overlay.LOG_ROTATE_BYTES
      and os.path.getsize(big) < 100)

n_ok = sum(1 for c in checks if c)
print(f"RESULT: {'PASS' if n_ok == len(checks) else 'FAIL'} ({n_ok}/{len(checks)})")
sys.exit(0 if n_ok == len(checks) else 1)
