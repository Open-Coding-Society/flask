"""
=============================================================================
GEMINI AI API - Text Analysis & Citation Checking
=============================================================================
Google's Gemini API for AI-powered text analysis, citation checking, and
general language model tasks.

SETUP REQUIRED:
1. Get an API key from: https://aistudio.google.com/app/apikey
2. Add to your .env file:
   GEMINI_API_KEY=your_key_here
   GEMINI_SERVER=https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent

ENDPOINTS PROVIDED:
- POST /api/gemini         - Main text analysis (citation checking, custom prompts)
- GET  /api/gemini/health  - Health check and configuration status
- POST /api/gemini/debug   - Debug endpoint for troubleshooting API issues

AUTHENTICATION:
All endpoints require authentication via token (uses @token_required decorator).
Include your auth token in request headers.

DEFAULT BEHAVIOR:
- Default prompt performs academic citation analysis (APA format)
- Custom prompts can be provided via the 'prompt' field
- 90 second timeout for API requests

USAGE EXAMPLE (JavaScript frontend):
    fetch('/api/gemini', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer your_token_here'
        },
        credentials: 'include',
        body: JSON.stringify({
            text: 'Your text to analyze here...',
            prompt: 'Optional custom prompt (defaults to citation analysis)'
        })
    })

RESPONSE FORMAT:
    Success: { "success": true, "text": "generated response", "user": "user_id" }
    Error:   { "message": "error description", "error_code": 500 }

ERROR CODES:
- 400: Bad request (missing text field or invalid input)
- 429: Rate limit exceeded
- 500: Server error or API configuration issue
- 503: Gemini API temporarily unavailable
=============================================================================
"""
from __init__ import app
from flask import Blueprint, request, jsonify, current_app, g
from flask_restful import Api, Resource
import requests
import time
import os
import io
import json
import re
from api.authorize import token_required

MAX_GEMINI_RETRIES = 3
GEMINI_RETRY_STATUS_CODES = {429, 503}

# Submission summarizer: one Gemini call covers up to this many submissions at once.
MAX_SUMMARY_BATCH_ITEMS = 10
MAX_SUMMARY_ITEM_CHARS = 3000

GITHUB_ISSUE_URL_RE = re.compile(r'^https?://github\.com/([^/]+)/([^/]+)/(issues|pull)/(\d+)')
GOOGLE_DOC_URL_RE = re.compile(r'^https?://docs\.google\.com/document/d/([a-zA-Z0-9_-]+)')

# =============================================================================
# AUTH DECORATOR (DEV-ONLY BYPASS)
# =============================================================================

def require_auth_if_production(f):
    """
    Dev-only bypass: require auth only in production.
    In development (IS_PRODUCTION not set), endpoints are public.
    In production, endpoints require token_required() authentication.
    """
    is_production = os.environ.get('IS_PRODUCTION', 'false').lower() == 'true'
    if is_production:
        return token_required()(f)
    return f

def require_admin_if_production(f):
    """
    Dev-only bypass: require Admin/Teacher role only in production.
    In development (IS_PRODUCTION not set), endpoints are public.
    In production, endpoints require an Admin or Teacher role (mirrors the
    admin/teacher gate on grading submissions in the Spring backend).
    """
    is_production = os.environ.get('IS_PRODUCTION', 'false').lower() == 'true'
    if is_production:
        return token_required(roles=["Admin", "Teacher"])(f)
    return f

# =============================================================================
# BLUEPRINT SETUP
# =============================================================================

gemini_api = Blueprint('gemini_api', __name__, url_prefix='/api')
api = Api(gemini_api)

# =============================================================================
# HELPERS
# =============================================================================

