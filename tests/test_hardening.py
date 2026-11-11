#!/usr/bin/env python3
"""Hardening tests for helper/btrfs_snapshots.py.

Focus: hostile input, resource limits, process hygiene, privacy, and the
privilege/creation invariants from docs/DESIGN.md. Run with the same commands
as tests/test_helper.py.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "helper"))
import btrfs_snapshots as bs  # noqa: E402

HELPER_SOURCE = (ROOT / "helper" / "btrfs_snapshots.py").read_text(encoding="utf-8")
QML_SOURCES = {
    name: (ROOT / name).read_text(encoding="utf-8")
    for name in ("BarWidget.qml", "Panel.qml")
}


def run_cli(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = bs.main(list(argv))
    assert code == 0, "helper exited %d: %s" % (code, out.getvalue())
    return json.loads(out.getvalue())


class PrivilegeInvariantTests(unittest.TestCase):
    """docs/DESIGN.md: no privilege-escalation literals anywhere in shipped
    source (marketplace static scans cannot tell comments from code), and no
    shell-built command strings."""

    def test_no_elevation_literals_in_helper_source(self):
        for forbidden in ("sudo", "pkexec", "polkit", "sudoers", "noexec"):
            self.assertNotIn(forbidden, HELPER_SOURCE.lower(),
                             "literal %r found in helper source" % forbidden)

    def test_no_elevation_literals_in_qml_sources(self):
        for name, source in QML_SOURCES.items():
            for forbidden in ("sudo", "pkexec", "polkit", "sudoers"):
                self.assertNotIn(forbidden, source.lower(),
                                 "literal %r found in %s" % (forbidden, name))

    def test_no_shell_execution_in_helper(self):
        for forbidden in ("shell=True", "os.system", "os.popen", "sh -c",
                          "bash -c", "/bin/sh"):
            self.assertNotIn(forbidden, HELPER_SOURCE)

    def test_only_one_subprocess_shape(self):
        # The helper may call subprocess.Popen exactly once, with a fixed
        # argv shape; snapper list mode is the only intended child.
        self.assertEqual(HELPER_SOURCE.count("subprocess.Popen"), 1)
        self.assertIn('run_cmd(["snapper", "--jsonout", "list-configs"]',
                      HELPER_SOURCE)
        self.assertIn('run_cmd(["snapper", "--jsonout", "-c", name, "list"]',
                      HELPER_SOURCE)

    def test_creation_command_is_the_documented_constant(self):
        self.assertEqual(
            bs.CREATION_COMMAND,
            "omarchy-launch-floating-terminal-with-presentation omarchy-snapshot create")

    def test_no_delete_or_restore_verbs_in_commands(self):
        # The helper never builds destructive commands of any kind.
        for verb in ("delete", "rollback", "undochange", "cleanup",
                     "snapper -c root delete"):
            for line in HELPER_SOURCE.splitlines():
                if line.strip().startswith("#") or "cleanup" in line and "algorithm" in line:
                    continue
                if 'run_cmd' in line or "CREATION_COMMAND =" in line:
                    self.assertNotIn(verb, line)


class ClipTests(unittest.TestCase):
    def test_strips_and_collapses(self):
        self.assertEqual(bs.clip("  a\tb  "), "a b")
        self.assertEqual(bs.clip("line\nbreak\r\nhere"), "line break here")

    def test_none_passthrough(self):
        self.assertIsNone(bs.clip(None))

    def test_no_empty_strings(self):
        self.assertIsNone(bs.clip("   \n  "))


class HostileInputTests(unittest.TestCase):
    def test_hostile_limine_lines(self):
        text = "\n".join([
            "  //Snapshots",
            '  ///2026-01-01 00:00:00$(rm -rf /) ; <script>alert("x")</script>',
            "  comment: desc\x00nul && cat /etc/shadow",
            "  ////k",
            "  cmdline: rootflags=subvol=/@/.snapshots/99999999999999999999/snapshot"
            " && whatever; `backticks`",
        ])
        rows, status = bs.parse_limite_conf(text)
        self.assertEqual(status, "ok")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["number"], 99999999999999999999)
        self.assertEqual(rows[0]["description"], "desc nul && cat /etc/shadow")
        blob = json.dumps(rows)
        self.assertNotIn("backticks", blob)
        self.assertNotIn("whatever", blob)

    def test_large_limine_input_bounded(self):
        # 20k junk lines must parse fast and stay bounded in output size.
        text = "\n".join("junk line %d rootflags=subvol=/@/.snapshots/%d/snapshot"
                         % (i, 1000 + i) for i in range(20000))
        rows, status = bs.parse_limite_conf(text)
        self.assertEqual(status, "no-section")  # no //Snapshots marker
        self.assertEqual(rows, [])

    def test_hostile_snapper_json(self):
        data = {"configs": [{"snapshots": [
            {"number": None, "description": "dropped"},
            {"number": [1], "description": "dropped"},
            {"number": "7", "description": "ok"},
            {"number": "8", "description": {"deep": ["a" * 500]}},
        ]}, "not-a-dict", 42]}
        rows = bs.parse_snapper_list(data)
        self.assertEqual([r["number"] for r in rows], [7, 8])
        # a dict description is stringified, clipped, and still JSON-safe
        self.assertEqual(len(rows[1]["description"]), bs.MAX_DESC_LEN)
        json.dumps(rows)

    def test_deeply_nested_json(self):
        data = {"a": [{"b": [{"c": [{"d": [{"number": "9", "description": "deep"}]}]}]}]}
        rows = bs.parse_snapper_list(data)
        self.assertEqual(rows[0]["number"], 9)

    def test_run_cmd_rejects_non_list(self):
        # argv must be a list; a string argv is refused, never run.
        out, err, kind, code = bs.run_cmd("snapper --jsonout list", 1.0)
        self.assertEqual(kind, "missing")

    def test_run_cmd_missing_binary(self):
        out, err, kind, code = bs.run_cmd(["/definitely/not/a/binary"], 1.0)
        self.assertEqual(kind, "missing")

    def test_run_cmd_timeout_kills_group(self):
        # A child that spawns a grandchild: both must die on timeout.
        script = Path(tempfile.mkdtemp()) / "hang.py"
        script.write_text(
            "import subprocess, sys\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "time.sleep(60)\n")
        start = time.time()
        out, err, kind, code = bs.run_cmd([sys.executable, str(script)], 0.5)
        self.assertEqual(kind, "timeout")
        self.assertLess(time.time() - start, 10)  # did not wait 60 s
        time.sleep(0.3)
        # no zombie children left behind
        zombies = subprocess.run(
            ["ps", "--ppid", str(os.getpid()), "-o", "stat="],
            capture_output=True, text=True).stdout
        self.assertNotIn("Z", zombies)


class StateTests(unittest.TestCase):
    def test_write_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            bs.write_json_atomic(Path(td) / "state.json",
                                 {"schema": 1, "tier_b": {"root": {}}})
            self.assertEqual(bs.load_state(_ctx(td)),
                             {"schema": 1, "tier_b": {"root": {}}})

    def test_load_state_repairs_bad_schema(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            path.write_text('{"schema": 99, "tier_b": "broken"}')
            self.assertEqual(bs.load_state(_ctx(td)), {"schema": 1, "tier_b": {}})

    def test_load_state_survives_garbage(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            path.write_text("not json at all\x00\x01")
            self.assertEqual(bs.load_state(_ctx(td)), {"schema": 1, "tier_b": {}})

    def test_state_dir_only_known_keys(self):
        state_dir = tempfile.mkdtemp()
        run_cli("--state-dir", state_dir, "--mock", "normal", "check")
        doc = json.loads((Path(state_dir) / "state.json").read_text())
        self.assertEqual(set(doc.keys()), {"schema", "tier_b"})


class ClampTests(unittest.TestCase):
    def test_clamp_int(self):
        self.assertEqual(bs.clamp_int(7, 0, 100, 50), 7)
        self.assertEqual(bs.clamp_int(-5, 0, 100, 50), 0)
        self.assertEqual(bs.clamp_int("200", 0, 100, 50), 100)
        self.assertEqual(bs.clamp_int("junk", 0, 100, 50), 50)
        self.assertEqual(bs.clamp_int(None, 0, 100, 50), 50)


def _ctx(state_dir):
    ctx = bs.Context(Path(state_dir), {}, None, False)
    return ctx


if __name__ == "__main__":
    unittest.main()
