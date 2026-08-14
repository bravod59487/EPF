"""
Push notifications for a low battery.

Credentials come from the environment, not config.yaml, for the same reason
IMMICH_API_KEY does: the settings page has no authentication, so anything stored
in the config would be readable by anyone who can reach the page.

  Telegram   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  LINE       LINE_CHANNEL_TOKEN, LINE_USER_ID

LINE needs the Messaging API: LINE Notify, which took a single token, was
discontinued in 2025.
"""
import os
import threading
import time

import requests

from . import config, eventlog, state

CHANNELS = ('telegram', 'line')

class NotifyError(Exception):
    """ Carries a short code so the caller can log or report it """

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail

def _telegram(text):
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    chat_id = os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        raise NotifyError('not_configured',
                          'TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must both be set')

    response = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                             json={'chat_id': chat_id, 'text': text}, timeout=10)
    if response.status_code != 200:
        raise NotifyError('rejected', f"Telegram returned {response.status_code}")

def _line(text):
    token = os.getenv('LINE_CHANNEL_TOKEN')
    user_id = os.getenv('LINE_USER_ID')
    if not token or not user_id:
        raise NotifyError('not_configured',
                          'LINE_CHANNEL_TOKEN and LINE_USER_ID must both be set')

    response = requests.post("https://api.line.me/v2/bot/message/push",
                             headers={'Authorization': f"Bearer {token}"},
                             json={'to': user_id,
                                   'messages': [{'type': 'text', 'text': text}]},
                             timeout=10)
    if response.status_code != 200:
        raise NotifyError('rejected', f"LINE returned {response.status_code}")

SENDERS = {'telegram': _telegram, 'line': _line}

def send(text):
    """ Push one message over the configured channel. Raises NotifyError. """
    channel = config.notify()['channel']
    sender = SENDERS.get(channel)
    if not sender:
        raise NotifyError('unknown_channel', channel)

    try:
        sender(text)
    except NotifyError:
        raise
    except requests.RequestException as error:
        raise NotifyError('unreachable', str(error))

def send_in_background(text, event='notified', **fields):
    """
    Send without holding up the caller.

    This is called from /download, and the frame gives up after 50 seconds, so a
    slow or unreachable notification service must not sit in that request.
    """
    def run():
        try:
            send(text)
            eventlog.record(event, channel=config.notify()['channel'], **fields)
        except NotifyError as error:
            eventlog.record('error', where='notify', message=error.code,
                            detail=error.detail)

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
