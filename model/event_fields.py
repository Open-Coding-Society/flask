""" Events: what each field of an event's definition must be, and what an uploaded file must start with

model/event.py keeps the rules about an event (who, when, which status); this module keeps the checks of the
values themselves, so they can be read in one place:

  DEFINITION_FIELDS  the event front matter keys (pages#2089) the service keeps in _data, each with its kind
  clean()            one field's value as it is saved, or FieldError
  DATE, CLOCK, ...   the patterns a date, a time, a link and an attachment group must match
  video_link()       a gallery video link as it is saved, or None when it is not one
  SIGNATURES         what a file of each extension starts with, so its contents must match its name

A FieldError carries ids and numbers, not English, so model/event.py answers it as bad_field and a page words it.
"""
import io
import re
import zipfile
from urllib.parse import urlsplit, urlunsplit

from model import event_rules

# What each kind of definition field must be, in bad_field's message
KIND_WORDS = {
    'text': "text", 'name': "text that is not blank", 'text_list': "a list of text", 'text_map': "a set of names and text",
    'map_list': "a list of entries", 'object': "a set of fields", 'link': "a site path (/...) or an https link",
    'days': "a whole number of days, 0 or more", 'markdown': "Markdown text", 'id': "a whole number or null",
}
LIST_KINDS = ('text_list', 'text_map', 'map_list')  # their bad_field also says how many items

# A kind is (kind, the limit in event_rules.json that sizes it), or a dict for an object with a kind per key.
TEXT, NAME, LINK = ('text', 'text'), ('name', 'text'), ('link', 'text')
DEFINITION_FIELDS = {
    'description': ('text', 'description'),
    'categories': ('text_list', 'text'),
    'event_type': NAME,
    'location': {'name': TEXT, 'address': TEXT, 'url': LINK},
    'organizer': {'name': TEXT, 'uids': ('text_list', 'text')},
    'attachments': ('map_list', {'type': NAME, 'id': NAME}),
    'image': LINK,
    'image_alt': TEXT,
    'image_credit': {'name': TEXT, 'url': LINK},
    'presenters': ('map_list', {'name': NAME, 'role': TEXT}),
    'schedule': ('map_list', {'time': TEXT, 'title': NAME, 'detail': TEXT}),
    'presentation': LINK,
    'template': NAME,
    'labels': ('text_map', 'text'),
    'alert_days': ('days', 'alert_days'),
    'body': ('markdown', 'body'),
}

DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")  # [0-9], not \d, which takes other scripts' digits too
CLOCK = re.compile(r"[0-9]{2}:[0-9]{2}")
# One character of a URL as RFC 3986 writes it (unreserved, reserved, or %XX), less the single quote. That leaves out
# spaces, quotes, < >, \ and control characters: a browser reads \ as /, so https://evil.test\@www.youtube.com goes
# to evil.test while urlsplit reads youtube, and a quote or < could close the HTML attribute a page puts the link in.
URL_CHARACTER = r"(?:[A-Za-z0-9\-._~:/?#\[\]@!$&()*+,;=]|%[0-9A-Fa-f]{2})"
URL = re.compile(URL_CHARACTER + "+")
SITE_PATH_OR_HTTPS = re.compile(rf"/(?!/){URL_CHARACTER}*|https://{URL_CHARACTER}+", re.IGNORECASE)  # never //host, which leaves the site
GROUP = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


def limit_words(kind, limit=None, items=None, keys=None):
    """A kind's limit in words, for bad_field's message."""
    if kind == 'object':
        return "the keys " + ", ".join(keys)
    if kind == 'id':
        return "one number"
    if kind in ('text_list', 'text_map'):
        return f"{items} items of up to {limit} characters"
    if kind == 'map_list':
        return f"{items} items"
    if kind == 'days':
        return f"{limit} days"
    return f"{limit:,} characters"


