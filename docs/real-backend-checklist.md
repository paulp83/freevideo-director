# Real-backend checklist

One consolidated list of everything the update could **not** be verified against in the
development worktree — no ComfyUI, no GPU, no director model, no human eyes were available
there. It merges and de-duplicates the Real-backend sections of
`verification-m1-m2.md`, `verification-m2.md`, `verification-m3.md`, `verification-m4.md`,
`verification-m5.md`, `pilot.md` and `evaluation.md`. Run for real on 2026-10-21/22 against ComfyUI 0.34.0 and `qwen3.8-27b`: groups A to C, results recorded under each item. Group D needs a human.

Work through it in order. Within each group the order is by risk: data safety
(restart recovery, cancel targeting, temp dirs) before finishing quality before UX.
Mark each box `pass` or `fail`; a `fail` names the owning test suite in the *claimed by* line,
so the fix has somewhere to land. All commands run on the operator's machine against the
real backends — the update itself never starts ComfyUI or the model containers.

## A. ComfyUI only (port 8190; minutes, no generation batch)

**A1. Real `/history` entry shapes match what the reconciler reads.**
- Steps: with ComfyUI up and some old renders in its history, `curl -s http://127.0.0.1:8190/history | python3 -m json.tool | head -80`; compare against `web.job_verdict` / `render_result` / `enhance.result` expectations (`status.completed`, `status_str`, `outputs.*.gifs` with `subfolder`/`type: output`). Then open the queue drawer in the UI with any stale unknown job (if none, create one by stopping ComfyUI for a few seconds mid-poll) and press **Reconcile**.
- Expected: the verdicts (`completed` / failed / still running) match what the drawer says; a provably finished unknown job records its take exactly once; entry shapes are as modelled.
- Claimed by: `verification-m1-m2.md` (REAL-BACKEND), commits `5a7a3a9`, `ded7889`; stub-pinned in `tests/test_jobs.py`.
- Result: ☑ pass after two fixes (2026-10-22, ComfyUI 0.34.0, real captures pinned in `tests/fixtures/comfy_history/` and `comfy_object_info/`): a genuinely failed prompt was classified `interrupted` (the stubs put `status_str` at the top of the entry, real ComfyUI nests it in `status` and uses `error` for interrupts too) → `30d9752`; RIFE's model list uses the legacy `[[names], {default}]` combo shape so an installed `rife49.pth` read as unconfirmed → `8399977`.

**A2. ComfyUI echoes the client-supplied `prompt_id` in the `POST /prompt` reply.**
- Steps: submit any tiny graph ComfyUI accepts with an explicit id: `curl -s -X POST http://127.0.0.1:8190/prompt -H 'Content-Type: application/json' -d '{"prompt": <tiny graph>, "client_id": "x", "prompt_id": "0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f"}'`; then `curl -s http://127.0.0.1:8190/history/0f0f...`.
- Expected: the reply carries the same `prompt_id`; `/history/<id>` finds it. The whole crash-window/reconnect argument (client id recorded **before** the post) depends on this.
- Claimed by: `verification-m4.md` (REAL-BACKEND), commit `46de6a5`; stub-pinned in `tests/test_recovery.py`.
- Result: ☑ pass: the `POST /prompt` reply echoes the client-supplied `prompt_id` exactly, `/history/<id>` finds it, and a non-UUID is refused with `invalid_prompt_id`.

**A3. History timing stamps exist for the pilot.**
- Steps: render anything and `curl -s http://127.0.0.1:8190/history/<prompt_id>`; look for `execution_start` and `execution_success` in `messages`.
- Expected: both stamps present. If absent (older build), `pilot.py` leaves `backend_seconds` null and says so — settle this before spending a night on 12 pilot slots.
- Claimed by: `pilot.md` (Real-backend checklist item 1), commit `9fe5774`.
- Result: ☑ pass: `execution_start` and `execution_success` are present in the history messages; a real draft render shows 109.8 s of backend execution against about 111 s for the job.

