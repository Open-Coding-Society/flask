"""Export/import of the presence tables for the drop-and-rebuild upgrade
process (api/data_export_import_api.py). Rows are linked by natural keys,
never by database ids, because ids change when tables are rebuilt:

    users      -> uid               rooms     -> name
    schedules  -> name              tags      -> tag_uid
    events     -> event_id          checks    -> event_id
    periods    -> old id, mapped to the new id during import

Users are imported before this runs, so people are matched by uid. Import
skips anything that already exists, so running it twice is safe.
"""

from datetime import date, datetime

from __init__ import db
from model.presence import CameraCheck, PresenceEvent, PresencePeriod, PresenceRoom
from model.presence_schedule import PresenceSchedule, PresenceScheduleOverride
from model.rfid_tag import RfidTag
from model.user import User

KEYS = ('presence_schedules', 'presence_rooms', 'presence_schedule_overrides', 'rfid_tags',
        'presence_periods', 'presence_events', 'camera_checks')


def _dt(value):
    """'...Z' -> naive UTC datetime (how presence timestamps are stored)."""
    return datetime.fromisoformat(value.replace("Z", "")) if value else None


def export_presence():
    """Every presence table as JSON-ready lists, keyed like KEYS."""
    uid_of = {u.id: u.uid for u in User.query.all()}
    return {
        'presence_schedules': [s.to_dict() for s in PresenceSchedule.query.all()],
        'presence_rooms': [r.to_dict() for r in PresenceRoom.query.all()],
        'presence_schedule_overrides': [o.to_dict() for o in PresenceScheduleOverride.query.all()],
        'rfid_tags': [{'tag_uid': t.tag_uid, 'uid': uid_of.get(t.user_id),
                       'created_at': t.created_at.isoformat() if t.created_at else None}
                      for t in RfidTag.query.all()],
        'presence_periods': [{**p.to_dict(), 'room': p.room.name if p.room else None,
                              'duration_seconds': p.duration_seconds, 'active': p.active}
                             for p in PresencePeriod.query.all()],
        'presence_events': [{**e.to_dict(), 'room': e.room.name if e.room else None,
                             'period_id': e._period_id, 'tag_uid': e._tag_uid}
                            for e in PresenceEvent.query.all()],
        'camera_checks': [c.to_dict() for c in CameraCheck.query.all()],
    }


def _result():
    return {'imported': 0, 'skipped': 0, 'failed': 0, 'errors': []}


def _fail(result, label, error):
    db.session.rollback()
    result['failed'] += 1
    result['errors'].append(f"{label}: {error}"[:200])


