"""smooth_pos regression battery (run: .venv/bin/python3 tests/test_smooth.py).

Covers the delayed chord interpolation: staircase measurements (game-FPS-
limited sampling) must render as CONSTANT continuous motion (anti-zappeln),
lag stays bounded to ~one measurement interval, far-field glide intact,
no drift, stationary markers rock-steady, reversals bounded, feed-gap stalls
snap instead of crawling. The legacy fixed-tau EMA is kept in this file ONLY
to prove the battery actually discriminates the old stutter behavior.
"""
import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import overlay as ov

TAU = 0.045
REAL_MONOTONIC = time.monotonic


class VirtualClock:
    """smooth_pos and the legacy EMA both read time.monotonic(); feeding them a
    synthetic clock turns the staircase battery into an exact measurement. The
    wall-clock version measured whatever the scheduler gave it and drifted to
    0.415 on a loaded GitHub runner against a 0.4 assert."""

    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def __enter__(self):
        time.monotonic = self.monotonic
        return self

    def __exit__(self, *exc):
        time.monotonic = REAL_MONOTONIC


def old_smooth(st, key, x, y, tau):
    """legacy fixed-tau EMA (what users experienced as zappeln on staircased
    feeds); used only to prove the battery catches that behavior"""
    now = time.monotonic()
    prev = st.get(key)
    if prev is None:
        st[key] = [x, y, now]
        return x, y
    dx, dy, tlast = prev
    dt = max(now - tlast, 1e-4)
    a = 1.0 - math.exp(-dt / tau)
    dx += (x - dx) * a
    dy += (y - dy) * a
    st[key] = [dx, dy, now]
    return dx, dy


