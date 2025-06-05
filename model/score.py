from sqlite3 import IntegrityError
from sqlalchemy.exc import SQLAlchemyError
from __init__ import app, db
import logging

class Score(db.Model):
    __tablename__ = 'scores'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    value = db.Column(db.Integer, nullable=False)
    section_id = db.Column(db.Integer, nullable=True)  # optional

    def __init__(self, user_id, value, section_id=None):
        self.user_id = user_id
        self.value = value
        self.section_id = section_id
