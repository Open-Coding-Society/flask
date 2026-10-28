"""
RFID API: the tap reader (a headless Raspberry Pi, not a logged-in user)
reports taps here, and staff manage which tag belongs to whom.

/scan is a thin wrapper around the generic presence API (api/presence_api.py)
and authenticates with the shared device key (X-API-Key = RFID_API_KEY),
since the device has no OCS login of its own. Tag management and the event
list use the normal OCS login (Admin/Teacher).

Self-registration: any logged-in person can open a short claim for a room's
reader (POST /claims) and then tap their new card there; the scan handler
links it to them (claim_unregistered_tag in api/presence_api.py). Replacing
or removing a card stays with staff (/tags).

Every response under /api/rfid is JSON, errors included.
Requests and expected results: api/presence.http.
"""

import uuid
from datetime import timedelta

from flask import Blueprint, g, request

from __init__ import db
from api.authorize import token_required
from api.presence_api import (STAFF, PresenceError, PresenceResource, get_room, get_user, json_body,
                              json_not_found, record_event, require_device_api_key)
from flask_restful import Api
from model.presence import PresenceEvent, to_iso_z, utc_now
from model.rfid_tag import RfidCardClaim, RfidTag
from model.user import User

rfid_api = Blueprint('rfid_api', __name__, url_prefix='/api/rfid')
api = Api(rfid_api)


class RfidAPI:
    class _Scan(PresenceResource):
        @require_device_api_key
        def post(self):
            """The reader calls this on every tap: {tag_uid, room_id, event_id?,
            occurred_at?, device_id?}. Forwards to the presence event handler as
            source=rfid. A reader that does not send event_id or occurred_at gets
            a fresh id and the server's receive time."""
            data = json_body()
            if not data.get('tag_uid') or data.get('room_id') is None:
                raise PresenceError("tag_uid and room_id required")
            return record_event({
                "event_id": data.get("event_id") or str(uuid.uuid4()),
                "source": "rfid",
                "room_id": data.get("room_id"),
                "occurred_at": data.get("occurred_at") or to_iso_z(utc_now()),
                "tag_uid": str(data.get("tag_uid")).strip(),
                "device_id": data.get("device_id"),
            })

    class _Tags(PresenceResource):
        @token_required(STAFF)
        def get(self):
            return [tag.to_dict() for tag in RfidTag.query.order_by(RfidTag.id).all()]

        @token_required(STAFF)
        def post(self):
            """Binds a tag to a person: {tag_uid, uid} (or user_id). Re-registering
            a tag moves it to the new person."""
            data = json_body()
            tag_uid = str(data.get('tag_uid') or '').strip()
            if not tag_uid or len(tag_uid) > 64:
                raise PresenceError("tag_uid required (max 64 chars)")
            if data.get('uid'):
                user = get_user(data['uid'])
            elif data.get('user_id') is not None:
                user = User.query.get(data['user_id'])
                if not user:
                    raise PresenceError(f"no user {data['user_id']}", 404)
            else:
                raise PresenceError("uid (or user_id) required")

            existing = RfidTag.query.filter_by(_tag_uid=tag_uid).first()
            if existing:
                existing.user_id = user.id
                db.session.commit()
                return existing.to_dict()
            tag = RfidTag(tag_uid=tag_uid, user_id=user.id)
            db.session.add(tag)
            db.session.commit()
            return tag.to_dict(), 201

    class _Tag(PresenceResource):
        @token_required(STAFF)
        def delete(self, tag_uid):
            tag = RfidTag.query.filter_by(_tag_uid=tag_uid).first()
            if not tag:
                raise PresenceError(f"tag {tag_uid} is not registered", 404)
            db.session.delete(tag)
            db.session.commit()
            return {"message": f"tag {tag_uid} removed"}

    class _RoomEvents(PresenceResource):
        @token_required(STAFF)
        def get(self, room_id):
            """The room's 200 most recent presence events, newest first."""
            room = get_room(room_id)
            events = (PresenceEvent.query.filter_by(_room_id=room.id)
                      .order_by(PresenceEvent._occurred_at.desc()).limit(200).all())
            return [e.to_dict() for e in events]

    class _MyCards(PresenceResource):
        @token_required()
        def get(self):
            """The logged-in person's card(s), shown by their last 4 digits, and
            their most recent claim."""
            user, now = g.current_user, utc_now()
            cards = RfidTag.query.filter_by(_user_id=user.id).all()
            claim = (RfidCardClaim.query.filter_by(_user_id=user.id)
                     .order_by(RfidCardClaim.id.desc()).first())
            return {
                "uid": user.uid,
                "cards": [{"card_ending": t.tag_uid[-4:],
                           "registered_at": to_iso_z(t.created_at) if t.created_at else None} for t in cards],
                "claim": claim.to_dict(now) if claim else None,
            }

    class _Claims(PresenceResource):
        @token_required()
        def post(self):
            """Opens a WINDOW_SECONDS window to register your own card at a
            room's reader: {room_id}. One card per person; a second card or a
            replacement goes through staff."""
            user, now = g.current_user, utc_now()
            room = get_room(json_body().get("room_id"))
            if RfidTag.query.filter_by(_user_id=user.id).first():
                raise PresenceError("you already have a registered card; ask a teacher to replace or remove it",
                                    409, status="has_card")
            RfidCardClaim.query.filter_by(_user_id=user.id, _status="open").update({"_status": "cancelled"})
            claim = RfidCardClaim(user_id=user.id, room_id=room.id, created_at=now,
                                  expires_at=now + timedelta(seconds=RfidCardClaim.WINDOW_SECONDS))
            db.session.add(claim)
            db.session.commit()
            return claim.to_dict(now), 201

    class _Claim(PresenceResource):
        def _own(self, claim_id):
            claim = RfidCardClaim.query.get(claim_id)
            if not claim:
                raise PresenceError(f"no claim {claim_id}", 404)
            if claim.user_id != g.current_user.id and g.current_user.role not in STAFF:
                raise PresenceError("that claim belongs to someone else", 403)
            return claim

        @token_required()
        def get(self, claim_id):
            return self._own(claim_id).to_dict(utc_now())

        @token_required()
        def delete(self, claim_id):
            claim = self._own(claim_id)
            if claim.status == "open":
                claim.status = "cancelled"
                db.session.commit()
            return claim.to_dict(utc_now())

    api.add_resource(_Scan, '/scan')
    api.add_resource(_Tags, '/tags')
    api.add_resource(_Tag, '/tags/<string:tag_uid>')
    api.add_resource(_RoomEvents, '/rooms/<int:room_id>/events')
    api.add_resource(_MyCards, '/cards/me')
    api.add_resource(_Claims, '/claims')
    api.add_resource(_Claim, '/claims/<int:claim_id>')


json_not_found(rfid_api)
