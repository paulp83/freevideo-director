"""The leak watchdog: kills a SIGKILLed runner's children, nobody else's."""
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))                                  # repo root
RUNNER = '''
import sys, time
sys.path.insert(0, {tests!r})
import leak_guard
child = leak_guard.spawn([sys.executable, '-c',
    'import subprocess, time; '
    'subprocess.Popen(["sleep", "120"]); time.sleep(120)'])
leak_guard._children.remove(child)          # leave the orphan for the watchdog
print("CHILD", child.pid, flush=True)
sys.stdin.readline()                        # 'go' -> normal exit; SIGKILL cases never send
'''.replace('{tests!r}', repr(str(HERE)))


def _alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True


def _tagged(runner_pid):
    tag = b'DIRECTOR_TEST_OWNER=%d\0' % int(runner_pid)
    out = set()
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if tag in entry.joinpath('environ').read_bytes():
                out.add(int(entry.name))
        except OSError:
            pass
    return out


def _watchdogs(runner_pid):
    out = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmd = entry.joinpath('cmdline').read_bytes().replace(b'\0', b' ').decode()
        except OSError:
            continue
        if 'leak_watchdog.py' in cmd and cmd.split()[-2] == str(runner_pid):
            out.append(int(entry.name))
    return out


def _start():
    proc = subprocess.Popen([sys.executable, '-c', RUNNER], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True)
    line = proc.stdout.readline().split()
    time.sleep(1.2)                                   # let the watchdog snapshot once
    return proc, int(line[1])


def _info(pid):
    try:
        st = Path(f'/proc/{pid}/stat').read_text(errors='replace').rsplit(') ', 1)[1].split()
        cmd = Path(f'/proc/{pid}/cmdline').read_bytes().replace(b'\0', b' ').decode()[:70]
        return f'{pid} state={st[0]} ppid={st[1]} start={st[19]} cmd={cmd!r}'
    except OSError:
        return f'{pid} gone'


def _gone_within(pids, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not any(_alive(p) for p in pids if p):
            return True
        time.sleep(0.2)
    return False


class Watchdog(unittest.TestCase):
    def test_sigkill_runner_sweeps_child_and_grandchild(self):
        runner, child = _start()
        grand = next(iter(_tagged(runner.pid) - {child, runner.pid} -
                          set(_watchdogs(runner.pid))), None)
        os.kill(runner.pid, signal.SIGKILL)
        self.assertTrue(_gone_within([child, grand]),
                        'survived: ' + ' | '.join(_info(p) for p in (child, grand) if p)
                        + ' | runner: ' + _info(runner.pid))
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and _watchdogs(runner.pid):
            time.sleep(0.2)
        self.assertEqual(_watchdogs(runner.pid), [], 'watchdog outlived its sweep')

    def test_normal_exit_with_orphaned_child_is_swept(self):
        runner, child = _start()
        self.assertTrue(_alive(child), 'child died before the watchdog had to sweep')
        runner.stdin.write('go\n')
        runner.stdin.flush()
        runner.wait(timeout=10)                       # normal exit, child left running
        self.assertTrue(_gone_within([child]), 'watchdog did not sweep on normal exit')

    def test_unrelated_process_is_untouched(self):
        outsider = subprocess.Popen(['sleep', '60'])  # our child, not the runner's
        runner, child = _start()
        os.kill(runner.pid, signal.SIGKILL)
        self.assertTrue(_gone_within([child]))
        time.sleep(2.0)                               # past any sweep window
        self.assertTrue(_alive(outsider.pid), 'watchdog killed an unrelated process')
        outsider.terminate()
        outsider.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
