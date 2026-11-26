"""Export/import of the Event Service's tables for the drop-and-rebuild upgrade process
(api/data_export_import_api.py), the same way as model/presence_backup.py. Rows are linked by natural keys,
never by database ids, because ids change when tables are rebuilt:

    events              -> page_path            (the owner by uid)
    event_participants  -> page_path + uid
    event_files         -> key for a stored file; page_path + group + link for a hosted video

Users are imported before this runs, so people are matched by uid. Import skips anything that already exists,
so running it twice is safe. Only the rows travel: the files' bytes stay on the instance volume or in S3.
"""
from datetime import datetime

from __init__ import db
from model.event import Event, EventFile, EventParticipant
from model.user import User

KEYS = ('events', 'event_participants', 'event_files')


def _iso(moment):
    """Naive UTC datetime -> '...Z', to the microsecond, so a round trip keeps the exact time."""
    return moment.isoformat(timespec="microseconds") + "Z" if moment else None


def _dt(value):
    """'...Z' -> naive UTC datetime (how event times are stored)."""
    return datetime.fromisoformat(value.replace("Z", "")) if value else None


def export_events():
    """Every event table as JSON-ready lists, keyed like KEYS, with every column."""
    uid_of = {u.id: u.uid for u in User.query.all()}
    page_of = {e.id: e.page_path for e in Event.query.all()}
    return {
        'events': [{
            'page_path': e._page_path, 'title': e._title, 'starts_at': _iso(e._starts_at), 'ends_at': _iso(e._ends_at),
            'timezone': e._timezone, 'status': e._status, 'registration_enabled': e._registration_enabled,
            'capacity': e._capacity, 'features': e._features, 'notice': e._notice, 'data': e._data,
            'owner': uid_of.get(e._owner_id), 'checkin_secret': e._checkin_secret, 'created_at': _iso(e._created_at),
            'updated_at': _iso(e._updated_at), 'published_at': _iso(e._published_at),
        } for e in Event.query.order_by(Event.id)],
        'event_participants': [{
            'event': page_of.get(p._event_id), 'uid': uid_of.get(p._user_id), 'role': p._role, 'rsvp': p._rsvp,
            'rsvp_at': _iso(p._rsvp_at), 'checked_in_at': _iso(p._checked_in_at), 'check_in_method': p._check_in_method,
        } for p in EventParticipant.query.order_by(EventParticipant.id)],
        'event_files': [{
            'event': page_of.get(f._event_id), 'group': f._group, 'kind': f._kind, 'name': f._name, 'alt': f._alt,
            'key': f._key, 'link': f._link, 'content_type': f._content_type, 'size': f._size,
            'uploader_uid': f._uploader_uid, 'created_at': _iso(f._created_at),
        } for f in EventFile.query.order_by(EventFile.id)],
    }


def _result():
    return {'imported': 0, 'skipped': 0, 'failed': 0, 'errors': []}


def _fail(result, label, error):
    db.session.rollback()
    result['failed'] += 1
    result['errors'].append(f"{label}: {error}"[:200])


def import_events(data):
    """Restores whatever event keys are in data. Returns {table: {imported, skipped, failed, errors}}."""
    results = {key: _result() for key in KEYS if key in data}
    user_id = {u.uid: u.id for u in User.query.all()}

    if 'events' in data:
        res = results['events']
        for item in data['events']:
            try:
                if Event.by_page(item['page_path']):
                    res['skipped'] += 1
                    continue
                if item.get('owner') not in user_id:
                    raise ValueError(f"no user {item.get('owner')}")
                event = Event(item['page_path'], item['title'], db.session.get(User, user_id[item['owner']]))
                event._starts_at, event._ends_at = _dt(item['starts_at']), _dt(item['ends_at'])
                event._timezone, event._status = item['timezone'], item['status']
                event._registration_enabled, event._capacity = bool(item.get('registration_enabled')), item.get('capacity')
                event._features, event._notice, event._data = item.get('features') or {}, item.get('notice'), item.get('data') or {}
                event._checkin_secret = item.get('checkin_secret')
                for name in ('created_at', 'updated_at'):
                    if item.get(name):  # left unset, the column's default (now) applies
                        setattr(event, '_' + name, _dt(item[name]))
                event._published_at = _dt(item.get('published_at'))
                db.session.add(event)
                db.session.commit()
                res['imported'] += 1
            except Exception as e:
                _fail(res, f"event {item.get('page_path')}", e)
    event_id = {e.page_path: e.id for e in Event.query.all()}

    if 'event_participants' in data:
        res = results['event_participants']
        for item in data['event_participants']:
            try:
                if item.get('event') not in event_id or item.get('uid') not in user_id:
                    raise ValueError(f"no event {item.get('event')} or user {item.get('uid')}")
                if EventParticipant.query.filter_by(_event_id=event_id[item['event']], _user_id=user_id[item['uid']]).first():
                    res['skipped'] += 1
                    continue
                row = EventParticipant(db.session.get(Event, event_id[item['event']]), db.session.get(User, user_id[item['uid']]))
                row._role, row._rsvp, row._rsvp_at = item['role'], item.get('rsvp'), _dt(item.get('rsvp_at'))
                row._checked_in_at, row._check_in_method = _dt(item.get('checked_in_at')), item.get('check_in_method')
                db.session.add(row)
                db.session.commit()
                res['imported'] += 1
            except Exception as e:
                _fail(res, f"participant {item.get('uid')} of {item.get('event')}", e)

    if 'event_files' in data:
        res = results['event_files']
        for item in data['event_files']:
            try:
                if item.get('event') not in event_id:
                    raise ValueError(f"no event {item.get('event')}")
                if item.get('key'):
                    existing = EventFile.query.filter_by(_key=item['key']).first()
                else:
                    existing = EventFile.query.filter_by(_event_id=event_id[item['event']], _group=item['group'], _link=item.get('link')).first()
                if existing:
                    res['skipped'] += 1
                    continue
                row = EventFile(db.session.get(Event, event_id[item['event']]), group=item['group'], kind=item['kind'],
                                name=item['name'], uploader_uid=item.get('uploader_uid') or '', alt=item.get('alt'),
                                key=item.get('key'), link=item.get('link'), content_type=item.get('content_type'),
                                size=item.get('size'))
                if item.get('created_at'):
                    row._created_at = _dt(item['created_at'])
                db.session.add(row)
                db.session.commit()
                res['imported'] += 1
            except Exception as e:
                _fail(res, f"file {item.get('key') or item.get('link')}", e)

    return results


def event_counts():
    """Row counts for every event table, for migration reconciliation."""
    return {
        'events': Event.query.count(),
        'event_participants': EventParticipant.query.count(),
        'event_files': EventFile.query.count(),
    }
