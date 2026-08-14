#-*- coding:utf8 -*-
from flask import Flask, jsonify, send_file, render_template, request, redirect, url_for
import yaml
import requests
import os
import io
import random
import rawpy
import numpy as np
from PIL import Image,ImageDraw,ImageFont,ImageEnhance,ImageOps
from pillow_heif import register_heif_opener
from datetime import datetime, timedelta
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import threading
import json
from cpy import  convert_image, load_scaled
import ntplib
import time

app = Flask(__name__)


DEFAULT_CONFIG = {
    'immich': {
        'url': 'http://192.168.1.10',   # Immich server URL ("localhost" is forbidden)
        'album': 'default_album',       # Album name
        'rotation': 270,                # 0/90/180/270
        'enhanced': 1.3,                # From 0.0 .. 1.0
        'contrast': 0.9,                # From 0.0 .. 1.0
        'strength': 0.8,                # From 0.0 .. 1.0
        'display_mode': 'fill',          # Add display mode setting (fit/fill)
        'image_order': 'random',        # Add image display order setting (random/newest)
        'sleep_start_hour': 23,         # Sleep start time 23:00 (11:00 PM)
        'sleep_start_minute': 0,        # Sleep start time 23:00 (11:00 PM)
        'sleep_end_hour': 6,            # Sleep end time 6:00 (6:00 AM)
        'sleep_end_minute': 0,          # Sleep end time 6:00 (6:00 AM)
        'wakeup_interval': 60,          # Default 60 minutes (1 hour)
    }
}

current_config = DEFAULT_CONFIG.copy()

# Initialize configuration
url = DEFAULT_CONFIG['immich']['url']
albumname = DEFAULT_CONFIG['immich']['album']
rotationAngle = DEFAULT_CONFIG['immich']['rotation']
img_enhanced = DEFAULT_CONFIG['immich']['enhanced']
img_contrast = DEFAULT_CONFIG['immich']['contrast']
strength = DEFAULT_CONFIG['immich']['strength']
display_mode = DEFAULT_CONFIG['immich']['display_mode']
image_order = DEFAULT_CONFIG['immich']['image_order']
sleep_start_hour = DEFAULT_CONFIG['immich']['sleep_start_hour']
sleep_start_minute = DEFAULT_CONFIG['immich']['sleep_start_minute']
sleep_end_hour = DEFAULT_CONFIG['immich']['sleep_end_hour']
sleep_end_minute = DEFAULT_CONFIG['immich']['sleep_end_minute']

# Retrieve environment variables with error handling
apikey = os.getenv('IMMICH_API_KEY')
photodir = os.getenv('IMMICH_PHOTO_DEST', '/photos')
tracking_file = os.path.join(photodir, 'tracking.txt')

# Ensure directory exists
os.makedirs(photodir, exist_ok=True)

# Ensure tracking.txt exists
if not os.path.exists(tracking_file):
    open(tracking_file, 'w').close()

headers = {
    'Accept': 'application/json',
    'x-api-key': apikey
}

# Allowed file extensions
ALLOWED_EXTENSIONS = {'.jpeg', '.raw', '.jpg', '.bmp', '.dng', '.heic', '.arw', '.cr2', '.dng', '.nef', '.raw'}

# Set up the directory for the downloaded images
os.makedirs(photodir, exist_ok=True)
register_heif_opener()

# Palltte only for WaveShare 7.5inch Spectra-E6 e-Paper
palette = [
    (0, 0, 0),
    (255, 255, 255),
    (255, 243, 56),
    (191, 0, 0),
    (100, 64, 255),
    (67, 138, 28)
]

last_battery_voltage = 0
last_battery_update = 0

# What the frame is currently showing, recorded on each /download. Kept in memory
# only, like the battery reading: the next wake-up refreshes it, so there is no
# point writing it to disk.
last_photo = {
    'asset_id': None,
    'shown_at': None,
}

# The asset the frame will be handed on its next wake-up. Chosen in advance so the
# settings page can show what is coming and offer to swap it for another.
next_photo = {
    'asset': None,
    'album': None,
    'album_id': None,
    'chosen_at': None,
}

# Rolling record of what the frame asked for and what changed on the server.
# Lives beside tracking.txt so the same mount that keeps the settings keeps the
# history; trimmed by size so it can never fill the volume.
log_file = os.path.join(photodir, 'events.jsonl')
log_lock = threading.Lock()
LOG_MAX_ENTRIES = 2000
LOG_TRIM_BYTES = 1000000

def _trim_log():
    """ Drop the oldest entries. Caller must hold log_lock. """
    with open(log_file, 'r', encoding='utf-8') as handle:
        lines = handle.readlines()
    temporary = log_file + '.tmp'
    with open(temporary, 'w', encoding='utf-8') as handle:
        handle.writelines(lines[-LOG_MAX_ENTRIES:])
    os.replace(temporary, log_file)

def log_event(event, **fields):
    """
    Append one event. Never raises: losing a log line must not fail a request,
    least of all the device's download.
    """
    entry = {'ts': datetime.now().isoformat(timespec='seconds'), 'event': event}
    entry.update({key: value for key, value in fields.items() if value is not None})
    try:
        line = json.dumps(entry, ensure_ascii=False)
        # Flask's server is threaded, so the device and a browser can write at
        # the same moment; tracking.txt has no such guard, this one does.
        with log_lock:
            with open(log_file, 'a', encoding='utf-8') as handle:
                handle.write(line + '\n')
            if os.path.getsize(log_file) > LOG_TRIM_BYTES:
                _trim_log()
    except Exception as error:
        print(f"Could not write event log: {error}")

