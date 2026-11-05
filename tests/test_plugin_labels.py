#!/usr/bin/env python3
"""Plugin battery: quest objective LABELS.

One quest can have several world objectives (the backported "Signal - Part 1"
has four). Unnumbered, the overlay shows four boxes that all read the quest
name with different meters, which looks like one marker changing its distance.
plugin/src/ObjectiveLabels.cs numbers them in enumeration order; this battery
drives that production file through the test harness (no Unity, no BepInEx).

Run: .venv/bin/python3 tests/test_plugin_labels.py    (needs `dotnet` on PATH)
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT)
BIN = os.path.join(ROOT, "tests", "plugin", "bin", "Release", "net8.0", "ws-harness")

passed = []
failed = []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}: {name}" + (f" — {detail}" if detail and not cond else ""))


def build():
    if shutil.which("dotnet") is None:
        print("FAIL: dotnet not installed - the plugin label battery cannot run")
        print("RESULT: FAIL (0/0)")
        sys.exit(1)
    subprocess.run(["dotnet", "build", "tests/plugin/WsHarness.csproj", "-c", "Release", "-v", "q"],
                   cwd=ROOT, check=True)


def labels(cases):
    """cases: list of (label, count, max_chars_or_None) -> list of label groups."""
    stdin = "".join(
        c[0] + "\t" + str(c[1]) + (("\t" + str(c[2])) if len(c) > 2 and c[2] else "") + "\n"
        for c in cases)
    out = subprocess.run([BIN, "labels"], input=stdin, capture_output=True, text=True, timeout=60)
    groups, current = [], []
    for line in out.stdout.splitlines():
        if line == "--":
            groups.append(current)
            current = []
        else:
            current.append(line)
    return groups


def main():
    build()

    # 1. the live raid case: "Signal - Part 1" emitted FOUR zone/beacon markers
    g = labels([("Signal - Part 1", 4)])[0]
    check("four objectives get four distinct labels",
          g == ["Signal - Part 1 (1/4)", "Signal - Part 1 (2/4)",
                "Signal - Part 1 (3/4)", "Signal - Part 1 (4/4)"], str(g))
    check("every objective of a multi-target quest is attributable",
          len(set(g)) == 4 and all(l.endswith(f"({i + 1}/4)") for i, l in enumerate(g)), str(g))

    # 2. a single-objective quest keeps the plain quest name (no "(1/1)" noise)
    g = labels([("Clean Sweep", 1)])[0]
    check("single objective keeps the plain name", g == ["Clean Sweep"], str(g))

    # 3. a quest that produced no world target adds no label noise
    g = labels([("Kill count", 0)])[0]
    check("no objectives -> no labels", g == [], str(g))

    # 4. the index survives the wire budget: the NAME gets clamped, the index does not
    long_name = "S" * 150
    g = labels([(long_name, 4, 112)])[0]
    clamped = [l for l in g if l.startswith("clamp:")]
    numbered = [l for l in g if not l.startswith("clamp:")]
    check("clamped name keeps its length budget",
          clamped and len(clamped[0][len("clamp:"):]) == 112, str(clamped))
    check("numbered labels stay inside the 120-char wire cap",
          numbered and all(len(l) <= 120 for l in numbered),
          str([len(l) for l in numbered]))
    check("the (i/n) index is never truncated",
          numbered and all(l.endswith(f"({i + 1}/4)") for i, l in enumerate(numbered)),
          str(numbered))

    # 5. index is independent of order of discovery of OTHER quests: a quest with
    #    two objectives is numbered against its own count, not the global marker list
    groups = labels([("Two Targets", 2), ("One Target", 1)])
    g = groups[0]
    check("numbering is per quest, not per feed",
          g == ["Two Targets (1/2)", "Two Targets (2/2)"] and groups[1] == ["One Target"],
          str(groups))

    # 6. a missing quest name still gets a usable label
    g = labels([("", 2)])[0]
    check("empty quest name falls back to a labelled objective",
          g == ["Quest (1/2)", "Quest (2/2)"], str(g))

    print()
    print(f"plugin objective labels: {len(passed)} passed, {len(failed)} failed")
    if failed:
        print("failed:", failed)
        print(f"RESULT: FAIL ({len(passed)}/{len(passed) + len(failed)})")
        sys.exit(1)
    print(f"RESULT: PASS ({len(passed)}/{len(passed)})")


if __name__ == "__main__":
    main()
