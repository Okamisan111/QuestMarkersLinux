"""Feed-auth battery (overlay side of the ws:// feed security, protocol v4).

Mock servers mirror the plugin's challenge handshake:
  * good server:          challenge with a valid server-side HMAC proof, client
                          answers with the client-side proof, frames carry a
                          per-frame HMAC stamp -> accepted
  * wrong-proof server:   cannot prove token knowledge -> overlay must refuse
                          AND must not send its own answer (no proof leak)
  * no-challenge server:  port squatter / pre-v4 feed -> refused, redial
  * foreign-stamp server: authenticates but stamps frames with a foreign value
                          -> frames dropped, redial
  * unstamped server:     authenticates but omits the stamp -> frames dropped
                          (fail closed, not fail open)
  * tokenless overlay:    no challenge -> tokless feed accepted (dev/file compat)
Plus: the connect URL and every frame stay free of the shared token, and the
overlay refuses a ws:// feed without a token unless --insecure is given.

The plugin side (identical WsServer.cs code as in production) is proven by
tests/test_plugin_ws.py against the real C# server.

Run: .venv/bin/python3 tests/test_feed_auth.py
"""
import asyncio
import contextlib
import hashlib
import hmac
import io
import json
import os
import secrets
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import overlay  # noqa: E402
import websockets  # noqa: E402

TOK = "test-token-0123456789abcdef"
P_GOOD, P_WRONG, P_NOCHAL, P_BADFR, P_LEGACY = 19401, 19402, 19403, 19404, 19405
P_NOSTAMP = 19406
P_IDLE, P_BADJSON = 19407, 19408
checks = []


def srv_proof(nonce):
    """The server's proof (p) - HMAC over the server-side domain."""
    return hmac.new(TOK.encode("utf-8"), ("server|" + nonce).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def cli_proof(nonce):
    """The client's answer (r) - a DIFFERENT derivation, so echoing p fails."""
    return hmac.new(TOK.encode("utf-8"), ("client|" + nonce).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def stamp(t):
    """Per-frame tok stamp over the frame's own timestamp (protocol v4)."""
    return hmac.new(TOK.encode("utf-8"), str(t).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def frame(tok: "str | None" = "good", t=None):
    t = int(time.time() * 1000) if t is None else t
    f = {"t": t, "v": [2560, 1440], "cfg": {},
         "m": [{"k": "Quest", "nx": 0.5, "ny": 0.5, "b": 0, "op": 1,
                "l": "Feed Auth Battery", "d": 40}]}
    if tok == "good":
        f["tok"] = stamp(t)
    elif tok is not None:
        f["tok"] = tok
    return f


def check(name, passed, detail=""):
    checks.append(bool(passed))
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))


async def serve(handler, port):
    return await websockets.serve(handler, "127.0.0.1", port)


def make_server(conns, paths=None, auth_seen=None, mode="good",
                frame_tok: "str | None" = "good"):
    """mode: good | wrong_proof | no_challenge. frame_tok: "good" (valid HMAC
    stamp), None (no stamp at all), or a literal foreign value."""
    async def handler(ws):
        conns.append(1)
        if paths is not None:
            req = getattr(ws, "request", None)
            paths.append(getattr(req, "path", "") if req is not None else "")
        if mode != "no_challenge":
            nonce = secrets.token_hex(16)
            p = srv_proof(nonce) if mode != "wrong_proof" else "0" * 64
            await ws.send(json.dumps({"c": "chal", "n": nonce, "p": p}))
            try:
                reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=3.0))
                ok = (isinstance(reply, dict) and reply.get("c") == "auth"
                      and hmac.compare_digest(str(reply.get("r", "")), cli_proof(nonce)))
            except (TimeoutError, asyncio.TimeoutError, json.JSONDecodeError,
                    websockets.exceptions.ConnectionClosed):
                ok = False
            if auth_seen is not None:
                auth_seen.append(ok)
            if mode == "wrong_proof":
                # overlay must have refused BEFORE answering - the server must
                # see no auth frame at all and must never reach the frame loop
                return
            if not ok:
                await ws.close(code=1008)
                return
        try:
            while True:
                await ws.send(json.dumps(frame(frame_tok)))
                await asyncio.sleep(0.03)
        except websockets.exceptions.ConnectionClosed:
            pass
    return handler


async def collect(get_feed, seconds):
    accepted = []
    deadline = time.time() + seconds
    while time.time() < deadline:
        f = get_feed()
        if f is not None:
            accepted.append(f)
        await asyncio.sleep(0.1)
    return accepted


