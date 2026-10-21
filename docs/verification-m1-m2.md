# M1/M2 verification + hardening pass

Evidence for the eight review risks in the M1/M2 brief. Every row was run in this worktree;
corrective commits are the ones that changed behaviour, test-only commits are marked as such.
Run everything with: `python3 -m unittest discover tests`, `node tests/autosave_check.js`,
`python3 tests/browser_autosave.py`.

| requirement | implemented behaviour | test performed | result | corrective commit |
|---|---|---|---|---|
| 1. unknown jobs must keep their reservations | an `unknown` job keeps its scene/take reservations and prompt ids until `reconcile()` (queries ComfyUI `/history/<id>` and `/queue`) proves finished/failed/interrupted/gone, or the user dismisses it via `POST /api/job/<id>/resolve`; a late-proven completion records the take exactly once; unknown jobs stay visible in `/api/queue` and block new work on the scene | `test_jobs.py`: `test_timeout_keeps_the_prompt_id_and_says_unknown`, `test_unknown_job_keeps_the_scene_reserved`, `test_reconcile_proves_the_unknown_job_finished_and_records_the_take_once`, `test_reconcile_while_still_running_keeps_everything`, `test_reconcile_releases_a_prompt_that_stopped_or_failed`, `test_queue_view_shows_an_unknown_job_with_its_prompts`, `test_dismissing_an_unknown_job_frees_the_scene` | pass | 5a7a3a9 |
| 2. enhance temp dir lifecycle | the workdir is removed only after confirmed completion, confirmed failure/cancel, or proof the graph was never submitted; kept (path recorded on the job) while the job is `unknown`; deleted when reconcile/dismissal resolves it; `enhance.sweep_workdirs` + `web.sweep_enhance_tmp` remove stale dirs owned by no live/unknown job after a day, at startup and periodically | `test_enhance_tmp.py`: `test_two_enhances_get_distinct_dirs_and_both_are_removed`, `test_workdir_is_removed_after_failure`, `test_workdir_is_removed_when_ffmpeg_fails`, `test_workdir_is_removed_after_cancel`, `test_submit_cleans_its_workdir_when_the_graph_is_refused`, `test_unknown_enhance_keeps_its_dir_until_reconciled`, `test_unconfirmed_cancel_keeps_the_dir`, `test_dismissal_deletes_the_kept_dir`, `test_sweep_clears_crash_leftovers_but_not_owned_or_fresh_dirs` | pass | 3bb7b13 |
| 3. cancellation must target only the cancelled job's prompts | after `submit_prompt` returns, the worker re-checks `cancel` and issues a targeted cancel (`web.comfy_queue` + `web.target_cancel`: `/queue delete` for a pending id, `/interrupt` only if that id is the running one); an unconfirmed targeted cancel ends the job `unknown`, not `cancelled`; the poll path issues the same targeted cancel | `test_jobs.py`: `test_cancel_during_submit_targets_only_the_new_prompt`, `test_unconfirmed_cancel_becomes_unknown`, `test_poll_cancel_interrupts_when_our_prompt_is_running`, `test_cancel_mid_prep_never_submits`, `test_cancel_before_start_never_submits`, `test_cancel_mid_poll_does_not_resubmit` | pass | ded7889 |
| 4. Keep mine must re-apply only the user's dirty fields and never drop server-side takes | the page sends only dirty scenes as patches with `scenes_delta: true` (structural changes stay full-list); `conflicts()` compares per-field key_revs against `base_rev` — a revision number alone is never trusted; a delta naming a scene the server deleted conflicts; server-recorded takes ride through untouched | `test_revisions.py`: `test_delta_save_keeps_takes_recorded_server_side`, `test_delta_only_touches_the_scenes_it_names`, `test_delta_naming_a_deleted_scene_conflicts`, `test_delta_applies_the_first_scene_rules_and_prompt_history`; browser scenario 7 (take injected on disk mid-conflict, Keep mine keeps it, only the edited fields change) and 7b (Reload discards the draft and adopts the rev) | pass | 049c15a |
| 5. real-JS behaviour incl. failed-save Reload/Keep mine, poll during in-flight save, surfaced flushEdits rejection | reran the suite and extended it; two real page defects fixed: the save loop retried a failing save forever (so `flushEdits()` could never reject and dependent actions were not stopped), and Reload left the discarded draft text in the still-focused input, so it rode into the next save | `tests/browser_autosave.py` scenarios 7, 7b, 8 (in-flight save + 3 s poll), 9 (Assemble refuses: zero `/assemble` POSTs, rejection in the status line, no save storm); 31 checks total | pass | a1a13ec |
| 6. submitted graph/snapshot/uploads come from one captured read | not a defect: `render_job` loads the project once (web.py), then graph, picture uploads, snapshot and take record all derive from that captured dict — test proves it | `test_immutable_inputs.py`: a rewrite of prompt/seconds/seed and ref pictures on disk inside the window between `build_graph` and `POST /prompt` changes nothing: submitted body and snapshot keep the captured values, uploaded bytes are the originals | pass | db72fb8 (test only) |
| 7. derived status must not be persisted | nothing is: `project.py` contains no `status` key at all; the scene record (project.py:313-315) holds only inputs, takes and the explicit selection (`star`/`selected_take`/`selection` — a pin, not a status); the page's draft/final/prompt/none chip is `sceneState()` computed from takes + chosen (project.html:497-501); job verdicts are computed live from ComfyUI history | grep over `project.py`, `web.py`, `stats.py`, pages | confirmed, no code change | — |
| 8. legacy selection unchanged; assembly freezes take ids at start | scenes with no explicit selection keep the latest-take behaviour; `assemble()` resolves every scene's take and builds the clip list + `used_takes` before any ffmpeg runs, so a take finishing mid-assembly cannot enter the cut | `test_selection.py`: `test_auto_choice_follows_the_latest_take`, `test_old_project_star_resolves_and_a_get_rewrites_nothing` (old files byte-for-byte unchanged on read), `test_assemble_records_the_used_takes_and_the_record_never_moves`, new `test_a_take_that_lands_mid_assemble_is_not_in_the_cut` (take injected during the first duration probe stays out of every ffmpeg command and out of `used_takes`) | pass | 6faf41b (test only) |

