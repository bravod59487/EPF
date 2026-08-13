# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An e-paper photo frame split across two codebases that talk over HTTP:

- **Server** (`app.py`, `cpy.pyx`, `templates/`) — a Flask app, normally run in Docker on a NAS. Pulls photos from an [Immich](https://immich.app) album, crops/enhances/dithers them to the panel's 6-color palette, and serves the result already packed for the display.
- **Firmware** (`Arduino/`) — an ESP32-C6 sketch (`epd7in3e.ino`) for a Waveshare 7.3" Spectra 6 (E6) panel, 800x480. It does no image processing: it streams bytes straight into the panel and goes back into deep sleep.
- **CAD** (`CAD/*.STEP`) — enclosure parts, not built by any toolchain here.

There are no tests, no linter config, and no build system for the Python side.

## Server: build and run

```bash
docker build -t jwchen119/epf .
docker run --name epf \
  -e IMMICH_API_KEY='<key>' \
  -v /host/config:/config \
  -v /host/photos:/photos \
  -d -p <port>:5000 jwchen119/epf
```

Or directly: `python app.py` (serves on `0.0.0.0:5000`).

Two paths are **hardcoded**, not configurable:

- `/config/config.yaml` — written by the settings page, watched by `watchdog` for external edits. Created with `DEFAULT_CONFIG` if missing. Without a volume here, settings are lost on container restart.
- `/photos` (override with `IMMICH_PHOTO_DEST`) — holds only `tracking.txt`; no photos are ever written to disk.

`IMMICH_API_KEY` is read once at import into the module-level `headers` dict. Note the README's `docker run` example writes `IMMICH-API-KEY` with hyphens, which the app does not read.

## Server: HTTP contract with the firmware

This contract is the thing to be careful about — both sides must change together.

- `GET /download` — the device sends its battery voltage in a `batteryCap` **request header** (millivolts). Response is `text/plain`: ASCII hex bytes as `"XX,XX,..."` terminated by `};`, i.e. C-array source text, not binary. The `X-Photo-Url` response header carries the Immich web URL of the chosen photo (intended for writing an NFC tag; the firmware does not read it yet).
- `GET /sleep` — returns `{current_time, next_wakeup, sleep_duration}` where `sleep_duration` is **milliseconds**. The firmware divides by 1000 and passes it to `esp_deep_sleep`. Falls back to 24h if absent. `/download` and `/sleep` are two separate requests per wake cycle.
- `GET /setting` (GET renders, POST saves) — the config UI; `/` redirects here. Battery percentage shown here comes from the last `/download` request's header, cached in module globals for one hour, so it reads 0% until the device has checked in.

## Server: image pipeline

`/download` → pick asset → `scale_img_in_memory()` → `convert_to_c_code_in_memory()`. Everything is in-memory `BytesIO`.

1. **Asset selection.** Album assets are fetched via paginated `POST /api/search/metadata` filtered by `albumIds` — *not* `GET /api/albums/{id}`, which stopped returning `assets` in Immich v3. `tracking.txt` records which assets have been shown: line 1 is the album name (changing albums resets the file), remaining lines are asset IDs. `image_order` is `random` (reset when exhausted) or `newest` (reset when a newer photo appears).
2. **Scale + enhance.** `cpy.load_scaled()` rotates and either letterboxes (`fit`) or center-crops (`fill`) to 800x480, then PIL `ImageEnhance` applies `enhanced` (saturation) and `contrast`.
3. **Quantize.** `cpy.convert_image()` does Floyd-Steinberg dithering to six pure-RGB colors, with `strength` scaling the error diffusion. The commented-out PIL `.quantize()` block in `scale_img_in_memory` is the superseded version.
4. **Pack.** `depalette_image()` nearest-matches each pixel against the module-level `palette` (the *measured* panel colors, e.g. yellow is `(255,243,56)`) and applies `indices[indices > 3] += 1` to line up with the panel's color codes in `Arduino/epd7in3e.h`. Then two 4-bit indices are packed per byte.

Three palettes must stay consistent: the pure-RGB one inside `cpy.pyx:convert_image`, the measured one at the top of `app.py`, and the `EPD_7IN3E_*` codes in the firmware header.

## cpy: the Cython module

**`cpy.so` is a prebuilt Linux x86-64 binary committed to the repo, and there is no `setup.py` or build step anywhere — not in the Dockerfile, not in `requirements.txt`.** Editing `cpy.pyx` therefore has no effect until you compile it yourself and replace `cpy.so`; on Windows you cannot load the committed `.so` at all, so `import app` fails locally. Assume any `.pyx` change needs a Linux build (`cython` + `numpy` headers) plus a note to the user that the binary must be regenerated.

`EPD_W`/`EPD_H` are duplicated as module constants in `cpy.pyx`; the target size is not passed in. `scale_img_in_memory`'s `target_width`/`target_height` arguments only affect the (currently disabled) date-overlay positioning.

## Firmware: build and flow

Arduino IDE, board FireBeetle 2 ESP32-C6. The folder must be renamed to `epd7in3e` to match the `.ino`. Libraries: ArduinoJson, AsyncTCP, ESPAsyncWebServer. Pin map is in the comment block at the top of `epd7in3e.ino`.

`setup()` runs once per wake and never returns to `loop()`:

1. Read battery on ADC pin 0 (×2 for the divider). Below 3050 mV: clear the screen and sleep 24h.
2. `epd.Init()`, mount SPIFFS, open the `data` Preferences namespace.
3. `Button(CONFIG_PIN).result()` — blocks ~3.5s watching GPIO 2. A ~3s hold enters the captive portal; a short press just proceeds (that's the wake-and-refresh path).
4. Connect via `WifiCaptivePortal` (up to 5 saved SSIDs, AP `ESP32_ePAPER` at `http://4.3.2.1`), or start the portal if nothing is saved.
5. `downloadImage()`: read `SERVER_BASE_URL` from Preferences, GET `/download`, stream-parse the hex text and `epd.SendData()` each byte after `SendCommand(0x10)`, then `TurnOnDisplay()`; GET `/sleep`; `hibernate()`.

State lives in two Preferences namespaces: `data` (`SERVER_BASE_URL`, retry counters) and `wificaptive` (SSIDs/passwords, last-used index). `WifiCaptive.cpp` writes `SERVER_BASE_URL` on portal save; the `SERVER_BASE_URL` `#define` in `config.h` is a leftover and is not the value used at runtime.

Deep sleep wakes on the timer or on GPIO 2 going low (`ext1`). `epd.Sleep()` before hibernating matters for the ~16µA target — dropping it leaves the panel drawing current.

`WifiCaptive*` files are adapted from [TRMNL firmware](https://github.com/usetrmnl/firmware/tree/main/lib/wificaptive); `epd7in3e.*` and `epdif.*` are Waveshare vendor drivers. Prefer keeping local edits minimal and obvious in all of these.

## Conventions

Everything committed to this repo is written in **English** — code, comments, identifiers, commit messages, docs — because changes may be submitted upstream as merge requests. Pre-existing Traditional Chinese comments in `cpy.pyx` and `Arduino/button.h` are the original author's; leave them alone, but write new comments in English.
