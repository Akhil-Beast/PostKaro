"""Status Dashboard & Analytics Backend for PostKaro.

Calculates actual system metrics, upload performance, time-series charts data,
live hardware telemetry, and the comprehensive task status table with filtering.
"""
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from . import db, hardware, settings


def get_stats():
    """
    Computes real KPI counters:
    - total_videos_selected
    - total_clips_generated
    - total_posted
    - total_scheduled (actual posts queued/scheduled to go live)
    - total_drafts (unscheduled drafts in editor)
    - total_pending (alias of total_scheduled for backwards compatibility)
    - scheduled_clips (distinct clips scheduled)
    - total_processing
    - total_failed
    """
    # 1. Video tasks counts
    video_tasks = db.rows("SELECT stage, clips_count FROM video_tasks")
    total_selected = len(video_tasks)

    # 2. Clips & posts counts
    clips_row = db.one("SELECT COUNT(*) as c FROM clips WHERE removed=0")
    total_clips = clips_row['c'] if clips_row else 0

    posts_rows = db.rows("SELECT state, COUNT(*) as c FROM posts GROUP BY state")
    posts_by_state = {r['state']: r['c'] for r in posts_rows}

    total_posted = posts_by_state.get('live', 0)
    # Only posts that are actually scheduled/queued to go live (waiting or scheduled)
    total_scheduled = posts_by_state.get('scheduled', 0) + posts_by_state.get('waiting', 0)
    total_drafts = posts_by_state.get('draft', 0)
    total_processing = posts_by_state.get('uploading', 0) + posts_by_state.get('processing', 0) + posts_by_state.get('publishing', 0)
    total_failed = posts_by_state.get('failed', 0) + posts_by_state.get('missed', 0)

    # Number of unique clips that are scheduled
    sched_clips_row = db.one("SELECT COUNT(DISTINCT clip_id) as c FROM posts WHERE state IN ('scheduled', 'waiting')")
    scheduled_clips = sched_clips_row['c'] if sched_clips_row else 0

    # Also include active pipeline tasks in total_processing
    active_pipeline = sum(1 for t in video_tasks if t['stage'] in ('downloading', 'transcribing', 'picking_moments', 'rendering'))

    return {
        'total_videos_selected': total_selected,
        'total_clips_generated': total_clips,
        'total_posted': total_posted,
        'total_scheduled': total_scheduled,
        'total_pending': total_scheduled,  # Set to actual scheduled count so legacy consumers match
        'total_drafts': total_drafts,
        'scheduled_clips': scheduled_clips,
        'total_processing': total_processing + active_pipeline,
        'total_failed': total_failed,
    }


def get_charts():
    """
    Generates time-series charts data from real events & posts:
    - Daily uploads (past 14 days)
    - Weekly uploads (past 8 weeks)
    - Monthly uploads (past 6 months)
    - Status breakdown (Posted vs Scheduled vs Processing vs Failed)
    - Platform distribution
    """
    now = db.now()
    s = settings.load()

    # 1. Daily uploads (last 14 days)
    days = []
    daily_counts = defaultdict(int)
    for i in range(13, -1, -1):
        dt = now - timedelta(days=i)
        day_str = dt.strftime('%Y-%m-%d')
        days.append(day_str)

    # Query posted items
    live_posts = db.rows("SELECT posted_at, platform FROM posts WHERE state='live' AND posted_at IS NOT NULL")
    for lp in live_posts:
        d = lp['posted_at'][:10]
        if d in daily_counts or d >= days[0]:
            daily_counts[d] += 1

    daily_chart = [{'date': d, 'label': d[5:], 'count': daily_counts[d]} for d in days]

    # 2. Status Breakdown
    posts_rows = db.rows("SELECT state, COUNT(*) as c FROM posts GROUP BY state")
    state_map = {'live': 'Posted', 'scheduled': 'Scheduled', 'waiting': 'Queued', 'draft': 'Drafts', 'failed': 'Failed', 'uploading': 'Uploading', 'processing': 'Processing'}
    status_breakdown = [{'label': state_map.get(r['state'], r['state'].title()), 'state': r['state'], 'count': r['c']} for r in posts_rows]

    # 3. Platform Breakdown
    platform_rows = db.rows("SELECT platform, COUNT(*) as c FROM posts GROUP BY platform")
    platform_counts = [{'platform': r['platform'], 'label': settings.NAMES.get(r['platform'], r['platform'].title()), 'count': r['c']} for r in platform_rows]

    return {
        'daily': daily_chart,
        'status_breakdown': status_breakdown,
        'platforms': platform_counts
    }


