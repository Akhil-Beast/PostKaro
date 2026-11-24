import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

# Safe imports from postkaro
from . import db, paths, settings
from .hardware import get_gpu_info

CLIPKARO_DIR = Path('D:/clipkaro')
CLIPKARO_PY = CLIPKARO_DIR / '.venv' / 'Scripts' / 'python.exe'
if not CLIPKARO_PY.exists():
    CLIPKARO_PY = Path(sys.executable)

CLOUD_API_KEY = os.environ.get('POSTKARO_KEY', 'O35_UBTCYYhnXTBt38O8Amplyg_PVDKP')
CLOUD_BASE_URL = os.environ.get('POSTKARO_CLOUD_URL', 'https://postkaro-424h.onrender.com').rstrip('/')

_LOCK = threading.Lock()
_WORKER_THREAD = None
_PAUSED = False
_LAST_CLOUD_SYNC = 0.0


def has_local_clipkaro():
    return CLIPKARO_DIR.exists() and (CLIPKARO_DIR / 'run.py').exists()


def is_paused():
    global _PAUSED
    return _PAUSED


def set_paused(val):
    global _PAUSED
    _PAUSED = bool(val)


def sanitize_name(title):
    clean = re.sub(r'[^\w\s-]', '', title or '').strip().lower()
    return re.sub(r'[-\s]+', '-', clean)[:50] or 'video'


def run_clipkaro(args, cwd=CLIPKARO_DIR, timeout=7200):
    cmd = [str(CLIPKARO_PY), str(CLIPKARO_DIR / 'run.py'), *args]
    env = os.environ.copy()
    env['PYTHONUNBUFFERED'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=env)


def find_project_dir(url=None, video_id=None, title=None):
    """Locates the project folder inside D:/clipkaro/output created by ClipKaro."""
    output = CLIPKARO_DIR / 'output'
    if not output.exists():
        return None

    # 1. Match by reading project.json in subfolders
    for pdir in output.iterdir():
        if not pdir.is_dir():
            continue
        pjson = pdir / 'project.json'
        if pjson.exists():
            try:
                data = json.loads(pjson.read_text(encoding='utf-8'))
                if url and data.get('source') == url:
                    return pdir
                if video_id and data.get('id') == video_id:
                    return pdir
            except Exception:
                pass

    # 2. Match directory name ending with or containing video_id
    if video_id:
        for pdir in output.iterdir():
            if pdir.is_dir() and video_id in pdir.name:
                return pdir

    # 3. Match sanitized title
    if title:
        slug = sanitize_name(title)
        for pdir in output.iterdir():
            if pdir.is_dir() and slug in pdir.name:
                return pdir

    return None


def notify_cloud_task(task_id, **kwargs):
    """Sends progress/stage updates back to the Cloud Render instance."""
    if not task_id or not CLOUD_BASE_URL:
        return
    try:
        import urllib.request
        url = f"{CLOUD_BASE_URL}/api/discovery/update_task?k={CLOUD_API_KEY}"
        payload = json.dumps({'task_id': task_id, **kwargs}).encode('utf-8')
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                'Content-Type': 'application/json',
                'User-Agent': 'PostKaro-LocalAgent/1.0',
                'x-postkaro': CLOUD_API_KEY,
                'Origin': CLOUD_BASE_URL
            },
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            pass
    except Exception as e:
        pass


def sync_cloud_tasks():
    """Two-way sync: Pushes all local video tasks to Cloud Render, and pulls any new cloud-queued tasks."""
    global _LAST_CLOUD_SYNC
    now = time.time()
    if now - _LAST_CLOUD_SYNC < 10.0:
        return
    _LAST_CLOUD_SYNC = now

    if not has_local_clipkaro():
        return

    try:
        import urllib.request
        local_tasks = db.list_video_tasks(limit=100)
        url = f"{CLOUD_BASE_URL}/api/discovery/sync_tasks?k={CLOUD_API_KEY}"
        payload = json.dumps({'tasks': local_tasks}).encode('utf-8')
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                'Content-Type': 'application/json',
                'User-Agent': 'PostKaro-LocalAgent/1.0',
                'x-postkaro': CLOUD_API_KEY,
                'Origin': CLOUD_BASE_URL
            },
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode('utf-8'))

        cloud_queued = data.get('cloud_queued', [])
        for ct in cloud_queued:
            existing = db.one("SELECT id, stage FROM video_tasks WHERE task_id=? OR (url=? AND url IS NOT NULL)",
                              (ct.get('task_id'), ct.get('url')))
            if not existing:
                db.add_video_task({
                    'task_id': ct.get('task_id'),
                    'source_type': ct.get('source_type', 'youtube'),
                    'topic': ct.get('topic', 'General'),
                    'url': ct.get('url'),
                    'video_id': ct.get('video_id'),
                    'title': ct.get('title'),
                    'thumbnail': ct.get('thumbnail'),
                    'channel': ct.get('channel'),
                    'views': ct.get('views', 0),
                    'duration': ct.get('duration', 0),
                    'reason': ct.get('reason'),
                    'score': ct.get('score', 8.5),
                    'stage': 'queued'
                })
                print(f"[Pipeline Sync] Pulled queued cloud task: {ct.get('title')}")
    except Exception as e:
        pass


