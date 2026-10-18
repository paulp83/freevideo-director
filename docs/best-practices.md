# Best practices

What has worked in practice. Numbers come from this repo's own measurements (`eval/` and the
render timings in [usage](usage.md)), measured on one RTX 5090 for rendering and one for the
director model.

## Writing better prompts

**Give the director more than one line.** A one-line idea gives a competent but plain script. Name
the mood, time of day and camera style, and describe a beat per shot ("slow push-in, then a whip
pan to…"). Keep **Reasoning** on.

**Always read and edit the prompt before rendering.** A Draft costs about 2.5 minutes of GPU time;
editing the prompt costs about 30 seconds.

**Pick the right image mode.**

| You want | Use | Why |
|---|---|---|
| The same face and clothes as your photo | **First frame** (I2VA) | Starts from your actual photo, so likeness holds |
| These people or props in a new scene | **Reference images** (Ref2VA) | Loose likeness is expected: FreeVideo marks reference mode experimental |

For a first frame, crop the photo to just the people you want, at 16:9.

## Choosing a director model

In a 6-case, 2-run A/B (`eval/`), both models passed the H3 format checks almost every time
(Qwen3.8-27B 92/92, Qwen3.6-35B-A3B 91/92).

| | Qwen3.8-27B | Qwen3.6-35B-A3B |
|---|---|---|
| Scripts | ~50% longer; a separate definition per person, timed multi-shot plans, dialogue beats and audio cues tied to shots | Plainer |
| Same-seed render | A cut, multi-shot scene | One static take |
| Decode speed | ~130 tok/s | ~236 tok/s |
| Time to write a prompt | ~13 s | ~12 s (reasons in more tokens) |

Use the 27B for scripts you intend to keep, and the 35B for fast drafting.

## Iterating on a render

1. **Fix the seed** while you tweak the prompt, so you compare prompt changes and not random
   variation. Clear the seed to explore.
2. **Draft small.** Use 1024×576 at 5–6 s to check composition and motion, then render the keeper
   at 1344×768 and full length.
3. **Enhance only the keepers.** **✨ Enhance this take** reuses the Draft, so you pay the 4–6
   minutes of finishing once, not per attempt.
4. **Share Finals, not takes.** Takes use FreeVideo's default encode (x264 CRF 23, ~1–4 Mbps).
   Finals are CRF 16 at 1080p48, measured +3 dB over the default on the same frames, before the upscale.

## Film projects

- **Invest in the style bible.** Every scene prompt is written from it. Fix a wrong character
  description in the bible, not scene by scene, then re-write the prompts.
- **Use ↪ Continue where the action is continuous,** and **Cut** where the story jumps. A Continue
  scene starts on the exact last frame of the previous ★ take, so re-draft the previous scene first
  if you change it.
- **Star a take (★) on every scene before ④ and ⑤.** Downstream steps use the ★ take. Without
  one they fall back to the latest take.
- **Watch the Animatic before the Finals.** Pacing and story problems are cheap to fix at 24 fps;
  Finals cost ~4–6 min per scene.
- **Add music last.** Scenes render with sound effects and dialogue only, and the music file is
  ducked under them when you assemble the film.

## Keeping it healthy

- **One director model per GPU.** Only one fits on the card at a time.
- **Keep the H3 skill current.** The director's output format comes from the pinned copy in
  `skill/`. Refresh it from the upstream MiniMax-H3 repository
  (`skills/h3-prompt-writing`) now and then.
