# Changelog

## v1.0.0 — 2026-11-04

Initial public release.

### Plugin (BepInEx, SPT)
- Streams quest / extract / airdrop / BTR markers over a token-authenticated
  loopback WebSocket (feed v2: normalized screen coords + behind flag,
  game-FPS-aligned sampling, Feed Hz 1-180, default 60)
- Toggle key + second hotkey with per-key marker sets, auto-show in raid,
  peek auto-hide, distance/marker caps, imperial units, byte-budgeted frames

### Overlay (Python)
- maschine-style teardrop pins, kind icons, label/tag boxes, `885m`/`1.1km`
  and `4921ft`/`1.2mi` distance formatting
- Edge arrows for off-screen/behind targets (never mirrored in the center)
- Delayed-chord position smoother (no EMA micro-stop stutter near targets)
- Overlap compositing (closest marker wins, no rectangular occlusion)
- Scaling: Marker Scale, Far Distance/Far Scale, Icon Scale
- Feed-freshness safety: markers clear within 1 s if the game feed stops

### Presenter
- Native Wayland layer-shell (per-pixel alpha, click-through, dirty-rect
  updates, KWin fractional-scale/mode-change aware)
- X11 fallback with depth-32 probe and ShapeClip fallback

### Feed security
- Browser-Origin handshakes rejected (403), wrong token rejected (401)
- Per-frame token check on the overlay side; token never appears in logs
- Frame/label/rect hardening on both feed directions

### Self-tests
- `overlay.py --test` (presenter proofs) plus `tests/` batteries:
  feed auth, feed hardening, frame rects, smoothing regression

### Tested
- Live SPT raids on Wayland/KWin 2560x1440 (incl. fractional scaling and
  mode-churn), X11/XWayland fallback, Wayland and X11 presenter paths.
