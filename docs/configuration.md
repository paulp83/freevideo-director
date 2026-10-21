# Configuration

Every service address, path and default lives in one place: **`config.py`**. The other modules ask
it for values instead of reading `os.environ` or hard-coding ports. No module imports another one
just to reach a setting; `config.py` imports only the standard library.

## Precedence

For every setting, highest wins:

1. **CLI flag** (on the entry point that advertises it)
2. **Environment variable** (the legacy names keep their exact meaning)
3. **`config.json`** — optional, flat JSON, next to the modules (the repo root). It is gitignored;
   copy the keys you want to change from the table below. Unknown keys print a warning naming the
   key and are ignored; a malformed file aborts startup with an error naming the file. The file
   location itself can be moved with `CONFIG_FILE`.
4. **Built-in default** (the AGENTS.md values: UI `8192`, ComfyUI `8190`, director models `8144`
   then `8078`)

Invalid values (a non-numeric port, a URL without a host, a "directory" setting that points at a
file) abort startup immediately, naming the setting and where the bad value came from.

## Settings

| Setting | Default | Env var | CLI flag | Used by (file:function) |
|---|---|---|---|---|
| `ui_host` | `127.0.0.1` | `UI_HOST` | `--host` | `web.py:__main__` (HTTP bind) |
| `ui_port` | `8192` | `UI_PORT` | `--port` | `web.py:__main__` |
| `ui_allow_hosts` | *(empty)* | `UI_ALLOW_HOSTS` (comma list) | `--allow-host` (repeatable) | `web.py:__main__` → `Handler.extra_hosts` |
| `comfy_url` | `http://127.0.0.1:8190` | `COMFY_URL` | `--comfy-url` | `enhance.py:COMFY/_post/_get/result`; `web.py:comfy/render_result/_post_prompt` |
| `comfy_root` | `<repo parent>/ComfyUI` | `COMFY_ROOT` | `--comfy-root` | `enhance.py:workdir_parent/result` (input + output dirs); `web.py:COMFY_OUTPUT`; `project.py:prune_plan/prune_takes` (via `enhance.COMFY_ROOT`) |
| *(websocket)* | derived from `comfy_url` | via `COMFY_URL` | via `--comfy-url` | `web.py:ws_target/ws_listen` — the progress websocket now follows a non-default ComfyUI instead of hard-coding `127.0.0.1:8190` |
| `comfy_client_id` | `freevideo-director` | `COMFY_CLIENT_ID` | — | `web.py:CLIENT_ID` (`_post_prompt`, `ws_listen`); `enhance.py:submit` |
| `director_backends` | `[[http://127.0.0.1:8144/v1/chat/completions, qwen3.8-27b], [http://127.0.0.1:8078/v1/chat/completions, qwen3.6-35b-a3b]]` | — | — (config file only) | `director.py:backend` discovery order |
| `director_url` | *(unset → search)* | `DIRECTOR_URL` | `--director-url` | `director.py:backend` |
| `director_model` | *(unset; `default` when `director_url` is set)* | `DIRECTOR_MODEL` | `--director-model` | `director.py:backend` |
| `projects_root` | `<repo>/projects` | `PROJECTS_ROOT` | `--projects-root` | `project.py:ROOT` (`path/listing/create/recover_interrupted`) |
| `library_root` | `<repo>/library` | `LIBRARY_ROOT` | `--library-root` | `library.py:ROOT` |
| `out_dir` | `<repo>/out` | `OUT_DIR` | `--out-dir` | `director.py:save` |
| `stats_file` | `<repo>/stats.json` | `STATS` | `--stats` | `stats.py:FILE` |
| `jobs_db_file` | `<repo>/jobs.db` | `JOBS_DB` | `--jobs-db` | `jobs_db.py:FILE/init` |
| `notify_url` | *(empty)* | `DIRECTOR_NOTIFY_URL` | `--notify-url` | `notify.py:send` — **secret**: masked in `/api/config` and never in the startup line |
| `ffmpeg` | `ffmpeg` | `FFMPEG` | — | `director.py:image_part`; `enhance.py:intermediate`; `project.py:last_frame/make_card/assemble`; `web.py:poster_view` |
| `ffprobe` | `ffprobe` | `FFPROBE` | — | `project.py:_duration/_has_audio` |
| `job_timeout` | `14400` s (4 h) | `JOB_TIMEOUT` | `--job-timeout` | `web.py:JOB_TIMEOUT` (poll deadline) |
| `poll_seconds` | `1.5` | `POLL_SECONDS` | `--poll-seconds` | `web.py:POLL_SECONDS` |
| `reconcile_seconds` | `15` | `RECONCILE_SECONDS` | `--reconcile-seconds` | `web.py:RECONCILE_SECONDS` (`reconcile_loop`) |
| `enhance_fps` | `24` | `ENHANCE_FPS` | — | `enhance.py:FPS` (raw-frame rate, graph `frame_rate`); `finishing.py:render_fps` — the rate the film policy multiplies from |
| `enhance_resolution` | `1080` | `ENHANCE_RESOLUTION` | `--resolution` | `finishing.py:defaults` (SeedVR2 short side for projects that say nothing) |
| `enhance_multiplier` | `2` | `ENHANCE_FPS_MULTIPLIER` | `--fps-multiplier` | `finishing.py:defaults` (RIFE factor for projects that say nothing) |
| `enhance_crf` | `16` | `ENHANCE_CRF` | `--crf` | `finishing.py:defaults` (encode quality for projects that say nothing) |
| `enhance_batch_size` | `21` | `ENHANCE_BATCH_SIZE` | `--batch-size` | `enhance.py:DEFAULTS/graph` (SeedVR2 frames per batch; keep `4n+1`) |
| `enhance_upscaler` | `seedvr2_ema_7b_fp8_e4m3fn_mixed_block35_fp16.safetensors` | `ENHANCE_UPSCALER` | — | `enhance.py:graph` (SeedVR2 DiT checkpoint) |
| `enhance_temporal_overlap` | `3` | `ENHANCE_TEMPORAL_OVERLAP` | — | `enhance.py:graph` |
| `enhance_rife` | `rife49.pth` | `ENHANCE_RIFE` | — | `enhance.py:graph` (RIFE checkpoint) |
| `preview_export_fps` | `24` | `PREVIEW_EXPORT_FPS` | — | `project.py:cut_plan/assemble` (`kind='animatic'`) |
| `preview_export_crf` | `20` | `PREVIEW_EXPORT_CRF` | — | `project.py:cut_plan/assemble` (`kind='animatic'`) |
| `xfade_seconds` | `0.5` | `XFADE_SECONDS` | — | `project.py:XFADE` (transition cuts) |
| `dip_seconds` | `0.4` | `DIP_SECONDS` | — | `project.py:DIP` (dip-to-black cuts) |
| `presets` | `draft` (two-pass, no finishing), `final` (single-pass, finishing) | — | — (config file only) | `web.py:PRESETS` |
| `readiness_cache` | `10` s | `READINESS_CACHE` | — | `readiness.py:check` (how long a readiness verdict is reused; must be > 0) |
| `readiness_timeout` | `2` s | `READINESS_TIMEOUT` | — | `readiness.py:_timeout` (per-probe network/folder timeout) |

