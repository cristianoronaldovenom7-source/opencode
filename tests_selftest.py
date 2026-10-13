"""Unleashed-Pi self-test — verifies the whole stack without hardware.

Runs on any machine (Windows/Mac/Linux, no Pi required): crypto vectors,
protocol round-trips through the mock GPIO, presets, .sub files, and the
brute-force generators. Used by `unleashed_pi.py selftest` and mirrored
by the pytest suite in tests/.
"""

from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from lib import keeloq_crypto as kq
from lib.bruteforce import frames_came, frames_keeloq_counters, frames_nice
from lib.cc1101 import CC1101, MockSPI
from lib.clone_state import CloneState
from lib.gpio_backend import MockBackend
from lib.keeloq_tx import clone_next_subs, predicted_counters
from lib.keystore import Keystore
from lib.presets import (AM270, AM650, FM476, custom_preset_from_bytes,
                          preset_by_name)
from lib.protocols import (CameEncoder, KeeLoqDecoder, KeeLoqEncoder,
                           NiceFloEncoder, PrincetonDecoder, PrincetonEncoder)
from lib.protocols.keeloq import next_counter
from lib.rx_engine import RxEngine
from lib.subfile import SubFile, raw_durations_to_levels
from lib.tx_engine import TxEngine
from lib.util import (frequency_to_registers, registers_to_frequency,
                      reverse_key)

DEMO_MF = "Unleashed-Pi Demo"
DEMO_KEY = 0x1122334455667788


class _Check:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.failures = []

    def run(self, name, fn):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            self.failed += 1
            self.failures.append((name, f"{type(exc).__name__}: {exc}"))
            print(f"  [{_RED}FAIL{_END}] {name}  ->  {type(exc).__name__}: {exc}")
        else:
            self.passed += 1
            print(f"  [{_GRN} ok {_END}] {name}")

    @property
    def ok(self):
        return self.failed == 0


_GRN = "\033[32m"
_RED = "\033[31m"
_END = "\033[0m"


# ---------------------------------------------------------------------------
# individual checks
# ---------------------------------------------------------------------------

def check_keeloq_crypto():
    key = 0x5CE7B6A6AC2A54B3
    for data in (0x00000000, 0x111F1234, 0xFFFFFFFF, 0x0BADF00D):
        enc = kq.keeloq_encrypt(data, key)
        assert enc != data
        assert kq.keeloq_decrypt(enc, key) == data, f"round trip failed for {data:#x}"
    # learning derivations are stable & 64-bit
    man = kq.normal_learning(0x12C5B1F, DEMO_KEY)
    assert 0 <= man <= 0xFFFFFFFFFFFFFFFF
    man2 = kq.secure_learning(0x12C5B1F, 0xDEADBEEF, DEMO_KEY)
    assert 0 <= man2 <= 0xFFFFFFFFFFFFFFFF
    assert man != man2
    # split/join frame helpers invert each other
    frame = kq.join_frame(0x12C5B1F, 0x89ABCDEF)
    fix, hop = kq.split_frame(frame)
    assert (fix, hop) == (0x12C5B1F, 0x89ABCDEF)


def check_counters():
    # mode 0 = standard += counter_mult (firmware default mult = 1)
    assert next_counter(0x0000, 0) == 1
    assert next_counter(0x0001, 0) == 2
    # OFEX (mult = -0x7FFFFFFF): 0000/0001/FFFE/FFFF pattern
    assert next_counter(0x0000, 0, counter_mult=-0x7FFFFFFF) == 1
    assert next_counter(0x0001, 0, counter_mult=-0x7FFFFFFF) == 0xFFFE
    assert next_counter(0xFFFE, 0, counter_mult=-0x7FFFFFFF) == 0xFFFF
    # mode 1 = same pattern
    assert next_counter(0x0001, 1) == 0xFFFE
    assert next_counter(0xFFFE, 1) == 0xFFFF
    # mode 2 = +0x3333
    assert next_counter(0x1234, 2) == 0x4567
    assert next_counter(0xE000, 2) == 0x0000  # wraps to 0 (firmware rule)
    # mode 3 / 4 / 5 / 6
    assert next_counter(0x1234, 6) == 0x1234  # freeze
    assert next_counter(0x0006, 3) == 0x0007
    assert next_counter(0x8007, 3) == 0x0006
    assert next_counter(0x007B, 4) == 0x007C
    assert next_counter(0x807C, 4) == 0x007B
    assert next_counter(0x0000, 5) == 0xFFFF


