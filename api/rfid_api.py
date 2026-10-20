"""
RFID API: the tap reader (a headless Raspberry Pi, not a logged-in user)
reports taps here, and staff manage which tag belongs to whom.

/scan is a thin wrapper around the generic presence API (api/presence_api.py)
and authenticates with the shared device key (X-API-Key = RFID_API_KEY),
since the device has no OCS login of its own. Tag management and the event
list use the normal OCS login (Admin/Teacher).

Every response under /api/rfid is JSON, errors included.
Requests and expected results: api/presence.http.
"""

import uuid

from flask import Blueprint, request

from __init__ import db
from api.authorize import token_required
from api.presence_api import (STAFF, PresenceError, PresenceResource, get_room, get_user, json_body,
                              json_not_found, record_event, require_device_api_key)
from flask_restful import Api
from model.presence import PresenceEvent, to_iso_z, utc_now
from model.rfid_tag import RfidTag
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

    api.add_resource(_Scan, '/scan')
    api.add_resource(_Tags, '/tags')
    api.add_resource(_Tag, '/tags/<string:tag_uid>')
    api.add_resource(_RoomEvents, '/rooms/<int:room_id>/events')


json_not_found(rfid_api)
