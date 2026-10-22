# M6 verification — prompt validation, comparability and the pilot

Evidence for Milestone 6 of `update-design.md` (lines 270–286). Every row was run in this
worktree against stubs; the live director model and ComfyUI were never contacted.
Run everything: `python3 -m py_compile *.py tests/*.py`, `python3 -m unittest discover tests`,
and `python3 tests/browser_pilot_review.py` (headless Firefox via geckodriver).
Design rule -> skill line mapping lives in `docs/prompt-validation.md`.

| requirement (design line) | implemented in | test | result | limitation |
|---|---|---|---|---|
| empty replies are caught (272) | `h3check.report` checks `empty_reply` and `code_fence_only` (h3check.py:120-122); `director.py` raises `DirectorError` naming the failed checks when the final reply is empty (director.py:194) | `test_h3check.py`: empty/whitespace/code-fence-only replies for every mode; `test_director_retry.py`: empty final reply raises with check names | pass | catches *empty*, not vacuous-but-nonempty text beyond the `refusal_only` guard |
| required sections (272) | `fields_present`, `fields_once`, `fields_in_order`, `fields_non_empty`, `unknown_section` per mode field set (`REF`/`BASE`, h3check.py:124-131) | `test_h3check.py` (38 tests) over real eval replies + crafted breakages (`tests/fixtures/h3/`) | pass | section labels come from the pinned skill; a new skill version needs `h3check.py` changed together (AGENTS gotcha) |
| timestamps (272) | `timestamps_format` (`mm:ss.mmm` strict), `timestamps_monotonic` (strictly increasing after 00:00), `timestamps_in_range` (`0 <= c < seconds`), `first_shot_zero`, `shot_cuts` (cut time right after each label, no strays) — h3check.py:173-204 | `test_h3check.py`: malformed, duplicate, decreasing, out-of-range stamps; missing/detached cut times; stamp inside Shot 1 | pass | stamps are parsed from text only; whether the *rendered* video lands on them is exactly what the pilot measures (rows below) |
| shot numbering (272) | `shot_numbering`: `[Shot 1]..[Shot N]` present, in order, no gaps (h3check.py:164-171) | `test_h3check.py`: renumbered, gapped, duplicate labels | pass | — |
| mode-specific rules (272) | `applicable(mode)` + per-mode checks: `no_instruction_line` (t2va/ref2va must open with the description), exact `instruction_line` for i2va/l2va/fl2va (shot number + `seconds` embedded), `labels_valid` (`<Picture N>` vs provided image count), `picture1_in_shot1` (i2va), `picture_labels_defined` + `summary_task_prefix` (ref2va), `dialogue_tags`, `no_markdown`, `no_placeholders` (h3check.py:139-271) | `test_h3check.py` across all five modes; `test_director_retry.py` feeds the failing check names back to the model for one corrective retry (director.py:170-190) | pass | retry is one round only; against a real model the *failure modes* of the loop (what models actually get wrong after feedback) are unobserved |
| evaluation fixtures portable (274) | `eval/cases.json` fixture map with `kind: generated` ids; `eval/make_fixtures.py` renders deterministic 640x480 PNGs via ffmpeg (fixed colours/shapes/labels, no randomness); `eval.py:case_is_portable` + `prepare_generated` build missing fixtures on demand; `--portable` runs only cases free of private photos | `test_eval.py`: fixture generation determinism, portable case filtering, missing-fixture rows | pass | portable covers 6 of 10 cases (4 ground on the user's private photos); label *text* is the ground truth, so a different font fallback changes bytes but not the case |
| model/settings/skill versions recorded (274) | `eval.run_meta`: `director_url`, `configured_model`, live `models_endpoint` probe, `call_settings` incl. `skill_files` hashes, `skill_revision`, `validator` (h3check version + sha256 of h3check/eval/cases), `git` state, python, host hash | `test_eval.py`: meta recorded per run; `eval_compare.py` refuses two labels unless `validator_key` matches | pass | comparability is *hash-equality*, not semantic equivalence — any h3check/cases edit forces `rescore.py` or a new label |
| failed calls must not disappear (274) | schema 2: every planned `(case, run)` gets a row — `ok`, `call_failed` (error kept), `skipped_fixture_missing`; `write_results` atomically rewrites `results.json` after **every** run (ctrl-C keeps rows); `--resume` refuses on validator/model mismatch (`_validator_key`) | `test_eval.py`: failure rows in totals, crash mid-run keeps completed rows, resume gates | pass | failed and skipped rows count against the total (denominator = planned rows) — pass counts only compare within one validator/case set |
| pilot: three representative scenes, two seeds, two arms, twelve generations (278-281) | `pilot.py plan()`: 3×2×2 = 12 slots fixed, manifest records scenes/refs/idea hash and versions, immutable except slot state | `test_pilot.py`: `test_twelve_slots_two_arms_and_deterministic_order`, `test_manifest_immutable_except_slots` | pass | scene choice is the operator's (`scenes.yaml`); representativeness is human judgement |
| simple user prompt vs director-generated prompt (280) | arms `simple`/`director` (`ARMS`): arm A uses the raw user prompt, arm B runs `director.py` (retry included); `cmd_prompts` records both with `seconds_spent` for arm B | `test_pilot.py`: `test_both_arms_recorded_and_resume_keeps_them`, `test_failed_director_call_is_a_recorded_row`, `test_private_variant_and_portable_fallback` | pass | the simple arm's wording is whatever the operator typed — same caution as the old eval: wording differences are part of the treatment |
| blinded review of action, identity, motion, cinematography, audio (282) | `review_prepare` shuffles clip names (`ARMS`/scene/seed never in filenames), writes `key.json` outside `review/`; leak-word audit of filenames/HTML/ffprobe tags; `review_server` append-only ratings log; `report` unblinds only at the end | `test_pilot.py`: `test_clips_page_and_key_reveal_nothing_about_arms`, `test_append_only_log_and_current_view`, `test_invalid_posts_rejected_and_concurrency_safe`, `test_refuses_until_every_clip_is_rated`; `browser_pilot_review.py`: 12 clips render in Firefox, click+key rating saves, reload restores, served DOM leaks nothing | pass | blinding is by presentation; a slot that *failed to render* could still hint at an arm — failed slots are excluded from rated stats |
| acceptance rate (284) | `_stats_block`: `acceptance_rate = accepted / rated`, per arm and per scene; report refuses until every clip is rated (`--partial` labels partials) | `test_pilot.py`: `test_full_report_arithmetic_and_no_prompt_metrics` | pass | n = 4 slots per arm: the report stamps `small_n` — a rate here is a signal, not a measurement |
| time per accepted take, wall vs backend (284) | `history_times` reads ComfyUI's `execution_start`/`execution_success` (`backend_seconds`); the pilot records its own `wall_seconds`; arm B adds `prompt.seconds_spent`; `_stats_block` reports `per_accepted_take_backend_seconds`, `per_accepted_take_wall_seconds` and wall+prompt **separately**, failed/unrendered slots spend time with zero accepts | `test_pilot.py`: `test_all_twelve_one_at_a_time_with_split_times`, `test_failed_slots_cost_time_with_zero_accepts` | pass | backend stamps need a ComfyUI build that emits them (older builds leave `backend_seconds` null) — checklist A3 |
| prompt length / keyword matches / format compliance are NOT quality evidence (286) | `pilot.py:CAUTION` printed in `report.md` and stored in `report.json`; the report carries **no** prompt length/token/keyword/format columns at all | `test_pilot.py`: `test_full_report_arithmetic_and_no_prompt_metrics` (asserts the absence) | pass | enforcing "do not treat" is a report design, not a proof: only the blinded ratings move any decision |

## MOCKED checks (ran here)

- `test_eval.py` drives `eval.py` against an **in-process stub model server** (ok/failing/empty
  replies); no live director model produced a schema-2 `results.json`.
- `test_pilot.py` renders the 12 slots against an **in-process ComfyUI stub** (scripted
  queue/history with the execution stamps); clips are re-encoded by the **real ffmpeg** from a
  tiny fixture, so blinding/tag checks are real-media checks.
- `test_h3check.py`/`test_director_retry.py` pin every rule on text only — no model call is
  made outside the retry-loop stub.
- `browser_pilot_review.py`: real headless Firefox against the real review page; the clips are
  the stub-rendered fixtures.

## REAL-BACKEND checks (none run here)

Merged into `docs/real-backend-checklist.md`: A3 (ComfyUI timing stamps exist for the pilot),
C1 (first live schema-2 `eval.py --portable` + `eval_compare`), C2 (*Write prompt* live, pass
count must not drop), C3 (the retry loop against real model mistakes), D1 (run + review the
12-slot pilot; unknown-slot handling, one machine/one model).

## Limitations

- n=12 (4 per arm): acceptance-rate differences of one take are noise; the report says so.
- `h3check` version 2 is stricter than the version 1 of the two saved labels; they must be
  re-scored (`rescore.py`) before any `eval_compare.py` gate passes — the gate refuses
  otherwise rather than comparing apples to oranges.
- Backend vs wall time split is unverified against a real ComfyUI build until checklist A3.
- `--portable` covers 6 of 10 eval cases; the private-photo cases stay un-runnable on another
  machine by design.
- Pilot takes land in ComfyUI's output folder; `pilot-runs/` stores paths and metadata only.
