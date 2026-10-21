"""Durable job bookkeeping: one SQLite file (jobs.db, beside stats.json) that survives a restart.

Stdlib only. The database is bookkeeping, never truth: takes stay in the project JSON and the
immutable request/result records stay in projects/<slug>/jobs/; this table only remembers which
job exists, in what state, against which backend, with which ComfyUI prompt ids and which record
files point at its request and result. That is what startup reconciliation needs to reattach to
live work, record late results idempotently, and leave the crash window (a prompt id recorded but
no answer to the /prompt POST yet) visible instead of silently resubmitting.

State column = the in-memory state machine's states PLUS one persisted-only value, 'submitting':
the prompt id is durably recorded but the POST /prompt has not been answered. Rows in that state
are the crash window; they are never pruned and never resubmitted automatically. Two recovery
markers ride on every row: `retry_allowed` (an unconfirmed submit the user may retry by hand)
and `recovering_at` (the epoch a recovery pass last found ComfyUI unreachable for this job;
it clears when a pass resolves it). Neither changes the state column.
A hand retry also links the two rows: the new job's `retry_of` names the unknown job it
replays, and that job's `retried_as` names the one job that replayed it -- one retry per
unknown job, idempotent.

Every write is its own transaction on a short connection, serialised by a process lock. The
functions here raise JobDbError on a real database failure; the caller decides who may log and
continue -- everything in the live job path does, EXCEPT the pre-submit record_submit, which the
web layer treats as "no durable record, no submit".
"""
import json, os, sqlite3, threading, time
from pathlib import Path

FILE = Path(os.environ.get('JOBS_DB') or Path(__file__).resolve().parent / 'jobs.db')
VERSION = 3
SUBMITTING = 'submitting'                     # persisted-only (see docstring)
TERMINAL = ('finished', 'failed', 'cancelled', 'unknown')
KEEP_TERMINAL = 500                           # retention: the last 500 terminal jobs...
KEEP_DAYS = 30                                # ...or everything younger than 30 days, whichever keeps more
LOCK = threading.Lock()


class JobDbError(Exception):
    """The durable store could not be read or written."""


