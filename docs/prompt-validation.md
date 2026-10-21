# Prompt validation: the rules `h3check.py` enforces

Every check encodes a rule the pinned MiniMax skill already states — `skill/SKILL.md`,
`skill/references/base-en.txt` (T2VA/I2VA/FL2VA/L2VA) and `skill/references/ref-en.txt` (Ref2VA).
The citations below point at that skill text; changing a rule means changing the pinned skill and
`h3check.py` together (see AGENTS.md). The check *names* are stable: they are what `prompt_check`
JSON stores, what the project page's format chip shows, and what the retry feedback quotes back to
the model. `h3check.MESSAGES` holds the same one-line messages.

The validator measures **format compliance only**. A clean report is not evidence the prompt will
render a good video; it only means the reply is shaped the way the video model expects.

## Shared rules (every mode)

| Check | Rule (skill citation) | Message on failure |
| --- | --- | --- |
| `empty_reply` | a reply must exist (SKILL.md:11-15 output contract) | the reply is empty - no prompt text at all |
| `refusal_only` | the workflow demands a formatted rewrite, not commentary (SKILL.md:11-14) | the reply is only a refusal or apology, not a prompt |
| `code_fence_only` | the final prompt is plain text (base-en.txt:38-44,173-222) | the reply is only a markdown code fence with no prompt inside |
| `fields_present` | every mode lists its sections (base-en.txt:38-48; ref-en.txt:13-22) | missing section(s): … |
| `fields_once` | one block per section | a section appears twice: … |
| `fields_in_order` | the sections appear in the documented order | sections appear as …; must be … |
| `fields_non_empty` | every section carries content (`N/A` is valid content for the two audio sections, base-en.txt:154,162) | no content after label: … |
| `unknown_section` | only the mode's labels are valid section names | not a section in this mode: … |
| `shot_numbering` | sequential shot numbers (base-en.txt:86) | expected [Shot 1]..[Shot n] in order … |
| `first_shot_zero` | *do not add a timestamp to the first shot* (base-en.txt:86; ref-en.txt:218) | a cut time appears inside [Shot 1] … |
| `shot_cuts` | each later shot *begins with* its cut time directly after the label (base-en.txt:86-90; ref-en.txt:218) | shot(s) without a cut time right after the label: … / stray cut time: … |
| `timestamps_format` | `At MM:SS.mmm` (base-en.txt:89; ref-en.txt:218); `At MM:SS` is also accepted, matching every skill example body that uses whole seconds | malformed stamp: … |
| `timestamps_monotonic` | *strictly increasing* cut times (base-en.txt:86) | cut times must strictly increase after 00:00 … |
| `timestamps_in_range` | cut times *fall within the video duration* (base-en.txt:86) | cut time(s) outside 0..Ns: … |
| `labels_valid` | `<Picture N>` must correspond to a provided image (base-en.txt:14-34; ref-en.txt:31) | references … but only n image(s) were provided / never references … / this mode has no images … |
| `dialogue_tags` | dialogue is `<d>[Language] …</d>` (base-en.txt:129; ref-en.txt:220) | unbalanced or empty `<d>` blocks |
| `no_markdown` | plain text prompt, no fences/headings/bold (base-en.txt:38-44,173-222) | contains code fence, bold **, heading |
| `no_placeholders` | the final prompt contains no template leftovers (`<...>`, `TODO`) or unknown `<...>` tags | leftover: … |

### Derived rules (stated here because they are implied, not spelled out)

