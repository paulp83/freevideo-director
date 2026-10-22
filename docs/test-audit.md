# Retrospective test audit

Why: the agents wrote their own tests and no hash baseline was recorded, so this audit reviews every later edit to a test
file instead. Method: for each file under `tests/` (py, js, golden), every commit after its creating commit on
`main..m6-evaluation` was diffed; removed assertion-like lines, deleted tests/files, added skips and changed numeric
thresholds were extracted (`director-work/audit/extract.py`) and each hit was matched against what exists today.

## Scope
- 91 later edits across the test files; 88 removed assertion-like lines.
- No test file was deleted. No skip/xfail was added (one comment mentioning a skipped tearDown only).
- One test was renamed, not deleted: `test_a_v1_database_gains_the_recovery_and_retry_markers_...` (jobs_db).

## Removed or changed assertions, grouped
| Group | Examples | Verdict |
|---|---|---|
| Product behaviour changed on purpose, assertion replaced by an equal-or-stronger one | prompt ids `stub-1` -> client-supplied ids with echo equality; `RESERVES == {}` after an unknown job -> exact hold map (`{scene_key: job_id}`) and release asserted for finished/failed/cancelled; header chip text -> `Prompts Down / Video Ready / Finishing Down` with name + state at 5 widths; take-strip `preload none` -> zero `<video>` and zero mp4 requests before interaction; disabled-reason titles -> `help — reason` | justified |
| Moved to another test | film/preview fps and crf defaults (`test_config` -> `test_finishing`: 48/'16' film, 24/'20' animatic); snapshot `finishing` keys (now with stage list and output fps) | justified, equivalents verified present |
| Fixture/count changes | queue rows 6 -> 7 and live count after the Retry fixtures were added | justified |
| Leak sweep edits (4fc080d) | `sys.exit(...)` and the geckodriver session check replaced by the shared leak-guard exit path | justified (suite fails on leftovers) |
| Threshold loosened | comparison players: brief said >=30% of viewport height, test asserts >=25% | justified: two 16:9 players side by side at 1440x900 cap at ~28% (454x255 px); disclosed by the agent |
| Coverage dropped | acceptance suite lost the keyboard path for the **Retry** dialog (its stub never offers `retry_allowed`); `browser_compare` still covers the button's gating, but no test drives Retry's dialog by keyboard or checks focus restoration after it | **gap, open** |
| Behaviour weakened | conflict dialog: the check "stale tab keeps its draft" was removed when **Cancel was changed to Reload**, which discards the unsaved edits. The design asks that a conflict "preserves the local draft" | **defect, open** (see below) |

## Open findings
1. **Conflict dialog discards edits on Cancel/Escape** (`project.html` autosave core, introduced with the hardening pass
   `a1a13ec`). A user who dismisses the native `confirm()` loses their draft. Fix proposed: a three-way in-page dialog
   (Keep mine / Reload / Decide later) where closing leaves the draft pending and dirty; reinstate "stale tab keeps its
   draft" and cover the new choices in `browser_autosave.py` and `tests/autosave_check.js`.
2. **Retry dialog keyboard/focus test missing.** Add a scenario with a stub that offers `retry_allowed` (the queue launcher
   already seeds retry rows) driving open, Tab trap, Escape and focus restoration by keyboard.

## Limits of this audit
- It only sees edits after a file's first commit. A test that was weak when first written, or an assertion that never
  existed, is invisible to this method. Mutation checks (break the product on purpose, expect a test to fail) would cover
  that; none were run.
- One browser test (`browser_layout`, "Details shows the recorded provenance") failed once and passed on rerun; browser
  suites contain timing sleeps and polls, so occasional flakes are possible. Not investigated beyond the rerun.
