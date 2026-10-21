#!/usr/bin/env python3
"""Enhance a finished FreeVideo take: SeedVR2 upscale -> RIFE frame interpolation -> CRF encode.

Works from the render's retained raw frames (video.artifacts/rgb.npy + audio.wav) when they exist, so the
take's own lossy MP4 is never re-compressed. FreeVideo 0.4.9+ deletes them once the MP4 is saved; then the MP4 is the source. Runs as a ComfyUI graph on the FreeVideo
workspace (:8190, GPU 0); FreeVideo's idle engine is released first via ComfyUI's /free.

  python3 enhance.py <freevideo run dir | video.mp4> [--resolution 1080] [--fps-multiplier 2]
"""
import argparse, ast, json, os, shutil, subprocess, tempfile, time, urllib.parse, urllib.request
from pathlib import Path

COMFY = os.environ.get('COMFY_URL') or 'http://127.0.0.1:8190'
COMFY_ROOT = Path(__file__).resolve().parent.parent / 'ComfyUI'
FPS = 24

DEFAULTS = dict(resolution=1080, multiplier=2, crf=16, upscaler='seedvr2_ema_7b_fp8_e4m3fn_mixed_block35_fp16.safetensors',
                batch_size=21, temporal_overlap=3, rife='rife49.pth')


def _post(path, body):
    req = urllib.request.Request(COMFY + path, json.dumps(body).encode(), {'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
    return json.loads(raw) if raw.strip() else {}


def _get(path):
    with urllib.request.urlopen(COMFY + path, timeout=60) as r:
        return json.load(r)


def run_dir_of(path):
    """Accept a FreeVideo run dir, its video.mp4, or anything inside it."""
    p = Path(path).expanduser().resolve()
    for candidate in (p, *p.parents):
        if (candidate / 'video.artifacts' / 'rgb.npy').is_file():
            return candidate
        if (candidate / 'video.mp4').is_file() and (candidate / 'video.artifacts').is_dir():
            return candidate                      # FreeVideo 0.4.9+ frees the raw frames after saving the MP4
    raise FileNotFoundError(f'no video.artifacts/rgb.npy or video.mp4 at or above {p} (only FreeVideo renders can be enhanced)')


def npy_header(path):
    """(shape, data offset) of a uint8 .npy file, stdlib only."""
    with open(path, 'rb') as f:
        if f.read(6) != b'\x93NUMPY':
            raise ValueError(f'not an .npy file: {path}')
        major = f.read(2)[0]
        size = int.from_bytes(f.read(2 if major == 1 else 4), 'little')
        header = ast.literal_eval(f.read(size).decode('latin1'))
        offset = f.tell()
    if header['descr'] not in ('|u1', '<u1') or header['fortran_order']:
        raise ValueError(f'unexpected frame array layout: {header}')
    return header['shape'], offset


def intermediate(run, workdir):
    """Write the raw frames (+ audio) losslessly to <workdir>/<run>-raw.mkv.

    `workdir` is the enhance job's private temp dir (see new_workdir), so concurrent enhances
    never share or clobber an intermediate. Without raw frames (FreeVideo 0.4.9+) return the
    take's own video.mp4 and no frame count.
    """
    if not (run / 'video.artifacts' / 'rgb.npy').is_file():
        return run / 'video.mp4', None
    shape, offset = npy_header(run / 'video.artifacts' / 'rgb.npy')
    frames, height, width, _ = shape
    out = workdir / f'{run.name}-raw.mkv'
    audio = run / 'video.artifacts' / 'audio.wav'
    cmd = ['ffmpeg', '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{width}x{height}',
           '-r', str(FPS), '-i', 'pipe:0']
    if audio.is_file():
        cmd += ['-i', str(audio), '-c:a', 'flac']
    cmd += ['-c:v', 'ffv1', '-pix_fmt', 'bgr0', str(out)]
    with open(run / 'video.artifacts' / 'rgb.npy', 'rb') as src:
        src.seek(offset)
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        while chunk := src.read(16 << 20):
            proc.stdin.write(chunk)
        proc.stdin.close()
        if proc.wait():
            raise RuntimeError('ffmpeg failed writing the lossless intermediate')
    return out, frames


def workdir_parent():
    """Parent of the per-enhance temp dirs; inside ComfyUI's input folder so the graph can read it."""
    return COMFY_ROOT / 'input' / 'director' / 'enhance-tmp'


def new_workdir(run, ident):
    """Private temp dir for one enhance job; the name carries the take and the job id."""
    parent = workdir_parent()
    parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f'{run.name}-{ident}-', dir=parent))


def remove_workdir(workdir):
    """Delete one enhance job's temp dir; never raises, so it is safe in a finally."""
    if workdir:
        shutil.rmtree(workdir, ignore_errors=True)


def sweep_workdirs(keep=(), max_age=86400):
    """Drop leftover enhance temp dirs from crashed or killed runs: older than `max_age`
    (a day by default) and not named in `keep` -- the dirs of jobs still live or still
    unknown. A dir holding a fresh file survives, whatever the dir entry itself says."""
    parent = workdir_parent()
    if not parent.is_dir():
        return []
    cutoff = time.time() - max_age
    removed = []
    for d in sorted(parent.iterdir()):
        if not d.is_dir() or d.name in keep:
            continue
        try:
            if d.stat().st_mtime > cutoff or any(f.stat().st_mtime > cutoff for f in d.rglob('*')):
                continue
        except OSError:
            continue                                  # vanished under us
        shutil.rmtree(d, ignore_errors=True)
        if not d.exists():
            removed.append(d.name)
    return removed