**A4. Enhance temp-dir sweep removes dirs left by a crashed server.**
- Steps: `mkdir -p enhance-tmp/stale-dir && touch -d '2 days ago' enhance-tmp/stale-dir` (no live job owns it), then start the director and watch startup, or wait for the periodic sweep.
- Expected: the stale dir is gone (`enhance.sweep_workdirs` / `web.sweep_enhance_tmp`); dirs of live or unknown jobs are **not** touched.
- Claimed by: `verification-m1-m2.md` (REAL-BACKEND), commits `fd2e6e1`, `3bb7b13`; stub-pinned in `tests/test_enhance_tmp.py`.
- Result: ☑ pass (2026-10-22): a two-day-old orphan folder in `enhance-tmp` was removed at director startup and a fresh one was left alone; the live enhance temp folders were also seen removed after completion (B4, B5).

## B. ComfyUI + GPU time (real render / enhance / finishing / recovery)

**B1. Kill the director during a real render — the reattach.** *(highest risk: duplicate paid generations)*
- Steps: start a real render from the UI; mid-flight `kill` the `web.py` process; restart the director; open the queue.
- Expected: the job reattaches to the same running prompt (never resubmitted), completes, and the take is recorded **exactly once**; `/history` shows no second prompt for it.
- Claimed by: `verification-m4.md` (REAL-BACKEND), commits `1e5a945`, `46de6a5`; mocked suite `tests/test_restart_acceptance.py` (`DIRECTOR_SLOW_SUITES=1`).
- Result: ☑ pass: SIGKILL during a real render, restart: the same job reattached to the same prompt, finished, ComfyUI's history grew by one entry, exactly one take recorded.

**B2. `/interrupt` by prompt id stops only the named prompt.**
- Steps: queue two renders on two scenes; cancel the running one from the queue drawer.
- Expected: only that prompt stops (the queued neighbour still runs); the cancelled job ends `cancelled`, not `unknown`; an unconfirmed cancel ends `unknown` with the scene reserved.
- Claimed by: `verification-m1-m2.md`, `verification-m3.md` (REAL-BACKEND), commit `ded7889`; stub-pinned in `tests/test_jobs.py`.
- Result: ☑ pass after a fix: cancelling the running render stopped only that prompt (the queued neighbour started and kept running). The first attempt ended `unknown` for ~17 s because the confirmation looked twice at 0.2 s intervals; `/interrupt` now names the prompt and waits up to `cancel_confirm_seconds` → `5ed402d`.

**B3. `/queue delete` of a pending id really removes it.**
- Steps: with a queued (not running) job, press cancel before it starts.
- Expected: the pending entry is gone from `/queue`, the job lands `cancelled`, the scene frees immediately.
- Claimed by: `verification-m1-m2.md` (REAL-BACKEND), commit `ded7889`.
- Result: ☑ pass: cancelling a pending render removed exactly that prompt from `/queue` in 0.5 s, the job ended `cancelled`, and the scene was usable again at once.

**B4. Kill the director during a real enhance (SeedVR2 on a live `enhance-tmp/*.mkv`).**
- Steps: start Enhance on a real take; kill the director mid-run; restart; reconcile/resolve the now-unknown job.
- Expected: the kept workdir survives while unknown, is removed once the verdict lands (or `Dismiss`/reconcile resolves it); FreeVideo's engine teardown does not leave the GPU wedged.
- Claimed by: `verification-m4.md` (REAL-BACKEND), commits `3bb7b13`, `6cd9a6b`; stub-pinned in `tests/test_enhance_tmp.py`, `tests/test_recovery.py`.
- Result: ☑ pass: SIGKILL at 16% of a real SeedVR2 upscale: the temp input survived while the director was down, the job reattached, finished (upscale, smooth, encode), the temp folder was removed after completion, one final recorded.

