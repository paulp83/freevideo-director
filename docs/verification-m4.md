# M4 Step 4 — Durable jobs and restart acceptance

Run 2026-10-21 from branch tip `d23b57e`. Everything below was executed, not reasoned:
`python3 -m py_compile *.py tests/*.py` OK; `python3 -m unittest discover tests` 187 tests OK
(10 skipped: the slow restart suite and its sub-tests, gated on `DIRECTOR_SLOW_SUITES=1`);
`DIRECTOR_SLOW_SUITES=1 python3 -m unittest tests.test_restart_acceptance` **10/10 OK in 13.6 s**
(five consecutive green runs; requirement < 90 s); `node tests/autosave_check.js` 10 checks;
`python3 tests/browser_queue.py` 61/61. No page changed this step, so `browser_autosave.py`
was not required.

Every restart stage runs against **real OS processes**: `web.py` is started as a real
`python3 web.py --port N` subprocess against a throwaway projects root, `jobs.db`, stats file
and ComfyUI root (via the new env hooks), SIGKILLed at the stage boundary, and restarted on the
same root. `tests/stub_comfy.py` is a second real process speaking the documented ComfyUI HTTP
API — it echoes client-supplied prompt ids like the real thing, can hold/accept/fail/complete a
prompt on command over a `/_ctl/...` channel, and reports every `/prompt` body it ever received.

## Matrix: every M4 design bullet

