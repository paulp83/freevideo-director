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
| Coverage dropped | acceptance suite lost the keyboard path for the **Retry** dialog (its stub never offers `retry_allowed`); `browser_compare` still covers the button's gating, but no test drove Retry's dialog by keyboard or checked focus restoration | **gap, resolved** (finding 2) |
| Behaviour weakened | conflict dialog: the check "stale tab keeps its draft" was removed when **Cancel was changed to Reload**, which discards the unsaved edits. The design asks that a conflict "preserves the local draft" | **defect, resolved** (finding 1) |

## Findings
1. **Conflict dialog discarded edits on Cancel/Escape** — *resolved* (`19c738b`). The workspace page now asks with
   Keep mine / Reload / Decide later in an in-page dialog (default focus on Decide later). Escape and Decide later keep
   the draft pending behind a banner; nothing is written or adopted until the user chooses. Covered by
   `tests/browser_autosave_workspace.py` (stale tab keeps its draft, a poll does not discard it, Reload and Keep mine from
   the dialog and from the banner). The classic fallback page still uses the old native confirm.
2. **Retry dialog had no keyboard-only test** — *resolved* (`browser_queue.py`): Enter opens it, Tab stays inside, Escape
   returns focus to the Retry… button and submits nothing.
3. **The autosave browser suite only ever drove `/project/classic`** — *found while fixing 1, resolved*. The workspace page,
   which ships, had no delayed-save, failed-save, switch, two-tab or poll scenarios in a real browser. A workspace version
   of the suite now exists (`tests/browser_autosave_workspace.py`, 31 checks); the classic suite stays for the fallback.
   The audit method (edits after first commit) could not see this: the gap was a suite that was weak from birth.
4. **Real-backend run exposed stubs that guessed ComfyUI's shapes** — fixed with real captures pinned as fixtures
   (`tests/fixtures/comfy_history`, `comfy_object_info`). A stub-only suite had passed while a failed prompt read as
   "interrupted" and an installed RIFE model read as unconfirmed.

## Limits of this audit
- It only sees edits after a file's first commit. A test that was weak when first written, or an assertion that never
  existed, is invisible to this method. Mutation checks (break the product on purpose, expect a test to fail) would cover
  that; none were run.
- One browser test (`browser_layout`, "Details shows the recorded provenance") failed once and passed on rerun; browser
  suites contain timing sleeps and polls, so occasional flakes are possible. Not investigated beyond the rerun.
