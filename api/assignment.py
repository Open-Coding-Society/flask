from flask import Blueprint, request, jsonify
from flask_restful import Api, Resource
from api.jwt_authorize import token_required
from model.assignment import Assignment, assignment_base64_upload, assignment_base64_decode, assignment_delete_files

assignment_api = Blueprint('assignment_api', __name__, url_prefix='/api/assignment')
api = Api(assignment_api)

class _Assignment(Resource):
    @token_required()
    def post(self):
        """
        Create a new assignment with uploaded files.
        Expected JSON:
        {
            "uid": "student_uid", 
            "title": "Assignment Title",
            "class": "CSA",
            "uploads": [
                {"file": "base64_string", "filename": "notes.pdf"},
                {"file": "base64_string", "filename": "notebook.ipynb"}
            ],
            "notes": "Optional notes",
            "link": "Optional link"
        }
        """
        data = request.get_json()
        if not data:
            return {'message': 'No input data provided'}, 400
            
        uid = data.get('uid')
        title = data.get('title')
        class_name = data.get('class')
        uploads = data.get('uploads', [])
        notes = data.get('notes')
        link = data.get('link')
        
        if not uid or not title or not class_name:
            return {'message': 'UID, Title, and Class are required'}, 400

        # List to store successfully uploaded filenames
        uploaded_files = []
        
        try:
            for upload in uploads:
                b64_file = upload.get('file')
                filename = upload.get('filename')
                
                if b64_file and filename:
                    saved_filename = assignment_base64_upload(b64_file, filename, uid, title)
                    if saved_filename:
                        uploaded_files.append(saved_filename)
                    else:
                        return {'message': f'Failed to upload file: {filename}'}, 500
            
            # Create Database Record
            assignment = Assignment(uid, title, class_name, uploaded_files, notes=notes, link=link)
            created_assignment = assignment.create()
            
            if created_assignment:
                return created_assignment.read(), 201
            else:
                return {'message': 'Failed to create assignment record'}, 500
                
        except Exception as e:
            return {'message': f'An error occurred: {str(e)}'}, 500
            
    @token_required()
    def put(self):
        """
        Update an assignment (e.g. grading).
        Expected JSON:
        {
            "uid": "student_uid",
            "title": "Assignment Title",
            "score": 95.5,
            "notes": "Updated notes",
            "link": "Updated link"
        }
        """
        data = request.get_json()
        if not data:
             return {'message': 'No input data provided'}, 400
             
        uid = data.get('uid')
        title = data.get('title')
        score = data.get('score')
        notes = data.get('notes')
        link = data.get('link')
        
        if not uid or not title:
            return {'message': 'UID and Title are required to identify assignment'}, 400
            
        if score is None and notes is None and link is None:
            return {'message': 'Score, notes, or link is required for update'}, 400

        assignment = Assignment.query.filter_by(_uid=uid, _title=title).first()
        if not assignment:
            return {'message': 'Assignment not found'}, 404

        updated_assignment = assignment.update({"score": score, "notes": notes, "link": link})
        if updated_assignment:
            return updated_assignment.read(), 200
        else:
             return {'message': 'Failed to update assignment'}, 500

    @token_required()
    def get(self):
        """
        Get all assignments.
        Returns list of assignments with base64 encoded file content.
        """
        assignments = Assignment.query.all()
        results = []
        
        for assignment in assignments:
            assignment_data = assignment.read()
            # New Step: Fetch the content for each file
            # assignment_data['content'] is a list of filenames like ['file1.pdf', 'file2.ipynb']
            # We want to transform this into: [{'filename': 'file1.pdf', 'file': 'base64str...'}, ...]
            
            enhanced_content = []
            for filename in assignment.content:
                base64_str = assignment_base64_decode(assignment.uid, assignment.title, filename)
                if base64_str:
                     enhanced_content.append({
                         "filename": filename,
                         "file": base64_str 
                     })
                else:
                    enhanced_content.append({
                        "filename": filename,
                        "error": "File not found or unreadable"
                    })
            
            assignment_data['content'] = enhanced_content
            results.append(assignment_data)
            
        return jsonify(results)

    @token_required()
    def delete(self):
        """
        Delete an assignment.
        """
        uid = request.args.get('uid')
        title = request.args.get('title')
        
        if not uid or not title:
            return {'message': 'UID and Title required'}, 400
            
        assignment = Assignment.query.filter_by(_uid=uid, _title=title).first()
        if not assignment:
            return {'message': 'Assignment not found'}, 404
            
        if assignment.delete():
            return {'message': 'Assignment deleted successfully'}, 200
        else:
            return {'message': 'Failed to delete assignment'}, 500

api.add_resource(_Assignment, '/')
