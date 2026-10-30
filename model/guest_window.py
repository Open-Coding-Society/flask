"""The Guest Window: the time during which guest accounts can be created, or come back.

A Teacher or Admin opens it for a number of minutes (the most is set in dataconfigs/account_types.json) and
can close it sooner. It is kept in a small file in the instance folder, so it is shared by every
worker, survives a restart, and needs no database table. A missing, unreadable or past
closing time all mean the window is closed.
"""
import json
import os
import tempfile
from datetime import datetime, timedelta

from flask import current_app


def _path():
    return current_app.config.get("GUEST_WINDOW_FILE") or os.path.join(current_app.instance_path, "guest_window.json")


def closes_at():
    """When the window closes (UTC), or None when it is closed."""
    try:
        with open(_path()) as fh:
            closes = datetime.fromisoformat(json.load(fh)["closes_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return closes if closes > datetime.utcnow() else None


def is_open():
    return closes_at() is not None


def _write(data):
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(handle, "w") as fh:
        json.dump(data, fh)
    os.replace(temp, path)  # a worker reading at the same time sees the old file or the new one


def open_for(minutes):
    """Open the window for this many minutes from now, replacing any time it already had."""
    closes = datetime.utcnow() + timedelta(minutes=minutes)
    _write({"closes_at": closes.isoformat()})
    return closes


def close():
    _write({"closes_at": None})
