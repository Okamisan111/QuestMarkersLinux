"""Semi-transparency battery (run: .venv/bin/python3 tests/test_alpha.py).

Guards the tiered-opacity renderer:
1. layer_alpha semantics: opacity 1.0 reproduces the old fully opaque look for
   every layer, and lowering opacity gives up opacity in the designed order
   (plate first, outlines/text last);
2. hostile `op` values stay clamped and hidden-but-crash-free (feed hardening);
3. the painted result really is translucent where it should be and is NOT where
   it should not be: tagbox plate at the plate tier, outlines strictly more
   solid than the plate they sit on, quest label glyphs never below the label
   tier, while opacity 1.0 keeps exactly the opaque pixels it always had;
4. the caches distinguish opacity buckets and do not churn, and the quest label
   surface is literally shared between opacity 0.4 and 1.0 (labels must not fade);
5. merge_overlapping footprints stay pairwise disjoint with alpha in play - with
   the presenter's memcpy that is a correctness property, not an optimisation;
6. edge arrows and the wire's own fractional `op` (what a 1.2.2 plugin sends)
   follow the same tiers.

Pixel expectations are computed the way cairo stores pixels: premultiplied, and
memory order BGRA while the colour constants here are (R, G, B). A layer painted
OVER another layer is expected as the OPERATOR_OVER result, not as its own tier
alpha - which is why an outline is always more opaque than the plate beneath it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import overlay as ov  # noqa: E402

VW, VH = 2560, 1440


def marker(nx=0.42, ny=0.31, op: float = 1.0, k="zone", label="Signal - Part 1 (1/4)",
           dist="123m", s=None):
    return {"k": k, "nx": nx, "ny": ny, "b": 0, "op": op, "l": label,
            "d": dist, "s": s}


def feed(markers, cfg=None):
    f = {"v": [VW, VH], "m": markers, "cfg": {"edge": True, "scale": 0.6,
                                              "farScale": 0.6}}
    if cfg:
        f["cfg"].update(cfg)
    return f


def rects(f):
    san = ov.sanitize_feed(f)
    assert san is not None, "sanitize_feed rejected a valid feed"
    out = ov.frame_rects(san, smooth=0.0)
    assert isinstance(out, list)
    return out


def stored(color, a, over=None):
    """expected premultiplied BGRA quad for `color` (R,G,B) at alpha `a`;
    `over=(color,alpha)` models OPERATOR_OVER onto an existing layer."""
    r, g, b = color
    if over is None:
        return (round(b * a), round(g * a), round(r * a), round(255 * a))
    dr, dg, db = over[0]
    da = over[1]
    out_a = a + da * (1.0 - a)
    return (round(b * a + db * da * (1.0 - a)),
            round(g * a + dg * da * (1.0 - a)),
            round(r * a + dr * da * (1.0 - a)),
            round(255 * out_a))


def find_px(surf, quad, tol=4, min_hits=0):
    """count pixels equal to the stored quad (within tol), 0 if below min_hits"""
    data, stride = bytes(surf.get_data()), surf.get_stride()
    hits = 0
    for y in range(surf.get_height()):
        row = data[y * stride:(y + 1) * stride]
        for x in range(surf.get_width()):
            px = row[x * 4:x * 4 + 4]
            if all(abs(px[i] - quad[i]) <= tol for i in range(4)):
                hits += 1
    return hits if hits >= min_hits else 0


def max_alpha_of_hue(surf, color, tol=12):
    """highest alpha of any pixel whose UN-premultiplied colour is `color`
    (catches OPERATOR_OVER blends of that hue at any tier)"""
    r, g, b = color
    data, stride = bytes(surf.get_data()), surf.get_stride()
    best = 0
    for y in range(surf.get_height()):
        row = data[y * stride:(y + 1) * stride]
        for x in range(surf.get_width()):
            bl, gr, rr, a = row[x * 4:x * 4 + 4]
            if a == 0:
                continue
            if (abs(bl * 255 // a - b) <= tol and abs(gr * 255 // a - g) <= tol
                    and abs(rr * 255 // a - r) <= tol and a > best):
                best = a
    return best


def alpha_counts(surf):
    data, stride = bytes(surf.get_data()), surf.get_stride()
    zero = semi = full = 0
    for y in range(surf.get_height()):
        row = data[y * stride:(y + 1) * stride]
        for x in range(surf.get_width()):
            a = row[x * 4 + 3]
            if a == 0:
                zero += 1
            elif a == 255:
                full += 1
            else:
                semi += 1
    return zero, semi, full


def test_layer_tiers():
    for w in ov.ALPHA_WEIGHTS:
        assert abs(ov.layer_alpha(1.0, w) - 1.0) < 1e-9, w
    half = {w: ov.layer_alpha(0.5, w) for w in ov.ALPHA_WEIGHTS}
    assert half["plate"] == 0.5 and half["pin"] == 0.5
    assert half["border"] == 0.75 and half["icon"] == 0.85
    assert half["label"] == 1.0 and half["dist"] == 0.96
    # designed fade order: background first, outlines/text last
    assert half["plate"] <= half["border"] <= half["icon"] <= half["dist"] <= half["label"]
    for w in ov.ALPHA_WEIGHTS:
        vals = [ov.layer_alpha(o, w) for o in (0.1, 0.25, 0.5, 0.75, 1.0)]
        assert all(bv >= av - 1e-9 for av, bv in zip(vals, vals[1:])), w
    print("layer tiers: opacity 1.0 -> all opaque; 0.5 ->",
          {k: round(v, 2) for k, v in half.items()})


def test_hostile_op_values():
    bad = [0, -3, 1e9, float("inf"), float("nan"), "x", None, True]
    # distinct positions: frame_rects merges co-located markers into one cluster,
    # which would hide the per-marker clamping result
    marks = [marker(nx=0.15 + 0.08 * i, ny=0.20 + 0.08 * i, label=f"M {i}",
                    dist=f"{10 + i}m", op=v) for i, v in enumerate(bad)]
    san = ov.sanitize_feed(feed(marks))
    assert san is not None and len(san["m"]) == len(bad)
    for m, v in zip(san["m"], bad):
        assert isinstance(m["op"], float) and 0.0 <= m["op"] <= 1.0, (v, m["op"])
    drawn = ov.frame_rects(san, smooth=0.0)
    # 0 and negatives stay hidden behind the op <= 0.01 gate; 1e9/inf/NaN/"x"/
    # None/True all fall back to fully opaque
    assert len(drawn) == len(bad) - 2, (len(drawn), len(bad))
    print(f"hostile op values {bad}: clamped, 2 hidden, {len(drawn)} drawn, "
          f"renderer survived")


def test_component_surfaces():
    """exact premultiplied pixels, before any sub-pixel compositing"""
    import cairo
    size = ov._marker_sizes({"sc": 1.0, "ic": 1.0})
    for op in (1.0, 0.45):
        plate_a = ov.layer_alpha(op, "plate")
        pin_a = ov.layer_alpha(op, "pin")
        border_a = ov.layer_alpha(op, "border")
        icon_a = ov.layer_alpha(op, "icon")
        dist_a = ov.layer_alpha(op, "dist")

        box = ov._box_bg(168, 42, 4, op)
        plate = find_px(box, stored(ov.BOX_BG, plate_a), min_hits=200)
        edge = find_px(box, stored(ov.PIN_BORDER, border_a,
                                   over=(ov.BOX_BG, plate_a)), tol=2, min_hits=20)
        old_edge = find_px(box, stored(ov.PIN_BORDER, 1.0))
        old_plate = find_px(box, stored(ov.BOX_BG, 1.0))
        z, s, f = alpha_counts(box)

        label = find_px(ov._text_surface("Signal - Part 1 (1/4)",
                                         size["label_font"], True, op)[0],
                        stored(ov.LABEL_COLOR, 1.0), min_hits=20)
        dist = find_px(ov._text_surface("123m", size["dist_font"], False, op)[0],
                       stored(ov.DIST_COLOR, dist_a), min_hits=10)

        kind = ov.kind_color("zone", None)
        pin = ov._pin_surf("zone", None, 84, 84, 168, size, op)
        fill = find_px(pin, stored(ov.PIN_BG, pin_a), min_hits=20)
        old_fill = find_px(pin, stored(ov.PIN_BG, 1.0))
        outline = (find_px(pin, stored(kind, border_a, over=(ov.PIN_BG, pin_a)), tol=3)
                   + find_px(pin, stored(kind, border_a), tol=3))

        # icon tier measured on its own canvas: strokes at the icon tier, the
        # sheet's accent fills at 0.25 of it (draw_icon's own ratio)
        accent_kind = ov.kind_color("airdrop", None)
        icon_surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 64, 64)
        ictx = cairo.Context(icon_surf)
        ictx.set_operator(cairo.OPERATOR_SOURCE)
        ictx.set_source_rgba(0, 0, 0, 0)
        ictx.paint()
        ictx.set_operator(cairo.OPERATOR_OVER)
        ov.draw_icon(ictx, "airdrop", accent_kind, 32, 32, 1.7, 1.0, icon_a)
        icon_stroke = find_px(icon_surf, stored(accent_kind, icon_a), tol=3, min_hits=10)
        icon_accent = find_px(icon_surf, stored(accent_kind, 0.25 * icon_a), tol=3, min_hits=5)

        print(f"components at op {op}: plate {plate} px @{stored(ov.BOX_BG, plate_a)}, "
              f"outline {edge} px @{stored(ov.PIN_BORDER, border_a, over=(ov.BOX_BG, plate_a))}, "
              f"pin fill {fill}, pin outline {outline}, label {label} px @A255, "
              f"distance {dist} px @A{round(255 * dist_a)}, icon strokes {icon_stroke} "
              f"@A{round(255 * icon_a)}, icon accent {icon_accent} "
              f"@A{round(255 * 0.25 * icon_a)}; tagbox alpha zero {z} semi {s} full {f}")
        assert plate >= 200 and edge >= 20 and label >= 20 and dist >= 10
        assert fill >= 20 and outline >= 10 and icon_stroke >= 10 and icon_accent >= 5
        if op == 1.0:
            assert old_edge >= 20 and f > 6000, "the opaque default look changed"
        else:
            # a translucent tagbox keeps nothing fully opaque inside - and the
            # old opaque outline/plate pixels must be gone, not merely dimmed
            assert old_edge == 0 and old_plate == 0 and f == 0, \
                (old_edge, old_plate, f)
            assert s > 6000, (z, s, f)
        assert plate_a <= border_a <= icon_a <= dist_a <= 1.0


def test_composite_levels():
    """marker surface: translucent plate, no leftover fully opaque plate"""
    m = {"k": "zone", "s": None, "x": 700.0, "y": 380.0, "sc": 1.0, "ic": 1.0,
         "l": "Signal - Part 1 (1/4)", "d": "123m"}
    opaque = ov.render_marker_cached({**m, "op": 1.0})[0]
    ghost = ov.render_marker_cached({**m, "op": 0.45})[0]
    z0, s0, f0 = alpha_counts(opaque)
    z1, s1, f1 = alpha_counts(ghost)
    plate0 = find_px(opaque, stored(ov.BOX_BG, 1.0), min_hits=200)
    plate1 = find_px(ghost, stored(ov.BOX_BG, ov.layer_alpha(0.45, "plate")),
                     min_hits=200)
    leftover = find_px(ghost, stored(ov.BOX_BG, 1.0))
    print(f"composite op 1.0: plate@A255 {plate0} px, alpha zero {z0} semi {s0} full {f0}")
    print(f"composite op 0.45: plate@A{round(255 * ov.layer_alpha(0.45, 'plate'))} "
          f"{plate1} px, leftover plate@A255 {leftover}, alpha zero {z1} semi {s1} full {f1}")
    assert plate0 >= 200 and plate1 >= 200
    assert leftover == 0, "semi-transparent marker still has fully opaque plate pixels"
    assert s1 > s0, "translucent marker has FEWER semi-transparent pixels than the opaque one"
    assert f1 > 0, "text and outline cores must stay fully opaque even at 0.45"
    assert max_alpha_of_hue(ghost, ov.LABEL_COLOR, tol=14) >= 240, "quest label faded"
    assert (opaque.get_width(), opaque.get_height()) == (ghost.get_width(), ghost.get_height()), \
        "opacity must not change the marker footprint"


def test_cfg_and_wire_opacity():
    """cfg markerAlpha and the wire's own op both drive the tiers"""
    opaque = rects(feed([marker()], {"markerAlpha": 1.0}))[0][2]
    via_cfg = rects(feed([marker()], {"markerAlpha": 0.4}))[0][2]
    via_wire = rects(feed([marker(op=0.4)], {"markerAlpha": 1.0}))[0][2]
    both = rects(feed([marker(op=0.4)], {"markerAlpha": 0.4}))[0][2]
    a40 = ov.layer_alpha(0.4, "plate")
    a16 = ov.layer_alpha(0.16, "plate")
    assert find_px(opaque, stored(ov.BOX_BG, 1.0), min_hits=200) >= 200
    assert find_px(via_cfg, stored(ov.BOX_BG, a40), min_hits=200) >= 200
    assert find_px(via_wire, stored(ov.BOX_BG, a40), min_hits=200) >= 200
    assert find_px(both, stored(ov.BOX_BG, a16), min_hits=200) >= 200
    # nothing of the old fully opaque plate survives, while the label text keeps
    # fully opaque pixels (tiers verified exactly at component level above)
    assert find_px(both, stored(ov.BOX_BG, 1.0)) == 0
    assert find_px(via_cfg, stored(ov.BOX_BG, 1.0)) == 0
    # the quest label is the layer that must survive: peak alpha of label-hue
    # pixels in the composited rect. Sub-pixel anchoring resamples glyph cores,
    # so a moving marker never shows exactly 255 - 240+ is the honest bar
    # (measured: 246-253 at opacities 1.0 down to 0.16).
    for tag, surf in (("opaque", opaque), ("cfg", via_cfg), ("wire", via_wire),
                      ("stacked", both)):
        peak = max_alpha_of_hue(surf, ov.LABEL_COLOR, tol=14)
        assert peak >= 240, (tag, peak)
        print(f"  {tag}: label peak A {peak}, "
              f"alpha counts {alpha_counts(surf)}")
    assert alpha_counts(both)[1] > alpha_counts(opaque)[1]
    print("opacity sources: cfg markerAlpha, wire op and their product tier "
          f"correctly (plate A255 / A{round(255 * a40)} / A{round(255 * a40)} / "
          f"A{round(255 * a16)})")