def check_frequency_math():
    # firmware-exact truncation: value = freq * 0x10000 / 26_000_000
    assert frequency_to_registers(433_920_000) == (0x10, 0xB0, 0x71)
    real = registers_to_frequency(0x10, 0xB0, 0x71)
    assert abs(real - 433_920_000) < 1500, real
    assert frequency_to_registers(315_000_000) == (0x0C, 0x1D, 0x89)
    for freq in (300_000_000, 390_000_000, 418_000_000, 433_420_000, 434_775_000,
                868_350_000, 915_000_000):
        f2, f1, f0 = frequency_to_registers(freq)
        real = registers_to_frequency(f2, f1, f0)
        assert abs(real - freq) < 400, (freq, real)


def check_presets():
    reg = dict(AM650.registers)
    assert reg[0x02] == 0x0D          # IOCFG0 async serial data
    assert reg[0x10] == 0x17          # MDMCFG4 -> 650 kHz RX BW
    assert reg[0x12] == 0x30          # MDMCFG2 -> ASK/OOK
    assert AM650.patable[1] == 0xC0   # 12 dBm OOK power
    reg270 = dict(AM270.registers)
    assert reg270[0x10] == 0x67       # 270 kHz RX BW
    reg476 = dict(FM476.registers)
    assert reg476[0x15] == 0x47       # DEVIATN 47.6 kHz (Kia/Hyundai)
    assert reg476[0x12] == 0x04       # 2-FSK
    assert FM476.patable[0] == 0xC0
    assert preset_by_name("FM476") is FM476
    assert preset_by_name("FuriHalSubGhzPresetOok650Async") is AM650
    # custom preset parsing: reg/val pairs until 00 00, then 8 patable bytes
    blob = [0x02, 0x0D, 0x10, 0x17, 0x00, 0x00, 0xC0, 0, 0, 0, 0, 0, 0, 0]
    custom = custom_preset_from_bytes(blob)
    assert dict(custom.registers) == {0x02: 0x0D, 0x10: 0x17}
    assert custom.patable[0] == 0xC0


def _rx_with(keystore=None):
    mock = MockBackend()
    radio = CC1101(spi=MockSPI())
    rx = RxEngine(radio, mock, 27, keystore=keystore)
    rx.start(AM650, 433_920_000)
    return mock, rx


def check_princeton_roundtrip():
    enc = PrincetonEncoder(data=0x1A2B3C, data_count_bit=24, te=390, guard_time=30)
    upload = enc.build_upload()
    mock, rx = _rx_with()
    found = []
    rx.on_found(found.append)
    # frame 1's guard gives the decoder sync, frames 2+3 satisfy the
    # "two identical consecutive frames" rule (real remotes repeat ~20x)
    mock.feed_levels(27, upload * 3)
    rx.stop()
    assert len(found) == 1, f"expected 1 decode, got {len(found)}"
    r = found[0]
    assert r.protocol_name == "Princeton"
    assert r.data == 0x1A2B3C
    assert r.data_count_bit == 24
    assert abs(r.te - 390) <= 2, r.te
    serial, btn = (r.data >> 4), r.data & 0xF
    assert serial == 0x1A2B3 and btn == 0xC


