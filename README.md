# Raspberry Pi ST7735S 1.8" LCD Control Hub

A high-performance, real-time control system and web dashboard for the **ST7735S 1.8" SPI TFT LCD (128x160 px)** powered by **Raspberry Pi 4B**.

Combines low-level hardware SPI acceleration with a modern asynchronous web interface, live rendering preview, animated GIF playback, hardware system telemetry, weather data, and a custom procedural CMatrix digital rain screensaver.

---

## 📺 Demo Video

> **Watch the live demonstration below:**
>
https://github.com/djmcg/rpi-st7735s-lcd-hub/issues/1#issue-5594425085

---

## ✨ Features

- **Hardware Accelerated SPI:** Direct communication via hardware SPI interface (`spidev`) for stable, low-latency display updates.
- **Modern Responsive Web UI:** Built with Tailwind CSS, supporting desktop and mobile viewports with full dark mode support.
- **Real-Time Live Canvas Preview:** Mirror the exact physical display output directly on the web interface in real time.
- **Hardware Telemetry Dashboard:** Live monitoring of Raspberry Pi CPU usage, RAM utilization, operating temperature, and IP addresses.
- **Procedural CMatrix Screensaver:** Custom optimized digital rain animation achieving smooth ~25 FPS rendering.
- **Animated GIF Engine:** Built-in loader and frame processor for animated graphics (includes preloaded animations like Mario and Matrix).
- **Full Display Control:** Dynamic control over display rotation (0°, 90°, 180°, 270°), backlight brightness, and contrast.
- **Weather Widget Integration:** Live local weather data polling and rendering.
- **Bilingual Interface:** Instant runtime switching between English and Polish (PL / EN).
- **Production-Ready Daemon:** Integrated `systemd` service configuration for fully automated background startup on boot.

---

## 📌 Hardware Pinout Connection

Connect the ST7735S display to the Raspberry Pi 4B GPIO header according to the following wiring table:

| ST7735S Pin | Raspberry Pi Pin | Physical Header Pin | Function |
| :--- | :--- | :--- | :--- |
| **VCC** | 3.3V / 5V | Pin 1 / Pin 2 | Power Input |
| **GND** | Ground | Pin 6 / Pin 9 | Ground |
| **CS** | GPIO 8 (CE0) | Pin 24 | SPI Chip Select |
| **RESET** | GPIO 25 | Pin 22 | Hardware Reset |
| **A0 / DC** | GPIO 24 | Pin 18 | Data / Command Control |
| **SDA / MOSI**| GPIO 10 (MOSI)| Pin 19 | SPI Data Line |
| **SCK / SCL** | GPIO 11 (SCLK)| Pin 23 | SPI Clock Line |
| **LED / BL** | GPIO 18 (PWM) / 3.3V | Pin 12 / Pin 1 | Backlight Control |

> **Note:** Ensure hardware SPI is enabled on your Raspberry Pi via `sudo raspi-config` -> **Interface Options** -> **SPI** -> **Enable**.

---

## 🏗️ Architecture & Technology Stack

- **Backend Daemon:** Python 3, `spidev`, `Pillow` (PIL) for graphics and font rendering, `psutil` for hardware system metrics.
- **Frontend Dashboard:** HTML5, Tailwind CSS, Vanilla JavaScript (Fetch API & WebSockets / Canvas API).
- **Service Management:** Linux `systemd` supervisor unit (`lcd-display.service`).

---

## 🚀 Installation & Setup

### 1. Clone the Repository
```bash
git clone https://github.com/djmcg/rpi-st7735s-lcd-hub.git
cd rpi-st7735s-lcd-hub
```

### 2. Prepare Virtual Environment & Dependencies

Install the required system packages first (needed for `python3-venv` and to build the `spidev` module):
```bash
sudo apt update && sudo apt install -y python3-venv python3-dev libspi-dev gcc
```

Then create the virtual environment and install the Python dependencies:
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Run Manually for Testing
```bash
bash run.sh
```

---

## ⚙️ Running as a System Service (Autostart)

To enable automatic background execution on system boot:

**Copy the service unit file to the systemd directory:**
```bash
sudo cp lcd-display.service /etc/systemd/system/
```

**Reload the systemd daemon and enable the service:**
```bash
sudo systemctl daemon-reload
sudo systemctl enable lcd-display.service
sudo systemctl start lcd-display.service
```

**Check runtime status:**
```bash
sudo systemctl status lcd-display.service
```

---

## 📄 License

This project is open-source and available under the MIT License.
