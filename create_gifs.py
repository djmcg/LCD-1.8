#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate the built-in retro pixel-art GIF library for the 1.8" LCD.

Creates (or refreshes) assets under ``assets/gifs``:

    mario.gif   classic 8-bit running/jumping Mario on NES sky blue
    coin.gif    spinning golden coin on a dark background
    matrix.gif  cascading green digital matrix rain

Run with the project venv:  ./venv/bin/python create_gifs.py
"""

import os
import random

from PIL import Image, ImageDraw

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GIF_DIR = os.path.join(BASE_DIR, "assets", "gifs")
os.makedirs(GIF_DIR, exist_ok=True)


def _save(frames, name, duration):
    path = os.path.join(GIF_DIR, name)
    frames[0].save(path, save_all=True, append_images=frames[1:],
                   duration=duration, loop=0, optimize=False, disposal=2)
    print("[+] %s (%d klatek)" % (path, len(frames)))
    return path


# 1. Generate Mario Run (8-bit style 128x160)
mario_frames = []
for step in range(4):
    im = Image.new("RGB", (128, 160), (92, 148, 252))  # NES sky blue
    d = ImageDraw.Draw(im)
    # Ground + grass line
    d.rectangle([0, 130, 128, 160], fill=(200, 76, 12))
    d.rectangle([0, 128, 128, 130], fill=(0, 168, 0))
    # Brick + floating ? block
    d.rectangle([20, 40, 50, 56], fill=(200, 76, 12), outline=(0, 0, 0))
    d.rectangle([50, 40, 80, 56], fill=(252, 188, 60), outline=(0, 0, 0))
    d.text((62, 44), "?", fill=(0, 0, 0))
    # Mario body (bobs every other frame, legs swap)
    x = 48
    y = 90 - (4 if step % 2 == 1 else 0)
    d.rectangle([x + 6, y, x + 24, y + 6], fill=(248, 56, 0))          # cap
    d.rectangle([x + 10, y + 6, x + 22, y + 12], fill=(252, 160, 68))  # face
    d.rectangle([x + 16, y + 8, x + 24, y + 11], fill=(0, 0, 0))       # mustache
    d.rectangle([x + 4, y + 12, x + 24, y + 24], fill=(0, 68, 252))    # overalls
    d.rectangle([x + 8, y + 12, x + 20, y + 20], fill=(248, 56, 0))    # shirt
    leg_shift = 4 if step in (1, 3) else 0
    d.rectangle([x + 2, y + 24, x + 10 + leg_shift, y + 30], fill=(136, 68, 0))
    d.rectangle([x + 16 - leg_shift, y + 24, x + 26, y + 30], fill=(136, 68, 0))
    d.text((18, 140), "SUPER MARIO", fill=(255, 255, 255))
    mario_frames.append(im)
_save(mario_frames, "mario.gif", 150)


# 2. Generate Coin / spinning bonus animation
coin_frames = []
for f in range(6):
    im = Image.new("RGB", (128, 160), (15, 15, 20))
    d = ImageDraw.Draw(im)
    width = abs(3 - f) * 8 + 4
    cx = 64
    d.ellipse([cx - width, 55, cx + width, 95],
              fill=(252, 216, 0), outline=(200, 140, 0))
    d.text((42, 115), "COIN BONUS", fill=(255, 215, 0))
    d.text((50, 25), "MARIO", fill=(255, 80, 80))
    coin_frames.append(im)
_save(coin_frames, "coin.gif", 120)


# 3. Generate Retro Matrix Rain
matrix_frames = []
drops = [random.randint(0, 160) for _ in range(16)]
for _ in range(12):
    im = Image.new("RGB", (128, 160), (0, 10, 0))
    d = ImageDraw.Draw(im)
    for col in range(16):
        drops[col] = (drops[col] + 12) % 160
        y = drops[col]
        x = col * 8 + 2
        d.text((x, y), chr(random.randint(33, 126)), fill=(180, 255, 180))
        d.text((x, (y - 12) % 160), chr(random.randint(33, 126)), fill=(0, 200, 50))
        d.text((x, (y - 24) % 160), chr(random.randint(33, 126)), fill=(0, 80, 20))
    matrix_frames.append(im)
_save(matrix_frames, "matrix.gif", 100)

print("GIFs generated successfully.")
