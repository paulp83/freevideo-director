# M2 verification — takes, provenance, selection and cut fingerprints

Evidence for Milestone 2 of `update-design.md` (lines 94–141). Every row was run in this worktree.
The scenario itself is pinned by `tests/test_m2_acceptance.py`; the individual mechanisms are pinned
by the module suites named in each row.

Run everything: `python3 -m py_compile *.py tests/*.py`, `python3 -m unittest discover tests`
(→ `Ran 105 tests in 29.2s`, OK), `node tests/autosave_check.js` (→ 10 checks ok), plus
`python3 web.py --port 8199` and `curl` on the two endpoints that answer derived state (see
*Live wire check*). No page changed in this pass, so the geckodriver suites
(`tests/browser_autosave.py`, `tests/browser_status.py`) were not re-run.

| requirement (design line) | implemented in | test | result | limitation |
|---|---|---|---|---|
| app-owned job and take ids (102) | `project.new_take_id` (16 hex) and `web.new_job` ids recorded as take keys `id`/`job`/`provenance`; records at `jobs/<id>.json` + `jobs/<id>.result.json` (`project.job_path` refuses any id that is not `[0-9a-f]{8,32}`) | `test_provenance.py`: `snapshot_lands_before_the_graph_is_submitted_and_never_changes`, `record_files_are_write_once`; `test_m2_acceptance.history_intact` re-reads every recorded job after each change | pass | ids exist only for takes rendered after M2; older rows keep `provenance: legacy` and are addressed by run directory |
| request snapshot saved before executing, results attached separately (102) | `web.render_job` → `write_job_snapshot` **before** `submit_prompt`, `web.write_job_result` → `project.write_job_result` once when the job ends; neither file is rewritten afterwards | `test_provenance.snapshot_lands_before_the_graph_is_submitted_and_never_changes`, `record_files_are_write_once`, `test_immutable_inputs.every_input_freezes_before_the_submit` | pass | a job that dies mid-render leaves a snapshot with no sibling result: the take is simply absent, nothing is inferred |
| the eleven recorded fields (106–115) | `web.render_snapshot`: project/scene ids, exact prompt + `mode`, `seed` (0 stays 0), `width`/`height`/`seconds`/`two_pass`, refs with `index` + `sha256`, `loras` (name/strength/enabled), `first` (frame hash + source take), the submitted `graph`, `director.model` + `director.skill_revision`, `finishing` (presets that finish); output references (`run`/`path`/`url`/`final`) in the result file | `test_provenance`: `prompt_seed_and_settings_are_recorded_as_submitted`, `variant_and_finishing_settings_are_recorded`, `reference_pictures_keep_order_hash_and_recreatable_copy`, `continuation_records_the_source_take_and_the_frame_bytes`, `project_view_exposes_provenance`; `test_m2_acceptance.test_a_take_shows_the_inputs_that_actually_ran` | pass | `director.model` is null when the prompt was typed rather than written in that job; `finishing` is null for Draft (nothing was finished) |
| retain reference assets — a hash cannot recreate a missing input (117) | `project.store_ref` copies every submitted picture/frame into `refs-store/<sha256><ext>` and the snapshot points at the copy | `test_provenance.reference_pictures_keep_order_hash_and_recreatable_copy`, `snapshot_and_refs_store_survive_pruning`, `prune_never_offers_record_paths_as_candidates`; `test_m2_acceptance.case_refs` (old bytes read back after replacement) and the take test (store bytes equal the bytes uploaded) | pass | one copy per distinct input, never garbage-collected: `refs-store/` grows with the film |
| legacy takes say *provenance unavailable*, settings never invented (119) | `project.take_provenance`; every flag reads `unknown_legacy` for such a take; `web.take_view` answers `{'provenance': 'legacy'}` with the take only | `test_provenance.legacy_takes_stay_legacy_and_are_never_backfilled`, `enhance_of_a_legacy_take_says_legacy`, `test_fingerprint.legacy_output_reports_unknown_legacy_and_get_writes_nothing`; live: `GET /api/p/lasso-test` on the untouched project | pass | for a legacy take nothing at all is decidable — `outdated` stays empty, and its continuation flag is `unknown_legacy` too |
| explicit selection vs automatic latest take (123) | `project.selection_of` / `chosen` / `set_explicit` / `set_auto` / `migrate_selections`; endpoint `POST /api/p/<slug>/star` (`{'take'}` pins, `{'auto': true}` releases) | `test_selection`: `auto_choice_follows_the_latest_take`, `explicit_choice_survives_newer_takes`, `auto_release_returns_the_scene_to_its_latest_take`, `star_endpoint_selects_by_id_and_auto_releases`, `a_finished_render_never_moves_an_explicit_pick`; the acceptance baseline pins scene 1 over HTTP | pass | the pin is per scene; there is no per-cut pin (a cut freezes `used_takes` instead, row below) |
| record which upstream take produced each continuation (124) | `web.continuation` resolves the source at submit time, `web.source_take_of` reads it back from the snapshot the same job wrote: take `source_take = {scene, take, run, frame_sha256}`, snapshot `first.source` | `test_provenance.continuation_records_the_source_take_and_the_frame_bytes`, `test_selection.continuation_take_record_names_its_source_take_and_frame_hash`, `continuation_from_a_legacy_take_names_the_run`; acceptance take test | pass | for a legacy source there is no id, so the run directory stands in |
| flag a continuation whose source no longer matches the selected upstream take (125) | `continuity_outdated` in `project.scene_status`, comparing the snapshot's recorded source with `project.chosen(previous)` | `test_status.continuity_reasons_name_the_drift`, `test_table`; `test_m2_acceptance.case_selection` | pass | it compares take **identity** (id + run), not the recorded starting-frame hash — see limitations |
| re-evaluate dependencies when scenes are reordered (126) | the same comparison names the *current* previous scene id, so a reorder re-reads the chain instead of keeping an old verdict | `test_m2_acceptance.case_reorder` (reason `previous scene b9be37 -> da1d20`), `test_fingerprint.reorder_moves_the_takes_component` | pass | reordering through the editor is a full-list save; a scene list save conflicts if the scene order moved underneath the editor (`test_revisions.reordered_scenes_conflict`) |
| flag prompts affected by changed global instructions, never rewrite them (127) | `project.instructions_of` (sha256 of bible + notes) is snapshotted per job; `instructions_changed` compares on read | `test_status.test_table` (bible/notes rows) | pass | only bible + notes are "instructions": `music_brief`, character notes and ref content are not in this flag |
| fingerprint assembled cuts (128) | `project.cut_plan` + `_plan_fingerprint` over `CUT_COMPONENTS` = ordered take ids/runs, transitions, cards, music bed hash, export settings; stored beside the entry by `assemble`; `output_status` derives `fingerprint`/`outdated`/`changes` on read | `test_fingerprint`: `each_component_change_moves_the_fingerprint`, `irrelevant_changes_keep_the_fingerprint`, `assemble_stores_the_fingerprint_with_the_entry`, `music_change_flags_the_cut_nothing_else_does`, `view_never_edits_stored_entries`; `test_m2_acceptance.case_music` | pass | cuts assembled before `9f7ddfe` have no plan and answer `fingerprint: unknown_legacy`, `changes: []`; `outputs` holds one entry per kind |
| preserve old takes and outputs when they go outdated (129) | nothing in the status/view path deletes anything; prune refuses record paths; `assemble` writes a new stamped file and only then replaces the entry | `test_status.outdated_flags_delete_and_requeue_nothing`, `test_fingerprint.reassembly_never_touches_the_old_entry_or_file`, `test_provenance.prune_never_offers_record_paths_as_candidates`; `test_m2_acceptance.history_intact` (every baseline take, snapshot, result and cut file still on disk and served after every case) | pass | a superseded cut stays on disk but is no longer listed anywhere — only `outputs[kind]` survives in `project.json` |
| directly outdated vs downstream-of-outdated (131) | `outdated` = flags of the scene's *own* selected take; `_downstream_scenes` returns the forward `continue` chain as `downstream_of` with `via`/`reason`/`text`, stored nowhere | `test_status.test_table` asserts the `downstream_of` mirror for every row; `test_m2_acceptance` case 1 (`Two` outdated, `Three` only in `Two.downstream_of`) and case 4 (chains re-derived after the move) | pass | the chain stops at the first `cut` scene, which is exactly the intended scope |
| acceptance: five changes (135–139) | one 4-scene film driven through the real render job and the real endpoints | `test_m2_acceptance.test_acceptance_table` | pass | mocked ComfyUI + mocked assemble encode |
| acceptance: export selection does not move when another take finishes (140) | `assemble` freezes `used_takes`/`cut_plan` before ffmpeg; `output_status` recomputes what *would* change; stored entry is never touched | `test_selection.a_take_that_lands_mid_assemble_is_not_in_the_cut`, `test_fingerprint.new_take_under_auto_selection_moves_the_takes_component`, `take_finishing_for_an_explicitly_selected_scene_keeps_the_cut_current`; `test_m2_acceptance.test_export_selection_holds_while_other_takes_finish` | pass | "unchanged" is proven for the stored record; re-assembling is still the user's decision |
| acceptance: a take can be inspected (141) | `GET /api/p/<slug>/take?id=<take id or run>` → `web.take_view` | `test_provenance.take_endpoint_returns_snapshot_and_result`, `take_url_is_routed_through_the_api`; `test_m2_acceptance.test_a_take_shows_the_inputs_that_actually_ran` compares the snapshot with the exact `POST /prompt` body, the uploaded picture bytes (in upload order) and the frame the continuation actually read | pass | an unknown id answers 404; a legacy take answers without a snapshot |

