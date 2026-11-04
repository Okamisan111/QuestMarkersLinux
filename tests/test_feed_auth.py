"""Feed-auth battery (client side of the ws:// feed security).

Overlay-side enforcement is tested here against mock servers that mirror the
plugin frame shape:
  * tok-stamped frames  -> accepted (positive path)
  * frames without tok  -> all dropped, pump dials the server a second time
  * no feedToken configured -> tokless frames accepted (legacy compatibility)

The plugin-side handshake rules (Origin-header rejection, URI ?token= with
constant-time compare, header caps) live in plugin/src/WsServer.cs and are
proven against the deployed DLL via the mono harness, not from Python.

Run: .venv/bin/python3 tests/test_feed_auth.py
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import overlay  # noqa: E402
import websockets  # noqa: E402

TOK = "test-token-0123456789abcdef"
P_GOOD, P_BAD, P_LEGACY = 19401, 19402, 19403
checks = []


def frame(tok=None):
    f = {"t": int(time.time() * 1000), "v": [2560, 1440], "cfg": {},
         "m": [{"k": "Quest", "nx": 0.5, "ny": 0.5, "b": 0, "op": 1,
                "l": "Feed Auth Battery", "d": 40}]}
    if tok is not None:
        f["tok"] = tok
    return f


def check(name, passed, detail=""):
    checks.append(passed)
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))


async def serve(handler, port):
    serve_obj = await websockets.serve(handler, "127.0.0.1", port)
    return serve_obj


def make_server(frames_fn, conns):
    async def handler(ws):
        conns.append(1)
        try:
            while True:
                await ws.send(json.dumps(frames_fn()))
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
    # 1) positive: plugin-shaped tok-stamped frames accepted
    conns_good = []
    s1 = await serve(make_server(lambda: frame(TOK), conns_good), P_GOOD)
    get1 = overlay.make_feed_provider(f"ws://127.0.0.1:{P_GOOD}", None, TOK)
    got = await collect(get1, 2.0)
    check("tok-stamped frames accepted", len(got) > 0 and
          all(f.get("tok") == TOK for f in got), f"{len(got)} frames")
    s1.close()
    await s1.wait_closed()

    # 2) negative: tokless frames all dropped, pump redials (>=2 connects)
    conns_bad = []
    s2 = await serve(make_server(lambda: frame(None), conns_bad), P_BAD)
    get2 = overlay.make_feed_provider(f"ws://127.0.0.1:{P_BAD}", None, TOK)
    got = await collect(get2, 5.0)
    check("squatter frames dropped + pump redials",
          len(got) == 0 and len(conns_bad) >= 2,
          f"accepted={len(got)} connects={len(conns_bad)}")
    s2.close()
    await s2.wait_closed()

    # 2b) wrong tok is also rejected
    conns_wrong = []
    s2b = await serve(make_server(lambda: frame("not-our-token"), conns_wrong),
                      P_BAD + 10)
    get2b = overlay.make_feed_provider(f"ws://127.0.0.1:{P_BAD + 10}", None, TOK)
    got = await collect(get2b, 5.0)
    check("wrong-tok frames dropped + pump redials",
          len(got) == 0 and len(conns_wrong) >= 2,
          f"accepted={len(got)} connects={len(conns_wrong)}")
    s2b.close()
    await s2b.wait_closed()

    # 3) legacy: no feedToken configured -> tokless feed keeps working
    conns_legacy = []
    s3 = await serve(make_server(lambda: frame(None), conns_legacy), P_LEGACY)
    get3 = overlay.make_feed_provider(f"ws://127.0.0.1:{P_LEGACY}", None, "")
    got = await collect(get3, 2.0)
    check("no feedToken -> tokless feed still works", len(got) > 0,
          f"{len(got)} frames")
    s3.close()
    await s3.wait_closed()

    n_ok = sum(1 for c in checks if c)
    print(f"RESULT: {'PASS' if n_ok == len(checks) else 'FAIL'} "
          f"({n_ok}/{len(checks)})")
    return 0 if n_ok == len(checks) else 1


sys.exit(asyncio.run(main()))