Which entry point advertises which flag: `web.py` accepts `--host/--port/--allow-host` plus every
service flag (`--comfy-url`, `--comfy-root`, `--projects-root`, `--library-root`, `--out-dir`,
`--stats`, `--jobs-db`, `--director-url`, `--director-model`, `--notify-url`); `enhance.py`
accepts `--comfy-url/--comfy-root` and its four finishing flags (per-run overrides of the same
switches); `director.py` keeps its own `--url/--model` (a per-call override that beats
everything) and `--out`.

## Finishing pipeline (per-project `finishing` settings)

Everything that happens to a take *after* it is rendered is one settings object, held per
project (`project.json` key `finishing`) and edited under **Project settings → Finishing**:

| Key | Values | Switch it off / change it and… |
| --- | --- | --- |
| `upscale` | `{enabled, height}` — height one of `720` `1080` `2160` | the SeedVR2 nodes leave the enhance graph; readiness stops demanding the upscaler and its checkpoints for that project |
| `interpolation` | `{enabled, factor}` — factor one of `2` `4` | the RIFE node leaves the graph; the film is cut at the rendered rate |
| `encode` | `{crf}` `12`–`28`; `preset` is derived (`high` 16, `standard` 20, `small` 24) | only the encoder quality of the finished file moves |
| `fps_policy` | `interpolated` \| `original` | the one frame-rate decision; `original` also turns interpolation off |

