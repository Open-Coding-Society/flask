"""
Event Service API: events, RSVPs, check-in and files, under /api/event.

An event is the parent object of the Events system (Open-Coding-Society/pages#2089): a page on the site
(its page_path), its lifecycle, who signed up and who came, and its files. Pages builds the event page from
its front matter; this service keeps the live parts (status, notice, RSVPs, check-ins, files) and the whole
definition, so the Event Builder's work survives a reload. Every rule is in model/event.py, and the lifecycle,
the event's roles and every size are data in model/dataconfigs/event_rules.json.

The lifecycle as event_rules.json ships it:

    status        who sees it      what it allows             moves to
    draft         its managers     edit, files, delete        published
    published     everyone         edit, files                draft, registration, active, archived
    registration  everyone         edit, files, RSVPs         published, active, archived
    active        everyone         edit, files, check-in      completed
    completed     everyone         edit, files                archived
    archived      everyone         nothing: it is read only   (nothing)

"Allows" is open_for in event_rules.json, and the model checks it on every change: edits and roles (edit), RSVPs
and cancelling them, until the event starts (rsvp), the door code and checking someone in by name (check_in), and
adding files or changing their alt text (files). Removing a file is allowed in every status, so a manager can
always take a bad photo down.

An event's managers are staff, its owner, and its participants with a role in manager_roles (organizer).
Leaving draft the first time is publishing, which needs a role in publish_roles (the staff roles).

Who may make each request (model/dataconfigs/permissions.json says the same):

    anyone     GET /api/event, GET /api/event/options, GET /api/event/files
               (a draft, and its files, reach only its managers; a login is read when there is one)
    staff      POST /api/event (Teacher and Admin, the staff_roles in account_types.json)
    signed in  every other request; whether the caller manages the event is checked in the model

Ids go in the body or the query, never the path. The person acting is always the login, never a uid in the
body. Every error is {"message": ..., "code": ...}, with code a key of MESSAGES in model/event.py, except the
two Flask gives itself: a body that is not a JSON object (400) and a body over the size limit (413). A file
store that does not answer is a 503 with Retry-After. Only POST /api/event/files takes bodies over 5 MB.

Requests and expected results: api/event.http.
"""
from functools import wraps

from flask import Blueprint, current_app, g, request
from flask_restful import Api, Resource
from werkzeug.exceptions import HTTPException

from __init__ import db
from api.authorize import account_refusal, identify, token_required
from api.request_body import json_object
from model import account_types, event_rules
from model.event import MESSAGES, Event, EventError
from model.event_storage import EventStorageError

event_api = Blueprint('event_api', __name__, url_prefix='/api')

# API docs https://flask-restful.readthedocs.io/en/latest/api.html
api = Api(event_api)

STAFF = account_types.staff_roles()


