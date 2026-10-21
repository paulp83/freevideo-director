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
| `enhance_fps` | `24` | `ENHANCE_FPS` | — | `enhance.py:FPS` (raw-frame rate, graph `frame_rate`); `web.py:enhance_snapshot` |
| `enhance_resolution` | `1080` | `ENHANCE_RESOLUTION` | `--resolution` | `enhance.py:DEFAULTS/graph` (SeedVR2 short side) |
| `enhance_multiplier` | `2` | `ENHANCE_FPS_MULTIPLIER` | `--fps-multiplier` | `enhance.py:DEFAULTS/graph` (RIFE; `1` = off) |
| `enhance_crf` | `16` | `ENHANCE_CRF` | `--crf` | `enhance.py:DEFAULTS/graph` (VHS H.264) |
| `enhance_batch_size` | `21` | `ENHANCE_BATCH_SIZE` | `--batch-size` | `enhance.py:DEFAULTS/graph` (SeedVR2 frames per batch; keep `4n+1`) |
| `enhance_upscaler` | `seedvr2_ema_7b_fp8_e4m3fn_mixed_block35_fp16.safetensors` | `ENHANCE_UPSCALER` | — | `enhance.py:graph` (SeedVR2 DiT checkpoint) |
| `enhance_temporal_overlap` | `3` | `ENHANCE_TEMPORAL_OVERLAP` | — | `enhance.py:graph` |
| `enhance_rife` | `rife49.pth` | `ENHANCE_RIFE` | — | `enhance.py:graph` (RIFE checkpoint) |
| `film_export_fps` | `48` | `FILM_EXPORT_FPS` | — | `project.py:cut_plan/assemble` (`kind='film'`) |
| `preview_export_fps` | `24` | `PREVIEW_EXPORT_FPS` | — | `project.py:cut_plan/assemble` (`kind='animatic'`) |
| `film_export_crf` | `16` | `FILM_EXPORT_CRF` | — | `project.py:cut_plan/assemble` (`kind='film'`) |
| `preview_export_crf` | `20` | `PREVIEW_EXPORT_CRF` | — | `project.py:cut_plan/assemble` (`kind='animatic'`) |
| `xfade_seconds` | `0.5` | `XFADE_SECONDS` | — | `project.py:XFADE` (transition cuts) |
| `dip_seconds` | `0.4` | `DIP_SECONDS` | — | `project.py:DIP` (dip-to-black cuts) |
| `presets` | `draft` (two-pass, no finishing), `final` (single-pass, finishing) | — | — (config file only) | `web.py:PRESETS` |
| `readiness_cache` | `10` s | `READINESS_CACHE` | — | `readiness.py:check` (how long a readiness verdict is reused; must be > 0) |
| `readiness_timeout` | `2` s | `READINESS_TIMEOUT` | — | `readiness.py:_timeout` (per-probe network/folder timeout) |

Which entry point advertises which flag: `web.py` accepts `--host/--port/--allow-host` plus every
service flag (`--comfy-url`, `--comfy-root`, `--projects-root`, `--library-root`, `--out-dir`,
`--stats`, `--jobs-db`, `--director-url`, `--director-model`, `--notify-url`); `enhance.py`
accepts `--comfy-url/--comfy-root` and its four finishing flags; `director.py` keeps its own
`--url/--model` (a per-call override that beats everything) and `--out`.

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
