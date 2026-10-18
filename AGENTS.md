# Agent guide

FreeVideo Director: idea + pictures → MiniMax H3 prompt → render on a local FreeVideo ComfyUI.
[README.md](README.md) has the file map and [docs/](docs/) the user documentation; this file holds
what the code does not say.

## Conventions

- **Standard library only.** Every module imports from the Python standard library; talk to
  ComfyUI and the director model over `urllib` and raw sockets. Shell out to `ffmpeg` for media work.
- **One module per concern**, flat in the repo root: `director.py` (prompt + CLI), `web.py` (HTTP
  server + jobs), `project.py` (films), `enhance.py` (finishing graph), `h3check.py` (format checks).
  `web.py` is the top of the import graph: it imports `director`, `project`, `enhance` and the rest, and
  nothing imports `web`.
- **Pages are single files.** `index.html`, `project.html`, `queue.html` carry their own CSS and JS
  and talk to `web.py` through `/api/...`.
- **Bind `127.0.0.1`.** New servers and endpoints stay local unless the user passes `--host`.

## Verify

There is no test suite. Check a change by running it:

1. `python3 -m py_compile *.py`
2. Start the UI on a spare port (`python3 web.py --port 8199`) and `curl` the pages and `/api/...`
   endpoints you touched. The UI runs without a director model or ComfyUI; only *Write prompt* and
   *Render* need them.
3. For prompt changes, run `eval.py` against a live director model and compare with `eval/*/results.json`;
   the pass count (`passed` / `total`) must not drop.

## Gotchas

- **H3 format comes from `skill/`.** The director's system prompt is the pinned MiniMax skill;
  `h3check.py` encodes its field names and rules, and `director.py` retries when a check fails. Change
  the skill and `h3check.py` together.
- **Enhance reads raw frames.** `enhance.py` works from `video.artifacts/rgb.npy` + `audio.wav`,
  falling back to `video.mp4` once FreeVideo has freed the raw frames. Keep that fallback.
- **SeedVR2 limits.** `decode_tiled` stays `True` (untiled decode runs out of memory at 1080p on 32 GB);
  `batch_size` is `4n+1`.
- **Takes live in ComfyUI's output folder**, not in `projects/`. A project stores references to them.
- **Ignored on purpose:** `out/`, `library/`, `stats.json`, `projects/*/*.mp4`, `projects/*/frames/`.
  Generated media stays out of git.
- **Ports:** UI `8192`, ComfyUI `8190`, director models `8144` then `8078`. `DIRECTOR_URL` /
  `DIRECTOR_MODEL` override discovery.

## Git

Commit messages are one lowercase imperative line that names the behaviour, e.g.
`continuity: take the last frame from video.mp4 when the raw frames are gone`.