async def collect_timed(get_feed, seconds, poll=0.005):
    """Like collect(), but keeps every sample (None included) with its arrival
    time - a None sample after a good frame is exactly what a frozen display
    looks like from the render loop's point of view."""
    out = []
    deadline = time.time() + seconds
    while time.time() < deadline:
        out.append((time.time(), get_feed()))
        await asyncio.sleep(poll)
    return out


async def main():
    # 1) positive path: challenge-authenticated feed with tok-stamped frames
    conns, paths, auth = [], [], []
    s1 = await serve(make_server(conns, paths, auth, mode="good"), P_GOOD)
    get1 = overlay.make_feed_provider(f"ws://127.0.0.1:{P_GOOD}", None, TOK)
    got = await collect(get1, 2.0)
    # NOTE: sanitize_feed() turns the frame's "t" into a float, so the stamp is
    # recomputed over int(t) - the exact string the plugin MACs.
    check("challenge-authenticated feed accepted", len(got) > 0
          and all(f.get("tok") == hmac.new(TOK.encode("utf-8"),
                                           str(int(f.get("t"))).encode("utf-8"),
                                           hashlib.sha256).hexdigest() for f in got),
          f"{len(got)} frames")
    check("shared token never appears in a frame",
          len(got) > 0 and all(TOK not in json.dumps(f) for f in got))
    check("client answered every challenge", auth and all(auth), str(auth))
    check("connect URL never carries the token", paths
          and all("token" not in p and TOK not in p for p in paths), str(paths))
    s1.close()
    await s1.wait_closed()

    # 2) server without token knowledge must be refused - and the overlay must
    # NOT send its own HMAC proof to such a server
    conns2, auth2 = [], []
    s2 = await serve(make_server(conns2, None, auth2, mode="wrong_proof"), P_WRONG)
    got = await collect(overlay.make_feed_provider(f"ws://127.0.0.1:{P_WRONG}", None, TOK), 5.0)
    check("server without token proof rejected, overlay sent no proof",
          len(got) == 0 and len(conns2) >= 2 and auth2 and not any(auth2),
          f"accepted={len(got)} connects={len(conns2)} auth_seen={auth2}")
    s2.close()
    await s2.wait_closed()

    # 3) squatter that never sends a challenge: refused, redial
    conns3 = []
    s3 = await serve(make_server(conns3, mode="no_challenge"), P_NOCHAL)
    got = await collect(overlay.make_feed_provider(f"ws://127.0.0.1:{P_NOCHAL}", None, TOK), 5.0)
    check("no-challenge server rejected + pump redials",
          len(got) == 0 and len(conns3) >= 2,
          f"accepted={len(got)} connects={len(conns3)}")
    s3.close()
    await s3.wait_closed()

    # 4) authenticated server stamping frames with a FOREIGN tok: frames dropped
    conns4, auth4 = [], []
    s4 = await serve(make_server(conns4, None, auth4, mode="good",
                                 frame_tok="not-our-token"), P_BADFR)
    got = await collect(overlay.make_feed_provider(f"ws://127.0.0.1:{P_BADFR}", None, TOK), 5.0)
    check("foreign frame tok dropped + pump redials",
          len(got) == 0 and len(conns4) >= 2 and all(auth4),
          f"accepted={len(got)} connects={len(conns4)} auth_seen={auth4}")
    s4.close()
    await s4.wait_closed()

    # 4b) authenticated server that omits the stamp entirely: frames must be
    # dropped (fail CLOSED - a missing stamp is not "no check needed")
    conns4b, auth4b = [], []
    s4b = await serve(make_server(conns4b, None, auth4b, mode="good",
                                  frame_tok=None), P_NOSTAMP)
    got = await collect(overlay.make_feed_provider(f"ws://127.0.0.1:{P_NOSTAMP}", None, TOK), 5.0)
    check("frames without a tok stamp dropped (fail closed)",
          len(got) == 0 and len(conns4b) >= 2 and all(auth4b),
          f"accepted={len(got)} connects={len(conns4b)} auth_seen={auth4b}")
    s4b.close()
    await s4b.wait_closed()

    # 5) tokenless overlay (dev/file-compat): no challenge, tokless frames in
    conns5 = []
    s5 = await serve(make_server(conns5, mode="no_challenge"), P_LEGACY)
    got = await collect(overlay.make_feed_provider(f"ws://127.0.0.1:{P_LEGACY}", None, ""), 2.0)
    check("no feedToken -> tokless feed still works", len(got) > 0, f"{len(got)} frames")
    s5.close()
    await s5.wait_closed()

    # 6) ws:// WITHOUT a token is refused outright (no --insecure given)
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "overlay.py"),
         "--feed", f"ws://127.0.0.1:{P_NOCHAL}",
         "--config", os.path.join(ROOT, ".this-config-does-not-exist.json")],
        capture_output=True, text=True, timeout=30, cwd=ROOT)
    out = proc.stdout + proc.stderr
    check("ws:// feed without token is refused (rc 2)",
          proc.returncode == 2 and "refusing an unauthenticated ws:// feed" in out,
          f"rc={proc.returncode}")

    # 7) idle frames (markers hidden by the toggle key / auto-hide) must be
    #    VALID frames. The plugin used to emit `,"m":[]}` (head missing) and
    #    the overlay froze the last markers for a second, because every idle
    #    frame failed to parse and the timestamp never advanced.
    idle_conns = []

    async def idle_handler(ws):
        idle_conns.append(1)
        nonce = secrets.token_hex(16)
        await ws.send(json.dumps({"c": "chal", "n": nonce, "p": srv_proof(nonce)}))
        await ws.recv()
        for _ in range(12):                      # markers visible ...
            await ws.send(json.dumps(frame("good")))
            await asyncio.sleep(1 / 60)
        while True:                              # ... then hidden
            f = frame("good")
            f["m"] = []
            await ws.send(json.dumps(f))
            await asyncio.sleep(1 / 60)

    s7 = await serve(idle_handler, P_IDLE)
    samples = await collect_timed(
        overlay.make_feed_provider(f"ws://127.0.0.1:{P_IDLE}", None, TOK), 1.4)
    marker_idx = [i for i, (_, f) in enumerate(samples) if f and f.get("m")]
    empty_idx = [i for i, (_, f) in enumerate(samples)
                 if f is not None and not f.get("m")]
    last_marker = marker_idx[-1] if marker_idx else -1
    gaps = [i for i in range(last_marker + 1, len(samples)) if samples[i][1] is None]
    delay = (samples[empty_idx[0]][0] - samples[last_marker][0]
             if empty_idx and last_marker >= 0 else -1)
    check("hidden markers clear at once (idle frames stay valid JSON)",
          bool(marker_idx) and bool(empty_idx) and not gaps and 0 <= delay < 0.30,
          f"marker->empty {delay:.2f}s, dropped samples={len(gaps)}")
    s7.close()
    await s7.wait_closed()

    # 8) a malformed frame must be REPORTED, must not drop the connection, and
    #    the feed must recover on the next valid frame (this is the diagnostic
    #    that would have made the auto-hide freeze visible in the log).
    bad_conns = []

    async def bad_handler(ws):
        bad_conns.append(1)
        nonce = secrets.token_hex(16)
        await ws.send(json.dumps({"c": "chal", "n": nonce, "p": srv_proof(nonce)}))
        await ws.recv()
        for _ in range(10):
            await ws.send(json.dumps(frame("good")))
            await asyncio.sleep(1 / 60)
        end = time.time() + 1.5
        while time.time() < end:                 # exactly what the old plugin sent
            await ws.send(',"m":[]}')
            await asyncio.sleep(1 / 60)
        while True:                              # recovery: valid frames again
            await ws.send(json.dumps(frame("good")))
            await asyncio.sleep(1 / 60)

    s8 = await serve(bad_handler, P_BADJSON)
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        feed8 = overlay.make_feed_provider(f"ws://127.0.0.1:{P_BADJSON}", None, TOK)
        samples8 = await collect_timed(feed8, 3.2)
    text = log.getvalue()
    check("malformed frames are reported, not swallowed",
          "malformed frame ignored" in text)
    check("malformed frames do not drop the connection",
          len(bad_conns) == 1, f"connects={len(bad_conns)}")
    check("last image holds ~1 s, then the feed recovers without a redial",
          any(f is None for _, f in samples8)
          and samples8[-1][1] is not None
          and bool(samples8[-1][1].get("m")))
    s8.close()
    await s8.wait_closed()

    n_ok = sum(1 for c in checks if c)
    print(f"RESULT: {'PASS' if n_ok == len(checks) else 'FAIL'} "
          f"({n_ok}/{len(checks)})")
    return 0 if n_ok == len(checks) else 1


sys.exit(asyncio.run(main()))
