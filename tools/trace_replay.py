"""Replay a real feed trace through a smoothing function at the overlay render
rate and quantify DISPLAY velocity pulsation (visible stutter) per measurement
cycle. Usage: trace_replay.py logs/trace.jsonl [label-substring]
"""
import math
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import overlay as ov

HZ = 120.0
TAU = 0.045
W, H = 2560.0, 1440.0


def chord_smooth(state, key, x, y, tau, now):
    """Candidate: delayed chord interpolation (1 sample behind, pure
    interpolation of past measurements, no extrapolation, continuous)."""
    if tau <= 0.0:
        return x, y
    prev = state.get(key)
    if prev is None:
        state[key] = [x, y, now, x, y, now]
        return x, y
    m1x, m1y, t1, m2x, m2y, t2 = prev
    if x != m2x or y != m2y:
        gap = now - t2
        if gap > 0.30:            # feed stall: snap, crawl again from m2
            m1x, m1y, t1 = m2x, m2y, now
        else:
            m1x, m1y, t1 = m2x, m2y, t2
        m2x, m2y, t2 = x, y, now
    dtn = max(t2 - t1, 1e-4)
    u = (now - t1) / dtn - 1.0   # delayed by exactly one measurement interval
    if u < 0.0:
        u = 0.0
    elif u > 1.0:
        u = 1.0
    state[key] = [m1x, m1y, t1, m2x, m2y, t2]
    return m1x + (m2x - m1x) * u, m1y + (m2y - m1y) * u


def main(path, want=None):
    frames = []
    with open(path) as fh:
        for line in fh:
            frames.append(__import__("json").loads(line))

    from collections import defaultdict
    labels = defaultdict(list)
    for f in frames:
        per = defaultdict(list)
        for m in (f.get("m") or []):
            per[m.get("l")].append((m.get("d"), m.get("nx"), m.get("ny"), m.get("b", 0)))
        for lbl, items in per.items():
            tracks = labels[lbl]
            free = list(items)
            for tr in tracks:
                if not free:
                    break
                cand = min(range(len(free)), key=lambda i: abs(free[i][0] - tr[-1][2]))
                if abs(free[cand][0] - tr[-1][2]) <= 3:
                    tr.append((f["lt"], f["t"] / 1000.0, *free[cand]))
                    free.pop(cand)
            for it in free:
                tracks.append([(f["lt"], f["t"] / 1000.0, *it)])

    hdr = f"{'marker':40} {'cyc':>4} {'d-range':>11} {'EMA-puls med/p90':>22} {'chord-puls med/p90':>22}"
    print(hdr)
    for lbl, tracks in sorted(labels.items()):
        if want and want.lower() not in lbl.lower():
            continue
        for ti, tr in enumerate(tracks):
            # row layout: (lt, tsec, d, nx, ny, b)
            ons = [t for t in tr if t[5] == 0 and -0.05 <= t[3] <= 1.05 and -0.05 <= t[4] <= 1.05]
            if len(ons) < 40:
                continue

            def replay(smoother, clock_base):
                st = {}
                i = 0
                t0 = ons[0][0]
                t_end = min(ons[-1][0], t0 + 20.0)
                prev_disp = prev_meas = meas_now = None
                disp_seq, meas_seq, dspeed, mt = [], [], [], None
                pulses = []
                t = t0
                fake = clock_base
                while t < t_end:
                    while i < len(ons) and ons[i][0] <= t:
                        meas_now = ons[i]
                        i += 1
                    if meas_now is None:
                        t += 1.0 / HZ
                        continue
                    mx, my = meas_now[3] * W, meas_now[4] * H
                    if smoother is ov.smooth_pos:
                        d = smoother(st, "m", mx, my, TAU)
                    else:
                        d = smoother(st, "m", mx, my, TAU, t)
                    if prev_disp is not None:
                        disp_seq.append(math.hypot(d[0] - prev_disp[0], d[1] - prev_disp[1]) * HZ)
                        meas_seq.append(math.hypot(mx - prev_meas[0], my - prev_meas[1]) * HZ)
                    prev_disp, prev_meas = d, (mx, my)
                    # group 4 renders per 33ms measurement cycle -> pulsation
                    if len(disp_seq) and len(disp_seq) % 4 == 0:
                        chunk = disp_seq[-4:]
                        vm = max(meas_seq[-4:])
                        if vm > 6:   # real movement cycles (slow crawl included)
                            pulses.append((meas_now[2], min(chunk), max(chunk), vm))
                    t += 1.0 / HZ
                ratios = [(mx_ - mn) / vm for _, mn, mx_, vm in pulses if vm > 6]
                return pulses, ratios

            p_e, r_e = replay(ov.smooth_pos, None)
            p_c, r_c = replay(chord_smooth, None)
            if not p_e or not r_e:
                continue
            dmids = sorted(p[0] for p in p_e)
            def fmt(r):
                if not r:
                    return "        -      -"
                return f"{statistics.median(r):10.2f}{sorted(r)[int(len(r)*0.9)]:12.2f}"
            print(f"{lbl} #{ti:1}" + " " * max(1, 35 - len(lbl)) +
                  f"{len(p_e):5} {dmids[0]:4}-{dmids[-1]:4}m {fmt(r_e)} {fmt(r_c)}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
