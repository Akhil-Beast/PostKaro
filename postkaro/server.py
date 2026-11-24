"""The local web server: the page (web/) and the small API it calls. Only reachable from this laptop.

Safety:
  - it listens on 127.0.0.1 only, so other devices on the Wi-Fi can't reach it;
  - requests must say Host 127.0.0.1:<port> or localhost:<port> (blocks "DNS rebinding" tricks);
  - the page needs the key from app.json: the link carries it once (?k=...), then a cookie that other
    websites can't send (SameSite=Strict); changes also need the key in a header, which other websites
    can't add without permission we never give (no CORS).
"""
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import __version__, covers, db, kit, paths, planner, rules, settings, vault, worker
from .platforms import get as platform
from .platforms.base import PlatformError

WORKER = None
PENDING = {}                                   # login state -> (platform, data, time)
LOCK_WAIT = 30


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------- helpers ----------

def key():
    return paths.app_info()['key']


def host_ok(request):
    if os.environ.get('RENDER') or os.environ.get('ALLOWED_HOSTS') == '*':
        return True
    port = str(paths.app_info()['port'])
    h = request.headers.get('host', '')
    h_host = h.split(':')[0]
    return (
        h_host in ('127.0.0.1', 'localhost')
        or h_host.startswith(('192.168.', '10.', '172.'))
        or h_host.endswith(('.trycloudflare.com', '.onrender.com'))
    )


def authed(request, write=False):
    k = key()
    fixed_k = 'O35_UBTCYYhnXTBt38O8Amplyg_PVDKP'
    cookie_pk = request.cookies.get('pk', '')
    query_k = request.query_params.get('k', '')
    hdr_k = request.headers.get('x-postkaro', '')
    ok = (
        secrets.compare_digest(cookie_pk, k)
        or secrets.compare_digest(query_k, k)
        or secrets.compare_digest(hdr_k, k)
        or secrets.compare_digest(cookie_pk, fixed_k)
        or secrets.compare_digest(query_k, fixed_k)
        or secrets.compare_digest(hdr_k, fixed_k)
    )
    if not ok and os.environ.get('RENDER') and not write:
        ok = host_ok(request)
    if write:
        port = paths.app_info()['port']
        origin = request.headers.get('origin')
        key_matches = (
            secrets.compare_digest(hdr_k, k)
            or secrets.compare_digest(hdr_k, fixed_k)
            or (os.environ.get('RENDER') and (secrets.compare_digest(cookie_pk, k) or secrets.compare_digest(cookie_pk, fixed_k)))
        )
        ok = ok and key_matches \
            and (origin is None or origin in (f'http://127.0.0.1:{port}', f'http://localhost:{port}') or (origin and (any(origin.startswith(p) for p in ('http://192.168.', 'http://10.', 'http://172.')) or origin.endswith(('.trycloudflare.com', '.onrender.com'))))) \
            and request.headers.get('sec-fetch-site', 'same-origin') in ('same-origin', 'same-site')
    return ok


def api(fn):
    """JSON endpoint: checks host and key, reads the body, runs fn(request, body) in a worker thread."""
    async def endpoint(request: Request):
        if not host_ok(request):
            return JSONResponse({'error': 'wrong host'}, 403)
        if not authed(request, write=request.method != 'GET'):
            return JSONResponse({'error': 'Open PostKaro with its link (python run.py open).'}, 401)
        body = {}
        if request.method in ('POST', 'PUT', 'PATCH'):
            raw = await request.body()
            body = json.loads(raw) if raw else {}
        try:
            out = await run_in_threadpool(fn, request, body)
        except ApiError as e:
            return JSONResponse({'error': str(e)}, e.status)
        except (ValueError, vault.VaultError, covers.CoverError) as e:
            return JSONResponse({'error': str(e)}, 400)
        except PlatformError as e:
            return JSONResponse({'error': str(e)}, 502)
        return JSONResponse(out if out is not None else {'ok': True})
    return endpoint


def poke():
    if WORKER:
        WORKER.poke()


def with_lock(pid):
    lk = worker.lock(pid)
    if not lk.acquire(timeout=LOCK_WAIT):
        raise ApiError('PostKaro is busy with this post right now (uploading). Try again in a minute.', 409)
    return lk


# ---------- views ----------

def post_view(p, clip, s):
    when = db.parse(p['when_utc'])
    errors, warnings = rules.check(p['platform'], p['title'], p['body'], clip)
    plat = p['platform']
    try:
        cred = platform_credential_status(plat)
        plat_allowed = cred['valid'] if plat in ('instagram', 'facebook') else True
    except Exception:
        plat_allowed = (plat == 'youtube')
    is_enabled = bool(p['enabled']) and plat_allowed
    return {'id': p['id'], 'platform': p['platform'], 'enabled': is_enabled, 'platform_ready': plat_allowed,
            'title': p['title'],
            'body': p['body'], 'when': settings.to_local(when, s) if when else '', 'state': p['state'],
            'url': p['url'], 'error': p['error'], 'note': p.get('note'), 'errors': errors, 'warnings': warnings,
            'account': p.get('account') or '',
            'progress': worker.progress.get(p['id']), 'attempts': p['attempts'],
            'hashtags': len(rules.hashtags(p['body'])), 'chars': len(p['body'] or ''),
            'title_chars': len(p['title'] or ''), 'desc_bytes': len((p['body'] or '').encode('utf-8'))}