def _call_gemini(payload):
    """
    POST a payload to Gemini with retry on 429/503.

    Returns the generated text (str) on success, or an (error_dict, status_code)
    tuple on failure -- callers should check `isinstance(result, tuple)`.
    """
    api_key = app.config.get('GEMINI_API_KEY')
    server = app.config.get('GEMINI_SERVER')

    if not api_key:
        return {'message': 'Gemini API key not configured'}, 500

    if not server:
        return {'message': 'Gemini server not configured'}, 500

    endpoint = f"{server}?key={api_key}"

    try:
        for attempt in range(1, MAX_GEMINI_RETRIES + 1):
            response = requests.post(
                endpoint,
                headers={'Content-Type': 'application/json'},
                json=payload,
                timeout=90
            )

            if response.status_code == 200:
                break

            error_details = {
                'status_code': response.status_code,
                'response_text': response.text,
                'endpoint': endpoint,
                'headers': dict(response.headers)
            }
            current_app.logger.error(f"Gemini API error: {error_details}")

            # Retry on 429/503 if attempts remain
            if response.status_code in GEMINI_RETRY_STATUS_CODES and attempt < MAX_GEMINI_RETRIES:
                retry_after = response.headers.get('Retry-After')
                try:
                    wait_seconds = int(retry_after)
                except (TypeError, ValueError):
                    wait_seconds = 2 ** attempt

                current_app.logger.warning(
                    f"Gemini rate limited (status={response.status_code}); "
                    f"retry {attempt}/{MAX_GEMINI_RETRIES} in {wait_seconds}s"
                )
                time.sleep(wait_seconds)
                continue

            # All retries exhausted or non-retryable error
            if response.status_code == 503:
                return {
                    'message': 'Gemini API is temporarily unavailable (503). Please try again later.',
                    'error_code': 503,
                    'details': 'The service may be overloaded or under maintenance.'
                }, 503
            elif response.status_code == 429:
                return {
                    'message': 'Rate limit exceeded after retries. Please try again later.',
                    'error_code': 429
                }, 429
            elif response.status_code == 400:
                return {
                    'message': 'Bad request to Gemini API. Please check your input.',
                    'error_code': 400,
                    'details': response.text
                }, 400
            else:
                return {
                    'message': f'Gemini API error: {response.status_code}',
                    'error_code': response.status_code,
                    'details': response.text
                }, 500

        result = response.json()
        try:
            return result['candidates'][0]['content']['parts'][0]['text']
        except (KeyError, IndexError) as e:
            current_app.logger.error(f"Error parsing Gemini response: {e}")
            return {
                'success': False,
                'message': 'Error parsing Gemini API response',
                'raw_response': result
            }, 500

    except requests.RequestException as e:
        current_app.logger.error(f"Error communicating with Gemini API: {e}")
        return {'message': f'Error communicating with Gemini API: {str(e)}'}, 500
    except Exception as e:
        current_app.logger.error(f"Unexpected error communicating with Gemini API: {e}")
        return {'message': f'Unexpected error: {str(e)}'}, 500


def analyze_log_text(log_text):
    """Send a Jekyll build log to Gemini and return the parsed response."""
    system_prompt = """
You are an AI assistant that analyzes Jekyll build logs.

Your job:
1. Determine whether the build succeeded or failed.
2. If it failed, identify the most likely cause.
3. Recommend what a student should do to fix the build or Makefile.
4. Do not print the full log contents.
5. Keep the answer concise and actionable.
"""

    log_text = log_text[-8000:]
    payload = {
        "contents": [{
            "parts": [{
                "text": f"{system_prompt}\n\n{log_text}"
            }]
        }]
    }

    current_app.logger.info("Gemini log analysis request made")

    result = _call_gemini(payload)
    if isinstance(result, tuple):
        return result

    return {
        'success': True,
        'analysis': result.strip()
    }


def extract_text(filename, raw_bytes):
    """
    Extract plain text from raw file bytes based on the file's extension.

    Returns the extracted text, or None if the file is empty/unreadable/unsupported
    (callers treat None as "no summary available" rather than failing the batch).
    """
    if not filename or not raw_bytes:
        return None

    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    plain_text_extensions = {
        'txt', 'md', 'py', 'java', 'js', 'ts', 'jsx', 'tsx', 'html', 'css',
        'json', 'csv', 'c', 'cpp', 'h', 'sh', 'yml', 'yaml'
    }

    try:
        if ext == 'pdf':
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(raw_bytes))
            return '\n'.join(page.extract_text() or '' for page in reader.pages).strip() or None

        if ext == 'docx':
            from docx import Document
            document = Document(io.BytesIO(raw_bytes))
            return '\n'.join(p.text for p in document.paragraphs).strip() or None

        if ext in plain_text_extensions:
            return raw_bytes.decode('utf-8', errors='replace').strip() or None

    except Exception as e:
        current_app.logger.error(f"Failed to extract text from '{filename}': {e}")
        return None

    return None


