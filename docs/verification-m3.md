# M3 Stage B3 — UX acceptance and capability verification

Run 2026-10-21 on branch tip `0739c69`. Everything below was executed, not reasoned:
`python3 -m py_compile *.py tests/*.py` OK; `python3 -m unittest discover tests` 125 tests OK
(includes the new `test_capability_map` table walker and the cancel-mid-encode test);
`node tests/autosave_check.js` 10 checks; `tests/browser_autosave.py` 33/33;
`tests/browser_status.py` 19/19; `tests/browser_layout.py` 78/78; `tests/browser_compare.py`
67/67; `tests/browser_queue.py` 54/54; `tests/browser_acceptance_m3.py` 66/66 (all suites
against the six-scene fixture `tests/fixtures/make_fixture.py`, headless Firefox, temp
projects root, stub ComfyUI). Screenshots: `docs/screenshots/m3-acceptance/` (7 files,
4 × 1440x900, 3 × 390x844, each < 400 KB).

| requirement | implemented behaviour | test performed | result | corrective notes |
| --- | --- | --- | --- | --- |
| layout regions: navigator left, viewer centre, intention right, takes/timeline below, header with name/save/backend/queue/next action | `project.html` grid; header carries `#nameTitle`/`#saveStatus`/`#backend`/`#openQueue`/`#nextAction` | `browser_layout.py` desktop stacking, header-above-viewer and next-action fits at 1920/1440/1024/768/390 (78 checks); acceptance "page does not scroll at 1440x900" and "scrollY stays 0" | pass | — |
| never scroll past global settings for the flow | settings live in the Settings dialog, not the page flow; page scrollY stays 0 through all steps | acceptance checks 4, 7, 24, 55 | pass | — |
| style bible, characters, music, title/end cards moved to Project settings | Settings dialog sections; capability-map rows verified there | `test_capability_map.py` walks every map row's selector column; phone walk opens the settings panel and measures each control | pass | — |
| explicit action labels (Create prompt / Generate preview / Generate another take / Compare takes / Use this take / Enhance selected take / Preview film / Export film) | labels drawn verbatim from state in `drawInspector`/`drawNext` | acceptance "primary 'Use this take'", "primary offers the previewed take, 'Generate another take' beside it", batch panel Preview film/Export film checks | pass | — |
| generating footage vs enhancing footage stay distinct | Generate posts `/prompt` with the generate graph; Enhance posts the enhance graph only after asking ComfyUI to free the GPU | acceptance flow 6 (exact `/prompt` body, one generate node) and enhance checks (SeedVR2+RIFE+VHS nodes, `interrupt` first, output prefix names the take run) | pass | — |
| no blanket "best quality" | button titles spell the actual processing (SeedVR2 upscale 1920×1080, RIFE ×2 to 48 fps, CRF-16) | `browser_status.py` wording checks; capability-map row text | pass | — |
| comparison: two user-picked takes, linked play/seek, one audio track, duration-mismatch indication, ids/seeds/setting diffs, explicit Use this take, no frame-exact claim | `#cmpWrap` panes + `cmpBar` | `browser_compare.py` 67/67 incl. pixel audits; acceptance flows 3 and 5 (seeds named, in-use pane labelled, unpinned pane offers Use, Escape restores focus) | pass | — |
| execution state shown separately from validity | `#scValidity` + `#scExec` drawn from different sources | acceptance "Running · render" + navigator badge; `b1-continuity-outdated` screenshot; status suite | pass | — |
| every disabled action carries a visible reason, shared reasons shown once | `sceneBlockReason`/`actionReason` + `shownWhy` dedupe in `drawInspector` | phone walk "whys" panel; capability-map walk asserts each why span or button title | pass | — |
| unknown-job scenes: honest notice, Reconcile/Dismiss, and the scene is not silently actionable | unknown block in `updateJob` with Reconcile/Dismiss…; server keeps the scene reserved until proved stopped | acceptance 35, 36, 48, 53, 61 (`resolved.verdict == 'dismissed'`, `by == 'user'`, holds freed) | pass | `0442d95`+`0739c69`: the primary action was left enabled (and `#primaryWhy` empty) on an unresolved unknown scene while the server refused the new job — a demonstrated lie; `drawInspector` now blocks the primary action too and shows "job state unknown - dismiss or reconcile it first". Caught by the phone walk. |
| queue drawer shows real jobs, queued time vs execution time, no survival claim | `queue_view` rows with `queued_for`/running seconds; "in this server session" wording | acceptance 19-22 (this-film live jobs, both honest times, other films collapsed behind a count); `browser_queue.py` 54/54 incl. targeted-cancel proof (stub `/queue` emptied) | pass | — |
| batch: header bar, progress, real Cancel, film export after | `.batch` bar with step/of; `assemble(cancel=)` monitors and `kill()`s the live ffmpeg | acceptance 32-34 (animatic + film export with real ffmpeg outputs) and 62 (phone Cancel… kills the stalled encode; batch error says cancelled); unit `test_cancelling_mid_encode_kills_ffmpeg_and_records_no_cut` | pass | `0442d95`: batch cancel was a UI no-op during the encode (single blocking `subprocess.run`); assemble now encodes under a Popen the worker can kill. |
| keyboard-only whole flow incl. dialogs/drawers/comparison, focus restored, never `<body>` | arrows in navigator only, Enter on cards/buttons, `<dialog>.showModal`, close handlers refocus the opener | acceptance 43-54 (Details, comparison, primary render started by Enter, drawer, cancel-confirm, Settings, Dismiss, focus-never-on-body) | pass | — |
| polling preserves typing focus and playback | dirty inputs not rewritten; video untouched by redraws | acceptance 39-42 (typed draft survives ≥2 polls with drawer open, video `currentTime` keeps advancing throughout) | pass | — |
| lazy take strips: no `<video>` outside viewer/comparison, zero media requests before interaction | take cards render `<img>` posters; `#viewer` `preload="none"` | acceptance 1-3 and 37 (after reload too); layout lazy-viewer check (`readyState 0`, src+poster set, no bytes) | pass | `0739c69` (viewer `preload="none"`; companion layout/compare expectations). |
| mobile 390x844: every capability-map control exists, is visible after opening its panel, ≥44 px, in-viewport | phone CSS (44 px incl. `input[type=range]`; stage caret exempt but ≥44 wide) | acceptance 56-60: walk of 17 panels over every kept/moved/live map row, 44 px + in-viewport per token, every token walked | pass | `0739c69`: save feedback was `display:none` on phones — the flash line now shares the picker row (`#saveStatus` visible, ellipsised); range inputs got the 44 px rule; header stays ≤190 px (layout re-passed 78/78). |
| dark theme walked (page honours `prefers-color-scheme`) | CSS palette follows the media query | acceptance session B via Firefox `layout.css.prefers-color-scheme.content-override`: `--bg #141312` at 1440x900 and in the 390x844 iframe with the drawer open | pass | contrast audit remains human (screenshots `3-mobile-dark.png`, `4-dark-drawer.png`). |
| six-scene acceptance: select scene 4, inspect take (Details, seed/ids), compare, keeper with explicit on-disk selection, next operation — ≤8 user actions, correct first primary action | full scripted flow at 1440x900 (and phone + 1920 variants) | acceptance 6-24: disk `selection == explicit` naming the older take, viewer update, exact `/prompt` body from disk state, counted 8 actions | pass | fixture gained the second Lantern take (seed 42) so scene 4 has a comparison and a keeper to pin. |
| capability map completeness + machine-checked selectors | `docs/ui-capability-map.md` rows all carry selector + verifier columns, no stale "pending Stage B" | `test_capability_map.py` (parses the doc, fails on missing selectors/verifiers), acceptance 57-58, 60 | pass | — |
| screenshots of the flow at both sizes | `1-load-lazy`, `2-compare-two-takes`, `3-queue-drawer-running`, `4-dark-drawer` (1440x900); `1-mobile-load`, `2-mobile-queue`, `3-mobile-dark` (390x844) | acceptance 66 (7 files < 400 KB) | pass | — |

