"""Feed-auth battery (overlay side of the ws:// feed security, protocol v3).

Mock servers mirror the plugin's challenge handshake:
  * good server:          challenge with a valid token proof, client answers,
                          tok-stamped frames -> accepted
  * wrong-proof server:   cannot prove token knowledge -> overlay must refuse
                          AND must not send its own answer (no proof leak)
  * no-challenge server:  port squatter / pre-v3 feed -> refused, redial
  * tok-mismatch server:  authenticates but stamps frames with a foreign token
                          -> frames dropped, redial
  * tokenless overlay:    no challenge -> tokless feed accepted (dev/file compat)
Plus: the connect URL never carries the token, and the overlay refuses a
ws:// feed without a token unless --insecure is given.

The plugin side (identical WsServer.cs code as in production) is proven by
tests/test_plugin_ws.py against the real C# server.

Run: .venv/bin/python3 tests/test_feed_auth.py
"""
import asyncio
import hashlib
import hmac
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
checks = []


def proof(nonce):
    return hmac.new(TOK.encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256).hexdigest()


def frame(tok=TOK):
    f = {"t": int(time.time() * 1000), "v": [2560, 1440], "cfg": {},
         "m": [{"k": "Quest", "nx": 0.5, "ny": 0.5, "b": 0, "op": 1,
                "l": "Feed Auth Battery", "d": 40}]}
    if tok is not None:
        f["tok"] = tok
    return f


def check(name, passed, detail=""):
    checks.append(bool(passed))
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))


async def serve(handler, port):
    return await websockets.serve(handler, "127.0.0.1", port)


def make_server(conns, paths=None, auth_seen=None, mode="good", frame_tok=TOK):
    """mode: good | wrong_proof | no_challenge | bad_frames(handshake fine,
    frames carry a foreign tok)."""
    async def handler(ws):
        conns.append(1)
        if paths is not None:
            req = getattr(ws, "request", None)
            paths.append(getattr(req, "path", "") if req is not None else "")
        if mode != "no_challenge":
            nonce = secrets.token_hex(16)
            p = proof(nonce) if mode != "wrong_proof" else "0" * 64
            await ws.send(json.dumps({"c": "chal", "n": nonce, "p": p}))
            try:
                reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=3.0))
                ok = (isinstance(reply, dict) and reply.get("c") == "auth"
                      and hmac.compare_digest(str(reply.get("r", "")), proof(nonce)))
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


async def main():
    # 1) positive path: challenge-authenticated feed with tok-stamped frames
    conns, paths, auth = [], [], []
    s1 = await serve(make_server(conns, paths, auth, mode="good"), P_GOOD)
    get1 = overlay.make_feed_provider(f"ws://127.0.0.1:{P_GOOD}", None, TOK)
    got = await collect(get1, 2.0)
    check("challenge-authenticated feed accepted", len(got) > 0
          and all(f.get("tok") == TOK for f in got), f"{len(got)} frames")
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

    n_ok = sum(1 for c in checks if c)
    print(f"RESULT: {'PASS' if n_ok == len(checks) else 'FAIL'} "
          f"({n_ok}/{len(checks)})")
    return 0 if n_ok == len(checks) else 1


sys.exit(asyncio.run(main()))