class FieldError(ValueError):
    """A field that breaks its kind's rule. fields is what the answer carries, as ids and numbers: field, kind (a
    KIND_WORDS key), limit (null when the kind has none), items for a list and keys for an object. words say the
    same for the message."""

    def __init__(self, field, kind, limit=None, items=None, keys=None):
        self.fields = {'field': field, 'kind': kind, 'limit': limit}
        if items is not None:
            self.fields['items'] = items
        if keys is not None:
            self.fields['keys'] = list(keys)
        self.words = {'kind': KIND_WORDS[kind], 'limit': limit_words(kind, limit, items, keys)}
        super().__init__(f"{field} must be {self.words['kind']} within {self.words['limit']}")


def clean(field, kind, value):
    """The value of one definition field as it is saved, or FieldError. Unknown keys inside an object are dropped,
    as unknown keys of the definition are."""
    if isinstance(kind, dict):
        if not isinstance(value, dict):
            raise FieldError(field, 'object', keys=kind)
        return {key: clean(f"{field}.{key}", sub, value[key]) for key, sub in kind.items() if value.get(key) is not None}
    name, limit = kind
    if name not in KIND_WORDS:
        raise ValueError(f"DEFINITION_FIELDS: {field} has a kind the service does not know: {name}")
    size = event_rules.limit(limit) if isinstance(limit, str) else None  # a map_list's limit is its entries' kinds
    bad = FieldError(field, name, size, event_rules.limit('list') if name in LIST_KINDS else None)
    if name in ('text', 'name', 'link', 'markdown'):
        if not isinstance(value, str) or len(value) > size:
            raise bad
        if name == 'name' and not value.strip():
            raise bad
        if name == 'link' and not SITE_PATH_OR_HTTPS.fullmatch(value):
            raise bad
        return value
    if name == 'days':
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= size:
            raise bad
        return value
    if name == 'text_list':
        if not isinstance(value, list) or len(value) > event_rules.limit('list'):
            raise bad
        if not all(isinstance(item, str) and len(item) <= size for item in value):
            raise bad
        return list(value)
    if name == 'text_map':
        if not isinstance(value, dict) or len(value) > event_rules.limit('list'):
            raise bad
        if not all(isinstance(k, str) and k.strip() and len(k) <= size and isinstance(v, str) and len(v) <= size for k, v in value.items()):
            raise bad
        return dict(value)
    if name == 'map_list':
        if not isinstance(value, list) or len(value) > event_rules.limit('list') or not all(isinstance(item, dict) for item in value):
            raise bad
        return [clean(field, limit, item) for item in value]
    raise ValueError(f"DEFINITION_FIELDS: {field} has a kind that is not for a definition field: {name}")


def video_link(link, longest):
    """A gallery video link as it is saved (urlunsplit's form of it), or None when it is not an https link to one of
    video_hosts. The host urlsplit reads must be the host a browser goes to, so the link has only URL characters,
    no user name or password before an @ (https://www.youtube.com@evil.test goes to evil.test), and no port."""
    if not isinstance(link, str) or len(link) > longest or not URL.fullmatch(link):
        return None
    try:
        parts = urlsplit(link)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or port is not None or parts.username or parts.password or "@" in parts.netloc:
        return None
    return urlunsplit(parts) if event_rules.is_video_host(parts.hostname) else None


# What a file of each extension starts with, so a file's contents must match its name. svg and html have no entry,
# so they are never accepted, even if the data file lists them.
def _zip_holding(member):
    def holds(data):
        if not data.startswith(b"PK\x03\x04"):
            return False
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                return member in archive.namelist()
        except (zipfile.BadZipFile, ValueError, OSError):
            return False
    return holds


SIGNATURES = {
    'pdf': lambda data: data.startswith(b"%PDF-"),
    'png': lambda data: data.startswith(b"\x89PNG\r\n\x1a\n"),
    'jpg': lambda data: data.startswith(b"\xff\xd8\xff"),
    'jpeg': lambda data: data.startswith(b"\xff\xd8\xff"),
    'gif': lambda data: data[:6] in (b"GIF87a", b"GIF89a"),
    'webp': lambda data: data[:4] == b"RIFF" and data[8:12] == b"WEBP",
    'docx': _zip_holding("word/document.xml"),
    'pptx': _zip_holding("ppt/presentation.xml"),
}
