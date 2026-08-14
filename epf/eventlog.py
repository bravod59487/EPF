"""
Rolling record of what the frame asked for and what changed on the server.

Lives beside tracking.txt so the mount that keeps the settings keeps the history.
Appending never raises: losing a log line must not fail a request, least of all
the device's download.
"""
import json
import os
import threading
from datetime import datetime

from flask import request

from . import config

log_file = os.path.join(config.photodir, 'events.jsonl')
_lock = threading.Lock()

MAX_ENTRIES = 2000
TRIM_BYTES = 1000000

def _trim():
    """ Drop the oldest entries. Caller must hold the lock. """
    with open(log_file, 'r', encoding='utf-8') as handle:
        lines = handle.readlines()
    temporary = log_file + '.tmp'
    with open(temporary, 'w', encoding='utf-8') as handle:
        handle.writelines(lines[-MAX_ENTRIES:])
    os.replace(temporary, log_file)

def record(event, **fields):
    """ Append one event. Fields that are None are left out rather than stored. """
    entry = {'ts': datetime.now().isoformat(timespec='seconds'), 'event': event}
    entry.update({key: value for key, value in fields.items() if value is not None})
    try:
        line = json.dumps(entry, ensure_ascii=False)
        # Flask's server is threaded, so the device and a browser can write at the
        # same moment; tracking.txt has no such guard, this one does.
        with _lock:
            with open(log_file, 'a', encoding='utf-8') as handle:
                handle.write(line + '\n')
            if os.path.getsize(log_file) > TRIM_BYTES:
                _trim()
    except Exception as error:
        print(f"Could not write event log: {error}")

def recent(limit=50):
    """ The most recent entries, newest first """
    entries = []
    try:
        with _lock:
            with open(log_file, 'r', encoding='utf-8') as handle:
                lines = handle.readlines()[-limit:]
        for line in lines:
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue  # a torn final line is not worth failing over
    except FileNotFoundError:
        pass

    entries.reverse()
    return entries

def clear():
    """ Empty the log. Raises so the caller can report a failure. """
    with _lock:
        open(log_file, 'w').close()

def client_ip():
    """ The caller's address, honouring a proxy header when one is present """
    forwarded = request.headers.get('X-Forwarded-For', '')
    return forwarded.split(',')[0].strip() if forwarded else request.remote_addr