**B5. Enhance end-to-end with SeedVR2 installed, upscale ON.**
- Steps: Enhance a real take from the workspace.
- Expected: raw `video.artifacts/rgb.npy` + `audio.wav` are read (and the `video.mp4` fallback works once FreeVideo frees the raw frames); the graph carries `decode_tiled=True` and `batch_size=4n+1`; the enhanced take lands in ComfyUI's output folder.
- Claimed by: `verification-m1-m2.md`, `verification-m2.md`, `verification-m3.md`, `verification-m5.md` (REAL-BACKEND), commits `cf4e617`, `35ba305`; graph shape pinned by `tests/test_enhance.py` + `tests/test_finishing_acceptance.py`.
- Result: ☑ pass: real SeedVR2 upscale on a real take, 152 s; H.264 1890×1080 (SeedVR2 scales the short side and keeps the take's aspect, so not 1920×1080; the page text was corrected → `916db70`), AAC audio, temp folder removed.

**B6. RIFE with `fps_policy=interpolated`.**
- Steps: set the project's finishing to interpolated fps, Enhance/finish a take.
- Expected: the RIFE nodes run and the output frame count doubles (60 fps from 30); `finishing_changed` behaves.
- Claimed by: `verification-m5.md` (item 2), commit `cf4e617`.
- Result: ☑ pass: RIFE ×2 turned 24 fps into exactly 48 fps (213 frames over 4.44 s).

**B7. Continuation first frames from real media.**
- Steps: render scene 1 for real, then *Continue from take* on scene 2.
- Expected: the starting frame is read from the real 1344×768 `rgb.npy` (falls back to `video.mp4` after the raw frames are freed), and `continuity_outdated` stays clear until the upstream selection changes.
- Claimed by: `verification-m2.md` (REAL-BACKEND), `verification-m1-m2.md`.
- Result: ☑ pass: the start frame of the continuation was extracted from the real take (1.2 MB, hash recorded, `i2va` mode, continuity flag `same`).

**B8. Real full-film assemble.**
- Steps: assemble a film of real takes with mixed source fps, transitions, cards and a music bed; also check a Draft render's `LoadImage` names resolve in ComfyUI's input folder.
- Expected: ffmpeg encodes clips/transitions/cards/ducked bed, normalises fps without audio drift; history output entries match `render_result` (gifs list, `subfolder`, `type: output`); the stored fingerprint matches.
- Claimed by: `verification-m2.md`, `verification-m1-m2.md`, `verification-m5.md` (item 3); pinned with real ffmpeg at 3-scene scale by `tests/test_finishing_acceptance.py` — cost scales with film length.
- Result: ☑ pass: a real film (title and end cards, crossfade and dip, a music bed, one 48 fps enhanced take and two 24 fps drafts) built in 9 s: H.264 1920×1080 yuv420p, 16.85 s video against 16.90 s audio. A contact sheet shows the cards, transitions and scenes as expected. The film export stretches 1890×1080 clips to 1920×1080 by about 1.6%.

**B9. `VHS_VideoCombine` output at a project fps other than 24.**
- Steps: finish at 30 or 60 fps and open the combined output.
- Expected: playable file at the requested fps; the golden captures only pin 24 fps.
- Claimed by: `verification-m5.md` (item 5), commit `cf4e617`.
- Result: ☑ pass at 48 fps (the project default); 30 and 60 fps were not run.

**B10. Retry replays the recorded inputs.**
- Steps: fail/cancel a render, edit the scene's prompt or seed, press **Retry**.
- Expected: ComfyUI receives the *old* recorded prompt/seed/presets from the snapshot, not the current scene edits (by design; believed, never exercised with a real model).
- Claimed by: `verification-m4.md` (REAL-BACKEND), commit `1e16305`; pinned against the stub in `tests/test_retry.py`.
- Result: ☑ pass after a fix: a submit dropped by a proxy went `unknown`; reconcile proved no trace; the user changed the seed; Retry sent ComfyUI the recorded seed, not the new one, and the proxy saw exactly one forwarded prompt. Before the fix a live unanswered submit never offered Retry (the row was no longer `submitting`) → `916db70`, jobs.db schema v4.

**B11. Websocket progress on a reattached job.**
- Steps: reattach per B1 and watch the drawer's stage chips and both time columns.
- Expected: stage progress keeps updating after the restart (`ws_listen` talks only to `127.0.0.1:8190`); it was never observed on a real reattached render.
- Claimed by: `verification-m4.md`, `verification-m3.md` (REAL-BACKEND).
- Result: ☑ pass after a fix: reattached jobs showed no progress and the sampler counter hit 50/50 forty seconds into a 110 s render. The render stage now follows FreeVideo's overall fraction with its phase label; after a SIGKILL and restart it continued 36% → 100% → `5ed402d`.

## C. Director model (8144, fallback 8078; no GPU render)

**C1. First live schema-2 eval run.**
- Steps (from `evaluation.md`):
  ```sh
  curl -s http://127.0.0.1:8144/v1/models        # read the model id; 8078 is the fallback
  python3 rescore.py
  python3 eval.py --portable --label qwen38-27b-portable-v2 \
      --url http://127.0.0.1:8144/v1/chat/completions --model <id> --runs 2
  python3 eval_compare.py qwen36-35b qwen38-27b-portable-v2
  ```
- Expected: a schema-2 `eval/qwen38-27b-portable-v2/results.json` exists; `eval_compare` passes its validator/case-set gate; failed calls appear as rows and count against the total; the other four old case-runs print `NO-ROW`.
- Claimed by: `evaluation.md` (Real-backend checklist), commits `54d5211`, `f43c999`, `5b097d6`, `48942bd`; harness pinned in-process by `tests/test_eval.py`.
- Result: ☑ pass: `eval.py --portable --runs 2` on `qwen3.8-27b`: 12/12 case-runs, 262/262 checks, 0 failed calls, 0 skipped; `eval_compare` passed its validator and case-set gate and printed `NO-ROW` for the cases the portable set does not include.

**C2. *Write prompt* / storyboard against the live model; pass count must not drop.**
- Steps: with the model up, use *Write prompt* in a project; re-run `eval.py --portable` and `eval_compare.py old new` after any skill/`h3check.py` change.
- Expected: prompts land; the pass count over the same case set does not drop (`h3check` version/hash and case set must match or the comparison refuses).
- Claimed by: `verification-m1-m2.md`, `verification-m2.md`, `verification-m3.md` (REAL-BACKEND), commits `67d74c0`, `15d6d4a`.
- Result: ☑ pass: real storyboards with two and one reference pictures (4 and 3 scenes) and 8 *Write prompt* calls all validated on the first try under the strict validator, 5 to 22 s each.

**C3. `director.py` retry loop against a real model's real mistakes.**
- Steps: watch a *Write prompt* call where a check fails.
- Expected: the failing reply is retried with the check feedback, and the retry count/reasons surface; against stubs the loop is pinned (`tests/test_eval.py`, `67d74c0`) but the model's real failure modes are unobserved.
- Claimed by: `evaluation.md`, `verification-m*` mocked-vs-real sections.
- Result: ◐ partly: no real model mistake occurred in 20 live prompt writes, so the retry loop has not been seen handling one on a real model.

## D. Human checks

**D1. Run the 12-slot blinded pilot and review it.**
- Steps: follow `pilot.md` exactly (A3 first): `python3 pilot.py run` for 3 scenes × 2 seeds × 2 arms, render without interruption if you can, review at `http://127.0.0.1:8393` (blind clip names, `key.json` never served), then `python3 pilot.py report`.
- Expected: an `unknown` slot exits 3 — resolve it in the queue UI, then `--retry-slot` (nothing resumes automatically); takes land in ComfyUI's output folder, the manifest only stores paths; set `PILOT_PORT`/`PILOT_PORTRAIT_IMAGE` only if needed; keep one machine and one model for all 12 slots — the comparison only means anything under fixed conditions. Acceptance rate and per-accepted-take time come out with **backend time kept separate from wall time**; arm B also reports prompt seconds separately.
- Claimed by: `pilot.md` (all five Real-backend items), commit `9fe5774`; pinned by `tests/test_pilot.py`, `tests/browser_pilot_review.py`.
- Result: ☐ pass ☐ fail — notes:

**D2. Listening check on the finished film.**
- Steps: assemble a real film with a music bed and watch it with real audio.
- Expected: no content-level audio glitches. The numeric A/V check (40 ms beep tolerance, measured 8 ms) covers sync only.
- Claimed by: `verification-m5.md` (item 4).
- Result: ☐ pass ☐ fail — notes:

**D3. Visual check of the UI at real display sizes, including a real phone.**
- Steps: open the workspace at normal desktop sizes and on an actual phone/tablet (390×844 class); use the queue drawer, finishing switches and readiness chips for a while.
- Expected: layout is comfortable and controls feel right in the hand. The geckodriver walks (`browser_layout.py`, `browser_acceptance_m3.py`) prove geometry, pixel sizes and tap-target dimensions, not touch feel or aesthetics.
- Claimed by: `verification-m3.md` (REAL-BACKEND, last item).
- Result: ☐ pass ☐ fail — notes:
