"""Kill a test runner's servers even when the runner itself is SIGKILLed.

Started detached (new session, no inherited fds) by tests/leak_guard.py, once per
runner, as:  python3 tests/leak_watchdog.py <runner pid> <pidfile>

Every 0.5 s it records (pid, starttime) for
  - every process in the runner's descendant tree (/proc ppid links), and
  - every process whose /proc environ carries DIRECTOR_TEST_OWNER=<runner pid>
    (set in the runner's env at guard import, so children inherit it; unreadable
    environ -- snap/non-dumpable -- is ignored, the tree snapshot is the fallback), and
  - every "pid starttime" line in the pidfile, appended by spawn() the instant a
    child exists, so a child orphaned within the first 0.5 s is still caught.

When the runner pid disappears (or is reused: starttime changed) it SIGKILLs every
recorded pid that is still alive *with the same recorded start time* -- children
first -- plus any recorded process group led by one of them, then exits. A normal
runner exit takes the same path, one sweep, then exit. It never touches a process
it did not record: never the user's firefox, docker, model servers, or other
repos' tests. Keep this module standalone -- importing leak_guard here would
start a watchdog per watchdog.
"""
import os
import signal
import sys
import time
from pathlib import Path


def _fields(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text(errors='replace').rsplit(') ', 1)[1].split()
    except (OSError, IndexError):
        return None                                     # vanished


def _starttime(pid):
    f = _fields(pid)
    if f is None:
        return None
    try:
        return int(f[19])                              # field 22
    except (IndexError, ValueError):
        return None


def _state(pid):
    f = _fields(pid)
    return f[0] if f is not None else None             # field 3 (idx 0 after ') ' split)


def main():
    runner, pidfile = int(sys.argv[1]), sys.argv[2]
    me = os.getpid()
    try:
        orig_start = _starttime(runner)
    except Exception:
        return
    if orig_start is None:
        return
    tag = b'DIRECTOR_TEST_OWNER=%d\0' % runner
    recorded = {}                                       # pid -> (starttime, depth)

    def snapshot():
        kids = {}                                       # pid -> parent pid
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            f = _fields(int(entry.name))
            if f is not None:
                try:
                    kids[int(entry.name)] = int(f[1])   # field 4: ppid
                except (IndexError, ValueError):
                    pass
        depth = {runner: 0}
        changed = True
        while changed:
            changed = False
            for pid, ppid_ in kids.items():
                if pid not in depth and ppid_ in depth and pid != me:
                    depth[pid] = depth[ppid_] + 1
                    changed = True
        for pid in depth:
            if pid == runner:
                continue
            st = _starttime(pid)
            if st is not None:
                recorded.setdefault(pid, (st, depth[pid]))
                if recorded[pid][0] != st:              # pid reused: not ours
                    del recorded[pid]
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid in (runner, me):
                continue
            try:
                env = entry.joinpath('environ').read_bytes()
            except OSError:
                continue                                # unreadable: tree is fallback
            if tag in env:
                st = _starttime(pid)
                if st is not None:
                    recorded.setdefault(pid, (st, depth.get(pid, 99)))
                    if recorded[pid][0] != st:
                        del recorded[pid]
        try:
            for line in Path(pidfile).read_text(errors='replace').splitlines():
                try:
                    pid_s, st_s = line.split()
                    pid, st = int(pid_s), int(st_s)
                except ValueError:
                    continue
                if pid == me or _starttime(pid) != st:
                    continue                            # dead or pid reused
                recorded.setdefault(pid, (st, 99))
        except OSError:
            pass
    def sweep():
        for pid, (st, _depth) in sorted(recorded.items(), key=lambda kv: -kv[1][1]):
            if _starttime(pid) == st:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        for pid in list(recorded):                      # groups the children led
            f = _fields(pid)
            if f is not None and _starttime(pid) == recorded[pid][0]:
                try:
                    if int(f[2]) == pid:                # field 5: pgid (idx 2)
                        os.killpg(pid, signal.SIGKILL)
                except (OSError, IndexError, ValueError):
                    pass

    while True:
        snapshot()
        st = _starttime(runner)
        if st != orig_start or _state(runner) == 'Z':   # dead, or zombie not yet reaped
            sweep()
            return
        if not Path(pidfile).exists():                  # runner exited cleanly (_sweep unlinked it)
            sweep()                                     # catch orphans it chose to forget
            return
        time.sleep(0.5)


if __name__ == '__main__':
    main()