def client_ip():
    """ The caller's address, honouring a proxy header when one is present """
    forwarded = request.headers.get('X-Forwarded-For', '')
    return forwarded.split(',')[0].strip() if forwarded else request.remote_addr

class ImmichError(Exception):
    """ Carries the message and HTTP status the caller should report """

    def __init__(self, message, status=500):
        super().__init__(message)
        self.message = message
        self.status = status

def resolve_album_id():
    """ Look up the configured album, raising ImmichError if it cannot be used """
    response = requests.get(f"{url}/api/albums", headers=headers)
    if response.status_code != 200:
        raise ImmichError("Failed to fetch albums")

    albumid = next((item['id'] for item in response.json()
                    if item['albumName'] == albumname), None)
    if not albumid:
        raise ImmichError("Album not found", 404)
    return albumid

def list_album_assets(albumid):
    """
    Every asset in the album.

    Immich v3 breaking change: GET /api/albums/{id} no longer returns the
    'assets' property, so this goes through the paginated search endpoint.
    """
    assets = []
    page = 1
    while True:
        search_body = {
            "albumIds": [albumid],
            "size": 1000,
            "page": page,
            "withExif": True,
        }
        response = requests.post(f"{url}/api/search/metadata", headers=headers, json=search_body)
        if response.status_code != 200:
            raise ImmichError("Failed to fetch album details")

        search_result = response.json().get('assets', {})
        assets.extend(search_result.get('items', []))

        next_page = search_result.get('nextPage')
        if not next_page:
            break
        page = int(next_page)

    if not assets:
        raise ImmichError("No images found in album", 404)
    return assets

def _taken_at(asset):
    return asset.get('exifInfo', {}).get('dateTimeOriginal', '1970-01-01T00:00:00')

def select_asset(assets):
    """
    Choose which asset to show next, honouring image_order and the history in
    tracking.txt. The history is reset when the album runs out, or when a newer
    photo turns up while ordering by date.
    """
    order = current_config['immich']['image_order']
    shown = load_downloaded_images()

    if order == 'newest':
        latest_id = max(assets, key=_taken_at)['id']
        if not shown or latest_id not in shown:
            reset_tracking_file(reason='newer_photo')
            remaining = sorted(assets, key=_taken_at, reverse=True)
        else:
            remaining = sorted([a for a in assets if a['id'] not in shown],
                               key=_taken_at, reverse=True)
        if not remaining:
            # Everything has been shown and nothing is newer: begin again rather
            # than indexing an empty list, which used to 500 the device
            reset_tracking_file(reason='album_exhausted')
            remaining = sorted(assets, key=_taken_at, reverse=True)
        return remaining[0]

    remaining = [a for a in assets if a['id'] not in shown]
    if not remaining:
        reset_tracking_file(reason='album_exhausted')
        remaining = assets
    return random.choice(remaining)

def refresh_next_photo():
    """ Choose and remember the photo the frame will get next. Raises ImmichError. """
    albumid = resolve_album_id()
    asset = select_asset(list_album_assets(albumid))
    next_photo.update({'asset': asset, 'album': albumname,
                       'album_id': albumid, 'chosen_at': datetime.now()})
    return asset

def photo_link(asset_id):
    """ The asset in the configured server's web UI, so the link works on the LAN """
    return f"{url.rstrip('/')}/photos/{asset_id}"

def load_downloaded_images():
    """ Load downloaded image ID from tracking.txt """
    global albumname
    try:
        # Ensure file exists and is readable/writable
        if not os.path.exists(tracking_file):
            open(tracking_file, 'w').close()
        
        # Ensure file has correct permissions
        os.chmod(tracking_file, 0o666)
        
        with open(tracking_file, 'r+') as f:
            lines = f.readlines()
            
            # If file is empty or first line is not current album name, return empty set
            if not lines or lines[0].strip() != albumname:
                # Rewrite album name
                f.seek(0)
                f.truncate()
                f.write(f"{albumname}\n")
                return set()
            
            # Return all lines except the first as downloaded image IDs
            return set(line.strip() for line in lines[1:] if line.strip())
    except Exception as e:
        print(f"Error reading tracking file: {e}")
        return set()

def save_downloaded_image(asset_id):
    """ Save downloaded image ID from tracking.txt """
    global albumname
    try:
        # Check the file exists and is writable
        if not os.path.exists(tracking_file):
            open(tracking_file, 'w').close()
        
        # Check the permission of the file
        os.chmod(tracking_file, 0o666)
        
        with open(tracking_file, 'r+') as f:
            # Read all lines
            lines = f.readlines()
            
            # If file is empty or first line is not current album name, reset file
            if not lines or lines[0].strip() != albumname:
                f.seek(0)
                f.truncate()
                f.write(f"{albumname}\n")
            else:
                f.seek(0, 2)  # Move to the end of the file
            
            # Add new image ID
            f.write(f"{asset_id}\n")
    except PermissionError:
        print(f"Permission denied when writing to {tracking_file}")
    except IOError as e:
        print(f"IO Error when writing to tracking file: {e}")
    except Exception as e:
        print(f"Unexpected error writing to tracking file: {e}")

def reset_tracking_file(reason=None):
    """Reset tracking.txt file"""
    log_event('tracking_reset', reason=reason)
    try:
        open(tracking_file, 'w').close()
    except Exception as e:
        print(f"Error resetting tracking file: {e}")


def depalette_image(pixels, palette):
    palette_array = np.array(palette)
    diffs = np.sqrt(np.sum((pixels[:, :, None, :] - palette_array[None, None, :, :]) ** 2, axis=3))
    indices = np.argmin(diffs, axis=2)
    indices[indices > 3] += 1  # Simulate the code from the C
    return indices