def import_presence(data):
    """Restores whatever presence keys are in data. Returns a result per key."""
    from model.presence_schedule import check_timezone
    results = {key: _result() for key in KEYS if key in data}
    user_id = {u.uid: u.id for u in User.query.all()}

    if 'presence_schedules' in data:
        res = results['presence_schedules']
        for item in data['presence_schedules']:
            try:
                if PresenceSchedule.query.filter_by(_name=item['name']).first():
                    res['skipped'] += 1
                    continue
                schedule = PresenceSchedule(name=item['name'], timezone=check_timezone(item.get('timezone')),
                                            grace_seconds=int(item.get('grace_seconds', 300)))
                schedule.set_blocks(item.get('blocks', []))
                db.session.add(schedule)
                db.session.commit()
                res['imported'] += 1
            except Exception as e:
                _fail(res, f"schedule {item.get('name')}", e)
    schedule_id = {s.name: s.id for s in PresenceSchedule.query.all()}

    if 'presence_rooms' in data:
        res = results['presence_rooms']
        for item in data['presence_rooms']:
            try:
                if PresenceRoom.query.filter_by(_name=item['name']).first():
                    res['skipped'] += 1
                    continue
                room = PresenceRoom(name=item['name'], description=item.get('description'),
                                    schedule_id=schedule_id.get(item.get('schedule')))
                db.session.add(room)
                db.session.flush()
                for uid in item.get('member_uids', []):
                    if uid in user_id:
                        room.members.append(User.query.get(user_id[uid]))
                db.session.commit()
                res['imported'] += 1
            except Exception as e:
                _fail(res, f"room {item.get('name')}", e)
    room_id = {r.name: r.id for r in PresenceRoom.query.all()}

    if 'presence_schedule_overrides' in data:
        res = results['presence_schedule_overrides']
        for item in data['presence_schedule_overrides']:
            try:
                day = date.fromisoformat(item['date'])
                rid = room_id.get(item['room']) if item.get('room') else None
                if PresenceScheduleOverride.query.filter_by(_date=day, _room_id=rid).first():
                    res['skipped'] += 1
                    continue
                db.session.add(PresenceScheduleOverride(date=day, room_id=rid, note=item.get('note'),
                                                        schedule_id=schedule_id.get(item.get('schedule'))))
                db.session.commit()
                res['imported'] += 1
            except Exception as e:
                _fail(res, f"override {item.get('date')}", e)

    if 'rfid_tags' in data:
        res = results['rfid_tags']
        for item in data['rfid_tags']:
            try:
                if RfidTag.query.filter_by(_tag_uid=item['tag_uid']).first():
                    res['skipped'] += 1
                    continue
                if item.get('uid') not in user_id:
                    raise ValueError(f"no user {item.get('uid')}")
                tag = RfidTag(tag_uid=item['tag_uid'], user_id=user_id[item['uid']])
                if item.get('created_at'):
                    tag._created_at = datetime.fromisoformat(item['created_at'])
                db.session.add(tag)
                db.session.commit()
                res['imported'] += 1
            except Exception as e:
                _fail(res, f"tag {item.get('tag_uid')}", e)

    period_id = {}  # old id -> new id, for events
    if 'presence_periods' in data:
        res = results['presence_periods']
        for item in data['presence_periods']:
            try:
                rid = room_id.get(item.get('room'))
                if rid is None:
                    raise ValueError(f"no room {item.get('room')}")
                start = _dt(item['start_time'])
                existing = PresencePeriod.query.filter_by(_room_id=rid, _source=item['source'],
                                                          _name=item['name'], _start_time=start).first()
                if existing:
                    period_id[item['id']] = existing.id
                    res['skipped'] += 1
                    continue
                period = PresencePeriod(room_id=rid, name=item['name'], start_time=start,
                                        duration_seconds=int(item['duration_seconds']),
                                        grace_seconds=int(item['grace_seconds']),
                                        source=item['source'], active=bool(item.get('active')))
                db.session.add(period)
                db.session.commit()
                period_id[item['id']] = period.id
                res['imported'] += 1
            except Exception as e:
                _fail(res, f"period {item.get('name')}", e)

    if 'presence_events' in data:
        res = results['presence_events']
        for item in data['presence_events']:
            try:
                if PresenceEvent.query.filter_by(_event_id=item['event_id']).first():
                    res['skipped'] += 1
                    continue
                if item.get('uid') not in user_id or item.get('room') not in room_id:
                    raise ValueError(f"no user {item.get('uid')} or room {item.get('room')}")
                db.session.add(PresenceEvent(
                    event_id=item['event_id'], source=item['source'], user_id=user_id[item['uid']],
                    room_id=room_id[item['room']], period_id=period_id.get(item.get('period_id')),
                    type=item['type'], occurred_at=_dt(item['occurred_at']), received_at=_dt(item['received_at']),
                    tag_uid=item.get('tag_uid'), device_id=item.get('device_id')))
                db.session.commit()
                res['imported'] += 1
            except Exception as e:
                _fail(res, f"event {item.get('event_id')}", e)

    if 'camera_checks' in data:
        res = results['camera_checks']
        for item in data['camera_checks']:
            try:
                if CameraCheck.query.filter_by(_event_id=item['event_id']).first():
                    res['skipped'] += 1
                    continue
                tap = PresenceEvent.query.filter_by(_event_id=item.get('tap_event_id')).first()
                if not tap:
                    raise ValueError(f"no tap {item.get('tap_event_id')}")
                db.session.add(CameraCheck(
                    event_id=item['event_id'], tap_id=tap.id, result=item['result'],
                    recognized_user_id=user_id.get(item.get('recognized_uid')), confidence=item.get('confidence'),
                    occurred_at=_dt(item['occurred_at']), received_at=_dt(item['received_at']),
                    device_id=item.get('device_id')))
                db.session.commit()
                res['imported'] += 1
            except Exception as e:
                _fail(res, f"camera check {item.get('event_id')}", e)

    return results


def presence_counts():
    """Row counts for every presence table, for migration reconciliation."""
    from model.presence import presence_room_members
    from model.presence_schedule import PresenceScheduleBlock
    return {
        'presence_schedules': PresenceSchedule.query.count(),
        'presence_schedule_blocks': PresenceScheduleBlock.query.count(),
        'presence_rooms': PresenceRoom.query.count(),
        'presence_room_members': db.session.query(presence_room_members).count(),
        'presence_schedule_overrides': PresenceScheduleOverride.query.count(),
        'rfid_tags': RfidTag.query.count(),
        'presence_periods': PresencePeriod.query.count(),
        'presence_events': PresenceEvent.query.count(),
        'camera_checks': CameraCheck.query.count(),
    }
