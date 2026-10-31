"""
Presence API: one input-agnostic endpoint that every presence signal (RFID
tap, camera result) posts to, plus rooms, schedules, and the period logic
that turns those events into presence states. Generic on purpose: a room can
be a classroom, a club meeting, or an event.

Devices authenticate with the shared X-API-Key (RFID_API_KEY), the same
device-to-server pattern as /api/rfid/scan, which is a thin wrapper around
record_event() below. Admin/Teacher actions use the normal OCS login.
Any logged-in person can enroll themselves in one period of a room
(/enroll, see PresenceEnroll).

RFID tap body:
    {
      "event_id": "<uuid from the device>",     # retries with the same id are not logged twice
      "source": "rfid",
      "room_id": 1,
      "occurred_at": "2026-10-05T18:42:07.123Z",  # must include Z or an offset
      "tag_uid": "...",
      "device_id": "pi4-room1"                  # optional
    }

The server derives the tap type: 'enter'/'exit' toggle per user, room, and
period (so it resets every period instead of carrying over), or 'ignored' if
occurred_at falls outside every period window of that room.

Camera result body (the camera's answer for one tap, see Jarvis3000#17):
    {
      "event_id": "<uuid for this result>",
      "source": "camera",
      "room_id": 1,
      "tap_event_id": "<event_id of the tap it answers>",
      "occurred_at": "<when the picture was taken>",
      "result": "match" | "no_match" | "no_face" | "not_enrolled",
      "uid": "...",                             # who was recognized; required for match
      "confidence": 0.93,                       # optional, 0 to 1
      "device_id": "door-cam"                   # optional
    }

The camera finds taps to answer by polling GET /api/presence/rooms/<id>/taps/pending.

Every response under /api/presence is JSON, errors included (see json_errors).
Requests and expected results: api/presence.http.
"""

import os
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Blueprint, current_app, g, jsonify, request
from flask_restful import Api, Resource
from sqlalchemy.exc import IntegrityError

from __init__ import db
from api.authorize import token_required
from model import account_types
from model.presence import (CameraCheck, PresenceEvent, PresencePeriod, PresenceRoom, membership_period,
                            membership_rows, period_names, presence_room_members, remove_membership,
                            set_membership, to_iso_z, utc_now)
from model.presence_schedule import (PresenceSchedule, PresenceScheduleOverride, ScheduleError,
                                     check_timezone, parse_date, scheduled_block_at)
from model.rfid_tag import RfidCardClaim, RfidTag
from model.user import User

presence_api = Blueprint('presence_api', __name__, url_prefix='/api/presence')
api = Api(presence_api)

DEVICE_API_KEY = os.environ.get("RFID_API_KEY", "")
STAFF = account_types.staff_roles()
SOURCES = {"rfid", "camera"}
SUPPORTED_SOURCES = {"rfid"}  # taps; camera results go through record_camera_result()
# A device clock running ahead of the server by more than this is treated
# as wrong, and the server's receive time is used instead.
MAX_FUTURE_SKEW = timedelta(minutes=2)
# How long after a tap the camera can still pick it up, and how long the
# dashboard shows a tap as being checked before calling it unverified.
CAMERA_WINDOW = timedelta(seconds=15)
# Until the camera is connected, taps without a camera result read as
# RFID_ONLY instead of PENDING/UNVERIFIED. Set CAMERA_ENABLED=true once the
# Jarvis camera is posting results.
CAMERA_ENABLED = os.environ.get("CAMERA_ENABLED", "").strip().lower() in ("1", "true", "yes")


class PresenceError(Exception):
    def __init__(self, message, status_code=400, **extra):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.extra = extra

    def response(self):
        return {"message": self.message, **self.extra}, self.status_code


