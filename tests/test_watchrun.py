"""Unit + integration tests for watchrun. Stdlib unittest only."""

import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import watchrun  # noqa: E402

WATCHRUN_PY = os.path.join(REPO_ROOT, "watchrun.py")


def wait_until(pred, timeout=20.0, poll=0.2, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(poll)
    raise AssertionError("timed out waiting for: %s" % what)


def pid_dead(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _proc_stat(pid):
    """Read /proc/<pid>/stat; None if the pid is gone (Linux only)."""
    try:
        with open("/proc/%d/stat" % pid) as f:
            parts = f.read().rsplit(")", 1)[1].split()
        return {"state": parts[0], "ppid": parts[1], "starttime": parts[19]}
    except (OSError, IndexError):
        return None


def _wait_killed(pid, starttime, timeout=10.0):
    """Poll until pid is provably no longer the original live process.

    Returns True when the pid disappears, becomes a zombie, or is reused
    by an unrelated process (different starttime):
    - a zombie is already terminated, only awaiting reaping by init —
      ``os.kill(pid, 0)`` still succeeds on zombies, so a naive
      kill-0 check false-fails here;
    - on a busy machine the freed pid can be reused between our kill and
      our check — the starttime comparison tells reuse apart from
      survival.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = _proc_stat(pid)
        if info is None:
            return True
        if info["starttime"] != starttime:
            return True
        if info["state"] == "Z":
            return True
        time.sleep(0.05)
    return False


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def advance(self, s):
        self.t += s


class PatternTest(unittest.TestCase):
    def test_bare_glob_matches_basename_at_any_depth(self):
        self.assertTrue(watchrun.pattern_matches("src/deep/mod.py", "*.py"))
        self.assertTrue(watchrun.pattern_matches("mod.py", "*.py"))
        self.assertFalse(watchrun.pattern_matches("src/mod.txt", "*.py"))

    def test_path_glob(self):
        self.assertTrue(watchrun.pattern_matches("src/a.py", "src/*.py"))
        self.assertTrue(watchrun.pattern_matches("src/deep/b.py", "src/**/*.py"))
        self.assertFalse(watchrun.pattern_matches("lib/a.py", "src/*.py"))

    def test_default_excludes(self):
        exc = watchrun.DEFAULT_EXCLUDES
        self.assertFalse(watchrun.is_watched(".git/config", [], exc))
        self.assertFalse(watchrun.is_watched("sub/.git/HEAD", [], exc))
        self.assertFalse(watchrun.is_watched("node_modules/x/index.js", [], exc))
        self.assertFalse(watchrun.is_watched("__pycache__/m.pyc", [], exc))
        self.assertFalse(watchrun.is_watched("a/b/__pycache__/m.pyc", [], exc))
        self.assertTrue(watchrun.is_watched("src/app.py", [], exc))
        self.assertTrue(watchrun.is_watched("README.md", [], exc))

    def test_include_filter(self):
        self.assertTrue(watchrun.is_watched("a.py", ["*.py"], []))
        self.assertFalse(watchrun.is_watched("a.txt", ["*.py"], []))

    def test_exclude_beats_include(self):
        self.assertFalse(
            watchrun.is_watched("secret/a.py", ["*.py"], ["secret/*"]))

    def test_no_default_excludes_flag_effect(self):
        # parse-level: with the flag, caller passes excludes without defaults
        args = watchrun.parse_args(
            ["--no-default-excludes", "--dry-run", "-p", REPO_ROOT])
        self.assertTrue(args.no_default_excludes)


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write(self, rel, content="x"):
        full = os.path.join(self.tmp.name, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
        return full

    def test_snapshot_picks_up_files(self):
        self._write("a.py")
        self._write("sub/b.py")
        snap = watchrun.build_snapshot(self.tmp.name, [], [])
        self.assertEqual(set(snap), {"a.py", "sub/b.py"})
        for _mtime_ns, size in snap.values():
            self.assertEqual(size, 1)

    def test_snapshot_ignores_unwatched(self):
        self._write(".git/config")
        self._write("a.py")
        snap = watchrun.build_snapshot(self.tmp.name, [],
                                       watchrun.DEFAULT_EXCLUDES)
        self.assertEqual(set(snap), {"a.py"})

    def test_snapshot_empty_dir(self):
        self.assertEqual(watchrun.build_snapshot(self.tmp.name, [], []), {})

    def test_diff_detects_added_removed_modified(self):
        p = self._write("a.py", "v1")
        self._write("gone.py", "x")
        old = watchrun.build_snapshot(self.tmp.name, [], [])
        # force a guaranteed mtime change (same size, different mtime)
        st = os.stat(p)
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        os.remove(os.path.join(self.tmp.name, "gone.py"))
        self._write("new.py", "y")
        new = watchrun.build_snapshot(self.tmp.name, [], [])
        d = watchrun.diff_snapshots(old, new)
        self.assertEqual(d["added"], ["new.py"])
        self.assertEqual(d["removed"], ["gone.py"])
        self.assertEqual(d["modified"], ["a.py"])

    def test_diff_identical_is_quiet(self):
        self._write("a.py", "v1")
        old = watchrun.build_snapshot(self.tmp.name, [], [])
        new = watchrun.build_snapshot(self.tmp.name, [], [])
        d = watchrun.diff_snapshots(old, new)
        self.assertEqual(d, {"added": [], "removed": [], "modified": []})


class DebouncerTest(unittest.TestCase):
    def test_quiet_period_required(self):
        clock = FakeClock()
        d = watchrun.RestartDebouncer(0.3, clock=clock.now)
        self.assertFalse(d.ready())
        d.note_change()
        clock.advance(0.2)
        self.assertFalse(d.ready())
        clock.advance(0.2)
        self.assertTrue(d.ready())
        d.reset()
        self.assertFalse(d.ready())

    def test_burst_resets_timer(self):
        clock = FakeClock()
        d = watchrun.RestartDebouncer(0.3, clock=clock.now)
        d.note_change()
        clock.advance(0.25)
        d.note_change()  # burst: timer restarts
        clock.advance(0.25)
        self.assertFalse(d.ready())
        clock.advance(0.1)
        self.assertTrue(d.ready())

    def test_zero_debounce_ready_immediately(self):
        clock = FakeClock()
        d = watchrun.RestartDebouncer(0.0, clock=clock.now)
        d.note_change()
        self.assertTrue(d.ready())


class ArgsTest(unittest.TestCase):
    def test_no_command_is_usage_error(self):
        with self.assertRaises(SystemExit) as cm:
            watchrun.parse_args([])
        self.assertEqual(cm.exception.code, 2)

    def test_bad_path_is_usage_error(self):
        with self.assertRaises(SystemExit) as cm:
            watchrun.parse_args(["-p", "/no/such/dir/xyz", "--", "true"])
        self.assertEqual(cm.exception.code, 2)

    def test_negative_debounce_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            watchrun.parse_args(["--debounce", "-5", "--", "true"])
        self.assertEqual(cm.exception.code, 2)

    def test_zero_interval_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            watchrun.parse_args(["--interval", "0", "--", "true"])
        self.assertEqual(cm.exception.code, 2)

    def test_double_dash_stripped_from_command(self):
        args = watchrun.parse_args(["--", "echo", "hi"])
        self.assertEqual(args.command, ["echo", "hi"])

    def test_options_before_command(self):
        args = watchrun.parse_args(
            ["--clear", "--interval", "0.2", "--", "make", "build"])
        self.assertTrue(args.clear)
        self.assertEqual(args.interval, 0.2)
        self.assertEqual(args.command, ["make", "build"])

    def test_dry_run_without_command_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = watchrun.main(["--dry-run", "-p", tmp])
        self.assertEqual(code, 0)


class DryRunNoExecTest(unittest.TestCase):
    def test_dry_run_does_not_execute_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "SHOULD_NOT_EXIST")
            with open(os.path.join(tmp, "a.py"), "w") as f:
                f.write("x = 1\n")
            code = watchrun.main([
                "--dry-run", "-p", tmp, "--",
                sys.executable, "-c",
                "open(%r, 'w').write('ran')" % marker,
            ])
            self.assertEqual(code, 0)
            self.assertFalse(os.path.exists(marker),
                             "dry-run executed the command!")


class MissingCommandTest(unittest.TestCase):
    def test_missing_command_once_exits_127_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = watchrun.main(
                ["--once", "-p", tmp, "--", "definitely-not-a-real-cmd-xyz"])
        self.assertEqual(code, 127)

    def test_missing_command_watch_mode_exits_127(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = watchrun.main(
                ["-p", tmp, "--", "definitely-not-a-real-cmd-xyz"])
        self.assertEqual(code, 127)


@unittest.skipUnless(os.name == "posix" and os.path.isdir("/proc"),
                   "process-group kill test needs Linux /proc")
class KillGroupTest(unittest.TestCase):
    def test_kill_process_group_kills_grandchildren(self):
        # child spawns a grandchild in the same process group; both must die.
        child = subprocess.Popen(
            [sys.executable, "-c",
             "import subprocess, sys, time; "
             "subprocess.Popen([sys.executable, '-c', "
             "'import time; time.sleep(120)']); "
             "print('ready', flush=True); time.sleep(120)"],
            start_new_session=True, stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.kill)
        self.addCleanup(child.stdout.close)
        wait_until(lambda: child.stdout.readline().strip() == "ready",
                   what="child ready")
        child_start = _proc_stat(child.pid)["starttime"]
        grandchild_pid = grandchild_start = None
        # find the grandchild via /proc (linux-only test env)
        for pid_dir in os.listdir("/proc"):
            if not pid_dir.isdigit():
                continue
            info = _proc_stat(int(pid_dir))
            if info and info["ppid"] == str(child.pid):
                grandchild_pid = int(pid_dir)
                grandchild_start = info["starttime"]
        self.assertIsNotNone(grandchild_pid, "grandchild not found")
        watchrun._kill_process_group(child)
        self.assertTrue(_wait_killed(child.pid, child_start),
                        "child still alive")
        self.assertTrue(_wait_killed(grandchild_pid, grandchild_start),
                        "grandchild survived")


@unittest.skipUnless(os.name == "posix", "integration tests are POSIX-only")
class IntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.logdir.cleanup)
        self.watch_procs = []
        self.addCleanup(self._cleanup_watchers)

    def _cleanup_watchers(self):
        for p in self.watch_procs:
            try:
                p.kill()
            except OSError:
                pass

    def _write(self, rel, content="x"):
        full = os.path.join(self.tmp.name, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
        return full

    def _spawn_watcher(self, *extra):
        cmd = [sys.executable, WATCHRUN_PY, "-p", self.tmp.name,
               "--interval", "0.2", "--debounce", "100"] + list(extra)
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
        self.watch_procs.append(p)
        return p

    def _stop_watcher(self, p):
        p.send_signal(signal.SIGINT)
        out, _ = p.communicate(timeout=15)
        return p.returncode, out

    def _counter_script(self):
        path = self._write("counter.py", textwrap.dedent("""\
            import sys, time
            with open(sys.argv[1], "a") as f:
                f.write("run\\n")
            """))
        return path

    def _log_lines(self, log):
        if not os.path.exists(log):
            return 0
        with open(log) as f:
            return len(f.readlines())

    def test_rerun_on_file_change(self):
        counter = self._counter_script()
        log = os.path.join(self.logdir.name, "runs.log")
        p = self._spawn_watcher("-i", "*.py", "--",
                               sys.executable, counter, log)
        wait_until(lambda: self._log_lines(log) >= 1,
                   what="initial run happened")
        # touch a watched file -> exactly one restart expected
        with open(counter, "a") as f:
            f.write("# touched\n")
        wait_until(lambda: self._log_lines(log) >= 2,
                   what="restart after file change")
        code, out = self._stop_watcher(p)
        self.assertEqual(code, 130, "watcher should exit 130 on Ctrl-C\n%s" % out)
        self.assertIn("run #2", out)
        self.assertGreaterEqual(self._log_lines(log), 2)

    def test_excluded_change_does_not_restart(self):
        counter = self._counter_script()
        self._write("notes.txt", "hello")
        log = os.path.join(self.logdir.name, "runs.log")
        p = self._spawn_watcher("-i", "*.py", "--",
                               sys.executable, counter, log)
        wait_until(lambda: self._log_lines(log) >= 1,
                   what="initial run happened")
        # change a file that is NOT watched -> no restart
        with open(os.path.join(self.tmp.name, "notes.txt"), "a") as f:
            f.write("more\n")
        time.sleep(1.5)  # several poll intervals + debounce
        self.assertEqual(self._log_lines(log), 1,
                         "excluded file change triggered a restart")
        code, _ = self._stop_watcher(p)
        self.assertEqual(code, 130)

    def test_old_process_group_is_dead_after_restart(self):
        pidfile = os.path.join(self.logdir.name, "pids")
        child_src = textwrap.dedent("""\
            import os, subprocess, sys, time
            pidfile = sys.argv[1]
            gc = subprocess.Popen([sys.executable, "-c",
                                   "import time; time.sleep(120)"])
            with open(pidfile, "w") as f:
                f.write("%d %d" % (os.getpid(), gc.pid))
            time.sleep(120)
            """)
        child_py = self._write("child.py", child_src)
        p = self._spawn_watcher("-i", "*.py", "--",
                               sys.executable, child_py, pidfile)

        def read_pids():
            if not os.path.exists(pidfile):
                return None
            with open(pidfile) as f:
                parts = f.read().strip().split()
            return (int(parts[0]), int(parts[1])) if len(parts) == 2 else None

        wait_until(lambda: read_pids() is not None, what="first run pidfile")
        old_child, old_grandchild = read_pids()
        old_child_start = (_proc_stat(old_child) or {}).get("starttime")
        old_gc_start = (_proc_stat(old_grandchild) or {}).get("starttime")
        # trigger a restart
        self._write("trigger.py", "x = 1\n")
        wait_until(lambda: read_pids() is not None
                   and read_pids()[0] != old_child,
                   what="second run started")
        new_child, _new_gc = read_pids()
        self.assertNotEqual(new_child, old_child)
        # the ENTIRE old process group must be gone. A zombie or a reused
        # pid both prove the original process is dead (see _wait_killed).
        if os.path.isdir("/proc") and old_child_start and old_gc_start:
            self.assertTrue(_wait_killed(old_child, old_child_start),
                            "old child pid %d still alive" % old_child)
            self.assertTrue(_wait_killed(old_grandchild, old_gc_start),
                            "old grandchild pid %d still alive"
                            % old_grandchild)
        else:
            # non-Linux POSIX: best-effort kill-0 polling
            wait_until(lambda: pid_dead(old_child) and
                       pid_dead(old_grandchild),
                       what="old process group reaped")
        code, _ = self._stop_watcher(p)
        self.assertEqual(code, 130)


if __name__ == "__main__":
    unittest.main()
