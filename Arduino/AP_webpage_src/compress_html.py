import gzip
import os

HERE = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(HERE, 'index.html')
HEADER_PATH = os.path.join(HERE, '..', 'WifiCaptivePage.h')

# Read the modified HTML
with open(HTML_PATH, 'rb') as f:
    html_content = f.read()

# Compress with gzip
compressed = gzip.compress(html_content, compresslevel=9)

# Format as C byte array
hex_values = ', '.join(f'0x{b:x}' for b in compressed)

# Generate the header file
header = f"""#ifndef WifiCaptivePage_h
#define WifiCaptivePage_h

#include <pgmspace.h>

const uint8_t INDEX_HTML[] PROGMEM = {{ {hex_values} }};
const int INDEX_HTML_LEN = sizeof(INDEX_HTML);

#endif
"""

with open(HEADER_PATH, 'w') as f:
    f.write(header)

print(f"Original HTML: {len(html_content)} bytes")
print(f"Compressed: {len(compressed)} bytes")
print(f"WifiCaptivePage.h updated")