def event_errors(func):
    """Every failure inside an Event Service request becomes {message, code}. Flask's own answers (the JSON body
    check's 400 and the size limit's 413) keep their status, so they are raised on, not turned into a 500."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except EventError as e:
            db.session.rollback()
            return e.response()
        except EventStorageError as e:
            db.session.rollback()
            current_app.logger.warning("Event Service: the file store did not answer: %s", e)
            # "A 500 is usually considered a server coding error" (spring#280): S3 being down is a 503
            return {'message': MESSAGES['storage_down'], 'code': 'storage_down'}, 503, {'Retry-After': '30'}
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001 - last resort, logged with the traceback
            db.session.rollback()
            current_app.logger.exception("Event Service error")
            return {'message': MESSAGES['server_error'], 'code': 'server_error'}, 500
    return wrapper


class EventResource(Resource):
    """Base for every Event Service resource: errors as {message, code} on all methods."""
    method_decorators = [event_errors]


def caller():
    """The account making this request, or None. Public reads take an optional login: a draft shows to its
    managers, and the viewer block says what the caller can do."""
    user, refusal = identify()
    if refusal or user is None or account_refusal(user, deactivate_expired=False):
        return None
    return user


def with_viewer(event, user, **extra):
    """An answer about one event: the event as this caller may see it, and what they can do with it."""
    return {**extra, 'event': event.read(user), 'viewer': event.viewer(user)}


class EventAPI:
    """
    Events, their lifecycle, RSVPs, check-in and files (see the module docstring for who may make each request).
    """

    class _CRUD(EventResource):  # Event API operation for Create, Read, Update, Delete
        def get(self):  # Read method: one event (?page_path= or ?id=), or a list (?status=a,b ?since=YYYY-MM-DD ?mine=true)
            user = caller()
            if 'page_path' in request.args:
                ''' A page with no event yet, or a draft the caller can't see, is event null, so the page logs no 404 '''
                event = Event.by_page(request.args.get('page_path'))
                if event is None or not event.visible_to(user):
                    return {'event': None, 'viewer': Event.caller_view(user)}
                return with_viewer(event, user)
            if 'id' in request.args:
                return with_viewer(Event.find(request.args.get('id'), user), user)

            ''' A list: every public event, or the events the caller manages, drafts included, without bodies or files '''
            mine = request.args.get('mine', '').lower() == 'true'
            if mine and user is None:
                raise EventError('mine_needs_login', 401)
            statuses, since = Event.filters(request.args.get('status'), request.args.get('since'))
            events = Event.listing(statuses, since, managed_by=user if mine else None)
            counts = Event.list_counts(events)  # one query for the whole list
            return {'events': [event.read(user, full=False, counts=counts[event.id]) for event in events], 'count': len(events),
                    'viewer': Event.caller_view(user)}

        @token_required(STAFF)
        def post(self):  # Create method: a draft from the event front matter keys (pages#2089)
            ''' Read data for json body '''
            body = json_object()

            ''' Check every key, then setup EVENT OBJECT and add it to DATABASE '''
            values = Event.validate(body)
            event = Event(values['page_path'], values['title'], g.current_user)
            event.create(values)
            return with_viewer(event, g.current_user), 201

        @token_required()
        def put(self):  # Update method: {id, ...the keys that change}
            body = json_object()
            event = Event.find(body.get('id'), g.current_user)
            event.update(body, g.current_user)
            return with_viewer(event, g.current_user)

        @token_required()
        def delete(self):  # Delete method: {id}, a draft nobody has joined
            body = json_object()
            Event.find(body.get('id'), g.current_user).delete(g.current_user)
            # 204 is the status code for delete with no json response
            return '', 204

    class _Options(EventResource):  # What a page needs to check an event the way this service does
        def get(self):
            return Event.options()

    class _Status(EventResource):  # The lifecycle: {id, status}
        @token_required()
        def put(self):
            body = json_object()
            event = Event.find(body.get('id'), g.current_user)
            event.transition(body.get('status'), g.current_user)
            return with_viewer(event, g.current_user)

    class _RSVP(EventResource):  # The caller signs up for an event, or cancels: {event_id}
        @token_required()
        def put(self):
            body = json_object()
            event = Event.find(body.get('event_id'), g.current_user, field='event_id')
            row = event.rsvp(g.current_user)
            return with_viewer(event, g.current_user, rsvp=row.rsvp)

        @token_required()
        def delete(self):
            body = json_object()
            event = Event.find(body.get('event_id'), g.current_user, field='event_id')
            rsvp = event.cancel_rsvp(g.current_user)
            return with_viewer(event, g.current_user, rsvp=rsvp)

    class _CheckIn(EventResource):  # The door code: an organizer shows it (?event_id=), people check in with it
        @token_required()
        def get(self):
            event = Event.find(request.args.get('event_id'), g.current_user, field='event_id')
            code, seconds_left = event.check_in_code(g.current_user)
            return {'code': code, 'seconds_left': seconds_left, 'code_seconds': event_rules.code_seconds()}

        @token_required()
        def post(self):  # {event_id, code}
            body = json_object()
            event = Event.find(body.get('event_id'), g.current_user, field='event_id')
            row = event.check_in(g.current_user, body.get('code'))
            return with_viewer(event, g.current_user, checked_in_at=row.read()['checked_in_at'])

    class _Participants(EventResource):  # The roster, for the event's managers
        @token_required()
        def get(self):  # ?event_id=
            event = Event.find(request.args.get('event_id'), g.current_user, field='event_id')
            return {'participants': [row.read() for row in event.roster(g.current_user)], 'counts': event.counts()}

        @token_required()
        def put(self):  # {event_id, uid, checked_in?, role?}: check someone in by name, or give them a role
            body = json_object()
            event = Event.find(body.get('event_id'), g.current_user, field='event_id')
            row = event.set_participant(g.current_user, body.get('uid'), checked_in=body.get('checked_in'), role=body.get('role'))
            return {'participant': row.read()}

        @token_required()
        def delete(self):  # {event_id, uid}
            body = json_object()
            event = Event.find(body.get('event_id'), g.current_user, field='event_id')
            event.remove_participant(g.current_user, body.get('uid'))
            return '', 204

    class _Files(EventResource):  # Documents, gallery photos with alt text, and hosted video links
        def get(self):  # ?id=: the file itself, to anyone who can see its event
            _, row = Event.find_file(request.args.get('id'), caller())
            return row.answer()

        @token_required()
        def post(self):  # {event_id, group, kind, name, content (base64), alt} or {event_id, group, kind, name, link}
            # Only this route takes big bodies (files up to max_bytes as base64); every other keeps the 5 MB limit
            request.max_content_length = event_rules.upload_cap()
            body = json_object()
            event = Event.find(body.get('event_id'), g.current_user, field='event_id')
            row = event.add_file(g.current_user, body.get('group'), body.get('kind'), body.get('name'),
                                 content=body.get('content'), link=body.get('link'), alt=body.get('alt'))
            return {'file': row.read()}, 201

        @token_required()
        def put(self):  # {id, alt}: a photo's alt text
            body = json_object()
            event, row = Event.find_file(body.get('id'), g.current_user)
            return {'file': event.set_file_alt(g.current_user, row.id, body.get('alt')).read()}

        @token_required()
        def delete(self):  # {id}
            body = json_object()
            event, row = Event.find_file(body.get('id'), g.current_user)
            event.remove_file(g.current_user, row.id)
            return '', 204

    # building RESTapi endpoints
    api.add_resource(_CRUD, '/event')
    api.add_resource(_Options, '/event/options')
    api.add_resource(_Status, '/event/status')
    api.add_resource(_RSVP, '/event/rsvp')
    api.add_resource(_CheckIn, '/event/checkin')
    api.add_resource(_Participants, '/event/participants')
    api.add_resource(_Files, '/event/files')
