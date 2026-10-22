#!/usr/bin/env python3
"""Deterministic image fixtures for the portable eval cases (eval/cases.json, gen-* ids).

Stdlib + ffmpeg only. Every fixture is one solid background, flat coloured drawboxes and one
drawtext label, rendered to a 640x480 PNG. There is no randomness anywhere in the pipeline
(fixed size, fixed colours, fixed shapes), so the same ffmpeg build re-renders byte-identical
files. The eval cases ground on the label *text* ('TEAM PIZZA', 'CAFE 42', 'RESTON RANCH'),
never on pixels, so picking a different font fallback changes the bytes but not the text and
not what a case asks the model to mention.

  python3 eval/make_fixtures.py                     # -> eval/fixtures/generated/
  python3 eval/make_fixtures.py --out DIR [--only team-pizza.png,cafe-mug.png] [--quiet]
"""
import argparse, hashlib, shutil, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT_DEFAULT = HERE / 'fixtures' / 'generated'
SIZE = '640x480'
FONT_SIZE = 56

# label text, background, flat shapes (drawbox arguments). Keep ids/names in step with the
# fixtures map in eval/cases.json (gen-* entries).
FIXTURES = {
    'team-pizza.png': dict(bg='0xCC2222', text='TEAM PIZZA',
                           shapes=['x=64:y=288:w=176:h=144:color=0x2244CC:t=fill',
                                   'x=400:y=304:w=128:h=128:color=0xE8C233:t=fill']),
    'cafe-mug.png': dict(bg='0xF2E6D8', text='CAFE 42',
                         shapes=['x=224:y=240:w=192:h=176:color=0x6B4226:t=fill',
                                 'x=416:y=288:w=56:h=88:color=0x6B4226:t=fill']),
    'reston-ranch.png': dict(bg='0x9CC4E4', text='RESTON RANCH',
                             shapes=['x=0:y=336:w=640:h=144:color=0x4C7A31:t=fill',
                                     'x=120:y=208:w=224:h=128:color=0x8A3324:t=fill']),
}

# tried in order; falling through changes the bytes only, never the rendered text
FONT_CANDIDATES = ['/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                   '/usr/share/fonts/dejavu/DejaVuSans.ttf',
                   '/usr/share/fonts/TTF/DejaVuSans.ttf']


def _filters(spec, fontfile=None):
    parts = [f"color=c={spec['bg']}:s={SIZE}:d=1"]
    parts += ['drawbox=' + s for s in spec['shapes']]
    dt = (f"drawtext=text='{spec['text']}':fontcolor=white:fontsize={FONT_SIZE}"
          ':x=(w-text_w)/2:y=(h-text_h)/2')
    if fontfile:
        dt = dt.replace('drawtext=', 'drawtext=fontfile=' + fontfile + ':', 1)
    parts.append(dt)
    return ','.join(parts)


def render(name, dest, ffmpeg='ffmpeg'):
    """Render one fixture; return its path. RuntimeError names the last ffmpeg attempt that
    failed (missing binary, no usable font)."""
    spec = FIXTURES[name]
    if not shutil.which(ffmpeg):
        raise RuntimeError(f'{ffmpeg} not found on PATH')
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    fonts = [f for f in FONT_CANDIDATES if Path(f).is_file()] + [None]   # None = fontconfig default
    errors = []
    for font in fonts:
        out = dest / name
        cmd = [ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i', _filters(spec, font),
               '-frames:v', '1', str(out)]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            return out
        errors.append(f'{"font=" + font if font else "default font"}: {res.stderr.strip()}')
    raise RuntimeError('ffmpeg drawtext failed: ' + ' | '.join(errors))


def ensure(names, dest, ffmpeg='ffmpeg'):
    """Render every missing fixture in names; return {name: path or None} (None = failed)."""
    out = {}
    for name in names:
        target = Path(dest) / name
        out[name] = target if target.is_file() else (render(name, dest, ffmpeg)
                                                     if name in FIXTURES else None)
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--out', default=str(OUT_DEFAULT), help='output dir (default eval/fixtures/generated)')
    p.add_argument('--only', help='comma list of fixture file names (default: all)')
    p.add_argument('--rebuild', action='store_true', help='re-render even if the file exists')
    p.add_argument('--quiet', action='store_true')
    a = p.parse_args()
    names = a.only.split(',') if a.only else sorted(FIXTURES)
    unknown = [n for n in names if n not in FIXTURES]
    if unknown:
        p.error(f'unknown fixture(s): {", ".join(unknown)} (have: {", ".join(sorted(FIXTURES))})')
    for name in names:
        try:
            if a.rebuild:
                (Path(a.out) / name).unlink(missing_ok=True)
            path = ensure([name], a.out)[name]
            if not a.quiet:
                print(f'{name}  {path}  sha256={hashlib.sha256(path.read_bytes()).hexdigest()[:16]}')
        except RuntimeError as e:
            print(f'{name}: {e}', file=sys.stderr)
            return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
