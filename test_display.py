#!/usr/bin/env python3
import time
from PIL import Image, ImageDraw, ImageFont
import spidev
from gpiozero import DigitalOutputDevice

WIDTH = 128
HEIGHT = 160

# GPIO pins
DC_PIN = 25
RST_PIN = 27
BL_PIN = 18

# SPI config
SPI_PORT = 0
SPI_DEVICE = 0
SPI_MAX_SPEED_HZ = 4000000

print("[+] Inicjalizacja sterownikow GPIO...")
dc = DigitalOutputDevice(DC_PIN)
rst = DigitalOutputDevice(RST_PIN)
bl = DigitalOutputDevice(BL_PIN)

print("[+] Inicjalizacja SPI...")
spi = spidev.SpiDev()
spi.open(SPI_PORT, SPI_DEVICE)
spi.mode = 0
spi.max_speed_hz = SPI_MAX_SPEED_HZ

def send_command(cmd):
    dc.off()
    spi.xfer2([cmd])

def send_data(data):
    dc.on()
    # Send in chunks to avoid overflow
    chunk_size = 4096
    for i in range(0, len(data), chunk_size):
        spi.xfer2(data[i:i+chunk_size])

print("[+] Reset wyswietlacza...")
rst.off()
time.sleep(0.1)
rst.on()
time.sleep(0.1)

print("[+] Wlaczanie podswietlenia...")
bl.on()

print("[+] Inicjalizacja kontrolera ST7735S...")
send_command(0x01)  # Software reset
time.sleep(0.15)

send_command(0x11)  # Sleep out
time.sleep(0.5)

send_command(0x3A)  # Color mode
send_data([0x05])   # 16-bit color

send_command(0x36)  # Memory access control
send_data([0xC8])   # BGR mode, row/col address order

send_command(0x29)  # Display on
time.sleep(0.1)

print("[+] Rysowanie testowego obrazu...")
image = Image.new('RGB', (WIDTH, HEIGHT), color=(255, 0, 0))
draw = ImageDraw.Draw(image)

try:
    font = ImageFont.truetype(
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 32
    )
except Exception:
    font = ImageFont.load_default()

text = "DZIALA!"
bbox = draw.textbbox((0, 0), text, font=font)
text_width = bbox[2] - bbox[0]
text_height = bbox[3] - bbox[1]
x = (WIDTH - text_width) // 2
y = (HEIGHT - text_height) // 2
draw.text((x, y), text, font=font, fill=(255, 255, 255))

print("[+] Wysylanie danych po SPI...")
# Set window to full display
send_command(0x2A)  # Column set
send_data([0x00, 0x02, 0x00, 0x81])  # XSTART=2, XEND=131

send_command(0x2B)  # Row set
send_data([0x00, 0x01, 0x00, 0xA0])  # YSTART=1, YEND=160

send_command(0x2C)  # Memory write

# Convert image to RGB565 and send
pixels = image.convert('RGB')
pixel_bytes = []
for y in range(HEIGHT):
    for x in range(WIDTH):
        r, g, b = pixels.getpixel((x, y))
        rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        pixel_bytes.append((rgb565 >> 8) & 0xFF)
        pixel_bytes.append(rgb565 & 0xFF)

send_data(pixel_bytes)
print("[+] Dane wyslane.")

print("[+] Wyświetlacz aktywny. Naciśnij Ctrl+C aby zakończyć.")
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("\n[+] Zakończono.")
finally:
    spi.close()
    dc.close()
    rst.close()
    bl.close()
