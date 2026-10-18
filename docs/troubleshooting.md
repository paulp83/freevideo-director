# Troubleshooting

| Symptom | Cause / fix |
|---|---|
| UI or CLI says **no director model is up** | None of the default endpoints answered. Start a director model, or point at one with `DIRECTOR_URL` and `DIRECTOR_MODEL` (see [usage](usage.md#configuration)). |
| UI says **ComfyUI is unreachable** | Start the FreeVideo ComfyUI workspace on `127.0.0.1:8190`. Writing prompts works without it; rendering does not. |
| Out of memory, or the engine won't start | Something else is using the render GPU. Check `nvidia-smi`. |
| A Final fails at **Upscale** with out of memory | SeedVR2 needs about 17 GB at 1080p. Check nothing else is on the GPU. Keep `decode_tiled: True` in `enhance.py` (untiled decode runs out of memory at 1080p on 32 GB), or lower `batch_size` (21 → 13; it must be 4n+1). Then retry with **Enhance this take**: the render is kept. |
| Vertical bars in **VLC**, clean in the browser | VLC's hardware decoding. Tools → Preferences → Input/Codecs → Hardware-accelerated decoding → Disable. If that doesn't help, set Video → Output → OpenGL. The file is fine. |
| Video won't play in GNOME Videos | Missing H.264 parser: `sudo apt install gstreamer1.0-plugins-bad`, or use VLC or `mpv`. |
| Likeness of people from references is loose | Expected: FreeVideo marks reference mode experimental. Use a first frame instead (see [best practices](best-practices.md#writing-better-prompts)). |
| A prompt looks malformed (missing fields, bad timestamps, markdown) | The director checks the format (`h3check.py`) and retries on its own. If a prompt still comes back wrong, regenerate with **Reasoning** on, or fix it by hand before rendering. |
