""" Events: the records of the Event Service, and every rule about them

An event is the parent object of the Events system (pages#2089): its page, its lifecycle, who may manage it,
who signed up and who came, and its files. Each rule lives here once. The lifecycle, the event's own roles and
every size come from dataconfigs/event_rules.json (model/event_rules.py), so the code never names a status or a
participant role. api/event.py is the way in, and api/event.http walks every request.

Tables
  events              one row per event. The fields this service enforces are columns; the rest of the definition
                      (Mort's front matter keys, the Markdown body included) is the _data JSON, so a draft made in
                      the Event Builder survives a reload.
  event_participants  one row per person per event: their role in it, their RSVP and their check-in, the
                      "Participants: RSVP and attendance" part of the event. Attendance is a column here and not a
                      Presence event: a presence event toggles enter and exit per room, so a second scan of the door
                      code would read as leaving (see the README).
  event_files         documents, gallery photos (each with alt text) and hosted video links

Times are stored as naive UTC, like presence (model/presence.py), and read out with a trailing Z.
"""
import base64
import hashlib
import hmac
import re
import secrets
from datetime import datetime, time, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from werkzeug.utils import secure_filename

from __init__ import db
from model import account_types, event_fields, event_rules, event_storage, permissions
from model.presence import to_iso_z, utc_now
from model.presence_schedule import ScheduleError, check_timezone, parse_clock, parse_date
from model.user import User

""" Messages: every error answer is {message, code}, with code one of these ids, so a page words it once """

MESSAGES = {
    'not_found': "There is no event with that id.",
    'page_taken': "An event already uses {page_path}.",
    'not_manager': "Only this event's organizers can do that.",
    'not_owner': "Only the event's owner or staff can change who organizes it.",
    'publish_role': "Only {roles} can publish an event.",
    'bad_page_path': "page_path must look like {prefix}your-event-name/",
    'page_frozen': "The address can't change after the event is published.",
    'bad_title': "title must be 1 to 120 characters",
    'bad_date': "date must be YYYY-MM-DD",
    'bad_time': "{field} must be HH:MM in quotes, like \"10:00\"",
    'end_needs_start': "end_time needs a start_time",
    'end_before_start': "end_time must be after start_time on the same date",
    'bad_timezone': "unknown timezone {timezone}",
    'bad_capacity': "registration.capacity must be a whole number of 1 or more, or null for no limit",
    'bad_registration': "registration.enabled must be true or false",
    'bad_feature': "features can only turn on {features}, each true or false",
    'bad_field': "{field} must be {kind} within {limit}",
    'bad_notice': "notice must be at most 280 characters",
    'status_in_body': "Change the status with PUT /api/event/status",
    'transition': "An event that is {status} can move to: {allowed}",
    'back_to_draft': "An event with people signed up can't go back to {status}",
    'registration_off': "Turn on registration first (registration.enabled and features.registration)",
    'not_open': "This event isn't taking {action} right now",
    'started': "Registration closed when the event started",
    'attendance_off': "Check-in is off for this event",
    'bad_code': "That code has changed. Scan the code on the screen again.",
    'delete_live': "Only a draft with no people and no files can be deleted. Archive it instead.",
    'bad_kind': "kind must be one of {kinds}",
    'bad_group': "group must be the attachment id from the event page, like workshop-gallery",
    'bad_file_type': "{kind} files can be {extensions}",
    'bad_content': "The file's contents don't match its name",
    'too_big': "Files can be up to {size}",
    'gallery_off': "The gallery is off for this event",
    'bad_alt': "Describe the image in 1 to {limit} characters (alt), so screen readers can say what it shows",
    'bad_item': "Send either a file (content) or a video link (link), not both",
    'bad_video': "Video links go in the gallery and must be https links to {hosts}",
    'no_person': "No account has the uid {uid}",
    'bad_role': "role must be one of {roles}",
    'mine_needs_login': "Sign in to see your events",
    'bad_filter': "status must be one or more of {statuses}, and since must be YYYY-MM-DD",
    'bad_id': "{field} must be a whole number, the id of an event or a file",
    'bad_flag': "{field} must be true or false",
    'storage_down': "File storage isn't answering. Try again in a minute.",
    'server_error': "Server error in the Event Service",
}

# What each status-gated action is called in not_open
ACTION_WORDS = {'edit': "changes", 'rsvp': "RSVPs", 'check_in': "check-ins", 'files': "new files", 'delete': "deleting"}


class EventError(Exception):
    """A request the rules refuse. response() is ({message, code, ...}, http_status): code is the MESSAGES key, and
    the fields travel with it as ids and numbers a page can word (id on page_taken is the event's id, allowed on
    transition the list). words fill in the message only, where its English differs from the id (not_open says
    "RSVPs" and sends action "rsvp"). The HTTP status is http_status, because transition and back_to_draft have a
    status field of their own."""

    def __init__(self, message_id, http_status=400, words=None, **fields):
        self.message_id = message_id
        self.http_status = http_status
        self.fields = fields
        filled = {**fields, **(words or {})}
        self.message = MESSAGES[message_id].format(**{key: _words(value) for key, value in filled.items()})
        super().__init__(self.message)

    def response(self):
        return {'message': self.message, 'code': self.message_id, **self.fields}, self.http_status


def _words(value):
    return ", ".join(str(item) for item in value) if isinstance(value, (list, tuple)) else str(value)