def extract_text_from_github_issue(owner, repo, kind, number):
    """
    Fetch a GitHub issue/PR's title, body, and first few comments via the REST API.

    Returns the combined text, or None if the request fails (private repo,
    deleted issue, rate limit, etc.) -- callers fall back to the raw URL.
    """
    token = app.config.get('GITHUB_TOKEN')
    headers = {'Accept': 'application/vnd.github+json'}
    if token:
        headers['Authorization'] = f'token {token}'

    try:
        issue_url = f'https://api.github.com/repos/{owner}/{repo}/issues/{number}'
        resp = requests.get(issue_url, headers=headers, timeout=15)

        if resp.status_code == 401 and token:
            # An invalid/placeholder token is rejected outright rather than falling
            # back to anonymous access -- retry unauthenticated for public repos.
            current_app.logger.warning(f"GITHUB_TOKEN rejected (401); retrying {owner}/{repo}#{number} unauthenticated")
            headers.pop('Authorization', None)
            resp = requests.get(issue_url, headers=headers, timeout=15)

        if resp.status_code != 200:
            current_app.logger.warning(f"GitHub {kind} fetch failed ({resp.status_code}): {owner}/{repo}#{number}")
            return None

        data = resp.json()
        parts = [f"Title: {data.get('title', '')}", f"Body:\n{data.get('body') or '(no description)'}"]

        if data.get('comments', 0) > 0 and data.get('comments_url'):
            c_resp = requests.get(data['comments_url'], headers=headers, timeout=15, params={'per_page': 5})
            if c_resp.status_code == 200:
                for comment in c_resp.json()[:5]:
                    author = (comment.get('user') or {}).get('login', 'unknown')
                    parts.append(f"Comment by {author}: {comment.get('body', '')}")

        return '\n\n'.join(parts).strip() or None

    except requests.RequestException as e:
        current_app.logger.error(f"Failed to fetch GitHub {kind} {owner}/{repo}#{number}: {e}")
        return None


def extract_text_from_google_doc(doc_id):
    """
    Fetch a publicly-viewable Google Doc's plain text via its export endpoint.

    Returns the doc text, or None if it's not publicly shared or the fetch fails
    -- callers fall back to the raw URL.
    """
    try:
        resp = requests.get(
            f'https://docs.google.com/document/d/{doc_id}/export?format=txt',
            timeout=15
        )
        if resp.status_code != 200 or 'text/plain' not in resp.headers.get('Content-Type', ''):
            # A private/unshared doc redirects to an HTML login or "request access" page
            # instead of returning the plain-text export.
            current_app.logger.warning(f"Google Doc {doc_id} is not publicly viewable")
            return None
        return resp.text.strip() or None

    except requests.RequestException as e:
        current_app.logger.error(f"Failed to fetch Google Doc {doc_id}: {e}")
        return None


def extract_text_from_url(url):
    """
    Best-effort fetch of readable text for a submission link.

    Recognizes GitHub issue/PR links and public Google Doc links; anything else
    returns None so the caller falls back to summarizing the raw URL string.
    """
    if not url:
        return None
    url = url.strip()

    github_match = GITHUB_ISSUE_URL_RE.match(url)
    if github_match:
        owner, repo, kind, number = github_match.groups()
        text = extract_text_from_github_issue(owner, repo, kind, number)
        return text[:MAX_SUMMARY_ITEM_CHARS] if text else None

    doc_match = GOOGLE_DOC_URL_RE.match(url)
    if doc_match:
        text = extract_text_from_google_doc(doc_match.group(1))
        return text[:MAX_SUMMARY_ITEM_CHARS] if text else None

    return None