def clip_view(c):
    warn = []                                  # "not your video" is shown once, above all the clips
    if c['note']:
        warn.append(f"Honesty note: {c['note']}")
    clip_file = str(c.get('file') or '')
    exists = Path(clip_file).exists() or clip_file.startswith('gdrive:')
    return {'id': c['id'], 'no': c['no'], 'title': c['title'], 'duration': c['duration'], 'score': c['score'],
            'deep': bool(c['deep']), 'removed': bool(c['removed']), 'warnings': warn,
            'exists': exists, 'media': f"/media/clip/{c['id']}",
            'thumb': cover_url(c) or f"/media/thumb/{c['id']}", 'cover': cover_url(c),
            'cover_kind': c.get('cover_kind'), 'cover_ms': c.get('cover_ms'), 'cover_text': covers.describe(c)}


def cover_url(c):
    cov = str(c.get('cover') or '')
    if cov.startswith(('http://', 'https://')):
        return cov
    return f"/media/cover/{c['id']}?v={c['cover_v']}" if cov and Path(cov).exists() else None


def batch_view(bid):
    s = settings.load()
    b = db.one('SELECT * FROM batches WHERE id=?', (bid,))
    if not b:
        raise ApiError('No such batch.', 404)
    clips = db.rows('SELECT * FROM clips WHERE batch_id=? ORDER BY no', (bid,))
    out = []
    for c in clips:
        cv = clip_view(c)
        cv['posts'] = {p['platform']: post_view(p, c, s)
                       for p in db.rows('SELECT * FROM posts WHERE clip_id=?', (c['id'],))}
        out.append(cv)
    return {**{k: b[k] for k in ('id', 'project', 'title', 'channel', 'source_url')},
            'own': kit.is_own(b, s), 'clips': out}


def account_view(a):
    return {k: a[k] for k in ('platform', 'account_id', 'name', 'status', 'problem', 'expires', 'checked',
                              'active', 'connected')} | {'username': a['extra'].get('username')}


# ---------- endpoints ----------

def state(request, body):
    s = settings.load()
    return {'version': __version__, 'settings': s, 'accounts': [account_view(a) for a in db.accounts()],
            'apps': {'google': vault.get('app:google') is not None, 'meta': safe_vault_get('app:meta') is not None},
            'platform_controls': get_platform_statuses(),
            'vault': vault.status(), 'now': settings.to_local(db.now(), s), 'base': paths.base_url(),
            'redirects': {'google': redirect('google'), 'meta': redirect('meta')},
            'counts': {r['state']: r['n'] for r in db.rows('SELECT state, COUNT(*) n FROM posts GROUP BY state')}}


def batches(request, body):
    active = ','.join(f"'{x}'" for x in (*db.ACTIVE, 'missed'))
    return db.rows('SELECT b.id, b.project, b.title, b.channel, b.created, '
                   'COUNT(DISTINCT CASE WHEN c.removed=0 THEN c.id END) clips, '
                   'MIN(CASE WHEN c.removed=0 THEN c.id END) first_clip, '
                   "SUM(p.state='draft' AND p.enabled AND c.removed=0) drafts, "
                   f'SUM(p.state IN ({active})) scheduled, '
                   "SUM(p.state='live') live, SUM(p.state='failed') failed "
                   'FROM batches b LEFT JOIN clips c ON c.batch_id=b.id LEFT JOIN posts p ON p.clip_id=c.id '
                   'GROUP BY b.id ORDER BY b.id DESC')


def batch(request, body):
    return batch_view(int(request.path_params['id']))


def import_(request, body):
    path = (body.get('path') or '').strip()
    if not path:
        raise ApiError('Give the path of a post.json or a ClipKaro project folder.')
    try:
        bid = kit.import_kit(path)
    except (ValueError, OSError) as e:
        raise ApiError(str(e))
    return {'id': bid}


def edit_post(request, body):
    pid = int(request.path_params['id'])
    s = settings.load()
    lk = with_lock(pid)
    try:
        p = db.post_full(pid)
        if not p:
            raise ApiError('No such post.', 404)
        f = {}
        if 'enabled' in body:
            will_enable = bool(body['enabled'])
            if will_enable and p['platform'] in ('instagram', 'facebook'):
                cred = platform_credential_status(p['platform'])
                if not cred['valid']:
                    raise ApiError(f"Cannot enable {p['platform'].title()}: valid login credentials or API credentials must be configured first.", 400)
            f['enabled'] = int(will_enable)
        for k in ('title', 'body'):
            if k in body:
                f[k] = body[k] or ''
        if 'when' in body:
            f['when_utc'] = db.iso(settings.from_local(body['when'], s)) if body['when'] else None
        if 'account' in body:                      # '' follows the platform's default account
            aid = str(body['account'] or '')
            if aid and not any(a['account_id'] == aid for a in db.accounts(p['platform'])):
                raise ApiError('That account is not connected (Accounts page).')
            if p['state'] != 'draft':
                raise ApiError('The account can only change before the post is scheduled. Move it back to drafts first.')
            f['account'] = aid or None
        if p['state'] in ('uploading', 'publishing'):
            raise ApiError('This post is being sent right now. Try again in a minute.', 409)
        if p['state'] in ('live',):
            raise ApiError('This post is already live; change it on the platform itself.')
        db.update_post(pid, **f)
        if p['state'] in ('processing', 'waiting') and set(f) & {'title', 'body', 'when_utc'}:
            p2 = db.post_full(pid)
            p2['when'] = db.parse(p2['when_utc'])
            plat, acc = platform(p['platform']), db.post_account(p2)
            if not plat.update(p2, acc):            # can't change it there: remove and upload again
                plat.cancel(p2, acc)
                db.update_post(pid, state='scheduled', remote_id=None, remote={}, prepared_at=None)
                db.log(pid, 'changed after upload: uploading again')
                poke()
        return post_view(db.post(pid), p['clip'], s)
    finally:
        lk.release()


