"""Summarize an overlay feed trace (overlay.py --trace logs/trace.jsonl).

Answers: are markers present continuously, how regular is the send cadence,
and how large are raw projection steps — i.e. is visible stutter data-side
(altating projections/presence gaps) or render-side.

Run: .venv/bin/python3 tools/trace_report.py logs/trace.jsonl [label-filter]
"""
import json
import statistics
import sys


def main(path, label_filter=None):
    frames = []
    with open(path) as fh:
        for line in fh:
            try:
                frames.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if not frames:
        print("no frames")
        return 1
    lts = [f["lt"] for f in frames]
    ts = [f["t"] for f in frames if f.get("t") is not None]
    dt_arr = [round((b - a) * 1000, 1) for a, b in zip(lts, lts[1:])]
    dt_feed = [round((b - a), 1) for a, b in zip(ts, ts[1:])]
    dur = lts[-1] - lts[0]
    print(f"frames={len(frames)} duration={dur:.1f}s rate={len(frames)/max(dur,1e-6):.1f}Hz")
    if dt_arr:
        print(f"arrival dt ms: median={statistics.median(dt_arr)} "
              f"p95={sorted(dt_arr)[int(len(dt_arr)*0.95)]} max={max(dt_arr)}")
    if dt_feed:
        print(f"feed t dt ms: median={statistics.median(dt_feed)} "
              f"p95={sorted(dt_feed)[int(len(dt_feed)*0.95)]} max={max(dt_feed)}")

    per = {}
    for f in frames:
        for m in (f.get("m") or []):
            key = (m.get("l"), m.get("k"))
            if label_filter and label_filter.lower() not in str(key[0]).lower():
                continue
            per.setdefault(key, []).append((f["lt"], m.get("nx"), m.get("ny"),
                                            m.get("b", 0)))
    for key, rows in sorted(per.items(), key=lambda kv: -len(kv[1])):
        if len(rows) < 5:
            continue
        gaps = 0
        max_gap_ms = 0.0
        for (a, _, _, _), (b, _, _, _) in zip(rows, rows[1:]):
            gap = (b - a) * 1000
            if gap > 120:
                gaps += 1
                max_gap_ms = max(max_gap_ms, gap)
        steps = []
        onscreen = [r for r in rows if r[3] == 0]
        for r1, r2 in zip(onscreen, onscreen[1:]):
            d = ((r2[1] - r1[1]) ** 2 + (r2[2] - r1[2]) ** 2) ** 0.5
            dt = max(r2[0] - r1[0], 1e-4)
            steps.append(d / dt)
        tag = f"{key[1]} '{key[0]}': frames={len(rows)}"
        if gaps:
            tag += f" PRESENCE-GAPS={gaps} max={max_gap_ms:.0f}ms"
        else:
            tag += " presence=continuous"
        if steps:
            pxs = sorted(s * 2560 for s in steps)   # normalized -> px @2560
            tag += (f" |screen speed| median={pxs[len(pxs)//2]:.1f}px/s "
                    f"p95={pxs[int(len(pxs)*0.95)]:.1f} max={pxs[-1]:.1f} px/s")
            med = pxs[len(pxs)//2] or 1.0
            if pxs[-1] > max(8.0, 6.0 * med):
                tag += "  <-- ALTATING/SPiking projection: DATA-SIDE jitter"
        print(tag)
    print("RESULT: done")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