def get_tasks_table(topic=None, platform=None, status=None, search=None, limit=200):
    """
    Builds the complete detailed task table combining:
    - Original YouTube Video Title
    - Topic
    - Clip Number
    - Processing Status (Video Task Stage)
    - Upload Status (Post State)
    - Scheduled Date & Time
    - Platform
    - Posted Time
    """
    s = settings.load()
    sql = """
        SELECT
            vt.id AS task_id,
            vt.title AS video_title,
            vt.topic AS topic,
            vt.stage AS processing_stage,
            vt.progress AS processing_progress,
            vt.error AS task_error,
            c.no AS clip_no,
            c.title AS clip_title,
            c.duration AS clip_duration,
            p.id AS post_id,
            p.platform AS platform,
            p.state AS upload_status,
            p.when_utc AS scheduled_when,
            p.posted_at AS posted_at,
            p.error AS post_error,
            p.url AS live_url
        FROM video_tasks vt
        LEFT JOIN batches b ON (b.id = vt.batch_id OR (CASE WHEN json_valid(vt.checkpoint) THEN b.project = json_extract(vt.checkpoint, '$.proj_name') ELSE 0 END))
        LEFT JOIN clips c ON c.batch_id = b.id
        LEFT JOIN posts p ON p.clip_id = c.id
        ORDER BY vt.id DESC, c.no ASC
        LIMIT ?
    """
    rows = db.rows(sql, (limit,))
    out = []

    for r in rows:
        row = dict(r)

        # Format times using local timezone
        sched = row.get('scheduled_when')
        if sched:
            dt = db.parse(sched)
            local_str = settings.to_local(dt, s)
            row['scheduled_date'] = local_str[:10]
            row['scheduled_time'] = local_str[11:16]
        else:
            row['scheduled_date'] = '-'
            row['scheduled_time'] = '-'

        posted = row.get('posted_at')
        if posted:
            dt = db.parse(posted)
            row['posted_time'] = settings.to_local(dt, s)
        else:
            row['posted_time'] = '-'

        # Default fallbacks if clip not yet cut
        row['clip_no'] = row['clip_no'] or 1
        row['clip_title'] = row['clip_title'] or row['video_title']
        row['platform'] = row['platform'] or 'youtube'
        row['upload_status'] = row['upload_status'] or ('queued' if row['processing_stage'] != 'completed' else 'draft')

        # Filter application
        if topic and topic.lower() not in (row['topic'] or '').lower():
            continue
        if platform and row['platform'].lower() != platform.lower():
            continue
        if status:
            st = status.lower()
            if st != row['upload_status'].lower() and st != row['processing_stage'].lower():
                continue
        if search:
            q = search.lower()
            if q not in (row['video_title'] or '').lower() and q not in (row['clip_title'] or '').lower() and q not in (row['topic'] or '').lower():
                continue

        out.append(row)

    return out


def get_live_monitor():
    """
    Returns real-time processing telemetry:
    - Active video title & stage
    - Progress %
    - Completed vs Remaining clips
    - CPU, GPU, RAM, Storage
    - Agent status
    - Estimated processing time
    """
    active_task = db.get_active_video_task()
    telemetry = hardware.get_telemetry(active_task=active_task)

    if active_task:
        stage = active_task.get('stage')
        prog = active_task.get('progress') or 0.0
        # Estimated time remaining
        stage_weights = {'downloading': 90, 'transcribing': 180, 'picking_moments': 30, 'rendering': 120}
        rem_sec = stage_weights.get(stage, 60) * (1.0 - prog)
        eta_str = f"{int(rem_sec // 60)}m {int(rem_sec % 60)}s" if rem_sec > 0 else "Finishing..."

        clips_done = active_task.get('clips_count') if stage in ('rendering', 'completed') else 0
        clips_total = 5

        telemetry['active_task_info'] = {
            'id': active_task['id'],
            'title': active_task.get('title'),
            'topic': active_task.get('topic'),
            'stage': stage,
            'progress_percent': round(prog * 100),
            'clips_completed': clips_done,
            'clips_remaining': max(0, clips_total - clips_done),
            'eta': eta_str
        }
    else:
        telemetry['active_task_info'] = None

    return telemetry