def json_errors(func):
    """Turns every failure inside a presence/RFID endpoint into a JSON body,
    so a device or the dashboard gets a readable message instead of an HTML
    error page (e.g. on a server whose presence tables were not created yet)."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except PresenceError as e:
            return e.response()
        except ScheduleError as e:
            return {"message": str(e)}, 400
        except Exception as e:  # noqa: BLE001 - last resort, logged with the traceback
            db.session.rollback()
            current_app.logger.exception("presence API error")
            return {"message": "Server error in the presence API", "error": f"{type(e).__name__}: {e}"[:300]}, 500
    return wrapper


class PresenceResource(Resource):
    """Base for every presence/RFID resource: JSON errors on all methods."""
    method_decorators = [json_errors]


def require_device_api_key(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not DEVICE_API_KEY:
            return {"message": "RFID_API_KEY not configured on server"}, 500
        if request.headers.get('X-API-Key', '') != DEVICE_API_KEY:
            return {"message": "Invalid or missing API key"}, 401
        return func(*args, **kwargs)
    return wrapper


def json_body():
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        raise PresenceError("JSON object body required")
    return data


def require_event_id(data):
    event_id = str(data.get("event_id") or "").strip()
    if not event_id or len(event_id) > 64:
        raise PresenceError("event_id required (max 64 chars)")
    return event_id


def get_room(room_id):
    room = PresenceRoom.query.get(room_id) if isinstance(room_id, int) and not isinstance(room_id, bool) else None
    if not room:
        raise PresenceError(f"no room {room_id}", 404)
    return room


def get_schedule(schedule_id):
    schedule = PresenceSchedule.query.get(schedule_id) if isinstance(schedule_id, int) else None
    if not schedule:
        raise PresenceError(f"no schedule {schedule_id}", 404)
    return schedule


def get_user(uid):
    user = User.query.filter_by(_uid=str(uid or "").strip()).first()
    if not user:
        raise PresenceError(f"no user with uid {uid}", 404, status="unregistered")
    return user


def check_period(room, period_name, allow_all):
    """period_name if it is one of the room's periods (see period_names), or
    None for every period. allow_all lets staff choose every period in any
    room; a student may only when the room has no schedule, so they cannot
    land on every period's roster by leaving the period out."""
    names = period_names(room)
    period_name = str(period_name).strip() if period_name is not None else ""
    if not period_name:
        if allow_all or not names:
            return None
        raise PresenceError(f"period_name required; room {room.id} has periods", 400, periods=names)
    if period_name not in names:
        raise PresenceError(f"{period_name!r} is not a period of room {room.id}", 400, periods=names)
    return period_name


def parse_occurred_at(value):
    """ISO 8601 with a timezone -> naive UTC. Naive timestamps are rejected
    since there is no way to know what zone the device meant."""
    if not isinstance(value, str) or not value:
        raise PresenceError("occurred_at required (ISO 8601 with Z or an offset)")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise PresenceError(f"occurred_at is not ISO 8601: {value!r}")
    if dt.tzinfo is None:
        raise PresenceError("occurred_at must include a timezone (Z or an offset)")
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------- periods

def manual_period_at(room, dt):
    manual = (
        PresencePeriod.query.filter(PresencePeriod._room_id == room.id, PresencePeriod._source == "manual",
                                    PresencePeriod._start_time <= dt)
        # Two periods started in the same second (e.g. a double-clicked Start)
        # share a start_time; the newer one wins, matching current_period().
        .order_by(PresencePeriod._start_time.desc(), PresencePeriod.id.desc())
        .first()
    )
    return manual if manual and manual.contains(dt) else None


def period_at(room, dt):
    """The room's period whose window contains dt (naive UTC). A manually
    started period wins while it runs, so someone who starts one (e.g. for a
    demo during passing time) knows taps will count; otherwise the room's
    schedule. Scheduled periods are stored the first time they are needed so
    events can reference them."""
    manual = manual_period_at(room, dt)
    if manual:
        return manual

    scheduled = scheduled_block_at(room, dt)
    if not scheduled:
        return None
    schedule, block, start_utc, end_utc = scheduled
    period = PresencePeriod.query.filter_by(_room_id=room.id, _source="scheduled",
                                            _name=block.name, _start_time=start_utc).first()
    if not period:
        period = PresencePeriod(
            room_id=room.id,
            name=block.name,
            start_time=start_utc,
            duration_seconds=int((end_utc - start_utc).total_seconds()),
            grace_seconds=schedule.grace_seconds,
            source="scheduled",
        )
        db.session.add(period)
        db.session.commit()
    return period


def current_period(room, now):
    """The period a dashboard should show right now for this room: a running
    manual period, else the live scheduled period, else the room's most
    recently started manual period (kept after it ends so final states stay
    visible)."""
    latest_manual = (
        PresencePeriod.query.filter_by(_room_id=room.id, _source="manual", _active=True)
        .order_by(PresencePeriod.id.desc())
        .first()
    )
    if latest_manual and latest_manual.contains(now):
        return latest_manual
    if scheduled_block_at(room, now):
        return period_at(room, now)
    return latest_manual


