#!/usr/bin/env python3

"""
db_utils.py
Shared utilities for database migration scripts.

Responsibilities:
- Shared configuration (BASE_URL, default data exclusion lists)
- Authentication against the production server
- Filtering default/seed data that should not be migrated
"""

import os
import sys
import requests

# Add the root directory to sys.path so app imports work
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from main import app

# ── Configuration ──────────────────────────────────────────────────────────────

BASE_URL = "https://flask.opencodingsociety.com"
AUTH_URL  = f"{BASE_URL}/api/authenticate"

# Credentials loaded from app config
UID      = app.config['ADMIN_UID']
PASSWORD = app.config['ADMIN_PASSWORD']

# Default data created by initUsers / init_posts that must not be duplicated.
# This list MUST match the users initUsers() seeds (model/user.py), otherwise a
# seeded user's production row is pulled but then silently dropped by the loader
# as "already exists", losing that user's real grade_data/ap_exam/sections.
DEFAULT_DATA = {
    'users': [
        app.config.get('ADMIN_UID'),
        app.config.get('USER_UID'),
        app.config.get('TEACHER_UID'),
        app.config.get('MY_UID'),
    ],
    'sections': ['CSA', 'CSP', 'Robotics', 'CSSE', 'CSH'],
    'topics': [
        '/lessons/flask-introduction',
        '/hacks/javascript-basics',
        '/projects/portfolio-showcase',
        '/general/daily-standup',
        '/resources/study-materials',
    ],
}

# ── Authentication ─────────────────────────────────────────────────────────────

def authenticate(uid=None, password=None):
    """Authenticate against the production server and return (cookies, error)."""
    uid      = uid      or UID
    password = password or PASSWORD

    auth_data = {"uid": uid, "password": password}
    headers   = {"Content-Type": "application/json", "X-Origin": "client"}

    print(f"  Authenticating as: {uid}")
    try:
        response = requests.post(AUTH_URL, json=auth_data, headers=headers)
        response.raise_for_status()
        print("  ✓ Authentication successful")
        return response.cookies, None
    except requests.RequestException as e:
        return None, {
            'message': 'Failed to authenticate',
            'code':    getattr(response, 'status_code', 0),
            'error':   str(e),
        }

# ── Default-data filters ───────────────────────────────────────────────────────

def is_default_user(uid):
    return uid in DEFAULT_DATA['users']

# ── Migration verification ─────────────────────────────────────────────────────

# Every data type the migration scripts move. Must stay in sync with
# MIGRATED_MODELS in api/data_export_import_api.py.
MIGRATED_TYPES = [
    'sections', 'users', 'topics', 'microblogs', 'posts', 'classrooms',
    'feedback', 'study', 'personas', 'user_personas',
    'leaderboard', 'elementary_leaderboard', 'skill_snapshots', 'presence',
]


def fetch_remote_counts(cookies):
    """Fetch per-table row counts from production. Returns (counts, error)."""
    headers = {"Content-Type": "application/json", "X-Origin": "client"}
    try:
        response = requests.get(
            f"{BASE_URL}/api/export/counts", headers=headers, cookies=cookies, timeout=120
        )
        if response.status_code not in (200, 201):
            return None, f"HTTP {response.status_code}: {response.text[:200]}"
        return response.json().get('counts', {}), None
    except requests.RequestException as e:
        return None, str(e)


def local_counts():
    """Row counts for every migrated table in the LOCAL database."""
    from model.user import User, Section, UserSection
    from model.post import Post
    from model.microblog import MicroBlog, Topic
    from model.classroom import Classroom, classroom_student
    from model.feedback import Feedback
    from model.study import Study
    from model.persona import Persona, UserPersona
    from model.leaderboard import ScoreCounterEvent, ElementaryLeaderboardEvent
    from model.skill_snapshot import SkillSnapshot
    from main import db

    models = {
        'sections': Section, 'users': User, 'topics': Topic, 'microblogs': MicroBlog,
        'posts': Post, 'classrooms': Classroom, 'feedback': Feedback, 'study': Study,
        'personas': Persona, 'user_personas': UserPersona,
        'leaderboard': ScoreCounterEvent,
        'elementary_leaderboard': ElementaryLeaderboardEvent,
        'skill_snapshots': SkillSnapshot,
    }
    counts = {name: model.query.count() for name, model in models.items()}
    counts['user_sections'] = UserSection.query.count()
    counts['classroom_students'] = db.session.query(classroom_student).count()
    from model.presence_backup import presence_counts
    counts.update(presence_counts())  # same keys as /api/export/counts
    return counts


