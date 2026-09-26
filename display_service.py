#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Web control service for the Waveshare 1.8" LCD (ST7735S, 128x160).

Runs a local Flask API plus a single background worker thread that owns the
SPI bus and GPIO pins, so only one mode ever draws to the panel at a time.

Confirmed panel parameters:
    width = 128, height = 160, col_offset (X) = 2, row_offset (Y) = 1
    RST = GPIO 27, DC = GPIO 25, BL = GPIO 18, SPI bus 0 / device 0
    SPI data is written in <= 4096 byte chunks (spidev limit).

API endpoints (both /api/* and /lcd-api/* are served):
    POST /api/text          {"text", "bg_color", "text_color", "font_size"}
    POST /api/upload        multipart file (png/jpg/gif), scaled to 128x160
    POST /api/mode/stats    looping clock + IP + CPU temp + RAM
    POST /api/mode/weather  Open-Meteo weather for Kraczkowa
    POST /api/backlight     {"on": true|false}
    POST /api/clear         clear the panel
    POST /api/stop          stop the current mode, show idle screen
    GET  /api/status        current mode / backlight / health
    GET  /api/preview.png   PNG mirror of the last frame pushed to the panel
"""

import io
import os
import random
import socket
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime

os.environ.setdefault("GPIOZERO_PIN_FACTORY", "lgpio")

try:
    import numpy as np
    import requests
    import spidev
    from flask import Flask, Response, jsonify, request, send_from_directory
    from gpiozero import DigitalOutputDevice
    from PIL import Image, ImageDraw, ImageFont, ImageOps
except ImportError as exc:  # pragma: no cover
    sys.stderr.write("Missing library: %s\n" % exc)
    sys.stderr.write("Use the project venv: ./venv/bin/python display_service.py\n")
    raise

# --------------------------------------------------------------------------
# Live frame mirror + hardware backlight state
# --------------------------------------------------------------------------
latest_frame_png = None
frame_lock = threading.Lock()
current_brightness = 100

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

RST_PIN = 27
DC_PIN = 25
BL_PIN = 18

SPI_BUS = 0
SPI_DEVICE = 0
SPI_SPEED_HZ = 10_000_000
SPI_MODE = 0
CHUNK_SIZE = 4096

BACKLIGHT_DUTY = 100

# Backlight on GPIO 18 (Pin 12) is plain digital ON/OFF. PWMOutputDevice is
# deliberately avoided: hardware PWM locks up on this kernel. A single
# DigitalOutputDevice is created here and shared by the ST7735S driver.
try:
    bl_device = DigitalOutputDevice(BL_PIN, active_high=True, initial_value=True)
except Exception:
    bl_device = None

X_OFFSET = 2
Y_OFFSET = 1

WIDTH = 128
HEIGHT = 160

# --------------------------------------------------------------------------
# Rotation (dynamic, selectable from the web dashboard)
#
# All render_* helpers draw in "logical" (UI) coordinates. A rotation of
# 0/180 keeps the native 128x160 portrait viewport; 90/270 uses a 160x128
# landscape viewport. Before the buffer is pushed to the panel the logical
# image is rotated back into the physical 128x160 frame.
# --------------------------------------------------------------------------
ROTATIONS = (0, 90, 180, 270)
DEFAULT_ROTATION = 180

_rotation_lock = threading.Lock()
_rotation = DEFAULT_ROTATION


def get_rotation():
    with _rotation_lock:
        return _rotation


def set_rotation(value):
    global _rotation
    try:
        value = int(value) % 360
    except (TypeError, ValueError):
        raise ValueError("rotation must be one of %s" % (list(ROTATIONS),))
    if value not in ROTATIONS:
        raise ValueError("rotation must be one of %s" % (list(ROTATIONS),))
    with _rotation_lock:
        _rotation = value
    return _rotation


def logical_size(rotation=None):
    """Logical (UI) viewport for a rotation; 90/270 swap width and height."""
    rot = get_rotation() if rotation is None else rotation
    if rot in (90, 270):
        return HEIGHT, WIDTH
    return WIDTH, HEIGHT


def rotate_to_panel(image, rotation=None):
    """Rotate a logical-size PIL image into the physical 128x160 panel frame."""
    rot = get_rotation() if rotation is None else rotation
    if rot == 0:
        return image
    if rot == 180:
        return image.rotate(180, expand=True)
    if rot == 90:
        return image.rotate(90, expand=True)
    return image.rotate(270, expand=True)


def set_lcd_brightness(percent):
    """Set backlight ON/OFF; returns True on success.

    The panel backlight is digital-only (no PWM), so any request above 0 is
    safely clamped to full ON and 0 turns it off.
    """
    global current_brightness
    try:
        val = max(0, min(100, int(percent)))
        current_brightness = val
        if bl_device is not None:
            bl_device.value = val > 0
        return True
    except Exception:
        return False


HOST = "127.0.0.1"
PORT = 5005

STATS_REFRESH_SECONDS = 5.0
WEATHER_REFRESH_SECONDS = 600.0
WEATHER_LAT = 50.0342
WEATHER_LON = 22.1681
WEATHER_CITY = "Kraczkowa"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"

MAX_UPLOAD_MB = 64
MAX_GIF_FRAMES = 300

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

# --------------------------------------------------------------------------
# Built-in retro GIF library + DEMO (autoplay) mode
# --------------------------------------------------------------------------
GIF_DIR = os.path.join(BASE_DIR, "assets", "gifs")
PRESETS = {
    "mario": "mario.gif",
    "coin": "coin.gif",
    "matrix": "matrix.gif",
}

DEMO_SLIDE_SECONDS = 7.0
DEMO_GIF_SECONDS = {"mario": 6.0, "matrix": 5.0, "coin": 5.0}

# --------------------------------------------------------------------------
# ST7735S driver (Waveshare init sequence, native portrait 128x160)
# --------------------------------------------------------------------------
class ST7735S:
    def __init__(self, spi_speed=SPI_SPEED_HZ):
        self.dc = DigitalOutputDevice(DC_PIN, active_high=True, initial_value=False)
        self.rst = DigitalOutputDevice(RST_PIN, active_high=True, initial_value=False)
        self.bl = bl_device if bl_device is not None else DigitalOutputDevice(
            BL_PIN, active_high=True, initial_value=False)

        self.spi = spidev.SpiDev()
        self.spi.open(SPI_BUS, SPI_DEVICE)
        self.spi.max_speed_hz = spi_speed
        self.spi.mode = SPI_MODE

        self.width = WIDTH
        self.height = HEIGHT
        self._io_lock = threading.RLock()

    # ---- low level ---------------------------------------------------------
    def command(self, cmd):
        self.dc.off()
        self.spi.xfer2([cmd])

    def data(self, val):
        self.dc.on()
        self.spi.xfer2([val])

    def write_data(self, buf):
        self.dc.on()
        view = memoryview(buf)
        for i in range(0, len(view), CHUNK_SIZE):
            self.spi.xfer2(list(view[i:i + CHUNK_SIZE]))

    def _cmd_data(self, cmd, params):
        self.command(cmd)
        for p in params:
            self.data(p)

    def backlight(self, on=True, duty=BACKLIGHT_DUTY):
        with self._io_lock:
            self.bl.value = bool(on)

    def reset(self):
        with self._io_lock:
            self.rst.off()
            time.sleep(0.1)
            self.rst.on()
            time.sleep(0.1)

    def init_reg(self):
        self._cmd_data(0xB1, [0x01, 0x2C, 0x2D])
        self._cmd_data(0xB2, [0x01, 0x2C, 0x2D])
        self._cmd_data(0xB3, [0x01, 0x2C, 0x2D, 0x01, 0x2C, 0x2D])
        self._cmd_data(0xB4, [0x07])
        self._cmd_data(0xC0, [0xA2, 0x02, 0x84])
        self._cmd_data(0xC1, [0xC5])
        self._cmd_data(0xC2, [0x0A, 0x00])
        self._cmd_data(0xC3, [0x8A, 0x2A])
        self._cmd_data(0xC4, [0x8A, 0xEE])
        self._cmd_data(0xC5, [0x0E])
        self._cmd_data(0xE0, [0x0F, 0x1A, 0x0F, 0x18, 0x2F, 0x28, 0x20,
                              0x22, 0x1F, 0x1B, 0x23, 0x37, 0x00, 0x07,
                              0x02, 0x10])
        self._cmd_data(0xE1, [0x0F, 0x1B, 0x0F, 0x17, 0x33, 0x2C, 0x29,
                              0x2E, 0x30, 0x30, 0x39, 0x3F, 0x00, 0x07,
                              0x03, 0x10])
        self._cmd_data(0xF0, [0x01])
        self._cmd_data(0xF6, [0x00])
        self._cmd_data(0x3A, [0x05])  # COLMOD: RGB565
        self._cmd_data(0x36, [0x00])  # MADCTL: L2R_U2D, native portrait

    def init(self):
        self.reset()
        self.init_reg()
        time.sleep(0.2)
        self.command(0x11)  # SLPOUT
        time.sleep(0.12)
        self.command(0x29)  # DISPON
        time.sleep(0.05)

    def set_window(self):
        self.command(0x2A)
        self.data(0x00)
        self.data(X_OFFSET)
        self.data(0x00)
        self.data((WIDTH - 1) + X_OFFSET)

        self.command(0x2B)
        self.data(0x00)
        self.data(Y_OFFSET)
        self.data(0x00)
        self.data((HEIGHT - 1) + Y_OFFSET)

        self.command(0x2C)

    def show_buffer(self, buf):
        """Write a pre-converted RGB565 buffer (len == WIDTH*HEIGHT*2)."""
        with self._io_lock:
            self.set_window()
            self.write_data(buf)
        store_preview_frame(buf)

    def show(self, image):
        """Rotate a logical-size UI image into the panel frame and push it."""
        global latest_frame_png
        try:
            _buf = io.BytesIO()
            image.save(_buf, format="PNG")
            with frame_lock:
                latest_frame_png = _buf.getvalue()
        except Exception:
            pass
        img = rotate_to_panel(image)
        if img.size != (WIDTH, HEIGHT):
            img = img.resize((WIDTH, HEIGHT))
        self.show_buffer(image_to_rgb565(img))

    def clear(self, rgb=(0, 0, 0)):
        hi = (rgb_to_565(rgb) >> 8) & 0xFF
        lo = rgb_to_565(rgb) & 0xFF
        self.show_buffer(bytes([hi, lo]) * (WIDTH * HEIGHT))

    def close(self):
        try:
            self.spi.close()
        except Exception:
            pass
        for dev in (self.dc, self.rst, self.bl):
            try:
                dev.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# Rendering helpers
# --------------------------------------------------------------------------
def load_font(size, bold=True):
    path = FONT_BOLD if bold else FONT_REGULAR
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        try:
            return ImageFont.truetype(FONT_REGULAR, size)
        except Exception:
            return ImageFont.load_default()


def hex_to_rgb(value, default=(0, 0, 0)):
    if not value:
        return default
    value = str(value).strip().lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if len(value) != 6:
        return default
    try:
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return default


def rgb_to_565(rgb):
    r, g, b = rgb
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


def image_to_rgb565(image):
    arr = np.asarray(image.convert("RGB"), dtype=np.uint16)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    value = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
    buf = np.empty((HEIGHT, WIDTH, 2), dtype=np.uint8)
    buf[..., 0] = (value >> 8) & 0xFF
    buf[..., 1] = value & 0xFF
    return buf.tobytes()


# --------------------------------------------------------------------------
# Live hardware preview (mirror of the last frame pushed to the panel)
#
# Every RGB565 buffer that reaches the SPI bus is also decoded back into an
# RGB image and PNG-encoded into memory, so the web dashboard can show the
# exact pixels currently on the physical ST7735S (including rotation, stats,
# weather and GIF animation frames).
# --------------------------------------------------------------------------
_preview_lock = threading.Lock()
_preview_png = None
_preview_seq = 0


def _rgb565_to_image(buf):
    """Rebuild an RGB PIL image from a raw RGB565 frame buffer."""
    arr = np.frombuffer(buf, dtype=np.uint8)
    if arr.size != WIDTH * HEIGHT * 2:
        return None
    arr = arr.reshape(HEIGHT, WIDTH, 2).astype(np.uint16)
    value = (arr[..., 0] << 8) | arr[..., 1]
    red = ((value >> 8) & 0xF8).astype(np.uint8)
    green = ((value >> 3) & 0xFC).astype(np.uint8)
    blue = ((value << 3) & 0xF8).astype(np.uint8)
    rgb = np.empty((HEIGHT, WIDTH, 3), dtype=np.uint8)
    rgb[..., 0] = red | (red >> 5)
    rgb[..., 1] = green | (green >> 6)
    rgb[..., 2] = blue | (blue >> 5)
    return Image.fromarray(rgb, "RGB")


def store_preview_frame(buf):
    """PNG-encode the RGB565 buffer that was just written to the panel."""
    global _preview_png, _preview_seq, latest_frame_png
    try:
        image = _rgb565_to_image(buf)
        if image is None:
            return
        out = io.BytesIO()
        image.save(out, format="PNG")
        png = out.getvalue()
        with _preview_lock:
            _preview_png = png
            _preview_seq += 1
        with frame_lock:
            latest_frame_png = png
    except Exception:
        traceback.print_exc()


def get_preview_frame():
    with _preview_lock:
        return _preview_png, _preview_seq


def render_preview_placeholder():
    img = Image.new("RGB", (WIDTH, HEIGHT), (8, 8, 20))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, WIDTH - 1, HEIGHT - 1], outline=(120, 45, 45), width=2)
    font = load_font(13, bold=True)
    txt = "NO SIGNAL"
    w, _ = text_size(draw, txt, font)
    bbox = draw.textbbox((0, 0), txt, font=font)
    draw.text(((WIDTH - w) // 2 - bbox[0], HEIGHT // 2 - 8), txt,
              font=font, fill=(190, 80, 80))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def text_size(draw, text, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def text_width(text, font):
    bbox = font.getbbox(text)
    return bbox[2] - bbox[0]


def fit_font(text, max_width, start, min_size=9, bold=True):
    """Largest font <= start whose rendered width fits inside max_width."""
    size = max(min_size, int(start))
    font = load_font(size, bold=bold)
    while size > min_size and text_width(text, font) > max_width:
        size -= 1
        font = load_font(size, bold=bold)
    return font


def wrap_lines(draw, text, font, max_width):
    lines = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            candidate = current + " " + word
            if text_size(draw, candidate, font)[0] <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def render_text(text, bg, fg, font_size=0):
    W, H = logical_size()
    text = (text or "").strip() or " "
    img = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)
    max_w, max_h = W - 8, H - 8

    if font_size and int(font_size) > 0:
        sizes = [min(int(font_size), 60)]
    else:
        sizes = list(range(40, 7, -1))

    chosen = None
    for size in sizes:
        font = load_font(size, bold=True)
        lines = wrap_lines(draw, text, font, max_w)
        line_h = max(text_size(draw, ln, font)[1] for ln in lines) + 2
        total_h = line_h * len(lines)
        if total_h <= max_h and all(text_size(draw, ln, font)[0] <= max_w for ln in lines):
            chosen = (font, lines, line_h)
            break
    if chosen is None:
        font = load_font(8, bold=True)
        lines = wrap_lines(draw, text, font, max_w)
        chosen = (font, lines, 10)

    font, lines, line_h = chosen
    total_h = line_h * len(lines)
    y = (H - total_h) // 2
    for ln in lines:
        w, _ = text_size(draw, ln, font)
        x = (W - w) // 2
        bbox = draw.textbbox((0, 0), ln, font=font)
        draw.text((x - bbox[0], y), ln, font=font, fill=fg)
        y += line_h
    return img


def render_idle():
    W, H = logical_size()
    img = Image.new("RGB", (W, H), (8, 8, 20))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, W - 1, H - 1], outline=(16, 185, 129), width=2)
    title = load_font(15, bold=True)
    small = load_font(11, bold=False)
    mid = H // 2
    for txt, font, y, color in (
        ("LCD 1.8\"", title, mid - 32, (16, 185, 129)),
        ("ST7735S", small, mid - 10, (200, 200, 200)),
        ("gotowy", small, mid + 18, (150, 150, 150)),
    ):
        w, _ = text_size(draw, txt, font)
        bbox = draw.textbbox((0, 0), txt, font=font)
        draw.text(((W - w) // 2 - bbox[0], y), txt, font=font, fill=color)
    return img


# --------------------------------------------------------------------------
# System stats
# --------------------------------------------------------------------------
def get_cpu_temp():
    for path in ("/sys/class/thermal/thermal_zone0/temp",
                 "/sys/class/hwmon/hwmon0/temp1_input"):
        try:
            with open(path) as fh:
                return float(fh.read().strip()) / 1000.0
        except Exception:
            continue
    return None


def get_ram_percent():
    try:
        values = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                values[key.strip()] = float(rest.strip().split()[0])
        total = values.get("MemTotal", 0.0)
        available = values.get("MemAvailable", values.get("MemFree", 0.0))
        if total <= 0:
            return None
        return (total - available) / total * 100.0
    except Exception:
        return None


def get_ip_address():
    try:
        out = subprocess.check_output(["hostname", "-I"], text=True, timeout=3)
        for ip in out.split():
            if ":" not in ip and not ip.startswith("127."):
                return ip
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "n/a"


def render_stats():
    """Clock + system stats laid out to fit the current logical viewport.

    The clock font shrinks until ``HH:MM:SS`` fits the width, and the three
    data rows are distributed over the remaining height, so nothing is ever
    clipped in either portrait (128x160) or landscape (160x128).
    """
    W, H = logical_size()
    img = Image.new("RGB", (W, H), (6, 8, 18))
    draw = ImageDraw.Draw(img)
    now = datetime.now()

    margin = 6
    usable = W - 2 * margin

    f_clock = fit_font("00:00:00", usable, start=34, min_size=13, bold=True)
    clock = now.strftime("%H:%M:%S")
    cw, ch = text_size(draw, clock, f_clock)
    cbx = draw.textbbox((0, 0), clock, font=f_clock)[0]
    draw.text(((W - cw) // 2 - cbx, margin), clock, font=f_clock, fill=(16, 185, 129))
    y = margin + ch + 3

    f_date = load_font(11, bold=False)
    date = now.strftime("%a %d.%m.%Y")
    dw, dh = text_size(draw, date, f_date)
    dbx = draw.textbbox((0, 0), date, font=f_date)[0]
    draw.text(((W - dw) // 2 - dbx, y), date, font=f_date, fill=(170, 170, 180))
    y += dh + 5

    draw.line([margin, y, W - margin, y], fill=(40, 42, 60))
    y += 5

    temp = get_cpu_temp()
    ram = get_ram_percent()
    ip = get_ip_address()

    rows = [
        ("CPU", ("%.1f C" % temp) if temp is not None else "n/a", (255, 120, 90)),
        ("RAM", ("%.0f %%" % ram) if ram is not None else "n/a", (120, 180, 255)),
        ("IP", ip, (230, 230, 120)),
    ]

    f_label = load_font(11, bold=True)
    label_w = max(text_size(draw, name, f_label)[0] for name, _, _ in rows) + 6
    bottom = H - margin
    row_h = max(14, (bottom - y) // len(rows))

    for index, (label, value, color) in enumerate(rows):
        f_value = fit_font(value, usable - label_w, start=15, min_size=9, bold=True)
        lh = text_size(draw, label, f_label)[1]
        vh = text_size(draw, value, f_value)[1]
        ty = y + index * row_h + (row_h - max(lh, vh)) // 2
        draw.text((margin, ty - draw.textbbox((0, 0), label, font=f_label)[1]),
                  label, font=f_label, fill=(140, 145, 165))
        vw = text_size(draw, value, f_value)[0]
        vbx = draw.textbbox((0, 0), value, font=f_value)[0]
        draw.text((W - margin - vw - vbx,
                   ty - draw.textbbox((0, 0), value, font=f_value)[1]),
                  value, font=f_value, fill=color)
    return img


# --------------------------------------------------------------------------
# Weather (Open-Meteo)
# --------------------------------------------------------------------------
WEATHER_CODES = {
    0: ("Bezchmurnie", "sun"),
    1: ("Gl. bezchmurnie", "sun"),
    2: ("Czesc. chmury", "partly"),
    3: ("Zachmurzone", "cloud"),
    45: ("Mgla", "fog"),
    48: ("Mgla osadz.", "fog"),
    51: ("Mzawka slab.", "drizzle"),
    53: ("Mzawka", "drizzle"),
    55: ("Mzawka gesta", "drizzle"),
    56: ("Mzawka mroz.", "drizzle"),
    57: ("Mzawka mroz.", "drizzle"),
    61: ("Deszcz slab.", "rain"),
    63: ("Deszcz", "rain"),
    65: ("Deszcz silny", "rain"),
    66: ("Deszcz mroz.", "rain"),
    67: ("Deszcz mroz.", "rain"),
    71: ("Snieg slab.", "snow"),
    73: ("Snieg", "snow"),
    75: ("Snieg silny", "snow"),
    77: ("Snieg ziarna", "snow"),
    80: ("Przelotny desz.", "rain"),
    81: ("Przelotny desz.", "rain"),
    82: ("Ulewa", "rain"),
    85: ("Przelotny snieg", "snow"),
    86: ("Przelotny snieg", "snow"),
    95: ("Burza", "thunder"),
    96: ("Burza z gradem", "thunder"),
    99: ("Burza z gradem", "thunder"),
}


def draw_weather_icon(draw, cx, cy, kind):
    if kind == "sun":
        draw.ellipse([cx - 16, cy - 16, cx + 16, cy + 16], fill=(255, 200, 40))
        for dx, dy in ((0, -24), (0, 24), (-24, 0), (24, 0),
                       (-17, -17), (17, -17), (-17, 17), (17, 17)):
            draw.line([cx + dx * 0.72, cy + dy * 0.72, cx + dx, cy + dy],
                      fill=(255, 200, 40), width=3)
    elif kind in ("cloud", "partly", "fog"):
        if kind == "partly":
            draw.ellipse([cx + 2, cy - 22, cx + 26, cy + 2], fill=(255, 200, 40))
        draw.ellipse([cx - 26, cy - 6, cx + 2, cy + 20], fill=(210, 214, 222))
        draw.ellipse([cx - 6, cy - 16, cx + 22, cy + 14], fill=(230, 233, 240))
        draw.ellipse([cx - 14, cy - 2, cx + 18, cy + 22], fill=(195, 200, 210))
        if kind == "fog":
            for i, yy in enumerate((cy + 26, cy + 32)):
                draw.line([cx - 26 + i * 6, yy, cx + 24, yy], fill=(160, 165, 175), width=2)
    elif kind in ("rain", "drizzle", "thunder"):
        draw.ellipse([cx - 26, cy - 14, cx + 4, cy + 12], fill=(180, 186, 198))
        draw.ellipse([cx - 6, cy - 22, cx + 24, cy + 10], fill=(205, 210, 220))
        draw.ellipse([cx - 16, cy - 8, cx + 18, cy + 18], fill=(165, 172, 186))
        for dx in (-16, 0, 16):
            draw.line([cx + dx, cy + 20, cx + dx - 4, cy + 34], fill=(90, 160, 255), width=3)
        if kind == "thunder":
            draw.polygon([(cx - 2, cy + 16), (cx + 10, cy + 16),
                          (cx + 2, cy + 30), (cx + 14, cy + 30),
                          (cx - 6, cy + 50), (cx + 2, cy + 34),
                          (cx - 8, cy + 34)], fill=(255, 220, 60))
    elif kind == "snow":
        draw.ellipse([cx - 26, cy - 16, cx + 4, cy + 10], fill=(210, 214, 222))
        draw.ellipse([cx - 8, cy - 24, cx + 22, cy + 8], fill=(235, 238, 245))
        for dx, dy in ((-16, 24), (0, 30), (16, 24), (-8, 38), (8, 38)):
            draw.ellipse([cx + dx - 3, cy + dy - 3, cx + dx + 3, cy + dy + 3],
                         fill=(255, 255, 255))


_weather_cache_lock = threading.Lock()
_weather_cache = {"data": None, "ts": 0.0}


def fetch_weather(timeout=15):
    params = {
        "latitude": WEATHER_LAT,
        "longitude": WEATHER_LON,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                   "weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min",
        "timezone": "Europe/Warsaw",
        "forecast_days": 1,
    }
    resp = requests.get(WEATHER_URL, params=params, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    current = data.get("current", {})
    daily = data.get("daily", {})
    code = int(current.get("weather_code", 3))
    label, icon = WEATHER_CODES.get(code, ("Nieznana", "cloud"))
    result = {
        "temp": current.get("temperature_2m"),
        "feels": current.get("apparent_temperature"),
        "humidity": current.get("relative_humidity_2m"),
        "wind": current.get("wind_speed_10m"),
        "label": label,
        "icon": icon,
        "tmax": (daily.get("temperature_2m_max") or [None])[0],
        "tmin": (daily.get("temperature_2m_min") or [None])[0],
    }
    with _weather_cache_lock:
        _weather_cache["data"] = result
        _weather_cache["ts"] = time.time()
    return result


def get_cached_weather(max_age=900.0, timeout=6):
    """Return the last weather reading, refreshing it if older than max_age.

    Used by DEMO mode so a slow/offline network never stalls the slideshow.
    """
    with _weather_cache_lock:
        data = _weather_cache["data"]
        ts = _weather_cache["ts"]
    if data is not None and (time.time() - ts) <= max_age:
        return data
    try:
        return fetch_weather(timeout=timeout)
    except Exception as exc:
        print("[weather] blad: %s" % exc)
        return data


def render_weather(data):
    W, H = logical_size()
    img = Image.new("RGB", (W, H), (10, 16, 32))
    draw = ImageDraw.Draw(img)
    icon_scale = H / float(HEIGHT)

    f_city = fit_font(WEATHER_CITY, W - 8, start=13, min_size=10, bold=True)
    f_temp = load_font(34, bold=True)
    f_desc = fit_font(data.get("label", ""), W - 8, start=11, min_size=8, bold=True)
    f_meta = load_font(10, bold=False)

    w, _ = text_size(draw, WEATHER_CITY, f_city)
    bbox = draw.textbbox((0, 0), WEATHER_CITY, font=f_city)
    draw.text(((W - w) // 2 - bbox[0], 5), WEATHER_CITY, font=f_city, fill=(150, 200, 255))

    icon_y = int(52 * icon_scale)
    draw_weather_icon(draw, W // 2, icon_y, data.get("icon", "cloud"))

    temp = data.get("temp")
    temp_txt = ("%.1f" % temp) if temp is not None else "--"
    w, _ = text_size(draw, temp_txt, f_temp)
    bbox = draw.textbbox((0, 0), temp_txt, font=f_temp)
    tx = (W - w) // 2 - bbox[0]
    temp_y = int(88 * icon_scale)
    draw.text((tx, temp_y), temp_txt, font=f_temp, fill=(255, 255, 255))
    draw.ellipse([tx + w + 4, temp_y + 4, tx + w + 12, temp_y + 12],
                 outline=(255, 255, 255), width=2)

    label = data.get("label", "")
    w, _ = text_size(draw, label, f_desc)
    bbox = draw.textbbox((0, 0), label, font=f_desc)
    draw.text(((W - w) // 2 - bbox[0], H - 34), label, font=f_desc, fill=(200, 205, 220))

    meta = "H:%s%%  W:%.0f" % (
        data.get("humidity", "--"),
        data.get("wind") if data.get("wind") is not None else 0.0,
    )
    w, _ = text_size(draw, meta, f_meta)
    bbox = draw.textbbox((0, 0), meta, font=f_meta)
    draw.text(((W - w) // 2 - bbox[0], H - 18), meta, font=f_meta, fill=(150, 155, 175))
    return img


def render_message(title, subtitle, color=(255, 120, 90)):
    W, H = logical_size()
    img = Image.new("RGB", (W, H), (8, 8, 20))
    draw = ImageDraw.Draw(img)
    f1 = fit_font(title, W - 8, start=16, min_size=10, bold=True)
    f2 = fit_font(subtitle, W - 8, start=11, min_size=8, bold=False)
    mid = H // 2
    for txt, font, y, col in (
        (title, f1, mid - 14, color),
        (subtitle, f2, mid + 12, (170, 170, 180)),
    ):
        w, _ = text_size(draw, txt, font)
        bbox = draw.textbbox((0, 0), txt, font=font)
        draw.text(((W - w) // 2 - bbox[0], y), txt, font=font, fill=col)
    return img


# --------------------------------------------------------------------------
# Image / GIF preparation
# --------------------------------------------------------------------------
def prepare_media(raw_bytes):
    """Decode an uploaded image/GIF into source RGB frames + durations.

    Frames are kept as PIL images (fit into the largest panel dimension) so
    they can be re-composed for whatever rotation is active when shown.
    """
    box = (max(WIDTH, HEIGHT), max(WIDTH, HEIGHT))
    with Image.open(io.BytesIO(raw_bytes)) as im:
        n_frames = getattr(im, "n_frames", 1)
        frames, durations = [], []
        for i in range(min(n_frames, MAX_GIF_FRAMES)):
            try:
                im.seek(i)
            except EOFError:
                break
            frames.append(ImageOps.contain(im.convert("RGB"), box))
            duration = im.info.get("duration") or 100
            durations.append(max(20, int(duration)))
    if not frames:
        raise ValueError("Nie udalo sie odczytac obrazu")
    return frames, durations


def compose_frame(source):
    """Build a logical-size canvas for one media source frame."""
    W, H = logical_size()
    canvas = Image.new("RGB", (W, H), (0, 0, 0))
    frame = ImageOps.contain(source, (W, H))
    canvas.paste(frame, ((W - frame.width) // 2, (H - frame.height) // 2))
    return canvas


# --------------------------------------------------------------------------
# Built-in GIF presets
# --------------------------------------------------------------------------
_preset_lock = threading.Lock()
_preset_cache = {}


def load_preset_frames(name):
    """Decode a named bundled GIF into (frames, durations), cached in memory."""
    key = str(name or "").strip().lower()
    if key not in PRESETS:
        raise ValueError("Nieznany preset: %s" % (name or ""))
    with _preset_lock:
        cached = _preset_cache.get(key)
    if cached is not None:
        return cached
    path = os.path.join(GIF_DIR, PRESETS[key])
    if not os.path.isfile(path):
        raise FileNotFoundError("Brak pliku %s (uruchom create_gifs.py)" % path)
    with open(path, "rb") as fh:
        frames, durations = prepare_media(fh.read())
    with _preset_lock:
        _preset_cache[key] = (frames, durations)
    return frames, durations


# --------------------------------------------------------------------------
# Single-worker display controller (prevents SPI/GPIO races)
# --------------------------------------------------------------------------
class DisplayController:
    def __init__(self, display):
        self.disp = display
        self._cond = threading.Condition()
        self._pending = None
        self._stop = threading.Event()
        self._state_lock = threading.Lock()
        self._state = {
            "mode": "idle",
            "detail": "",
            "backlight": True,
            "rotation": get_rotation(),
            "updated": time.time(),
        }
        self._last = None
        self._worker = threading.Thread(target=self._run, name="display-worker", daemon=True)
        self._worker.start()

    # ---- job submission ----------------------------------------------------
    def submit(self, mode, factory, detail=""):
        """Queue a job factory; interrupting any running loop so the newest wins.

        ``factory`` is a zero-argument callable returning a ``job(ctrl, stop)``
        so the same screen can be rebuilt immediately (e.g. after a rotation).
        """
        job = factory()
        self._last = (mode, factory, detail)
        with self._cond:
            self._pending = (mode, detail, job)
            self._stop.set()
            self._cond.notify()
        self._set_state(mode=mode, detail=detail, updated=time.time())

    def rerender(self):
        """Re-submit the most recent screen (used to apply a new rotation)."""
        if self._last is None:
            return False
        mode, factory, detail = self._last
        self.submit(mode, factory, detail)
        return True

    def _run(self):
        while True:
            with self._cond:
                while self._pending is None:
                    self._cond.wait()
                mode, detail, job = self._pending
                self._pending = None
                self._stop.clear()
            try:
                job(self, self._stop)
            except Exception:
                traceback.print_exc()
                try:
                    self.disp.show(render_message("Blad", "Spojrz w log"))
                except Exception:
                    pass

    def stop_requested(self):
        return self._stop.is_set()

    def sleep(self, seconds):
        """Sleep in small slices, returning False if interrupted by a new job."""
        end = time.time() + seconds
        while time.time() < end:
            if self._stop.is_set():
                return False
            time.sleep(min(0.2, max(0.0, end - time.time())))
        return True

    # ---- state -------------------------------------------------------------
    def _set_state(self, **kw):
        with self._state_lock:
            self._state.update(kw)

    def state(self):
        with self._state_lock:
            return dict(self._state)


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------
def make_text_job(text, bg, fg, font_size):
    def job(ctrl, stop):
        ctrl.disp.show(render_text(text, bg, fg, font_size))
        ctrl._set_state(mode="text", detail=text[:40], updated=time.time())
    return job


def make_media_job(frames, durations):
    def job(ctrl, stop):
        if len(frames) == 1:
            ctrl.disp.show(compose_frame(frames[0]))
            ctrl._set_state(mode="image", updated=time.time())
            return
        index = 0
        while not ctrl.stop_requested():
            ctrl.disp.show(compose_frame(frames[index % len(frames)]))
            frame_s = durations[index % len(durations)] / 1000.0
            index += 1
            if not ctrl.sleep(max(0.02, frame_s)):
                break
    return job


def make_stats_job(interval):
    def job(ctrl, stop):
        ctrl._set_state(mode="stats", updated=time.time())
        while not ctrl.stop_requested():
            ctrl.disp.show(render_stats())
            if not ctrl.sleep(interval):
                break
    return job


def make_weather_job(interval):
    def job(ctrl, stop):
        ctrl._set_state(mode="weather", updated=time.time())
        data = None
        while not ctrl.stop_requested():
            try:
                data = fetch_weather()
                ctrl._set_state(weather=data, updated=time.time(), detail=WEATHER_CITY)
            except Exception as exc:
                print("[weather] blad: %s" % exc)
            if data is None:
                ctrl.disp.show(render_message("Pogoda", "blad sieci"))
            else:
                ctrl.disp.show(render_weather(data))
            if not ctrl.sleep(interval):
                break
    return job


def make_clear_job():
    def job(ctrl, stop):
        ctrl.disp.clear((0, 0, 0))
        ctrl._set_state(mode="cleared", updated=time.time())
    return job


def make_idle_job():
    def job(ctrl, stop):
        ctrl.disp.show(render_idle())
        ctrl._set_state(mode="idle", updated=time.time())
    return job


# --------------------------------------------------------------------------
# Procedural CMatrix screensaver (live falling ASCII "digital rain")
# --------------------------------------------------------------------------
def run_cmatrix_loop(ctrl, duration_seconds=None):
    """Render an authentic, live procedural CMatrix rain to the LCD panel.

    Each column has its own drop position, fall speed and tail length. The
    leading glyph is drawn bright white-green, the next few are vivid green
    and the rest fade out along the tail, so the result looks like the real
    terminal screensaver in motion. When ``duration_seconds`` is given the
    loop stops after that time, otherwise it runs until ``stop_requested``.
    """
    w, h = logical_size()
    col_width = 8
    num_cols = max(1, w // col_width)

    drops = [random.randint(-20, 0) for _ in range(num_cols)]
    speeds = [random.uniform(1.2, 2.5) for _ in range(num_cols)]
    lengths = [random.randint(6, 14) for _ in range(num_cols)]

    start_t = time.time()
    font = load_font(10, bold=False) or ImageFont.load_default()

    while not ctrl.stop_requested():
        if duration_seconds and (time.time() - start_t >= duration_seconds):
            break

        im = Image.new("RGB", (w, h), (0, 0, 0))
        draw = ImageDraw.Draw(im)

        for c in range(num_cols):
            x = c * col_width + 1
            y = int(drops[c])
            length = lengths[c]

            for i in range(length):
                char_y = y - (i * 10)
                if 0 <= char_y < h:
                    char = chr(random.randint(33, 126))
                    if i == 0:
                        color = (200, 255, 200)
                    elif i < 3:
                        color = (0, 255, 65)
                    else:
                        fade = max(20, int(200 - (i / length) * 180))
                        color = (0, fade, 20)
                    draw.text((x, char_y), char, font=font, fill=color)

            drops[c] += speeds[c]
            if drops[c] - (length * 10) > h:
                drops[c] = random.randint(-15, 0)
                speeds[c] = random.uniform(1.2, 2.5)
                lengths[c] = random.randint(6, 14)

        ctrl.disp.show(im)
        ctrl.sleep(0.04)

    return True


def make_cmatrix_job(duration_seconds=None, detail="Screensaver"):
    def job(ctrl, stop):
        ctrl._set_state(mode="cmatrix", detail=detail, updated=time.time())
        run_cmatrix_loop(ctrl, duration_seconds=duration_seconds)
    return job


# --------------------------------------------------------------------------
# DEMO mode (autoplay slideshow)
# --------------------------------------------------------------------------
def _hold_screen(ctrl, render_fn, seconds, refresh=1.0):
    """Show render_fn() repeatedly for `seconds` (so clocks keep ticking)."""
    end = time.time() + seconds
    while not ctrl.stop_requested() and time.time() < end:
        ctrl.disp.show(render_fn())
        if not ctrl.sleep(min(refresh, max(0.02, end - time.time()))):
            return False
    return True


def _play_gif_slide(ctrl, name, seconds):
    """Animate a bundled GIF for roughly `seconds`, honouring stop requests."""
    try:
        frames, durations = load_preset_frames(name)
    except Exception as exc:
        print("[demo] preset %s: %s" % (name, exc))
        ctrl.disp.show(render_message(name.upper(), "brak GIF"))
        return ctrl.sleep(seconds)
    if len(frames) == 1:
        ctrl.disp.show(compose_frame(frames[0]))
        return ctrl.sleep(seconds)
    end = time.time() + seconds
    index = 0
    while not ctrl.stop_requested() and time.time() < end:
        ctrl.disp.show(compose_frame(frames[index % len(frames)]))
        frame_s = max(0.02, durations[index % len(durations)] / 1000.0)
        index += 1
        if not ctrl.sleep(frame_s):
            return False
    return True


def _weather_slide_image():
    data = get_cached_weather()
    if data is None:
        return render_message("Pogoda", "brak danych")
    return render_weather(data)


def make_demo_job():
    """Infinite autoplay loop: stats -> weather -> Mario -> CMatrix -> Coin."""
    def job(ctrl, stop):
        ctrl._set_state(mode="demo", detail="autoplay", updated=time.time())
        while not ctrl.stop_requested():
            ctrl._set_state(detail="zegar + CPU", updated=time.time())
            if not _hold_screen(ctrl, render_stats, DEMO_SLIDE_SECONDS):
                break

            ctrl._set_state(detail="pogoda " + WEATHER_CITY, updated=time.time())
            if not _hold_screen(ctrl, _weather_slide_image, DEMO_SLIDE_SECONDS,
                                refresh=5.0):
                break

            ctrl._set_state(detail="GIF mario", updated=time.time())
            if not _play_gif_slide(ctrl, "mario", DEMO_GIF_SECONDS.get("mario", 6.0)):
                break

            # Live procedural CMatrix screensaver for 8 seconds in the cycle
            ctrl._set_state(detail="CMatrix Screensaver", updated=time.time())
            run_cmatrix_loop(ctrl, duration_seconds=8)
            if ctrl.stop_requested():
                break

            ctrl._set_state(detail="GIF coin", updated=time.time())
            if not _play_gif_slide(ctrl, "coin", DEMO_GIF_SECONDS.get("coin", 5.0)):
                break
    return job


# --------------------------------------------------------------------------
# Flask application
# --------------------------------------------------------------------------
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

display = None
controller = None
START_TIME = time.time()


def dual(rule, methods=("GET",)):
    """Register a route under /api/*, /lcd-api/* and bare /*.

    The bare route keeps the service working whether nginx proxies with a
    trailing slash (strips /lcd-api/) or without one (keeps it).
    """
    def deco(func):
        app.add_url_rule("/api" + rule, endpoint=func.__name__ + "_api",
                         view_func=func, methods=list(methods))
        app.add_url_rule("/lcd-api" + rule, endpoint=func.__name__ + "_lcd",
                         view_func=func, methods=list(methods))
        app.add_url_rule(rule, endpoint=func.__name__ + "_root",
                         view_func=func, methods=list(methods))
        return func
    return deco


def display_ready():
    return display is not None and controller is not None


@dual("/text", methods=["POST"])
def api_text():
    if not display_ready():
        return jsonify(ok=False, error="Ekran niedostepny"), 503
    data = request.get_json(silent=True) or request.form or {}
    text = str(data.get("text", ""))
    bg = hex_to_rgb(data.get("bg_color"), (0, 0, 0))
    fg = hex_to_rgb(data.get("text_color"), (255, 255, 255))
    try:
        font_size = int(data.get("font_size") or 0)
    except (TypeError, ValueError):
        font_size = 0
    controller.submit("text", lambda: make_text_job(text, bg, fg, font_size), text[:40])
    return jsonify(ok=True, mode="text")


@dual("/upload", methods=["POST"])
def api_upload():
    if not display_ready():
        return jsonify(ok=False, error="Ekran niedostepny"), 503
    file = request.files.get("file")
    if file is None or not file.filename:
        return jsonify(ok=False, error="Brak pliku"), 400
    raw = file.read()
    if not raw:
        return jsonify(ok=False, error="Pusty plik"), 400
    try:
        frames, durations = prepare_media(raw)
    except Exception as exc:
        return jsonify(ok=False, error="Nieobslugiwany plik: %s" % exc), 400
    controller.submit("media", lambda: make_media_job(frames, durations),
                      "%s (%d kl.)" % (file.filename, len(frames)))
    return jsonify(ok=True, mode="media", frames=len(frames))


@dual("/mode/stats", methods=["POST"])
def api_mode_stats():
    if not display_ready():
        return jsonify(ok=False, error="Ekran niedostepny"), 503
    try:
        interval = float((request.get_json(silent=True) or {}).get("interval",
                                                                 STATS_REFRESH_SECONDS))
    except (TypeError, ValueError):
        interval = STATS_REFRESH_SECONDS
    interval = max(1.0, min(interval, 60.0))
    controller.submit("stats", lambda: make_stats_job(interval), "zegar + CPU")
    return jsonify(ok=True, mode="stats", interval=interval)


@dual("/mode/weather", methods=["POST"])
def api_mode_weather():
    if not display_ready():
        return jsonify(ok=False, error="Ekran niedostepny"), 503
    controller.submit("weather", lambda: make_weather_job(WEATHER_REFRESH_SECONDS), WEATHER_CITY)
    return jsonify(ok=True, mode="weather", city=WEATHER_CITY)


@dual("/mode/demo", methods=["POST"])
def api_mode_demo():
    if not display_ready():
        return jsonify(ok=False, error="Ekran niedostepny"), 503
    controller.submit("demo", lambda: make_demo_job(), "autoplay")
    return jsonify(ok=True, mode="demo")


@dual("/mode/cmatrix", methods=["POST"])
def api_mode_cmatrix():
    if not display_ready():
        return jsonify(ok=False, error="Ekran niedostepny"), 503
    controller.submit("cmatrix", lambda: make_cmatrix_job(), "Screensaver")
    return jsonify({"ok": True, "mode": "cmatrix"})


@dual("/play-preset", methods=["POST"])
def api_play_preset():
    if not display_ready():
        return jsonify(ok=False, error="Ekran niedostepny"), 503
    data = request.get_json(silent=True) or request.form or {}
    name = str(data.get("name", "")).strip().lower()
    try:
        frames, durations = load_preset_frames(name)
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 400
    controller.submit("media", lambda: make_media_job(frames, durations), "GIF: " + name)
    return jsonify(ok=True, mode="media", preset=name, frames=len(frames))


@dual("/gifs/<path:filename>", methods=["GET"])
def api_gif_asset(filename):
    return send_from_directory(GIF_DIR, filename)


@dual("/backlight", methods=["POST"])
def api_backlight():
    if not display_ready():
        return jsonify(ok=False, error="Ekran niedostepny"), 503
    data = request.get_json(silent=True) or {}
    on = bool(data.get("on", True))
    display.backlight(on=on)
    controller._set_state(backlight=on, updated=time.time())
    return jsonify(ok=True, backlight=on)


@dual("/rotate", methods=["POST"])
def api_rotate():
    if not display_ready():
        return jsonify(ok=False, error="Ekran niedostepny"), 503
    data = request.get_json(silent=True) or {}
    try:
        rotation = set_rotation(data.get("rotation", DEFAULT_ROTATION))
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    controller._set_state(rotation=rotation, updated=time.time())
    controller.rerender()
    return jsonify(ok=True, rotation=get_rotation())


@dual("/clear", methods=["POST"])
def api_clear():
    if not display_ready():
        return jsonify(ok=False, error="Ekran niedostepny"), 503
    controller.submit("clear", lambda: make_clear_job(), "czyszczenie")
    return jsonify(ok=True)


@dual("/stop", methods=["POST"])
def api_stop():
    if not display_ready():
        return jsonify(ok=False, error="Ekran niedostepny"), 503
    controller.submit("idle", lambda: make_idle_job(), "tryb bezczynny")
    return jsonify(ok=True, mode="idle")


@dual("/status", methods=["GET"])
def api_status():
    state = controller.state() if controller else {"mode": "unavailable"}
    state = dict(state)
    state["ok"] = display_ready()
    state["uptime"] = round(time.time() - START_TIME, 1)
    state["width"] = WIDTH
    state["height"] = HEIGHT
    state["location"] = WEATHER_CITY
    state["rotation"] = get_rotation()
    state["brightness"] = current_brightness
    return jsonify(state)


@dual("/preview.png", methods=["GET"])
def api_preview():
    """Return the last frame drawn on the panel as a PNG snapshot."""
    global latest_frame_png
    with frame_lock:
        data = latest_frame_png
    if not data:
        blank = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
        b = io.BytesIO()
        blank.save(b, format="PNG")
        data = b.getvalue()

    resp = Response(data, mimetype="image/png")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@dual("/brightness", methods=["POST"])
def api_brightness():
    data = request.get_json(silent=True) or {}
    val = data.get("value", 100)
    set_lcd_brightness(val)
    return jsonify({"ok": True, "brightness": current_brightness})


@app.route("/")
@app.route("/lcd/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


def main():
    global display, controller
    print("[+] Uruchamianie display_service.py (128x160, offsets %d/%d)..." % (X_OFFSET, Y_OFFSET))
    try:
        display = ST7735S()
        display.init()
        display.backlight(True)
        display.clear((0, 0, 0))
        print("[+] ST7735S gotowy. Podswietlenie ON.")
    except Exception as exc:
        display = None
        print("[!] Inicjalizacja ekranu nieudana: %s" % exc)
        traceback.print_exc()

    controller = DisplayController(display)
    if display is not None:
        controller.submit("idle", lambda: make_idle_job(), "start")

    print("[+] API: http://%s:%d/  (nginx: /lcd-api/)" % (HOST, PORT))
    app.run(host=HOST, port=PORT, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[+] Zatrzymano.")
    finally:
        if display is not None:
            try:
                display.clear((0, 0, 0))
                display.close()
            except Exception:
                pass
            print("[+] SPI/GPIO zwolnione.")