# ---------------------------------------------------------------- events

def resolve_user(source, data):
    if source == "rfid":
        tag_uid = str(data.get("tag_uid") or "").strip()
        if not tag_uid:
            raise PresenceError("tag_uid required for source rfid")
        tag = RfidTag.query.filter_by(_tag_uid=tag_uid).first()
        if not tag:
            raise PresenceError(f"tag {tag_uid} is not registered", 404, status="unregistered")
        return tag.user, tag_uid

    uid = str(data.get("uid") or "").strip()
    if not uid:
        raise PresenceError(f"uid required for source {source}")
    return get_user(uid), None


def claim_unregistered_tag(room, tag_uid, now):
    """Self-registration (see /api/rfid/claims): links an unregistered card
    tapped at this room's reader to the one person with an open claim there.
    Returns a response dict, or None when nobody is registering (the tap is
    then rejected as unregistered). Two open claims at once is a conflict:
    nobody gets the card and both are asked to try again."""
    claims = [c for c in RfidCardClaim.query.filter_by(_room_id=room.id, _status="open").all() if c.is_open(now)]
    if not claims:
        return None
    if len(claims) > 1:
        for claim in claims:
            claim.status = "conflict"
        db.session.commit()
        raise PresenceError("more than one person is registering a card at this reader; try again one at a time",
                            409, status="claim_conflict")
    claim = claims[0]
    if RfidTag.query.filter_by(_user_id=claim.user_id).first():
        claim.status = "cancelled"  # they got a card some other way since opening the claim
        db.session.commit()
        return None
    db.session.add(RfidTag(tag_uid=tag_uid, user_id=claim.user_id))
    claim.status, claim.tag_uid = "registered", tag_uid
    db.session.commit()
    return {"status": "card_registered", "uid": claim.user.uid, "user_name": claim.user.name,
            "message": f"card registered to {claim.user.name}"}


def record_event(data):
    """Validates, de-duplicates, and stores one presence event. Returns
    (response_dict, http_status). Raises PresenceError on bad input."""
    received_at = utc_now()
    event_id = require_event_id(data)

    existing = PresenceEvent.query.filter_by(_event_id=event_id).first()
    if existing:
        return {"status": "accepted", "duplicate": True, "event": existing.to_dict()}, 200

    source = data.get("source")
    if source not in SOURCES:
        raise PresenceError(f"source must be one of {sorted(SOURCES)}")
    if source not in SUPPORTED_SOURCES:
        raise PresenceError(f"source {source} is not supported yet")

    room = get_room(data.get("room_id"))

    occurred_at = parse_occurred_at(data.get("occurred_at"))
    adjusted = False
    if occurred_at > received_at + MAX_FUTURE_SKEW:
        occurred_at = received_at
        adjusted = True

    try:
        user, tag_uid = resolve_user(source, data)
    except PresenceError as e:
        # An unregistered card may be someone registering it right now.
        if source == "rfid" and e.extra.get("status") == "unregistered":
            claimed = claim_unregistered_tag(room, str(data.get("tag_uid")).strip(), received_at)
            if claimed:
                return claimed, 200
        raise

    period = period_at(room, occurred_at)
    if period:
        last_event = (
            PresenceEvent.query.filter(
                PresenceEvent._user_id == user.id,
                PresenceEvent._room_id == room.id,
                PresenceEvent._period_id == period.id,
                PresenceEvent._type.in_(["enter", "exit"]),
                PresenceEvent._occurred_at <= occurred_at,
            )
            .order_by(PresenceEvent._occurred_at.desc(), PresenceEvent.id.desc())
            .first()
        )
        event_type = "exit" if last_event and last_event.type == "enter" else "enter"
    else:
        event_type = "ignored"

    device_id = data.get("device_id")
    event = PresenceEvent(
        event_id=event_id,
        source=source,
        user_id=user.id,
        room_id=room.id,
        period_id=period.id if period else None,
        type=event_type,
        occurred_at=occurred_at,
        received_at=received_at,
        tag_uid=tag_uid,
        device_id=str(device_id)[:64] if device_id else None,
    )
    try:
        db.session.add(event)
        db.session.commit()
    except IntegrityError:
        # Same event_id arrived concurrently; the other request won.
        db.session.rollback()
        existing = PresenceEvent.query.filter_by(_event_id=event_id).first()
        return {"status": "accepted", "duplicate": True, "event": existing.to_dict()}, 200

    response = {"status": "accepted", "duplicate": False, "event": event.to_dict()}
    if adjusted:
        response["occurred_at_adjusted"] = True
    return response, 200


