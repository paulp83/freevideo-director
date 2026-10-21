#!/usr/bin/env python3
"""Regenerate tests/golden/legacy_m4_*.json from a checked-out M4 worktree.

    git worktree add --detach <dir> a0928c4
    python3 tests/golden/capture_legacy_m4.py --tree <dir>
    git worktree remove <dir>

The M4 tree's own project/enhance/web modules run against the shared fixture
(tests/fixtures/legacy_m4.py); every value they produce is written as canonical
JSON (sorted keys, {{ROOT}}-normalised paths, cut filenames as film-TIMESTAMP).
The acceptance suite then demands the same bytes from HEAD. Clip durations come
from Matroska/pcm clips, so they are exact and identical across ffmpeg builds.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent


def canon(value, root):
    text = json.dumps(value, sort_keys=True, indent=1)
    text = text.replace(str(root), '{{ROOT}}')
    text = re.sub(r'(film|animatic)-\d{8}-\d{6}', r'\1-TIMESTAMP', text)
    return text + '\n'


class FakePopen:
    """Stands in for the encode: assemble() records its command line and finishes at once."""

    calls = []

    def __init__(self, cmd):
        FakePopen.calls.append(list(cmd))
        self.returncode = 0

    def poll(self):
        return 0

    def communicate(self):
        return '', ''


def record_encode(cmd, **kwargs):
    """Drop-in for subprocess.Popen: the assemble encode (an ffmpeg command with
    -filter_complex and piped stdout) is recorded and short-circuited; ffprobe calls
    from subprocess.run -- which rewrites capture_output to PIPE itself -- go through."""
    if kwargs.get('stdout') is subprocess.PIPE and '-filter_complex' in cmd:
        return FakePopen(cmd)
    return _real_popen(cmd, **kwargs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tree', required=True, help='worktree checked out at a0928c4')
    args = ap.parse_args()
    tree = Path(args.tree).resolve()
    if not (tree / 'project.py').is_file():
        sys.exit(f'not a repo checkout: {tree}')

    root = Path(tempfile.mkdtemp(prefix='golden-m4-'))
    os.environ['PROJECTS_ROOT'] = str(root)
    sys.path.insert(0, str(tree))
    sys.path.insert(0, str(REPO / 'tests' / 'fixtures'))
    sys.path.insert(1, str(tree))
    import legacy_m4
    import project as projects
    import enhance
    import web
    assert projects.ROOT == root, f'fixture root is {projects.ROOT}, expected {root}'

    legacy_m4.install(root)
    p = projects.load(legacy_m4.SLUG)

    global _real_popen
    FakePopen.calls.clear()
    _real_popen = subprocess.Popen
    projects.subprocess.Popen = record_encode
    projects.assemble(p, 'film')
    (cmd,) = FakePopen.calls

    goldens = {
        'legacy_m4_project.json': canon(legacy_m4.project_dict(root), root),
        'legacy_m4_cut_plan_film.json': canon(projects.cut_plan(p, projects.current_used_takes(p), 'film'), root),
        'legacy_m4_cut_plan_animatic.json': canon(projects.cut_plan(p, projects.current_used_takes(p), 'animatic'), root),
        'legacy_m4_enhance_graph.json': canon(enhance.graph('take.mkv', 'golden-run'), root),
        'legacy_m4_job_finishing.json': canon(web.job_finishing(), root),
        'legacy_m4_assemble_cmd.json': canon(cmd, root),
    }
    for name, text in goldens.items():
        (HERE / name).write_text(text)
        print(f'wrote {name} ({len(text)} bytes)')


if __name__ == '__main__':
    main()
