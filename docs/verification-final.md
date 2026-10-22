# Verification — the whole update (M1–M6)

Evidence for the full branch, 93 commits from `main` (`1395bd5`) to here. Per-milestone
matrices already exist — [verification-m1-m2](verification-m1-m2.md),
[verification-m2](verification-m2.md), [verification-m3](verification-m3.md),
[verification-m4](verification-m4.md), [verification-m5](verification-m5.md),
[verification-m6](verification-m6.md) — this file is the gate record for the merged
branch plus what remains unproven without real backends
([real-backend-checklist](real-backend-checklist.md)).

## Gate battery (run on the final tree, one suite at a time)

| Gate | Result |
|---|---|
| `python3 -m py_compile *.py tests/*.py` | PASS |
| `python3 -m unittest discover tests` — run 1 | Ran 345 tests — OK (skipped=10) |
| `python3 -m unittest discover tests` — run 2 | Ran 345 tests — OK (skipped=10) |
| leftover-process scan after each discover run (`/proc` for `web.py`, `stub_comfy.py`, geckodriver, headless firefox) | 0 leftovers after both runs |
| leak gate inside the suite (`tests/test_zz_no_leaks.py`, runs last) | part of the 345 above — the suite fails if a watched process survives |
| `node tests/autosave_check.js` | 10/10 autosave-core checks |
| `tests/browser_autosave.py` | 33/33 |
| `tests/browser_status.py` | 19/19 |
| `tests/browser_layout.py` | 78/78 |
| `tests/browser_compare.py` | 68/68 |
| `tests/browser_queue.py` | 61/61 |
| `tests/browser_readiness.py` | 21/21 |
| `tests/browser_finishing.py` | 18/18 |
| `tests/browser_acceptance_m3.py` | 75/75 |
| `tests/browser_pilot_review.py` | 22/22 |
| leftover scan after every browser suite | 0 leftovers after every one of the 11 runs |
| `DIRECTOR_SLOW_SUITES=1 python3 -m unittest tests.test_restart_acceptance` | Ran 10 tests — OK, 0 leftovers |

## Milestone → commits map

| Milestone | Commits (on this branch) |
|---|---|
| M1 job states, reservations, autosave core, enhance temp dirs | `d2266e0`…`3bb7b13` (incl. merge `9b20234`) |
| M2 immutable provenance, selection, fingerprints, derived flags | `dd004bb`, `c0089a3`, `9f7ddfe`, `4c39d42`, `86e60b0`, `ced4b0a`, `6faf41b`, `db72fb8` |
| M3 workspace redesign (grid, viewer, queue drawer, compare, posters) | `320b9e2`…`38c0ffe` |
| M4 durable job store, reattach, retry, restart acceptance | `7ad7000`…`d23b57e` |
| readiness chips + config centralisation | `c932a3a`…`a0928c4` |
| M5 per-project finishing settings, one fps policy | `cf4e617`…`b0b3ddd`, plus `963cb6b`, fixes `25163fc`–`d42ae79` |
| M6 h3check v2, portable eval, schema 2, rescore/compare, pilot | `5d570f4`…`9fe5774` |
| wrap-up (checklists, leak sweep, docs, migration) | `34ceca9`…HEAD |

## Mocked versus real, in one place

Everything above is provable with stubs, and the stubs are honest about what they
cover: the ComfyUI stub reproduces queue/history JSON shapes, the dropped-submit and
interrupt paths, and the node inventories — never GPU behaviour. No measurement in any
verification doc was taken against a real GPU, real SeedVR2/RIFE, or a live director
model unless it says so explicitly:

- **Real backends used:** the render timings, SeedVR2 memory numbers, encode
  quality comparisons and the original eval A/B in the earlier docs were measured once
  on real hardware earlier in the project; the *re-scored* eval numbers
  (`docs/prompt-validation.md`) reuse those recorded outputs under today's validator.
- **Never exercised here:** every item in the
  [real-backend checklist](real-backend-checklist.md) — reattach on a real killed
  render, SeedVR2/RIFE end to end, the live schema-2 eval run, and the pilot itself,
  which needs 12 real renders plus a human review.

## Process cleanup

