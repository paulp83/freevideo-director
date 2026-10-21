# UI capability map — project.html workspace (Stage A + B1 + B2)

Inventory of every control/handler in the **old** `project.html` (pre-M3, kept byte-for-byte as
`project-classic.html` and served at `/project/classic?p=<slug>`), and where each capability lives in
the new workspace page. All three M3 stages are now live: Stage B1 (take comparison, validity-vs-
execution state, disabled-action reasons, unknown-job notices, continuity warnings) and Stage B2
(the in-page queue drawer with live-job cancel, header next-action, keyboard scene navigation).
Nothing in this table is pending; the honest remaining caveats are listed under "Caveats".

The **Selector** column lists the CSS selectors of the control as it exists in `project.html` today —
`tests/test_capability_map.py` fails if any of them is missing from the page, and
`tests/browser_acceptance_m3.py` walks every token in a real 390x844 render (visible after opening
its panel, inside the viewport, touch controls ≥ 44 px).

Verified-by legend: `autosave_check.js` = JS unit check; `browser_autosave.py` = Firefox check of the
classic page (autosave behaviour is page-independent, the core block is identical); `browser_status.py`
= Firefox check of badges/details on the classic page; `browser_layout.py` = Firefox check of the new
page (sizes, tap targets, loads ids/handler names below); `browser_compare.py` = Firefox check of the
Stage B1 surfaces (comparison view, validity-vs-execution wording, disabled-action reasons,
unknown-job notices, continuity context); `browser_queue.py` = Firefox check of the Stage B2 queue
drawer (rows, times, Tab-trap, Escape, poll-not-disturb); `browser_acceptance_m3.py` = end-to-end
acceptance walk of the whole flow against a stub ComfyUI (real POST bodies, real job state, keyboard-
only run, the Selector-column phone walk); `test_capability_map.py` = offline check that every
Selector below exists in the page; `test_jobs.py` = in-process job lifecycle with a stub ComfyUI.

