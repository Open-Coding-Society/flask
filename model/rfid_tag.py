from datetime import datetime
from sqlalchemy.exc import IntegrityError
from __init__ import db


class RfidTag(db.Model):
    """Binds a physical RFID tag UID to an OCS user. This is the
    "tag-registration step", an admin action kept separate from logging
    taps (PresenceEvent in model/presence.py)."""
    __tablename__ = 'rfid_tags'

    id = db.Column(db.Integer, primary_key=True)
    _tag_uid = db.Column(db.String(64), unique=True, nullable=False)
    _user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    _created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User')

    def __init__(self, tag_uid, user_id):
        self._tag_uid = tag_uid
        self._user_id = user_id

    @property
    def tag_uid(self): return self._tag_uid
    @tag_uid.setter
    def tag_uid(self, val): self._tag_uid = val

    @property
    def user_id(self): return self._user_id
    @user_id.setter
    def user_id(self, val): self._user_id = val

    @property
    def created_at(self): return self._created_at

    def create(self):
        try:
            db.session.add(self)
            db.session.commit()
            return self
        except IntegrityError:
            db.session.rollback()
            return None

    def delete(self):
        db.session.delete(self)
        db.session.commit()

    def to_dict(self):
        return {
            'id': self.id,
            'tag_uid': self.tag_uid,
            'user_id': self.user_id,
            'user_name': self.user.name if self.user else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class RfidCardClaim(db.Model):
    """A short window in which a logged-in person registers their own card:
    they open a claim for a room's reader, then the next unregistered card
    tapped there is linked to them (see claim_unregistered_tag in
    api/presence_api.py). Two open claims at the same reader resolve to
    'conflict' instead, so a card is never linked to the wrong person.

    Claims are short-lived, so they are deliberately left out of the
    export/import migration (model/presence_backup.py)."""
    __tablename__ = 'rfid_card_claims'

    WINDOW_SECONDS = 60
    STATUSES = ('open', 'registered', 'conflict', 'cancelled')

    id = db.Column(db.Integer, primary_key=True)
    _user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    _room_id = db.Column(db.Integer, db.ForeignKey('presence_rooms.id'), nullable=False)
    _status = db.Column(db.String(16), nullable=False, default='open')
    _created_at = db.Column(db.DateTime, nullable=False)
    _expires_at = db.Column(db.DateTime, nullable=False, index=True)
    _tag_uid = db.Column(db.String(64), nullable=True)

    user = db.relationship('User')
    room = db.relationship('PresenceRoom')

    def __init__(self, user_id, room_id, created_at, expires_at):
        self._user_id = user_id
        self._room_id = room_id
        self._created_at = created_at
        self._expires_at = expires_at
        self._status = 'open'

    @property
    def user_id(self): return self._user_id

    @property
    def room_id(self): return self._room_id

    @property
    def status(self): return self._status

    @status.setter
    def status(self, value): self._status = value

    @property
    def tag_uid(self): return self._tag_uid

    @tag_uid.setter
    def tag_uid(self, value): self._tag_uid = value

    def is_open(self, now):
        return self._status == 'open' and now < self._expires_at

    def to_dict(self, now):
        """Status 'expired' is derived: an open claim past its window."""
        from model.presence import to_iso_z
        status = 'expired' if self._status == 'open' and now >= self._expires_at else self._status
        return {
            'id': self.id,
            'status': status,
            'uid': self.user.uid if self.user else None,
            'user_name': self.user.name if self.user else None,
            'room_id': self._room_id,
            'room': self.room.name if self.room else None,
            'expires_at': to_iso_z(self._expires_at),
            'seconds_left': max(0, int((self._expires_at - now).total_seconds())) if status == 'open' else 0,
            'card_ending': self._tag_uid[-4:] if self._tag_uid else None,
        }