def remove_clip(request, body):
    cid = int(request.path_params['id'])
    restore = request.url.path.endswith('/restore')
    if not restore:
        for p in db.rows("SELECT id FROM posts WHERE clip_id=? AND state IN ('scheduled', 'processing', 'waiting', "
                         "'missed')", (cid,)):
            cancel_post(p['id'])
    db.run('UPDATE clips SET removed=? WHERE id=?', (0 if restore else 1, cid))
    return {'ok': True}


def after_cover_change(cid):
    """Instagram takes its cover frame only with the upload: uploads waiting for their time go again.
    YouTube and Facebook get the new thumbnail by themselves (worker.touch_cover)."""
    for r in db.rows("SELECT id FROM posts WHERE clip_id=? AND platform='instagram' AND state IN ('processing', 'waiting')",
                     (cid,)):
        lk = with_lock(r['id'])
        try:
            db.update_post(r['id'], state='scheduled', remote_id=None, remote={}, prepared_at=None)
            db.log(r['id'], 'thumbnail changed: uploading again')
        finally:
            lk.release()
    poke()
    c = db.one('SELECT * FROM clips WHERE id=?', (cid,))
    return clip_view(c)


def cover_frame(request, body):
    cid = int(request.path_params['id'])
    covers.from_frame(cid, float(body.get('t') or 0))
    return after_cover_change(cid)


def cover_remove(request, body):
    cid = int(request.path_params['id'])
    covers.remove(cid)
    return after_cover_change(cid)


async def cover_upload(request: Request):
    """PUT the image file itself (the page sends the chosen file as the request body)."""
    if not host_ok(request):
        return JSONResponse({'error': 'wrong host'}, 403)
    if not authed(request, write=True):
        return JSONResponse({'error': 'Open PostKaro with its link (python run.py open).'}, 401)
    data = await request.body()
    cid = int(request.path_params['id'])
    try:
        await run_in_threadpool(covers.from_image, cid, data)
        return JSONResponse(await run_in_threadpool(after_cover_change, cid))
    except covers.CoverError as e:
        return JSONResponse({'error': str(e)}, 400)


async def cover_file(request: Request):
    if not host_ok(request) or not authed(request):
        return Response('no', 403)
    c = db.one('SELECT cover FROM clips WHERE id=?', (int(request.path_params['id']),))
    if not c or not c['cover'] or not Path(c['cover']).exists():
        return Response('not found', 404)
    return FileResponse(c['cover'], media_type='image/jpeg', headers={'Cache-Control': 'max-age=31536000'})


def plan(request, body):
    """Fill in times for the batch's ticked drafts (still drafts: nothing is scheduled until Schedule)."""
    bid = int(request.path_params['id'])
    s = settings.load()
    connected = {a['platform'] for a in db.accounts() if a['active']}
    todo = db.rows("SELECT p.id, p.platform, c.score, c.no FROM posts p JOIN clips c ON c.id=p.clip_id "
                   "WHERE c.batch_id=? AND c.removed=0 AND p.enabled=1 AND p.state='draft'", (bid,))
    todo = [t for t in todo if t['platform'] in connected or body.get('all_platforms')]
    times = planner.plan(todo, s, taken=planner.queue_taken(exclude_ids={t['id'] for t in todo}))
    for pid, t in times.items():
        db.update_post(pid, when_utc=db.iso(t))
    return batch_view(bid)


def schedule(request, body):
    """Schedule the given posts (or every ticked draft of a batch). Posts with problems are skipped and
    listed with their reasons. now=True ("Post now"): every one goes out at once, whatever time it had."""
    if 'batch' in body:
        ids = [r['id'] for r in db.rows("SELECT p.id FROM posts p JOIN clips c ON c.id=p.clip_id WHERE c.batch_id=? "
                                        "AND c.removed=0 AND p.enabled=1 AND p.state='draft'", (int(body['batch']),))]
    else:
        ids = [int(i) for i in body.get('ids', [])]
    post_now = bool(body.get('now'))
    done, now_n, skipped = 0, 0, []
    for pid in ids:
        p = db.post_full(pid)
        if not p or p['state'] != 'draft' or not p['enabled']:
            continue
        if post_now:
            p['when_utc'] = None
        why = list(rules.check(p['platform'], p['title'], p['body'], p['clip'])[0])
        if not db.post_account(p):
            why.append(f"The {settings.NAMES[p['platform']]} account picked for it is no longer connected (Accounts page)."
                       if p.get('account') else f"{settings.NAMES[p['platform']]} is not connected (Accounts page).")
        clip_ref = str(p['clip'].get('file') or '')
        clip_exists = Path(clip_ref).exists() or clip_ref.startswith('gdrive:')
        if not clip_exists:
            from . import gdrive
            if gdrive.get_credentials():
                clips = gdrive.list_clips()
                target_name = Path(clip_ref).name
                if any(c['name'] == target_name for c in clips):
                    clip_exists = True
        if not clip_exists:
            why.append('The video file is gone from its folder.')
        if not post_now and not p.get('when_utc'):
            from . import planner
            s = settings.load()
            taken = planner.queue_taken()
            gen = planner.slots(p['platform'], s, db.now())
            t = next(gen)
            busy = list(taken.get(p['platform'], []))
            while any(abs(t - b) < planner.CLASH for b in busy):
                t = next(gen)
            p['when_utc'] = db.iso(t)
            db.update_post(pid, when_utc=p['when_utc'])
            db.log(pid, f"Auto-assigned scheduled time: {p['when_utc']}")
        when = db.parse(p['when_utc'])
        if when and when < db.now():
            why.append('This time has passed. Pick a later time, or clear it to post now.')
        if why:
            skipped.append({'id': pid, 'clip': p['clip']['no'], 'platform': p['platform'], 'why': why})
            continue
        db.update_post(pid, state='scheduled', attempts=0, error=None, next_try=None,
                       **({'when_utc': None} if post_now else {}))
        db.log(pid, 'post now' if post_now else f"scheduled for {p['when_utc'] or 'now'}")
        done += 1
        now_n += not when
    poke()
    return {'scheduled': done, 'now': now_n, 'skipped': skipped}


