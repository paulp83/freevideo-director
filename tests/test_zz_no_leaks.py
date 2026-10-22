"""Suite-level process hygiene. The name sorts after every other test_* module, so
discover runs it last, after every harness has had its tearDown.

Fails the suite when a watched server -- this repo's web.py, tests/stub_comfy.py,
geckodriver or a headless firefox -- is still alive once the suite is over. Processes
that were already running when the run started (a developer's own server, the user's
browser) are baseline and never reported: only processes the run itself started can
fail this check. The harnesses keep their own terminate/kill in tearDown; the guard is
the net that makes forgetting one visible instead of poisoning the next run's ports.
"""
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import leak_guard


class NoLeftoverProcesses(unittest.TestCase):
    def test_no_watched_process_survived_the_suite(self):
        leaks = leak_guard.final_leaks()
        self.assertFalse(
            leaks, 'leftover test servers after the suite: '
            + '; '.join(f'{pid}: {cmd[:160]}' for pid, cmd in leaks))


if __name__ == '__main__':
    unittest.main()
