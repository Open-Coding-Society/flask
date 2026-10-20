"""Schedules for the presence system, stored in the database so they can be
edited on demand (see the /api/presence/schedules endpoints) instead of
being hardcoded.

A schedule is a named set of blocks ("Period 1", 08:35-09:41 on mon,tue,thu,fri).
A room follows one schedule. A schedule override replaces the schedule for one
date, for one room or for every room, so special days (minimum days,
assemblies, no school) need no code change. A manually started period still
takes precedence over all of this (see api/presence_api.py).

Block times are local wall-clock times in the schedule's timezone (or
PRESENCE_TIMEZONE, default America/Los_Angeles), since the server may run in
UTC (e.g. in Docker). Timestamps elsewhere are stored as naive UTC.

The first schedules come from model/presence_schedules.json via
seed_schedules(), which only runs when no schedules exist yet.
"""

import json
import os
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from __init__ import db

DEFAULT_TIMEZONE = os.environ.get("PRESENCE_TIMEZONE") or "America/Los_Angeles"
DEFAULT_GRACE_SECONDS = 300
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")  # index = date.weekday()
SEED_FILE = os.path.join(os.path.dirname(__file__), "presence_schedules.json")


class ScheduleError(ValueError):
    """Bad schedule input; the message is safe to show to the API caller."""


def parse_weekdays(value):
    """'mon,tue' or ['mon', 'tue'] -> 'mon,tue' (validated, in week order)."""
    items = value.split(",") if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)):
        raise ScheduleError("weekdays must be a list or a comma-separated string, e.g. 'mon,wed'")
    days = {str(d).strip().lower()[:3] for d in items if str(d).strip()}
    unknown = days - set(WEEKDAYS)
    if not days or unknown:
        raise ScheduleError(f"weekdays must use {', '.join(WEEKDAYS)}")
    return ",".join(d for d in WEEKDAYS if d in days)


def parse_clock(value, field):
    """'08:35' -> time(8, 35)."""
    try:
        return datetime.strptime(str(value).strip(), "%H:%M").time()
    except ValueError:
        raise ScheduleError(f"{field} must be HH:MM (24-hour), got {value!r}")


def check_timezone(name):
    if name is None:
        return None
    try:
        ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError):
        raise ScheduleError(f"unknown timezone {name!r}")
    return str(name)


class PresenceSchedule(db.Model):
    __tablename__ = 'presence_schedules'

    id = db.Column(db.Integer, primary_key=True)
    _name = db.Column(db.String(80), unique=True, nullable=False)
    _timezone = db.Column(db.String(64), nullable=True)  # None -> DEFAULT_TIMEZONE
    _grace_seconds = db.Column(db.Integer, nullable=False, default=DEFAULT_GRACE_SECONDS)

    blocks = db.relationship('PresenceScheduleBlock', backref='schedule', cascade='all, delete-orphan',
                             order_by='PresenceScheduleBlock._start_time')

    def __init__(self, name, timezone=None, grace_seconds=DEFAULT_GRACE_SECONDS):
        self._name = name
        self._timezone = timezone
        self._grace_seconds = grace_seconds

    @property
    def name(self): return self._name

    @property
    def grace_seconds(self): return self._grace_seconds

    @property
    def zone(self):
        return ZoneInfo(self._timezone or DEFAULT_TIMEZONE)

    def set_blocks(self, blocks_data):
        """Replaces all blocks from a list of {name, weekdays, start, end}."""
        if not isinstance(blocks_data, list):
            raise ScheduleError("blocks must be a list")
        new_blocks = []
        for i, block in enumerate(blocks_data):
            if not isinstance(block, dict) or not str(block.get("name") or "").strip():
                raise ScheduleError(f"block {i + 1} needs a name")
            start = parse_clock(block.get("start"), f"block {i + 1} start")
            end = parse_clock(block.get("end"), f"block {i + 1} end")
            if end <= start:
                raise ScheduleError(f"block {i + 1} must end after it starts")
            new_blocks.append(PresenceScheduleBlock(
                name=str(block["name"]).strip()[:80],
                weekdays=parse_weekdays(block.get("weekdays")),
                start_time=start,
                end_time=end,
            ))
        self.blocks = new_blocks

    def block_at(self, local_dt):
        """The block that contains local_dt (aware, in this schedule's zone), or None."""
        day = WEEKDAYS[local_dt.weekday()]
        now = local_dt.time()
        for block in self.blocks:
            if day in block.weekdays.split(",") and block.start_time <= now < block.end_time:
                return block
        return None

    def to_dict(self):
        return {
            'id': self.id,
            'name': self._name,
            'timezone': self._timezone or DEFAULT_TIMEZONE,
            'grace_seconds': self._grace_seconds,
            'blocks': [b.to_dict() for b in self.blocks],
        }