def check_came_roundtrip():
    enc = CameEncoder(data=0x6A3, data_count_bit=12)
    upload = enc.build_upload()
    mock, rx = _rx_with()
    found = []
    rx.on_found(found.append)
    mock.feed_levels(27, upload * 3)
    rx.stop()
    assert found, "CAME frame not decoded"
    assert found[-1].protocol_name == "CAME"
    assert found[-1].data == 0x6A3
    assert found[-1].data_count_bit == 12


def check_nice_roundtrip():
    enc = NiceFloEncoder(data=0x345, data_count_bit=12)
    upload = enc.build_upload()
    mock, rx = _rx_with()
    found = []
    rx.on_found(found.append)
    mock.feed_levels(27, upload * 3)
    rx.stop()
    assert found, "Nice FLO frame not decoded"
    assert found[-1].protocol_name == "Nice FLO"
    assert found[-1].data == 0x345


def _demo_keystore():
    ks = Keystore()
    ks._entries[DEMO_MF] = type("M", (), {"name": DEMO_MF, "key": DEMO_KEY,
                                         "type": "simple"})()
    return ks


def check_keeloq_roundtrip():
    ks = _demo_keystore()
    enc = KeeLoqEncoder(keystore=ks)
    enc.set_frame(serial=0x2C5B1F, btn=0x1, cnt=0x1234,
                  manufacture_name=DEMO_MF, counter_mode=6)  # freeze counter
    upload, repeat = enc.get_upload()
    assert repeat == 3
    assert len(upload) > 130

    mock, rx = _rx_with(keystore=ks)
    found = []
    rx.on_found(found.append)
    mock.feed_levels(27, upload)
    rx.stop()

    assert found, "KeeLoq frame not decoded"
    r = found[0]
    assert r.protocol_name == "KeeLoq"
    assert r.manufacture_name == DEMO_MF, r.manufacture_name
    assert r.serial == 0x2C5B1F
    assert r.btn == 0x1
    assert r.cnt == 0x1234, hex(r.cnt)


def check_keeloq_counter_modes():
    ks = _demo_keystore()
    enc = KeeLoqEncoder(keystore=ks)
    enc.set_frame(serial=0x2C5B1F, btn=0x1, cnt=0x1000,
                  manufacture_name=DEMO_MF, counter_mode=2)
    upload1, _ = enc.get_upload()
    d1 = enc.data
    upload2, _ = enc.get_upload()
    d2 = enc.data
    assert d1 != d2, "counter mode 2 must advance the hopping code"
    # verify both decode back with correct counters (+0x3333 steps)
    mock, rx = _rx_with(keystore=ks)
    seen = []
    rx.on_found(seen.append)
    mock.feed_levels(27, upload1 + upload2)
    rx.stop()
    assert [r.cnt for r in seen] == [0x4333, 0x7666], [hex(r.cnt) for r in seen]