def test_cache_buckets():
    size = ov._marker_sizes({"sc": 1.0, "ic": 1.0})
    text, font_l, font_d = "Signal - Part 1 (1/4)", size["label_font"], size["dist_font"]
    before = len(ov._TEXT_CACHE)
    for _ in range(50):
        ov._text_surface(text, font_l, True, 0.42)
    grew = len(ov._TEXT_CACHE) - before
    assert grew <= 1, f"text cache churns: {grew} new entries for 50 calls"
    l1 = ov._text_surface(text, font_l, True, 0.4)[0]
    l2 = ov._text_surface(text, font_l, True, 1.0)[0]
    assert l1 is l2, "quest label must not fade - same surface expected"
    d1 = ov._text_surface("123m", font_d, False, 0.4)[0]
    d2 = ov._text_surface("123m", font_d, False, 1.0)[0]
    assert d1 is not d2, "distance text tiers must not share a surface"
    assert ov._box_bg(140, 36, 4, 0.4) is ov._box_bg(140, 36, 4, 0.4)
    assert ov._box_bg(140, 36, 4, 0.4) is not ov._box_bg(140, 36, 4, 0.9)
    assert ov._box_bg(140, 36, 4, 0.4) is not ov._box_bg(141, 36, 4, 0.4)
    p1 = ov._pin_surf("zone", None, 70, 70, 140, size, 0.4)
    p2 = ov._pin_surf("zone", None, 70, 70, 140, size, 0.4)
    p3 = ov._pin_surf("zone", None, 70, 70, 140, size, 1.0)
    assert p1 is p2 and p1 is not p3
    print(f"caches: 50 repeated calls added {grew} entry, label shared across "
          f"0.4/1.0, plate+pin differ per bucket")


