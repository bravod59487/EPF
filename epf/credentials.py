"""
Notification credentials, kept in a file rather than the environment.

Environment variables would mean recreating the container to change a token, so
these live beside config.yaml on the mounted volume instead. They are kept out of
config.yaml so that file stays safe to copy around, and the settings page never
renders the values - it is only ever told whether a channel is bound.

A channel counts as bound only once a test message has actually been delivered,
so "bound" means "known to work", not "something was typed in".
"""
import json
import os
import threading
from datetime import datetime

from . import config

CREDENTIALS_PATH = os.path.join(os.path.dirname(config.CONFIG_PATH), 'credentials.json')

# The fields each channel needs. Deliberately not read from the environment:
# changing an env var means recreating the container, and a value that existed but
# had never been tested would show as "not linked" while still being present.
FIELDS = {
    'telegram': ('bot_token', 'chat_id'),
    'line': ('channel_token', 'user_id'),
}

_lock = threading.Lock()

def _read():
    try:
        with open(CREDENTIALS_PATH, 'r', encoding='utf-8') as handle:
            return json.load(handle) or {}
    except FileNotFoundError:
        return {}
    except Exception as error:
        print(f"Could not read credentials: {error}")
        return {}

def _write(everything):
    temporary = CREDENTIALS_PATH + '.tmp'
    with open(temporary, 'w', encoding='utf-8') as handle:
        json.dump(everything, handle, indent=2, sort_keys=True)
    os.replace(temporary, CREDENTIALS_PATH)
    try:
        os.chmod(CREDENTIALS_PATH, 0o600)
    except OSError:
        pass  # best effort; the volume may not support it

def get(channel):
    """ The channel's credentials, or {} when it has none """
    stored = _read().get(channel) or {}
    return {field: stored[field] for field in FIELDS.get(channel, ()) if stored.get(field)}

def is_complete(channel):
    """ Whether every field the channel needs has a value """
    have = get(channel)
    return bool(FIELDS.get(channel)) and all(field in have for field in FIELDS[channel])

def verified_at(channel):
    """ When a test message last went through, or None if never """
    return (_read().get(channel) or {}).get('verified_at')

def save_verified(channel, values):
    """ Store credentials that have just been proved to work """
    with _lock:
        everything = _read()
        entry = {field: values[field] for field in FIELDS[channel] if values.get(field)}
        entry['verified_at'] = datetime.now().isoformat(timespec='seconds')
        everything[channel] = entry
        _write(everything)

def forget(channel):
    """ Drop a channel's credentials """
    with _lock:
        everything = _read()
        if channel in everything:
            del everything[channel]
            _write(everything)

def summary():
    """
    What the settings page is allowed to know: whether each channel is usable,
    and when it was last proved. Never the values themselves.
    """
    return {channel: {'bound': is_complete(channel) and bool(verified_at(channel)),
                      'has_values': is_complete(channel),
                      'verified_at': verified_at(channel)}
            for channel in FIELDS}
