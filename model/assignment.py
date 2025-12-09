import base64
import os
import shutil
from werkzeug.utils import secure_filename
from __init__ import app, db
import json

class Assignment(db.Model):
    """
    Assignment Model
    
    Represents an assignment submission that can contain multiple files.
    """
    __tablename__ = 'assignments'

    id = db.Column(db.Integer, primary_key=True)
    _uid = db.Column(db.String(255), nullable=False)
    _title = db.Column(db.String(255), nullable=False)
    _class_name = db.Column(db.String(255), nullable=False)
    _score = db.Column(db.Float, default=0.0)
    _content = db.Column(db.JSON, nullable=False) # List of filenames
    _notes = db.Column(db.Text, nullable=True)
    _link = db.Column(db.String(512), nullable=True)

    def __init__(self, uid, title, class_name, content=None, score=0.0, notes=None, link=None):
        self._uid = uid
        self._title = title
        self._class_name = class_name
        self._content = content if content else []
        self._score = score
        self._notes = notes
        self._link = link

    @property
    def uid(self):
        return self._uid

    @property
    def title(self):
        return self._title
    
    @property
    def class_name(self):
        return self._class_name
        
    @property
    def score(self):
        return self._score
    
    @property
    def content(self):
        return self._content

    @property
    def notes(self):
        return self._notes
    
    @notes.setter
    def notes(self, notes):
        self._notes = notes

    @property
    def link(self):
        return self._link
    
    @link.setter
    def link(self, link):
        self._link = link

    def create(self):
        try:
            db.session.add(self)
            db.session.commit()
            return self
        except Exception as e:
            print(f"Error creating assignment: {e}")
            db.session.rollback()
            return None

    def read(self):
        return {
            "id": self.id,
            "uid": self.uid,
            "title": self.title,
            "class": self.class_name,
            "score": self.score,
            "content": self.content,
            "notes": self.notes,
            "link": self.link
        }

    def update(self, inputs):
        """
        Updates the assignment score.
        """
        if not isinstance(inputs, dict):
            return self
            
        score = inputs.get("score")
        notes = inputs.get("notes")
        link = inputs.get("link")
        
        if score is not None:
            self._score = float(score)

        if notes is not None:
             self._notes = notes
             
        if link is not None:
             self._link = link

        try:
            db.session.commit()
            return self
        except Exception as e:
            print(f"Error updating assignment: {e}")
            db.session.rollback()
            return None

    def delete(self):
        try:
            # Delete physical files
            assignment_delete_files(self.uid, self.title)
            # Delete DB record
            db.session.delete(self)
            db.session.commit()
            return True
        except Exception as e:
            print(f"Error deleting assignment: {e}")
            db.session.rollback()
            return False

def assignment_base64_upload(base64_data, filename, uid, assignment_title):
    """
    Uploads a base64 encoded file to an assignment specific directory.
    """
    try:
        file_data = base64.b64decode(base64_data)
        clean_filename = secure_filename(filename)
        
        # Structure: instance/uploads/<uid>/<assignment_title>/<filename>
        target_dir = os.path.join(app.config['UPLOAD_FOLDER'], uid, secure_filename(assignment_title))
        
        if not os.path.exists(target_dir):
            os.makedirs(target_dir)
            
        file_path = os.path.join(target_dir, clean_filename)
        with open(file_path, 'wb') as f:
            f.write(file_data)
            
        return clean_filename
    except Exception as e:
        print(f"Error uploading assignment file: {e}")
        return None

def assignment_base64_decode(uid, assignment_title, filename):
    """
    Reads a file from an assignment directory.
    """
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], uid, secure_filename(assignment_title), filename)
    try:
        with open(file_path, 'rb') as f:
            return base64.b64encode(f.read()).decode('utf-8')
    except Exception as e:
        print(f"Error reading assignment file: {e}")
        return None

def assignment_delete_files(uid, assignment_title):
    """
    Deletes the entire assignment directory.
    """
    target_dir = os.path.join(app.config['UPLOAD_FOLDER'], uid, secure_filename(assignment_title))
    try:
        if os.path.exists(target_dir):
            shutil.rmtree(target_dir)
        return True
    except Exception as e:
        print(f"Error deleting assignment files: {e}")
        return False
