"""Multi-monitor battery (run: .venv/bin/python3 tests/test_monitor.py).

Guards the monitor picking that decides WHERE the overlay is drawn:

1. probe_monitors() turns the presenter's --list JSON into sanitized dicts and
   survives junk: non-JSON lines, JSON that is not an object, a name of 400
   characters, tabs/newlines inside a description, negative sizes, scale 0,
   string numbers, null refresh, and a runner that explodes or lies;
2. the JSON shape the C presenter prints is the shape Python reads (the field
   names are pinned against the literal --list output, so a rename on either
   side fails here instead of silently returning no monitors);
3. resolve_monitor() maps every documented spec form ("1", "#1", "DP-1",
   "HDMI-A-1", a description fragment, "1280,0") onto exactly one monitor, and
   every ambiguous or unknown spec onto a message that names the alternatives;
4. choose_monitor() asks only when it must: never for one monitor, never for an
   explicit spec, never without a terminal (a launcher would hang on input());
   a typo in an explicit spec is an error, not a silent wrong screen;
5. the presenter really receives the selector: run_wayland_feed spawns it with
   the monitor as the fourth argument and keeps it across a respawn, while the
   legacy three-argument call stays working without a selector.

Nothing in here needs a compositor: the presenter is a fake subprocess and the
prompt is an injected function.
"""
import io
import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import overlay as ov  # noqa: E402

MONO = [{"index": 0, "name": "", "description": "LG Electronics LG ULTRAGEAR",
         "x": 0, "y": 0, "width": 2560, "height": 1440, "scale": 1,
         "refresh_mhz": 179960}]

DUAL = [
    {"index": 0, "name": "DP-1", "description": "LG Electronics LG ULTRAGEAR",
     "x": 0, "y": 0, "width": 2560, "height": 1440, "scale": 1,
     "refresh_mhz": 179960},
    {"index": 1, "name": "HDMI-A-1", "description": "AOC 24G2",
     "x": 2560, "y": 0, "width": 1920, "height": 1080, "scale": 1,
     "refresh_mhz": 74998},
]

# stacked layout: both outputs sit at x=0, only y differs - an x offset alone
# cannot tell them apart, which is why position/index selection exists
STACKED = [
    {"index": 0, "name": "DP-1", "description": "LG ULTRAGEAR",
     "x": 0, "y": 0, "width": 2560, "height": 1440, "scale": 1,
     "refresh_mhz": 179960},
    {"index": 1, "name": "DP-2", "description": "LG ULTRAGEAR",
     "x": 0, "y": 1440, "width": 2560, "height": 1440, "scale": 1,
     "refresh_mhz": 179960},
]


class Runner:
    """Stand-in for subprocess.run([presenter, '--list'], ...)."""

    def __init__(self, stdout=b"", exc=None, returncode=0):
        self.stdout = stdout
        self.exc = exc
        self.returncode = returncode
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if self.exc:
            raise self.exc
        return types.SimpleNamespace(stdout=self.stdout, returncode=self.returncode,
                                     stderr=b"")


def lines(rows):
    return "\n".join(json.dumps(r) for r in rows).encode() + b"\n"


