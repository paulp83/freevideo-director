# M6 pilot: a 12-slot blinded A/B of prompt arms

Does writing the prompt with the director model produce **better videos** than pasting the user's
plain idea text straight into the video graph? The pilot answers that the only way it can be
answered: generate both, watch them blind, count acceptances and time. `pilot.py` runs the whole
protocol; everything for a run lives in `pilot-runs/<id>/` (gitignored).

> **The caution, verbatim (it is printed in every report, and the report deliberately contains no
> other prompt-related metric):**
> Prompt length, keyword matches and format compliance are NOT evidence of superior video quality.
> This report therefore shows none of them: only acceptance, the five blinded per-criterion
> scores, and time measured two ways (backend execution time kept separate from wall time).

That is a design constraint, not a style rule: prompt character counts, `h3check` pass rates and
token counts are all recorded during `prompts` for operational debugging, but they are never
joined into the report, and no report artefact carries any prompt text.

## Design

3 committed scenes (`pilot/scenes.json`) × 2 seeds × 2 arms = **12 slots**.

| Arm | What is rendered as the prompt |
|---|---|
| `simple` | The scene's `idea` text, **verbatim**. No wrapper of any kind — not even "make a video of". |
| `director` | The prompt `director.py` writes for the same idea, seconds and reference pictures (`director.direct_report`, with the usual `h3check` retry loop). |

Everything else is **identical between arms**: same graph (`web.build_graph` with the same width,
height, seconds, two-pass and LoRA settings), same seeds per slot pair, one take each, no
finishing pass, submitted one at a time through `web.py`'s tracked submit path (`submit_tracked`
/ `jobs.db`, client-generated prompt ids, nothing ever auto-resubmitted). The only difference a
reviewer can see is the prompt.

### Scenes and why

| Scene | Length | What it tests |
|---|---|---|
| `action-rooftop` | 8 s | Pure text-to-video with four sequential action beats (sprint, leap, roll, run). A weak prompt tends to drop or merge beats — that is exactly what the arm comparison is about. |
| `dialogue-kitchen` | 6 s | A spoken line plus diegetic sound: the only scene where the *audio* criterion can separate the arms; speech is where raw idea text most often loses structure. |
| `identity-mug` | 6 s | Image-referenced generation: identity means the mug (label, shape, colour) survives being handled. Portable by default (generated fixture `cafe-mug.png`); if you set `PILOT_PORTRAIT_IMAGE` to a private portrait it rides along as a second reference and identity also covers the person — equally for both arms. |

Each scene carries a `why` field in `pilot/scenes.json`; the table above is that text.

## Running it

```bash
python3 pilot.py plan           --id spring                     # 1) write the manifest
python3 pilot.py prompts        --id spring                     # 2) record both arms' prompts
python3 pilot.py render         --id spring                     # 3) 12 renders, ONE at a time
python3 pilot.py review-prepare --id spring                     # 4) blind the clips
python3 pilot.py review-serve   --id spring                     # 5) rate them in a browser
python3 pilot.py report         --id spring                     # 6) unblind and report
```

Flags worth knowing:

- `plan`: `--seeds 11,22` (two seeds), `--order-seed` (the render order is shuffled and the seed
  recorded, so time-of-day or thermal effects cannot line up with an arm), `--width/--height`
  (default 1344×768), `--no-two-pass`, `--scenes <file>`, `--fixture-dir`.
- `prompts`: `--redo-slot <slot-id>` regenerates exactly one arm-B prompt (needs the director
  model; arm A is recorded without it).
- `render`: `--only <ids>` renders a subset; `--retry-slot <ids>` explicitly resubmits
  finished/failed slots. A slot that ends `unknown` stops the run (exit 3) and both resume and
  `--only` refuse to move past it until it is resolved in the queue UI.
- `review-prepare`: `--seed` fixes the clip numbering (recorded in `key.json`; the default draws
  one and records it, so the run stays reproducible).
- `review-serve`: binds `ui_host` (127.0.0.1) on `pilot_port` (8392), overridable with
  `--host/--port` or `PILOT_PORT`.
- `report`: refuses with exit 2 and lists what is missing unless **every clip carries all five
  scores plus accept/reject**; `--partial` prints a clearly labelled `PARTIAL REPORT`.

The manifest is immutable after `plan` except the per-slot `prompt` and `render` records —
`save_manifest` refuses anything else, so nobody can quietly edit a scene or seed mid-run.