def scale_img_in_memory(image, target_width=800, target_height=480, bg_color=(255, 255, 255)):
    """
    Process image in memory, return BytesIO object

    :param image: PIL Image object
    :param target_width: width of epaper
    :param target_height: height of epaper
    :param bg_color: background color
    :param rotation: rotation angle (0, 90, 180, 270)
    :return: BytesIO object
    """

    # Update the angle
    rotation = rotationAngle

    # Get data from EXIF
    try:
        exif = image._getexif()
        if exif:
            # EXIF time tag is 36867
            date_time = exif.get(36867)
            if not date_time:
                # Alternative time tag is 306
                date_time = exif.get(306)
        else:
            date_time = None
    except:
        date_time = None

    # Read correct photo orientation from EXIF
    image = ImageOps.exif_transpose(image)
    # output_img = Image.new('RGB', (target_width, target_height), bg_color)

    # # calculate position
    # paste_x = (target_width - new_width) // 2
    # paste_y = (target_height - new_height) // 2
    img = load_scaled(image, rotation, display_mode)
    # Enhance color and contrast
    enhanced_img = ImageEnhance.Color(img).enhance(img_enhanced)
    enhanced_img = ImageEnhance.Contrast(enhanced_img).enhance(img_contrast)
    
    # Palette definition (matching previous quantization logic)
    palette = [
        0, 0, 0,         # Black
        255, 255, 255,   # White
        255, 255, 0,    # Yellow
        255, 0, 0,       # Deep Red
        0, 0, 255,    # Blue
        0, 255, 0      # Green
    ]
    
    # Prepare palette image (similar to previous code)
    e = len(palette)
    assert e > 0, "Palette unexpectedly short"
    assert e <= 768, "Palette unexpectedly long"
    assert e % 3 == 0, "Palette not multiple of 3, so not RGB"

    # Create temporary palette image
    pal_image = Image.new("P", (1, 1))
    
    # Zero-pad palette to 768 values
    palette += (768 - e) * [0]
    pal_image.putpalette(palette)
    
    # Quantize image
    # output_img = enhanced_img.convert("RGB").quantize(
    #     palette=pal_image,
    #     dither=Image.Dither.FLOYDSTEINBERG
    # ).convert("RGB")
    
    output_img = convert_image(enhanced_img, dithering_strength=strength)
    output_img = Image.fromarray(output_img, mode="RGB")
    
    # output_img.paste(quantized_img, (paste_x, paste_y))
    
    # Add date if available
    if date_time:
        draw = ImageDraw.Draw(output_img)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
        except:
            font = ImageFont.load_default()
        
        # Format the date
        try:
            try:
                dt = datetime.strptime(date_time, "%Y:%m:%d %H:%M:%S")
                formatted_time = dt.strftime("%Y/%m/%d")
            except ValueError:
                dt = datetime.strptime(date_time, "%Y.%m.%d")
                formatted_time = dt.strftime("%Y/%m/%d")
        except:
            formatted_time = date_time

        def draw_text_with_background(draw, text, font, text_color=(255, 255, 255), bg_color=(0, 0, 0)):
            # Calculate rotated width/height
            if rotation in [90, 270]:
                img_width, img_height = target_height, target_width  # width and height swapped
            else:
                img_width, img_height = target_width, target_height
        
            # Set text position
            if rotation == 0:  # no rotation
                position = (img_width - 200, img_height - 40)
            elif rotation == 90:  # 90 degrees clockwise (actually counterclockwise)
                position = (img_height - 30, 30)
            elif rotation == 180:  # 180 degrees
                position = (img_width -200 , img_height - 40)
            elif rotation == 270:  # 270 degrees clockwise (actually counterclockwise)
                position = (30, img_width - 30)
        
            # Get text bounding box
            text_bbox = draw.textbbox((0, 0), text, font=font)  # use (0, 0) to get text size
            text_width = text_bbox[2] - text_bbox[0]
            text_height = text_bbox[3] - text_bbox[1]
            padding = 5
        
            # Set text position and background rectangle bounds
            if rotation == 0:  # no rotation, bottom right
                position = (img_width - text_width - 40, img_height - text_height - 40)
                rect_coords = [
                    position[0] - padding,  # Top left X
                    position[1] - padding,  # Top left Y
                    position[0] + text_width + padding,  # Bottom right X
                    position[1] + text_height + padding  # Bottom right Y
                ]
            elif rotation == 90:  # 90 degrees, top right
                position = (img_height - text_height - 40, 40)
                rect_coords = [
                    position[0] - padding,  # Top left X
                    position[1] - padding,  # Top left Y
                    position[0] + text_height + padding,  # Bottom right X
                    position[1] + text_width + padding   # Bottom right Y
                ]
            elif rotation == 180:  # 180 degrees, top left
                position = (40, 40)
                rect_coords = [
                    position[0] - padding,  # Top left X
                    position[1] - padding,  # Top left Y
                    position[0] + text_width + padding,  # Bottom right X
                    position[1] + text_height + padding  # Bottom right Y
                ]
            elif rotation == 270:  # 270 degrees, bottom left
                position = (40, img_width - text_width - 40)
                rect_coords = [
                    position[0] - padding,  # Top left X
                    position[1] - padding,  # Top left Y
                    position[0] + text_height + padding,  # Bottom right X
                    position[1] + text_width + padding   # Bottom right Y
                ]
            
            # Draw rectangular background
            draw.rectangle(rect_coords, fill=bg_color)
        
            # Create text based on the rotation of image
            if rotation == 0:
                draw.text(position, text, fill=text_color, font=font)
            else:
                # Create a new image to draw rotated text
                rotated_text = Image.new("RGB", (text_width, text_height), (255, 255, 255))  # white background
                rotated_draw = ImageDraw.Draw(rotated_text)
                rotated_draw.text((0, 0), text, fill=text_color, font=font)
                
                # Rotate text image
                rotated_text = rotated_text.rotate(rotation, expand=True, resample=Image.BICUBIC)
                
                # Calculate where rotated text should be pasted
                if rotation == 90:
                    # 90 degree rotation, display in top right
                    output_img.paste(rotated_text, (position[1], position[0]))
                elif rotation == 180:
                    # 180 degree rotation, display in top left
                    output_img.paste(rotated_text, (position[0], position[1]))
                elif rotation == 270:
                    # 270 degree rotation, display in bottom left
                    output_img.paste(rotated_text, (position[1], position[0]))
                
        # Drawing the text on forground (WIP)
        # draw_text_with_background(draw, formatted_time, font)
    
    # Save image into ram
    img_io = io.BytesIO()
    output_img.save(img_io, 'BMP')
    img_io.seek(0)
    return img_io