def record_camera_result(data):
    """Stores the camera's answer for one tap. One result per tap: a retry
    with the same event_id is a duplicate, a second result with a new
    event_id is rejected with 409."""
    received_at = utc_now()
    event_id = require_event_id(data)

    existing = CameraCheck.query.filter_by(_event_id=event_id).first()
    if existing:
        return {"status": "accepted", "duplicate": True, "check": existing.to_dict()}, 200

    room = get_room(data.get("room_id"))

    tap_event_id = str(data.get("tap_event_id") or "").strip()
    if not tap_event_id:
        raise PresenceError("tap_event_id required for source camera")
    tap = PresenceEvent.query.filter_by(_event_id=tap_event_id, _source="rfid").first()
    if not tap:
        raise PresenceError(f"no tap with event_id {tap_event_id}", 404)
    if tap.room_id != room.id:
        raise PresenceError(f"tap {tap_event_id} is not in room {room.id}")
    if CameraCheck.query.filter_by(_tap_id=tap.id).first():
        raise PresenceError(f"tap {tap_event_id} already has a camera result", 409, status="already_checked")

    result = data.get("result")
    if result not in CameraCheck.RESULTS:
        raise PresenceError(f"result must be one of {list(CameraCheck.RESULTS)}")

    recognized_user = None
    if result == "match":
        if not str(data.get("uid") or "").strip():
            raise PresenceError("uid required when result is match")
        recognized_user = get_user(data.get("uid"))

    confidence = data.get("confidence")
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise PresenceError("confidence must be a number from 0 to 1")
        confidence = float(confidence)

    occurred_at = parse_occurred_at(data.get("occurred_at"))
    if occurred_at > received_at + MAX_FUTURE_SKEW:
        occurred_at = received_at

    device_id = data.get("device_id")
    check = CameraCheck(
        event_id=event_id,
        tap_id=tap.id,
        result=result,
        recognized_user_id=recognized_user.id if recognized_user else None,
        confidence=confidence,
        occurred_at=occurred_at,
        received_at=received_at,
        device_id=str(device_id)[:64] if device_id else None,
    )
    try:
        db.session.add(check)
        db.session.commit()
    except IntegrityError:
        # A concurrent request stored this event_id or a result for this tap first.
        db.session.rollback()
        existing = CameraCheck.query.filter_by(_event_id=event_id).first()
        if existing:
            return {"status": "accepted", "duplicate": True, "check": existing.to_dict()}, 200
        raise PresenceError(f"tap {tap_event_id} already has a camera result", 409, status="already_checked")

    return {"status": "accepted", "duplicate": False, "check": check.to_dict()}, 200


# ---------------------------------------------------------------- status

def tap_verification(tap, check, now):
    """Camera verification for one tap, as shown on the dashboard."""
    if tap._source != "rfid":
        return None
    if check:
        return check.verification
    if not CAMERA_ENABLED:
        return "RFID_ONLY"
    return "PENDING" if now - tap.occurred_at <= CAMERA_WINDOW else "UNVERIFIED"


def compute_state(events, period, now):
    """State for one person in one period, from their counted (enter/exit)
    events in occurred_at order. Ported from the standalone prototype."""
    window_open = now < period.end_time

    if not events:
        return "NOT_YET_ARRIVED" if window_open else "ABSENT"

    first_in = next((e for e in events if e.type == "enter"), None)
    late = first_in is not None and first_in.occurred_at > period.start_time + timedelta(
        seconds=period.grace_seconds
    )
    currently_in = events[-1].type == "enter"

    if currently_in:
        return "TARDY" if late else "PRESENT"

    return "TEMP_OUT" if window_open else "LEFT_EARLY"