# --- 1 ---------------------------------------------------------------------
def test_probe_parses_and_sanitizes():
    junk = (b"not json at all\n"
            b"\n"
            b'{"index":0,"name":"DP-1","description":"LG ULTRAGEAR","x":0,"y":0,'
            b'"width":2560,"height":1440,"scale":1,"refresh_mhz":179960}\n'
            b'[1,2,3]\n'
            b'{"index":1,"name":"' + b"u" * 400 + b'","description":"has\\tTab and\\nnewline",'
            b'"x":"2560","y":null,"width":-1920,"height":1080,"scale":0,'
            b'"refresh_mhz":null,"done":1}\n'
            b'{"index":"2","name":"","x":0,"y":0}\n')
    got = ov.probe_monitors(presenter="/bin/true", runner=Runner(stdout=junk))
    assert len(got) == 3, got
    good, odd, thin = got
    assert good["name"] == "DP-1" and good["width"] == 2560 and good["scale"] == 1
    assert odd["index"] == 1 and odd["name"] == "u" * 128, odd["name"]
    assert odd["description"] == "has Tab and newline", repr(odd["description"])
    assert odd["x"] == 2560, "string x must become an int"
    assert odd["y"] == 0 and odd["scale"] == 1 and odd["refresh_mhz"] == 0
    assert odd["width"] == 0 and odd["height"] == 1080, "negative sizes clamp to 0"
    assert thin["index"] == 2 and thin["name"] == "" and thin["description"] == ""
    # labels and choices survive the junk rows too
    assert ov.monitor_choice(good) == "DP-1"
    assert ov.monitor_choice(thin) == "#2"
    assert "2560x1440+0+0" in ov.monitor_label(good)
    assert "180 Hz" in ov.monitor_label(good) and "75 Hz" not in ov.monitor_label(good)
    assert "scale unknown" not in ov.monitor_label(thin)
    print("probe: 3 rows kept of 5, hostile values clamped, junk lines dropped")


def test_probe_failure_paths():
    # missing binary: not even attempted
    called = Runner(stdout=b"{}")
    assert ov.probe_monitors(presenter="/nonexistent/wayland_present",
                             runner=called) == []
    assert called.calls == []

    # presenter exploded / timed out
    assert ov.probe_monitors(presenter="/bin/true",
                             runner=Runner(exc=OSError("boom"))) == []

    # a presenter built before this feature: usage error, stdout silent
    assert ov.probe_monitors(presenter="/bin/true",
                             runner=Runner(stdout=b"", returncode=2)) == []

    # stderr noise must never be parsed as a monitor
    assert ov.probe_monitors(presenter="/bin/true",
                             runner=Runner(stdout=b"cannot connect to Wayland display\n")) == []

    # probe always asks for --list and never for a surface
    r = Runner(stdout=lines(MONO))
    ov.probe_monitors(presenter="/bin/true", runner=r)
    assert r.calls == [["/bin/true", "--list"]], r.calls
    print("probe: missing binary, crash, old binary and stderr junk all mean 'no monitors'")


# --- 2 ---------------------------------------------------------------------
def test_wire_contract_with_presenter_output():
    # This is the exact shape wayland_present.c prints (printf format copy),
    # including an escaped quote inside a monitor description.
    first = (b'{"index":0,"name":"","description":"LG Electronics LG ULTRAGEAR",'
             b'"x":0,"y":0,"width":2560,"height":1440,"scale":1,'
             b'"refresh_mhz":179960,"done":1}')
    # json.dumps escapes the inner quotes exactly like the C's json_escape does,
    # so this row is the same wire shape with a harder payload
    quoted = json.dumps({
        "index": 1, "name": "HDMI-A-1", "description": 'AOC 24G2 ' + chr(34) + 'gaming' + chr(34),
        "x": 2560, "y": 0, "width": 1920, "height": 1080, "scale": 1,
        "refresh_mhz": 74998, "done": 1}).encode()
    raw = first + b"\n" + quoted + b"\n"
    assert chr(34) in raw.decode() and chr(92) in raw.decode(), raw
    got = ov.probe_monitors(presenter="/bin/true", runner=Runner(stdout=raw))
    assert len(got) == 2, got
    assert set(got[0]) == {"index", "name", "description", "x", "y", "width",
                           "height", "scale", "refresh_mhz"}, got[0]
    assert got[1]["description"] == 'AOC 24G2 "gaming"'
    assert [ov.monitor_choice(m) for m in got] == ["#0", "HDMI-A-1"]
    print("wire: the --list JSON shape Python expects matches what the C prints")


