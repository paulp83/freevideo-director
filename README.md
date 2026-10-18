# FreeVideo Director

Turn a rough idea (plus optional pictures) into a MiniMax H3 prompt, then render it on a local
[FreeVideo](https://github.com/FlashML-org/FreeVideo) ComfyUI workspace. It comes as a browser UI,
a CLI, and a multi-scene film editor. Pure Python standard library, no `pip install`.

![Director UI](docs/ui.png)

```
idea + images ──► Director UI :8192 ──► director model (local vision LLM) ──► H3 prompt
                                   └──► ComfyUI :8190 ──► FreeVideo engine ──► video.mp4
```

The director uses the official MiniMax-H3 `h3-prompt-writing` skill (`skill/`, pinned copy) as the
system prompt, and talks to any local OpenAI-compatible **vision** model.

## Requirements

- Python 3.10+ and `ffmpeg` on `PATH`
- A FreeVideo ComfyUI workspace on `127.0.0.1:8190` (needed to render, not to write prompts)
- A director model behind an OpenAI-compatible `/v1/chat/completions` endpoint. The tool uses the
  first of `127.0.0.1:8144` (`qwen3.8-27b`) or `127.0.0.1:8078` (`qwen3.6-35b-a3b`) that answers.
  Pin another with `DIRECTOR_URL` and `DIRECTOR_MODEL`.

## Quick start

```bash
python3 web.py                       # UI at http://127.0.0.1:8192
python3 director.py "a fox runs toward camera at dawn" --seconds 6      # CLI: writes a prompt to out/
```

In the UI: describe the idea, optionally add pictures, click **Write prompt**, edit it, then
**Render**. Use **Draft** for a quick look and **Final** (1080p, 48 fps) for the keeper.

For film projects, open **Film project →** (`/project`). The director storyboards a short film from
a logline, and you draft, pick, and finish scenes step by step:

![Film editor](docs/film-editor.png)

## Documentation

| | |
|---|---|
| [Usage](docs/usage.md) | Every UI control, film-project steps, all CLI flags, finishing takes, configuration |
| [Best practices](docs/best-practices.md) | Writing better prompts, choosing a director model, iterating cheaply, film workflow |
| [Troubleshooting](docs/troubleshooting.md) | Common errors, out-of-memory fixes, playback problems |
| [Multi-scene design](MULTISCENE-PLAN.md) | Design notes and planned work for film projects |

## Files

| Path | Purpose |
|---|---|
| `director.py` | Prompt writer and CLI |
| `web.py` | Browser UI and job runner (port 8192) |
| `project.py`, `project.html` | Multi-scene film projects |
| `enhance.py` | SeedVR2 upscale, RIFE 48 fps, H.264 encode |
| `h3check.py` | H3 format checks used by the retry loop and `eval.py` |
| `skill/` | Pinned MiniMax-H3 prompt-writing skill |
| `eval/` | Director-model A/B outputs (Qwen3.6-35B vs Qwen3.8-27B) |
| `docs/` | Documentation and screenshots |

All servers bind to `127.0.0.1` by default.
