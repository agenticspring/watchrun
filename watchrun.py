#!/usr/bin/env python3
"""watchrun: rerun a command whenever watched files change.

A stdlib-only, zero-dependency file-change re-runner for dev loops
(think nodemon/watchexec, but zero-install and process-group aware).

Polls a directory tree once per interval, comparing each file's mtime
and size. When a change is detected it kills the *entire* previous
process group (no zombies, no orphaned grandchildren) and starts the
command again.

Usage:
    watchrun [options] -- COMMAND [ARGS ...]

Examples:
    watchrun -i 'src/**/*.py' -- python -m pytest
    watchrun --clear -- make build
    watchrun --once --dry-run -- python -m pytest
"""

import argparse
import fnmatch
import os
import signal
import stat
import subprocess
import sys
import time

VERSION = "1.0.0"

# Directories/files never watched unless --no-default-excludes is given.
DEFAULT_EXCLUDES = [
    ".git/*", "*/.git/*",
    "node_modules/*", "*/node_modules/*",
    "__pycache__/*", "*/__pycache__/*",
    ".venv/*", "*/.venv/*",
    "venv/*", "*/venv/*",
    "*.pyc", "*.pyo", "*.swp", "*~", ".DS_Store",
]

CTRL_C_EXIT = 130
NOT_FOUND_EXIT = 127


class StartupError(Exception):
    """Raised when the child command cannot be started."""


# ---------------------------------------------------------------------------
# Pattern matching
# ---------------------------------------------------------------------------

def _posix(path):
    """Normalize a path to forward slashes for fnmatch comparisons."""
    return path.replace(os.sep, "/")


def pattern_matches(rel_path, pattern):
    """True if a repo-relative path matches a glob pattern.

    A pattern without a slash (e.g. ``*.py``) also matches against the
    basename, so ``*.py`` matches ``src/deep/mod.py``.
    """
    rel = _posix(rel_path)
    pat = _posix(pattern)
    if fnmatch.fnmatchcase(rel, pat):
        return True
    if "/" not in pat and fnmatch.fnmatchcase(os.path.basename(rel), pat):
        return True
    return False


def is_watched(rel_path, includes, excludes):
    """True if rel_path passes the exclude/include filter."""
    for pat in excludes:
        if pattern_matches(rel_path, pat):
            return False
    if not includes:
        return True
    return any(pattern_matches(rel_path, pat) for pat in includes)


# ---------------------------------------------------------------------------
# Snapshots + diff
# ---------------------------------------------------------------------------

def build_snapshot(root, includes, excludes):
    """Walk root and return {relpath: (mtime_ns, size)} for watched files."""
    snap = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            if not is_watched(rel, includes, excludes):
                continue
            try:
                st = os.stat(full)
            except OSError:
                continue  # vanished mid-walk / unreadable
            if not stat.S_ISREG(st.st_mode):
                continue
            snap[_posix(rel)] = (st.st_mtime_ns, st.st_size)
    return snap


def diff_snapshots(old, new):
    """Return {'added': [...], 'removed': [...], 'modified': [...]}."""
    added = sorted(p for p in new if p not in old)
    removed = sorted(p for p in old if p not in new)
    modified = sorted(p for p in new if p in old and new[p] != old[p])
    return {"added": added, "removed": removed, "modified": modified}


# ---------------------------------------------------------------------------
# Debounce
# ---------------------------------------------------------------------------

class RestartDebouncer:
    """Restart at most once per quiet period after a burst of changes."""

    def __init__(self, debounce_s, clock=time.monotonic):
        self.debounce_s = debounce_s
        self.clock = clock
        self.pending_since = None

    def note_change(self):
        self.pending_since = self.clock()

    def ready(self):
        return (
            self.pending_since is not None
            and (self.clock() - self.pending_since) >= self.debounce_s
        )

    def reset(self):
        self.pending_since = None


# ---------------------------------------------------------------------------
# Process-group-aware child management
# ---------------------------------------------------------------------------

def _spawn(cmd):
    """Start cmd as the leader of its own process group / session."""
    if os.name == "posix":
        return subprocess.Popen(cmd, start_new_session=True)
    return subprocess.Popen(
        cmd, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
    )


def _kill_process_group(proc, sigterm_timeout=5.0):
    """SIGTERM the whole process group, escalate to SIGKILL, then reap."""
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
    except (ProcessLookupError, OSError):
        pass  # already gone
    try:
        proc.wait(timeout=sigterm_timeout)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, OSError):
            pass
        proc.wait()  # reap: no zombies


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