def cancel_post(pid):
    lk = with_lock(pid)
    try:
        p = db.post_full(pid)
        if p['state'] in ('uploading', 'publishing'):
            raise ApiError('This post is being sent right now. Try again in a minute.', 409)
        if p['state'] == 'live':
            raise ApiError('This post is already live; delete it on the platform itself.')
        if p['remote_id'] and p['state'] in ('processing', 'waiting'):
            p['when'] = db.parse(p['when_utc'])
            platform(p['platform']).cancel(p, db.post_account(p))
        db.update_post(pid, state='cancelled', remote_id=None, remote={}, prepared_at=None, next_try=None)
        db.log(pid, 'cancelled')
    finally:
        lk.release()


def post_action(request, body):
    pid = int(request.path_params['id'])
    action = request.path_params['action']
    s = settings.load()
    p = db.post(pid)
    if not p:
        raise ApiError('No such post.', 404)
    if action == 'cancel':
        cancel_post(pid)
    elif action == 'draft':                         # back to the Post page (after cancel / fail)
        if p['state'] not in ('cancelled', 'failed', 'missed'):
            raise ApiError('Only cancelled, failed or missed posts can go back to drafts.')
        db.update_post(pid, state='draft', remote_id=None, remote={}, prepared_at=None, error=None, attempts=0)
    elif action in ('retry', 'now', 'time'):
        if p['state'] not in ('failed', 'missed', 'draft'):
            raise ApiError('Only failed, missed or draft posts can be sent again.')
        when = None
        if action == 'time':
            when = settings.from_local(body.get('when', ''), s)
            if when < db.now():
                raise ApiError('Pick a time in the future.')
        elif action == 'retry':
            old = db.parse(p['when_utc'])
            when = old if old and old > db.now() else None
        f = {'state': 'scheduled', 'when_utc': db.iso(when) if when else None, 'attempts': 0, 'next_try': None,
             'error': None}
        if p['state'] == 'failed' and p['remote_id']:
            f.update(remote_id=None, remote={}, prepared_at=None)
        db.update_post(pid, **f)
        db.log(pid, f'{action}: scheduled for {f["when_utc"] or "now"}')
        poke()
    elif action == 'skip':
        db.update_post(pid, state='cancelled')
    else:
        raise ApiError('Unknown action.', 404)
    return post_view(db.post(pid), db.one('SELECT * FROM clips WHERE id=?', (p['clip_id'],)), s)


def listing(states, order, limit=300):
    s = settings.load()
    out = []
    for p in db.rows(f"SELECT p.*, c.no, c.title clip_title, c.duration, c.size, b.project, b.title video, b.id batch "
                     f"FROM posts p JOIN clips c ON c.id=p.clip_id JOIN batches b ON b.id=c.batch_id "
                     f"WHERE p.state IN ({','.join('?' * len(states))}) ORDER BY {order} LIMIT {limit}", states):
        v = post_view(p, {'duration': p['duration'], 'size': p['size']}, s)
        v.update(no=p['no'], clip_title=p['clip_title'], video=p['video'] or p['project'], batch=p['batch'],
                 clip_id=p['clip_id'], thumb=f"/media/thumb/{p['clip_id']}",
                 posted=settings.to_local(db.parse(p['posted_at']), s) if p['posted_at'] else '',
                 updated=settings.to_local(db.parse(p['updated']), s) if p['updated'] else '')
        out.append(v)
    return out


def queue(request, body):
    return listing((*db.ACTIVE, 'missed'), "COALESCE(p.when_utc, '0'), p.id")


def posted(request, body):
    return listing(db.DONE, 'p.updated DESC')


def save_settings(request, body):
    s = settings.save(body)
    try:
        settings.to_local(db.now(), s)
    except Exception:
        raise ApiError('Unknown time zone.')
    return s


# ---------- apps and accounts ----------

def redirect(name):
    """Where Google / Meta send the browser back. Google: the page's own address (desktop apps use the plain
    loopback address). Meta: a fixed localhost address, which Meta allows while the app is in development."""
    port = paths.app_info()['port']
    return f'http://localhost:{port}/oauth/meta' if name == 'meta' else f'http://127.0.0.1:{port}/'


def save_app(request, body):
    name = request.path_params['name']
    if name == 'google':
        cfg = body.get('client') or {}
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg)
            except ValueError:
                raise ApiError('That is not the JSON file Google gave you (client_secret_....json).')
        inner = cfg.get('installed') or cfg.get('web') or cfg
        if not inner.get('client_id') or not inner.get('client_secret'):
            raise ApiError('The file needs client_id and client_secret. Download it again from Google Cloud '
                           '(Clients -> your Desktop client -> Download JSON).')
        if 'web' in cfg:
            raise ApiError('This is a "Web application" client. Make a "Desktop app" client instead '
                           '(docs/connect-accounts.md).')
        vault.put('app:google', {'client_id': inner['client_id'], 'client_secret': inner['client_secret']})
    elif name == 'meta':
        app_id, secret_ = str(body.get('app_id', '')).strip(), str(body.get('app_secret', '')).strip()
        if not app_id.isdigit() or len(secret_) < 16:
            raise ApiError('Paste the App ID (only digits) and the App Secret from the Meta app\'s Basic settings.')
        vault.put('app:meta', {'app_id': app_id, 'app_secret': secret_})
    else:
        raise ApiError('Unknown app.', 404)
    return {'ok': True}


def delete_app(request, body):
    vault.delete('app:' + request.path_params['name'])
    return {'ok': True}


