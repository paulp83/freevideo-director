#!/usr/bin/env python3
"""Generic H3 output-format checks shared by eval.py and the director retry loop."""
import re

BASE = ['integrated_multimodal_description', 'overall_soundscape', 'non_diegetic_music']
REF = ['subject_definitions', 'summary', 'retention_analysis', 'detailed_description',
       'overall_soundscape', 'non_diegetic_music']
I2VA_LINE = 'For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.'


def failures(mode, text, seconds, n_images):
    """Names of the generic format checks this text fails; empty list means all pass."""
    fields = REF if mode == 'ref2va' else BASE
    pos = [re.search(rf'(?m)^{f}:', text) for f in fields]
    bad = []
    if not (all(pos) and [p.start() for p in pos] == sorted(p.start() for p in pos)):
        bad.append('fields_in_order')
    if mode == 'i2va' and text.splitlines()[0].strip() != I2VA_LINE:
        bad.append('instruction_line')
    stamps = [int(m) * 60 + float(s) for m, s in re.findall(r'At (\d\d):(\d\d(?:\.\d+)?)', text)]
    shots = re.findall(r'\[Shot (\d+)\]', text)
    if not (bool(shots) and shots[0] == '1'):
        bad.append('has_shots')
    if not all(t < seconds for t in stamps):
        bad.append('timestamps_in_range')
    labels = {int(n) for n in re.findall(r'<Picture (\d+)>', text)}
    if not (labels <= set(range(1, n_images + 1)) and (not n_images or bool(labels))):
        bad.append('labels_valid')
    if re.search(r'(?m)^(#|```|\*\*)', text):
        bad.append('no_markdown')
    return bad