class WatchRunner:
    """Owns the child command lifecycle across restarts."""

    def __init__(self, cmd, clear=False):
        self.cmd = list(cmd)
        self.clear = clear
        self.proc = None
        self.run_count = 0
        self.last_exit = None

    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        if self.clear:
            clear_screen()
        self.run_count += 1
        print(
            "[watchrun] run #%d: %s" % (self.run_count, " ".join(self.cmd)),
            flush=True,
        )
        try:
            self.proc = _spawn(self.cmd)
        except OSError as exc:
            print(
                "[watchrun] error: cannot start %r: %s"
                % (self.cmd[0], exc.strerror or exc),
                file=sys.stderr,
                flush=True,
            )
            raise StartupError(self.cmd[0])

    def stop(self):
        proc = self.proc
        if proc is None:
            return
        if self.is_running():
            _kill_process_group(proc)
        else:
            proc.wait()  # already exited; make sure it is reaped
        self.last_exit = proc.returncode
        self.proc = None

    def restart(self):
        proc = self.proc
        if proc is not None and self.is_running():
            print(
                "[watchrun] change detected — stopping previous run "
                "(pid %d)" % proc.pid,
                flush=True,
            )
            self.stop()
            print(
                "[watchrun] previous run stopped (exit %s)" % self.last_exit,
                flush=True,
            )
        else:
            if proc is not None:
                proc.wait()
                self.last_exit = proc.returncode
            print(
                "[watchrun] change detected — previous run had already "
                "exited (exit %s)" % self.last_exit,
                flush=True,
            )
            self.proc = None
        self.start()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="watchrun",
        description="Rerun a command whenever watched files change "
                    "(stdlib only, zero dependencies).",
        epilog="Example: watchrun -i 'src/**/*.py' -- python -m pytest",
    )
    parser.add_argument("-p", "--path", default=".",
                        help="directory to watch (default: .)")
    parser.add_argument("-i", "--include", action="append", default=[],
                        metavar="PATTERN",
                        help="glob to watch (repeatable; default: everything "
                             "not excluded)")
    parser.add_argument("-e", "--exclude", action="append", default=[],
                        metavar="PATTERN",
                        help="glob to ignore (repeatable; adds to defaults)")
    parser.add_argument("--no-default-excludes", action="store_true",
                        help="do not apply the built-in excludes "
                             "(.git, node_modules, __pycache__, ...)")
    parser.add_argument("--debounce", type=float, default=300, metavar="MS",
                        help="quiet period in ms before restarting "
                             "(default: 300)")
    parser.add_argument("--interval", type=float, default=0.5, metavar="SEC",
                        help="polling interval in seconds (default: 0.5)")
    parser.add_argument("--clear", action="store_true",
                        help="clear the terminal before each run")
    parser.add_argument("--once", action="store_true",
                        help="run the command once and exit with its code")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be watched/run without "
                             "executing anything")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print the changed file list on each restart")
    parser.add_argument("--version", action="version",
                        version="watchrun " + VERSION)
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="command to run, e.g. -- python -m pytest")
    args = parser.parse_args(argv)

    if args.debounce < 0:
        parser.error("--debounce must be >= 0")
    if args.interval <= 0:
        parser.error("--interval must be > 0")
    # argparse.REMAINDER keeps a literal leading "--"; drop it for display.
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command and not args.dry_run:
        parser.error("a COMMAND is required (separate options from the "
                     "command with --)")
    if not os.path.isdir(args.path):
        parser.error("--path is not a directory: %r" % args.path)
    return args


def _print_dry_run(root, args, excludes, snapshot):
    print("[watchrun] DRY RUN — nothing will be executed")
    print("  watching : %s (%d file(s))" % (root, len(snapshot)))
    print("  include  : %s" % (", ".join(args.include)
                                if args.include else "(all)"))
    print("  exclude  : %s" % (", ".join(excludes) if excludes else "(none)"))
    print("  interval : %gs   debounce: %gms" % (args.interval, args.debounce))
    if args.command:
        print("  command  : %s" % " ".join(args.command))
    else:
        print("  command  : (none)")


def main(argv=None):
    args = parse_args(argv)
    root = os.path.abspath(args.path)
    excludes = list(args.exclude)
    if not args.no_default_excludes:
        excludes = excludes + DEFAULT_EXCLUDES

    snapshot = build_snapshot(root, args.include, excludes)

    if args.dry_run:
        _print_dry_run(root, args, excludes, snapshot)
        return 0

    print(
        "[watchrun] watching %s (%d file(s), interval %gs, debounce %gms)"
        % (root, len(snapshot), args.interval, args.debounce),
        flush=True,
    )
    runner = WatchRunner(args.command, clear=args.clear)
    try:
        runner.start()
    except StartupError:
        return NOT_FOUND_EXIT

    if args.once:
        try:
            code = runner.proc.wait()
        except KeyboardInterrupt:
            runner.stop()
            print("\n[watchrun] interrupted", flush=True)
            return CTRL_C_EXIT
        print("[watchrun] done (exit %d)" % code, flush=True)
        return code

    debouncer = RestartDebouncer(args.debounce / 1000.0)
    try:
        while True:
            time.sleep(args.interval)
            new_snapshot = build_snapshot(root, args.include, excludes)
            changes = diff_snapshots(snapshot, new_snapshot)
            total = sum(len(v) for v in changes.values())
            if total:
                snapshot = new_snapshot
                debouncer.note_change()
                if args.verbose:
                    changed = (changes["added"] + changes["removed"]
                               + changes["modified"])
                    print(
                        "[watchrun] %d file(s) changed: %s"
                        % (total, ", ".join(changed)),
                        flush=True,
                    )
                else:
                    print("[watchrun] %d file(s) changed" % total, flush=True)
            if debouncer.ready():
                debouncer.reset()
                runner.restart()
    except StartupError:
        return NOT_FOUND_EXIT
    except KeyboardInterrupt:
        print("\n[watchrun] interrupted — stopping child", flush=True)
        runner.stop()
        return CTRL_C_EXIT


if __name__ == "__main__":
    sys.exit(main())
