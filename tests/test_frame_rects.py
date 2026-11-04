"""frame_rects regression battery (run: .venv/bin/python3 tests/test_frame_rects.py).

Guards the behind/off-screen semantics the users reported broken:
1. behind markers (b=1, plugin mirrors their coords) must NEVER appear as a
   marker in the middle of the screen - edge arrow when cfg edge on, nothing
   when off;
2. no cross-screen chord shots: a marker going behind->front starts fresh at
   its real position (mirrored coords never enter the smoothing state);
3. wildly out-of-range b=0 coords (NaN side-plane fallback) behave like
   off-screen markers too, while <=10 % overflow still clips faithfully;
4. the configured tau (Smoothing) must actually change the blend.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import overlay as ov

VW, VH = 2560, 1440
CX, CY = VW / 2, VH / 2


def m(k, nx, ny, b=None, d=50, label="Obj"):
    e = {"k": k, "s": "objective", "l": label, "d": str(d),
         "nx": nx, "ny": ny, "op": 1}
    if b is not None:
        e["b"] = b
    return e


def feed(markers, edge=True):
    return {"v": [VW, VH], "m": markers, "cfg": {"edge": edge}}


def center_rects(rects):
    """rects whose area overlaps the middle screen quadrant-box"""
    out = []
    for rx, ry, surf in rects:
        w, h = surf.get_width(), surf.get_height()
        if abs(rx + w / 2 - CX) < VW / 4 and abs(ry + h / 2 - CY) < VH / 4:
            out.append((rx, ry, w, h))
    return out


def near_rects(rects, side):
    """arrow-sized rects in the border band of one screen side (arrows sit
    up to EDGE_MAX_DIST=250 px inward by design)"""
    out = []
    for rx, ry, surf in rects:
        w, h = surf.get_width(), surf.get_height()
        if w > 60:          # full markers are wider than an arrow at sc<=1
            continue
        if side == "R" and rx + w >= VW - 320 and rx < VW:
            out.append((rx, ry, w, h))
        if side == "L" and rx <= 320 and rx + w > 0:
            out.append((rx, ry, w, h))
        if side == "B" and ry + h >= VH - 320 and ry < VH:
            out.append((rx, ry, w, h))
        if side == "T" and ry <= 320 and ry + h > 0:
            out.append((rx, ry, w, h))
    return out


def test_behind_marker_edge_on():
    st = {}
    # directly behind camera center: plugin mirrors -> nx,ny ~ 0.45/0.52
    r = ov.frame_rects(feed([m("extract", 0.45, 0.52, b=1)]), 0.045, st)
    assert not center_rects(r), "behind marker drawn in the middle of the screen"
    assert near_rects(r, "T") or near_rects(r, "B") or \
        near_rects(r, "L") or near_rects(r, "R"), "no edge arrow for behind marker"
    assert not st, "mirrored coords leaked into smoothing state"
    print("behind marker (edge on): arrow only, no center marker: OK")


def test_behind_marker_edge_off():
    st = {}
    r = ov.frame_rects(feed([m("extract", 0.45, 0.52, b=1)], edge=False),
                       0.045, st)
    assert len(r) == 0, f"behind marker rendered with edge arrows off: {r}"
    assert not st
    print("behind marker (edge off): nothing rendered: OK")


def test_far_off_b0_fallback_coords():
    # side-plane NaN fallback with fd>0: wildly out-of-range b=0 coords
    st = {}
    r = ov.frame_rects(feed([m("quest", 4.2, 0.4, b=0)]), 0.045, st)
    assert not center_rects(r)
    assert near_rects(r, "R"), "far-off b=0 coord must render as edge arrow"
    assert not st
    r2 = ov.frame_rects(feed([m("quest", 4.2, 0.4, b=0)], edge=False),
                        0.045, st)
    assert len(r2) == 0, "far-off marker rendered with edge off"
    # <=10 % overflow stays a legitimately clipped marker, not an arrow
    r3 = ov.frame_rects(feed([m("quest", 1.05, 0.5, b=0)]), 0.045, st)
    mr = [t for t in r3 if t[0] + t[2].get_width() >= VW - 200
          and t[2].get_width() > 60]
    assert mr, "slightly off-screen marker must remain a real marker"
    print("far-off fallback coords: arrow/hidden, <=10 % overflow clipped marker: OK")


def test_transition_no_cross_screen_shot():
    st = {}
    f1 = feed([m("quest", 0.15, 0.40, b=1)])   # behind phase (mirrored coords)
    ov.frame_rects(f1, 0.045, st)
    assert not st
    # one frame later the marker is genuinely in view at nx=0.8
    t0 = time.monotonic()
    while time.monotonic() - t0 < 0.05:
        time.sleep(0.005)
    f2 = feed([m("quest", 0.80, 0.40, b=0)])
    r = ov.frame_rects(f2, 0.045, st)
    assert r, "re-entering marker missing"
    rx, ry, surf = r[0]
    w, h = surf.get_width(), surf.get_height()
    tipx = rx + w / 2
    assert abs(tipx - 0.80 * VW) < 30, (tipx, 0.80 * VW)
    assert 0.40 * VH - 140 < ry < 0.40 * VH + 60, ry  # anchored near real ny
    print("behind->front transition: fresh start at real position: OK")


def test_tau_is_effective():
    """the config knob must change behavior: slow chord, big vs tiny tau"""
    def lag_for(tau):
        st = {}
        key = ("quest", "L", "objective")
        # 10 Hz feed staircase: +60 px steps; render at 120 Hz, measure
        # steady-state lag display vs newest chord point after settling
        meas = 0.0
        lag = 0.0
        t0 = time.monotonic()
        last_meas = t0
        d = None
        while time.monotonic() - t0 < 1.5:
            now = time.monotonic()
            if now - last_meas >= 0.1:
                meas += 6.0
                last_meas = now
            d, _ = ov.smooth_pos(st, key, meas, 100.0, tau)
            lag = max(lag, meas - d)
            time.sleep(1 / 120)
        return lag
    lag_small = lag_for(0.005)
    time.sleep(0.2)
    lag_big = lag_for(0.100)
    print(f"tau effect: lag(tau=0.005)={lag_small:.1f}px lag(tau=0.1)={lag_big:.1f}px")
    assert lag_big > lag_small + 2.0, "Smoothing config does nothing"


if __name__ == "__main__":
    test_behind_marker_edge_on()
    test_behind_marker_edge_off()
    test_far_off_b0_fallback_coords()
    test_transition_no_cross_screen_shot()
    test_tau_is_effective()
    print("RESULT: PASS")
