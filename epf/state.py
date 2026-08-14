"""
Runtime state shared between requests.

All of it is deliberately in memory only: the next wake-up replaces it, so there
is nothing worth writing to disk. Every value lives in a dict that is updated in
place, so importing modules see changes without re-importing.
"""

# The device's last reported battery, from the batteryCap header on /download.
# Treated as stale after an hour, since the frame is asleep in between.
battery = {
    'voltage': 0,
    'updated': 0,       # time.time() of the reading
}

# What the frame is showing right now
last_photo = {
    'asset_id': None,
    'shown_at': None,    # when the server handed it over
    'taken_at': None,    # the photo's own EXIF date, for display
}

# What it will be handed on its next wake-up, chosen in advance so the settings
# page can show what is coming and offer to swap it for another.
next_photo = {
    'asset': None,
    'album': None,
    'album_id': None,
    'chosen_at': None,
}

# When the last low-battery warning went out. In memory, so a restart lets one
# more through rather than needing another file on disk.
notify = {
    'last_sent': 0,
}

def clear_next_photo():
    """ Called once the asset has been handed over: it is no longer "next" """
    next_photo.update({'asset': None, 'album': None, 'album_id': None, 'chosen_at': None})