def test_disjoint_footprints_with_alpha():
    """a cluster AND spread-out markers: presenter memcpy must stay exact"""
    clustered = [marker(nx=0.40 + 0.006 * i, ny=0.30 + 0.006 * i,
                        label=f"Quest {i} ({i + 1}/4)", dist=f"{40 + i}m")
                 for i in range(4)]
    apart = [marker(nx=nx, ny=ny, label=f"Extract {i}", dist=f"{500 + i}m", k="extract")
             for i, (nx, ny) in enumerate(((0.08, 0.12), (0.90, 0.14), (0.10, 0.86)))]
    rs = rects(feed(clustered + apart, {"markerAlpha": 0.45}))
    assert len(rs) >= 3, len(rs)
    boxes = [(rx, ry, s.get_width(), s.get_height()) for rx, ry, s in rs]

    def overlap(a, b):
        return not (a[0] + a[2] <= b[0] or b[0] + b[2] <= a[0]
                    or a[1] + a[3] <= b[1] or b[1] + b[3] <= a[1])

    clashes = [(i, j) for i in range(len(boxes)) for j in range(i + 1, len(boxes))
               if overlap(boxes[i], boxes[j])]
    print(f"cluster + spread frame: {len(rs)} rects, {len(clashes)} bbox overlaps "
          f"(0 expected after merge)")
    assert not clashes, clashes[:5]
    assert any(find_px(s, stored(ov.BOX_BG, ov.layer_alpha(0.45, "plate")),
                       min_hits=200) >= 200 for _, _, s in rs), \
        "cluster lost the translucent plate"