def convert_to_c_code_in_memory(image_data):
    """ Convert image to C code in memory """
    # Convert image data to numpy array
    pixels = np.array(image_data)
    
    # Process palette
    indices = depalette_image(pixels, palette)
    
    # Compress pixels
    height, width = indices.shape
    bytes_array = [
        (indices[y, x] << 4) | indices[y, x + 1] if x + 1 < width else (indices[y, x] << 4)
        for y in range(height)
        for x in range(0, width, 2)
    ]
    
    # Generate C code
    output = io.StringIO()

    for i, byte_value in enumerate(bytes_array):
        output.write(f"{byte_value:02X},")
        if (i + 1) % 16 == 0:
            output.write("\n")
    
    output.write("};\n")
    
    # Convert output to bytes
    result = output.getvalue().encode('utf-8')
    output_bytes = io.BytesIO(result)
    output_bytes.seek(0)
    
    return output_bytes

def convert_raw_or_dng_to_jpg(input_file_path, output_dir):
    """Convert RAW or DNG files to JPG using rawpy."""
    with rawpy.imread(input_file_path) as raw:
        rgb = raw.postprocess(use_camera_wb=True, use_auto_wb=False)
        base_name = os.path.splitext(os.path.basename(input_file_path))[0]
        jpg_file_path = os.path.join(output_dir, f"{base_name}.jpg")
        Image.fromarray(rgb).save(jpg_file_path, 'JPEG')
        return jpg_file_path

def convert_heic_to_jpg(input_file_path, output_dir):
    """Convert heic files to JPG using rawpy."""
    img = Image.open(input_file_path)
    img = img.convert("RGB")
    base_name = os.path.splitext(os.path.basename(input_file_path))[0]
    jpg_file_path = os.path.join(output_dir, f"{base_name}.jpg")
    img.save(jpg_file_path, "JPEG", quality=95)
    # print(f"Successfully converted {input_file_path} to {output_dir}")
    return jpg_file_path

class ConfigFileHandler(FileSystemEventHandler):
    """ Reload configuration and notify application when config.yaml changes """
    def __init__(self, config_path, config_update_callback):
        self.config_path = config_path
        self.config_update_callback = config_update_callback
        
        # Ensure directory and config file exist
        self.ensure_config_exists()
        
        # Load configuration
        self.config = self.load_config()
    
    def ensure_config_exists(self):
        """ 
        Ensure the config directory and config file exist. 
        Create them if they don't exist.
        """
        # Get the directory path
        config_dir = os.path.dirname(self.config_path)
        
        # Create the directory if it doesn't exist
        if not os.path.exists(config_dir):
            try:
                os.makedirs(config_dir)
                print(f"Created config directory: {config_dir}")
            except Exception as e:
                print(f"Error creating config directory: {e}")
        
        # Create the config file if it doesn't exist
        if not os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'w') as file:
                    yaml.dump(DEFAULT_CONFIG, file)
                print(f"Created default configuration file: {self.config_path}")
            except Exception as e:
                print(f"Error creating config file: {e}")
    
    def on_modified(self, event):
        if event.src_path == self.config_path:
            print("File modification detected, reloading configuration...")
            log_event('config_reloaded', source='file')
            new_config = self.load_config()
            # Use callback function to update configuration
            self.config_update_callback(new_config)
    
    def load_config(self):
        """ Load config """
        try:
            with open(self.config_path, 'r') as file:
                return yaml.safe_load(file)
        except Exception as e:
            print(f"Error reading config file: {e}")
            # Fallback to default configuration if reading fails
            return DEFAULT_CONFIG
    
