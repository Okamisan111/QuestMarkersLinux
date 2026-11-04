#!/usr/bin/env python3
"""Feed-WebSocket protocol integration tests (protocol v4) against the REAL
production server code.

tests/plugin/Harness.cs compiles plugin/src/WsServer.cs UNCHANGED (no Unity,
no game DLLs) into a small console harness. These tests hit it with the same
client semantics the overlay uses and prove the server side of the feed
security: challenge/response authentication, rejection of wrong or missing
answers, browser Origin rejection, handshake header caps, ping/pong, the
authenticated takeover, and that an unauthenticated connection never occupies
or evicts the client slot.

(The plugin DLL itself references BepInEx/UnityEngine and is therefore not
buildable in CI; the harness is the protocol-identical stand-in.)

Run: .venv/bin/python3 tests/test_plugin_ws.py     (needs `dotnet` on PATH)
"""
import asyncio
import hashlib
import hmac
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 19411
TOKEN = "HARNESS-TEST-TOKEN-0000000000000000"   # test fixture, not a secret
URL = f"ws://127.0.0.1:{PORT}/"
BIN = os.path.join(ROOT, "tests", "plugin", "bin", "Release", "net8.0", "ws-harness")

checks = []
server_log = []
log_lock = threading.Lock()


def check(name, passed, detail=""):
    checks.append(bool(passed))
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}" + (f": {detail}" if detail and not passed else ""))


def server_proof(nonce):
    """What the server must send as p: HMAC over the server-side domain."""
    return hmac.new(TOKEN.encode(), ("server|" + nonce).encode(),
                    hashlib.sha256).hexdigest()


def client_proof(nonce):
    """What the client must answer as r: a DIFFERENT derivation, so echoing
    the server's own p back does not authenticate."""
    return hmac.new(TOKEN.encode(), ("client|" + nonce).encode(),
                    hashlib.sha256).hexdigest()


def frame_stamp(feed_time):
    """Per-frame tok stamp: HMAC over the frame's own timestamp."""
    return hmac.new(TOKEN.encode(), str(feed_time).encode(),
                    hashlib.sha256).hexdigest()


def logged(sub):
    with log_lock:
        return any(sub in line for line in server_log)


def build():
    if shutil.which("dotnet") is None:
        # Never "skip" a security battery silently: CI would stay green while
        # the whole server-side auth/origin/deadline coverage ran zero checks.
        print("FAIL: dotnet not installed - the plugin WS battery cannot run")
        print("RESULT: FAIL (0/0)")
        sys.exit(1)
    subprocess.run(["dotnet", "build", "tests/plugin/WsHarness.csproj", "-c", "Release", "-v", "q"],
                   cwd=ROOT, check=True)


def start_server():
    proc = subprocess.Popen([BIN], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert proc.stdout is not None
    ready = threading.Event()

    def pump():
        # collect EVERY server log line for the whole run (the log assertions
        # for refusal/takeover/Origin events prove the server semantics)
        for line in proc.stdout:
            with log_lock:
                server_log.append(line.strip())
            if "HARNESS-READY" in line:
                ready.set()
    t = threading.Thread(target=pump, daemon=True)
    t.start()
    if not ready.wait(timeout=20):
        print("harness server did not start")
        sys.exit(1)
    return proc


async def read_challenge(ws):
    raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
    chal = json.loads(raw)
    ok_shape = (isinstance(chal, dict) and chal.get("c") == "chal"
                and isinstance(chal.get("n"), str) and isinstance(chal.get("p"), str))
    return chal, raw, ok_shape


async def authenticate(ws):
    chal, raw, ok_shape = await read_challenge(ws)
    assert ok_shape, "malformed challenge"
    assert server_proof(chal["n"]) == chal["p"], "server proof invalid"
    await ws.send(json.dumps({"c": "auth", "r": client_proof(chal["n"])}))


async def recv_frame(ws, timeout=3.0):
    return json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))


