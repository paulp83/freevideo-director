# Usage

Full reference for the web UI, film projects, and the command line. For quick results see the
[README](../README.md); for getting better output see [best practices](best-practices.md).

## Configuration

| Variable | Effect |
|---|---|
| `DIRECTOR_URL` | Full chat-completions URL of the director model. Skips the backend search. |
| `DIRECTOR_MODEL` | Served model name to send (used with `DIRECTOR_URL`; default `default`). |
| `DIRECTOR_NOTIFY_URL` | Optional. A finished job POSTs its message here (ntfy style). `notify-send` is used too when available. |

Without `DIRECTOR_URL`, the tool tries `127.0.0.1:8144` (`qwen3.8-27b`) then `127.0.0.1:8078`
(`qwen3.6-35b-a3b`) and uses the first that answers.

## Web UI

```bash
python3 web.py            # http://127.0.0.1:8192   (--host, --port, --allow-host)
```

![Director UI](ui.png)

### 1. Idea and images

- **Idea:** plain words, in any language.
- **Reference images** (up to 2): people, objects or places to reuse, labelled `<Picture 1>` and
  `<Picture 2>` (Ref2VA).
- **First / last frame:** slot 1 opens the video, slot 2 ends it (I2VA, L2VA, FL2VA).
- **No images:** text only (T2VA).
- **Length:** 4–15 s.
- **Reasoning:** on gives better scripts (about 10–15 s to write); off gives instant, usually weaker drafts.

### 2. Prompt

**Write prompt** produces the H3 prompt for the mode: three fields, or six sections in reference
mode. Every prompt is saved to `out/`. Edit it freely before rendering.

### 3. Render

Pick **Size** and an optional **Seed**, then choose a preset:

| Preset | What it does | Time for 10 s |
|---|---|---|
| **Draft** | Fast two-pass render, 24 fps | ~2.5 min |
| **Final** | Sharp single-pass render, upscale to 1080p (SeedVR2), smooth to 48 fps (RIFE), encode H.264 CRF 16 with the original audio | ~9–10 min |

A tracker shows each stage live (step counts, elapsed time), and the take plays inline.

- **✨ Enhance this take** runs only the finishing stages on an existing Draft, with no re-render
  (~4–6 min for 10 s).
- **Recent takes** lists the session's takes so you can enhance an earlier one.
- `/queue` lists every job:

![Queue](queue.png)

**After a restart:** the director re-checks ComfyUI for every unfinished job. Jobs it cannot
confirm stay **unknown**: their scene stays reserved and they are never resubmitted automatically.
**Reconcile** asks ComfyUI again whether the prompt exists. When the evidence proves the submit
never reached ComfyUI (no prompt id was ever recorded, or a client-assigned id is absent from
ComfyUI's queue and history), a **Retry…** button appears. Retry re-checks ComfyUI first — if the
original prompt turns out to be running or finished it is adopted, not duplicated — and then
re-submits the *recorded* request (the prompt, seed, size and length exactly as first submitted)
as a new job, once. A retried job cannot be retried again, and a job whose submit ComfyUI
acknowledged never gets a Retry: resubmitting that on a guess could double the GPU work, so
reconcile or dismiss it instead.

## Film projects

Open **Film project →** (`/project`). A film is a set of scenes planned together, with a shared
style bible so characters and places stay consistent.

![Film editor](film-editor.png)

**New film:** name, logline, length (default 30 s), aspect (16:9 default, or 9:16), and up to 2
reference pictures. **Create & storyboard** (~20 s) plans 3–8 scenes of 4–15 s that sum to the length,
plus a **style bible** (every character's exact look, the places, palette, camera style) and a
**music brief**.

You can edit anything: scene title, seconds, **Cut** or **↪ Continue**, action, and the bible. Edits
save automatically. Scenes can be added, moved and deleted.

| Step | What it does | Time (5–10 s scenes) |
|---|---|---|
| ① Storyboard | Plans the scenes. **Re-plan** replaces all of them. | ~20 s |
| ② Prompts | Writes each scene's H3 prompt from the bible, with diegetic sound only (no score) | ~15 s per scene |
| ③ Drafts | Drafts every scene that has no take, in order, each with its own fixed seed | ~2–3 min per scene |
| ④ Animatic | Cuts the ★ takes (drafts, 24 fps) to check story and pacing | seconds |
| ⑤ Finals | Enhances only the ★ takes: upscale, 48 fps, CRF 16 | ~4–6 min per scene |
| ⑥ Film | Final cut at 1080p48, with music bed and loudness | seconds |

Per scene: **Write prompt**, **Draft**, **Final**, **✨ Enhance ★**, and **☆ choose** on any take.
Choosing pins that take explicitly (it becomes the ★): later takes never replace it. **auto** on the
chosen take releases the pin, and a scene that was never chosen simply follows its latest take.

**Continuity**

- **Cut** scenes start from the project's reference pictures plus the bible.
- **↪ Continue** scenes start on the exact last frame of the previous scene's ★ take (I2VA, taken
  losslessly from the raw frames). Re-draft the previous scene first if you change it.

**Sound**

- Scenes render without music.
- **Add music file…** sets one track for the whole film. It is looped or trimmed to length, ducked
  under the scenes' sound and dialogue, and faded out. Without a track, the film keeps the scenes' own sound.
- Every cut gets 30 ms audio fades, and the mix is normalised to −14 LUFS.

**Files** in `projects/<name>/`: `project.json`, `refs/`, `frames/` (handoff frames), `music.*`,
`animatic-*.mp4`, `film-*.mp4`. Takes stay in ComfyUI's output folder. Design notes:
[MULTISCENE-PLAN.md](../MULTISCENE-PLAN.md).

## Command line

```bash
python3 director.py "a fox runs toward camera at dawn" --seconds 6
python3 director.py "the two people on the right have a lightsaber duel" --ref group.jpg
python3 director.py "she turns and walks into the sea" --first start.png --last end.png
```

| Flag | Meaning |
|---|---|
| `--seconds N` | Target length, 4–15 s (default 10) |
| `--ref FILE` | Reference image (Ref2VA), repeatable, up to 4 |
| `--first FILE` / `--last FILE` | First / last frame (I2VA, L2VA, FL2VA) |
| `--no-think` | Skip model reasoning (faster, usually weaker) |
| `--temperature T` | Sampling temperature (default 0.7) |
| `--max-side N` | Downscale images to this long side (default 1024) |
| `--url URL` / `--model NAME` | Use a specific chat-completions endpoint |
| `--out FILE` | Write the prompt here (default `out/<timestamp>.txt`) |

## Finishing a take

```bash
python3 enhance.py <run dir or its video.mp4>    # --resolution 1080 --fps-multiplier 2 --crf 16
```

Enhance works from the take's raw decoded frames (`video.artifacts/rgb.npy` + `audio.wav`), never
its compressed MP4. It runs as one ComfyUI graph (SeedVR2, then RIFE, then H.264) after asking
ComfyUI to unload FreeVideo's idle engine. Output is 1890×1080 (16:9), 48 fps, ~20 Mbps, AAC.

Measured on an RTX 5090:

| Clip | Upscale (SeedVR2 7B fp8, 1080p, batch 21) | Smooth (RIFE ×2) | Encode |
|---|---|---|---|
| 6 s (144 frames) | 204 s | 8 s | 8 s |
| 8 s (192 frames) | ~230 s | ~10 s | ~10 s |

Peak VRAM at 8 s is about 16.9 GB. The first run downloads the SeedVR2 7B fp8 model (~7.9 GB), its
VAE, and the RIFE checkpoint.