def update_app_config(new_config):
    """ Update global configuration and Flask application configuration """
    global current_config, url, albumname, rotationAngle, img_enhanced, img_contrast, strength, display_mode, image_order, sleep_start_hour, sleep_end_hour, sleep_start_minute, sleep_end_minute
    
    current_config = new_config
    
    # Update Flask application configuration
    app.config['IMMICH_URL'] = new_config['immich']['url']
    app.config['IMMICH_ALBUM'] = new_config['immich']['album']
    app.config['IMMICH_ROTATION'] = new_config['immich']['rotation']
    app.config['IMMICH_ENHANCED'] = new_config['immich']['enhanced']
    app.config['IMMICH_CONTRAST'] = new_config['immich']['contrast']
    app.config['IMMICH_STRENGH'] = new_config['immich']['strength']
    app.config['IMMICH_DISPLAY_MODE'] = new_config['immich']['display_mode']
    app.config['IMMICH_IMAGE_ORDER'] = new_config['immich']['image_order']
    app.config['IMMICH_SLEEP_START_HOUR'] = new_config['immich']['sleep_start_hour']
    app.config['IMMICH_SLEEP_END_HOUR'] = new_config['immich']['sleep_end_hour']
    app.config['IMMICH_SLEEP_START_MINUTE'] = new_config['immich']['sleep_start_minute']
    app.config['IMMICH_SLEEP_END_MINUTE'] = new_config['immich']['sleep_end_minute']

    
    # Update global variables
    url = new_config['immich']['url']
    albumname = new_config['immich']['album']
    rotationAngle = new_config['immich']['rotation']
    img_enhanced = new_config['immich']['enhanced']
    img_contrast = new_config['immich']['contrast']
    strength = new_config['immich']['strength']
    display_mode = new_config['immich']['display_mode']
    image_order = new_config['immich']['image_order']
    sleep_start_hour = new_config['immich']['sleep_start_hour']
    sleep_end_hour = new_config['immich']['sleep_end_hour']
    sleep_start_minute = new_config['immich']['sleep_start_minute']
    sleep_end_minute = new_config['immich']['sleep_end_minute']
    
    print(f"Configuration updated: URL = {url}, Album = {albumname}, angle = {rotationAngle}, enhance = {img_enhanced}, contrast = {img_contrast}, strength = {strength}, display_mode = {display_mode}, image_order = {image_order}")

def start_config_watcher(config_path):
    """ Start configuration file monitoring """
    config_handler = ConfigFileHandler(config_path, update_app_config)
    
    # Start monitoring file changes
    observer = Observer()
    observer.schedule(config_handler, path=os.path.dirname(config_path), recursive=False)
    observer.start()
    
    return observer

# Add lithium battery voltage table (voltage: battery percentage)
BATTERY_LEVELS = {
    4200: 100,
    4150: 95,
    4110: 90,
    4080: 85,
    4020: 80,
    3980: 75,
    3950: 70,
    3910: 65,
    3870: 60,
    3850: 55,
    3840: 50,
    3820: 45,
    3800: 40,
    3790: 35,
    3770: 30,
    3750: 25,
    3730: 20,
    3710: 15,
    3690: 10,
    3610: 5,
    3400: 0
}

def calculate_battery_percentage(voltage):
    """
    Calculate actual battery percentage based on battery voltage
    Use piecewise linear interpolation for more accurate battery estimation
    """
    if voltage >= 4200:
        return 100
    if voltage <= 3400:
        return 0
    
    # Find the two closest reference points
    voltages = list(BATTERY_LEVELS.keys())
    for i in range(len(voltages)-1):
        if voltages[i] >= voltage >= voltages[i+1]:
            v1, v2 = voltages[i], voltages[i+1]
            p1, p2 = BATTERY_LEVELS[v1], BATTERY_LEVELS[v2]
            # Linear interpolation
            percentage = p2 + (voltage - v2) * (p1 - p2) / (v1 - v2)
            return round(percentage, 1)
    
    return 0

@app.context_processor
def inject_current_year():
    """ Expose the current year to every template, so the footer never goes stale """
    return {'current_year': datetime.now().year}

@app.context_processor
def inject_current_photo():
    """ Expose the photo the frame is showing, or None before the first check-in """
    if not last_photo['asset_id']:
        return {'photo': None}

    shown_at = last_photo['shown_at']
    return {'photo': {
        'asset_id': last_photo['asset_id'],
        'album': albumname,
        # Link to the configured server rather than my.immich.app, so the link
        # opens the web UI on the LAN instead of needing an internet round trip
        'link': photo_link(last_photo['asset_id']),
        'shown_at': shown_at.strftime('%Y-%m-%d %H:%M') if shown_at else '',
    }}

def _no_store(response):
    """ Status and previews change constantly, so they must never be cached """
    response.headers['Cache-Control'] = 'no-store'
    return response

