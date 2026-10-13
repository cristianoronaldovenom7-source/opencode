#!/bin/bash
# ===========================================================================
# Unleashed-Pi installer — Raspberry Pi 3 / 4 / 5 (Raspberry Pi OS, 32/64-bit)
#
#   sudo bash install.sh
#
# What it does:
#   1. installs the runtime packages (python3, spidev, lgpio, pigpio)
#   2. enables the SPI bus (needed for the CC1101)
#   3. enables pigpiod (best TX timing on Pi 3/4; unused on Pi 5)
#   4. runs the self-test
# ===========================================================================
set -e

if [ "$EUID" -ne 0 ]; then
    echo "[-] please run as root:  sudo bash install.sh"
    exit 1
fi

echo "[*] Unleashed-Pi installer for Raspberry Pi 3/4/5"

# --- detect Pi model ---------------------------------------------------------
MODEL=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo "unknown")
echo "[*] board: ${MODEL}"

echo "[*] updating package lists..."
apt-get update -qq

echo "[*] installing packages (python3, spidev, lgpio, pigpio)..."
apt-get install -y -qq python3 python3-spidev python3-lgpio pigpio

# --- SPI ---------------------------------------------------------------------
if command -v raspi-config >/dev/null 2>&1; then
    if ! grep -q "^dtparam=spi=on" /boot/config.txt 2>/dev/null && \
       ! grep -q "^dtparam=spi=on" /boot/firmware/config.txt 2>/dev/null; then
        echo "[*] enabling SPI ..."
        raspi-config nonint do_spi 0
    else
        echo "[+] SPI already enabled"
    fi
fi

# --- pigpiod (DMA-precise TX waves on Pi 3/4) --------------------------------
if systemctl list-unit-files 2>/dev/null | grep -q "^pigpiod.service"; then
    systemctl enable pigpiod >/dev/null 2>&1 || true
    systemctl start  pigpiod >/dev/null 2>&1 || true
    echo "[+] pigpiod enabled (used on Pi 3/4 for DMA-timed TX)"
else
    echo "[!] pigpiod not present (fine on Pi 5 - lgpio is used)"
fi

# --- run self-test -----------------------------------------------------------
cd "$(dirname "$0")"
echo
echo "[*] running self-test ..."
if python3 unleashed_pi.py selftest; then
    echo
    echo "[+] INSTALLATION COMPLETE"
    echo
    echo "    wiring (CC1101 module -> Pi header):"
    echo "      VCC  -> 3.3V  (pin 1)   *** never 5V ***"
    echo "      GND  -> GND   (pin 6)"
    echo "      MOSI -> GPIO10 (pin 19)"
    echo "      MISO -> GPIO9  (pin 21)"
    echo "      SCK  -> GPIO11 (pin 23)"
    echo "      CSN  -> GPIO8  (pin 24)"
    echo "      GDO0 -> GPIO27 (pin 13)"
    echo
    echo "    start hacking (test only what you own!):"
    echo "      sudo python3 unleashed_pi.py            # menu"
    echo "      sudo python3 unleashed_pi.py read        # capture car keys"
    echo "      sudo python3 unleashed_pi.py send assets/subghz/SAMPLE_CAME.sub"
    echo
    echo "    If SPI was just enabled, reboot first:  sudo reboot"
else
    echo "[-] self-test failed - please report the output above"
    exit 1
fi
