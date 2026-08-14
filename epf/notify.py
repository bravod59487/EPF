"""
Push notifications for a low battery.

Credentials live in a file next to config.yaml (see credentials.py), not in the
environment, so a token can be changed without recreating the container. They are
never rendered into the settings page.

A warning goes to every channel that has been bound, where bound means a test
message actually got through.

LINE needs the Messaging API: LINE Notify, which took a single token, was
discontinued in 2025.
"""
import threading
import time

import requests

from . import config, credentials, eventlog, state

CHANNELS = ('telegram', 'line')

class NotifyError(Exception):
    """ Carries a short code so the caller can log or report it """

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail

def _telegram(text, values):
    token = values.get('bot_token')
    chat_id = values.get('chat_id')
    if not token or not chat_id:
        raise NotifyError('not_configured', 'A bot token and a chat id are both needed')

    response = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                             json={'chat_id': chat_id, 'text': text}, timeout=10)
    if response.status_code != 200:
        raise NotifyError('rejected', f"Telegram returned {response.status_code}")

def _line(text, values):
    token = values.get('channel_token')
    user_id = values.get('user_id')
    if not token or not user_id:
        raise NotifyError('not_configured', 'A channel token and a user id are both needed')

    response = requests.post("https://api.line.me/v2/bot/message/push",
                             headers={'Authorization': f"Bearer {token}"},
                             json={'to': user_id,
                                   'messages': [{'type': 'text', 'text': text}]},
                             timeout=10)
    if response.status_code != 200:
        raise NotifyError('rejected', f"LINE returned {response.status_code}")

SENDERS = {'telegram': _telegram, 'line': _line}

def send(text, channel, values=None):
    """
    Push one message over one channel. Raises NotifyError.

    `values` lets a binding attempt use credentials that have not been saved yet,
    which is how a test can run before anything is stored.
    """
    sender = SENDERS.get(channel)
    if not sender:
        raise NotifyError('unknown_channel', channel)

    try:
        sender(text, values if values is not None else credentials.get(channel))
    except NotifyError:
        raise
    except requests.RequestException as error:
        raise NotifyError('unreachable', str(error))

def bound_channels():
    """ The channels whose credentials have been proved to work """
    return [channel for channel, info in credentials.summary().items() if info['bound']]

def send_channels():
    """
    Where a warning would actually go: linked, and ticked on the settings page.

    Kept separate from bound_channels() so that linking two services does not
    force both to be messaged.
    """
    settings = config.notify()
    return [channel for channel in bound_channels()
            if settings.get('use_' + channel, True)]

def send_in_background(text, event='notified', **fields):
    """
    Send to every bound channel without holding up the caller.

    This is called from /download, and the frame gives up after 50 seconds, so a
    slow or unreachable notification service must not sit in that request.
    """
    def run():
        for channel in send_channels():
            try:
                send(text, channel)
                eventlog.record(event, channel=channel, **fields)
            except NotifyError as error:
                eventlog.record('error', where='notify', message=error.code,
                                detail=error.detail, channel=channel)

    threading.Thread(target=run, daemon=True).start()

def check_battery(percentage, voltage):
    """
    Warn if the battery has fallen to the configured level.

    Rate-limited so a frame that wakes every half hour does not send a message
    every time. The window is kept in memory, so restarting the server allows one
    more message through - which beats writing another file for it.
    """
    settings = config.notify()
    if not settings['enabled'] or percentage is None:
        return False
    if not send_channels():
        return False

    threshold = float(settings['battery_threshold'])
    if percentage > threshold:
        return False

    interval = float(settings['min_interval_hours']) * 3600
    since = time.time() - state.notify['last_sent']
    if state.notify['last_sent'] and since < interval:
        return False

    state.notify['last_sent'] = time.time()
    send_in_background(
        f"E-paper frame battery low: {percentage:.1f}% ({int(voltage)} mV)",
        event='notified', reason='battery_low',
        battery_pct=percentage, battery_mv=int(voltage))
    return True
