import os
from flask import request, jsonify, Blueprint, send_from_directory
from werkzeug.utils import secure_filename
from model.microblog import MicroBlog, Topic
from __init__ import db, app

microblog_api = Blueprint("microblog_api", __name__)

# -----------------------
# Upload folder setup
# -----------------------
UPLOAD_FOLDER = os.path.join(os.getcwd(), "uploads", "microblog")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER


# ============================================================
#                CREATE A MICROBLOG POST
# ============================================================
@microblog_api.route("/api/microblog", methods=["POST"])
def create_microblog():
    try:
        # If user uploads a file, request type = multipart/form-data

        # Accept both form and JSON body
        if request.form:
            user_id = request.form.get("userId")
            content = request.form.get("content")
            topic_id = request.form.get("topicId")
        else:
            data_json = request.get_json(silent=True) or {}
            user_id = data_json.get("userId")
            content = data_json.get("content")
            topic_id = data_json.get("topicId")

        if not user_id or not content:
            return jsonify({
                "error": f"Missing userId or content.",
                "received": {"userId": user_id, "content": content}
            }), 400

        try:
            user_id_int = int(user_id)
        except Exception:
            return jsonify({"error": f"userId must be an integer, got: {user_id}"}), 400

        # Check if user exists
        from model.user import User
        user = User.query.get(user_id_int)
        if not user:
            return jsonify({"error": f"User with id {user_id_int} does not exist."}), 400

        if topic_id is not None and topic_id != '':
            try:
                topic_id = int(topic_id)
            except Exception:
                topic_id = None

        # Defensive: ensure file upload errors are clear
        if "file" in request.files:
            file = request.files["file"]
            if file and not file.filename:
                return jsonify({"error": "File upload present but filename is empty."}), 400

        data = {}

        # ---------------------
        # FILE UPLOAD HANDLING
        # ---------------------
        if "file" in request.files:
            file = request.files["file"]

            if file and file.filename:
                filename = secure_filename(file.filename)
                filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
                file.save(filepath)

                # Include file metadata in _data JSON column
                data["file"] = {
                    "filename": filename,
                    "url": f"/api/microblog/file/{filename}",
                }

        # ---------------------
        # CREATE POST
        # ---------------------
        post = MicroBlog(
            user_id=user_id,
            content=content,
            topic_id=topic_id,
            data=data
        )

        created = post.create()
        if not created:
            return jsonify({"error": "Failed to create post"}), 500
        
        if "file" in data:
            post._data = post._data or {}
            post._data["file"] = data["file"]
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(post, "_data")
            db.session.commit()

        return jsonify(post.read()), 201

    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


# ============================================================
#                GET ALL POSTS
# ============================================================
@microblog_api.route("/api/microblog", methods=["GET"])
def get_microblogs():
    posts = MicroBlog.query.all()
    return jsonify({"microblogs": [post.read() for post in posts]})


# ============================================================
#                DOWNLOAD ATTACHMENT
# ============================================================
@microblog_api.route("/api/microblog/file/<filename>", methods=["GET"])
def microblog_download_file(filename):
    return send_from_directory(
        app.config["UPLOAD_FOLDER"],
        filename,
        as_attachment=True
    )


# ============================================================
#                GET TOPICS
# ============================================================
@microblog_api.route("/api/microblog/topics", methods=["GET"])
def get_topics():
    topics = Topic.query.all()
    return jsonify([topic.read() for topic in topics])