def room_status(room):
    """Each person's current state for the room's current period. In a
    scheduled period the roster is the members enrolled in that period (by
    name) plus every-period members; a manual period (a demo, an event) shows
    every member. Anyone who has ever produced a presence event in the room
    and is not a member is added too (so tagged people show up before
    membership is set up); an enrolled student is not, so they only appear in
    their own period."""
    now = utc_now()
    period = current_period(room, now)
    body = {
        "room": {"id": room.id, "name": room.name},
        "generated_at": to_iso_z(now),
        "period": period.to_dict(now=now) if period else None,
        "students": [],
    }
    if not period:
        return body

    rows = membership_rows(room)
    period_of = {u.id: p for u, p in rows}
    members = {u.id: u for u, p in rows if period.source == "manual" or p is None or p == period.name}
    seen_ids = {
        row[0]
        for row in db.session.query(PresenceEvent._user_id).filter(PresenceEvent._room_id == room.id).distinct()
    }
    roster = dict(members)
    missing = seen_ids - period_of.keys()
    if missing:
        roster.update({u.id: u for u in User.query.filter(User.id.in_(missing))})

    events_by_user = {}
    period_events = (
        PresenceEvent.query.filter(
            PresenceEvent._room_id == room.id,
            PresenceEvent._period_id == period.id,
            PresenceEvent._type.in_(["enter", "exit"]),
        )
        .order_by(PresenceEvent._occurred_at, PresenceEvent.id)
        .all()
    )
    for event in period_events:
        events_by_user.setdefault(event.user_id, []).append(event)
    checks_by_tap = {}
    if period_events:
        checks = CameraCheck.query.filter(CameraCheck._tap_id.in_([e.id for e in period_events])).all()
        checks_by_tap = {c.tap_id: c for c in checks}

    for user in sorted(roster.values(), key=lambda u: (u.name or "").lower()):
        events = events_by_user.get(user.id, [])
        last = events[-1] if events else None
        body["students"].append({
            "uid": user.uid,
            "name": user.name,
            "state": compute_state(events, period, now),
            "since": to_iso_z(last.occurred_at) if last else None,
            "last_source": last._source if last else None,
            # Camera verification of the person's most recent tap.
            "verification": tap_verification(last, checks_by_tap.get(last.id), now) if last else None,
            "member": user.id in members,
            "period_name": period_of.get(user.id),
        })
    return body


# ---------------------------------------------------------------- rooms

def apply_room_fields(room, data):
    if "name" in data:
        name = str(data.get("name") or "").strip()
        if not name:
            raise PresenceError("name required")
        clash = PresenceRoom.query.filter_by(_name=name[:120]).first()
        if clash and clash.id != room.id:
            raise PresenceError(f"a room named {name!r} already exists", 409)
        room.name = name[:120]
    if "description" in data:
        room.description = (str(data["description"]).strip()[:255] or None) if data["description"] else None
    if "schedule_id" in data:
        room.schedule_id = get_schedule(data["schedule_id"]).id if data["schedule_id"] is not None else None
    if "member_uids" in data:
        if not isinstance(data["member_uids"], list):
            raise PresenceError("member_uids must be a list of uids")
        users = [get_user(uid) for uid in data["member_uids"]]
        # Only the difference changes, so members who stay keep their period.
        keep = {user.id for user in users}
        for user, _ in membership_rows(room):
            if user.id not in keep:
                remove_membership(room, user)
        for user in users:
            if not membership_period(room, user)[0]:
                set_membership(room, user, None)