def report_reconciliation(source_counts, target_counts, source_label, target_label,
                          expected_deficit=None):
    """Print a source-vs-target table comparison. Returns True when nothing is missing.

    *expected_deficit* maps a data type to the number of rows that are legitimately
    absent from the target (e.g. seed users filtered out of the transfer).
    """
    expected_deficit = expected_deficit or {}
    complete = True

    width = max(len(k) for k in source_counts) if source_counts else 10
    print(f"\n  {'table'.ljust(width)}  {source_label:>10}  {target_label:>10}  status")
    print(f"  {'-' * width}  {'-' * 10}  {'-' * 10}  ------")

    for name in sorted(source_counts):
        src = source_counts.get(name)
        tgt = target_counts.get(name)

        if not isinstance(src, int) or not isinstance(tgt, int):
            print(f"  {name.ljust(width)}  {str(src):>10}  {str(tgt):>10}  UNKNOWN")
            complete = False
            continue

        allowed = expected_deficit.get(name, 0)
        missing = src - tgt - allowed

        if missing > 0:
            print(f"  {name.ljust(width)}  {src:>10}  {tgt:>10}  MISSING {missing}")
            complete = False
        elif missing < 0:
            print(f"  {name.ljust(width)}  {src:>10}  {tgt:>10}  +{-missing} extra")
        else:
            print(f"  {name.ljust(width)}  {src:>10}  {tgt:>10}  ok")

    only_in_source = set(source_counts) - set(target_counts)
    if only_in_source:
        print(f"\n  Tables present in {source_label} but absent from {target_label}: "
              f"{', '.join(sorted(only_in_source))}")
        complete = False

    return complete

def is_default_section(abbreviation):
    return abbreviation in DEFAULT_DATA['sections']

def is_default_topic(page_path):
    return page_path in DEFAULT_DATA['topics']


def filter_default_data(all_data):
    """Remove seed/default records from *all_data* and return the cleaned copy."""
    filtered = {}

    users = all_data.get('users', [])
    if users:
        filtered['users'] = [u for u in users if not is_default_user(u.get('uid'))]
        skipped = len(users) - len(filtered['users'])
        if skipped:
            print(f"  Filtered out {skipped} default users")

    sections = all_data.get('sections', [])
    if sections:
        filtered['sections'] = [s for s in sections if not is_default_section(s.get('abbreviation'))]
        skipped = len(sections) - len(filtered['sections'])
        if skipped:
            print(f"  Filtered out {skipped} default sections")

    topics = all_data.get('topics', [])
    if topics:
        page_path_key = 'pagePath' if 'pagePath' in topics[0] else 'page_path'
        filtered['topics'] = [
            t for t in topics
            if not is_default_topic(t.get(page_path_key) or t.get('page_path'))
        ]
        skipped = len(topics) - len(filtered['topics'])
        if skipped:
            print(f"  Filtered out {skipped} default topics")

    microblogs = all_data.get('microblogs', [])
    if microblogs:
        filtered['microblogs'] = [
            m for m in microblogs
            if not is_default_user(m.get('userUid') or m.get('user', {}).get('uid'))
        ]
        skipped = len(microblogs) - len(filtered['microblogs'])
        if skipped:
            print(f"  Filtered out {skipped} microblogs from default users")

    posts = all_data.get('posts', [])
    if posts:
        filtered['posts'] = [
            p for p in posts
            if not is_default_user(
                p.get('userUid') or
                (p.get('user', {}).get('uid') if isinstance(p.get('user'), dict) else None)
            )
        ]
        skipped = len(posts) - len(filtered['posts'])
        if skipped:
            print(f"  Filtered out {skipped} posts from default users")

    # Pass-through for types without default data
    for key in all_data:
        if key not in filtered:
            filtered[key] = all_data[key]

    return filtered
