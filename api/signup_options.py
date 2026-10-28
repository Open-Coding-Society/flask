from flask import Blueprint
from flask_restful import Api, Resource

from model import schools

signup_options_api = Blueprint('signup_options_api', __name__, url_prefix='/api')
api = Api(signup_options_api)


class _SignupOptions(Resource):
    """What the sign-up form offers, read from the data files in model/. Public: the form is shown
    to people who have no account yet, and nothing here is about any one person."""
    def get(self):
        return {'schools': schools.schools()}


api.add_resource(_SignupOptions, '/signup-options')
