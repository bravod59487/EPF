import gzip
import os

HERE = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(HERE, 'index.html')
HEADER_PATH = os.path.join(HERE, '..', 'WifiCaptivePage.h')
import re

# Read the WifiCaptivePage.h file
with open(HEADER_PATH, 'r') as f:
    content = f.read()

# Extract the byte array
match = re.search(r'\{([^}]+)\}', content)
if match:
    hex_str = match.group(1)
    # Parse hex values
    bytes_list = []
    for item in hex_str.split(','):
        item = item.strip()
        if item:
            bytes_list.append(int(item, 16))
    
    # Decompress gzip
    compressed = bytes(bytes_list)
    decompressed = gzip.decompress(compressed)
    
    # Write decompressed HTML
    with open(HTML_PATH, 'wb') as f:
        f.write(decompressed)
    
    print(f"Decompressed {len(compressed)} bytes -> {len(decompressed)} bytes")
    print(f"Saved to scratch/index.html")