def test_edge_arrows_follow_opacity():
    off = marker(nx=1.42, ny=0.44, k="extract", label="Extract: CrossRoad",
                 dist="842m", s="locked")
    solid = rects(feed([off], {"markerAlpha": 1.0}))
    ghost = rects(feed([off], {"markerAlpha": 0.4}))
    assert len(solid) == 1 and len(ghost) == 1, (len(solid), len(ghost))
    s_surf, g_surf = solid[0][2], ghost[0][2]
    assert (s_surf.get_width(), s_surf.get_height()) == (g_surf.get_width(), g_surf.get_height()), \
        "opacity must not change the arrow footprint"
    kind = ov.kind_color("extract", "locked")
    a1, a4 = ov.layer_alpha(1.0, "border"), ov.layer_alpha(0.4, "border")
    fill1 = find_px(s_surf, stored(kind, a1), tol=8, min_hits=20)
    fill4 = find_px(g_surf, stored(kind, a4), tol=8, min_hits=20)
    plate4 = find_px(g_surf, stored(ov.BOX_BG, ov.layer_alpha(0.4, "plate")),
                     min_hits=20)
    print(f"edge arrow {s_surf.get_width()}x{s_surf.get_height()}: arrow fill "
          f"{fill1} px @A{round(255 * a1)} -> {fill4} px @A{round(255 * a4)}, "
          f"ghost distance plate {plate4} px")
    assert fill1 >= 20 and fill4 >= 20 and plate4 >= 20


if __name__ == "__main__":
    test_layer_tiers()
    test_hostile_op_values()
    test_component_surfaces()
    test_composite_levels()
    test_cfg_and_wire_opacity()
    test_cache_buckets()
    test_disjoint_footprints_with_alpha()
    test_edge_arrows_follow_opacity()
    print("RESULT: PASS")
