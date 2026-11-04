#!/usr/bin/env python3
"""Simulated SPT feed for overlay testing (no game needed).

Generates a rotating marker set for a virtual 2560x1440 viewport and writes
frames to latest.json in the QuestMarkersLinux feed format v2 (the same the
BepInEx plugin emits over the WebSocket): normalized positions
nx/ny in [0,1] (may exceed the viewport, then b=1 -> edge arrow), distances in
meters, kind state in "s", plus the cfg block mirroring the mod config.

python3 feed_sim.py [--path logs/latest.json] [--hz 30] [--imperial]
"""
import argparse
import json
import math
import os
import time


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--path", default="logs/latest.json")
    p.add_argument("--hz", type=float, default=30.0)
    p.add_argument("--imperial", action="store_true", help="cfg imperial units")
    p.add_argument("--scale", type=float, default=0.5, help="cfg marker scale")
    args = p.parse_args()

    w, h = 2560, 1440
    os.makedirs(os.path.dirname(args.path) or ".", exist_ok=True)
    t0 = time.time()
    labels = [
        ("zone", "Zone Goal", None),
        ("extract", "Factory Exit", "open"),
        ("extract", "Old Gate", "locked"),
        ("item", "Blue keycard", None),
        ("plant", "Secure Planting Site", None),
        ("beacon", "Custom Beacon", None),
    ]
    n = len(labels)
    while True:
        t = time.time() - t0
        m = []
        for i, (k, l, s) in enumerate(labels):
            ang = t * 0.35 + i * 2 * math.pi / n
            # 900/1280 of half-width -> markers periodically leave the viewport
            nx = 0.5 + (900 / w) * math.sin(ang) * 1.45
            ny = 0.5 + (500 / h) * math.cos(ang) * 1.45
            meters = 80 + 1250 * abs(math.sin(ang / 2))
            behind = 1 if (nx < 0 or nx > 1 or ny < 0 or ny > 1) else 0
            m.append({
                "i": i, "k": k, "nx": round(nx, 5), "ny": round(ny, 5),
                "d": round(meters, 1), "b": behind, "s": s, "l": l,
            })
        rec = {
            "t": int(time.time() * 1000), "v": [w, h], "m": m, "e": [],
            "cfg": {"labels": True, "distance": True, "edge": True,
                    "scale": args.scale, "farScale": 0.6,
                    "farDistance": 300, "imperial": args.imperial,
                    "iconScale": 3.0},
        }
        tmp = args.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(rec, f, separators=(",", ":"))
        os.replace(tmp, args.path)
        time.sleep(1.0 / args.hz)


if __name__ == "__main__":
    main()
