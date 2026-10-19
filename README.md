# watchrun

Rerun a command whenever watched files change — a `nodemon`/`watchexec`-style
dev-loop helper with **zero dependencies** (Python 3.8+ stdlib only) that is
**process-group aware**: on every change it kills the *entire* previous
process group before restarting, so you never accumulate zombie children or
orphaned grandchildren.

## Features

- **Recursive directory watching** via lightweight polling (mtime + size per
  interval — `os`/`hashlib`-free, no inotify dependency, works everywhere).
- **`--include` / `--exclude` glob filters** (fnmatch). A bare pattern like
  `*.py` matches at any depth; `.git`, `node_modules`, `__pycache__`, `venv`
  and friends are excluded by default.
- **`--debounce` (default 300 ms)**: a burst of saves triggers exactly one
  restart, after a quiet period.
- **Process-group-aware restarts**: the child is spawned as a process-group
  leader (`start_new_session` on POSIX, `CREATE_NEW_PROCESS_GROUP` on
  Windows); on change the whole group gets SIGTERM (escalating to SIGKILL),
  then is reaped — no zombies.
- **`--clear`**: clear the terminal before each run.
- **Exit-code reporting**: the previous run's exit code is printed on every
  restart; `--once` exits with the child's code (CI-friendly).
- **`--dry-run`**: print what *would* be watched and run, without executing
  anything.
- **Graceful Ctrl-C**: the child group is terminated and `watchrun` exits 130.
- **Clean startup errors**: a missing/unrunnable command exits 127 with a
  one-line error instead of a traceback.

## Installation

No dependencies, no build step. Requires Python 3.8+.

```bash
# Option 1: run the script directly
python3 watchrun.py -i 'src/**/*.py' -- python -m pytest

# Option 2: install as a console script
pip install .
watchrun --help
```

## Configuration

Everything is a CLI flag — there is no config file:

| Flag | Default | Description |
|---|---|---|
| `-p, --path` | `.` | Directory to watch |
| `-i, --include` | (all) | Glob to watch, repeatable |
| `-e, --exclude` | (defaults) | Glob to ignore, repeatable (adds to defaults) |
| `--no-default-excludes` | off | Disable built-in excludes |
| `--debounce` | `300` | Quiet period in ms before restarting |
| `--interval` | `0.5` | Polling interval in seconds |
| `--clear` | off | Clear terminal before each run |
| `--once` | off | Run once, exit with the child's code |
| `--dry-run` | off | Print the watch/run plan, execute nothing |
| `-v, --verbose` | off | Print changed files on each restart |

Put options before the command, separated with `--`:

```bash
watchrun [options] -- COMMAND [ARGS ...]
```

## Usage examples

```bash
# Re-run pytest on every Python change under src/
watchrun -i 'src/**/*.py' -- python -m pytest

# Rebuild on change, clearing the screen each time
watchrun --clear -- make build

# Watch the current dir, 1s poll, 1s debounce
watchrun --interval 1 --debounce 1000 -- npm test

# Plan mode: see what would be watched, run nothing (great for CI linting)
watchrun --once --dry-run -- python -m pytest

# Run exactly once and propagate the exit code
watchrun --once -- python -m pytest -q; echo "exit=$?"

# Verbose: show which files triggered each restart
watchrun -v -i '*.md' -- mkdocs serve
```

Sample session:

```
$ watchrun -i 'src/**/*.py' -- python -m pytest
[watchrun] watching /home/me/proj (14 file(s), interval 0.5s, debounce 300ms)
[watchrun] run #1: python -m pytest
... pytest output ...
[watchrun] 1 file(s) changed
[watchrun] change detected — stopping previous run (pid 12345)
[watchrun] previous run stopped (exit -15)
[watchrun] run #2: python -m pytest
```

## Limitations

- **Polling, not inotify**: change detection latency is bounded by
  `--interval`, and very large trees (tens of thousands of files) cost CPU
  per poll. Lower `--interval` for snappier loops on small projects.
- **Detection is mtime + size**: a change that preserves both (extremely
  rare in practice) is not detected. Content hashing was deliberately left
  out for speed.
- **No recursive process-tree kill**: grandchildren that escape into a *new*
  process group/session of their own (e.g. via `setsid`) are not killed.
  Normal children and grandchildren are.
- **stdin**: the child inherits `watchrun`'s stdin, but each restart spawns a
  fresh child — interactive REPL-style commands restart from scratch.
- A command that writes into the watched tree on every run (e.g. compiling
  into `src/`) will re-trigger itself; exclude its outputs with `-e`.

## Development & tests

```bash
python3 -m unittest discover -s tests -v
```

The suite covers glob matching, snapshot diffing, debounce timing (fake
clock), argparse error paths, dry-run non-execution, process-group killing
(grandchild verified dead via `os.kill`), and three end-to-end integration
tests that spawn the real CLI: rerun-on-change, excluded-change silence, and
old-process-group death after restart.

## License

MIT — see [LICENSE](LICENSE).
