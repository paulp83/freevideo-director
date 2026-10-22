# Migration notes

What this update changes on disk, and what happens if you go back. Nothing here is
destructive: no migration rewrites or deletes film content, and the durable stores only
add.

## What gets created or gains fields

| Path | Change |
|---|---|
| `projects/<slug>/project.json` | New bookkeeping written on every save: `rev` (whole-film revision) and `key_revs` (per-field revision). Scenes gain `selected_take` + `selection` (`auto` / `explicit`); a bare legacy `star` folds into those fields on the first new write, never on read. Take records gain `id`, `job`, `provenance`, `source_take` and `thumb_url`. `outputs` gains the film `fingerprint` and `cut_plan`. A film-level `finishing` block stores the per-project finishing settings. |
| `projects/<slug>/jobs/` | New: one immutable request snapshot `<job id>.json` per render/enhance job (prompt, mode, settings, reference hashes, start frame), plus the `<job id>.result.json` verdict written when the job ends. Snapshots are never edited in place; that is what makes a re-run of the same job replay-safe. |
| `projects/<slug>/refs-store/` | New: content-addressed copies (`<sha256><ext>`) of every input picture a job used, so a generation record stays readable even after the reference picture is replaced or deleted. |
| `jobs.db` (repo root) | New durable sqlite store for the queue: `PRAGMA user_version` walks 1 (jobs table) → 2 (recovery markers `retry_allowed`, `recovering_at`) → 3 (hand-retry links `retry_of`, `retried_as`) → 4 (`submit_unanswered`: a submit the director never saw answered, so Retry can be offered once ComfyUI shows no trace). Migration only ever adds columns. |
| `config.json` (repo root) | Stays optional; the new settings (`comfy_url`, `jobs_db`, finishing defaults…) all have `config.py` defaults and flag/env overrides like the old ones. |
| `pilot-runs/` | New, created by `pilot.py` only when you run the pilot. |

## Going back to an older version

An older `project.py` ignores unknown keys, so an older server can *open* a film this
editor wrote. But:

- An older editor **saves the whole scene list** where this one sends deltas, and its
  rewrite drops `rev`, `key_revs`, `selected_take`/`selection`, `finishing` and the take
  ids. Your take choice and finishing settings would silently reset to star/latest.
- An older director has no queue or recovery: jobs that are unfinished right now would
  simply vanish from its view (their ComfyUI work still runs and lands in the output
  folder; the takes can still be found by hand).

Safe rollback, in order:

1. Back up `projects/` and `jobs.db*` (that is the whole mutable state; takes themselves
   live in ComfyUI's output folder and are never touched).
2. Stop `web.py`, check out the older tree, start it.
3. Do not edit in both versions at the same time; one editor holds the revision clock.

Rolling *forward* again needs nothing: the new code reads old `project.json` files
untouched and folds in the new fields on its own first write.

## Before first use

1. `python3 -m unittest discover tests` (and the browser suites if you changed UI code).
2. `docker start` the director model and start the FreeVideo ComfyUI workspace, then
   start `python3 web.py` and check the three header chips read **Ready**.
3. Open an existing film once: the first save adds the bookkeeping fields; confirm the
   star still marks the take you expect (it now shows as a ★ with an **auto** tag).
4. The old single-column editor still answers at `/project/classic` if the workspace page
   ever misbehaves; say so instead of working around it.