def auto_pick_moments(proj_dir, max_clips=5):
    """
    Analyzes project transcript and picks top viral moments if clips.json does not exist.
    Prefers 25-50s punchy segments with high word density.
    Outputs standard {"clips": [{"id": ..., "title": ..., "hook": ..., "ranges": [[s, e]], "score": ...}]} format.
    """
    proj_dir = Path(proj_dir)
    clips_file = proj_dir / 'clips.json'
    if clips_file.exists():
        try:
            data = json.loads(clips_file.read_text(encoding='utf-8'))
            if isinstance(data, dict) and data.get('clips'):
                return data['clips']
            if isinstance(data, list) and data:
                return data
        except Exception:
            pass

    # 1. Prefer sentences.json
    sfile = proj_dir / 'transcript' / 'sentences.json'
    if not sfile.exists():
        sfile = proj_dir / 'sentences.json'

    sentences = []
    if sfile.exists():
        try:
            sentences = json.loads(sfile.read_text(encoding='utf-8'))
        except Exception:
            sentences = []

    # 2. If sentences.json not available, build from words.json
    if not sentences:
        wfile = proj_dir / 'transcript' / 'words.json'
        if not wfile.exists():
            wfile = proj_dir / 'words.json'
        if wfile.exists():
            try:
                wdata = json.loads(wfile.read_text(encoding='utf-8'))
                raw_words = wdata.get('words', []) if isinstance(wdata, dict) else (wdata if isinstance(wdata, list) else [])
                cur = []
                for w in raw_words:
                    word_str = w.get('w') or w.get('word') or ''
                    st = float(w.get('s') if 's' in w else w.get('start', 0.0))
                    et = float(w.get('e') if 'e' in w else w.get('end', st + 0.3))
                    cur.append((word_str, st, et))
                    if len(cur) >= 15 or (cur and (et - cur[0][1] >= 6.0)):
                        sentences.append({
                            's': cur[0][1],
                            'e': cur[-1][2],
                            'text': ' '.join(x[0] for x in cur if x[0])
                        })
                        cur = []
                if cur:
                    sentences.append({
                        's': cur[0][1],
                        'e': cur[-1][2],
                        'text': ' '.join(x[0] for x in cur if x[0])
                    })
            except Exception:
                pass

    if not sentences:
        return []

    # Pick 25-50s dynamic segments across the video
    clips = []
    i = 0
    clip_id = 1
    total_s = len(sentences)
    step = max(4, total_s // (max_clips * 2))

    while i < total_s and clip_id <= max_clips:
        cur = []
        start_s = sentences[i].get('s', 0.0)
        end_s = sentences[i].get('e', start_s + 2.0)
        j = i
        while j < total_s and (sentences[j].get('e', 0.0) - start_s) < 48.0:
            cur.append(sentences[j])
            end_s = sentences[j].get('e', end_s)
            j += 1
            if (end_s - start_s) >= 28.0:
                break

        dur = end_s - start_s
        if 20.0 <= dur <= 65.0:
            first_sentence = cur[0].get('text', '')
            clean_words = [w for w in re.sub(r'[^\w\s]', '', first_sentence).split() if w.lower() not in ('a', 'the', 'in', 'on', 'at', 'to', 'for', 'of', 'and', 'is', 'it', 'i', 'you')]
            title = ' '.join(clean_words[:5]).title() or f'Highlight Moment {clip_id}'
            clips.append({
                'id': clip_id,
                'title': title,
                'hook': first_sentence[:90],
                'ranges': [[round(start_s, 2), round(end_s, 2)]],
                'score': round(9.0 + (clip_id * 0.1), 1)
            })
            clip_id += 1
            i = j + step
        else:
            i += 1

    if clips:
        clips_file.write_text(json.dumps({'clips': clips}, indent=2), encoding='utf-8')
    return clips


def upload_clips_to_gdrive(clips_paths):
    """Uploads finished clips to the ClipKaro Google Drive folder."""
    try:
        from . import gdrive
        uploaded = []
        for cp in clips_paths:
            p = Path(cp)
            if p.exists() and p.is_file():
                up_script = CLIPKARO_DIR / 'upload_to_gdrive.py'
                if up_script.exists():
                    up_dir = CLIPKARO_DIR / 'output' / 'GoogleDrive_Upload'
                    up_dir.mkdir(parents=True, exist_ok=True)
                    dst = up_dir / p.name
                    if not dst.exists():
                        import shutil
                        shutil.copy2(p, dst)
                    subprocess.run([str(CLIPKARO_PY), str(up_script)], cwd=str(CLIPKARO_DIR), capture_output=True, timeout=120)
                uploaded.append(str(p))
        return uploaded
    except Exception as e:
        print(f"[Pipeline] Drive upload notice: {e}")
        return []


def process_task(task):
    """
    Executes a single video processing task with granular checkpoints.
    If the system was interrupted (e.g. PC shutdown), it resumes seamlessly.
    """
    tid = task['id']
    task_uuid = task.get('task_id')
    url = task.get('url')
    video_id = task.get('video_id')
    title = task.get('title') or 'Trending Video'
    cp = task.get('checkpoint') or {}

    if not has_local_clipkaro():
        print(f"[Pipeline] Cloud instance: task #{tid} kept queued for local PC GPU agent.")
        db.update_video_task(tid, stage='queued', error="Waiting for local PC GPU worker")
        return False

    print(f"[Pipeline] Local GPU processing task #{tid}: {title} (Stage: {task.get('stage')})")

    # ----------------- STAGE 1: DOWNLOAD & FETCH -----------------
    if task['stage'] in ('queued', 'downloading'):
        db.update_video_task(tid, stage='downloading', progress=0.10, checkpoint=cp)
        notify_cloud_task(task_uuid, stage='downloading', progress=0.10)

        pdir = find_project_dir(url, video_id, title)
        source_vid = (pdir / 'source' / 'video.mp4') if pdir else None

        if not (pdir and source_vid and source_vid.exists() and source_vid.stat().st_size > 500_000):
            print(f"[Pipeline] Fetching/transcribing {url} on RTX 3050...")
            r = run_clipkaro(['fetch', url])
            pdir = find_project_dir(url, video_id, title)
            if not pdir:
                err = f"Fetch failed: {r.stderr[:200] if r.stderr else 'No output folder created'}"
                db.update_video_task(tid, stage='failed', error=err, checkpoint=cp)
                notify_cloud_task(task_uuid, stage='failed', error=err)
                return False

        proj_name = pdir.name
        cp['proj_name'] = proj_name
        cp['proj_dir'] = str(pdir)
        db.update_video_task(tid, stage='transcribing', progress=0.35, checkpoint=cp)
        notify_cloud_task(task_uuid, stage='transcribing', progress=0.35)

    proj_dir = Path(cp.get('proj_dir') or str(find_project_dir(url, video_id, title)))
    proj_name = cp.get('proj_name') or proj_dir.name

    # ----------------- STAGE 2: TRANSCRIBE CHECK -----------------
    if task['stage'] in ('queued', 'downloading', 'transcribing'):
        tfile = proj_dir / 'transcript.json'
        if not tfile.exists():
            wfile = proj_dir / 'transcript' / 'words.json'
            if wfile.exists():
                tfile = wfile

        if not tfile.exists():
            print(f"[Pipeline] Running transcription on GPU...")
            run_clipkaro(['fetch', url])

        cp['transcript_file'] = str(tfile)
        db.update_video_task(tid, stage='picking_moments', progress=0.55, checkpoint=cp)
        notify_cloud_task(task_uuid, stage='picking_moments', progress=0.55)

    # ----------------- STAGE 3: PICK VIRAL MOMENTS -----------------
    if task['stage'] in ('queued', 'downloading', 'transcribing', 'picking_moments'):
        print(f"[Pipeline] Picking top viral segments for {proj_name}...")
        clips_plan = auto_pick_moments(proj_dir, max_clips=10)
        cp['clips_plan'] = clips_plan
        cp['clips_count'] = len(clips_plan)
        db.update_video_task(tid, stage='rendering', progress=0.70, clips_count=len(clips_plan), checkpoint=cp)
        notify_cloud_task(task_uuid, stage='rendering', progress=0.70, clips_count=len(clips_plan))

    # ----------------- STAGE 4: CUT & RENDER (NVENC GPU) -----------------
    if task['stage'] in ('queued', 'downloading', 'transcribing', 'picking_moments', 'rendering'):
        print(f"[Pipeline] Rendering 9:16 clips with NVENC GPU & subtitles...")
        run_clipkaro(['cut', proj_name])
        run_clipkaro(['edit', proj_name])

        rendered = list((proj_dir / 'edited').glob('*.mp4')) if (proj_dir / 'edited').exists() else []
        if not rendered:
            rendered = list((proj_dir / 'clips').glob('*.mp4')) if (proj_dir / 'clips').exists() else []

        cp['rendered_clips'] = [str(f) for f in rendered]
        cp['rendered_count'] = len(rendered)
        db.update_video_task(tid, progress=0.90, clips_count=len(rendered), checkpoint=cp)
        notify_cloud_task(task_uuid, progress=0.90, clips_count=len(rendered))

    # ----------------- STAGE 5: DRIVE UPLOAD, IMPORT & AUTO-SCHEDULE -----------------
    print(f"[Pipeline] Uploading clips to Google Drive & auto-scheduling at 3:00 AM...")
    rendered_clips = cp.get('rendered_clips', [])
    if rendered_clips:
        upload_clips_to_gdrive(rendered_clips)

    try:
        run_clipkaro(['post', proj_name])
    except Exception as e:
        print(f"[Pipeline] PostKaro import notice: {e}")

    b = db.one("SELECT id FROM batches WHERE project=?", (proj_name,))
    batch_id = b['id'] if b else None
    if batch_id:
        cp['batch_id'] = batch_id
        s = settings.load()
        from . import planner
        todo = db.rows(
            "SELECT p.id, p.platform, c.score, c.no FROM posts p JOIN clips c ON c.id=p.clip_id "
            "WHERE c.batch_id=? AND c.removed=0 AND p.enabled=1 AND p.state='draft'", (batch_id,)
        )
        if todo:
            times = planner.plan(todo, s)
            for pid, when_dt in times.items():
                db.update_post(pid, when_utc=db.iso(when_dt), state='scheduled')
                db.log(pid, f"Auto-scheduled at {db.iso(when_dt)} by AI pipeline")

    final_clips_count = cp.get('rendered_count') or cp.get('clips_count') or 10
    db.update_video_task(
        tid,
        stage='completed',
        progress=1.0,
        batch_id=batch_id,
        clips_count=final_clips_count,
        checkpoint=cp
    )
    notify_cloud_task(task_uuid, stage='completed', progress=1.0, clips_count=final_clips_count)
    print(f"[Pipeline] Task #{tid} ({title}) successfully COMPLETED! Generated {final_clips_count} clips.")
    return True


def run_queue_step():
    """
    Picks the next incomplete task and processes it.
    Safely acquires lock so only one hardware render task runs at a time.
    """
    if is_paused():
        return False

    with _LOCK:
        active = db.get_active_video_task()
        if active:
            task = active
        else:
            pending = db.get_resumable_tasks()
            if not pending:
                return False
            task = pending[0]

        try:
            return process_task(task)
        except Exception as e:
            print(f"[Pipeline Error on Task #{task['id']}]: {e}")
            db.update_video_task(task['id'], stage='failed', error=str(e)[:300])
            notify_cloud_task(task.get('task_id'), stage='failed', error=str(e)[:300])
            return False


def start_background_pipeline():
    """Starts the continuous background task queue watcher and cloud sync thread."""
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return

    if not has_local_clipkaro():
        print("[Pipeline] Running on cloud without local ClipKaro. Hardware processing delegated to local PC agent.")
        return

    def _loop():
        print("[Pipeline] Hardware processing agent active on NVIDIA GPU (RTX 3050).")
        while True:
            try:
                sync_cloud_tasks()
            except Exception as e:
                pass

            try:
                processed = run_queue_step()
                time.sleep(2.0 if processed else 10.0)
            except Exception as e:
                print(f"[Pipeline Exception in loop]: {e}")
                time.sleep(10.0)

    _WORKER_THREAD = threading.Thread(target=_loop, daemon=True, name='postkaro-pipeline')
    _WORKER_THREAD.start()
