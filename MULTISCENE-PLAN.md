# Multi-scene films — plan

**Goal:** turn one idea into a 30–90 s film made of several FreeVideo scenes (each 4–15 s, H3's limit), with consistent characters and look, then splice them into one video with continuous sound.

**Status:** plan only, nothing built yet. It builds on what exists: the director (Qwen3.8-27B on GPU 1), Draft/Final rendering, and Enhance (SeedVR2 + RIFE + CRF 16).

---

## 1. The model: project → scenes → takes

```
Project  "Reston Wranglers: the escape"       logline, total length, aspect, refs, style bible
 ├─ Scene 1  8 s  Ref2VA   "the roundup"       prompt · takes [draft s1, draft s2, ★final]
 ├─ Scene 2  6 s  I2VA ←1  "he spins free"     starts on Scene 1's last frame (continuous action)
 ├─ Scene 3 10 s  Ref2VA   "the chase"         hard cut, same bible
 └─ Scene 4  5 s  FL2VA    "hat tip at sunset" lands on a chosen end frame
Assembly: cut list + transitions + music bed → film.mp4
```

Everything lives in `director/projects/<name>/project.json` (bible, scenes, prompts, takes, chosen take per scene). Takes point at FreeVideo run dirs, so nothing is copied.

## 2. Workflow (what the user does)

1. **Logline → storyboard.** Enter the idea, total length, aspect, and reference photos. The director returns a **scene list**: title, duration (4–15 s, summing to the target), mode, how each scene connects to the previous one (cut / continue / land-on), and one-line action. Edit it like a table: reorder, retime, add or delete scenes.
2. **Style bible.** The director also writes a short shared bible: each character's wardrobe and look, the location, the time of day and palette, the camera style, the music style. Every scene prompt is written *from the bible*, which is the main defence against character drift.
3. **Prompts for every scene** in one click. Each is still editable per scene.
4. **Draft pass.** Render all scenes as Drafts with fixed seeds (~2.5 min per 10 s scene). Re-roll or revise any scene until each has a take you like (★).
5. **Animatic.** Splice the chosen drafts (24 fps) into a rough cut to judge pacing and story before spending Final time.
6. **Final pass.** Enhance only the ★ takes (≈ 8–12 min each). No re-render is needed, because Enhance works from the draft's raw frames. Or re-render a scene as Final (single-pass) when sharpness matters most.
7. **Assemble the film.** Cuts and transitions, a continuous music bed, loudness normalisation, one 1080p48 export.

## 3. Continuity techniques (FreeVideo-specific)

| Connection | How | Notes |
|---|---|---|
| **Hard cut** (default) | Independent Ref2VA/T2VA scene: same references + bible text | Fine for new angles and locations. Expect some drift in faces and props. |
| **Continuous action** | Next scene is **I2VA** whose first frame = the **last frame of the chosen take**, taken losslessly from `video.artifacts/rgb.npy` | Seamless motion across the cut. Drift and softness accumulate, so re-anchor every 2–3 chained scenes. |
| **Land on a moment** | **FL2VA**: first frame from the previous scene, last frame = a chosen still | Good for endings and reveals. Needs a still (see keyframes). |
| **Keyframes (optional)** | Generate a clean still per scene with an image model, then use it as each scene's first frame | This is club-3090 studio's "storyboard" continuity. It needs an image model (HiDream-O1 / Chroma / Z-Image) and GPU time, so it's milestone M5. |

The director knows the connection type, so it writes the matching H3 mode and alignment line automatically.

## 4. Sound across cuts

H3 generates audio per clip, so music would restart at every cut. Plan:

- Scene prompts ask for **diegetic sound and dialogue only** (`non_diegetic_music` left minimal or "none").
- One **music bed** spans the whole film. It's either a track you supply, or generated (ACE-Step is in club-3090's studio, which would cost GPU time) — **decision needed**.
- At each cut: an 8–12-frame crossfade of the diegetic tracks; the music is ducked under dialogue (sidechain); the mix is normalised to −14 LUFS.

## 5. Assembly (ffmpeg, no GPU)

- Inputs: the ★ takes, all re-timed to one frame rate (48 for Finals, 24 for animatics) and one resolution.
- Video: concatenate with per-boundary transitions (`cut` default, plus `crossfade` and `dip-to-black` via `xfade`), and optional title and end cards.
- Audio: `acrossfade` the diegetic tracks, then `amix` the music bed with `sidechaincompress` ducking, then `loudnorm`.
- Output: `projects/<name>/film-<animatic|final>.mp4` (H.264 CRF 16, AAC). Optionally keep a lossless master (FFV1) for re-edits.
- Runs in seconds, so it's cheap to re-cut after swapping a take.

## 6. UI

A **Project** tab next to the current single-shot page:
- **Storyboard table:** one card per scene with duration, connection icon, prompt (editable), take thumbnails with ★, and status chips (no prompt / drafting / ★ draft / final).
- **Batch buttons:** *Write storyboard* → *Write all prompts* → *Draft all* → *Assemble animatic* → *Enhance ★ takes* → *Assemble film*. They run through the same job and stage tracker as today, queued in ComfyUI.
- **Timeline strip** under the table: scene widths ∝ duration, transitions between them, total length, and a play-through preview.

## 7. Time budget (60 s film = 6 × 10 s scenes)

| Pass | Per scene | 6 scenes |
|---|---|---|
| Storyboard + prompts | ~15 s (director) | ~2 min |
| Draft pass (×2–3 tries) | ~2.5 min each | 30–45 min |
| Animatic | — | < 1 min |
| Enhance ★ takes | ~8–12 min | ~50–70 min (can run unattended) |
| Assemble film | — | < 1 min |

## 8. Risks and mitigations

- **Character drift between scenes** (VDN isn't reference-trained). Mitigate with the bible text in every prompt, I2VA chaining for continuous action, and keyframes (M5) when faces must match.
- **Chained-frame degradation** (softness and colour drift building over several I2VA hops). Re-anchor with a reference or keyframe every 2–3 scenes.
- **Frame-rate or size mismatch** between Draft and Final takes in one cut. The assembler normalises everything, and the animatic is always 24 fps.
- **GPU contention.** The director (GPU 1) and FreeVideo (GPU 0) don't conflict. An image model for keyframes would need GPU 1 time, swapped with the director.

## 9. Milestones

| # | Deliverable | Depends on |
|---|---|---|
| **M1** | Storyboard + bible + per-scene prompts from the director (JSON schema; CLI + UI table) | — |
| **M2** | Project storage, per-scene takes with ★, batch Draft all | M1 |
| **M3** | Continuity: lossless last-frame handoff, auto I2VA/FL2VA per connection type | M2 |
| **M4** | Assembler: animatic + final, transitions, music bed + ducking + loudnorm | M2 |
| **M5** | Optional keyframes via an image model, for strong character consistency | M3 |
| **M6** | Polish: title/end cards, captions from dialogue, project export/import | M4 |

**Suggested order:** M1 → M2 → M4 (so a rough film exists early) → M3 → M5/M6.

## 10. Decisions for you

1. **Music:** your own tracks, generated (ACE-Step, costs GPU time), or both?
2. **Keyframes (M5):** worth an image model for consistent faces, or start with references + chaining?
3. **Default film shape:** typical length (30 s? 60 s?) and aspect (16:9 vs 9:16 for phones)?
