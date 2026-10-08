"""Server-side verification of a Google Identity Services ID token."""
import requests
from flask import current_app

STUDENT_EMAIL_SUFFIX = "@stu.powayusd.com"
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")


def verified_student_email(id_token):
    """Return the lowercased email if id_token is a valid Google ID token for this app's
    client ID with a verified @stu.powayusd.com address, otherwise None. Never trusts an
    email the client merely claims; every check is done against Google's own response."""
    client_id = current_app.config.get("GOOGLE_CLIENT_ID")
    if not id_token or not client_id:
        return None
    try:
        response = requests.get(
            "https://oauth2.googleapis.com/tokeninfo",
            params={"id_token": id_token},
            timeout=5,
        )
        if response.status_code != 200:
            return None
        claims = response.json()
    except (requests.RequestException, ValueError):
        return None

    email = (claims.get("email") or "").lower()
    if (
        claims.get("aud") != client_id
        or claims.get("iss") not in GOOGLE_ISSUERS
        or str(claims.get("email_verified")).lower() != "true"
        or not email.endswith(STUDENT_EMAIL_SUFFIX)
    ):
        return None
    return email