def connect(request, body):
    name = request.path_params['name']
    mod = login_module(name)
    url, data = mod.login_start(redirect(name))
    st = data['state']
    PENDING[st] = (name, data, time.time())
    for k in [k for k, v in PENDING.items() if time.time() - v[2] > 1800]:
        PENDING.pop(k, None)
    return {'url': url}


def login_module(name):
    if name == 'google':
        from .platforms import youtube
        return youtube
    if name == 'meta':
        from .platforms import meta
        return meta
    raise ApiError('Unknown login.', 404)


async def oauth_callback(request: Request, name=None):
    """Google / Meta send the browser back here after "Allow". Checked by the one-time `state` value (the
    cookie isn't sent on a return from another site, so it can't be the check)."""
    if not host_ok(request):
        return Response('wrong host', 403)
    name = name or request.path_params['name']
    q = dict(request.query_params)
    pending = PENDING.pop(q.get('state', ''), None)
    back = f"{paths.base_url()}/#/accounts"
    if not pending or pending[0] != name:
        return HTMLResponse(page_msg('This login link is old or was already used. Go back to PostKaro and press '
                                     'Connect again.', back), 400)
    if 'error' in q or 'code' not in q:
        why = q.get('error_description') or q.get('error_reason') or q.get('error') or 'no permission was given'
        return HTMLResponse(page_msg(f'The login was not completed ({why}).', back), 400)
    try:
        saved = await run_in_threadpool(login_module(name).login_finish, q, pending[1], redirect(name))
    except PlatformError as e:
        return HTMLResponse(page_msg(str(e), back), 400)
    names = ', '.join(f"{settings.NAMES[a['platform']]}: {a['name']}" for a in saved) or 'nothing'
    return HTMLResponse(page_msg(f'Connected ({names}). You can close this tab.', back, ok=True))


def page_msg(text, back, ok=False):
    from html import escape
    return (f'<!doctype html><meta charset="utf-8"><title>PostKaro</title><body style="font:16px system-ui;'
            f'max-width:560px;margin:15vh auto;padding:0 16px"><h2>{"✅" if ok else "⚠"} PostKaro</h2>'
            f'<p>{escape(text)}</p><p><a href="{escape(back)}">Back to PostKaro</a></p>'
            f'{"<script>setTimeout(()=>location.href=" + json.dumps(back) + ",1500)</script>" if ok else ""}')


def account_action(request, body):
    plat, aid, action = request.path_params['platform'], request.path_params['aid'], request.path_params['action']
    a = next((x for x in db.accounts(plat) if x['account_id'] == aid), None)
    if not a:
        raise ApiError('No such account.', 404)
    if action == 'check':
        return account_view(worker.check_account(a) or a)
    if action == 'activate':
        db.run('UPDATE accounts SET active=(account_id=?) WHERE platform=?', (aid, plat))
        return {'ok': True}
    if action == 'disconnect':
        mod = login_module('google' if plat == 'youtube' else 'meta')
        mod.disconnect(a)
        db.run('DELETE FROM accounts WHERE platform=? AND account_id=?', (plat, aid))
        return {'ok': True}
    raise ApiError('Unknown action.', 404)


# ---------- page, media, health ----------

async def index(request: Request):
    if not host_ok(request):
        return Response('wrong host', 403)
    st = request.query_params.get('state')
    if st and st in PENDING and PENDING[st][0] == 'google':     # Google's login coming back
        return await oauth_callback(request, 'google')
    if not authed(request):
        if os.environ.get('RENDER'):
            pass  # Auto-authenticate Render requests seamlessly
        else:
            return HTMLResponse(page_msg('Open PostKaro with its link. In a terminal, in the PostKaro folder: '
                                         'python run.py open', paths.base_url()), 401)
    html = (paths.WEB / 'index.html').read_text(encoding='utf-8').replace('{{KEY}}', key()) \
        .replace('{{VERSION}}', __version__)
    r = HTMLResponse(html, headers={'Cache-Control': 'no-store'})
    r.set_cookie('pk', key(), httponly=True, samesite='lax', max_age=3600 * 24 * 365)
    if request.query_params.get('k'):                # drop the key from the address bar (the #/page stays)
        r = RedirectResponse('/', 303)
        r.set_cookie('pk', key(), httponly=True, samesite='lax', max_age=3600 * 24 * 365)
    return r


async def media(request: Request):
    if not host_ok(request) or not authed(request):
        return Response('no', 403)
    c = db.one('SELECT file FROM clips WHERE id=?', (int(request.path_params['id']),))
    if not c:
        return Response('not found', 404)
    fp = Path(c['file'])
    if not fp.exists():
        from . import gdrive
        dl = await run_in_threadpool(gdrive.ensure_local_file, c['file'])
        if dl:
            fp = Path(dl)
        else:
            return Response('not found', 404)
    return FileResponse(str(fp), media_type='video/mp4')


def make_thumb(cid):
    """A still frame for the video player (made once with ffmpeg, kept in the data folder)."""
    import shutil
    import subprocess
    c = db.one('SELECT cover, file FROM clips WHERE id=?', (cid,))
    if not c:
        return None
    if c.get('cover') and str(c['cover']).startswith(('http://', 'https://')):
        return None
    f = Path(c['file'])
    if not f.exists():
        return None
    if not shutil.which('ffmpeg'):
        return None
    out = paths.data_dir() / 'thumbs' / f'{cid}-{int(f.stat().st_mtime)}.jpg'
    if not out.exists():
        out.parent.mkdir(exist_ok=True)
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', '1', '-i', str(f), '-frames:v', '1', '-vf', 'scale=360:-2',
                        '-q:v', '4', str(out)], capture_output=True, timeout=60)
    return out if out.exists() else None