def summarize_submission_batch(items):
    """
    Summarize up to MAX_SUMMARY_BATCH_ITEMS submissions with a single Gemini call,
    also scoring each one's feedback quality from 1-5 in the same call.

    `items` is a list of dicts: {id, assignment_name, submitter_name, text}.
    Returns {'success': True, 'summaries': [{'id':.., 'summary':.., 'quality_score':..}, ...]}
    or an (error_dict, status_code) tuple on failure.
    """
    system_prompt = """
You are an AI assistant helping a teacher grade student assignment submissions.

For each submission listed below, do two things:
1. Write a concise 2-3 sentence summary of what the student submitted, in plain
   language a grader can skim before opening the full submission.
2. Assign a Quality Score from 1-5 rating how thorough and useful the student's
   feedback/submission is, using this rubric:
   5 (Exemplar): Outstanding, highly structured feedback covering nearly all
     relevant categories or aspects, with specific details, reproduction steps,
     or actionable proposals.
   4 (High Quality): Strong submission detailing multiple specific points, clear
     observations, and actionable insights across several categories.
   3 (Moderate Quality): Clear submission identifying 2-3 standard issues or
     points with sufficient context.
   2 (Basic Quality): Brief submission addressing 1-2 surface-level issues with
     minimal detail.
   1 (Minimal Quality): Very minimal comments, placeholder notes, or a duplicate
     submission with no distinct additional input.

Respond with ONLY a JSON array, no other text and no markdown code fences, in this
exact format:
[{"id": "<submission id>", "summary": "<summary text>", "quality_score": <integer 1-5>}, ...]

Include exactly one entry per submission listed below, using the exact id given.
If a submission's content is empty or unreadable, use "No readable content to
summarize." as its summary and a quality_score of 1.
"""

    sections = []
    for item in items:
        text = (item.get('text') or '').strip()[:MAX_SUMMARY_ITEM_CHARS]
        sections.append(
            f"Submission id: {item['id']}\n"
            f"Assignment: {item.get('assignment_name') or 'Unknown'}\n"
            f"Student: {item.get('submitter_name') or 'Unknown'}\n"
            f"Content:\n{text or '(no readable content)'}"
        )

    prompt_text = system_prompt + "\n\n" + "\n\n---\n\n".join(sections)
    payload = {"contents": [{"parts": [{"text": prompt_text}]}]}

    current_app.logger.info(f"Gemini batch submission summary request made for {len(items)} submissions")

    result = _call_gemini(payload)
    if isinstance(result, tuple):
        return result

    raw_text = result.strip()
    if raw_text.startswith('```'):
        raw_text = raw_text.strip('`')
        if raw_text.lower().startswith('json'):
            raw_text = raw_text[4:]
        raw_text = raw_text.strip()

    try:
        parsed = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError) as e:
        current_app.logger.error(f"Failed to parse Gemini batch summary response: {e}; raw: {raw_text[:500]}")
        return {
            'message': 'Could not parse Gemini response as JSON',
            'raw_response': raw_text
        }, 500

    # Clamp/validate quality_score to an int 1-5; anything unparseable becomes
    # None rather than failing the whole batch over one malformed field.
    if isinstance(parsed, list):
        for entry in parsed:
            if not isinstance(entry, dict):
                continue
            try:
                score = int(entry.get('quality_score'))
                entry['quality_score'] = score if 1 <= score <= 5 else None
            except (TypeError, ValueError):
                entry['quality_score'] = None

    return {'success': True, 'summaries': parsed}

# =============================================================================
# ENDPOINTS
# =============================================================================

