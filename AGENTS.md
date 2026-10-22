# Agent guide

FreeVideo Director: idea + pictures → MiniMax H3 prompt → render on a local FreeVideo ComfyUI.
[README.md](README.md) has the file map and [docs/](docs/) the user documentation; this file holds
what the code does not say.

## Conventions

- **Standard library only.** Every module imports from the Python standard library; talk to
  ComfyUI and the director model over `urllib` and raw sockets. Shell out to `ffmpeg` for media work.
- **One module per concern**, flat in the repo root: `config.py` (every service address, path and
  default), `director.py` (prompt + CLI), `web.py` (HTTP server + jobs), `project.py` (films),
  `jobs_db.py` (durable sqlite job store behind the queue and the restart recovery pass),
  `enhance.py` (finishing graph), `finishing.py` (finishing stages and the one frame-rate policy),
  `h3check.py` (format checks), `readiness.py` (the three health checks behind the header chips and
  the 503 gates on POST actions). `web.py` is the top of the import graph: it imports `director`,
  `project`, `jobs_db`, `finishing`, `enhance` and the rest; only `config.py` sits below
  everything and imports nothing from the project.
- **Pages are single files.** `index.html`, `project.html`, `queue.html` carry their own CSS and JS
  and talk to `web.py` through `/api/...`.
- **Bind `127.0.0.1`.** New servers and endpoints stay local unless the user passes `--host`.
- **Settings come from `config.py`.** Never add a raw `os.environ` read or a hard-coded port: add
  a setting there (docs/configuration.md has the table). Precedence flag > env > `config.json` >
  default; `GET /api/config` shows the effective values and their sources.

## Verify

Check a change by running it:

1. `python3 -m py_compile *.py`
2. `python3 -m unittest discover tests` — the suite lives in `tests/`, runs from the repo root, and keeps
   to a temp projects dir, so it never touches `projects/`. `test_restart_acceptance` skips unless
   `DIRECTOR_SLOW_SUITES=1`. `tests/test_zz_no_leaks.py` runs last and fails the whole suite when a
   test-spawned server (`web.py`, `stub_comfy.py`, geckodriver, headless firefox) outlives the run:
   start children with `tests/leak_guard.py` (`spawn` + `stop`), which sweeps them at exit and
   starts a detached `tests/leak_watchdog.py` that sweeps them even if the runner is SIGKILLed.
3. The headless-Firefox suites (`tests/browser_*.py`) and `node tests/autosave_check.js` are not under
   discover — run each separately, one at a time; they need geckodriver + firefox on PATH and bind
   fixed ports, so leftovers from one run break the next.
4. Start the UI on a spare port (`python3 web.py --port 8199`) and `curl` the pages and `/api/...`
   endpoints you touched. The UI runs without a director model or ComfyUI; only *Write prompt* and
   *Render* need them.
5. For prompt changes, run `eval.py --portable` against a live director model and compare labels with
   `eval_compare.py old new` — it refuses runs whose `h3check` version/hash or case set differs
   (`rescore.py` re-scores old runs first). Pass counts are only comparable over the same denominator:
   failed calls and skipped fixtures are rows and count against the total.

## Gotchas

- **H3 format comes from `skill/`.** The director's system prompt is the pinned MiniMax skill;
  `h3check.py` encodes its field names and rules, and `director.py` retries when a check fails. Change
  the skill and `h3check.py` together.
- **Enhance reads raw frames.** `enhance.py` works from `video.artifacts/rgb.npy` + `audio.wav`,
  falling back to `video.mp4` once FreeVideo has freed the raw frames. Keep that fallback.
- **SeedVR2 limits.** `decode_tiled` stays `True` (untiled decode runs out of memory at 1080p on 32 GB);
  `batch_size` is `4n+1`.
- **Takes live in ComfyUI's output folder**, not in `projects/`. A project stores references to them.
- **Ignored on purpose:** `out/`, `library/`, `stats.json`, `jobs.db*`, `config.json`,
  `projects/*/*.mp4`, `projects/*/frames/`, `eval/fixtures/generated/`, `eval/fixtures/*.jpg`.
  Generated media, eval fixture images and local settings stay out of git.
- **Ports:** UI `8192`, ComfyUI `8190`, director models `8144` then `8078` — the defaults in
  `config.py`, overridable by flag / env (`DIRECTOR_URL` / `DIRECTOR_MODEL`) / `config.json`.
- **Unknown jobs hold their reservations.** An unconfirmed job keeps its scene reservation until
  Reconcile or Dismiss resolves it, and is never resubmitted automatically. Every submit goes
  through `web.submit_tracked` with a client-generated prompt id, so a restart reattaches to the
  existing ComfyUI prompt instead of sending a second one.
- **`project.json` is written only through `projects.mutate`** (it bumps `rev` and `key_revs` and
  replaces atomically); delta saves are only valid against the `rev` they read. Derived state —
  outdated/finishing_changed/downstream flags — is computed on read and never persisted.
- **Job snapshots under `projects/*/jobs/` are immutable**; the verdict lands in a sibling
  `<job id>.result.json`. That immutability is what makes replay after a restart safe.
- **The finishing stages and the frame-rate policy live in `finishing.py` alone** — `web.py`,
  `enhance.py` and the page read them from there; never hard-code fps, CRF or stage lists elsewhere.
- **Both sides gate on `readiness`.** The page disables an action and the server route replies 503
  with the same sentence — add a gated action on both sides or neither.
- **The autosave core is lifted out of `project.html`.** `tests/autosave_check.js` extracts the
  block between the `==AUTOSAVE-CORE-BEGIN/END==` comment markers and runs it under node: keep that
  block DOM-free and the markers byte-exact.

## Git

Commit messages are one lowercase imperative line that names the behaviour, e.g.
`continuity: take the last frame from video.mp4 when the raw frames are gone`.