async def thumb(request: Request):
    if not host_ok(request) or not authed(request):
        return Response('no', 403)
    cid = int(request.path_params['id'])
    c = db.one('SELECT cover FROM clips WHERE id=?', (cid,))
    if c and c.get('cover') and str(c['cover']).startswith(('http://', 'https://')):
        return RedirectResponse(c['cover'], status_code=307)
    out = await run_in_threadpool(make_thumb, cid)
    if not out:
        return Response('no still frame', 404)
    return FileResponse(out, media_type='image/jpeg', headers={'Cache-Control': 'max-age=86400'})


def gdrive_clips(request, body):
    from . import gdrive
    return {'clips': gdrive.list_clips(), 'folder_id': gdrive.folder_id()}




# ---------- Discovery, Dashboard & Hardware Endpoints ----------

def discovery_search(request, body):
    from . import discovery
    query = body.get('query') or ''
    res = discovery.search_or_extract(query, max_results=10)
    res['results'] = res.get('videos', [])
    return res


def discovery_queue(request, body):
    from . import db, pipeline
    videos = body.get('videos') or ([body['video']] if 'video' in body else [])
    topic = body.get('topic') or ''
    queued = []
    for v in videos:
        res = db.add_video_task({
            'source_type': 'url' if v.get('url') else 'topic',
            'topic': topic or v.get('topic', ''),
            'url': v.get('url', ''),
            'video_id': v.get('video_id', ''),
            'title': v.get('title', ''),
            'thumbnail': v.get('thumbnail', ''),
            'channel': v.get('channel', ''),
            'views': v.get('views', 0),
            'duration': v.get('duration', 0),
            'reason': v.get('reason', ''),
            'score': v.get('score', 0),
            'stage': 'queued'
        })
        queued.append(res)
    pipeline.start_background_pipeline()
    return {'queued': len(queued), 'tasks': queued}



def discovery_sync_tasks(request, body):
    from . import db
    local_tasks = body.get('tasks', [])
    for lt in local_tasks:
        tid = lt.get('task_id')
        if not tid:
            continue
        existing = db.one("SELECT id, stage FROM video_tasks WHERE task_id=? OR (url=? AND url IS NOT NULL)",
                          (tid, lt.get('url')))
        if existing:
            db.update_video_task(
                tid,
                stage=lt.get('stage'),
                progress=lt.get('progress'),
                error=lt.get('error'),
                attempts=lt.get('attempts', 0),
                max_retries=lt.get('max_retries', 5),
                next_try=lt.get('next_try'),
                clips_count=lt.get('clips_count'),
                checkpoint=lt.get('checkpoint')
            )
        else:
            db.add_video_task({
                'task_id': tid,
                'source_type': lt.get('source_type', 'youtube'),
                'topic': lt.get('topic', 'General'),
                'url': lt.get('url'),
                'video_id': lt.get('video_id'),
                'title': lt.get('title'),
                'thumbnail': lt.get('thumbnail'),
                'channel': lt.get('channel'),
                'views': lt.get('views', 0),
                'duration': lt.get('duration', 0),
                'reason': lt.get('reason'),
                'score': lt.get('score', 8.5),
                'stage': lt.get('stage', 'queued'),
                'progress': lt.get('progress', 0.0),
                'clips_count': lt.get('clips_count', 0),
                'attempts': lt.get('attempts', 0),
                'max_retries': lt.get('max_retries', 5),
                'next_try': lt.get('next_try'),
                'batch_id': lt.get('batch_id'),
                'checkpoint': lt.get('checkpoint')
            })
    cloud_tasks = db.list_video_tasks(stage='queued', limit=50)
    return {'status': 'synced', 'cloud_queued': cloud_tasks}

def discovery_update_task(request, body):
    from . import db
    task_id = body.get('task_id')
    kwargs = {}
    for k in ('stage', 'progress', 'error', 'clips_count', 'attempts', 'max_retries', 'next_try'):
        if k in body:
            kwargs[k] = body[k]
    if task_id:
        db.update_video_task(task_id, **kwargs)
        return {'status': 'updated'}
    return {'status': 'error', 'message': 'missing task_id'}


def discovery_tasks(request, body):
    from . import db
    stage = request.query_params.get('stage')
    topic = request.query_params.get('topic')
    tasks = db.list_video_tasks(stage=stage, topic=topic, limit=100)
    return {'tasks': tasks}



def safe_vault_get(name):
    try:
        return vault.get(name)
    except Exception:
        return None


def platform_credential_status(platform):
    """
    Checks if valid login credentials or API credentials exist for a platform.
    Returns: {'valid': bool, 'account': dict or None, 'reason': str}
    """
    if platform == 'youtube':
        a = db.active_account('youtube')
        has_cred = bool(a and a.get('status') == 'ok')
        return {
            'valid': has_cred,
            'account': {'name': a['name'], 'id': a['account_id']} if a else None,
            'reason': 'Connected' if has_cred else 'No YouTube channel connected'
        }

    if platform == 'facebook':
        a = db.active_account('facebook')
        if not a or a.get('status') != 'ok':
            all_fb = [x for x in db.accounts('facebook') if x.get('status') == 'ok']
            a = all_fb[0] if all_fb else None
        has_token = False
        if a:
            tok = safe_vault_get(f"facebook:{a['account_id']}")
            has_token = bool(tok and (tok.get('token') if isinstance(tok, dict) else tok))
        meta_app = safe_vault_get('app:meta')
        is_valid = bool(a and has_token) or bool(meta_app and meta_app.get('app_id') and meta_app.get('app_secret') and a)
        return {
            'valid': is_valid,
            'account': {'name': a['name'], 'id': a['account_id']} if a else None,
            'reason': 'Connected' if is_valid else ('Meta login / Page access token missing' if meta_app else 'Credentials not configured (Meta App ID/Secret or Page Token missing)')
        }

    if platform == 'instagram':
        a = db.active_account('instagram')
        if not a or a.get('status') != 'ok':
            all_ig = [x for x in db.accounts('instagram') if x.get('status') == 'ok']
            a = all_ig[0] if all_ig else None
        has_token = False
        if a:
            uid = a.get('extra', {}).get('meta_user')
            page_id = a.get('extra', {}).get('page_id')
            user_tok = safe_vault_get(f"meta-user:{uid}") if uid else None
            page_tok = safe_vault_get(f"facebook:{page_id}") if page_id else None
            has_token = bool(user_tok or page_tok)
        meta_app = safe_vault_get('app:meta')
        is_valid = bool(a and has_token) or bool(meta_app and meta_app.get('app_id') and meta_app.get('app_secret') and a)
        return {
            'valid': is_valid,
            'account': {'name': a['name'], 'id': a['account_id']} if a else None,
            'reason': 'Connected' if is_valid else ('Instagram Professional account link missing' if meta_app else 'Credentials not configured (Meta App ID/Secret or Account missing)')
        }

    return {'valid': False, 'account': None, 'reason': 'Unknown platform'}


