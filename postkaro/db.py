"""The queue and history (SQLite, one file in the data folder). No logins in here.

A batch is one ClipKaro video (or one post.json). Each clip in it has one post per platform. A post
moves through these states:

    draft       on the Post page, not scheduled yet
    scheduled   waiting for its time (or for the moment it may be uploaded)
    uploading   the video is being sent
    processing  the platform is processing the video
    waiting     uploaded; goes live at its time (YouTube and Facebook by themselves, Instagram by PostKaro)
    publishing  PostKaro is pressing "publish" (Instagram)
    live        posted, link known
    failed      something went wrong; the reason is in `error`, the page offers Retry
    missed      Instagram: the laptop was off at posting time; the page asks what to do
    cancelled   removed by the user
"""
import json
import sqlite3
from datetime import datetime, timezone

from . import paths

ACTIVE = ('scheduled', 'uploading', 'processing', 'waiting', 'publishing')
DONE = ('live', 'failed', 'cancelled')

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY, kit TEXT UNIQUE, project TEXT, title TEXT, channel TEXT, channel_url TEXT,
    source_url TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY, batch_id INTEGER, no INTEGER, file TEXT, duration REAL, size INTEGER,
    score REAL, title TEXT, caption TEXT, note TEXT, deep INTEGER DEFAULT 0, removed INTEGER DEFAULT 0,
    UNIQUE (batch_id, no));
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY, clip_id INTEGER, platform TEXT, enabled INTEGER DEFAULT 1,
    title TEXT DEFAULT '', body TEXT DEFAULT '', when_utc TEXT, state TEXT DEFAULT 'draft', account TEXT,
    remote_id TEXT, remote TEXT DEFAULT '{}', url TEXT, error TEXT, attempts INTEGER DEFAULT 0,
    next_try TEXT, prepared_at TEXT, posted_at TEXT, updated TEXT,
    UNIQUE (clip_id, platform));