- **Shot boundary coverage.** A stamp is only ever a *shot boundary* (base-en.txt:86: the stamp
  belongs to a later shot's label). Combined with `[Shot 1]` having no stamp and the timeline
  implicitly starting at `00:00`, the rule "first shot starts at 0, every other shot has a strictly
  increasing cut inside the duration" makes the shots tile `[0, duration)`. It is why
  `first_shot_zero` fires even when the number itself is in range: an `At 00:02.000` written inside
  `[Shot 1]`'s prose (as `eval/qwen36-35b/C6-ref-spin-1.txt` does) is either an unlabelled cut or
  a misused marker — the skill has no in-shot timestamp form.
- **Prose is not scanned for stamps.** Only `At` (capital A) followed by `NN:N…` counts, and within
  a shot block only a stamp directly after the `[Shot N]` label counts as that shot's cut. Sentences
  like "…at 00:05.500 the two pass under the drain outlet" (inside a shot's prose) fail
  `shot_cuts`/`first_shot_zero` as stray cut times, matching base-en.txt:86-90's form where the cut
  time opens the shot.
- **Instruction-line parameters are cross-checked.** For L2VA/FL2VA the skill says `N` is "the index
  of the actual final shot" and `S.SS` is "the effective video duration formatted to exactly two
  decimal places" (base-en.txt:34), so `instruction_line` requires the named shot number to equal
  the last `[Shot N]` in the body and the named time to equal the requested duration at `.2f`.
- **FL2VA writes `Picture N` without brackets.** base-en.txt:25 uses bare `Picture 1 (from Shot 1)`
  in the FL2VA instruction while L2VA (base-en.txt:31) uses `<Picture 1>`; the checks encode
  exactly that difference.
- **Refusal detection is conservative.** `refusal_only` fires only when the first line opens with a
  sorry/cannot/As-an-AI phrase, a required section is missing, and the whole reply is under ~600
  characters — a genuine prompt that merely apologises in passing is never flagged.

## Per-mode rules

| Mode | Instruction line | Sections | Extra checks |
| --- | --- | --- | --- |
| t2va | none — the reply opens with `integrated_multimodal_description:` (base-en.txt:14) → `no_instruction_line` | the three core fields (base-en.txt:38-48) | `labels_valid` fails if any `<Picture N>` appears (no images exist) |
| i2va | exactly the I2VA sentence (base-en.txt:16-20) → `instruction_line` | three core fields | `picture1_in_shot1`: `<Picture 1>` is "the actual first frame … and belongs to [Shot 1]" (base-en.txt:54), so the [Shot 1] block must reference it |
| l2va | the L2VA sentence with `N` = final shot, `S.SS` = duration (base-en.txt:28-34) | three core fields | — |
| fl2va | the FL2VA sentence, unbracketed (base-en.txt:22-26,34) | three core fields | bare `Picture 1`/`Picture 2` also count for `labels_valid` |
| ref2va | none — opens with `subject_definitions:` → `no_instruction_line` | six sections in order (ref-en.txt:13-22) | `picture_labels_defined`: every `<Picture N>` used must be defined or cited inside `subject_definitions` (ref-en.txt:37,62-68); `summary_task_prefix`: summary opens `[task type]` with types joined by ` + `, no repeats (ref-en.txt:123-141) |

## Rules the skill states that h3check does NOT check

- `<Video N>` / `<Audio N>` numbering and video/audio reference correctness (ref-en.txt:32-33) —
  no such assets exist in this product; images only.
- Section prose quality: 1-4 soundscape sentences, 1-3 music sentences (base-en.txt:154,162),
  description length, camera-motion vocabulary (base-en.txt:92-94), speaker-ID discipline
  (base-en.txt:127-129), verbatim dialogue/text (base-en.txt:129,146). These are prompt-content
  rules the retry loop cannot fix mechanically; the skill asks the model directly.
- `<scenetrans>` / `<cutoff>` usage across cuts (base-en.txt:142) — only their tag spelling is
  allowed by `no_placeholders`; using them correctly is not enforced.

## The retry loop

`director.direct_report` runs every applicable check on the model's reply. A failing reply is sent
back exactly once (when retries are on) with the failed check **names and their one-line messages**
(`fields_in_order: sections appear as …; must be …`), plus the standing instruction to output only
the corrected prompt. The better reply wins (ties go to the later attempt); a reply that still
fails is stored with its names in the job's `prompt_check` (`first_failures` / `final_failures` /
`retried`) and the format chip on the project page lists the names. If the final reply has no text
at all, the job fails with `the model replied with no prompt text (checks: …)`.

## Rescoring the saved runs

(added with `rescore.py`)