def check_subfile_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        # decoded KeeLoq file
        sub = SubFile()
        sub.frequency = 433_920_000
        sub.preset_name = "FuriHalSubGhzPreset2FSKDev476Async"
        sub.protocol = "KeeLoq"
        sub.bit = 64
        sub.key = 0x0123456789ABCDEF
        sub.te = 400
        sub.manufacture = DEMO_MF
        sub.seed = 0xDEADBEEF
        sub.counter_mode = 2
        sub.repeat = 7
        path = os.path.join(tmp, "t.sub")
        sub.save(path)
        back = SubFile.load(path)
        assert back.frequency == sub.frequency
        assert back.preset_name == sub.preset_name
        assert back.preset_short() == "FM476"
        assert back.protocol == "KeeLoq"
        assert back.bit == 64
        assert back.key == 0x0123456789ABCDEF
        assert back.manufacture == DEMO_MF
        assert back.seed == 0xDEADBEEF
        assert back.counter_mode == 2
        assert back.repeat == 7

        # RAW file
        raw = SubFile()
        raw.filetype = "Flipper SubGhz RAW File"
        raw.frequency = 315_000_000
        raw.preset_name = "FuriHalSubGhzPresetOok650Async"
        raw.raw_data = [400, -800, 1200, -400, 30000]
        rawpath = os.path.join(tmp, "r.sub")
        raw.save(rawpath)
        back2 = SubFile.load(rawpath)
        assert back2.is_raw
        assert back2.raw_data == [400, -800, 1200, -400, 30000]
        levels = raw_durations_to_levels(back2.raw_data)
        assert levels == [(True, 400), (False, 800), (True, 1200),
                          (False, 400), (True, 30000)]

        # custom preset round trip
        cust = SubFile()
        cust.preset_name = "FuriHalSubGhzPresetCustom"
        cust.custom_preset_bytes = [0x02, 0x0D, 0x10, 0x17, 0x00, 0x00,
                                    0xC0, 0, 0, 0, 0, 0, 0, 0]
        cust.protocol = "CAME"
        cust.bit = 12
        cust.key = 0x6A3
        cpath = os.path.join(tmp, "c.sub")
        cust.save(cpath)
        back3 = SubFile.load(cpath)
        assert back3.custom_preset_bytes == cust.custom_preset_bytes
        p = back3.preset()
        assert dict(p.registers) == {0x02: 0x0D, 0x10: 0x17}
        assert p.patable[0] == 0xC0


def check_bruteforce_generators():
    codes = list(frames_came(0, 4096))
    assert len(codes) == 4096 and codes[0] == 0 and codes[-1] == 4095
    codes = list(frames_nice(0x100, 16))
    assert codes[0] == 0x100 and len(codes) == 16

    sub = SubFile()
    sub.protocol = "KeeLoq"
    sub.bit = 64
    sub.key = 0x0123456789ABCDEF
    sub.manufacture = "Unknown"
    encs = list(frames_keeloq_counters(sub, None, 4, counter_mode=2))
    assert len(encs) == 4


def check_tx_mock_loopback():
    """End-to-end through TxEngine with the mock GPIO: pulses must land in
    the mock's wave buffer with the exact protocol timing."""
    mock = MockBackend()
    radio = CC1101(spi=MockSPI())
    tx = TxEngine(radio, mock, 27, allow_extended=True)
    enc = PrincetonEncoder(data=0x0BADC0, data_count_bit=24, te=390)
    sent = tx.send(AM650, 433_920_000, enc.build_upload(), repeat=2)
    assert sent == 2
    assert len(mock.sent_waves) == 2
    wave = mock.sent_waves[0]
    # MSB of 0x0BADC0 is 0 -> first pair is (HIGH 1T, LOW 3T)
    assert wave[0] == (True, 390)
    assert wave[1] == (False, 390 * 3)
    total = sum(d for _l, d in wave)
    assert total == 24 * 4 * 390 + 390 + 30 * 390  # bits + stop + guard


# ---------------------------------------------------------------------------

def check_holtek_roundtrip():
    from lib.protocols import HoltekEncoder
    data = 0x5123456789 & 0xFFFFFFFFFF
    enc = HoltekEncoder(data=data, data_count_bit=40)
    upload = enc.build_upload()
    mock, rx = _rx_with()
    found = []
    rx.on_found(found.append)
    mock.feed_levels(27, upload * 3)
    rx.stop()
    assert found, "Holtek frame not decoded"
    assert found[-1].protocol_name == "Holtek"
    assert found[-1].data == data
    assert found[-1].data_count_bit == 40