## MOCKED checks (ran here)

- Stub ComfyUI: `POST /prompt` bodies are captured, never executed; `/queue`, `/history`,
  `/object_info`, `/interrupt` are modelled from the documented API. So "the drawer lists the
  job", "cancel emptied the queue" and the enhance graph shape are checks against the model,
  not a live renderer. No SeedVR2/RIFE ever ran; the enhance POSTs were asserted node-by-node.
- ffmpeg is the real binary everywhere except one deliberate wrapper: for the phone batch
  walk, a `#!/bin/sh` gate script shadows ffmpeg so the animatic stalls mid-encode and
  Cancel… provably kills it (the batch then reports `cancelled`); afterwards the gate reopens
  and the real ffmpeg runs the actual assemble, export and posters.
- Character library seeded in-process in the launcher (`library.ROOT` patched to a temp dir);
  the settings panel list is real, the repo `library/` is untouched.
- Headless Firefox (geckodriver, raw W3C WebDriver); phone is the 390x844 iframe approach;
  dark theme via Firefox content-override, not a real device.
- Music brief/duck and the single-shot link were carried and mapped but their audio behaviour
  was not exercised (no music track rendered here).

## REAL-BACKEND checks (not run; run against ComfyUI on 8190 before believing it)

- Real job progress in the drawer: stage chips and both time columns fed by a live render.
- Real Enhance from the workspace: raw `video.artifacts/rgb.npy` + `audio.wav` path and the
  mp4 fallback, SeedVR2 `decode_tiled=True`, `batch_size=4n+1`.
- Real poster generation on real takes (`#takeStrip` images come from ffmpeg over live media).
- `/interrupt` and `/queue delete` against a live prompt: confirm targeted cancel stays
  `cancelled`, and that an unanswered submit really lands `unknown` with holds kept.
- Reconcile against live `/history`/`/queue` entry shapes.
- *Write prompt* / storyboard against the live director model; `eval.py` pass count must not
  drop (this stage changed no prompt logic).
- A real phone/tablet at 390x844: the walk proves geometry and tap-target sizes, not touch
  feel.

## Migration notes

- `project.assemble()` gained an optional `cancel=None` keyword; existing callers are
  unaffected. The batch worker passes `lambda: bool(state.get('cancel'))`, so batch cancel
  now interrupts a live encode (the child is killed; the batch outcome is `cancelled`).
- Scenes with an unresolved unknown job now show their primary action disabled with the
  reason — matching the server, which has always refused new work for such a scene.
- The viewer video is `preload="none"` and take cards are posters: first media bytes arrive
  on user interaction. Anything scraping card DOM for `<video>` must read `<img>` instead.
- On phones the save flash is visible in the header again (picker row); the film title moves
  to the picker itself.
- `project.json` format is unchanged; old projects load and behave as before.
- The classic page (`/project/classic?p=` and `project-classic.html`) is untouched and stays
  until the lead retires it.