## MOCKED checks (ran here)

- Stub ComfyUI (`tests/test_jobs.py` StubComfy; modes complete/hang/drop/reject/gate/gone/
  interrupted/failed): submit/refusal/timeout/cancel/reconcile/queue shapes are *modelled* from
  the documented API, not observed from a running ComfyUI.
- Fake ffmpeg via `projects.subprocess` (probe durations, encode success); continuation frame
  extraction tests in `test_selection.py` do use the real ffmpeg binary.
- Headless Firefox (geckodriver) against the real `web.py` on a spare port with a temp projects
  root; save failures and delays injected through a fetch hook.

## REAL-BACKEND checks (not run; run against ComfyUI on port 8190)

- Timeout → `unknown` → `reconcile()` against live `/history` and `/queue`: verify real entry
  shapes match what `job_verdict` expects (completed / status_str error / recorded-but-uncompleted).
- `/interrupt` by prompt id: confirm it stops only the named prompt and never a queued neighbour.
- `/queue delete` of a pending id: confirm it really removes it (targeted cancel stays `cancelled`,
  not `unknown`).
- Enhance end-to-end: raw `video.artifacts/rgb.npy` + `audio.wav` (and the mp4 fallback once the
  raw frames are freed), SeedVR2 with `decode_tiled=True`, `batch_size=4n+1`.
- Real enhance-tmp sweep against directories left by a crashed server.
- Full-film assemble and continuation first-frames from real takes (the unit tests cover the
  logic with stubs; the media never ran through here).
- *Write prompt* / storyboard generation against the live director model (`eval.py` pass count
  must not drop).

## Migration notes

- `project.json` format is unchanged; legacy projects (bare `star`, no selection keys) load and
  work unchanged; a GET rewrites nothing, the first write folds `star` into
  `selected_take`/`selection`.
- Editor saves may carry `scenes_delta: true` with only the dirty scenes. This needs the matching
  `web.py`: an older server would treat a partial scene list as a full replacement.
- New endpoint `POST /api/job/<id>/resolve` (dismisses an unknown job; the response warns that
  dismissing may allow a duplicate generation).
- After a timeout or an unconfirmed cancel the scene stays reserved until reconcile or dismissal;
  that blocking is intentional.
