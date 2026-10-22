# Evaluating director models

`eval.py` runs a fixed case set against a director model over the OpenAI-compatible API and
scores every reply with `h3check.py` plus case-specific grounding checks. It answers "is this
new model better at writing H3 prompts?" with data that cannot silently shrink.

```sh
python3 eval.py --label qwen38-27b --url http://127.0.0.1:8144/v1/chat/completions --model qwen3.8-27b
python3 eval.py --portable --label qwen38-27b --url ... --model ...   # no private photos needed
python3 eval.py --label qwen38-27b --resume ...                        # continue an interrupted run
python3 eval_compare.py qwen36-35b qwen38-27b                          # diff two labels
python3 rescore.py                                                    # re-score old runs, no model
```

## The case set: `eval/cases.json`

Cases are **data**, not code, and reference pictures only by fixture id. Ten cases: t2va,
dialogue, reference-image, first-frame and two-image cases; grounding regexes quote text the
fixtures visibly contain. Two fixtures are private photos (`group`, `rangels`) kept out of git;
`--eval-fixture-dir` / `--eval-group-image` / `--eval-range-image` (env `EVAL_FIXTURE_DIR`,
`EVAL_GROUP_IMAGE`, `EVAL_RANGE_IMAGE`, or `config.json`) point at them, and `eval/fixtures/`
holds them by default. `--portable` drops only the cases that need private photos; the
generated fixtures (`gen-*`) are rebuilt on demand by `eval/make_fixtures.py` - deterministic
ffmpeg renders, so any machine reproduces the same images and the same label text. If a picture
is still missing, the case does **not** disappear: it gets a `skipped_fixture_missing` row
naming the id and every path searched.

## `results.json` (schema 2)

`eval/<label>/results.json` is a dict: a run header plus one row per **attempted** case-run.

- **Header** records everything a later comparison needs to trust the numbers: label,
  start/finish (UTC), `director_url`, the configured model and what `/v1/models` actually
  answered, the exact call settings (`temperature`, `max_tokens`, `top_p`, `seed`, `think`) and
  a sha256 of every `skill/` file, `skill_revision`, the validator block (`h3check_version`,
  `h3check_sha256`, `cases_sha256`, `eval_sha256`), git commit + dirty flag, python version and
  a hashed host name.
- **Rows** have `status` `ok`, `failed_call` or `skipped_fixture_missing`. A model call that
  dies (HTTP 500, refused connection, timeout, malformed JSON, empty reply) is a row with the
  error text, error class and attempt count - never a missing row.
- **Written after every case-run**, atomically (`.tmp` + rename). ctrl-C or a crash keeps
  everything that already ran; `--resume` continues the same label and refuses (exit 2) if the
  validator, case set or model changed underneath it.

Summary line, e.g. `qwen38-27b: 9/12 passed, 2 skipped, 1 failed calls`: the denominator is
every planned case-run, so failed calls and skips lower the pass rate instead of vanishing from
it. Exit code 1 whenever any call failed.

## Comparing labels: `eval_compare.py`

Prints every `(case, run)` where the failed checks or the call status differ, then per-label
`passed/total case-runs, failed calls, skipped`. It **refuses (exit 2)** unless both labels
record the same `h3check_version`, `h3check_sha256` and `cases_sha256` - a "regression" against
a stale `results.json` is often just a stricter checker. `--force` compares anyway with a
warning.

The schema-1 runs in `eval/qwen36-35b/` and `eval/qwen38-27b/` predate all of this (flat list,
validator unrecorded). Run `python3 rescore.py` first: it re-scores their saved replies with
today's `h3check` via the same scorer `eval.py` uses, writes `rescored.json` naming both the
recorded ("unknown") and current validators, and `eval_compare.py` then uses the re-scored
checks.

## Real-backend checklist (NOT yet run)

The schema-2 harness has so far only been exercised against a local HTTP model stub (the test
suite) and against re-scored schema-1 data. **No live director model has produced a schema-2
`results.json` yet** - that run is a checklist item for the operator (the model containers are
the user's; the harness never starts them):

```sh
curl -s http://127.0.0.1:8144/v1/models        # read the model id; 8078 is the fallback
python3 eval.py --portable --label qwen38-27b-portable-v2 \
    --url http://127.0.0.1:8144/v1/chat/completions --model <id> --runs 2
python3 eval_compare.py qwen36-35b qwen38-27b-portable-v2    # rescore.py must have run first
```

`rescore.py` has already stamped both old labels with today's validator, so the comparison
gate passes; the portable run covers 6 of the 10 cases, so the other old case-runs print as
`NO-ROW`.

## Migration notes (from the M2-M5 harness)

- `eval.py`'s hardcoded `CASES` list and the fixture paths in its header are gone; `rescore.py`
  and everything else read `eval/cases.json`.
- The old harness silently lost failed calls: a row got `passed=0, total=0` and both were
  skipped in the summary, and `results.json` was written only once at the very end - ctrl-C or
  a crash lost every row. Schema 2 fixes both; old files stay readable by `rescore.py`.
- `H3CHECK_VERSION` (currently `2`) in `h3check.py` must be bumped whenever a check's meaning
  changes; that, not a memory of what changed, is what `eval_compare.py` gates on.