async def test_full_auth():
    async with websockets.connect(URL, close_timeout=1) as ws:
        await authenticate(ws)
        got, raws = [], []
        for _ in range(5):
            raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
            raws.append(raw)
            got.append(json.loads(raw))
        check("authenticated client receives stamped frames",
              len(got) == 5 and all("m" in f for f in got))
        check("frame stamp is HMAC over the frame timestamp, not the token",
              all(f.get("tok") == frame_stamp(f.get("t")) for f in got)
              and all(f.get("tok") != TOKEN for f in got))
        check("token never appears in any frame on the wire",
              all(TOKEN not in raw for raw in raws))
        check("server logs authenticated feed",
              logged("feed token verified via challenge"))


async def test_reflection_rejected():
    """A client that echoes the server's own proof must NOT be admitted -
    the regression that made the v3 challenge authenticate nobody."""
    async with websockets.connect(URL, close_timeout=1) as ws:
        chal, _, ok_shape = await read_challenge(ws)
        assert ok_shape
        # zero token knowledge: send the server's own p back as the answer
        await ws.send(json.dumps({"c": "auth", "r": chal["p"]}))
        admitted = False
        try:
            await asyncio.wait_for(ws.recv(), timeout=3.0)
            admitted = True
        except ConnectionClosed:
            admitted = False
        check("echoed server proof is refused (no token knowledge)", not admitted)


async def test_wrong_answer():
    async with websockets.connect(URL, close_timeout=1) as ws:
        chal, _, ok_shape = await read_challenge(ws)
        assert ok_shape
        # the server PROVED knowledge of the token (proof verifies)...
        good_proof = server_proof(chal["n"]) == chal["p"]
        # ...so answer with a WRONG proof on purpose:
        await ws.send(json.dumps({"c": "auth", "r": "deadbeef" * 8}))
        refused = False
        try:
            await asyncio.wait_for(ws.recv(), timeout=3.0)
        except ConnectionClosed:
            refused = True
        check("wrong challenge answer is refused before any frame",
              good_proof and refused and logged("challenge response missing or wrong"))


async def test_missing_answer():
    t0 = time.time()
    async with websockets.connect(URL, close_timeout=1) as ws:
        await read_challenge(ws)
        closed = False
        try:
            while time.time() - t0 < 13.0:
                await asyncio.wait_for(ws.recv(), timeout=13.0 - (time.time() - t0))
        except ConnectionClosed:
            closed = True
    dt = time.time() - t0
    check("silent client dropped by 8 s auth deadline (slot never held)",
          closed and 7.0 < dt < 12.0 and logged("challenge response missing or wrong"),
          f"after {dt:.1f}s")


async def test_origin_rejected():
    """A browser-Origin handshake must be refused with HTTP 403 BEFORE the
    upgrade. Only the status code counts: a server that answers 101 and then
    drops the connection has accepted the handshake and must FAIL here."""
    status = None
    upgraded = False
    try:
        async with websockets.connect(URL, close_timeout=1,
                                      additional_headers={"Origin": "http://127.0.0.1:8080"}) as ws:
            upgraded = True
            await asyncio.wait_for(ws.recv(), timeout=3.0)
    except InvalidStatus as e:
        status = e.response.status_code
    except ConnectionClosed:
        status = None
    check("browser Origin handshake rejected with 403",
          status == 403 and not upgraded and logged("browser Origin header present"),
          f"status={status} upgraded={upgraded}")


def test_oversized_headers():
    s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    try:
        s.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZWQ=\r\n"
                  b"X-Junk: " + b"a" * 9000 + b"\r\n\r\n")
        data = s.recv(4096)
        check("oversized handshake headers: no 101, connection closed",
              b"101" not in data and data == b"")
    finally:
        s.close()
    time.sleep(0.3)
    check("oversized headers logged", logged("oversized request headers"))