CREATE TABLE IF NOT EXISTS accounts (
    platform TEXT, account_id TEXT, name TEXT, extra TEXT DEFAULT '{}', connected TEXT, expires TEXT,
    checked TEXT, status TEXT DEFAULT 'ok', problem TEXT, active INTEGER DEFAULT 1,
    PRIMARY KEY (platform, account_id));
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, post_id INTEGER, at TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS video_tasks (
    id INTEGER PRIMARY KEY,
    task_id TEXT UNIQUE,
    source_type TEXT DEFAULT 'topic',
    topic TEXT DEFAULT '',
    url TEXT DEFAULT '',
    video_id TEXT DEFAULT '',
    title TEXT DEFAULT '',
    thumbnail TEXT DEFAULT '',
    channel TEXT DEFAULT '',
    views INTEGER DEFAULT 0,
    duration REAL DEFAULT 0,
    reason TEXT DEFAULT '',
    score REAL DEFAULT 0,
    stage TEXT DEFAULT 'queued',
    progress REAL DEFAULT 0.0,
    clips_count INTEGER DEFAULT 0,
    batch_id INTEGER,
    error TEXT,
    attempts INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 5,
    next_try TEXT,
    checkpoint TEXT DEFAULT '{}',
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_stage ON video_tasks(stage);

"""


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ') if dt else None


def parse(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace('Z', '+00:00')).astimezone(timezone.utc)


# columns added later (v0.3): the clip's custom thumbnail, and a post's non-blocking note
ADDED = {
    'clips': [('cover', 'TEXT'), ('cover_kind', 'TEXT'), ('cover_ms', 'INTEGER'), ('cover_v', 'INTEGER')],
    'posts': [('note', 'TEXT')],
    'video_tasks': [('attempts', 'INTEGER DEFAULT 0'), ('max_retries', 'INTEGER DEFAULT 5'), ('next_try', 'TEXT')],
}
_ready = set()


def connect():
    """A new connection (the caller closes it). The tables are made once per database file."""
    path = paths.db_path()
    fresh = str(path) not in _ready or not path.exists()
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    if fresh:
        con.execute('PRAGMA journal_mode=WAL')
        con.executescript(SCHEMA)
        for table, cols in ADDED.items():               # columns added after a release: add them to old files
            have = {r[1] for r in con.execute(f'PRAGMA table_info({table})')}
            for name, kind in cols:
                if name not in have:
                    con.execute(f'ALTER TABLE {table} ADD COLUMN {name} {kind}')
        # since 0.4.5 a post records the account it was uploaded to; posts uploaded before that are on the
        # default account of the time, so pin them to it (a later change of default must not move them)
        con.execute("UPDATE posts SET account=(SELECT a.account_id FROM accounts a WHERE a.platform=posts.platform "
                    "AND a.active=1) WHERE account IS NULL AND remote_id IS NOT NULL "
                    "AND state IN ('processing', 'waiting', 'publishing')")
        con.commit()
        _ready.add(str(path))
    return con


def rows(sql, args=()):
    con = connect()
    try:
        with con:
            return [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def one(sql, args=()):
    r = rows(sql, args)
    return r[0] if r else None


def run(sql, args=()):
    con = connect()
    try:
        with con:
            return con.execute(sql, args).lastrowid
    finally:
        con.close()


def post(pid):
    p = one('SELECT * FROM posts WHERE id=?', (pid,))
    if p:
        p['remote'] = json.loads(p.get('remote') or '{}')
    return p


def update_post(pid, **f):
    if 'remote' in f and not isinstance(f['remote'], str):
        f['remote'] = json.dumps(f['remote'])
    f['updated'] = iso(now())
    cols = ', '.join(f'{k}=?' for k in f)
    run(f'UPDATE posts SET {cols} WHERE id=?', (*f.values(), pid))


def log(pid, text):
    run('INSERT INTO events (post_id, at, text) VALUES (?, ?, ?)', (pid, iso(now()), text))


def post_full(pid):
    """A post with its clip and batch fields (what a platform needs to upload it)."""
    p = post(pid)
    if not p:
        return None
    c = one('SELECT * FROM clips WHERE id=?', (p['clip_id'],))
    b = one('SELECT * FROM batches WHERE id=?', (c['batch_id'],))
    p['clip'], p['batch'] = c, b
    return p


def accounts(platform=None):
    sql, args = 'SELECT * FROM accounts', ()
    if platform:
        sql, args = sql + ' WHERE platform=?', (platform,)
    out = rows(sql + ' ORDER BY platform, name', args)
    for a in out:
        a['extra'] = json.loads(a.get('extra') or '{}')
    return out


def active_account(platform):
    for a in accounts(platform):
        if a['active']:
            return a
    return None


def post_account(p):
    """The account a post goes to: the one picked for it on the page, or the one it was uploaded to (the
    worker records it when the upload starts), else the platform's default. None when the picked account
    was disconnected: the post must not quietly go to another channel."""
    if p.get('account'):
        return next((a for a in accounts(p['platform']) if a['account_id'] == p['account']), None)
    return active_account(p['platform'])


def save_account(platform, account_id, name, extra=None, expires=None, active=True):
    if active:
        run('UPDATE accounts SET active=0 WHERE platform=?', (platform,))
    run('INSERT INTO accounts (platform, account_id, name, extra, connected, expires, checked, status, problem, '
        'active) VALUES (?, ?, ?, ?, ?, ?, ?, "ok", NULL, ?) ON CONFLICT (platform, account_id) DO UPDATE SET '
        'name=excluded.name, extra=excluded.extra, connected=excluded.connected, expires=excluded.expires, '
        'checked=excluded.checked, status="ok", problem=NULL, active=excluded.active',
        (platform, account_id, name, json.dumps(extra or {}), iso(now()), expires, iso(now()), int(active)))


# ---------- Video Tasks & Pipeline Checkpoints ----------

def add_video_task(t):
    """Inserts a new video task to be processed by the local hardware agent."""
    import uuid
    tid = t.get('task_id') or str(uuid.uuid4())
    cp = json.dumps(t.get('checkpoint') or {})
    rid = run(
        "INSERT INTO video_tasks (task_id, source_type, topic, url, video_id, title, thumbnail, channel, "
        "views, duration, reason, score, stage, progress, clips_count, batch_id, error, checkpoint, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (tid, t.get('source_type', 'topic'), t.get('topic', ''), t.get('url', ''), t.get('video_id', ''),
         t.get('title', ''), t.get('thumbnail', ''), t.get('channel', ''), int(t.get('views', 0)),
         float(t.get('duration', 0.0)), t.get('reason', ''), float(t.get('score', 0.0)),
         t.get('stage', 'queued'), float(t.get('progress', 0.0)), int(t.get('clips_count', 0)),
         t.get('batch_id'), t.get('error'), cp, iso(now()), iso(now()))
    )
    return {'id': rid, 'task_id': tid}


def update_video_task(tid_or_id, **fields):
    """Updates fields of a task, ensuring checkpoint is properly serialized and updated_at is refreshed."""
    if 'checkpoint' in fields and not isinstance(fields['checkpoint'], str):
        fields['checkpoint'] = json.dumps(fields['checkpoint'])
    fields['updated_at'] = iso(now())
    cols = ', '.join(f'{k}=?' for k in fields)
    if isinstance(tid_or_id, int) or str(tid_or_id).isdigit():
        run(f'UPDATE video_tasks SET {cols} WHERE id=?', (*fields.values(), int(tid_or_id)))
    else:
        run(f'UPDATE video_tasks SET {cols} WHERE task_id=?', (*fields.values(), str(tid_or_id)))


def get_video_task(tid_or_id):
    if isinstance(tid_or_id, int) or str(tid_or_id).isdigit():
        row = one('SELECT * FROM video_tasks WHERE id=?', (int(tid_or_id),))
    else:
        row = one('SELECT * FROM video_tasks WHERE task_id=?', (str(tid_or_id),))
    if row:
        row = dict(row)
        try:
            row['checkpoint'] = json.loads(row.get('checkpoint') or '{}')
        except Exception:
            row['checkpoint'] = {}
    return row


def list_video_tasks(stage=None, topic=None, limit=100):
    sql = 'SELECT * FROM video_tasks'
    clauses = []
    args = []
    if stage:
        clauses.append('stage=?')
        args.append(stage)
    if topic:
        clauses.append('topic LIKE ?')
        args.append(f'%{topic}%')
    if clauses:
        sql += ' WHERE ' + ' AND '.join(clauses)
    sql += ' ORDER BY id DESC LIMIT ?'
    args.append(limit)
    out = rows(sql, tuple(args))
    for r in out:
        try:
            r['checkpoint'] = json.loads(r.get('checkpoint') or '{}')
        except Exception:
            r['checkpoint'] = {}
    return out


def get_active_video_task():
    """Returns the task currently being processed by the local hardware agent, if any."""
    row = one("SELECT * FROM video_tasks WHERE stage IN ('downloading', 'transcribing', 'picking_moments', 'rendering') ORDER BY id ASC")
    if row:
        row = dict(row)
        try:
            row['checkpoint'] = json.loads(row.get('checkpoint') or '{}')
        except Exception:
            row['checkpoint'] = {}
    return row


def get_resumable_tasks():
    """Returns all unfinished tasks that need to be processed or resumed after reboot."""
    cur_iso = iso(now())
    out = rows(
        "SELECT * FROM video_tasks "
        "WHERE stage NOT IN ('completed', 'failed') "
        "  AND (next_try IS NULL OR next_try <= ?) "
        "ORDER BY id ASC",
        (cur_iso,)
    )
    for r in out:
        try:
            r['checkpoint'] = json.loads(r.get('checkpoint') or '{}')
        except Exception:
            r['checkpoint'] = {}
    return out