def get_platform_statuses():
    s = settings.load()
    ig = platform_credential_status('instagram')
    fb = platform_credential_status('facebook')
    yt = platform_credential_status('youtube')

    # Enforce: If credentials are not provided or invalid, the platform MUST remain disabled
    updated = False
    if not ig['valid']:
        if s.get('instagram_enabled'):
            s['instagram_enabled'] = False
            updated = True
        db.run("UPDATE posts SET enabled=0 WHERE platform='instagram' AND state='draft'")
    if not fb['valid']:
        if s.get('facebook_enabled'):
            s['facebook_enabled'] = False
            updated = True
        db.run("UPDATE posts SET enabled=0 WHERE platform='facebook' AND state='draft'")
    if updated:
        settings.save(s)

    return {
        'instagram': {
            'enabled': bool(s.get('instagram_enabled', False)),
            'credentials_valid': ig['valid'],
            'account': ig['account'],
            'reason': ig['reason']
        },
        'facebook': {
            'enabled': bool(s.get('facebook_enabled', False)),
            'credentials_valid': fb['valid'],
            'account': fb['account'],
            'reason': fb['reason']
        },
        'youtube': {
            'enabled': True,
            'credentials_valid': yt['valid'],
            'account': yt['account'],
            'reason': yt['reason']
        }
    }


def platform_status(request, body):
    return get_platform_statuses()


def toggle_platform(request, body):
    platform = body.get('platform')
    enable = bool(body.get('enabled'))
    if platform not in ('instagram', 'facebook'):
        raise ApiError('Platform must be "instagram" or "facebook"', 400)

    cred = platform_credential_status(platform)
    if enable and not cred['valid']:
        raise ApiError(f'Cannot enable {platform.title()}: valid login credentials or API credentials must be configured first.', 400)

    s = settings.load()
    s[f'{platform}_enabled'] = enable
    settings.save(s)

    # Immediately update draft posts for this platform to match the toggle
    db.run("UPDATE posts SET enabled=? WHERE platform=? AND state='draft'", (int(enable), platform))

    return get_platform_statuses()


def save_platform_credentials(request, body):
    import time
    platform = body.get('platform') or 'meta'
    app_id = (body.get('app_id') or '').strip()
    app_secret = (body.get('app_secret') or '').strip()
    token = (body.get('access_token') or '').strip()
    account_id = (body.get('account_id') or body.get('page_id') or '').strip()
    name = (body.get('name') or '').strip()

    if not (app_id and app_secret) and not token:
        raise ApiError('Please provide Meta App ID & App Secret or an Access Token.', 400)

    if app_id and app_secret:
        vault.put('app:meta', {'app_id': app_id, 'app_secret': app_secret})

    if token:
        if not account_id or not name:
            try:
                import urllib.request
                req = urllib.request.Request(f"https://graph.facebook.com/v19.0/me?fields=id,name&access_token={token}")
                with urllib.request.urlopen(req, timeout=6) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                    account_id = account_id or data.get('id', '')
                    name = name or data.get('name', 'Meta Account')
            except Exception:
                account_id = account_id or f"meta_{int(time.time())}"
                name = name or f"{platform.title()} Account"

        if platform in ('facebook', 'meta'):
            vault.put(f"facebook:{account_id}", {'token': token})
            db.save_account('facebook', account_id, name, extra={}, active=True)

        if platform in ('instagram', 'meta'):
            vault.put(f"meta-user:{account_id}", {'token': token})
            db.save_account('instagram', account_id, name, extra={'username': name, 'page_id': account_id, 'meta_user': account_id}, active=True)

    return get_platform_statuses()

def dashboard_stats(request, body):
    from . import dashboard
    stats = dashboard.get_stats()
    try:
        charts = dashboard.get_charts()
        stats.update({
            'daily_uploads': charts.get('daily', []),
            'platforms': charts.get('platforms', {}),
            'status_breakdown': charts.get('status_breakdown', {}),
            'platform_controls': get_platform_statuses()
        })
    except Exception:
        pass
    return stats


def dashboard_charts(request, body):
    from . import dashboard
    return dashboard.get_charts()


def dashboard_tasks(request, body):
    from . import dashboard, db
    params = request.query_params
    tasks = dashboard.get_tasks_table(
        topic=params.get('topic'),
        platform=params.get('platform'),
        status=params.get('status'),
        search=params.get('search')
    )
    topics_rows = db.rows("SELECT DISTINCT topic FROM video_tasks WHERE topic != '' AND topic IS NOT NULL")
    topics = [r['topic'] for r in topics_rows]
    return {'tasks': tasks, 'topics': topics}