# --- 3 ---------------------------------------------------------------------
def test_resolve_specs():
    def pick(rows, spec):
        return ov.resolve_monitor(rows, spec)

    assert pick(DUAL, "1")["name"] == "HDMI-A-1"
    assert pick(DUAL, "#1")["name"] == "HDMI-A-1"
    assert pick(DUAL, "0")["name"] == "DP-1"
    assert pick(DUAL, "dp-1")["name"] == "DP-1", "names are case-insensitive"
    assert pick(DUAL, "HDMI-A-1")["index"] == 1
    assert pick(DUAL, "AOC 24G2")["index"] == 1, "exact description match"
    assert pick(DUAL, "ultragear")["index"] == 0, "description fragment"
    assert pick(DUAL, "2560,0")["index"] == 1, "layout position wins for the right screen"
    assert pick(DUAL, "0,0")["index"] == 0

    out_of_range = pick(DUAL, "#7")
    assert isinstance(out_of_range, str) and out_of_range.startswith("no monitor #7")
    assert "DP-1" in out_of_range and "HDMI-A-1" in out_of_range, out_of_range
    # unnamed screens are offered by index, named ones by the name to type
    no_names = [dict(m, name="") for m in DUAL]
    listed = pick(no_names, "#7")
    assert "#0" in listed and "#1" in listed, listed
    unknown = pick(DUAL, "DisplayPort-9")
    assert isinstance(unknown, str) and "DP-1" in unknown and "HDMI-A-1" in unknown
    ambiguous = pick(STACKED, "ultragear")
    assert isinstance(ambiguous, str) and "matches 2 monitors" in ambiguous, ambiguous
    same_x = pick(STACKED, "0,0")
    assert isinstance(same_x, dict) and same_x["name"] == "DP-1", "stacked screens are distinct"
    assert pick(STACKED, "0,1440")["name"] == "DP-2"
    assert isinstance(pick(STACKED, "0,2880"), str)
    both_at_zero = ov.resolve_monitor(
        [dict(STACKED[0], index=1, x=0, y=0), dict(STACKED[1], index=2, x=0, y=0)], "0,0")
    assert isinstance(both_at_zero, str) and "use the number" in both_at_zero, both_at_zero
    assert isinstance(pick(DUAL, ""), str)
    assert isinstance(pick(DUAL, "1,,2"), str), "'1,,2' is not a position"
    assert isinstance(pick(DUAL, "2560,0,0"), str), "'2560,0,0' is not a position"
    print("resolve: index/name/description/position specs pick exactly one screen, "
          "ambiguous ones list the options")


# --- 4 ---------------------------------------------------------------------
def test_choose_single_monitor_never_asks():
    log = []
    def never_ask(prompt):
        raise AssertionError(f"prompted although there is only one monitor: {prompt!r}")
    got = ov.choose_monitor(MONO, None, interactive=True, prompt=never_ask, say=log.append)
    assert got["monitor"] is MONO[0] and got["selector"] == "#0"
    assert got["error"] is None and not got["asked"]
    assert any("the only monitor" in line for line in log), log

    # an explicit spec is honoured without asking, even when it is the same screen
    log.clear()
    got = ov.choose_monitor(DUAL, "HDMI-A-1", interactive=True, prompt=never_ask,
                            say=log.append)
    assert got["selector"] == "HDMI-A-1" and got["monitor"]["index"] == 1
    assert not got["asked"]
    print("choose: one monitor and an explicit spec both skip the prompt entirely")


def test_choose_explicit_typo_is_an_error():
    log = []
    def never_ask(prompt):
        raise AssertionError("a wrong --monitor must exit, not prompt")
    got = ov.choose_monitor(DUAL, "DP-3", interactive=True, prompt=never_ask,
                            say=log.append)
    assert got["monitor"] is None and got["selector"] is None
    assert "no monitor matching" in got["error"], got["error"]
    assert "DP-1" in got["error"] and "HDMI-A-1" in got["error"], got["error"]
    assert any("no monitor matching" in line for line in log), log
    # ...and 'auto'/'default' mean "decide yourself", not "monitor named auto"
    log.clear()
    asked = []
    def ask(prompt):
        asked.append(prompt)
        return "1"
    got = ov.choose_monitor(DUAL, "auto", interactive=True, prompt=ask, say=log.append)
    assert asked and got["monitor"]["index"] == 1
    log.clear()
    asked.clear()
    got = ov.choose_monitor(DUAL, "Default", interactive=True, prompt=ask, say=log.append)
    assert asked and got["monitor"]["index"] == 1
    print("choose: an unknown --monitor is a loud error listing the options, "
          "'auto'/'default' hand over to the prompt")


