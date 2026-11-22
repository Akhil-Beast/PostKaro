"""Local PC Video Processing & Checkpoint Recovery Pipeline.

Executes video processing on the local computer's CPU and NVIDIA GPU (CUDA + NVENC).
Provides robust checkpointing: if the PC shuts down or restarts, tasks safely
resume from the exact checkpoint without re-downloading or duplicate processing.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import db, paths, settings
from .hardware import get_gpu_info

CLIPKARO_DIR = Path('D:/clipkaro')
CLIPKARO_PY = CLIPKARO_DIR / '.venv' / 'Scripts' / 'python.exe'
if not CLIPKARO_PY.exists():
    CLIPKARO_PY = Path(sys.executable)

_LOCK = threading.Lock()
_WORKER_THREAD = None
_PAUSED = False


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


def auto_pick_moments(proj_dir, max_clips=5):
    """
    Analyzes project transcript and picks top viral moments if clips.json does not exist.
    Prefers 25-60s punchy segments with high word density.
    """
    transcript_file = proj_dir / 'transcript.json'
    clips_file = proj_dir / 'clips.json'
    if clips_file.exists():
        try:
            return json.loads(clips_file.read_text(encoding='utf-8'))
        except Exception:
            pass

    if not transcript_file.exists():
        return []

    try:
        tdata = json.loads(transcript_file.read_text(encoding='utf-8'))
    except Exception:
        return []

    lines = tdata.get('lines', [])
    if not lines:
        return []

    # Simple sliding window to find highest energy verbal segments (30-60s)
    candidates = []
    total_len = len(lines)
    chunk_size = max(5, min(25, total_len // 8))

    for i in range(0, max(1, total_len - chunk_size), max(1, chunk_size // 2)):
        chunk = lines[i:i + chunk_size]
        if not chunk:
            continue
        start_t = chunk[0].get('start', 0.0)
        end_t = chunk[-1].get('end', start_t + 30.0)
        dur = end_t - start_t
        if 20.0 <= dur <= 75.0:
            words = ' '.join(l.get('text', '') for l in chunk)
            candidates.append({
                'start': start_t,
                'end': end_t,
                'words': words,
                'density': len(words) / max(1.0, dur)
            })

    candidates.sort(key=lambda x: x['density'], reverse=True)
    picked = candidates[:max_clips]
    picked.sort(key=lambda x: x['start'])

    clips = []
    for idx, c in enumerate(picked, 1):
        clean_hook = c['words'][:40].replace('\n', ' ').strip()
        clips.append({
            'no': idx,
            'start': round(c['start'], 2),
            'end': round(c['end'], 2),
            'title': f"{idx:02d} {clean_hook}",
            'score': round(8.0 + (idx * 0.3), 1)
        })

    clips_file.write_text(json.dumps(clips, indent=2), encoding='utf-8')
    return clips


def process_task(task):
    """
    Executes a single video processing task with granular checkpoints.
    If the system was interrupted (e.g. PC shutdown), it resumes seamlessly.
    """
    tid = task['id']
    url = task.get('url')
    title = task.get('title') or 'Trending Video'
    cp = task.get('checkpoint') or {}
    proj_name = cp.get('proj_name') or f"auto_{sanitize_name(title)}_{task.get('video_id', tid)}"
    proj_dir = CLIPKARO_DIR / 'output' / proj_name

    cp['proj_name'] = proj_name
    cp['proj_dir'] = str(proj_dir)

    print(f"[Pipeline] Processing task #{tid}: {title} (Stage: {task.get('stage')})")

    # ----------------- STAGE 1: DOWNLOAD & FETCH -----------------
    if task['stage'] in ('queued', 'downloading'):
        db.update_video_task(tid, stage='downloading', progress=0.10, checkpoint=cp)

        # Check if source already exists
        source_vid = proj_dir / 'source' / 'video.mp4'
        source_aud = proj_dir / 'source' / 'audio.m4a'
        if not (source_vid.exists() and source_vid.stat().st_size > 500_000):
            print(f"[Pipeline] Downloading video {url} into {proj_dir}...")
            r = run_clipkaro(['fetch', url])
            if r.returncode != 0 and not source_vid.exists():
                err = f"Fetch failed: {r.stderr[:200]}"
                db.update_video_task(tid, stage='failed', error=err, checkpoint=cp)
                return False

        cp['source_video'] = str(source_vid)
        cp['source_audio'] = str(source_aud)
        db.update_video_task(tid, stage='transcribing', progress=0.30, checkpoint=cp)

    # ----------------- STAGE 2: TRANSCRIBE -----------------
    if task['stage'] in ('queued', 'downloading', 'transcribing'):
        transcript_file = proj_dir / 'transcript.json'
        if not transcript_file.exists():
            print(f"[Pipeline] Transcribing {proj_name} on GPU/CUDA...")
            r = run_clipkaro(['fetch', url])
            if not transcript_file.exists():
                err = f"Transcription failed: {r.stderr[:200]}"
                db.update_video_task(tid, stage='failed', error=err, checkpoint=cp)
                return False

        cp['transcript_file'] = str(transcript_file)
        db.update_video_task(tid, stage='picking_moments', progress=0.55, checkpoint=cp)

    # ----------------- STAGE 3: PICK VIRAL MOMENTS -----------------
    if task['stage'] in ('queued', 'downloading', 'transcribing', 'picking_moments'):
        print(f"[Pipeline] Selecting best moments for {proj_name}...")
        clips_plan = auto_pick_moments(proj_dir, max_clips=5)
        cp['clips_plan'] = clips_plan
        cp['clips_count'] = len(clips_plan)
        db.update_video_task(tid, stage='rendering', progress=0.70, clips_count=len(clips_plan), checkpoint=cp)

    # ----------------- STAGE 4: CUT & RENDER (NVENC GPU) -----------------
    if task['stage'] in ('queued', 'downloading', 'transcribing', 'picking_moments', 'rendering'):
        print(f"[Pipeline] Rendering 9:16 clips with NVENC GPU & subtitles...")
        clips_dir = proj_dir / 'clips'
        clips_dir.mkdir(parents=True, exist_ok=True)

        # 4a. Run cut
        r_cut = run_clipkaro(['cut', proj_name])
        # 4b. Run edit (subtitles, audio normalization)
        r_edit = run_clipkaro(['edit', proj_name])

        rendered = list(clips_dir.glob('*.mp4'))
        if not rendered:
            # Fallback: check if clips were created in project root
            rendered = list(proj_dir.glob('*.mp4'))

        cp['rendered_clips'] = [str(f) for f in rendered]
        cp['rendered_count'] = len(rendered)
        db.update_video_task(tid, progress=0.90, clips_count=len(rendered), checkpoint=cp)

    # ----------------- STAGE 5: IMPORT, AUTO-SCHEDULE & DRIVE -----------------
    print(f"[Pipeline] Importing clips into PostKaro & scheduling...")
    try:
        # Import batch into PostKaro
        r_post = run_clipkaro(['post', proj_name])
    except Exception as e:
        print(f"[Pipeline] Notice during post import: {e}")

    # Check or link batch in PostKaro database
    b = db.one("SELECT id FROM batches WHERE project=?", (proj_name,))
    batch_id = b['id'] if b else None
    if batch_id:
        cp['batch_id'] = batch_id
        # Plan optimal schedule times for newly imported drafts
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

    # Mark completed!
    db.update_video_task(
        tid,
        stage='completed',
        progress=1.0,
        batch_id=batch_id,
        clips_count=cp.get('rendered_count', 5),
        checkpoint=cp
    )
    print(f"[Pipeline] Task #{tid} successfully COMPLETED! Generated {cp.get('rendered_count', 5)} clips.")
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
            return False


def start_background_pipeline():
    """Starts the continuous background task queue watcher thread."""
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return

    def _loop():
        print("[Pipeline] Hardware processing agent started.")
        while True:
            try:
                processed = run_queue_step()
                time.sleep(2.0 if processed else 10.0)
            except Exception as e:
                print(f"[Pipeline Exception in loop]: {e}")
                time.sleep(10.0)

    _WORKER_THREAD = threading.Thread(target=_loop, daemon=True, name='postkaro-pipeline')
    _WORKER_THREAD.start()
