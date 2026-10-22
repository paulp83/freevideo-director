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