def test_choose_without_terminal_never_blocks():
    log = []
    def never_ask(prompt):
        raise AssertionError(f"would block a launcher or CI: {prompt!r}")
    got = ov.choose_monitor(DUAL, None, interactive=False, prompt=never_ask,
                            say=log.append)
    assert got["monitor"] is None and got["selector"] is None and not got["asked"]
    joined = " | ".join(log)
    assert "--monitor" in joined and "compositor default" in joined, joined
    assert "HDMI-A-1" in joined and "1920x1080+2560+0" in joined, "the hint lists the screens"

    # no monitor list at all: default output, still no prompt, still no error
    log.clear()
    got = ov.choose_monitor([], None, interactive=True, prompt=never_ask, say=log.append)
    assert got["monitor"] is None and got["error"] is None and not got["asked"]
    assert any("compositor default" in line for line in log), log
    print("choose: without a terminal (or without a monitor list) it never blocks on input")


# --- 5 ---------------------------------------------------------------------
def test_prompt_flow():
    def feed(answers):
        it = iter(answers)

        def ask(prompt):
            a = next(it)
            if isinstance(a, BaseException):
                raise a
            return a
        return ask

    # number
    log = []
    got = ov.choose_monitor(DUAL, None, True, prompt=feed(["1"]), say=log.append)
    assert got["monitor"]["index"] == 1 and got["selector"] == "HDMI-A-1"
    assert got["asked"]

    # name, case-insensitive
    got = ov.choose_monitor(DUAL, None, True, prompt=feed([" hdmi-a-1 "]), say=log.append)
    assert got["monitor"]["index"] == 1

    # layout position
    got = ov.choose_monitor(STACKED, None, True, prompt=feed(["0,1440"]), say=log.append)
    assert got["monitor"]["name"] == "DP-2"

    # Enter alone = first listed monitor
    log.clear()
    got = ov.choose_monitor(DUAL, None, True, prompt=feed([""]), say=log.append)
    assert got["monitor"]["index"] == 0 and "answered with Enter" in log[-1], log[-1]

    # the prompt itself shows every screen with a value the user can type
    shown = []
    got = ov.choose_monitor(STACKED, None, True, prompt=feed(["DP-2"]), say=shown.append)
    banner = " ".join(shown)
    assert "[DP-1]" in banner and "[DP-2]" in banner and "2 monitors detected" in banner
    assert "2560x1440+0+1440" in banner, banner
    assert got["monitor"]["name"] == "DP-2"

    # garbage, then a valid answer: the rejection is explained, then it proceeds
    log = []
    got = ov.choose_monitor(DUAL, None, True, prompt=feed(["ninth screen", "1"]),
                           say=log.append)
    assert got["monitor"]["index"] == 1
    assert any("not a monitor" in line for line in log), log

    # garbage every time: give up after a bounded number of tries, default screen
    log = []
    got = ov.choose_monitor(DUAL, None, True,
                           prompt=feed(["x", "y", "z", "should-not-be-read"]),
                           say=log.append, attempts=3)
    assert got["monitor"] is None and got["asked"]
    assert any("after 3 tries" in line for line in log), log

    # Ctrl-D / Ctrl-C at the prompt: default screen, no traceback
    log = []
    got = ov.choose_monitor(DUAL, None, True, prompt=feed([EOFError()]), say=log.append)
    assert got["monitor"] is None
    assert any("no answer" in line for line in log), log
    log = []
    got = ov.choose_monitor(DUAL, None, True, prompt=feed([KeyboardInterrupt()]),
                           say=log.append)
    assert got["monitor"] is None
    print("prompt: number, name, position, Enter, garbage-then-valid, garbage-always, "
          "EOF and Ctrl-C all land on a defined screen")