The flow: takes render at `enhance_fps` (24) → Enhance runs exactly the enabled stages
(SeedVR2 → RIFE → H.264 encode) → each take gets a *finished* version at that size, rate and
CRF → **Export film** cuts those finals at the *same* fps and CRF. One function,
`finishing.output_fps()`, answers every frame-rate question — the graph's `frame_rate`, the
snapshot record, the cut plan's `export.fps` and the ffmpeg `fps=` filter — so enhance and the
film can no longer disagree. Before this, four places carried the rate twice
(`enhance_fps` × `enhance_multiplier` in `enhance.py`, `film_export_fps` in `project.py`, and
literal `48 fps` labels in `project.html`); all of them now derive from the policy.

- **Absent key = legacy.** A project without `finishing` gets `finishing.defaults()` built from
  `enhance_resolution`/`enhance_multiplier`/`enhance_crf` — the old pipeline exactly. Nothing
  rewrites `project.json` on read; the first save in Project settings writes the key.
- **Old fingerprints keep validating.** For a legacy project the enhance graph and the cut
  plan are byte-identical to what they were before this feature, so stored cut fingerprints
  still answer "same". The plan only gains `upscale`/`interpolation` keys once the project
  leaves the defaults — and that changes the fingerprint, marking the film
  `cut outdated: … export` until it is cut again.
- **Which made which final.** Each take's finished version records the enhance (or final-render)
  job that produced it; the scene flag `finishing_changed` compares that job's recorded
  settings with today's and names the part that moved (`upscaling 1080p -> off`, `crf 16 -> 20`).
  Finals from before job records carried settings answer `unknown_legacy` — nothing is invented.

**Migration.** `film_export_fps` and `film_export_crf` are gone from this table: they no longer
exist as settings. A `config.json` still carrying them (or those env vars) now produces an
unknown-key warning and the values are ignored — the film rate and CRF come from the project's
finishing settings, whose defaults equal the old 48 fps / CRF 16. The animatic keeps its own
fixed pair (`preview_export_fps`/`preview_export_crf`), unchanged.

## Inspecting the running configuration

- **Startup line.** `web.py` prints one line at startup with the effective service addresses:
  `FreeVideo director: UI http://127.0.0.1:8192 · ComfyUI http://127.0.0.1:8190 (websocket
  127.0.0.1:8190) · director searching 127.0.0.1:8144, 127.0.0.1:8078`. `director.py` and
  `enhance.py` print the same summary on stderr when run as a CLI.
- **`GET /api/config`** returns the effective, secret-free configuration: every setting with its
  value and **where the value came from** (`flag`, `env`, `config.json`, `default`), plus the
  derived websocket address and the `config.json` path. Secret settings (`notify_url`) are masked
  to scheme + host (userinfo and path hidden), and the startup line never repeats them.
- From the shell: `python3 -c 'import config; print(config.summary())'`.

## Deliberately not centralised (this step)

Binary *lookup* of `notify-send`, H3's 4–15 s length range, the library image size cap, project
edit-window/history limits, the encoder codec/preset/audio strings in `project.py`, ComfyUI's
fixed subfolder names (`input/director`, `output/director`, `enhance-tmp`, `.director-trash`),
`stats.py`'s history size, and the `eval.py` fixture picture paths. These are algorithmic or
page-level constants, not service addresses, paths or deployment defaults.
