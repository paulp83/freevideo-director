# Eval fixtures

Images the eval cases (eval/cases.json) attach to prompts. Cases reference fixtures by **id**,
never by path, so a checkout without these files still runs: `eval.py --portable` uses only the
`gen-*` fixtures, and missing `gen-*` files are rebuilt by `python3 eval/make_fixtures.py`.

| id | kind | file | where it comes from |
|---|---|---|---|
| `group` | private | `group.jpg` | your own picture (cases C3-C6) |
| `rangels` | private | `rangels.jpg` | your own picture (cases C5, C6) |
| `gen-team-pizza` | generated | `generated/team-pizza.png` | `eval/make_fixtures.py` |
| `gen-cafe-mug` | generated | `generated/cafe-mug.png` | `eval/make_fixtures.py` |
| `gen-reston-ranch` | generated | `generated/reston-ranch.png` | `eval/make_fixtures.py` |

Private photos are deliberately not in git. Put your own files here with these names, or point
elsewhere via the settings `eval_group_image` / `eval_range_image` (env `EVAL_GROUP_IMAGE`,
`EVAL_RANGE_IMAGE`, or `--eval-group-image`), or set `eval_fixture_dir` to a directory holding
them. When a case's picture is missing, `eval.py` records a `skipped_fixture_missing` row naming
the id and every path searched - it never silently drops the case.

Generated fixtures are deterministic (fixed size, colours and label text; `--rebuild` on the same
ffmpeg build reproduces identical bytes). They live in `generated/` and are gitignored; they are
inputs, not results.