class PresenceAPI:
    class _Event(PresenceResource):
        @require_device_api_key
        def post(self):
            """A device reports a presence signal (RFID tap or camera result)."""
            data = json_body()
            if data.get("source") == "camera":
                return record_camera_result(data)
            return record_event(data)

    class _PendingTaps(PresenceResource):
        @require_device_api_key
        def get(self, room_id):
            """Taps from the last CAMERA_WINDOW that the camera has not answered
            yet. The camera polls this about once a second, takes a picture for
            each, and posts a camera result back."""
            room = get_room(room_id)
            taps = (
                PresenceEvent.query.outerjoin(CameraCheck, CameraCheck._tap_id == PresenceEvent.id)
                .filter(
                    CameraCheck.id.is_(None),
                    PresenceEvent._room_id == room.id,
                    PresenceEvent._source == "rfid",
                    PresenceEvent._type.in_(["enter", "exit"]),
                    PresenceEvent._occurred_at >= utc_now() - CAMERA_WINDOW,
                )
                .order_by(PresenceEvent._occurred_at)
                .all()
            )
            return [
                {"tap_event_id": tap.event_id, "uid": tap.user.uid if tap.user else None,
                 "occurred_at": to_iso_z(tap.occurred_at), "type": tap.type}
                for tap in taps
            ]

    class _Rooms(PresenceResource):
        @token_required(STAFF)
        def get(self):
            return [room.to_dict() for room in PresenceRoom.query.order_by(PresenceRoom._name).all()]

        @token_required(STAFF)
        def post(self):
            """Create a room: {name, description?, schedule_id?, member_uids?}."""
            data = json_body()
            name = str(data.get("name") or "").strip()[:120]
            if not name:
                raise PresenceError("name required")
            existing = PresenceRoom.query.filter_by(_name=name).first()
            if existing:  # the id lets a client (e.g. api/presence.http) continue with the existing room
                raise PresenceError(f"a room named {name!r} already exists", 409, id=existing.id)
            room = PresenceRoom(name=name)
            db.session.add(room)
            db.session.flush()  # members can only be linked once the room has an id
            apply_room_fields(room, {k: v for k, v in data.items() if k != "name"})
            db.session.commit()
            return room.to_dict(), 201

    class _Room(PresenceResource):
        @token_required(STAFF)
        def get(self, room_id):
            return get_room(room_id).to_dict()

        @token_required(STAFF)
        def put(self, room_id):
            """Update any of {name, description, schedule_id (null = none), member_uids}."""
            room = get_room(room_id)
            apply_room_fields(room, json_body())
            db.session.commit()
            return room.to_dict()

        @token_required(STAFF)
        def delete(self, room_id):
            """Deletes a room that has no recorded events (its history stays intact)."""
            room = get_room(room_id)
            if PresenceEvent.query.filter_by(_room_id=room.id).first():
                raise PresenceError(f"room {room.id} has recorded events and cannot be deleted", 409)
            PresenceScheduleOverride.query.filter_by(_room_id=room.id).delete()
            PresencePeriod.query.filter_by(_room_id=room.id).delete()
            db.session.execute(presence_room_members.delete().where(presence_room_members.c.room_id == room.id))
            db.session.delete(room)
            db.session.commit()
            return {"message": f"room {room_id} deleted"}

    class _RoomMember(PresenceResource):
        @token_required(STAFF)
        def post(self, room_id, uid):
            """Adds a member: optional {period_name} (null or left out = every
            period). Posting an existing member with period_name changes it;
            without one, their period is left as it is."""
            room, user = get_room(room_id), get_user(uid)
            data = request.get_json(silent=True) or {}
            if not isinstance(data, dict):
                raise PresenceError("body must be a JSON object, e.g. {\"period_name\": \"Period 3\"}")
            if "period_name" in data or not membership_period(room, user)[0]:
                set_membership(room, user, check_period(room, data.get("period_name"), allow_all=True))
                db.session.commit()
            return room.to_dict()

        @token_required(STAFF)
        def delete(self, room_id, uid):
            room, user = get_room(room_id), get_user(uid)
            remove_membership(room, user)
            db.session.commit()
            return room.to_dict()

    class _RoomPeriods(PresenceResource):
        @token_required(STAFF)
        def get(self, room_id):
            """The room's current period and its 10 most recent periods."""
            room, now = get_room(room_id), utc_now()
            period = current_period(room, now)
            recent = (PresencePeriod.query.filter_by(_room_id=room.id)
                      .order_by(PresencePeriod._start_time.desc()).limit(10).all())
            return {"current": period.to_dict(now=now) if period else None,
                    "recent": [p.to_dict(now=now) for p in recent]}

        @token_required(STAFF)
        def post(self, room_id):
            """Starts a manual period in this room now: {duration_seconds?,
            grace_seconds?, name?}. While it runs it takes precedence over the
            room's schedule (see period_at)."""
            room = get_room(room_id)
            data = request.get_json(silent=True) or {}
            try:
                duration_seconds = int(data.get("duration_seconds", 120))
                grace_seconds = int(data.get("grace_seconds", 15))
            except (TypeError, ValueError):
                raise PresenceError("duration_seconds and grace_seconds must be integers")
            if duration_seconds <= 0 or grace_seconds < 0:
                raise PresenceError("duration_seconds must be > 0 and grace_seconds >= 0")

            PresencePeriod.query.filter_by(_room_id=room.id, _source="manual", _active=True).update({"_active": False})
            period = PresencePeriod(
                room_id=room.id,
                name=str(data.get("name") or "Manual Period")[:80],
                # Whole seconds, so a tap stamped (to the millisecond) right as the
                # period starts never sorts before it.
                start_time=utc_now().replace(microsecond=0),
                duration_seconds=duration_seconds,
                grace_seconds=grace_seconds,
                source="manual",
                active=True,
            )
            db.session.add(period)
            db.session.commit()
            return period.to_dict(now=utc_now()), 201

    class _RoomStatus(PresenceResource):
        @token_required(STAFF)
        def get(self, room_id):
            """Each person's current state for the room's current period."""
            return room_status(get_room(room_id))

    class _Schedules(PresenceResource):
        @token_required(STAFF)
        def get(self):
            return [s.to_dict() for s in PresenceSchedule.query.order_by(PresenceSchedule._name).all()]

        @token_required(STAFF)
        def post(self):
            """Create a schedule: {name, timezone?, grace_seconds?, blocks: [{name, weekdays, start, end}]}."""
            data = json_body()
            name = str(data.get("name") or "").strip()[:80]
            if not name:
                raise PresenceError("name required")
            existing = PresenceSchedule.query.filter_by(_name=name).first()
            if existing:  # the id lets a client continue with the existing schedule
                raise PresenceError(f"a schedule named {name!r} already exists", 409, id=existing.id)
            schedule = PresenceSchedule(name=name, timezone=check_timezone(data.get("timezone")),
                                        grace_seconds=_grace(data))
            schedule.set_blocks(data.get("blocks", []))
            db.session.add(schedule)
            db.session.commit()
            return schedule.to_dict(), 201

    class _Schedule(PresenceResource):
        @token_required(STAFF)
        def get(self, schedule_id):
            return get_schedule(schedule_id).to_dict()

        @token_required(STAFF)
        def put(self, schedule_id):
            """Update any of {name, timezone, grace_seconds, blocks (replaces all blocks)}.
            Periods already created keep their times; new ones use the edit."""
            schedule, data = get_schedule(schedule_id), json_body()
            if "name" in data:
                name = str(data.get("name") or "").strip()[:80]
                clash = PresenceSchedule.query.filter_by(_name=name).first()
                if not name or (clash and clash.id != schedule.id):
                    raise PresenceError(f"schedule name {name!r} is empty or taken", 409 if name else 400)
                schedule._name = name
            if "timezone" in data:
                schedule._timezone = check_timezone(data["timezone"])
            if "grace_seconds" in data:
                schedule._grace_seconds = _grace(data)
            if "blocks" in data:
                schedule.set_blocks(data["blocks"])
            db.session.commit()
            return schedule.to_dict()

        @token_required(STAFF)
        def delete(self, schedule_id):
            """Deletes a schedule no room or override uses."""
            schedule = get_schedule(schedule_id)
            rooms = [r.name for r in PresenceRoom.query.filter_by(_schedule_id=schedule.id)]
            overrides = PresenceScheduleOverride.query.filter_by(_schedule_id=schedule.id).count()
            if rooms or overrides:
                raise PresenceError(f"schedule {schedule.id} is in use", 409, rooms=rooms, overrides=overrides)
            db.session.delete(schedule)
            db.session.commit()
            return {"message": f"schedule {schedule_id} deleted"}

    class _Overrides(PresenceResource):
        @token_required(STAFF)
        def get(self):
            """Overrides from ?from=YYYY-MM-DD (default: today, UTC) onward."""
            start = parse_date(request.args["from"], "from") if request.args.get("from") else utc_now().date()
            rows = (PresenceScheduleOverride.query.filter(PresenceScheduleOverride._date >= start)
                    .order_by(PresenceScheduleOverride._date).all())
            return [o.to_dict() for o in rows]

        @token_required(STAFF)
        def post(self):
            """Replace the schedule for one date: {date, room_id? (null = every
            room), schedule_id? (null = no periods), note?}. Replaces an existing
            override for the same date and room."""
            data = json_body()
            day = parse_date(data.get("date"))
            room_id = get_room(data["room_id"]).id if data.get("room_id") is not None else None
            schedule_id = get_schedule(data["schedule_id"]).id if data.get("schedule_id") is not None else None
            PresenceScheduleOverride.query.filter_by(_date=day, _room_id=room_id).delete()
            override = PresenceScheduleOverride(date=day, room_id=room_id, schedule_id=schedule_id,
                                                note=(str(data.get("note")).strip()[:120] if data.get("note") else None))
            db.session.add(override)
            db.session.commit()
            return override.to_dict(), 201

    class _Override(PresenceResource):
        @token_required(STAFF)
        def delete(self, override_id):
            override = PresenceScheduleOverride.query.get(override_id)
            if not override:
                raise PresenceError(f"no override {override_id}", 404)
            db.session.delete(override)
            db.session.commit()
            return {"message": f"override {override_id} deleted"}

    api.add_resource(_Event, '/event')
    api.add_resource(_Rooms, '/rooms')
    api.add_resource(_Room, '/rooms/<int:room_id>')
    api.add_resource(_RoomMember, '/rooms/<int:room_id>/members/<string:uid>')
    api.add_resource(_RoomPeriods, '/rooms/<int:room_id>/periods')
    api.add_resource(_RoomStatus, '/rooms/<int:room_id>/status')
    api.add_resource(_PendingTaps, '/rooms/<int:room_id>/taps/pending')
    api.add_resource(_Schedules, '/schedules')
    api.add_resource(_Overrides, '/schedules/overrides')
    api.add_resource(_Override, '/schedules/overrides/<int:override_id>')
    api.add_resource(_Schedule, '/schedules/<int:schedule_id>')