class GeminiAPI:
    class _Ask(Resource):
        """
        Main analysis endpoint - POST /api/gemini
        Handles text analysis with customizable prompts.
        Default: Academic citation checking (APA format).
        """
        @require_auth_if_production
        def post(self):
            """
            Send a request to the Gemini API.

            Expected JSON body:
            {
                "text": "Text to analyze",
                "prompt": "Optional custom prompt" (defaults to citation analysis)
            }

            Returns:
                JSON response from Gemini API or error message
            """
            current_user = g.current_user
            body = request.get_json()

            # Validate request body
            if not body:
                return {'message': 'Request body is required'}, 400

            text = body.get('text', '')
            if not text:
                return {'message': 'Text field is required'}, 400

            # Get configuration
            api_key = app.config.get('GEMINI_API_KEY')
            server = app.config.get('GEMINI_SERVER')

            if not api_key:
                return {'message': 'Gemini API key not configured'}, 500

            if not server:
                return {'message': 'Gemini server not configured'}, 500

            # Build the endpoint URL
            endpoint = f"{server}?key={api_key}"

            # Default prompt for citation analysis, can be overridden
            default_prompt = "Please look at this text for correct academic citations, and recommend APA references for each area of concern"
            prompt = body.get('prompt', default_prompt)

            # Prepare the request payload for Gemini API
            payload = {
                "contents": [{
                    "parts": [{
                        "text": f"{prompt}: {text}"
                    }]
                }]
            }

            # Log the request for auditing purposes
            current_app.logger.info(f"User {current_user.uid} made a Gemini API request")

            try:
                current_app.logger.info(f"Making request to Gemini API: {endpoint}")
                current_app.logger.debug(f"Payload: {payload}")

                response = requests.post(
                    endpoint,
                    headers={'Content-Type': 'application/json'},
                    json=payload,
                    timeout=90
                )

                if response.status_code != 200:
                    error_details = {
                        'status_code': response.status_code,
                        'response_text': response.text,
                        'endpoint': endpoint,
                        'headers': dict(response.headers)
                    }
                    current_app.logger.error(f"Gemini API error: {error_details}")

                    if response.status_code == 503:
                        return {
                            'message': 'Gemini API is temporarily unavailable (503). Please try again later.',
                            'error_code': 503,
                            'details': 'The service may be overloaded or under maintenance.'
                        }, 503
                    elif response.status_code == 429:
                        return {
                            'message': 'Rate limit exceeded. Please try again later.',
                            'error_code': 429
                        }, 429
                    elif response.status_code == 400:
                        return {
                            'message': 'Bad request to Gemini API. Please check your input.',
                            'error_code': 400,
                            'details': response.text
                        }, 400
                    else:
                        return {
                            'message': f'Gemini API error: {response.status_code}',
                            'error_code': response.status_code,
                            'details': response.text
                        }, 500

                result = response.json()

                try:
                    generated_text = result['candidates'][0]['content']['parts'][0]['text']
                    return {
                        'success': True,
                        'text': generated_text,
                        'user': current_user.uid
                    }
                except (KeyError, IndexError) as e:
                    current_app.logger.error(f"Error parsing Gemini response: {e}")
                    return {
                        'success': False,
                        'message': 'Error parsing Gemini API response',
                        'raw_response': result
                    }, 500

            except requests.RequestException as e:
                current_app.logger.error(f"Error communicating with Gemini API: {e}")
                return {'message': f'Error communicating with Gemini API: {str(e)}'}, 500
            except Exception as e:
                current_app.logger.error(f"Unexpected error in Gemini API: {e}")
                return {'message': f'Unexpected error: {str(e)}'}, 500

    class _Health(Resource):
        """
        Health check - GET /api/gemini/health
        Verifies API configuration and tests connectivity.
        """
        @require_auth_if_production
        def get(self):
            """
            Check if Gemini API is properly configured.

            Returns:
                JSON response indicating configuration status
            """
            api_key = app.config.get('GEMINI_API_KEY')
            server = app.config.get('GEMINI_SERVER')

            status_info = {
                'gemini_configured': bool(api_key and server),
                'server': server if server else 'Not configured',
                'api_key_present': bool(api_key)
            }

            if api_key and server:
                try:
                    test_endpoint = f"{server}?key={api_key}"
                    test_payload = {
                        "contents": [{
                            "parts": [{"text": "Hello"}]
                        }]
                    }

                    response = requests.post(
                        test_endpoint,
                        headers={'Content-Type': 'application/json'},
                        json=test_payload,
                        timeout=10
                    )

                    status_info['api_test'] = {
                        'status_code': response.status_code,
                        'available': response.status_code == 200
                    }

                    if response.status_code != 200:
                        status_info['api_test']['error'] = response.text

                except Exception as e:
                    status_info['api_test'] = {
                        'available': False,
                        'error': str(e)
                    }

            return status_info

    class _LogAnalyzer(Resource):
        """
        Log analysis endpoint - POST /api/gemini/analyze-log
        Analyzes Jekyll build logs using AI.
        """
        @require_auth_if_production
        def post(self):
            """
            Analyze a Jekyll build log.

            Expected JSON body:
            {
                "log": "The log content to analyze"
            }

            Returns:
                JSON response with analysis or error message
            """
            body = request.get_json()

            if not body:
                return {'message': 'Request body is required'}, 400

            log = body.get('log', '')
            if not log:
                return {'message': 'Log field is required'}, 400

            return analyze_log_text(log)

    class _LogUpload(Resource):
        """
        File upload endpoint - POST /api/gemini/upload
        Accepts a file and analyzes its contents as a Jekyll build log.
        """
        @require_auth_if_production
        def post(self):
            if 'file' not in request.files:
                return {'message': 'File is required'}, 400

            file = request.files['file']
            if file.filename == '':
                return {'message': 'File name is required'}, 400

            try:
                content = file.read().decode('utf-8', errors='replace')
            except Exception as e:
                current_app.logger.error(f"Failed to read uploaded file: {e}")
                return {'message': f'File processing failed: {str(e)}'}, 400

            return analyze_log_text(content)

    class _SummarizeSubmissions(Resource):
        """
        Batch submission summarizer - POST /api/gemini/summarize-submissions
        Admin/Teacher only.

        Accepts up to MAX_SUMMARY_BATCH_ITEMS submissions as multipart form data and
        returns one AI-generated summary per submission from a single Gemini call.

        Expected form fields per item i (0-indexed, contiguous starting at 0):
            id_i             - submission id (echoed back in the response)
            type_i           - "text" | "link" | "file"
            text_i           - submission text/notes, or the URL for link submissions
                                (GitHub issue/PR and public Google Doc links are fetched
                                and summarized by content; other links are summarized
                                by URL alone)
            file_i           - (type "file" only) the uploaded file bytes
            filename_i       - (type "file" only) original filename, used to pick an extractor
            assignmentName_i - optional, for prompt context
            submitterName_i  - optional, for prompt context
        """
        @require_admin_if_production
        def post(self):
            form = request.form
            files = request.files

            count = 0
            while f'id_{count}' in form:
                count += 1

            if count == 0:
                return {'message': 'No submission items provided'}, 400
            if count > MAX_SUMMARY_BATCH_ITEMS:
                return {'message': f'Too many items; max {MAX_SUMMARY_BATCH_ITEMS} per batch'}, 400

            items = []
            for i in range(count):
                item_type = (form.get(f'type_{i}') or '').lower()
                text_value = form.get(f'text_{i}', '')

                if item_type == 'file' and f'file_{i}' in files:
                    file_storage = files[f'file_{i}']
                    filename = form.get(f'filename_{i}') or file_storage.filename or ''
                    try:
                        raw_bytes = file_storage.read()
                    except Exception as e:
                        current_app.logger.error(f"Failed to read uploaded file {filename}: {e}")
                        raw_bytes = None
                    extracted = extract_text(filename, raw_bytes)
                    text_value = extracted or ''
                elif item_type == 'link':
                    fetched = extract_text_from_url(text_value)
                    text_value = f"Link: {text_value}\n\n{fetched}" if fetched else text_value

                items.append({
                    'id': form.get(f'id_{i}'),
                    'assignment_name': form.get(f'assignmentName_{i}', 'Unknown'),
                    'submitter_name': form.get(f'submitterName_{i}', 'Unknown'),
                    'text': text_value,
                })

            current_user = getattr(g, 'current_user', None)
            requester = getattr(current_user, 'uid', 'dev-user')
            current_app.logger.info(f"User {requester} requested a submission summary batch of {count}")

            return summarize_submission_batch(items)

    class _Debug(Resource):
        """
        Debug endpoint to help troubleshoot Gemini API issues.
        """
        @require_auth_if_production
        def post(self):
            """
            Debug the Gemini API request to identify 503 issues.

            Returns detailed information about the request and response.
            """
            current_user = g.current_user
            body = request.get_json()

            api_key = app.config.get('GEMINI_API_KEY')
            server = app.config.get('GEMINI_SERVER')

            debug_info = {
                'user': current_user.uid,
                'config_check': {
                    'api_key_present': bool(api_key),
                    'api_key_length': len(api_key) if api_key else 0,
                    'server': server,
                    'server_valid': bool(server and server.startswith('https://'))
                },
                'request_body': body
            }

            if not api_key or not server:
                debug_info['error'] = 'Missing API configuration'
                return debug_info, 500

            endpoint = f"{server}?key={api_key}"
            debug_info['endpoint'] = endpoint

            test_payload = {
                "contents": [{
                    "parts": [{"text": "Test"}]
                }]
            }

            try:
                response = requests.post(
                    endpoint,
                    headers={'Content-Type': 'application/json'},
                    json=test_payload,
                    timeout=30
                )

                debug_info['response'] = {
                    'status_code': response.status_code,
                    'headers': dict(response.headers),
                    'content': response.text[:500] if response.text else None
                }

                return debug_info

            except Exception as e:
                debug_info['exception'] = str(e)
                return debug_info, 500

    # Register all endpoints
    api.add_resource(_Ask, '/gemini')                        # Main analysis endpoint
    api.add_resource(_Health, '/gemini/health')              # Health check
    api.add_resource(_Debug, '/gemini/debug')                # Debug/troubleshooting
    api.add_resource(_LogAnalyzer, '/gemini/analyze-log')    # Log analysis endpoint
    api.add_resource(_LogUpload, '/gemini/upload')           # File upload log analysis endpoint
    api.add_resource(_SummarizeSubmissions, '/gemini/summarize-submissions')  # Batch submission summarizer