| requirement (design §6) | implemented behaviour | test performed | result | corrective notes |
| --- | --- | --- | --- | --- |
| persist identity, request snapshot, state, timestamps, backend address, prompt IDs, result references, errors | `jobs_db.py` table + `COLUMNS` (`take_id`, `workdir`, `holds`, `prompt_ids`, `resolved`, `retry_allowed`, …); request snapshot lives in the project (`write_job`/`read_job`), DB keeps `state` incl. `submitting` | `test_jobs_db.py` column round-trips; acceptance stages read `jobs.db` directly with `sqlite3` (rows survive SIGKILL) | pass | — |
| startup 1: reconcile known prompt ids against queue and history | `recover_jobs` → `_recovery_pass` reads `/queue` + `/history/<pid>` per row before deciding anything | S3–S5, S7–S10 (verdicts match stub queue/history); `test_recovery.py` | pass | — |
| startup 2: reattach to pending or running jobs | `_reattach` resumes polling the existing prompt; no build, no submit | S3 (killed inside the acceptance window) and S4 (killed while running): stub prompt count unchanged, take recorded once | pass | — |
| startup 3: record completed outputs idempotently | `_settle` → `finish_recorded` replays take/final recorders + result file; `take_recorder` matches on take id *or* job id | S5 (completed while director was dead) and S6 (killed between the take write and the finish write): exactly one take in `project.json`, job `finished` | pass | — |
| startup 4: record confirmed failures | `_chain_verdict` `failed` branch: fails the job with ComfyUI's own `exception_message`, releases holds | S7: restart reports `CUDA out of memory` verbatim, scene free | pass | found gap: the kept enhance temp dir survived the verdict — now removed by the verdict itself (commit `6cd9a6b`) |
| startup 5: mark unresolved jobs unknown | `_lost_message`/`_chain_verdict` unknown branches with `retry_allowed` only when honest | S2 (`the submit was not confirmed`), S9 (`ComfyUI has no record of this job`) | pass | found gap: an unknown *never-submitted* job kept holding the scene forever — `_settle` now releases + persists the free (commit `d64d370`) |
| crash window between backend acceptance and local persistence; correlation id when supported | `submit_tracked` writes the row + client-supplied prompt id BEFORE the POST (`record_submit`); ComfyUI echoes that id back, so acceptance is provable either way | S2 (row with id, stub held before accepting → unknown, Retry offers honestly), S3 (stub accepted, answer never arrived → reattached by the client id) | pass | — |
| never auto-resubmit unknown jobs | nothing in recovery POSTs `/prompt`; Retry is user-initiated only | every stage asserts stub prompt count after restart == count before (S2's extra POST is the explicit user Retry, and it replays the recorded graph byte-identically) | pass | — |
| missing history is not proof of failed execution | a prompt in neither queue nor history lands `unknown`, never `failed`; message names both possibilities and refuses auto-resubmit | S2, S9 (`ComfyUI down at restart` → `recovering`, row untouched, fresh backend resolves via the 1.2 s reconcile loop, second restart idempotent); `test_recovery.py` vanished-prompt tests | pass | — |
| acceptance: restart at several stages; completed work recovered, results not appended twice, no automatic duplication | `tests/test_restart_acceptance.py` S1–S10, real SIGKILLs, fresh temp root per stage; global invariants after every stage (stub count invariant, take ids unique, no unexplained non-terminal `submitting` rows, queue view honest) | 10/10 OK, 13.6 s total | pass | S6's take→finish window is raced by tight polling (≤12 attempts); it caught the window on the first attempt in all five runs |

## MOCKED checks (ran here)

- Stub ComfyUI (`tests/stub_comfy.py`) as a real *separate process*: queue/history/`/view`/
  `/interrupt`/`/upload/image` are modelled from the documented API; completed prompts "produce"
  a 32-byte fake `video.mp4` plus a `video.artifacts/` marker dir (the director's mp4 fallback
  requires the marker). No GPU, no FreeVideo, no SeedVR2/RIFE ever ran.
- `web.py` itself, `jobs.db` (SQLite), project JSON writes, SIGKILL semantics and process
  restarts are all real — nothing is simulated in-process for this suite.
- Timings come from env overrides (`POLL_SECONDS=0.15`, `RECONCILE_SECONDS=1.2`); the 15 s
  production reconcile loop itself was not timed here, so queue error texts interpolate the
  configured interval — the tests assert message prefixes, never the literal "15s".

## REAL-BACKEND checks (not run; run against ComfyUI on 8190 before believing it)

- Kill the director during a **real render** on ComfyUI 8190 and confirm the reattach: same
  prompt kept, take recorded once, nothing resubmitted.
- Confirm the real `/history` entry shape for a **client-supplied prompt id** matches what
  `render_result`/`enhance.result` read (`status.completed`, `outputs.*.gifs`).
- Confirm the real ComfyUI really **echoes the client-supplied prompt_id** in the `POST /prompt`
  reply (the stub models it; the whole crash-window argument depends on it).
- Kill the director during a **real enhance** (SeedVR2 reading a live `enhance-tmp/*.mkv`) and
  confirm S7's workdir removal and FreeVideo's engine teardown behave.
- Websocket progress for reattached jobs: `ws_listen` only talks to `127.0.0.1:8190`; stage
  progress on a reattached job was never observed against a real render.
- Retry replays the *recorded* inputs; against a real backend that means old seeds/presets, not
  current scene edits — believed by design, not exercised with a real model.

## Migration notes

- New env knobs, all optional, defaults unchanged: `PROJECTS_ROOT`, `JOBS_DB`, `STATS`,
  `COMFY_ROOT`, `COMFY_URL`, `POLL_SECONDS`, `RECONCILE_SECONDS` (plus the existing
  `DIRECTOR_URL`/`DIRECTOR_MODEL`/`JOB_TIMEOUT`). Nothing about default startup changes.
- Behaviour change for recovery: an unknown job that provably never reached ComfyUI (row still
  without prompt ids) now **releases its scene** when the verdict lands. Such a scene is
  immediately actionable again; Retry is still offered and still replays exactly once.
- Failed or cancelled chain verdicts now delete the enhance job's temp dir at verdict time;
  the startup sweep of stale `enhance-tmp` dirs remains as the crash belt-and-braces.
- `jobs.db` and `project.json` formats are unchanged; old projects and existing databases load
  and behave as before.