async def test_ping_pong():
    async with websockets.connect(URL, close_timeout=1, ping_interval=None) as ws:
        await authenticate(ws)
        pong_ok = False
        try:
            await asyncio.wait_for(ws.ping(), timeout=3.0)  # resolves when pong arrives
            pong_ok = True
        except (TimeoutError, asyncio.TimeoutError, ConnectionClosed):
            pass
        f = await recv_frame(ws)  # connection still healthy after the ping
        check("server pongs ping frames (RFC6455 5.5.3)", pong_ok and "m" in f)


async def test_takeover():
    a = await websockets.connect(URL, close_timeout=1)
    await authenticate(a)
    await recv_frame(a)
    b = await websockets.connect(URL, close_timeout=1)
    await authenticate(b)
    fb = await recv_frame(b)
    evicted = False
    try:
        while True:
            await asyncio.wait_for(a.recv(), timeout=5.0)
    except ConnectionClosed:
        evicted = True
    a_close_ok = a.close_code in (1000, 1005, 1006, None)
    await b.close()
    check("newest AUTHENTICATED client replaces the old one (takeover works)",
          "m" in fb and evicted and logged("takeover by the newest client") and a_close_ok,
          f"a close code {a.close_code}")


async def test_pending_client_cannot_evict():
    a = await websockets.connect(URL, close_timeout=1)
    await authenticate(a)
    pend = await websockets.connect(URL, close_timeout=1)
    await read_challenge(pend)  # connected, challenge received - never answers
    frames, evicted = 0, False
    t0 = time.time()
    try:
        while time.time() - t0 < 4.0:
            await recv_frame(a, timeout=2.5)
            frames += 1
    except ConnectionClosed:
        evicted = True
    # a pending, unauthenticated connection must not have touched the slot;
    # after it disconnects the authenticated feed is still healthy
    await pend.close()
    healthy = False
    try:
        await recv_frame(a, timeout=2.5)
        healthy = True
    except ConnectionClosed:
        pass
    await a.close()
    check("unauthenticated pending connection evicts nothing",
          frames >= 50 and not evicted and healthy,
          f"frames={frames} evicted={evicted} healthy={healthy}")


def plugin_frame_head_unconditional():
    """BuildFrame must write the frame head BEFORE the projection block.

    The head (t/v/cfg/tok) used to live inside `if (camera != null &&
    targets != null)`, so every idle frame - exactly what the auto-hide timer
    and the toggle key produce - started with a comma:  ,"m":[]}
    That is not JSON. The overlay dropped those frames silently, the feed
    timestamp stopped advancing, and the last markers stayed frozen on screen
    for the full 1 s staleness window before the display cleared.
    """
    src = open(os.path.join(ROOT, "plugin", "src", "Plugin.cs"),
               encoding="utf-8").read()
    body = src[src.index("private void BuildFrame"):]
    body = body[:body.index("\n    }")]
    head = body.find('sb.Append("{\\"t\\":"')
    cond = body.find("if (camera != null && targets != null)")
    tail = body.find('sb.Append(",\\"m\\":["')
    check("BuildFrame writes the frame head unconditionally (idle frames parse)",
          -1 < head < cond < tail, f"head@{head} cond@{cond} m-array@{tail}")


async def main():
    build()
    plugin_frame_head_unconditional()
    proc = start_server()
    try:
        await test_full_auth()
        await test_reflection_rejected()
        await test_wrong_answer()
        await test_missing_answer()
        await test_origin_rejected()
        test_oversized_headers()
        await test_ping_pong()
        await test_takeover()
        await test_pending_client_cannot_evict()
        # slow-loris handshake deadline (12 s) is implemented in WsServer; it is
        # not exercised here to keep the test suite fast (missing_answer proves
        # the auth deadline path).
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    n_ok = sum(1 for c in checks if c)
    print(f"RESULT: {'PASS' if n_ok == len(checks) else 'FAIL'} "
          f"({n_ok}/{len(checks)})")
    return 0 if n_ok == len(checks) else 1


sys.exit(asyncio.run(main()))