SCHEDULE_KEYS = ('date', 'start_time', 'end_time', 'timezone')  # the keys that set when an event runs

# What a participant's RSVP and check-in can be. These are the service's own words, not data: pages read them.
GOING, WAITLIST, DECLINED = 'going', 'waitlist', 'declined'
BY_QR, BY_ORGANIZER = 'qr', 'organizer'
# Mort's feature keys that switch a rule on (the features list in event_rules.json)
REGISTRATION, ATTENDANCE, GALLERY = 'registration', 'attendance', 'gallery'


def _length(model, column):
    """The size of a String column, read from the model so a number is written once."""
    return model.__table__.c[column].type.length


def _bad_field(error):
    """The bad_field answer for a field event_fields refused: its ids and numbers travel, its words fill in the message."""
    return EventError('bad_field', words=error.words, **error.fields)


def _clean(field, kind, value):
    """event_fields.clean, with a refusal answered as bad_field."""
    try:
        return event_fields.clean(field, kind, value)
    except event_fields.FieldError as error:
        raise _bad_field(error)


def _clock(value, field):
    if not isinstance(value, str) or not event_fields.CLOCK.fullmatch(value):
        raise EventError('bad_time', field=field)
    try:
        return parse_clock(value, field)
    except ScheduleError:
        raise EventError('bad_time', field=field)


def _whole_id(value, field):
    """A whole number id from a JSON body or a query, or EventError('bad_id')."""
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,18}", value):
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise EventError('bad_id', field=field)
    return value


def _epoch_seconds(moment):
    """Whole seconds since 1970 for a naive UTC datetime."""
    return int(moment.replace(tzinfo=timezone.utc).timestamp())


def _size_words(size):
    return f"{size // 1000000} MB" if size % 1000000 == 0 else f"{size / 1000000:.1f} MB"