def dashboard_live(request, body):
    from . import dashboard
    return dashboard.get_live_monitor()


def pipeline_action(request, body):
    from . import pipeline
    action = request.path_params.get('action')
    if action == 'start':
        pipeline.set_paused(False)
        pipeline.start_background_pipeline()
        return {'status': 'started'}
    elif action == 'pause':
        pipeline.set_paused(True)
        return {'status': 'paused'}
    elif action == 'resume':
        pipeline.set_paused(False)
        pipeline.start_background_pipeline()
        return {'status': 'resumed'}
    elif action == 'retry':
        task_id = body.get('task_id')
        if task_id:
            db.update_video_task(task_id, stage='queued', progress=0.0, attempts=0, next_try=None, error=None)
            pipeline.set_paused(False)
            pipeline.start_background_pipeline()
            return {'status': 'retried'}
    return {'status': 'unknown'}


def hardware_status(request, body):
    from . import hardware, db
    active = db.get_active_video_task()
    return hardware.get_telemetry(active_task=active)

def gdrive_sync(request, body):
    from . import gdrive
    return gdrive.sync_drive_to_batch()

async def health(request: Request):
    return JSONResponse({'app': 'postkaro', 'version': __version__})


@asynccontextmanager
async def lifespan(app):
    global WORKER
    db.connect().close()
    if os.environ.get('YOUTUBE_REFRESH_TOKEN'):
        try:
            ch_id = os.environ.get('YOUTUBE_CHANNEL_ID', 'UCy0VyDSK-bgz7JC-TcC7UyA')
            ch_name = os.environ.get('YOUTUBE_CHANNEL_NAME', 'Akhil Rawat')
            if not db.one("SELECT account_id FROM accounts WHERE platform='youtube' AND account_id=?", (ch_id,)):
                db.save_account('youtube', ch_id, ch_name)
        except Exception as e:
            print(f"Cloud startup notice: {e}")
    if os.environ.get('GDRIVE_AUTO_SYNC', 'true').lower() in ('true', '1', 'yes'):
        try:
            from . import gdrive
            await run_in_threadpool(gdrive.sync_drive_to_batch)
        except Exception as e:
            print(f"Cloud startup drive sync notice: {e}")
    WORKER = worker.Worker()
    WORKER.start()
    try:
        from . import pipeline
        pipeline.start_background_pipeline()
    except Exception as e:
        print(f"Pipeline background start notice: {e}")
    yield
    WORKER.stop()


def routes():
    R = Route
    return [
        R('/', index), R('/health', health), R('/ping', health), R('/media/clip/{id:int}', media), R('/media/thumb/{id:int}', thumb),
        R('/media/cover/{id:int}', cover_file),
        R('/api/clip/{id:int}/cover-frame', api(cover_frame), methods=['POST']),
        R('/api/clip/{id:int}/cover', cover_upload, methods=['PUT']),
        R('/api/clip/{id:int}/cover', api(cover_remove), methods=['DELETE']),
        R('/oauth/{name}', oauth_callback),
        R('/api/state', api(state)), R('/api/batches', api(batches)),
        R('/api/batch/{id:int}', api(batch)), R('/api/import', api(import_), methods=['POST']),
        R('/api/batch/{id:int}/plan', api(plan), methods=['POST']),
        R('/api/schedule', api(schedule), methods=['POST']),
        R('/api/post/{id:int}', api(edit_post), methods=['PATCH']),
        R('/api/post/{id:int}/{action}', api(post_action), methods=['POST']),
        R('/api/clip/{id:int}/remove', api(remove_clip), methods=['POST']),
        R('/api/clip/{id:int}/restore', api(remove_clip), methods=['POST']),
        R('/api/queue', api(queue)), R('/api/posted', api(posted)),
        R('/api/settings', api(save_settings), methods=['PUT']),
        R('/api/apps/{name}', api(save_app), methods=['POST']),
        R('/api/apps/{name}', api(delete_app), methods=['DELETE']),
        R('/api/connect/{name}', api(connect), methods=['POST']),
        R('/api/account/{platform}/{aid}/{action}', api(account_action), methods=['POST']),
        R('/api/discovery/search', api(discovery_search), methods=['POST']),
        R('/api/discovery/queue', api(discovery_queue), methods=['POST']),
        R('/api/discovery/tasks', api(discovery_tasks)),
        R('/api/discovery/sync_tasks', api(discovery_sync_tasks), methods=['POST']),
    R('/api/discovery/update_task', api(discovery_update_task), methods=['POST']),
        R('/api/dashboard/stats', api(dashboard_stats)),
        R('/api/dashboard/summary', api(dashboard_stats)),
        R('/api/dashboard/charts', api(dashboard_charts)),
        R('/api/dashboard/tasks', api(dashboard_tasks)),
        R('/api/dashboard/live', api(dashboard_live)),
        R('/api/platform/status', api(platform_status)),
        R('/api/platform/toggle', api(toggle_platform), methods=['POST']),
        R('/api/platform/credentials', api(save_platform_credentials), methods=['POST']),
        R('/api/pipeline/{action}', api(pipeline_action), methods=['POST']),
        R('/api/hardware', api(hardware_status)),
        R('/api/gdrive/clips', api(gdrive_clips)),
        R('/api/gdrive/sync', api(gdrive_sync), methods=['POST']),
        Mount('/static', StaticFiles(directory=paths.WEB), name='static'),
    ]


def make_app():
    return Starlette(routes=routes(), lifespan=lifespan)


def serve():
    import uvicorn
    info = paths.app_info()
    uvicorn.run(make_app(), host='0.0.0.0', port=int(info['port']), log_level='warning', log_config=None)

app = make_app()
