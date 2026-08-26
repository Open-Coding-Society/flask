from flask import Blueprint, request
from flask_restful import Api, Resource
import requests

python_exec_api = Blueprint('python_exec_api', __name__, url_prefix='/run')

api = Api(python_exec_api)

RUNNER_URL = "http://code_runner:8591/python"

class PythonExec(Resource):
    def post(self):
        data = request.get_json(silent=True) or {}

        try:
            response = requests.post(
                RUNNER_URL,
                json=data,
                timeout=10
            )

            return response.json(), response.status_code

        except requests.Timeout:
            return {
                "output": "⏱️ Runner timed out."
            }, 504

        except requests.RequestException as e:
            return {
                "output": f"❌ Could not connect to code runner: {str(e)}"
            }, 502


api.add_resource(PythonExec, "/python")