Test harnesses spawn servers (`web.py`, `tests/stub_comfy.py`, geckodriver, headless
Firefox). `tests/leak_guard.py` stops them on any normal end of a run (atexit plus the
browser harnesses' `tearDown`), and `tests/test_zz_no_leaks.py` fails the suite if any
survive. Neither runs when the runner itself is SIGKILLed, so `leak_guard` also starts a
detached `tests/leak_watchdog.py`: it tracks every descendant, every process carrying
`DIRECTOR_TEST_OWNER=<runner pid>` in its environment, and every pid `spawn()` recorded
in a pidfile — re-checking start times each 0.5 s so a reused pid is never killed — and
SIGKILLs them deepest-first the moment the runner exits, crashes, or is killed. The
watchdog only ever touches pids it recorded, so a user's own servers and browser are
off-limits. On a clean exit `leak_guard` unlinks the pidfile, which tells the watchdog
to sweep anything left (including orphans `stop()` was told to forget) and quit, and
`_sweep()` waits for it — so the runner is finished only once everything it started,
watchdog included, is gone. Proofs, each with an external `/proc` scan (matching
web/stub cmdlines, geckodriver, headless Firefox, and the watchdog itself):

| Run | state at scan | leftovers |
|---|---|---|
| `kill -9 tests/browser_status.py` at 25 s, scan 4 s later | 7 checks done, servers + Firefox live | 0 (was: 2× web.py, geckodriver, Firefox) |
| `kill -9` restart-acceptance runner at 12 s, scan 4 s later | 8 tests run, servers live | 0 |
| `kill -9 tests.test_finishing_acceptance` at 8 s, scan 4 s later | mid ffmpeg export, servers live | 0 |
| `timeout -s KILL 20 unittest discover tests`, scan 3 s later, rc 137 | mid finishing-acceptance | 0, watchdog itself included |
| full suite ×2, node autosave check, `browser_autosave.py`, `browser_pilot_review.py`, scan 0.5 s after each | normal exit | 0 |

`tests/test_leak_watchdog.py` (3 cases, green on repeated standalone runs and under
discover) covers sweep after SIGKILL of the runner, sweep of an orphan deliberately
forgotten by `stop()`, and that an unrelated process survives untouched.


## Remaining limits

- Restart recovery, retry and reconcile are proven against a stub ComfyUI process and
  against real process kills of the director — not against a real render mid-flight.
  Group A/B of the checklist covers exactly that gap.
- The pilot framework is tested (blinding, stats split, review UI) but no verdict
  exists until the 12-slot run is done by a human (checklist D1).
- `eval/` labels on disk are schema 1, re-scored, not re-run: first live schema-2 run
  is checklist C1.
- Browser suites cover desktop widths plus one phone width on the layout suite; the
  rest run at 1440-wide. Touch behaviour beyond that is unchecked.
- Voice: unchanged by this branch, still untested.


## Real-backend run (2026-10-21/22) and the final gate battery

Run against ComfyUI 0.34.0 (RTX 5090, FreeVideo) and the live director model `qwen3.8-27b`, with a throwaway projects root
and its own `jobs.db`. Per-item results are in `docs/real-backend-checklist.md`: groups A and B pass, C1 and C2 pass, C3 is
partly verified (no real model mistake occurred), D (human) is open.

Bugs found only by the real backend, each fixed with a test and a real capture where one applies:
| Found by | Bug | Fix |
|---|---|---|
| A1 | a failed prompt classified `interrupted` (stubs put `status_str` at the wrong level) | `30d9752` |
| A1 | installed RIFE model read as unconfirmed (legacy combo shape) | `8399977` |
| real renders | a cached repeat recorded the same video as a second take | `e99f9b5` |
| B2 | cancel with a neighbour queued ended `unknown` for ~17 s | `5ed402d` |
| B11 | render progress stuck at 50/50 for 70 s and absent after a reattach | `5ed402d` |
| B10 | a live unanswered submit never offered Retry (jobs.db v4 `submit_unanswered`) | `916db70` |
| B5 | page said SeedVR2 outputs 1920×1080; it keeps the take's aspect (1890×1080) | `916db70` |
| result files | the render stage never recorded an end time | `19c738b` |
| restart suite | a test depended on whether a real model happened to run on the machine | launchers pin a dead `DIRECTOR_URL` |

Final gates on the last commit: unit 370 OK (10 skipped); node autosave core 10/10; restart acceptance 10/10;
browser: autosave (classic) 33, autosave workspace 31, status 19, layout 78, compare 68, queue 62, readiness 21,
finishing 18, acceptance 75, pilot review 22; zero leftover processes after the batch.