def graph(source, run_name, **opts):
    o = {**DEFAULTS, **opts}
    return {
        '1': {'class_type': 'VHS_LoadVideoPath', 'inputs': {
            'video': str(source), 'force_rate': 0, 'custom_width': 0, 'custom_height': 0,
            'frame_load_cap': 0, 'skip_first_frames': 0, 'select_every_nth': 1, 'format': 'None'}},
        '2': {'class_type': 'SeedVR2LoadDiTModel', 'inputs': {
            'model': o['upscaler'], 'device': 'cuda:0', 'offload_device': 'cpu', 'cache_model': False,
            'blocks_to_swap': 0, 'swap_io_components': False, 'attention_mode': 'sdpa'}},
        '3': {'class_type': 'SeedVR2LoadVAEModel', 'inputs': {
            'model': 'ema_vae_fp16.safetensors', 'device': 'cuda:0', 'offload_device': 'cpu', 'cache_model': False,
            'encode_tiled': True, 'decode_tiled': True}},
        '4': {'class_type': 'SeedVR2VideoUpscaler', 'inputs': {
            'image': ['1', 0], 'dit': ['2', 0], 'vae': ['3', 0], 'seed': 42, 'resolution': o['resolution'],
            'max_resolution': 0, 'batch_size': o['batch_size'], 'uniform_batch_size': False,
            'color_correction': 'lab', 'temporal_overlap': o['temporal_overlap'], 'offload_device': 'cpu'}},
        '5': {'class_type': 'RIFE VFI', 'inputs': {
            'frames': ['4', 0], 'ckpt_name': o['rife'], 'clear_cache_after_n_frames': 10,
            'multiplier': o['multiplier'], 'fast_mode': True, 'ensemble': True, 'scale_factor': 1.0,
            'dtype': 'float32', 'torch_compile': False, 'batch_size': 1}},
        '6': {'class_type': 'VHS_VideoCombine', 'inputs': {
            'images': ['5', 0] if o['multiplier'] > 1 else ['4', 0], 'audio': ['1', 2],
            'frame_rate': FPS * max(1, o['multiplier']), 'loop_count': 0,
            'filename_prefix': f'director/{run_name}-final', 'format': 'video/h264-mp4',
            'pix_fmt': 'yuv420p', 'crf': o['crf'], 'save_metadata': True, 'trim_to_audio': False,
            'pingpong': False, 'save_output': True}},
    }


def free_gpu():
    """Ask ComfyUI to unload models; FreeVideo's residency hook closes its idle engine too."""
    _post('/free', {'unload_models': True, 'free_memory': True})
    time.sleep(3)


def submit(path, ident='cli', **opts):
    """Prepare the intermediate in a private temp dir and queue the enhance graph.

    Returns (prompt_id, run dir, work dir); the caller must remove_workdir() in a finally once
    ComfyUI no longer needs the intermediate. On failure the dir is already removed.
    """
    run = run_dir_of(path)
    workdir = new_workdir(run, ident)
    try:
        source, _ = intermediate(run, workdir)
        free_gpu()
        reply = _post('/prompt', {'prompt': graph(source, run.name, **opts), 'client_id': 'freevideo-director'})
    except BaseException:
        remove_workdir(workdir)
        raise
    return reply['prompt_id'], run, workdir


def result(prompt_id):
    """None while running; else dict(path=..., url=...) or raises with ComfyUI's error."""
    entry = _get('/history/' + prompt_id).get(prompt_id)
    if not entry:
        return None
    status = entry.get('status', {})
    for kind, data in status.get('messages', []):
        if kind in ('execution_error', 'execution_interrupted'):
            raise RuntimeError(data.get('exception_message') or kind)
    if not status.get('completed'):
        return None
    items = [item for output in entry.get('outputs', {}).values()
             for item in output.get('gifs', []) + output.get('videos', [])
             if item.get('filename', '').endswith('.mp4')]
    items.sort(key=lambda item: not Path(item['filename']).stem.endswith('-audio'))   # prefer the one with sound
    for item in items[:1]:
        rel = Path(item.get('subfolder', '')) / item['filename']
        if rel.stem.endswith('-audio'):
            # VHS also writes a silent twin and a PNG thumbnail of the same take; keep only this one.
            twin = COMFY_ROOT / 'output' / rel.with_name(rel.stem[:-len('-audio')])
            for extra in (twin.with_suffix('.mp4'), twin.with_suffix('.png')):
                extra.unlink(missing_ok=True)
        q = urllib.parse.urlencode({'filename': item['filename'], 'subfolder': item.get('subfolder', ''),
                                    'type': item.get('type', 'output')})
        return {'path': str(COMFY_ROOT / 'output' / rel), 'url': f'{COMFY}/view?{q}'}
    raise RuntimeError('enhance finished but produced no video')


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('take', help='FreeVideo run dir or its video.mp4')
    p.add_argument('--resolution', type=int, default=DEFAULTS['resolution'], help='Output short side (default 1080)')
    p.add_argument('--fps-multiplier', type=int, default=DEFAULTS['multiplier'], help='RIFE multiplier (1 = off)')
    p.add_argument('--crf', type=int, default=DEFAULTS['crf'])
    p.add_argument('--batch-size', type=int, default=DEFAULTS['batch_size'], help='SeedVR2 frames per batch (4n+1)')
    a = p.parse_args()
    start = time.time()
    workdir = None
    try:
        pid, run, workdir = submit(a.take, resolution=a.resolution, multiplier=a.fps_multiplier, crf=a.crf,
                                   batch_size=a.batch_size)
        print(f'enhance: queued {pid} for {run.name}')
        while (r := result(pid)) is None:
            time.sleep(5)
        print(f'enhance: {r["path"]}  ({time.time() - start:.0f}s)')
    finally:
        remove_workdir(workdir)         # the lossless raw mkv is only needed while ComfyUI reads it


if __name__ == '__main__':
    main()