| Old control (id / handler) | Old location | New location | Status | Selector (project.html) | Verified by |
|---|---|---|---|---|---|
| `#picker` open film (`open()`) | top bar | header `#picker` | kept | `#picker` | browser_layout (fixture load) |
| `#newBtn` + `#newBox` (name, logline, `#nSeconds`, `#nAspect`, 4 ref slots, `#createBtn` → POST `/api/projects`) | top of page | full-screen New film view (same ids) | kept | `#newBtn, #newBox, #nSeconds, #nAspect, #createBtn` | browser_layout (open view); browser_acceptance_m3 (phone walk) |
| Reference pictures upload/replace at film creation (the 4 `.slot`s, `newRefs`) | New film | New film view `#newBox` slots — per-film reference replacement never existed on this page | kept (as-is) | `#newBox, .slot` | browser_layout (slot handler fires) |
| Character library: `#nCharacters` pickers, `#libName`/`#libDesc`/`#libPick`/`#libAdd`, `#libList` delete → GET/POST `/api/library`, POST `/api/library/<slug>/delete` | New film only | Project settings drawer (add/delete/manage) **and** New film view (`#nLib*` mirror) | moved to Project settings | `#nLibName, #nLibAdd, #libName, #libAdd, #libList` | browser_layout (lib add form posts); browser_acceptance_m3 (phone walk) |
| Characters of this film: `#pChars` checkboxes + `#pCharsApply` → POST `/api/p/<slug>/characters` | global section | Project settings drawer (same ids) | moved to Project settings | `#pChars, #pCharsApply` | browser_layout (apply posts); browser_acceptance_m3 (phone walk) |
| Style bible `#pBible` (`markDirty bible`) | global section | Project settings drawer (same id) | moved to Project settings | `#pBible` | browser_layout (typing → /save) |
| Director's notes `#pNotes` | global section | Project settings drawer | moved to Project settings | `#pNotes` | browser_layout (typing → /save) |
| LoRAs `#loras`/`#addLora`/`#loraHint` (GET `/api/loras`, `markDirty loras`) | global section | Project settings drawer | moved to Project settings | `#loras, #addLora, #loraHint` | browser_layout (add LoRA → /save) |
| Music brief `#pMusicBrief`; track `#musicBtn`/`#musicName` → POST `/api/p/<slug>/music` | global section | Project settings drawer | moved to Project settings | `#pMusicBrief, #musicBtn, #musicName` | browser_layout (brief typing → /save) |
| Title/end cards `#pTitleCardText`/`#pTitleCardSecs`/`#pEndCardText`/`#pEndCardSecs` (`cardChanged`) | global section | Project settings drawer | moved to Project settings | `#pTitleCardText, #pTitleCardSecs, #pEndCardText, #pEndCardSecs` | browser_layout (card text → /save) |
| Film title `#pTitle` | global section | header shows name; editing input `#pTitle` in Project settings drawer | moved to Project settings | `#pTitle` | browser_layout (typing → /save) |
| Film length/aspect (`#nSeconds`, `#nAspect`) | New film | New film view; Project settings shows read-only `#pSizeText` (aspect + render size) — size was never editable per-film | kept | `#nSeconds, #nAspect, #pSizeText` | browser_layout (settings open) |
| Pruning `#freeSpace` → POST `/api/p/<slug>/prune` | Scenes header | Project settings drawer (same id) | moved to Project settings | `#freeSpace` | browser_layout (dry-run request) |
| Project re-plan `replan()` → POST `storyboard` | stepper step 1 | stage pill's caret menu "Re-plan…" (same handler) | kept | `.stage, .caret, #stageMenu` | browser_layout (pill → menu → POST); browser_acceptance_m3 (phone walk) |
| Write missing prompts `run('prompts',{only_missing:true})` | stepper step 2 | stage pill's caret menu | kept | `#stageMenu` | browser_layout (menu button → POST) |
| Draft missing (`draft-all`) | stepper step 3 | stage pill's caret menu | kept | `#stageMenu` | browser_layout |
| Assemble animatic (`assemble {kind:'animatic'}`) | stepper step 4 | "Preview film" button (`#bPreviewFilm`, viewer row) + stage pill's caret menu | kept, label fixed | `#bPreviewFilm, #stageMenu` | browser_layout (POST); browser_acceptance_m3 (real ffmpeg animatic runs, batch bar live) |
| Enhance ★ takes (`enhance-starred`) | stepper step 5 | stage pill's caret menu (Project-level batch) | kept | `#stageMenu` | browser_layout |
| Assemble film (`assemble {kind:'film'}`) | stepper step 6 | "Export film" button (`#bExportFilm`, viewer row) + stage pill's caret menu | kept, label fixed | `#bExportFilm, #stageMenu` | browser_layout (POST) |
| Batch progress bar `#batch`/`#batchText`/`#batchBar` + `#batchCancel` → POST `cancel` | stepper card | header batch bar (same ids) | kept | `#batch, #batchText, #batchBar, #batchCancel` | browser_acceptance_m3 (real animatic batch: bar shows, Cancel button present); browser_layout (fixture has no live batch; handler checked) |
| Per-scene cancellation | (same batch cancel) | inspector: header batch bar `#batchCancel` cancels the film's jobs (unchanged API) | kept | `#batchCancel` | browser_acceptance_m3 (visible during live batch) |
| Timeline `#timeline` + legend | section | below viewer; click selects scene instead of scrolling | kept | `#timeline, .tl` | browser_layout |
| Cut outputs `#animatic`/`#film` videos + info rows | "Cuts" section | viewer mode switch (Scene / Animatic / Film) — same `/files/...` srcs and info text | kept | `#viewer, #viewerMode` | browser_layout; browser_acceptance_m3 (Animatic/Film buttons live after a real assemble) |
| Save status `#saveStatus` (`flash()`) | Scenes header | header, always visible (autosave core unchanged) | kept | `#saveStatus` | autosave_check.js, browser_autosave.py (classic), browser_layout (new) |
| Autosave core (AUTOSAVE markers) | script | copied byte-for-byte | kept | `AUTOSAVE-CORE-BEGIN` | autosave_check.js |
| Add scene `#addScene` (`save([...])`) | Scenes header | inspector scene actions (`#bAddScene`) | moved | `#bAddScene` | browser_layout (structure save) |
| Scene order `move()` ↑↓ (`save(structure)`) | per scene header row | inspector `#bMoveUp`/`#bMoveDown` | moved | `#bMoveUp, #bMoveDown` | browser_layout (structure save) |
| Delete scene `remove()` | per scene header row | inspector `#bDeleteScene` | moved | `#bDeleteScene` | browser_layout (confirm + save) |
| Scene title/seconds/connect/transition inputs (`data-k`) | per scene card | inspector (`#scTitle`/`#scSeconds`/`#scConnect`/`#scTransition`) | moved | `#scTitle, #scSeconds, #scConnect, #scTransition` | browser_layout (typing → /save); browser_acceptance_m3 (phone walk) |
| Scene action/intent textarea (`data-k=action`) | per scene card | inspector `#scAction` | moved | `#scAction` | browser_layout; browser_acceptance_m3 (phone walk) |
| H3 prompt textarea + `mode`/`check` badges (details) | per scene card | inspector, always visible above the fold (no details wrapper); badges in the status line | moved | `#scPrompt` | browser_layout (typing → /save; textarea fully visible with `scrollTop==0`) |
| Prompt history `#hist`/`historyRows` + Restore → POST `restore_prompt` | per scene card | inspector expandable "Prompt history" | moved | `#hist, #histList` | browser_layout (Restore posts); browser_acceptance_m3 (phone walk after a real edit writes history) |
| Rewrite/Write prompt `bPrompt` (`scenePrompt()` → POST `prompt`) | per scene button | inspector primary action button, label **Create prompt** while the scene has no prompt (`#bPrimary` is the single primary; the label follows the scene state) | moved, label fixed | `#bPrimary` | browser_layout (POST); browser_acceptance_m3 (primary label right in all three viewports) |
| Draft render `bDraft` (POST `render` preset draft) | per scene button | same primary button, label **Generate preview** when the scene has no takes (`#bPrimary`) | moved, label fixed | `#bPrimary` | browser_layout (POST); browser_acceptance_m3 |
| Seed reroll `bReroll` (`sceneReroll`: POST `reroll` + POST `render`) | per scene button | inspector **Generate another take** (`#bAnother`; the primary button takes this label when the take in use is pinned) | moved, label fixed | `#bAnother, #bPrimary` | browser_layout (both POSTs); browser_acceptance_m3 (exact graph + uploads against the stub renderer) |
| Draft variants `#vCount` + `bVariants` (POST `variants`) | per scene row | inspector action row (Generate variants) | kept | `#vCount, #bVariants` | browser_layout (POST) |
| One-shot Final `bFinal` (POST `render` preset final) | per scene row | inspector action row ("Generate final: render + upscale + smooth + encode") | kept | `#bFinal` | browser_layout (POST) |
| Enhance ★ `bEnhance` (POST `enhance {scene}`) | per scene row | inspector **Enhance selected take** (`#bEnhanceSel`; stays visible with "no take to enhance" when the scene has none) | moved, label fixed | `#bEnhanceSel` | browser_layout (POST), browser_compare (reason on a new scene); browser_acceptance_m3 (POST /free + the enhance graph against the stub renderer) |
| Compare takes | — (did not exist) | inspector **Compare takes** → two-take side-by-side view (`#cmpWrap`/`#cmpBar`: shared seek, approximate sync, per-pane metadata + diffs) | live (Stage B1) | `#bCompare, #cmpWrap, #cmpBar, #cmpPlay, #cmpSeek, #cmpAudio, #cmpDiff` | browser_compare (open, play/seek/audio/diffs/use-take/Escape), browser_layout (enabled with 2 takes; reason on 1-take scene); browser_acceptance_m3 (counted flow picks a take from pane B) |
| Job progress chips `data-k=job` (stages) | per scene card | inspector status line ("Running · render …") + take-strip state text | kept (words, not colour) | `#scExec` | browser_layout (fixture text states); browser_acceptance_m3 (live job wording) |
| Validity badges `updateFlags` (`.flags .badge.stale`/`.soft`, FLAG_LABEL texts) | per scene card | inspector status line + navigator state text (same flag words from `scene.status`) | kept (words/icons) | `#scValidity, .navstate` | browser_status.py (classic); browser_layout (new, fixture text) |
| Take strip videos (`preload=none`, `.take`) | per scene card | take strip cards: poster `<img>` from GET `poster?id=` (ffmpeg, cached in `frames/`), placeholder + play overlay; **zero** `<video>` elements in the strip. The single `#viewer` is `preload="none"` too: a load fetches zero take media; src loads only on an explicit play | moved, lazy | `#takeStrip, .take, #viewer` | browser_layout (0 strip videos, posters, click → viewer src, `preload=none`); browser_acceptance_m3 (zero mp4/webm resource entries before the first user action) |
| Validity vs execution split (`#scValidity` / `#scExec` + `data-exec`, navigator `.navstate`, timeline chip wording) | one muddled status line | inspector status line + navigator + timeline chip (e.g. "Ready · prompt changed" vs "Running · upscale") | live (Stage B1) | `#scValidity, #scExec, .tl` | browser_compare (verbatim strings for each fixture state) |
| Disabled-action reasons (`#primaryWhy`, `#anotherWhy`, `#enhanceWhy`, `#renderWhy`, `#compareWhy`) | `title` tooltip only (mostly) | visible sentence beside each inspector button | live (Stage B1) | `#primaryWhy, #anotherWhy, #enhanceWhy, #compareWhy` | browser_compare (per-state reason strings); browser_acceptance_m3 (phone walk of each visible span) |
| Unknown-job notice (Reconcile → POST `job/<id>/reconcile`, Dismiss → native `<dialog>` → POST `job/<id>/resolve`) | nothing (job looked failed) | inspector notice `#unkJob` with both buttons + per-job reply line | live (Stage B1) | `#unkJob, #unkReconcile, #unkDismiss, #dismissAsk, #dismissGo` | browser_compare (notice strings, reconcile-keeps-unknown, dismiss frees scene), test_jobs.py (route); browser_acceptance_m3 (keyboard Dismiss dialog: opens, Escape restores focus, changes nothing) |
| Continuity-outdated context (`#contBox` + "Regenerate from current take") and instructions-changed honesty line | badge only | inspector notice box under the status line | live (Stage B1) | `#contBox, #contRegen` | browser_compare (scene 2 strings) |
| Cut-outdated flags `#animFlag`/`#filmFlag` beside Preview/Export film | absent | under each film button | live (Stage B1) | `#animFlag, #filmFlag` | browser_compare (flag contains outdated + takes) |
| Take badges (preset/final, seed, variant, chosen) | take meta | take strip card meta + viewer info | kept | `.take` | browser_layout |
| Choose take `bChoose` (POST `star {scene,take}`) | take meta | take strip card **Use this take**; the inspector's single primary action offers the same when the viewer take is unpinned | kept, one control per card | `[data-k=bUse]` | browser_layout (POST, badge "pinned"); browser_acceptance_m3 (explicit selection proven on disk + viewer update) |
| Release to auto `bAuto` (POST `star {auto:true}`) | take meta | the pinned card's own button (the one release control; no duplicate in the inspector) | moved | `[data-k=bUse]` | browser_layout (exactly one visible "Release to auto") |
| Take Details (GET `/api/p/<slug>/take?id=…`, `.trow` provenance rows, legacy honesty) | take card | inspector expandable "Technical details" (lazy, per-take cache `takeOpen/takeData`) | kept | `[data-k=bDetails], #techDetails, #techBox, .trow` | browser_status.py (classic); browser_layout (opens + rows); browser_acceptance_m3 (take id + seed rows) |
| Save-status flows: `pump/doSave/flushEdits`, 409 Reload/Keep-mine | script | script (unchanged except DOM glue) | kept | `flushEdits` | autosave_check.js, browser_autosave.py |
| Poll `setInterval(refresh, 3000)` + `setIfIdle` focus guard | script | script (same) | kept | `setInterval(refresh,` | browser_autosave.py (classic), browser_layout (new: typing focus survives poll); browser_acceptance_m3 (play + draft + drawer survive two poll cycles) |
| — (did not exist) | — | header **Queue** button + count → in-page drawer: this film's live jobs first, other films collapsed, honest two-time lines, per-job Cancel…/Reconcile/Dismiss inside rows | live (Stage B2) | `#openQueue, #queueDrawer, #queueMine, #queueOthersWrap, .qrow, .qkind, .qstate, .qtime, .qnote, #closeQueue` | browser_queue (rows, times, Tab-trap, Escape, poll-not-disturb); browser_acceptance_m3 (drawer lists a real running job with both times) |
| — (did not exist) | — | live-job cancellation from the drawer behind a confirm dialog (`Cancel…` → POST `cancel` only when proven) | live (Stage B2) | `#cancelAsk, #cancelAskWhat, #cancelGo` | browser_queue (dialog + targeted cancel); browser_acceptance_m3 (phone walk: dialog present at 390x844) |
| — (did not exist) | — | header next-action button naming the next step of the whole film | live (Stage B2) | `#nextAction` | browser_layout (inside viewport at every size); browser_acceptance_m3 (phone walk) |
| — (did not exist) | — | keyboard scene navigation: listbox with roving tabindex, Arrow/Home/End, `#navSay` live announcements | live (Stage B2) | `#navSay` | browser_queue (ArrowDown/End move + announce); browser_acceptance_m3 (keyboard-only full flow, focus restored after every close) |

## Caveats (honest, not pending features)

- **Per-take posters in real projects**: the strip renders `take.thumb_url` when a record carries one
  (fixture-only — real `project.json` takes never do); otherwise the card asks
  `GET /api/p/<slug>/poster?id=`, which extracts a frame with ffmpeg and caches it as
  `frames/<take id>-poster.jpg`. If no media is on disk the card falls back to a plain placeholder —
  nothing eager loads and the strip still holds no `<video>` elements.
- The queue drawer reads an in-memory queue: jobs live in the server process, and restarting the
  director forgets them (the drawer says so; ComfyUI may still finish them).
- `#batchCancel` is only present while a film-level batch runs; the acceptance walk shows it during
  a real animatic assemble instead of fabricating a batch.
- A shared block reason is written **once** (dedupe in `drawInspector`), so `#renderWhy` alone is
  never visible: whenever its text exists, an earlier span already shows the identical sentence.
  The phone walk therefore measures the four spans that can surface, not `#renderWhy`.

## Migration notes

- `project-classic.html` (the old page) stays served at `/project/classic?p=<slug>` unchanged until
  the lead retires it; both pages carry byte-identical AUTOSAVE core blocks.
