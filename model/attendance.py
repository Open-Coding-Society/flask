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
