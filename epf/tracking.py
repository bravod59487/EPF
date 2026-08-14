"""
Which photos have already been shown.

tracking.txt holds the album name on the first line and one asset id per line
after it, so changing albums resets the history on its own.
"""
import os

from . import config, eventlog

tracking_file = os.path.join(config.photodir, 'tracking.txt')

if not os.path.exists(tracking_file):
    open(tracking_file, 'w').close()

def _album():
    return config.immich()['album']

def shown_ids():
    """ The asset ids already shown from the current album """
    album = _album()
    try:
        if not os.path.exists(tracking_file):
            open(tracking_file, 'w').close()

        os.chmod(tracking_file, 0o666)

        with open(tracking_file, 'r+') as handle:
            lines = handle.readlines()

            # Empty, or a different album: start over
            if not lines or lines[0].strip() != album:
                handle.seek(0)
                handle.truncate()
                handle.write(f"{album}\n")
                return set()

            return set(line.strip() for line in lines[1:] if line.strip())
    except Exception as error:
        print(f"Error reading tracking file: {error}")
        return set()

def mark_shown(asset_id):
    """ Record that an asset has been sent to the frame """
    album = _album()
    try:
        if not os.path.exists(tracking_file):
            open(tracking_file, 'w').close()

        os.chmod(tracking_file, 0o666)

        with open(tracking_file, 'r+') as handle:
            lines = handle.readlines()

            if not lines or lines[0].strip() != album:
                handle.seek(0)
                handle.truncate()
                handle.write(f"{album}\n")
            else:
                handle.seek(0, 2)

            handle.write(f"{asset_id}\n")
    except PermissionError:
        print(f"Permission denied when writing to {tracking_file}")
    except IOError as error:
        print(f"IO Error when writing to tracking file: {error}")
    except Exception as error:
        print(f"Unexpected error writing to tracking file: {error}")

def reset(reason=None):
    """ Forget the history, so the album starts again """
    eventlog.record('tracking_reset', reason=reason)
    try:
        open(tracking_file, 'w').close()
    except Exception as error:
        print(f"Error resetting tracking file: {error}")