class Event(db.Model):
    """
    Event Model

    One event: its page, its lifecycle status, its registration, the features it turns on, the organizer's notice
    and the rest of its definition. Its owner is the account that made it. participants and files belong to it and
    go with it.

    Attributes:
        _page_path (Column): the event page's permalink, like /events/community-technology-workshop/. Unique, and
            frozen once the event has been published, because it names the page's Microblog topic.
        _title (Column): the title, 1 to 120 characters.
        _starts_at, _ends_at (Column): when it runs, as naive UTC. With no start_time it runs all day.
        _timezone (Column): the zone its date and times are written in.
        _status (Column): one of the statuses in event_rules.json. Only transition() changes it.
        _registration_enabled, _capacity (Column): whether it takes RSVPs, and how many people can be going.
        _features (Column): which parts are on, from event_rules.json's features.
        _notice (Column): the organizer's banner (cancelled, moved, live updates), up to 280 characters.
        _data (Column): the rest of the definition, keyed as in DEFINITION_FIELDS, plus the date and time strings
            as written and the Spring calendar item's id.
        _owner_id (Column): the account that made it.
        _checkin_secret (Column): the key the check-in codes are made from, set while check-in is open.
        _created_at, _updated_at, _published_at (Column): when it was made, last changed, and first published.
    """
    __tablename__ = 'events'

    id = db.Column(db.Integer, primary_key=True)
    _page_path = db.Column(db.String(120), unique=True, nullable=False)
    _title = db.Column(db.String(120), nullable=False)
    _starts_at = db.Column(db.DateTime, nullable=False, index=True)
    _ends_at = db.Column(db.DateTime, nullable=False)
    _timezone = db.Column(db.String(64), nullable=False)
    _status = db.Column(db.String(16), nullable=False)
    _registration_enabled = db.Column(db.Boolean, nullable=False, default=False)
    _capacity = db.Column(db.Integer, nullable=True)
    _features = db.Column(db.JSON, nullable=False, default=dict)
    _notice = db.Column(db.String(280), nullable=True)
    _data = db.Column(db.JSON, nullable=False, default=dict)
    _owner_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    _checkin_secret = db.Column(db.String(64), nullable=True)
    _created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    _updated_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    _published_at = db.Column(db.DateTime, nullable=True)

    # Each pair is declared on both sides, so SQLAlchemy has no overlap to warn about. The owner has no backref:
    # scripts/remove_user.py stops on an account that owns an event.
    owner = db.relationship('User', foreign_keys=[_owner_id])
    participants = db.relationship('EventParticipant', back_populates='event', cascade='all, delete-orphan', lazy='dynamic')
    files = db.relationship('EventFile', back_populates='event', cascade='all, delete-orphan', lazy='dynamic')

    def __init__(self, page_path, title, owner):
        self._page_path = page_path
        self._title = title
        self.owner = owner
        self._status = event_rules.initial_status()
        self._registration_enabled = False
        self._features = {}
        self._data = {}

    def __repr__(self):
        return f"Event(id={self.id}, page_path={self._page_path}, status={self._status})"

    @property
    def page_path(self):
        return self._page_path

    @property
    def title(self):
        return self._title

    @property
    def starts_at(self):
        return self._starts_at

    @property
    def ends_at(self):
        return self._ends_at

    @property
    def timezone(self):
        return self._timezone

    @property
    def status(self):
        return self._status

    @property
    def registration_enabled(self):
        return self._registration_enabled

    @property
    def capacity(self):
        return self._capacity

    @property
    def features(self):
        """Every feature in event_rules.json, on or off. A feature the event never set is off."""
        saved = self._features or {}
        return {name: bool(saved.get(name, False)) for name in event_rules.features()}

    @property
    def notice(self):
        return self._notice

    @property
    def owner_id(self):
        return self._owner_id

    @property
    def created_at(self):
        return self._created_at

    @property
    def updated_at(self):
        return self._updated_at

    @property
    def published_at(self):
        return self._published_at

    # CRUD create: save a new event from Event.validate's values. A page_path already in use is page_taken.
    def create(self, values):
        self._apply(values)
        try:
            db.session.add(self)
            db.session.commit()
        except IntegrityError:
            Event._page_taken(values['page_path'])
        return self

    @staticmethod
    def _page_taken(page_path):
        """After a unique page_path refused a save: roll back, and answer with the event that has it."""
        db.session.rollback()
        existing = Event.by_page(page_path)
        raise EventError('page_taken', 409, page_path=page_path, id=existing.id if existing else None)

    # CRUD read: Mort's keys, the counts and the files. Never a person's name or uid, and never the secret.
    def read(self, for_user=None, include_body=True):
        data = self._data or {}
        event = {
            'id': self.id,
            'page_path': self._page_path,
            'title': self._title,
            'date': data.get('date'),
            'start_time': data.get('start_time'),
            'end_time': data.get('end_time'),
            'timezone': self._timezone,
            'starts_at': to_iso_z(self._starts_at),
            'ends_at': to_iso_z(self._ends_at),
            'status': self._status,
            'created_at': to_iso_z(self._created_at),
            'updated_at': to_iso_z(self._updated_at),
            'published_at': to_iso_z(self._published_at),
            'registration': {'enabled': self._registration_enabled, 'capacity': self._capacity},
            'features': self.features,
            'notice': self._notice,
            'counts': self.counts(),
            'files': [file.read() for file in self.files.order_by(EventFile.id)],
        }
        for key in event_fields.DEFINITION_FIELDS:
            if key in data and (include_body or key != 'body'):
                event[key] = data[key]
        if self.can_manage(for_user):
            event['calendar_item_id'] = data.get('calendar_item_id')
        return event

    # CRUD update: a manager changes the definition while the status allows edits
    def update(self, inputs, actor):
        self.require_manager(actor)
        self._require_open('edit')
        values = Event.validate(inputs, current=self)
        page_path = values.get('page_path', self._page_path)
        try:
            self._apply(values)
            self._updated_at = utc_now()
            self._promote()  # a capacity that grew moves the waitlist up, first in, first out
            db.session.commit()
        except IntegrityError:
            Event._page_taken(page_path)  # a draft moved onto another event's address
        return self

    # CRUD delete: only a draft that nobody has joined and that has no files
    def delete(self, actor):
        self.require_manager(actor)
        if not event_rules.open_for('delete', self._status) or self.participants.count() or self.files.count():
            raise EventError('delete_live', 409)
        db.session.delete(self)
        db.session.commit()
        return None

    def _apply(self, values):
        for name in ('page_path', 'title', 'starts_at', 'ends_at', 'timezone', 'registration_enabled', 'capacity', 'features', 'notice'):
            if name in values:
                setattr(self, '_' + name, values[name])
        self._data = values['data']  # a new dict, so SQLAlchemy sees the change

    def _require_open(self, action):
        if not event_rules.open_for(action, self._status):
            raise EventError('not_open', 409, action=action, words={'action': ACTION_WORDS[action]})

    def _takes_registration(self):
        return bool(self._registration_enabled) and self.features[REGISTRATION]

    def transition(self, status, actor):
        """Move the event to another status. Every rule comes from the data file; none names a status."""
        self.require_manager(actor)
        allowed = event_rules.next_statuses(self._status)
        if status not in allowed:
            raise EventError('transition', 409, status=self._status, allowed=allowed)
        publishing = event_rules.is_private(self._status) and not event_rules.is_private(status)
        if publishing and actor.role not in event_rules.publish_roles():
            raise EventError('publish_role', 403, roles=list(event_rules.publish_roles()))
        if event_rules.open_for('rsvp', status) and not self._takes_registration():
            raise EventError('registration_off', 409)
        if event_rules.is_private(status) and self.participants.filter(EventParticipant._rsvp.in_([GOING, WAITLIST])).count():
            raise EventError('back_to_draft', 409, status=status)
        now = utc_now()
        if publishing and self._published_at is None:
            self._published_at = now  # the first time only, so it stays the day it was announced
        if event_rules.open_for('check_in', status):
            self._checkin_secret = self._checkin_secret or secrets.token_hex(32)
        else:
            self._checkin_secret = None  # the codes stop working
        self._status = status
        self._updated_at = now
        db.session.commit()
        return self

    """ Who may do what """

    def can_manage(self, user):
        """Staff, the owner, or a participant whose role is one of manager_roles."""
        if user is None:
            return False
        if user.is_staff() or user.id == self._owner_id:
            return True
        row = self._participant(user)
        return bool(row and event_rules.is_manager_role(row.role))

    def require_manager(self, user):
        if not self.can_manage(user):
            raise EventError('not_manager', 403)

    def _may_change_roles(self, user):
        return user.is_staff() or user.id == self._owner_id

    def visible_to(self, user):
        """A draft reaches only its managers. Every read goes through this, files included."""
        return not event_rules.is_private(self._status) or self.can_manage(user)

    @staticmethod
    def can_create(user):
        """Whether this account may make an event, as permissions.json says for POST /api/event."""
        if user is None or not account_types.request_allowed(user.role, 'POST', '/api/event'):
            return False
        who = permissions.who_may_call('POST', '/api/event')
        return who in ('anyone', 'signed_in') or (isinstance(who, list) and user.role in who)

    def viewer(self, user):
        """What the caller is to this event: None for an anonymous caller."""
        if user is None:
            return None
        row = self._participant(user)
        view = {
            'signed_in': True,
            'can_create': Event.can_create(user),
            'manages': self.can_manage(user),
            'rsvp': row.rsvp if row else None,
            'waitlist_position': self._waitlist_position(row),
            'checked_in': bool(row and row.checked_in_at),
        }
        if view['manages']:
            view['next_statuses'] = event_rules.next_statuses(self._status)
        return view

    @staticmethod
    def caller_view(user):
        """viewer() for a list, or for a page with no event yet, where managing means being able to set one up."""
        if user is None:
            return None
        can_create = Event.can_create(user)
        return {'signed_in': True, 'can_create': can_create, 'manages': can_create, 'rsvp': None,
                'waitlist_position': None, 'checked_in': False}

    """ RSVPs and the waitlist """

    def _participant(self, user):
        return None if user is None else self.participants.filter_by(_user_id=user.id).first()

    def _waitlist_position(self, row):
        if row is None or row.rsvp != WAITLIST:
            return None
        ahead = self.participants.filter(EventParticipant._rsvp == WAITLIST,
                                         or_(EventParticipant._rsvp_at < row.rsvp_at,
                                             (EventParticipant._rsvp_at == row.rsvp_at) & (EventParticipant.id < row.id))).count()
        return ahead + 1

    def _going(self):
        """How many people are going: what capacity limits."""
        return self.participants.filter_by(_rsvp=GOING).count()

    def counts(self):
        going = self._going()
        return {
            'going': going,
            'waitlist': self.participants.filter_by(_rsvp=WAITLIST).count(),
            'checked_in': self.participants.filter(EventParticipant._checked_in_at.isnot(None)).count(),
            'spots_left': None if self._capacity is None else max(self._capacity - going, 0),
        }

    def roster(self):
        """Everyone in the event, by when they signed up (walk-ins last), then by name."""
        rows = self.participants.all()
        return sorted(rows, key=lambda row: (row.rsvp_at is None, row.rsvp_at or datetime.min, (row.user.name or "").lower()))

    def _lock(self):
        """End the current transaction and lock this event's row until the next commit, so two RSVPs at once
        (gunicorn runs several workers) cannot both take the last spot. Ending the transaction first means the
        counts read after the lock see every RSVP already saved."""
        db.session.commit()
        Event.query.filter_by(id=self.id).with_for_update().first()

    def _promote(self):
        """Move waitlisted people to going, first in, first out, while there are spots."""
        going = self._going()
        waiting = self.participants.filter_by(_rsvp=WAITLIST).order_by(EventParticipant._rsvp_at, EventParticipant.id)
        for row in waiting:
            if self._capacity is not None and going >= self._capacity:
                break
            row.rsvp = GOING
            going += 1

    def rsvp(self, user, now=None):
        """Sign the caller up: going while there are spots, else the waitlist. Someone already going or waiting keeps
        their place, so a repeated RSVP is safe."""
        now = now or utc_now()
        if not self.visible_to(user):
            raise EventError('not_found', 404)
        self._require_open('rsvp')
        if not self._takes_registration():
            raise EventError('registration_off', 409)
        if now >= self._starts_at:
            raise EventError('started', 409)
        self._lock()
        row = self._participant(user)
        if row is not None and row.rsvp in (GOING, WAITLIST):
            db.session.commit()
            return row
        if row is None:
            row = EventParticipant(self, user)
            db.session.add(row)
        row.rsvp = GOING if self._capacity is None or self._going() < self._capacity else WAITLIST
        row.rsvp_at = now
        db.session.commit()
        return row

    def cancel_rsvp(self, user):
        """The caller is no longer coming: declined, and the first person waiting moves up. Returns the caller's RSVP
        afterwards: declined, or None when they never signed up."""
        if not self.visible_to(user):
            raise EventError('not_found', 404)
        self._lock()
        row = self._participant(user)
        if row is not None and row.rsvp in (GOING, WAITLIST):
            row.rsvp = DECLINED
            db.session.flush()
            self._promote()
        db.session.commit()
        return row.rsvp if row else None

    """ Check-in: a code on the organizer's screen that changes every check_in_code_seconds """

    def _require_check_in(self):
        self._require_open('check_in')
        if not self.features[ATTENDANCE]:
            raise EventError('attendance_off', 409)

    def _code(self, period):
        """The code for one period: nothing is kept in memory, so every worker gives the same code."""
        digest = hmac.new(self._checkin_secret.encode(), f"{self.id}:{period}".encode(), hashlib.sha256).digest()
        return base64.b32encode(digest).decode()[:8]

    def check_in_code(self, now=None):
        """(code, seconds_left) for the organizer's screen."""
        self._require_check_in()
        if not self._checkin_secret:  # an event that entered check-in before the secret existed, or restored without it
            self._checkin_secret = secrets.token_hex(32)
            db.session.commit()
        moment, seconds = _epoch_seconds(now or utc_now()), event_rules.code_seconds()
        return self._code(moment // seconds), seconds - moment % seconds

    def check_in(self, user, code, now=None):
        """Check the caller in with the code on the screen: this period's or the one before, so a code is good for
        at most two periods. Someone who never RSVPed joins as a walk-in. A second check-in keeps the first time."""
        now = now or utc_now()
        if not self.visible_to(user):
            raise EventError('not_found', 404)
        self._require_check_in()
        period = _epoch_seconds(now) // event_rules.code_seconds()
        given = (code.strip().upper() if isinstance(code, str) else "").encode()
        if not self._checkin_secret or not any(hmac.compare_digest(given, self._code(p).encode()) for p in (period, period - 1)):
            raise EventError('bad_code', 400)
        row = self._participant(user)
        if row is None:
            row = EventParticipant(self, user)
            db.session.add(row)
        if row.checked_in_at is None:
            row.checked_in_at = now
            row.check_in_method = BY_QR
        db.session.commit()
        return row

    """ The roster, managed by the organizers """

    @staticmethod
    def _person(uid):
        person = User.query.filter_by(_uid=uid).first() if isinstance(uid, str) else None
        if person is None:
            raise EventError('no_person', 404, uid=uid)
        return person

    def set_participant(self, actor, uid, checked_in=None, role=None):
        """Check someone in by name (or clear it), or give them a role in the event (owner or staff only)."""
        self.require_manager(actor)
        if role is not None:
            if not self._may_change_roles(actor):
                raise EventError('not_owner', 403)
            if role not in event_rules.participant_roles():
                raise EventError('bad_role', roles=list(event_rules.participant_roles()))
        if checked_in is not None and not isinstance(checked_in, bool):
            raise EventError('bad_flag', field='checked_in')
        person = Event._person(uid)
        row = self._participant(person)
        if row is None:
            row = EventParticipant(self, person)
            db.session.add(row)
        if role is not None:
            row.role = role
        if checked_in is True and row.checked_in_at is None:
            row.checked_in_at = utc_now()
            row.check_in_method = BY_ORGANIZER
        elif checked_in is False:
            row.checked_in_at = None
            row.check_in_method = None
        db.session.commit()
        return row

    def remove_participant(self, actor, uid):
        """Take someone out of the event, and move the waitlist up. Removing an organizer is the owner's or staff's."""
        self.require_manager(actor)
        row = self._participant(Event._person(uid))
        if row is None:
            return None
        if event_rules.is_manager_role(row.role) and not self._may_change_roles(actor):
            raise EventError('not_owner', 403)
        db.session.delete(row)
        db.session.flush()
        self._promote()
        db.session.commit()
        return None

    """ Files: documents, gallery photos with alt text, and hosted video links """

    @staticmethod
    def _alt(alt, required):
        """Alt text as it is saved. A gallery photo needs it; for anything else it is optional."""
        limit = _length(EventFile, '_alt')
        text = alt.strip() if isinstance(alt, str) else None
        if (alt is not None and not isinstance(alt, str)) or (text and len(text) > limit) or (required and not text):
            raise EventError('bad_alt', limit=limit)
        return text or None

    def add_file(self, actor, group, kind, name, content=None, link=None, alt=None):
        """Add an upload (content, base64) or a hosted video (link) to one of the event page's attachment groups."""
        self.require_manager(actor)
        self._require_open('files')
        rule = event_rules.file_kind(kind) if isinstance(kind, str) else None
        if rule is None:
            raise EventError('bad_kind', kinds=list(event_rules.kinds()))
        if kind == GALLERY and not self.features[GALLERY]:
            raise EventError('gallery_off', 409)
        if not isinstance(group, str) or not event_fields.GROUP.fullmatch(group):
            raise EventError('bad_group')
        if (content is None) == (link is None):
            raise EventError('bad_item')
        if link is not None:
            return self._add_video(actor, group, kind, name, link)

        # The extension comes from the name as sent: secure_filename drops letters outside ASCII, which would lose it
        sent = name.strip() if isinstance(name, str) else ""
        extension = sent.rsplit(".", 1)[1].lower() if "." in sent else ""
        if extension not in rule['extensions']:
            raise EventError('bad_file_type', kind=kind, extensions=rule['extensions'])
        stem = secure_filename(sent.rsplit(".", 1)[0])[:255 - len(extension) - 1] or "file"
        filename = f"{stem}.{extension}"  # a safe name, up to 255 characters, that keeps its type
        data = Event._decode(content)
        if len(data) > rule['max_bytes']:
            raise EventError('too_big', 413, max_bytes=rule['max_bytes'], words={'size': _size_words(rule['max_bytes'])})
        check = event_fields.SIGNATURES.get(extension)
        if check is None or not check(data):
            raise EventError('bad_content')
        alt_text = Event._alt(alt, required=kind == GALLERY)

        key = f"{self.id}/{uuid4().hex}.{extension}"
        content_type = event_storage.EXT_TYPES[extension]
        event_storage.STORE.save(key, data, content_type)  # the file first, then the row that points at it
        row = EventFile(self, group=group, kind=kind, name=filename, uploader_uid=actor.uid,
                        alt=alt_text, key=key, content_type=content_type, size=len(data))
        try:
            db.session.add(row)
            db.session.commit()
        except Exception:
            db.session.rollback()
            try:
                event_storage.STORE.remove(key)  # no row points at it now
            except event_storage.EventStorageError:
                pass
            raise
        return row

    @staticmethod
    def _decode(content):
        """The bytes of a base64 upload. A data: prefix, as a browser's FileReader makes, is fine."""
        if not isinstance(content, str):
            raise EventError('bad_content')
        if content.startswith("data:") and "," in content:
            content = content.split(",", 1)[1]
        try:
            return base64.b64decode(content, validate=True)
        except (ValueError, TypeError):
            raise EventError('bad_content')

    def _add_video(self, actor, group, kind, name, link):
        saved = event_fields.video_link(link, _length(EventFile, '_link')) if kind == GALLERY else None
        if saved is None:
            raise EventError('bad_video', hosts=list(event_rules.video_hosts()))
        title = name.strip() if isinstance(name, str) else ""
        if not title or len(title) > event_rules.limit('text'):
            raise _bad_field(event_fields.FieldError('name', 'name', event_rules.limit('text')))
        row = EventFile(self, group=group, kind=kind, name=title, uploader_uid=actor.uid, link=saved)
        db.session.add(row)
        db.session.commit()
        return row

    def _file(self, file_id):
        row = self.files.filter_by(id=file_id).first()
        if row is None:
            raise EventError('not_found', 404)
        return row

    def set_file_alt(self, actor, file_id, alt):
        self.require_manager(actor)
        row = self._file(file_id)
        row.alt = Event._alt(alt, required=row.is_photo)
        db.session.commit()
        return row

    def remove_file(self, actor, file_id):
        """Delete the row and the stored file. If the store does not answer, nothing changes (the API rolls back)."""
        self.require_manager(actor)
        row = self._file(file_id)
        db.session.delete(row)
        if row.key:
            event_storage.STORE.remove(row.key)
        db.session.commit()
        return None

    """ Finding events """

    @staticmethod
    def page_path_pattern():
        return f"^{re.escape(event_rules.page_prefix())}[a-z0-9]+(-[a-z0-9]+)*/$"

    @staticmethod
    def options():
        """What GET /api/event/options answers: the data file's public settings, the page_path pattern validate
        uses, and the column sizes this model owns, so a page checks with the same numbers."""
        settings = event_rules.public_settings()
        settings['page_path_pattern'] = Event.page_path_pattern()
        settings['limits'].update({'title': _length(Event, '_title'), 'page_path': _length(Event, '_page_path'),
                                   'notice': _length(Event, '_notice'), 'alt': _length(EventFile, '_alt')})
        return settings

    @staticmethod
    def by_page(page_path):
        return Event.query.filter_by(_page_path=page_path).first() if isinstance(page_path, str) else None

    @staticmethod
    def by_id(event_id):
        return db.session.get(Event, event_id)

    @staticmethod
    def find(event_id, user, field='id'):
        """The event with this id, when the caller may see it. A draft the caller cannot see is not_found."""
        event = Event.by_id(_whole_id(event_id, field))
        if event is None or not event.visible_to(user):
            raise EventError('not_found', 404)
        return event

    @staticmethod
    def find_file(file_id, user):
        """(event, file) for a file the caller may see."""
        row = db.session.get(EventFile, _whole_id(file_id, 'id'))
        if row is None or not row.event.visible_to(user):
            raise EventError('not_found', 404)
        return row.event, row

    @staticmethod
    def filters(status_text=None, since_text=None):
        """(statuses, since) from ?status=a,b and ?since=YYYY-MM-DD, or EventError('bad_filter')."""
        bad = EventError('bad_filter', statuses=list(event_rules.statuses()))
        statuses = None
        if status_text is not None:
            statuses = [part.strip() for part in str(status_text).split(",") if part.strip()]
            if not statuses or any(part not in event_rules.statuses() for part in statuses):
                raise bad
        since = None
        if since_text is not None:
            try:
                if not event_fields.DATE.fullmatch(str(since_text)):
                    raise ScheduleError("since")
                since = parse_date(since_text, "since")
            except ScheduleError:
                raise bad
        return statuses, since

    @staticmethod
    def listing(statuses=None, since=None, managed_by=None):
        """Events by start: every event that is not private, or (managed_by) the ones the caller manages, drafts
        included. since keeps the events that end on or after that date's midnight in default_timezone."""
        query = Event.query
        if managed_by is None:
            query = query.filter(Event._status.notin_([s for s in event_rules.statuses() if event_rules.is_private(s)]))
        elif not managed_by.is_staff():
            managed = (db.session.query(EventParticipant._event_id)
                       .filter(EventParticipant._user_id == managed_by.id,
                               EventParticipant._role.in_([r for r in event_rules.participant_roles() if event_rules.is_manager_role(r)])))
            query = query.filter(or_(Event._owner_id == managed_by.id, Event.id.in_(managed)))
        if statuses:
            query = query.filter(Event._status.in_(statuses))
        if since is not None:
            midnight = datetime.combine(since, time(0), tzinfo=ZoneInfo(event_rules.default_timezone()))
            query = query.filter(Event._ends_at >= midnight.astimezone(timezone.utc).replace(tzinfo=None))
        return query.order_by(Event._starts_at, Event.id).all()

    """ Checking what a request sends """

    @staticmethod
    def validate(inputs, current=None):
        """The values to save from a create (current is None) or an update of current: the rules for each of Mort's
        keys. Unknown keys are dropped. On an update a key that is missing stays as it is, and null clears it.
        Raises EventError naming the first rule broken."""
        if 'status' in inputs:
            raise EventError('status_in_body')
        creating = current is None
        values = {}

        if creating or 'page_path' in inputs:
            page_path = inputs.get('page_path')
            if not (isinstance(page_path, str) and len(page_path) <= _length(Event, '_page_path')
                    and re.fullmatch(Event.page_path_pattern(), page_path)):
                raise EventError('bad_page_path', prefix=event_rules.page_prefix())
            if current is not None and page_path != current.page_path and current.published_at is not None:
                raise EventError('page_frozen', 409)
            values['page_path'] = page_path

        if creating or 'title' in inputs:
            title = inputs.get('title').strip() if isinstance(inputs.get('title'), str) else ""
            if not title or len(title) > _length(Event, '_title'):
                raise EventError('bad_title')
            values['title'] = title

        data = dict(current._data or {}) if current is not None else {}
        if creating or any(key in inputs for key in SCHEDULE_KEYS):
            written = {key: data.get(key) for key in SCHEDULE_KEYS[:3]}
            written['timezone'] = current.timezone if current is not None else None
            written.update({key: inputs[key] for key in SCHEDULE_KEYS if key in inputs})
            values.update(Event._schedule(written))
            data.update({key: written[key] for key in SCHEDULE_KEYS[:3]})

        if creating or 'registration' in inputs:
            registration = inputs.get('registration')
            registration = {} if registration is None else registration
            if not isinstance(registration, dict):
                raise EventError('bad_registration')
            enabled = registration.get('enabled', current.registration_enabled if current is not None else False)
            if not isinstance(enabled, bool):
                raise EventError('bad_registration')
            capacity = registration['capacity'] if 'capacity' in registration else (current.capacity if current is not None else None)
            if capacity is not None and (isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1):
                raise EventError('bad_capacity')
            values['registration_enabled'], values['capacity'] = enabled, capacity

        if creating or 'features' in inputs:
            features = inputs.get('features')
            features = {} if features is None else features
            if not isinstance(features, dict) or any(key not in event_rules.features() or not isinstance(on, bool) for key, on in features.items()):
                raise EventError('bad_feature', features=list(event_rules.features()))
            values['features'] = {name: features.get(name, False) for name in event_rules.features()}

        if 'notice' in inputs:
            notice = inputs['notice']
            if notice is not None and (not isinstance(notice, str) or len(notice) > _length(Event, '_notice')):
                raise EventError('bad_notice')
            values['notice'] = notice or None

        for key, kind in event_fields.DEFINITION_FIELDS.items():
            if key in inputs:
                if inputs[key] is None:
                    data.pop(key, None)
                else:
                    data[key] = _clean(key, kind, inputs[key])
        if data.get('image') and not data.get('image_alt'):
            raise EventError('bad_alt', limit=event_rules.limit('text'))
        if 'calendar_item_id' in inputs:
            item = inputs['calendar_item_id']
            if item is not None and (isinstance(item, bool) or not isinstance(item, int) or item < 1):
                raise _bad_field(event_fields.FieldError('calendar_item_id', 'id'))
            data['calendar_item_id'] = item
        values['data'] = data
        return values

    @staticmethod
    def _schedule(written):
        """{timezone, starts_at, ends_at} from the date and times as written. No start_time means all day, from
        00:00 to the next 00:00; a start with no end runs to the end of that day."""
        if not isinstance(written['date'], str) or not event_fields.DATE.fullmatch(written['date']):
            raise EventError('bad_date')
        try:
            day = parse_date(written['date'])
        except ScheduleError:
            raise EventError('bad_date')
        zone = written['timezone'] if written['timezone'] is not None else event_rules.default_timezone()
        try:
            if not isinstance(zone, str):
                raise ScheduleError(zone)
            check_timezone(zone)
        except ScheduleError:
            raise EventError('bad_timezone', timezone=zone)
        start = _clock(written['start_time'], 'start_time') if written['start_time'] is not None else None
        end = _clock(written['end_time'], 'end_time') if written['end_time'] is not None else None
        if end is not None and start is None:
            raise EventError('end_needs_start')
        if start is not None and end is not None and end <= start:
            raise EventError('end_before_start')
        place = ZoneInfo(zone)
        begins = datetime.combine(day, start or time(0), tzinfo=place)
        finishes = datetime.combine(day, end, tzinfo=place) if end is not None else datetime.combine(day + timedelta(days=1), time(0), tzinfo=place)
        as_utc = lambda moment: moment.astimezone(timezone.utc).replace(tzinfo=None)
        return {'timezone': zone, 'starts_at': as_utc(begins), 'ends_at': as_utc(finishes)}


class EventParticipant(db.Model):
    """
    EventParticipant Model

    One person in one event: their role in it (participant_roles in event_rules.json), their RSVP (going,
    waitlist, declined, or null for a walk-in) and their check-in (qr from the door code, organizer by name).
    Removing a user removes their rows.
    """
    __tablename__ = 'event_participants'
    __table_args__ = (db.UniqueConstraint('_event_id', '_user_id', name='uq_event_participant'),)

    id = db.Column(db.Integer, primary_key=True)
    _event_id = db.Column(db.Integer, db.ForeignKey('events.id'), nullable=False, index=True)
    _user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    _role = db.Column(db.String(16), nullable=False)
    _rsvp = db.Column(db.String(16), nullable=True)
    _rsvp_at = db.Column(db.DateTime, nullable=True)
    _checked_in_at = db.Column(db.DateTime, nullable=True)
    _check_in_method = db.Column(db.String(16), nullable=True)

    event = db.relationship('Event', back_populates='participants')
    # Deleting a user deletes their rows here (the pattern of model/user_preferences.py)
    user = db.relationship('User', backref=db.backref('event_participations', cascade='all, delete-orphan', lazy='dynamic'))

    def __init__(self, event, user):
        self.event = event
        self.user = user
        self._role = event_rules.initial_role()  # set here, not as a column default, so no role is named in code

    @property
    def event_id(self):
        return self._event_id

    @property
    def user_id(self):
        return self._user_id

    # Event sets these as RSVPs, check-ins and roles change; the rules for them live in Event
    @property
    def role(self):
        return self._role

    @role.setter
    def role(self, value):
        self._role = value

    @property
    def rsvp(self):
        return self._rsvp

    @rsvp.setter
    def rsvp(self, value):
        self._rsvp = value

    @property
    def rsvp_at(self):
        return self._rsvp_at

    @rsvp_at.setter
    def rsvp_at(self, value):
        self._rsvp_at = value

    @property
    def checked_in_at(self):
        return self._checked_in_at

    @checked_in_at.setter
    def checked_in_at(self, value):
        self._checked_in_at = value

    @property
    def check_in_method(self):
        return self._check_in_method

    @check_in_method.setter
    def check_in_method(self, value):
        self._check_in_method = value

    def read(self):
        return {
            'uid': self.user.uid,
            'name': self.user.name,
            'role': self._role,
            'rsvp': self._rsvp,
            'rsvp_at': to_iso_z(self._rsvp_at),
            'checked_in_at': to_iso_z(self._checked_in_at),
            'check_in_method': self._check_in_method,
        }


class EventFile(db.Model):
    """
    EventFile Model

    A document, a gallery photo or a hosted video link, in one of the event page's attachment groups. A stored
    file has a key in the file store (model/event_storage.py); a hosted video has only its link. The uploader is
    kept as a uid, not a foreign key, so removing a user never stops on their photos.
    """
    __tablename__ = 'event_files'

    id = db.Column(db.Integer, primary_key=True)
    _event_id = db.Column(db.Integer, db.ForeignKey('events.id'), nullable=False, index=True)
    _group = db.Column(db.String(64), nullable=False)
    _kind = db.Column(db.String(16), nullable=False)
    _name = db.Column(db.String(255), nullable=False)
    _alt = db.Column(db.String(200), nullable=True)
    _key = db.Column(db.String(255), unique=True, nullable=True)
    _link = db.Column(db.String(500), nullable=True)
    _content_type = db.Column(db.String(100), nullable=True)
    _size = db.Column(db.Integer, nullable=True)
    _uploader_uid = db.Column(db.String(64), nullable=False)
    _created_at = db.Column(db.DateTime, nullable=False, default=utc_now)

    event = db.relationship('Event', back_populates='files')

    def __init__(self, event, group, kind, name, uploader_uid, alt=None, key=None, link=None, content_type=None, size=None):
        self.event = event
        self._group = group
        self._kind = kind
        self._name = name
        self._uploader_uid = uploader_uid
        self._alt = alt
        self._key = key
        self._link = link
        self._content_type = content_type
        self._size = size

    @property
    def event_id(self):
        return self._event_id

    @property
    def group(self):
        return self._group

    @property
    def kind(self):
        return self._kind

    @property
    def name(self):
        return self._name

    @property
    def alt(self):
        return self._alt

    @alt.setter
    def alt(self, value):  # Event.set_file_alt checks it first
        self._alt = value

    @property
    def key(self):
        return self._key

    @property
    def link(self):
        return self._link

    @property
    def content_type(self):
        return self._content_type

    @property
    def size(self):
        return self._size

    @property
    def uploader_uid(self):
        return self._uploader_uid

    @property
    def created_at(self):
        return self._created_at

    @property
    def extension(self):
        return self._key.rsplit(".", 1)[1] if self._key and "." in self._key else None

    @property
    def is_photo(self):
        """A file in the gallery (not a hosted video): it needs alt text."""
        return self._kind == GALLERY and self._key is not None

    def answer(self):
        """The response that sends the file: its bytes, or a redirect to S3. A hosted video has nothing stored."""
        response = None
        if self._key is not None:
            response = event_storage.STORE.answer(self._key, self._name, self._content_type, event_rules.is_inline(self.extension))
        if response is None:
            raise EventError('not_found', 404)
        return response

    def read(self):
        created = to_iso_z(self._created_at)
        if self._key is None:
            return {'id': self.id, 'group': self._group, 'kind': self._kind, 'name': self._name, 'link': self._link, 'created_at': created}
        return {'id': self.id, 'group': self._group, 'kind': self._kind, 'name': self._name, 'alt': self._alt, 'size': self._size,
                'content_type': self._content_type, 'url': f"/api/event/files?id={self.id}", 'created_at': created}