class PresenceScheduleBlock(db.Model):
    __tablename__ = 'presence_schedule_blocks'

    id = db.Column(db.Integer, primary_key=True)
    _schedule_id = db.Column(db.Integer, db.ForeignKey('presence_schedules.id'), nullable=False)
    _name = db.Column(db.String(80), nullable=False)
    _weekdays = db.Column(db.String(32), nullable=False)  # e.g. 'mon,tue,thu,fri'
    _start_time = db.Column(db.Time, nullable=False)
    _end_time = db.Column(db.Time, nullable=False)

    def __init__(self, name, weekdays, start_time, end_time):
        self._name = name
        self._weekdays = weekdays
        self._start_time = start_time
        self._end_time = end_time

    @property
    def name(self): return self._name

    @property
    def weekdays(self): return self._weekdays

    @property
    def start_time(self): return self._start_time

    @property
    def end_time(self): return self._end_time

    def to_dict(self):
        return {
            'name': self._name,
            'weekdays': self._weekdays,
            'start': self._start_time.strftime("%H:%M"),
            'end': self._end_time.strftime("%H:%M"),
        }


class PresenceScheduleOverride(db.Model):
    """Replaces the schedule on one date. room_id None means every room;
    schedule_id None means no scheduled periods that day."""
    __tablename__ = 'presence_schedule_overrides'

    id = db.Column(db.Integer, primary_key=True)
    _date = db.Column(db.Date, nullable=False, index=True)
    _room_id = db.Column(db.Integer, db.ForeignKey('presence_rooms.id'), nullable=True)
    _schedule_id = db.Column(db.Integer, db.ForeignKey('presence_schedules.id'), nullable=True)
    _note = db.Column(db.String(120), nullable=True)

    room = db.relationship('PresenceRoom')
    schedule = db.relationship('PresenceSchedule')

    def __init__(self, date, room_id=None, schedule_id=None, note=None):
        self._date = date
        self._room_id = room_id
        self._schedule_id = schedule_id
        self._note = note

    @property
    def date(self): return self._date

    @property
    def room_id(self): return self._room_id

    def to_dict(self):
        return {
            'id': self.id,
            'date': self._date.isoformat(),
            'room_id': self._room_id,
            'room': self.room.name if self.room else None,
            'schedule_id': self._schedule_id,
            'schedule': self.schedule.name if self.schedule else None,
            'note': self._note,
        }


def schedule_for(room, local_date):
    """The schedule a room follows on local_date: a room override, else an
    every-room override, else the room's own schedule. None = no periods."""
    overrides = PresenceScheduleOverride.query.filter(PresenceScheduleOverride._date == local_date).all()
    for wanted_room_id in (room.id, None):
        for override in overrides:
            if override.room_id == wanted_room_id:
                return override.schedule
    return room.schedule


def scheduled_block_at(room, utc_dt):
    """(schedule, block, start_utc, end_utc) for the scheduled block that
    contains utc_dt (naive UTC) in this room, or None. The local date is taken
    in the room's own schedule zone so an override date means the room's day."""
    base_zone = room.schedule.zone if room.schedule else ZoneInfo(DEFAULT_TIMEZONE)
    aware = utc_dt.replace(tzinfo=timezone.utc)
    schedule = schedule_for(room, aware.astimezone(base_zone).date())
    if not schedule:
        return None

    local_dt = aware.astimezone(schedule.zone)
    block = schedule.block_at(local_dt)
    if not block:
        return None
    start_local = datetime.combine(local_dt.date(), block.start_time, tzinfo=schedule.zone)
    end_local = datetime.combine(local_dt.date(), block.end_time, tzinfo=schedule.zone)
    to_utc = lambda dt: dt.astimezone(timezone.utc).replace(tzinfo=None)
    return schedule, block, to_utc(start_local), to_utc(end_local)


def seed_schedules(path=SEED_FILE):
    """Creates the schedules in presence_schedules.json if there are none yet.
    Returns the number created. Safe to run more than once."""
    if PresenceSchedule.query.first():
        return 0
    with open(path) as f:
        seed = json.load(f)
    created = 0
    for item in seed.get("schedules", []):
        schedule = PresenceSchedule(
            name=item["name"],
            timezone=check_timezone(item.get("timezone")),
            grace_seconds=int(item.get("grace_seconds", DEFAULT_GRACE_SECONDS)),
        )
        schedule.set_blocks(item.get("blocks", []))
        db.session.add(schedule)
        created += 1
    db.session.commit()
    return created


def parse_date(value, field="date"):
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ScheduleError(f"{field} must be YYYY-MM-DD, got {value!r}")