Exit codes: `0` done; `1` something failed but was recorded (a failed render, a failed director
call — both stay as rows, never retried behind your back); `2` refused (bad state, unrated
report); `3` a render ended `unknown` and the run stopped.

## Cost and time

12 Full renders at the FreeVideo resolution — expect a night of GPU time; the real duration is
entirely hardware-dependent and gets measured by the run itself, not guessed here. `prompts`
needs the director model (12 arm-B calls plus possible retries); arm A costs nothing.

## Blinding

`review-prepare` writes `review/clip-01.mp4 … clip-12.mp4`:

- slot → clip-number mapping comes from a recorded shuffle seed (a `random.Random` draw, stored
  in `key.json`), not from the file walk;
- every clip goes through **one identical ffmpeg re-encode** (`-map_metadata -1 -map_chapters -1`
  plus `+bitexact` on container and stream): the take's container metadata — including ComfyUI's
  own encoder tag — is stripped, and all twelve clips share the same container profile;
- filenames carry nothing but the number; the page, the HTML and the clip ids contain no arm,
  seed, scene id or prompt information (the browser test asserts this on the served DOM);
- `key.json` sits at `pilot-runs/<id>/key.json`, **outside** `review/`, and the review server
  never serves it.

The review page (`review-serve`, 127.0.0.1) rates each clip 1–5 on five criteria (Action
adherence, Identity, Motion, Cinematography, Audio) plus accept/reject and free notes. Every
change saves on its own into an append-only `ratings.json` (atomic writes, one entry per change,
latest per clip wins); a reload restores exactly where you stopped.

**Limits of the blinding, stated honestly:** the reviewer shares the machine and the model, so
both arms carry the same engine style; a familiar reviewer may still *infer* an arm from the
characteristic failure modes of weak prompts. The blinding removes label information and file
metadata, not inference — and n = 6 per arm cannot support significance claims anyway (the
report says so and shows ranges, not conclusions).

## What the report shows

Per arm and per scene: acceptance rate with `n`; per-criterion `{n, mean, min, max}`; and time —
kept in three separate figures:

1. **backend execution time**: from ComfyUI's own `/history` `execution_start` /
   `execution_success` millisecond stamps, per finished slot; time per accepted take =
   Σ backend over *all* rendered slots of the arm ÷ accepted takes (failed and unknown attempts
   spent time too, so they cost what they cost);
2. **wall time**: submission-to-result as measured by the pilot process, same Σ/accepted
   arithmetic;
3. **prompt seconds**: arm B only (the `director.py` call time including retries), reported both
   separately and added to wall — so "time per accepted take including writing the prompt" is
   visible without hiding what it contains.

No prompt text, lengths, token counts or check-pass counts appear anywhere in `report.md` or
`report.json`.

## Real-backend checklist (before the first real run)

1. **Check the timing stamps once.** On the installed ComfyUI, render anything and look at
   `GET /history/<prompt_id>`: the `messages` must include `execution_start` and
   `execution_success`. Older builds without them leave `backend_seconds` null and `render`
   prints a note about it — wall time still gets recorded, but figure out whether your build can
   do it properly before spending a night on 12 slots.
2. **Takes land in the ComfyUI output folder** (`comfy_root/output`), not in `pilot-runs/` — the
   manifest only stores the path.
3. Render the whole pilot without interruption if you can: an `unknown` slot (exit 3) means
   ComfyUI may still be working on that prompt; resolve it in the queue UI first, then resume.
   Nothing resumes automatically — `--retry-slot` is the explicit way back.
4. Set `PILOT_PORT` (default 8392) only if something already owns it, and `PILOT_PORTRAIT_IMAGE`
   if you want the private portrait to ride along (both arms get it; leaving it unset is fine).
5. Keep the run on one machine and one model for the whole 12 slots — the comparison only means
   anything under fixed conditions.

## Testing

- `tests/test_pilot.py`: plan/manifest immutability, both arms' prompt records (in-process
  director stub), the 12 renders one-at-a-time against an in-process ComfyUI stub (split backend
  times, failure/unknown handling, resume, `--only`/`--retry-slot`), blinding (name discipline,
  leak words absent from filenames/HTML/ffprobe tags, identical container profiles, key outside
  `review/`), the ratings server (append-only log, restore, 400s, concurrent writes) and the
  report (refusal gate, partial labelling, arithmetic).
- `tests/browser_pilot_review.py`: drives headless Firefox through geckodriver against the real
  review page: 12 clips render, click and arrow-key rating save, reload restores, and the served
  DOM leaks no arm/scene/seed information while `key.json` stays unserved.