## The acceptance scenario

`tests/test_m2_acceptance.py` builds one film four times over (fresh baseline per case): scenes
*One* (cut) → *Two* (continue) → *Three* (continue) → *Four* (cut), two reference pictures, a music
bed, four takes rendered through the real `web.render_job` against a stub ComfyUI (so scene 2 and 3
really read their previous take's last frame with the real ffmpeg out of a real `video.artifacts/rgb.npy`),
scene 1 pinned by `POST /api/p/<slug>/star`, and one assembled film cut stored with its fingerprint.
Observed, per case:

- **upstream selection moved** — `Two: outdated=['continuity_outdated']`, reason
  `source take 9ada2a404dd1ec80 -> 70466425c9ed1c9a`; `Three: outdated=[]` while it appears in
  `Two.downstream_of`; `Four: continuity_outdated=not_applicable`; the cut says `changes=['takes']`,
  and pinning the first take back clears both to `changes=[]`.
- **prompt edited** (scene 4) — `Four: outdated=['prompt_changed']`, reason `prompt text`; the other
  three scenes clean; the cut stays `outdated=False, changes=[]` (prompts are not a cut component).
- **reference replaced** (`refs/picture-1.png` rewritten on disk) — `refs_changed=changed` exactly on
  *One* and *Four*, the two scenes whose takes submitted pictures; *Two*/*Three* stay `same` because
  their takes started from a frame; reason `picture-1.png content`; the previous bytes are still
  readable from `refs-store/<sha256>.png`.
- **scenes reordered** to One, Four, Two, Three — `Two: continuity_outdated=changed`, reason
  `previous scene b9be37 -> da1d20`; `Three` clean; `One.downstream_of` becomes empty and
  `Four.downstream_of` becomes `[Two, Three]`; the cut says `changes=['takes']`.
- **music replaced** — no scene flagged at all (`outdated=[]` ×4), the cut says
  `outdated=True, changes=['music']`; after re-assembling the new entry is clean and its `used_takes`
  are byte-identical to the baseline, while the older `film-<stamp>.mp4` is still on disk and still
  served.
- every case ends with `history_intact` (all baseline takes, their snapshots, results, mp4 files and
  cut files present and served, superseded takes still answered by the take endpoint) and with a
  `GET /api/p/<slug>` whose stored `project.json` is byte-identical before and after and contains none
  of `status/flags/outdated/changes/unknown_legacy/downstream_of/chosen/selected` at any depth, while
  the HTTP reply does carry them.

The test's teeth were checked with throwaway mutation probes (not committed): breaking
`_downstream_scenes` → 5 failures, making `output_status` always answer `changes=[]` → 4, forcing
`continuity_outdated` to `same` → 6, making `store_ref` hash-only → 7.

## MOCKED checks (ran here)

- ComfyUI: `tests/test_provenance.py` `StubComfy`, extended by `Runs` in `test_m2_acceptance.py`
  (a distinct output directory per submit, and a capture of every `/prompt` body and every uploaded
  picture so a snapshot can be compared with what the backend actually received). Submit/history
  shapes are modelled from the documented API, not observed from a running ComfyUI. The stub reports
  the same take filename (`take.mp4`) for every run.
- The assemble encode: `test_fingerprint.FakeFfmpeg` answers the duration probe with 6.0 s and the
  encode with exit 0 while writing nothing, so the cut mp4s this test asserts on are placeholder
  bytes it writes itself. Cut *content* was never encoded here.
- Take media: `take.mp4` is 3 bytes; the raw frames are a real 3-frame `rgb.npy` written by
  `test_provenance.write_npy` at 128×96.
- Real ffmpeg (not faked) runs in this scenario: extracting each continuation's last frame
  (`project.last_frame`) from the npy, and the png that is then uploaded and content-addressed.
- The HTTP surface is real: the module serves `web.Handler` on a spare port and drives
  `/api/p/<slug>/star`, `/save`, `/music`, `/render`, `GET /api/p/<slug>`,
  `GET /api/p/<slug>/take?id=` and `/files/<slug>/<cut>`.

## Live wire check (real project, no fixtures)

`python3 web.py --port 8199` in this worktree, against the untouched `projects/lasso-test`
(pre-M2 project: no take ids, no `selection`, cut entries with no plan):

- `GET /api/p/lasso-test` → each scene carries `status`/`chosen`/`job`/`selection`/`selected_take`;
  every flag reads `unknown_legacy`, `outdated=[]`, `downstream_of=['The Wriggle Free'] on scene 1;
  both cuts answer `fingerprint='unknown_legacy'`, `outdated=False`, `changes=[]`.
- `GET /api/p/lasso-test/take?id=<run dir>` → `{"provenance": "legacy", ...}` pointing at the real
  ComfyUI output path; an unknown id → 404.
- `project.json` md5 identical before and after those three GETs, and `grep -c` for any derived key
  in the stored file → 0.

## REAL-BACKEND checks (not run here; run against ComfyUI on port 8190)

- A real Draft render end to end: that the recorded `LoadImage` names really resolve in ComfyUI's
  input folder, and that real `/history/<prompt_id>` output entries look like what
  `web.render_result` expects (gifs list, `subfolder`, `type: output`).
- Real continuation first frames: last frames read out of a real 1344×768 `rgb.npy` (the acceptance
  film renders at 128×96), and the fallback to `video.mp4` once FreeVideo has freed the raw frames.
- A real full-film assemble: ffmpeg actually encoding clips, transitions, title/end cards and the
  ducked music bed; the fingerprint rows here only prove the *plan*, not the media.
- Enhance/finishing against SeedVR2 with `decode_tiled=True` and `batch_size=4n+1` (the recorded
  `finishing` settings come from `enhance.DEFAULTS`).
- `eval.py` against a live director model: the pass count in `eval/*/results.json` must not drop
  (no prompt was changed in this pass).

## Migration notes

- `project.json` gains, on write only: `rev`/`key_revs`; per scene `selected_take` and `selection`
  (`star` folds into them on the first write, `migrate_selections`); per take `id`, `job`,
  `provenance`, `source_take`; per output entry `fingerprint` and `cut_plan`. Old files load
  unchanged, and reading one rewrites nothing.
- New directories per project: `jobs/<job id>.json` + `jobs/<job id>.result.json` (write-once
  records) and `refs-store/<sha256><ext>` (content-addressed inputs). Both are excluded from pruning.
- `GET /api/p/<slug>` now answers extra per-scene keys (`job`, `chosen`, `selected_take`,
  `selection`, `status`) and extra per-output keys (`fingerprint`, `outdated`, `changes`); they are
  derived per request and are never stored. An older `project.html` ignores unknown keys.
- New endpoint `GET /api/p/<slug>/take?id=` (accepts a take id, or a run directory for a legacy
  take). `POST /api/p/<slug>/star` takes `{'scene', 'take'}` or `{'scene', 'auto': true}`.
- Cuts assembled before the fingerprint commit answer `fingerprint: unknown_legacy` and never claim
  to be up to date; re-assembling records them properly.
- `refs-store/` and `jobs/` are new disk growth per film; nothing cleans them yet.

## Known limitations

- `continuity_outdated` compares **take identity** (id + run), not the recorded starting-frame hash.
  Re-rendering the same upstream take reproducibly (same seed, same inputs) still flags its
  continuations, because a new take means a new run directory. The frame hash is recorded
  (`source_take.frame_sha256`, `first.sha256`) but not compared.
- `outputs` keeps only the newest entry per kind. Superseded cut files remain on disk and remain
  served by `/files/<slug>/<name>`, but nothing lists them; `history_intact` has to be given the
  names.
- Replacing a reference picture has no dedicated endpoint: the acceptance case swaps the bytes in
  `refs/` the way the upload path does (`overwrite=true`).
- `refs_changed` walks the **snapshot's** ref list and re-hashes `refs/<name>`: a changed file reads
  `<name> content`, a deleted one `<name> missing`, and a *newly added* picture flags nothing unless
  the ref count differs and the take had no starting frame (then `count 2 -> 3`). Ordering is
  recorded (`refs[].index`) but never compared, so re-ordering two existing pictures does not flag
  the take.
- Nothing is garbage-collected: a film's `jobs/`, `refs-store/` and cut files grow forever; `prune`
  deliberately refuses record paths and ComfyUI's own output folder is outside `projects/`.
- The `changes` a cut reports are component names (`takes`, `transitions`, `cards`, `music`,
  `export`); which scene inside that component moved is only visible in the scene rows.
- No page changed in this step, so the geckodriver suites were not re-run; the badge/UI behaviour
  they cover was verified in the commit that added it (`86e60b0`, `tests/browser_status.py`).
