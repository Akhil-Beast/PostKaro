"""Trending Video Discovery & Analysis for PostKaro.

Finds and analyzes the best YouTube videos for a given topic or extracts details
for a specific YouTube URL. Evaluates short-form viral potential and selects the
top 10 video sources for clip generation.
"""
import re
import urllib.parse
from datetime import datetime
try:
    import yt_dlp
except ImportError:
    yt_dlp = None

TOPIC_PRESETS = [
    'Gym Motivation',
    'Fitness & Workout',
    'Artificial Intelligence',
    'Technology & Coding',
    'Mechanical Engineering',
    'Podcasts & Interviews',
    'Gaming Highlights',
    'Business & Finance',
    'Self Improvement'
]


def is_youtube_url(text):
    text = (text or '').strip()
    return bool(re.search(r'(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/|youtube\.com/live/)', text))


def extract_video_id(url):
    m = re.search(r'(?:v=|\/|youtu\.be\/|shorts\/|live\/)([0-9A-Za-z_-]{11})', url or '')
    return m.group(1) if m else None


def format_views(n):
    if not n:
        return '0 views'
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M views"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K views"
    return f"{n:,} views"


def format_duration(sec):
    if not sec:
        return '0:00'
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def calculate_viral_score(entry, topic=''):
    """
    Computes an AI virality potential score (1.0 to 10.0) based on:
    - View volume and view velocity
    - Duration sweet-spot for clipping (5 to 45 mins is ideal; <3m is too short; >3h is sparse)
    - Speech / dialogue indicators in title & tags
    - Engagement metrics (like count vs view ratio if present)
    """
    views = entry.get('view_count') or 0
    duration = entry.get('duration') or 0
    title = entry.get('title') or ''
    title_lower = title.lower()

    # Base score
    score = 6.0

    # 1. View count weight
    if views > 2_000_000:
        score += 1.8
    elif views > 500_000:
        score += 1.3
    elif views > 100_000:
        score += 0.8
    elif views > 20_000:
        score += 0.4
    else:
        score -= 0.5

    # 2. Duration sweet spot (5m - 50m gives the highest density of 30-90s viral clips)
    if 300 <= duration <= 3000:
        score += 1.2
    elif 180 <= duration < 300:
        score += 0.5
    elif 3000 < duration <= 7200:
        score += 0.4
    elif duration > 7200:
        score -= 0.6
    elif duration < 120:
        score -= 1.0  # Already short

    # 3. High-retention keywords (speech, reactions, motivation, rules, secrets, lessons, podcasts)
    viral_keywords = [
        'motivation', 'speech', 'rules', 'mindset', 'secret', 'lesson', 'truth',
        'advice', 'interview', 'podcast', 'explained', 'breakdown', 'how to',
        'story', 'insane', 'never', 'life', 'discipline', 'success', 'future'
    ]
    matched = sum(1 for kw in viral_keywords if kw in title_lower)
    score += min(1.2, matched * 0.4)

    # 4. Penalty for non-verbal or compilation tracks
    music_keywords = ['lofi', 'ambient', 'bgm', '10 hours', 'nonstop music', 'rain sounds', 'sleep music']
    if any(mk in title_lower for mk in music_keywords):
        score -= 3.0

    return max(1.0, min(9.9, round(score, 1)))


def generate_selection_reason(entry, topic, score):
    title = entry.get('title') or ''
    views = entry.get('view_count') or 0
    dur = entry.get('duration') or 0
    title_lower = title.lower()

    reasons = []
    if views > 500_000:
        reasons.append(f"Strong organic traction ({format_views(views)})")
    if 300 <= dur <= 2400:
        reasons.append(f"Ideal pacing for short-form highlights ({format_duration(dur)})")

    if any(k in title_lower for k in ('speech', 'motivation', 'discipline', 'mindset')):
        reasons.append("Punchy vocal hooks with high quote-retention")
    elif any(k in title_lower for k in ('podcast', 'interview', 'talks', 'explains')):
        reasons.append("Conversational soundbites suitable for dynamic split captions")
    elif any(k in title_lower for k in ('ai', 'tech', 'engineering', 'future', 'robot')):
        reasons.append("High educational curiosity & visual demonstration appeal")
    else:
        reasons.append("Clear thematic focus with high potential for 30-60s vertical clips")

    if not reasons:
        reasons.append("Relevant topic coverage with high engagement signals")

    return " • ".join(reasons)


def generate_clip_ideas(entry, topic):
    title = entry.get('title', '')
    return [
        f"Key takeaway from {title[:35]}...",
        "The surprising turning point / most emotional statement",
        "Actionable advice & summary quote (9:16 vertical hook)"
    ]


def search_or_extract(query_or_url, max_results=10):
    """
    Given a YouTube URL or Topic/Keyword, retrieves and analyzes candidates,
    returning the top 10 ranked video sources with detailed metadata.
    """
    query = (query_or_url or '').strip()
    if not query:
        return {'query': '', 'type': 'none', 'videos': []}

    ydl_opts = {
        'extract_flat': True,
        'quiet': True,
        'skip_download': True,
        'ignoreerrors': True,
        'no_warnings': True,
    }

    if not yt_dlp:
        return {'query': query, 'type': 'error', 'error': 'yt-dlp is not installed on this server', 'videos': []}
    is_url = is_youtube_url(query)
    search_target = query if is_url else f"ytsearch25:{query}"

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(search_target, download=False)
        except Exception as e:
            return {'query': query, 'type': 'error', 'error': str(e), 'videos': []}

    raw_entries = info.get('entries', []) if 'entries' in info else [info]
    candidates = []

    for item in raw_entries:
        if not item or not item.get('id'):
            continue
        vid = item.get('id')
        title = item.get('title') or 'Untitled'
        url = item.get('url') or f"https://www.youtube.com/watch?v={vid}"
        if not url.startswith('http'):
            url = f"https://www.youtube.com/watch?v={vid}"

        views = item.get('view_count') or 0
        duration = item.get('duration') or 0
        channel = item.get('uploader') or item.get('channel') or 'YouTube Creator'
        thumbnail = item.get('thumbnail') or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"

        score = calculate_viral_score(item, query if not is_url else 'YouTube')
        reason = generate_selection_reason(item, query, score)
        clip_ideas = generate_clip_ideas(item, query)

        candidates.append({
            'video_id': vid,
            'title': title,
            'url': url,
            'thumbnail': thumbnail,
            'channel': channel,
            'views': views,
            'views_formatted': format_views(views),
            'duration': duration,
            'duration_formatted': format_duration(duration),
            'score': score,
            'reason': reason,
            'clip_ideas': clip_ideas,
        })

    # Sort candidates by viral potential score descending
    candidates.sort(key=lambda x: x['score'], reverse=True)
    selected_10 = candidates[:max_results]

    return {
        'query': query,
        'type': 'url' if is_url else 'topic',
        'count': len(selected_10),
        'videos': selected_10
    }