def _connect():
    conn = sqlite3.connect(str(FILE), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _migrate(conn):
    """Walk the schema up to VERSION (PRAGMA user_version). Version 1: the jobs table.
    Version 2: the recovery markers. Version 3: the hand-retry link columns."""
    version = conn.execute('PRAGMA user_version').fetchone()[0]
    if version == 0:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id               TEXT PRIMARY KEY,
                kind             TEXT NOT NULL,
                project          TEXT,
                scene            TEXT,
                state            TEXT NOT NULL,
                created          REAL,
                started          REAL,
                ended            REAL,
                backend          TEXT,
                prompt_ids       TEXT NOT NULL DEFAULT '[]',
                correlation      TEXT,
                snapshot_path    TEXT,
                result_path      TEXT,
                take_id          TEXT,
                run              TEXT,
                error            TEXT,
                resolved_by      TEXT,
                resolved_verdict TEXT,
                resolved_at      REAL,
                workdir          TEXT,
                holds            TEXT NOT NULL DEFAULT '[]'
            );
            CREATE INDEX IF NOT EXISTS jobs_state_ended ON jobs (state, ended);
            CREATE INDEX IF NOT EXISTS jobs_project     ON jobs (project);
        """)
        conn.commit()
    if version < 2:
        conn.execute('ALTER TABLE jobs ADD COLUMN retry_allowed INTEGER NOT NULL DEFAULT 0')
        conn.execute('ALTER TABLE jobs ADD COLUMN recovering_at REAL')
    if version < 3:
        conn.execute('ALTER TABLE jobs ADD COLUMN retry_of TEXT')
        conn.execute('ALTER TABLE jobs ADD COLUMN retried_as TEXT')
    conn.execute(f'PRAGMA user_version = {VERSION}')
    conn.commit()


_INITED = None


def init(path=None):
    """Point the store at `path` (or keep FILE) and make sure the schema is there. Cheap to call."""
    global FILE, _INITED
    with LOCK:
        if path is not None:
            FILE = Path(path)
        if _INITED == str(FILE):
            return
        try:
            FILE.parent.mkdir(parents=True, exist_ok=True)
            conn = _connect()
        except (sqlite3.Error, OSError) as e:
            raise JobDbError(f'cannot open the job store {FILE}: {e}')
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            _migrate(conn)
        except sqlite3.Error as e:
            raise JobDbError(f'cannot set up the job store {FILE}: {e}')
        finally:
            conn.close()
        _INITED = str(FILE)


def _row(job, state=None, backend=None, take_id=None, snapshot_path=None, result_path=None,
         prompt_ids=None, correlation=None, retry_allowed=None, recovering_at=None):
    """A row dict for an in-memory web.py job dict; the job dict stays the live view."""
    resolved = job.get('resolved') or {}
    take = job.get('take') or {}
    return {
        'id': job['id'], 'kind': job['kind'], 'project': job.get('project'), 'scene': job.get('scene'),
        'state': state or job['state'],
        'created': job.get('created'), 'started': job.get('started'), 'ended': job.get('ended'),
        'backend': backend,
        'prompt_ids': json.dumps(list(prompt_ids if prompt_ids is not None
                                     else (job.get('prompt_ids') or []))),
        'correlation': correlation if correlation is not None else job.get('correlation'),
        'snapshot_path': snapshot_path, 'result_path': result_path,
        'take_id': take_id or job.get('take_id'), 'run': take.get('run'), 'error': job.get('error'),
        'resolved_by': resolved.get('by'), 'resolved_verdict': resolved.get('verdict'),
        'resolved_at': resolved.get('at'),
        'workdir': job.get('workdir'),
        'holds': json.dumps([list(k) for k in (job.get('holds') or [])]),
        'retry_allowed': 1 if (retry_allowed if retry_allowed is not None
                               else job.get('retry_allowed')) else 0,
        'recovering_at': recovering_at if recovering_at is not None else job.get('recovering_at'),
        'retry_of': job.get('retry_of'), 'retried_as': job.get('retried_as'),
    }


COLUMNS = ('id', 'kind', 'project', 'scene', 'state', 'created', 'started', 'ended', 'backend',
           'prompt_ids', 'correlation', 'snapshot_path', 'result_path', 'take_id', 'run', 'error',
           'resolved_by', 'resolved_verdict', 'resolved_at', 'workdir', 'holds',
           'retry_allowed', 'recovering_at', 'retry_of', 'retried_as')


def _put(conn, r):
    marks = ', '.join('?' for _ in COLUMNS)
    updates = ', '.join(f'{c} = excluded.{c}' for c in COLUMNS if c != 'id')
    conn.execute(f'INSERT INTO jobs ({", ".join(COLUMNS)}) VALUES ({marks}) '
                 f'ON CONFLICT(id) DO UPDATE SET {updates}', [r[c] for c in COLUMNS])


def save(job, state=None, backend=None, take_id=None, snapshot_path=None, result_path=None,
         prompt_ids=None, correlation=None, retry_allowed=None, recovering_at=None):
    """Upsert the whole row from the live job dict. Raises JobDbError on failure; the live job
    path in web.py logs those and continues -- only submit_tracked refuses to go on."""
    init()
    r = _row(job, state=state, backend=backend, take_id=take_id,
             snapshot_path=snapshot_path, result_path=result_path,
             prompt_ids=prompt_ids, correlation=correlation,
             retry_allowed=retry_allowed, recovering_at=recovering_at)
    with LOCK:
        conn = _connect()
        try:
            _put(conn, r)
            conn.commit()
        except sqlite3.Error as e:
            raise JobDbError(str(e))
        finally:
            conn.close()


def record_submit(job, prompt_id, backend=None, snapshot_path=None, result_path=None):
    """The pre-submit write: durably record `prompt_id` and mark the row 'submitting' BEFORE the
    POST /prompt leaves. This one failure must stop the submit (JobDbError propagates): a render
    we cannot track by id is not allowed to happen."""
    ids = list(job.get('prompt_ids') or [])
    if prompt_id not in ids:
        ids.append(prompt_id)
    save(job, state=SUBMITTING, backend=backend, snapshot_path=snapshot_path,
         result_path=result_path, prompt_ids=ids, correlation='client-supplied')


def forget(job_id):
    """Drop a job that was never accepted anywhere (abandon at the door): nothing to reconcile."""
    init()
    with LOCK:
        conn = _connect()
        try:
            conn.execute('DELETE FROM jobs WHERE id = ?', (job_id,))
            conn.commit()
        except sqlite3.Error as e:
            raise JobDbError(str(e))
        finally:
            conn.close()


def mark_recovering(job_id, at=None):
    """A recovery pass could not reach ComfyUI for this job: timestamp the row WITHOUT
    touching its state -- the job is neither resolved nor abandoned."""
    init()
    with LOCK:
        conn = _connect()
        try:
            conn.execute('UPDATE jobs SET recovering_at = ? WHERE id = ?',
                         (at if at is not None else time.time(), job_id))
            conn.commit()
        except sqlite3.Error as e:
            raise JobDbError(str(e))
        finally:
            conn.close()


def clear_recovering(job_id):
    """A pass reached ComfyUI and is handling the job again: drop the marker."""
    init()
    with LOCK:
        conn = _connect()
        try:
            conn.execute('UPDATE jobs SET recovering_at = NULL WHERE id = ?', (job_id,))
            conn.commit()
        except sqlite3.Error as e:
            raise JobDbError(str(e))
        finally:
            conn.close()


def _decode(row):
    out = dict(row)
    for key in ('prompt_ids', 'holds'):
        try:
            out[key] = json.loads(out[key] or '[]')
        except ValueError:
            out[key] = []
    out['resolved'] = ({'by': out['resolved_by'], 'verdict': out['resolved_verdict'], 'at': out['resolved_at']}
                       if out['resolved_verdict'] is not None else None)
    out['retry_allowed'] = bool(out['retry_allowed'])
    return out


def get(job_id):
    """The row for one job id as a dict, or None."""
    init()
    with LOCK:
        conn = _connect()
        try:
            row = conn.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
        except sqlite3.Error as e:
            raise JobDbError(str(e))
        finally:
            conn.close()
    return _decode(row) if row else None


def find_retry(old_id):
    """The one job (if any) already recorded as the retry of `old_id`: the idempotency door
    for a second retry click, working even after the old row left memory."""
    init()
    with LOCK:
        conn = _connect()
        try:
            row = conn.execute('SELECT * FROM jobs WHERE retry_of = ? ORDER BY created LIMIT 1',
                               (old_id,)).fetchone()
        except sqlite3.Error as e:
            raise JobDbError(str(e))
        finally:
            conn.close()
    return _decode(row) if row else None


def list_jobs(project=None, states=None, limit=None, since=None):
    """Persisted jobs, newest ended first. `states` restricts to a set of state values, `since`
    to rows with ended at or after that epoch, `project` to one film slug."""
    init()
    where, args = [], []
    if project is not None:
        where.append('project = ?')
        args.append(project)
    if states:
        where.append('state IN (%s)' % ', '.join('?' for _ in states))
        args.extend(states)
    if since is not None:
        where.append('COALESCE(ended, created) >= ?')
        args.append(since)
    sql = 'SELECT * FROM jobs'
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY COALESCE(ended, created) DESC'
    if limit is not None:
        sql += ' LIMIT ?'
        args.append(int(limit))
    with LOCK:
        conn = _connect()
        try:
            rows = conn.execute(sql, args).fetchall()
        except sqlite3.Error as e:
            raise JobDbError(str(e))
        finally:
            conn.close()
    return [_decode(r) for r in rows]


def prune(keep=KEEP_TERMINAL, days=KEEP_DAYS):
    """Startup retention: keep every terminal job from the last `days` days AND the last `keep`
    terminal jobs (whichever set is larger); non-terminal rows (live states and the crash-window
    'submitting') are never pruned. Returns the number of rows deleted."""
    init()
    cutoff = time.time() - days * 86400
    term = TERMINAL
    with LOCK:
        conn = _connect()
        try:
            cur = conn.execute(
                f"""DELETE FROM jobs
                    WHERE state IN ({', '.join('?' for _ in term)})
                      AND (ended IS NULL OR ended < ?)
                      AND id NOT IN (SELECT id FROM jobs
                                     WHERE state IN ({', '.join('?' for _ in term)})
                                     ORDER BY COALESCE(ended, created) DESC LIMIT ?)""",
                [*term, cutoff, *term, int(keep)])
            conn.commit()
            return cur.rowcount
        except sqlite3.Error as e:
            raise JobDbError(str(e))
        finally:
            conn.close()
