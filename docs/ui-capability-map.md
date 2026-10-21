# UI capability map — M3 Stage A (project.html workspace)

Inventory of every control/handler in the **old** `project.html` (pre-M3, kept byte-for-byte as
`project-classic.html` and served at `/project/classic?p=<slug>`), and where each capability lives in
the new workspace page. "Stage B" = deliberately deferred per the M3 plan (comparison UI, richer
selection UI, continuity-warning UI, queue drawer).

Verified-by legend: `autosave_check.js` = JS unit check; `browser_autosave.py` = Firefox check of the
classic page (autosave behaviour is page-independent, the core block is identical); `browser_status.py`
= Firefox check of badges/details on the classic page; `browser_layout.py` = Firefox check of the new
page (loads ids/handler names below).

| Old control (id / handler) | Old location | New location | Status | Verified by |
|---|---|---|---|---|
| `#picker` open film (`open()`) | top bar | header `#picker` | kept | browser_layout (fixture load) |
| `#newBtn` + `#newBox` (name, logline, `#nSeconds`, `#nAspect`, 4 ref slots, `#createBtn` → POST `/api/projects`) | top of page | full-screen New film view (same ids) | kept | browser_layout (open view) |
| Reference pictures upload/replace at film creation (the 4 `.slot`s, `newRefs`) | New film | New film view `#newBox` slots — per-film reference replacement never existed on this page | kept (as-is) | browser_layout (slot handler fires) |
| Character library: `#nCharacters` pickers, `#libName`/`#libDesc`/`#libPick`/`#libAdd`, `#libList` delete → GET/POST `/api/library`, POST `/api/library/<slug>/delete` | New film only | Project settings drawer (add/delete/manage) **and** New film view (`#nLib*` mirror) | moved to Project settings | browser_layout (lib add form posts) |
| Characters of this film: `#pChars` checkboxes + `#pCharsApply` → POST `/api/p/<slug>/characters` | global section | Project settings drawer (same ids) | moved to Project settings | browser_layout (apply posts) |
| Style bible `#pBible` (`markDirty bible`) | global section | Project settings drawer (same id) | moved to Project settings | browser_layout (typing → /save) |
| Director's notes `#pNotes` | global section | Project settings drawer | moved to Project settings | browser_layout (typing → /save) |
| LoRAs `#loras`/`#addLora`/`#loraHint` (GET `/api/loras`, `markDirty loras`) | global section | Project settings drawer | moved to Project settings | browser_layout (add LoRA → /save) |
| Music brief `#pMusicBrief`; track `#musicBtn`/`#musicName`/`#musicRemove` → POST `/api/p/<slug>/music` | global section | Project settings drawer | moved to Project settings | browser_layout (brief typing → /save) |
| Title/end cards `#pTitleCardText`/`#pTitleCardSecs`/`#pEndCardText`/`#pEndCardSecs` (`cardChanged`) | global section | Project settings drawer | moved to Project settings | browser_layout (card text → /save) |
| Film title `#pTitle` | global section | header shows name; editing input `#pTitle` in Project settings drawer | moved to Project settings | browser_layout (typing → /save) |
| Film length/aspect (`#nSeconds`, `#nAspect`) | New film | New film view; Project settings shows read-only `#pSizeText` (aspect + render size) — size was never editable per-film | kept | — |
| Pruning `#freeSpace` → POST `/api/p/<slug>/prune` | Scenes header | Project settings drawer (same id) | moved to Project settings | browser_layout (dry-run request) |
| Project re-plan `replan()` → POST `storyboard` | stepper step 1 | stage chip "Re-plan…" (same handler) | kept | browser_layout (chip fires confirm+POST) |
| Write missing prompts `run('prompts',{only_missing:true})` | stepper step 2 | stage chip | kept | browser_layout (click → POST) |
| Draft missing (`draft-all`) | stepper step 3 | stage chip | kept | browser_layout |
| Assemble animatic (`assemble {kind:'animatic'}`) | stepper step 4 | "Preview film" button (`#bPreviewFilm`) + stage chip | kept, label fixed | browser_layout (POST) |
| Enhance ★ takes (`enhance-starred`) | stepper step 5 | stage chip (Project-level batch) | kept | browser_layout |
| Assemble film (`assemble {kind:'film'}`) | stepper step 6 | "Export film" button (`#bExportFilm`) + stage chip | kept, label fixed | browser_layout (POST) |
| Batch progress bar `#batch`/`#batchText`/`#batchBar` + `#batchCancel` → POST `cancel` | stepper card | header batch bar (same ids) | kept | browser_layout (fixture has no live batch; handler checked) |
| Per-scene cancellation | (same batch cancel) | inspector: header batch bar `#batchCancel` cancels the film's jobs (unchanged API) | kept | browser_layout |
| Timeline `#timeline` + legend | section | below viewer; click selects scene instead of scrolling | kept | browser_layout |
| Cut outputs `#animatic`/`#film` videos + info rows | "Cuts" section | viewer mode switch (Scene / Animatic / Film) — same `/files/...` srcs and info text | kept | browser_layout |
| Save status `#saveStatus` (`flash()`) | Scenes header | header, always visible (autosave core unchanged) | kept | autosave_check.js, browser_autosave.py (classic), browser_layout (new) |
| Autosave core (AUTOSAVE markers) | script | copied byte-for-byte | kept | autosave_check.js |
| Add scene `#addScene` (`save([...])`) | Scenes header | inspector scene actions (`#bAddScene`) | moved | browser_layout (structure save) |
| Scene order `move()` ↑↓ (`save(structure)`) | per scene header row | inspector `#bMoveUp`/`#bMoveDown` | moved | browser_layout (structure save) |
| Delete scene `remove()` | per scene header row | inspector `#bDeleteScene` | moved | browser_layout (confirm + save) |
| Scene title/seconds/connect/transition inputs (`data-k`) | per scene card | inspector (`#scTitle`/`#scSeconds`/`#scConnect`/`#scTransition`) | moved | browser_layout (typing → /save) |
| Scene action/intent textarea (`data-k=action`) | per scene card | inspector `#scAction` | moved | browser_layout |
| H3 prompt textarea + `mode`/`check` badges (details) | per scene card | inspector expandable (same key `prompt`) | moved | browser_layout (typing → /save) |
| Prompt history `#hist`/`historyRows` + Restore → POST `restore_prompt` | per scene card | inspector expandable "Prompt history" | moved | browser_layout (Restore posts) |
| Rewrite/Write prompt `bPrompt` (`scenePrompt()` → POST `prompt`) | per scene button | inspector **Create prompt** (`#bCreatePrompt`, same flow incl. elapsed tick) | moved, label fixed | browser_layout (POST) |
| Draft render `bDraft` (POST `render` preset draft) | per scene button | inspector **Generate preview** (`#bPreview`) | moved, label fixed | browser_layout (POST) |
| Seed reroll `bReroll` (`sceneReroll`: POST `reroll` + POST `render`) | per scene button | inspector **Generate another take** (`#bAnother`, same two POSTs) | moved, label fixed | browser_layout (both POSTs) |
| Draft variants `#vCount` + `bVariants` (POST `variants`) | per scene row | inspector action row (Generate variants) | kept | browser_layout (POST) |
| One-shot Final `bFinal` (POST `render` preset final) | per scene row | inspector action row ("Generate final: render + upscale + smooth + encode") | kept | browser_layout (POST) |
| Enhance ★ `bEnhance` (POST `enhance {scene}`) | per scene row | inspector **Enhance selected take** (`#bEnhanceSel`) | moved, label fixed | browser_layout (POST) |
| Compare takes | — (did not exist) | inspector **Compare takes**, disabled with reason "Stage B" | not yet — Stage B (comparison UI is the next M3 step) | browser_layout (disabled + reason shown) |
| Job progress chips `data-k=job` (stages) | per scene card | inspector status line ("Running · upscale …") + take-strip state text | kept (words, not colour) | browser_layout (fixture has no live job; text states checked) |
| Validity badges `updateFlags` (`.flags .badge.stale`/`.soft`, FLAG_LABEL texts) | per scene card | inspector status line + navigator state text (same flag words from `scene.status`) | kept (words/icons) | browser_status.py (classic); browser_layout (new, fixture text) |
| Take strip videos (`preload=none`, `.take`) | per scene card | viewer take strip (same `preload=none`, poster/thumb first) | kept + lazy poster | browser_status.py #10 (classic); browser_layout (new) |
| Take badges (preset/final, seed, variant, chosen) | take meta | take strip card meta + viewer info | kept | browser_layout |
| Choose take `bChoose` (POST `star {scene,take}`) | take meta | take strip **Use this take** + inspector `#bUseTake` | kept, label fixed | browser_layout (POST) |
| Release to auto `bAuto` (POST `star {auto:true}`) | take meta | inspector button beside Use this take | kept | browser_layout |
| Take Details (GET `/api/p/<slug>/take?id=…`, `.trow` provenance rows, legacy honesty) | take card | inspector expandable "Technical details" (lazy, per-take cache `takeOpen/takeData`) | kept | browser_status.py (classic); browser_layout (opens + rows) |
| Save-status flows: `pump/doSave/flushEdits`, 409 Reload/Keep-mine | script | script (unchanged except DOM glue) | kept | autosave_check.js, browser_autosave.py |
| Poll `setInterval(refresh, 3000)` + `setIfIdle` focus guard | script | script (same) | kept | browser_autosave.py (classic), browser_layout (new: typing focus survives poll) |

## Not on the old project page (for completeness)

- "best quality" badge: only on `index.html` (single-shot page) — out of scope for this step; the new
  project page describes the actual chain everywhere instead ("SeedVR2 upscale to 1920×1080, RIFE
  interpolation to 48 fps, CRF-16 encode" — verified against `web.py` PRESETS/STAGES and
  `enhance.py` DEFAULTS).

## Pending Stage B (with reason)

- **Compare takes**: two-take linked playback UI is the next M3 step; the button ships disabled with the
  visible reason "Stage B".
- **Queue drawer**: the header links to the existing `/queue` page (real jobs, real times); an in-page
  drawer is Stage B.
- **Per-take thumbnails in real projects**: the page renders `take.thumb_url` when a take record carries
  one (the fixture demonstrates this); no server code writes thumbnails yet, so real projects fall back to
  a `preload="none"` video box — nothing eager loads.