def check_counter_window_sweep():
    import tempfile
    from lib.bruteforce import BruteForcer
    ks = _demo_keystore()
    sub = SubFile.load(os.path.join(HERE, "assets", "subghz",
                                    "SAMPLE_KeeLoq.sub"))
    mock = MockBackend()
    radio = CC1101(spi=MockSPI())
    tx = TxEngine(radio, mock, 27, allow_extended=True, keystore=ks)
    bf = BruteForcer(tx)
    sent = bf.run_keeloq_sweep(FM476, sub.frequency, sub, ks,
                               counter_from=0x4000, counter_to=0x4004, step=1)
    assert sent == 5, sent
    rx = RxEngine(radio, mock, 27, keystore=ks)
    rx.start(FM476, sub.frequency)
    seen = []
    rx.on_found(seen.append)
    for wave in mock.sent_waves:
        mock.feed_levels(27, wave)
    rx.stop()
    counters = sorted(r.cnt for r in seen)
    assert counters == [0x4000, 0x4001, 0x4002, 0x4003, 0x4004], \
        [hex(c) for c in counters]


def check_shadow_clone():
    import tempfile
    ks = _demo_keystore()
    sub = SubFile.load(os.path.join(HERE, "assets", "subghz",
                                    "SAMPLE_KeeLoq.sub"))
    subs, counters = clone_next_subs(sub, keystore=ks, steps=3, counter_mode=2)
    assert counters == [0x4567, 0x789A, 0xABCD], [hex(c) for c in counters]
    dec = KeeLoqDecoder(keystore=ks)
    for clone, cnt in zip(subs, counters):
        result = dec.make_result_from_data(clone.key)
        assert result.cnt == cnt, (hex(result.cnt), hex(cnt))
        assert result.manufacture_name == DEMO_MF
    mf, chain = predicted_counters(sub, keystore=ks, count=3, counter_mode=2)
    assert chain == [0x4567, 0x789A, 0xABCD]
    with tempfile.TemporaryDirectory() as tmp:
        state = CloneState(os.path.join(tmp, "state.json"))
        assert state.observe(DEMO_MF, 0x2C5B1F, 1, 0x1000) == 0x1000
        assert state.observe(DEMO_MF, 0x2C5B1F, 1, 0x0800) == 0x1000
        state.advance(DEMO_MF, 0x2C5B1F, 0x4567)
        assert CloneState(os.path.join(tmp, "state.json")).get(
            DEMO_MF, 0x2C5B1F)["cnt"] == 0x4567


# ---------------------------------------------------------------------------

def run_selftest(verbose: bool = True) -> bool:
    print("\nUnleashed-Pi self-test")
    print("=" * 60)
    c = _Check()
    c.run("KeeLoq cipher round-trip + learning", check_keeloq_crypto)
    c.run("rolling-counter modes 0-7", check_counters)
    c.run("CC1101 frequency math", check_frequency_math)
    c.run("radio presets (AM650/AM270/FM476/custom)", check_presets)
    c.run("Princeton encode->radio->decode", check_princeton_roundtrip)
    c.run("CAME encode->radio->decode", check_came_roundtrip)
    c.run("Nice FLO encode->radio->decode", check_nice_roundtrip)
    c.run("KeeLoq encode->radio->decode (mf identified)", check_keeloq_roundtrip)
    c.run("KeeLoq counter mode 2 (+0x3333) over the air", check_keeloq_counter_modes)
    c.run("Flipper .sub file round-trips", check_subfile_roundtrip)
    c.run("brute-force generators", check_bruteforce_generators)
    c.run("TX engine mock loopback", check_tx_mock_loopback)
    c.run("Holtek encode->radio->decode", check_holtek_roundtrip)
    c.run("counter-window sweep (rolling-code bypass)", check_counter_window_sweep)
    c.run("shadow clone + next-code prediction", check_shadow_clone)

    print("=" * 60)
    if c.ok:
        print(f"{_GRN}ALL {c.passed} CHECKS PASSED{_END} — stack is healthy.\n")
    else:
        print(f"{_RED}{c.failed} CHECK(S) FAILED{_END}\n")
        for name, why in c.failures:
            print(f"  - {name}: {why}")
        print()
    return c.ok


if __name__ == "__main__":
    sys.exit(0 if run_selftest() else 1)
