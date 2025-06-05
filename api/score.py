from flask import Blueprint, request, jsonify, current_app, Response, g
from flask_restful import Api, Resource
from __init__ import app, db
from model.score import Score
from api.jwt_authorize import token_required
from model.user import User  # assuming you have a User model

# Blueprint
score_api = Blueprint('score_api', __name__, url_prefix='/api')
api = Api(score_api)

class ScoreAPI:

    class _CRUD(Resource):
        @token_required()
        def post(self):
            current_user = g.current_user
            '''Create a new score entry for the current user'''
            data = request.get_json()

            # Validate score
            value = data.get("value")
            section_id = data.get("section_id")  # optional

            if value is None:
                return jsonify({"error": "Missing score value"})

            try:
                # Create and save score
                new_score = Score(user_id=current_user.id, value=value, section_id=section_id)
                db.session.add(new_score)
                db.session.commit()
                return jsonify({
                    "id": new_score.id,
                    "user_id": new_score.user_id,
                    "value": new_score.value,
                    "section_id": new_score.section_id
                })
            except Exception as e:
                db.session.rollback()
                return jsonify({"error": str(e)}), 500

        def get(self):
            try:
                scores = (
                    db.session.query(Score, User)
                    .join(User, Score.user_id == User.id)
                    .all()
                )

                result = [{
                    "id": score.id,
                    "value": score.value,
                    "section_id": score.section_id,
                    "user_id": score.user_id,
                    "player_name": user.name  # or user.username depending on what your User model uses
                } for score, user in scores]

                return jsonify(result)
            except Exception as e:
                return jsonify({"error": str(e)}), 500


    api.add_resource(_CRUD, '/scores')

# Register in main.py or __init__.py if not already done
# app.register_blueprint(score_api)