# --- 6 ---------------------------------------------------------------------
class FakeProc:
    """Duck-typed stand-in for subprocess.Popen(...) as run_wayland_feed uses it."""

    def __init__(self, argv, **kw):
        self.argv = list(argv)
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(b"ready 2560x1440\nconfigured 2560x1440\n")
        self.killed = False
        self.closed = False
        type(self).startup += 1
        type(self).argvs.append(self.argv)

    def readline(self):
        return self.stdout.readline()

    def poll(self):
        return None

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0

    def communicate(self, input=None):
        return b"", b""

    @property
    def returncode(self):
        return None

    def _close_stdin(self):
        self.closed = True

    startup = 0
    argvs = []


def fake_subprocess():
    """A subprocess stand-in that records the presenter argv."""
    fake = types.SimpleNamespace()
    fake.PIPE = -1

    def popen(argv, **kw):
        return FakeProc(argv)

    fake.Popen = popen
    fake.run = lambda *a, **k: types.SimpleNamespace(stdout=b"", returncode=0, stderr=b"")
    return fake


def test_presenter_receives_the_selector():
    original = ov.subprocess
    try:
        FakeProc.argvs = []
        FakeProc.startup = 0
        ov.subprocess = fake_subprocess()

        # legacy path: no selector, exactly the three-argument call
        rc = ov.run_wayland_feed("/nonexistent/latest.json", 0.05, 60.0, 0.0,
                                 2560, 1440, 0)
        assert rc == 0, rc
        assert FakeProc.startup == 1
        assert FakeProc.argvs[0][:4] == [ov.PRESENTER, "2560", "1440", "0"], FakeProc.argvs
        assert len(FakeProc.argvs[0]) == 4, "no monitor asked for, no fourth argument"

        # selector path: monitor name as the fourth argument, x still passed
        rc = ov.run_wayland_feed("/nonexistent/latest.json", 0.05, 60.0, 0.0,
                                 1920, 1080, 2560, monitor_selector="HDMI-A-1")
        assert rc == 0, rc
        assert FakeProc.argvs[1] == [ov.PRESENTER, "1920", "1080", "2560", "HDMI-A-1"], \
            FakeProc.argvs[1]

        # an index selector travels the same way (what KWin users get)
        rc = ov.run_wayland_feed("/nonexistent/latest.json", 0.05, 60.0, 0.0,
                                 2560, 1440, 0, monitor_selector="#1")
        assert FakeProc.argvs[2] == [ov.PRESENTER, "2560", "1440", "0", "#1"], FakeProc.argvs[2]
    finally:
        ov.subprocess = original
    print("presenter argv: legacy 3-arg call unchanged, monitor selector added as "
          "fourth argument")


def test_main_geometry_follows_the_chosen_screen():
    # main() derives the surface size from the picked monitor unless --geometry
    # says otherwise; a 1920x1080 screen must not be served a 2560x1440 frame.
    picked = ov.choose_monitor(DUAL, "HDMI-A-1", interactive=False)
    assert picked["monitor"]["width"] == 1920 and picked["monitor"]["x"] == 2560
    assert ov.monitor_choice(picked["monitor"]) == "HDMI-A-1"
    assert ov.parse_geometry("1920x1080+2560+0") == (1920, 1080, 2560, 0)
    auto = ov.choose_monitor(DUAL, None, interactive=False)
    assert auto["monitor"] is None, "no screen asked for and no terminal: compositor decides"
    print("geometry: the chosen screen's own size and offset flow into the surface")


if __name__ == "__main__":
    test_probe_parses_and_sanitizes()
    test_probe_failure_paths()
    test_wire_contract_with_presenter_output()
    test_resolve_specs()
    test_choose_single_monitor_never_asks()
    test_choose_explicit_typo_is_an_error()
    test_choose_without_terminal_never_blocks()
    test_prompt_flow()
    test_presenter_receives_the_selector()
    test_main_geometry_follows_the_chosen_screen()
    print("RESULT: PASS")