@app.route('/status')
def status():
    """
    Live health for the header: can we reach Immich, and is the frame checking in.

    Returns machine-readable codes rather than sentences, so the page can render
    them in whichever language it is showing.
    """
    # One request settles reachability, credentials and whether the album exists,
    # which is the same call /download makes first anyway.
    immich = {'state': 'error', 'code': 'not_configured'}
    if url and albumname:
        try:
            response = requests.get(f"{url}/api/albums", headers=headers, timeout=6)
            if response.status_code == 200:
                names = [album.get('albumName') for album in response.json()]
                if albumname in names:
                    immich = {'state': 'ok', 'code': 'connected', 'album': albumname}
                else:
                    immich = {'state': 'warn', 'code': 'album_missing', 'album': albumname}
            elif response.status_code in (401, 403):
                immich = {'state': 'error', 'code': 'unauthorized'}
            else:
                immich = {'state': 'error', 'code': 'server_error', 'http': response.status_code}
        except requests.RequestException:
            immich = {'state': 'error', 'code': 'unreachable'}

    # The device is silent between wake-ups, so "connected" can only mean
    # "checked in recently enough", measured against its own wake-up interval.
    last_seen = datetime.fromtimestamp(last_battery_update) if last_battery_update else None
    if last_photo['shown_at'] and (last_seen is None or last_photo['shown_at'] > last_seen):
        last_seen = last_photo['shown_at']

    if last_seen is None:
        frame = {'state': 'unknown', 'code': 'never', 'minutes_ago': None}
    else:
        minutes_ago = max(0, int((datetime.now() - last_seen).total_seconds() // 60))
        interval = int(current_config['immich']['wakeup_interval'])
        frame = {
            'state': 'ok' if minutes_ago <= interval * 2 else 'warn',
            'code': 'seen',
            'minutes_ago': minutes_ago,
            'last_seen': last_seen.strftime('%Y-%m-%d %H:%M'),
        }

    # Same one-hour freshness rule the settings page uses for the reading
    voltage = last_battery_voltage if (time.time() - last_battery_update) < 3600 else 0
    battery = {
        'voltage': voltage,
        'percentage': calculate_battery_percentage(voltage) if voltage > 0 else None,
    }

    return _no_store(jsonify({'immich': immich, 'frame': frame, 'battery': battery}))

@app.route('/log')
def read_log():
    """ Recent events, newest first """
    try:
        limit = min(max(int(request.args.get('limit', 50)), 1), 500)
    except ValueError:
        limit = 50

    entries = []
    try:
        with log_lock:
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
    return _no_store(jsonify({'entries': entries}))

def proxy_thumbnail(asset_id):
    """
    An asset's thumbnail, proxied from Immich.

    It has to be proxied rather than linked: thumbnails need the API key, which
    only the server holds. A thumbnail is also the right thing to ask for because
    originals may be HEIC or RAW, which browsers cannot display.
    """
    try:
        upstream = requests.get(f"{url}/api/assets/{asset_id}/thumbnail",
                                headers=headers, params={'size': 'preview'}, timeout=15)
    except requests.RequestException as e:
        return jsonify({"error": f"Could not reach Immich: {e}"}), 502

    if upstream.status_code != 200:
        return jsonify({"error": f"Immich returned {upstream.status_code}"}), 502

    return _no_store(send_file(io.BytesIO(upstream.content),
                               mimetype=upstream.headers.get('Content-Type', 'image/jpeg')))

@app.route('/preview/original')
def preview_original():
    """ The photo the frame is showing now """
    if not last_photo['asset_id']:
        return jsonify({"error": "No photo has been sent to the frame yet"}), 404
    return proxy_thumbnail(last_photo['asset_id'])

@app.route('/preview/next')
def preview_next():
    """ The photo the frame will be given on its next wake-up """
    if not next_photo['asset']:
        return jsonify({"error": "No photo has been chosen yet"}), 404
    return proxy_thumbnail(next_photo['asset']['id'])

@app.route('/next', methods=['GET', 'POST'])
def upcoming_photo():
    """
    What the frame will show next. GET chooses one only if none is remembered;
    POST always picks a different one, which is what the "swap" button uses.
    """
    try:
        if request.method == 'POST' or not next_photo['asset'] or next_photo['album'] != albumname:
            refresh_next_photo()
    except ImmichError as error:
        log_event('error', where='next', message=error.message, ip=client_ip())
        return _no_store(jsonify({"error": error.message})), error.status

    asset = next_photo['asset']
    if not asset:
        return _no_store(jsonify({"error": "No photo could be chosen"})), 404

    if request.method == 'POST':
        log_event('photo_swapped', ip=client_ip(), asset_id=asset['id'], album=albumname)

    chosen_at = next_photo['chosen_at']
    return _no_store(jsonify({
        'asset_id': asset['id'],
        'album': next_photo['album'],
        'link': photo_link(asset['id']),
        'chosen_at': chosen_at.strftime('%Y-%m-%d %H:%M') if chosen_at else None,
    }))

@app.route('/log/clear', methods=['POST'])
def clear_log():
    """ Empty the event log, then record that it happened """
    try:
        with log_lock:
            open(log_file, 'w').close()
    except Exception as error:
        return _no_store(jsonify({"error": str(error)})), 500

    log_event('log_cleared', ip=client_ip())
    return _no_store(jsonify({'cleared': True}))

@app.route('/setting', methods=['GET', 'POST'])
def settings():
    global current_config, last_battery_voltage, last_battery_update
    config_path = '/config/config.yaml'
    
    # Use stored battery voltage (if updated within the last hour)
    current_time = time.time()
    if current_time - last_battery_update < 3600:  # 1 hour = 3600 seconds
        battery_voltage = last_battery_voltage
    else:
        battery_voltage = 0
    
    # Use new battery calculation method
    battery_percentage = calculate_battery_percentage(battery_voltage) if battery_voltage > 0 else 0
    
    if battery_voltage > 0:
        print(f"Battery: {battery_voltage:.0f}mV ({battery_percentage:.1f}%)")
    else:
        print("No battery information available")

    if request.method == 'POST':
        # Collect form data
        new_config = {
            'immich': {
                'url': request.form.get('url', current_config['immich']['url']),
                'album': request.form.get('album', current_config['immich']['album']),
                'rotation': int(request.form.get('rotation', current_config['immich']['rotation'])),
                'enhanced': float(request.form.get('enhanced', current_config['immich']['enhanced'])),
                'contrast': float(request.form.get('contrast', current_config['immich']['contrast'])),
                'strength': float(request.form.get('strength', current_config['immich']['strength'])),
                'display_mode': request.form.get('display_mode', current_config['immich']['display_mode']),
                'image_order': request.form.get('image_order', current_config['immich']['image_order']),
                'sleep_start_hour': int(request.form.get('sleep_start_hour', current_config['immich']['sleep_start_hour'])),
                'sleep_start_minute': int(request.form.get('sleep_start_minute', current_config['immich']['sleep_start_minute'])),
                'sleep_end_hour': int(request.form.get('sleep_end_hour', current_config['immich']['sleep_end_hour'])),
                'sleep_end_minute': int(request.form.get('sleep_end_minute', current_config['immich']['sleep_end_minute'])),
                'wakeup_interval': int(request.form.get('wakeup_interval', current_config['immich']['wakeup_interval'])),
            }
        }
        
        # Validate rotation values
        if new_config['immich']['rotation'] not in [0, 90, 180, 270]:
            return render_template('settings.html',
                                   config=current_config,
                                   defaults=DEFAULT_CONFIG['immich'],
                                   battery_voltage=battery_voltage,
                                   battery_percentage=battery_percentage,
                                   error="Rotation must be 0, 90, 180, or 270 degrees")
        
        try:
            # Write to config file
            with open(config_path, 'w') as file:
                yaml.safe_dump(new_config, file)
            
            # Record only the fields that actually moved, so the log stays useful
            previous = current_config.get('immich', {})
            changes = {key: [previous.get(key), value]
                       for key, value in new_config['immich'].items()
                       if previous.get(key) != value}

            # Update current configuration
            update_app_config(new_config)

            log_event('settings_saved', ip=client_ip(), changes=changes or None)

            return redirect(url_for('settings'))
        
        except Exception as e:
            return render_template('settings.html',
                                   config=current_config,
                                   defaults=DEFAULT_CONFIG['immich'],
                                   battery_voltage=battery_voltage,
                                   battery_percentage=battery_percentage,
                                   error=f"Error saving configuration: {str(e)}")

    return render_template('settings.html',
                         config=current_config,
                         defaults=DEFAULT_CONFIG['immich'],
                         battery_voltage=battery_voltage,
                         battery_percentage=battery_percentage)

@app.route('/')
def index():
    return redirect(url_for('settings'))

def run_daily_ntp_sync():
    """Daily NTP sync task"""
    while True:
        try:
            # Get current time
            now = datetime.now()
            # Calculate next 4:00 AM
            next_sync = now.replace(hour=4, minute=11, second=0, microsecond=0)
            if now >= next_sync:
                next_sync = next_sync + timedelta(days=1)
            
            # Calculate wait time
            wait_seconds = (next_sync - now).total_seconds()
            time.sleep(wait_seconds)
            
            # Perform NTP sync
            synced_time = sync_time_with_ntp()
            print(f"Daily NTP sync completed at {synced_time.strftime('%Y-%m-%d %H:%M:%S')}")
            
        except Exception as e:
            print(f"Error in daily NTP sync: {e}")
            time.sleep(3600)  # Retry after 1 hour if error occurs

def main():
    config_path = '/config/config.yaml'
    
    # Start configuration file monitoring
    config_observer = start_config_watcher(config_path)
    
    try:
        # Initialize configuration
        initial_config = ConfigFileHandler(config_path, update_app_config).config
        update_app_config(initial_config)
        
        log_event('startup')

        # Start daily NTP sync thread
        ntp_sync_thread = threading.Thread(target=run_daily_ntp_sync, daemon=True)
        ntp_sync_thread.start()
        
        # Run Flask application in a separate thread
        app.run(host='0.0.0.0', port=5000, use_reloader=False)
    except KeyboardInterrupt:
        config_observer.stop()
    config_observer.join()

@app.route('/download', methods=['GET'])
def process_and_download():
    
    global url, albumname, last_battery_voltage, last_battery_update
    
    # Update battery information when received. Kept in its own variable because
    # battery_voltage below is reassigned to the raw header string.
    reported_mv = 0
    try:
        reported_mv = float(request.headers.get('batteryCap', '0'))
        if reported_mv > 0:
            last_battery_voltage = reported_mv
            last_battery_update = time.time()
    except (TypeError, ValueError):
        reported_mv = 0
    
    # Use current global configuration
    current_url = url
    current_albumname = albumname
    
    battery_voltage = request.headers.get('batteryCap', 'Unknown')
    # print(f"Battery: {battery_voltage} mV")
    
    try:
        # Check if url and albumname are valid
        if not current_url or not current_albumname:
            log_event('error', where='download', message="Immich URL or Album not configured", ip=client_ip())
            return jsonify({"error": "Immich URL or Album not configured"}), 500
            
        # Use the photo already chosen for this wake-up when there is one, so the
        # frame gets exactly what the settings page was showing as "next".
        if next_photo['asset'] and next_photo['album'] == current_albumname:
            selected_image = next_photo['asset']
            albumid = next_photo['album_id']
        else:
            albumid = resolve_album_id()
            selected_image = select_asset(list_album_assets(albumid))

        # Handed over, so it is no longer "next"; the settings page asks for a
        # fresh one the next time it loads.
        next_photo.update({'asset': None, 'album': None, 'album_id': None, 'chosen_at': None})

        asset_id = selected_image['id']

        # Record it as shown so it is not repeated
        save_downloaded_image(asset_id)

        # Download image to memory
        response = requests.get(f"{url}/api/assets/{asset_id}/original", headers=headers, stream=True)
        if response.status_code != 200:
            log_event('error', where='download', message="Failed to download image", ip=client_ip())
            return jsonify({"error": "Failed to download image"}), 500

        # Process image in memory
        image_data = io.BytesIO(response.content)
        
        # Process image based on its type
        if selected_image['originalPath'].lower().endswith(('.raw', '.dng', '.arw', '.cr2', '.nef')):
            with rawpy.imread(image_data) as raw:
                rgb = raw.postprocess(use_camera_wb=True, use_auto_wb=False)
                image = Image.fromarray(rgb)
        elif selected_image['originalPath'].lower().endswith('.heic'):
            image = Image.open(image_data).convert("RGB")
        else:
            image = Image.open(image_data)

        # Process image
        processed_image = scale_img_in_memory(image)
        
        # Convert to C code
        processed_image.seek(0)
        c_code = convert_to_c_code_in_memory(Image.open(processed_image))

        # Remember what the frame is showing, so /setting can display it
        last_photo.update({'asset_id': asset_id, 'shown_at': datetime.now()})


        # Build Immich photo URL for NFC tag
        # Immich web UI URL format: {base_url}/albums/{album_id}/photos/{asset_id}
        photo_url = f"https://my.immich.app/albums/{albumid}/photos/{asset_id}"

        response = send_file(
            c_code,
            mimetype='text/plain',
            as_attachment=True,
            download_name=f"image_{asset_id}.c"
        )
        response.headers['X-Photo-Url'] = photo_url
        print(f"Setting X-Photo-Url header: {photo_url}")

        # MAC and signal strength are only here if the firmware sends them; HTTP
        # carries no MAC of its own and the container cannot see the LAN's ARP table.
        log_event('checkin', ip=client_ip(), asset_id=asset_id, album=current_albumname,
                  battery_mv=int(reported_mv) if reported_mv else None,
                  battery_pct=calculate_battery_percentage(reported_mv) if reported_mv else None,
                  mac=request.headers.get('X-Device-Mac'),
                  rssi=request.headers.get('X-Device-Rssi'),
                  agent=request.headers.get('User-Agent'))

        return response

    except ImmichError as e:
        log_event('error', where='download', message=e.message, ip=client_ip())
        return jsonify({"error": e.message}), e.status
    except Exception as e:
        log_event('error', where='download', message=str(e), ip=client_ip())
        return jsonify({"error": str(e)}), 500

@app.route('/sleep', methods=['GET'])
def get_sleep_duration():
    # Use system time instead of NTP sync
    current_time = datetime.now()
    
    # Get wake interval from config (in minutes)
    interval = int(current_config['immich']['wakeup_interval'])
    
    def calculate_next_interval_time(base_time, intervals=1):
        # Calculate next interval time
        total_minutes = base_time.hour * 60 + base_time.minute
        next_total_minutes = interval * ((total_minutes // interval) + intervals)
        
        # Handle case where next_total_minutes exceeds 24 hours
        next_total_minutes = next_total_minutes % (24 * 60)  # Wrap around to next day
        
        # Create next wake time
        next_time = base_time.replace(
            hour=next_total_minutes // 60,
            minute=next_total_minutes % 60,
            second=0,
            microsecond=0
        )
        
        # If we crossed into the next day, add a day
        if next_time < base_time:
            next_time = next_time + timedelta(days=1)
        
        return next_time
    
    # Get initial next wake time
    next_wakeup = calculate_next_interval_time(current_time)
    
    # Check if next wake time is in sleep period
    sleep_start = current_time.replace(
        hour=current_config['immich']['sleep_start_hour'],
        minute=current_config['immich']['sleep_start_minute'],
        second=0,
        microsecond=0
    )
    
    sleep_end = current_time.replace(
        hour=current_config['immich']['sleep_end_hour'],
        minute=current_config['immich']['sleep_end_minute'],
        second=0,
        microsecond=0
    )

    # Adjust sleep end time if it's less than start time (crosses midnight)
    if sleep_end < sleep_start:
        if current_time >= sleep_start:
            sleep_end = sleep_end + timedelta(days=1)
        elif current_time < sleep_end:
            sleep_start = sleep_start - timedelta(days=1)

    # If next wake time is in sleep period, set to sleep end time
    if sleep_start <= next_wakeup < sleep_end:
        next_wakeup = sleep_end

    # Calculate sleep duration in milliseconds
    sleep_ms = int((next_wakeup - current_time).total_seconds() * 1000)
    
    # If sleep duration is less than 10 minutes, calculate next interval
    if sleep_ms < 600000:  # 10 minutes = 600,000 milliseconds
        next_wakeup = calculate_next_interval_time(current_time, intervals=2)
        # Check again for sleep period
        if sleep_start <= next_wakeup < sleep_end:
            next_wakeup = sleep_end
        sleep_ms = int((next_wakeup - current_time).total_seconds() * 1000)
    
    # Deliberately not logged: /sleep runs on every wake-up and its answer is
    # implied by the check-in, so it only crowded out the entries that matter.
    return jsonify({
        "current_time": current_time.strftime("%Y-%m-%d %H:%M:%S"),
        "next_wakeup": next_wakeup.strftime("%Y-%m-%d %H:%M:%S"),
        "sleep_duration": sleep_ms
    })

def sync_time_with_ntp():
    """Sync time with NTP server"""
    try:
        ntp_client = ntplib.NTPClient()
        response = ntp_client.request('pool.ntp.org', timeout=5)
        return datetime.fromtimestamp(response.tx_time)
    except Exception as e:
        print(f"NTP sync failed: {e}")
        return datetime.now()

# def calculate_next_wakeup(current_time, wakeup_hour, wakeup_minute):
#     """Calculate next wakeup time, considering sleep time range"""
#     # Get sleep time range
#     sleep_start = current_time.replace(
#         hour=current_config['immich']['sleep_start_hour'],
#         minute=current_config['immich']['sleep_start_minute'],
#         second=0,
#         microsecond=0
#     )
    
#     sleep_end = current_time.replace(
#         hour=current_config['immich']['sleep_end_hour'],
#         minute=current_config['immich']['sleep_end_minute'],
#         second=0,
#         microsecond=0
#     )

#     # If current time is before sleep end time but after midnight
#     if sleep_end < sleep_start and current_time < sleep_end:
#         sleep_start = sleep_start - timedelta(days=1)
#     # If current time is after sleep start time
#     elif sleep_end < sleep_start and current_time >= sleep_start:
#         sleep_end = sleep_end + timedelta(days=1)
    
#     # Calculate next wake up time
#     interval_minutes = int(current_config['immich']['wakeup_interval'])
#     next_wakeup = current_time + timedelta(minutes=interval_minutes)
    
#     # Adjust next wake up time if it falls within sleep period
#     if sleep_start <= next_wakeup < sleep_end:
#         next_wakeup = sleep_end
    
#     return next_wakeup

if __name__ == '__main__':
    main()
