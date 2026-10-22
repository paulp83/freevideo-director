# Troubleshooting

| Symptom | Cause / fix |
|---|---|
| UI or CLI says **no director model is up** | None of the default endpoints answered. Start a director model, or point at one with `DIRECTOR_URL` and `DIRECTOR_MODEL` (see [usage](usage.md#configuration)). |
| UI says **ComfyUI is unreachable** | Start the FreeVideo ComfyUI workspace on `127.0.0.1:8190`. Writing prompts works without it; rendering does not. |
| A job shows **state unknown** after a restart | The director could not prove what ComfyUI is doing with it; nothing was resubmitted and the scene stays reserved. Use **Reconcile** to ask ComfyUI again. If the submit provably never arrived, **Retry…** re-submits the recorded request once, after re-checking ComfyUI first; if ComfyUI had acknowledged the submit, resubmitting on a guess could double the GPU work — reconcile or dismiss instead ([usage](usage.md#web-ui)). |
| A save stops with **changed elsewhere** | The film moved on while you were editing — another tab, or a job recorded a take. **Reload** reads the newer film; **Keep mine** re-applies only the fields you actually touched. Delta saves mean a save can never clobber a take or a field you did not edit. |
| Right after a restart an unfinished job seems to do nothing | The director is re-checking ComfyUI for every unfinished job. If ComfyUI is not up yet, the rows keep their state (**ComfyUI not reachable yet**) and the reconcile pass tries again once it answers — nothing is lost or resubmitted in the meantime. |
| Out of memory, or the engine won't start | Something else is using the render GPU. Check `nvidia-smi`. |
| A Final fails at **Upscale** with out of memory | SeedVR2 needs about 17 GB at 1080p. Check nothing else is on the GPU. Keep `decode_tiled: True` in `enhance.py` (untiled decode runs out of memory at 1080p on 32 GB), or lower `batch_size` (21 → 13; it must be 4n+1). Then retry with **Enhance this take**: the render is kept. |
| Vertical bars in **VLC**, clean in the browser | VLC's hardware decoding. Tools → Preferences → Input/Codecs → Hardware-accelerated decoding → Disable. If that doesn't help, set Video → Output → OpenGL. The file is fine. |
| Video won't play in GNOME Videos | Missing H.264 parser: `sudo apt install gstreamer1.0-plugins-bad`, or use VLC or `mpv`. |
| Likeness of people from references is loose | Expected: FreeVideo marks reference mode experimental. Use a first frame instead (see [best practices](best-practices.md#writing-better-prompts)). |
| A prompt looks malformed (missing fields, bad timestamps, markdown) | The director checks the format (`h3check.py`) and retries on its own. If a prompt still comes back wrong, regenerate with **Reasoning** on, or fix it by hand before rendering. |

## Readiness

The project page header carries three chips — **Prompts**, **Video**, **Finishing** — one per
dependency group (the checks live in `readiness.py`). Each shows a word: **Ready**, **Limited**
(something is unconfirmed, everything still works) or **Down** (a required piece is missing).
Click a chip, or focus it and press Enter, for the item list: what is missing, what it is called,
and the fix line. A Down chip is not decoration — the actions that need it are disabled with the
reason spelled out beside them (a stage-menu item instead closes and flashes the same sentence),
and the server refuses those routes too with the same sentence: HTTP 503, `error` in the body.
**Limited never disables anything.** The queue page has no chips: it is a view of jobs
already accepted, and its fix rows link back to the project page.

| Chip | Item | What it means / how to fix |
|---|---|---|
| Prompts | director model | No configured endpoint answered (`8144`, then `8078`). Start one — `docker start director-qwen38-27b` or `freevideo-director` — or set `DIRECTOR_URL` / `DIRECTOR_MODEL`. |
| Prompts | model name | The endpoint answers but does not serve the configured model; set `DIRECTOR_MODEL` to a name it reports. |
| Prompts | H3 skill files | `git checkout skill/` — the prompt guides under `skill/` are missing. |
| Video | ComfyUI server | Not reachable at `comfy_url` (`127.0.0.1:8190`). Start the FreeVideo ComfyUI workspace. |
| Video | render node set | The FreeVideo custom nodes are not installed — the classes the render graph needs are absent from `/object_info`. Install the FreeVideo node pack and restart ComfyUI. |
| Video | LoRA picker | The LoRA input lists no choices (informational); renders without a LoRA still work. |
| Video | ComfyUI output folder | `comfy_root/output` is missing or unwritable — takes would have nowhere to land. Point `comfy_root` at the real ComfyUI install. |
| Video | ffmpeg | Needed to stitch continuation frames; install ffmpeg. |
| Finishing | SeedVR2 upscaler not installed · frame interpolation not installed · video in/out nodes not installed | A node *family* is absent: `ComfyUI-SeedVR2_VideoUpscaler`, RIFE, or VideoHelperSuite. Install the named pack and restart ComfyUI. |
| Finishing | model file &lt;name&gt; | A SeedVR2 model file is not among the choices the nodes enumerate; `comfy_root/input` may point at the wrong place. |
| Finishing | ffmpeg filters for export | `ffmpeg -filters` lacks a filter the assemble step uses; install a full ffmpeg build. |
| Finishing | raw frame fallback | Informational: `video.artifacts/rgb.npy` + `audio.wav` are gone, so Enhance falls back to `video.mp4`. Expected once ComfyUI cleaned the take; nothing to fix. |

Finishing being Down never blocks writing prompts or rendering Drafts — that is by design.
Which actions each chip gates: **Prompts** — Create prompt / storyboard / Rewrite prompt and
film creation; **Video** — Draft renders, Generate another take, variants, Draft (all);
**Finishing** — Enhance, Final, Assemble and Export (Final also needs Video).