# ---------------------------------------------------------------- enrollment

def my_enrollments(user):
    """The logged-in person's own memberships, for /enroll/me. Only their own
    rows, so a student never sees who else is in a room."""
    rows = (db.session.query(PresenceRoom, presence_room_members.c.period_name)
            .join(presence_room_members, presence_room_members.c.room_id == PresenceRoom.id)
            .filter(presence_room_members.c.user_id == user.id)
            .order_by(PresenceRoom._name)
            .all())
    return {
        "uid": user.uid,
        "name": user.name,
        "enrollments": [{"room_id": room.id, "room": room.name, "period_name": period_name}
                        for room, period_name in rows],
    }


class PresenceEnroll:
    """Self-enrollment: any logged-in person picks a room and one of its
    periods, and then shows on that room's roster during that period (see
    room_status). Staff manage everyone else through /rooms/<id>/members."""

    class _EnrollRooms(PresenceResource):
        @token_required()
        def get(self):
            """Rooms to enroll in, with their period names. No member data."""
            return [{"id": room.id, "name": room.name, "periods": period_names(room)}
                    for room in PresenceRoom.query.order_by(PresenceRoom._name).all()]

    class _EnrollMe(PresenceResource):
        @token_required()
        def get(self):
            return my_enrollments(g.current_user)

        @token_required()
        def put(self):
            """Enroll in a room's period: {room_id, period_name}. Enrolling in
            the same room again switches the period. period_name may be null
            only in a room without a schedule (every period)."""
            user, data = g.current_user, json_body()
            room = get_room(data.get("room_id"))
            set_membership(room, user, check_period(room, data.get("period_name"), allow_all=False))
            db.session.commit()
            return my_enrollments(user)

    class _EnrollMeRoom(PresenceResource):
        @token_required()
        def delete(self, room_id):
            """Leave a room. Leaving one you are not in changes nothing."""
            user = g.current_user
            remove_membership(get_room(room_id), user)
            db.session.commit()
            return my_enrollments(user)

    api.add_resource(_EnrollRooms, '/enroll/rooms')
    api.add_resource(_EnrollMe, '/enroll/me')
    api.add_resource(_EnrollMeRoom, '/enroll/me/<int:room_id>')


def _grace(data):
    try:
        grace = int(data.get("grace_seconds", 300))
    except (TypeError, ValueError):
        raise PresenceError("grace_seconds must be an integer")
    if grace < 0:
        raise PresenceError("grace_seconds must be >= 0")
    return grace


def json_not_found(blueprint):
    """Unknown paths under the blueprint's prefix get a JSON 404 instead of the
    site's HTML 404 page. Scoped to this prefix; the rest of the site is untouched."""
    @blueprint.route('/', defaults={'unknown': ''}, methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
    @blueprint.route('/<path:unknown>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
    def not_found(unknown):
        return jsonify({"message": f"no presence endpoint {request.method} {request.path}"}), 404


json_not_found(presence_api)
