"""Learned job durations for the queue page's estimates: the last 20 per job kind, the median is the estimate. Stdlib only.

stats.json lives next to this file (user data, git-ignored). Everything here is best effort: a missing, corrupt or
hand-edited file counts as empty, and nothing raises, so statistics can never break a render.
"""
import json, math, statistics, threading, uuid
from pathlib import Path

FILE = Path(__file__).resolve().parent / 'stats.json'
KEEP = 20
LOCK = threading.Lock()


def _read():
    try:
        data = json.loads(Path(FILE).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _numbers(values):
    """The usable durations in a stored list: finite positive numbers (bools and strings are skipped)."""
    if not isinstance(values, list):
        return []
    return [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0]


def record(kind, seconds):
    """Remember that a job of `kind` took `seconds`; keeps the last KEEP per kind. Ignores junk, never raises."""
    if not isinstance(kind, str) or not kind or isinstance(seconds, bool) or not isinstance(seconds, (int, float)) \
            or not math.isfinite(seconds) or seconds <= 0:
        return
    with LOCK:
        tmp = None
        try:
            data = _read()
            data[kind] = (_numbers(data.get(kind)) + [seconds])[-KEEP:]
            tmp = Path(FILE).with_name(f'.{Path(FILE).name}.{uuid.uuid4().hex}.tmp')
            tmp.write_text(json.dumps(data))
            tmp.replace(FILE)
        except Exception:
            if tmp is not None:
                tmp.unlink(missing_ok=True)


def estimate(kind):
    """Median duration (float) of the recorded jobs of `kind`, or None when there are none."""
    if not isinstance(kind, str):
        return None
    numbers = _numbers(_read().get(kind))
    return float(statistics.median(numbers)) if numbers else None
