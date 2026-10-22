"""Process hygiene shared by every suite that starts servers.

Every watched child must be started with spawn() so the interpreter reaps it at exit even
when setUp dies half way, stopped with stop() (terminate, then kill past a grace), and
final_leaks() scans /proc for watched servers that outlived the run: it is called by
tests/test_zz_no_leaks.py (the suite-level gate, named to sort last under discover) and
by each browser suite before its exit code.

Watched command lines: this repo's web.py, tests/stub_comfy.py, geckodriver and a
headless firefox. Anything already running when this module is imported -- a developer's
own director server, the user's browser -- is baseline and never reported, so the check
only ever blames processes the run itself started.
"""
import atexit
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

_WATCH_FILES = (str(REPO / 'web.py'), str(REPO / 'tests' / 'stub_comfy.py'))
_me = os.getpid()


def _match(cmdline):
    """Why this command line is a watched server, or None."""
    for needle in _WATCH_FILES:
        if needle in cmdline:
            return needle
    for tok in cmdline.split():
        if tok.split('/')[-1] == 'geckodriver':
            return 'geckodriver'
    if 'firefox' in cmdline and '-headless' in cmdline:
        return 'firefox -headless'
    return None


def _scan():
    """pid -> (starttime, cmdline) for every watched process alive right now."""
    found = {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == _me:
            continue
        try:
            cmdline = (entry.joinpath('cmdline').read_bytes()
                       .replace(b'\0', b' ').decode(errors='replace').strip())
            stat = entry.joinpath('stat').read_text(errors='replace').rsplit(') ', 1)[1].split()
        except OSError:
            continue                                    # vanished mid-scan
        if _match(cmdline):
            found[pid] = (stat[19], cmdline)            # field 22: starttime
    return found


BASELINE = _scan()                                      # not ours: never reported

_children = []                                          # every spawn() we handed out


def spawn(cmd, **kw):
    """Popen, registered so the atexit sweep below stops it even if the test died
    between starting and cleaning it up."""
    proc = subprocess.Popen(cmd, **kw)
    _children.append(proc)
    return proc


def stop(proc, grace=5.0):
    """terminate, then kill past the grace; always ends waited."""
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def final_leaks():
    """Watched servers alive now that were not alive at import: [(pid, cmdline)]."""
    now = _scan()
    return sorted((pid, now[pid][1]) for pid in now
                  if pid not in BASELINE or BASELINE[pid][0] != now[pid][0])


def _sweep():
    for proc in _children:
        stop(proc, grace=3.0)


atexit.register(_sweep)