def test_staircase_constant_motion():
    """Decisive anti-zappeln test: 15 Hz sampling (game-FPS staircase),
    120 Hz render, constant 300 px/s true motion. New smoother: near-constant
    display steps. Legacy EMA on the same staircase: pulsating steps.

    Driven by a synthetic clock: the measurement grid is exactly 1/120 s and the
    sample staircase exactly 0.0667 s, so every run computes the same numbers.
    A real-time version here tests the load of the machine, not the smoother."""
    sample, tick, ticks = 0.0667, 1 / 120, 3000      # 25 s of virtual time
    st, st_o = {}, {}
    steps, lags, steps_o = [], [], []
    prev_d = prev_o = None
    with VirtualClock() as clock:
        for i in range(ticks):
            clock.t = i * tick
            mx = 300.0 * ((clock.t // sample) * sample)
            d, _ = ov.smooth_pos(st, "q", mx, 100.0, TAU)
            do = old_smooth(st_o, "q", mx, 100.0, 0.030)[0]
            if prev_d is not None:
                steps.append(d - prev_d)
                lags.append(mx - d)
            if prev_o is not None:
                steps_o.append(do - prev_o)
            prev_d, prev_o = d, do

    moving = steps[30:-10]
    med = sorted(moving)[len(moving) // 2]
    lo, hi = min(moving), max(moving)
    lag_max = max(lags[30:])
    print(f"staircase: steps med={med:.2f} min={lo:.2f} max={hi:.2f} "
          f"ratio={hi / max(lo, 1e-9):.2f} min/med={lo / med:.2f} lagmax={lag_max:.1f}px")
    assert med > 1.5, "no motion"
    assert lo >= 0.55 * med, "micro-stops (zappeln) in display motion"
    assert lag_max <= 32.0, lag_max

    mo = steps_o[30:-10]
    mo_med = sorted(mo)[len(mo) // 2]
    micro = min(mo) / max(mo_med, 1e-9)
    print(f"staircase legacy: min/med={micro:.2f} (must be <0.4 - old stutter) "
          f"relative to new smoother: {micro / max(lo / med, 1e-9):.2f}")
    assert micro < 0.4, micro
    assert micro <= 0.7 * (lo / med), "legacy stutter not discriminated from the new smoother"


def test_close_parallax(jitter):
    """strafe 2 m/s, distance 40->4 m (screen speed 64->640 px/s), 120 Hz"""
    st, old_st = {}, {}
    lags_new, lags_old = [], []
    disp_steps, meas_steps = [], []
    prev_meas = prev_disp = 0.0
    first = True
    t0 = time.monotonic()
    while time.monotonic() - t0 <= 6.2:
        t = time.monotonic() - t0
        d = max(40.0 - 6.0 * t, 4.0)
        meas = 1280.0 + 1280.0 * 2.0 / d
        dn, _ = ov.smooth_pos(st, "q", meas, 100.0, TAU)
        do, _ = old_smooth(old_st, "q", meas, 100.0, TAU)
        dn, do = float(dn), float(do)
        if not first:
            disp_steps.append(dn - prev_disp)
            meas_steps.append(meas - prev_meas)
        lags_new.append(abs(meas - dn))
        lags_old.append(abs(meas - do))
        prev_meas, prev_disp = meas, dn
        first = False
        dt = 1.0 / 120.0
        if jitter:
            dt += random.uniform(-0.003, 0.003)
        time.sleep(dt)
    lag_new = max(lags_new[-60:])
    lag_old = max(lags_old[-60:])
    med_new = sorted(disp_steps)[len(disp_steps) // 2]
    med_meas = sorted(meas_steps)[len(meas_steps) // 2]
    print(f"close parallax jitter={jitter}: old lag {lag_old:.1f}px -> "
          f"new {lag_new:.1f}px, step ratio med {med_new / med_meas:.2f}")
    assert lag_new <= 16.0, lag_new
    assert lag_old > 20.0, lag_old          # regression measurable
    ratio = med_new / med_meas
    assert 0.9 <= ratio <= 1.15, ratio


def test_far_field_glide():
    st2 = {}
    lags = []
    for i in range(150):
        meas = 100.0 + 60.0 * i / 60.0
        dxx, _ = ov.smooth_pos(st2, "p", meas, 100, TAU)
        lags.append(meas - dxx)
        time.sleep(1 / 60)
    lag = max(lags[30:])
    print(f"far field: lag {lag:.2f}px")
    assert lag <= 4.5, lag


def test_no_drift_convergence():
    st = {}
    prevv = 0.0
    for k in range(60):
        d, _ = ov.smooth_pos(st, "p", 500.0, 100.0, TAU)
        if k:
            assert d >= prevv - 1e-9 and d <= 500.0 + 1e-9, (prevv, d)
        prevv = float(d)
        time.sleep(1 / 60)
    d2, _ = ov.smooth_pos(st, "p", 500.0, 100.0, TAU)   # re-render same value
    assert 499.9 < prevv <= 500.0 + 1e-3 and 499.9 < float(d2) <= 500.0 + 1e-3
    d3, _ = ov.smooth_pos(st, "p", 500.0, 100.0, TAU)   # rock-steady stationary
    assert abs(float(d3) - 500.0) < 1e-6
    print("no-drift + re-render convergence + rock-steady: OK")


def test_reversal_bounded():
    st = {}
    vals = [100.0 + 400.0 * min(i, 60) / 60.0 for i in range(120)]
    vals += [500.0 - 400.0 * (i - 120) / 60.0 for i in range(120, 180)]
    overshoot = 0.0
    for i, vm in enumerate(vals):
        d, _ = ov.smooth_pos(st, "r", vm, 100.0, TAU)
        if i:
            lo, hi = min(vm, vals[i - 1]), max(vm, vals[i - 1])
            overshoot = max(overshoot, max(d - hi, lo - d))
        time.sleep(1 / 120)
    print(f"reversal overshoot {overshoot:.1f}px")
    assert overshoot <= 18.0, overshoot


def test_feed_gap_snap():
    st = {}
    for x in (100.0, 120.0, 140.0):
        ov.smooth_pos(st, "g", x, 100.0, TAU)
        time.sleep(1 / 30)
    time.sleep(0.5)                                   # feed stall
    d, _ = ov.smooth_pos(st, "g", 400.0, 100.0, TAU)  # resume: teleports
    assert abs(float(d) - 400.0) < 2.0                # snaps, never crawls
    d2, _ = ov.smooth_pos(st, "g", 420.0, 100.0, TAU)
    time.sleep(1 / 60)
    d3, _ = ov.smooth_pos(st, "g", 420.0, 100.0, TAU)
    assert abs(float(d2) - 400.0) < 8.0 and abs(float(d3) - 400.0) < 25.0
    print("feed-gap snap (no crawl across stall): OK")


if __name__ == "__main__":
    test_staircase_constant_motion()
    test_close_parallax(jitter=False)
    test_close_parallax(jitter=True)
    test_far_field_glide()
    test_no_drift_convergence()
    test_reversal_bounded()
    test_feed_gap_snap()
    print("RESULT: PASS")
