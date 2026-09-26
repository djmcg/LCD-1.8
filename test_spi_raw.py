#!/usr/bin/env python3
import time
import spidev
import gpiod

# SPI setup
spi = spidev.SpiDev()
spi.open(0, 0)
spi.mode = 0
spi.max_speed_hz = 4000000

# GPIO setup - use full path to gpiochip
chip = gpiod.Chip('/dev/gpiochip0')
dc_line = chip.get_line(25)
rst_line = chip.get_line(27)
bl_line = chip.get_line(18)

dc_line.request(consumer='st7735-dc', type=gpiod.LINE_REQ_DIR_OUT)
rst_line.request(consumer='st7735-rst', type=gpiod.LINE_REQ_DIR_OUT)
bl_line.request(consumer='st7735-bl', type=gpiod.LINE_REQ_DIR_OUT)

def send_command(cmd):
    dc_line.set_value(0)
    spi.xfer2([cmd])

def send_data(data):
    dc_line.set_value(1)
    spi.xfer2(data)

def read_id():
    send_command(0x04)
    dc_line.set_value(1)
    result = spi.xfer2([0x00, 0x00, 0x00])
    return result

# Reset display
print("[+] Reset display...")
rst_line.set_value(1)
time.sleep(0.1)
rst_line.set_value(0)
time.sleep(0.1)
rst_line.set_value(1)
time.sleep(0.1)

# Turn on backlight
print("[+] Backlight ON...")
bl_line.set_value(1)

# Read display ID
print("[+] Reading display ID...")
id_bytes = read_id()
print(f"[+] ID: {id_bytes}")

# Initialize display
print("[+] Initializing display...")
send_command(0x01)  # Software reset
time.sleep(0.15)

send_command(0x11)  # Sleep out
time.sleep(0.5)

send_command(0x3A)  # Color mode
send_data([0x05])   # 16-bit color

send_command(0x36)  # Memory access control
send_data([0xC8])   # BGR mode

send_command(0x29)  # Display on
time.sleep(0.1)

print("[+] Sending test pattern...")
# Send a simple red test pattern (128x160 = 20480 pixels)
# Each pixel is 2 bytes (RGB565)
red_pixel = [0x00, 0xF8]  # Red in RGB565
test_data = red_pixel * (128 * 160)

# Set window to full display
send_command(0x2A)  # Column set
send_data([0x00, 0x02, 0x00, 0x81])  # XSTART=2, XEND=131

send_command(0x2B)  # Row set
send_data([0x00, 0x01, 0x00, 0xA0])  # YSTART=1, YEND=160

send_command(0x2C)  # Memory write
send_data(test_data)

print("[+] Test pattern sent. Display should show red.")

# Keep running
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass

finally:
    spi.close()
    chip.close()
