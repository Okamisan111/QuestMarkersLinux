# Changelog

## v1.1.0 — 2026-11-04

Protocol **breaking** release: the overlay feed now uses mutual challenge
authentication (feed protocol v3). Plugin 1.1.x and overlay 1.1.x must be
used together; a 1.0.x overlay cannot authenticate against a 1.1 plugin and
vice versa (it redials instead of accepting frames).

### Feed security (v3 challenge/response)
- The token is never transmitted anymore: not in the connect URL, not in a
  header, not per frame. After the WebSocket upgrade the server sends
  `{"c":"chal","n":<nonce>,"p":HMAC-SHA256(key=token, msg=n)}`; the overlay
  VERIFIES `p` (only the real plugin can produce it - a process squatting
  port 9378 is cut off before it injects a single frame) and answers
  `{"c":"auth","r":<same HMAC>}` within 8 s. Both compares are constant-time.
- Frames still carry a `tok` stamp; the overlay compares it with
  `hmac.compare_digest` (frame-level integrity).
- A ws:// feed WITHOUT a token is now refused by the overlay unless
  `--insecure` is given explicitly (dev/test against a plugin with an empty
  Feed Token).
- Mid-raid token edits are a single source of truth now: the plugin captures
  Feed Token/Feed Port once at game start and authenticates AND stamps frames
  from that one snapshot - a live-edited cfg can no longer desync the two
  sides. A token change still requires: game closed -> edit both sides ->
  start game -> start overlay.

### Server hardening (WsServer.cs)
- Every client runs on its own thread; a silent connection can no longer
  stall the accept loop or occupy the client slot (slot is taken only after
  successful challenge, 8 s auth deadline, 60 s idle timeout).
- Takeover now actually works: the newest AUTHENTICATED client replaces the
  old one; unauthenticated connections evict nothing.
- Handshake deadline (12 s) against slow-loris header drips; header byte/line
  caps unchanged; MASK bit and extended-length parsing fixed (EOF can no
  longer be mistaken for a length byte; unmasked client frames rejected).
- `Feed Hz` stays 60 default in the cfg; overlay default hz stays 120.

### Overlay / presenter hardening
- Sanitize closes its DoS gaps: per-marker `sc`/`ic` clamped 0.1..8, edge
  `w`/`h` clamped 1..4096 and `sc` clamped, non-string `s` states dropped
  (they used to crash the smoothing key tuple with TypeError).
- Oversized merged rects are now SPLIT into row-aligned sub-rects at the
  surface size (the old code only warned - and its `break` merely ended the
  warning loop while the oversized rect still desynced the presenter pipe).
- Presenter rect cap derived from the live surface (`st.frame_bytes`) instead
  of a flat 17 MiB (legit 4K frames up to 33 MB pass, hostile sums capped);
  `stride < 4*w` headers rejected before any memcpy.
- X11 feed loop guards every frame (one broken frame no longer kills the
  loop); window teardown moved into `finally` (a crash cannot leave an
  invisible fullscreen override window mapped).

### Tooling
- New `tests/test_plugin_ws.py` + `tests/plugin/` harness: compiles the
  production `WsServer.cs` unchanged (no Unity/game DLLs) and proves the
  whole protocol from the server side (challenge, wrong/missing answers,
  Origin 403, header caps, ping/pong, takeover, pending-connection
  non-eviction).
- GitHub Actions CI (`.github/workflows/ci.yml`): presenter build, harness
  build, all five test batteries, presenter artifact + sha256.
- `test_feed_auth.py` rewritten for v3 mocks; hardening battery extended
  (sc/ic/edge clamps, split_surface, per-frame payload caps).

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
