"""Test suite for varbit: unit tests, bit-exact differential testing
against refimpl, streaming-vs-block consistency, and size comparison.

Run:  python3 -m unittest -v test_varbit
"""

import random
import unittest

import varbit
from varbit import (
    BitReader,
    BitWriter,
    NeedMoreBits,
    NonCanonicalError,
    VarintOverflow,
    zigzag_decode,
    zigzag_encode,
)
from refimpl import RefWriter, RefReader

U64_MAX = (1 << 64) - 1
I64_MIN = -(1 << 63)
I64_MAX = (1 << 63) - 1


def random_ops(rng, n):
    """Random program of write operations shared by both implementations."""
    ops = []
    for _ in range(n):
        kind = rng.randrange(3)
        if kind == 0:
            w = rng.randrange(1, 65)
            ops.append(("bits", rng.randrange(1 << w) if w < 64 else rng.getrandbits(64), w))
        elif kind == 1:
            u = rng.choice(
                [rng.randrange(128), rng.getrandbits(rng.randrange(1, 65))]
            )
            ops.append(("uvarint", u))
        else:
            nbits = rng.randrange(1, 64)
            v = rng.getrandbits(nbits)
            if rng.randrange(2):
                v = -v - 1
            ops.append(("svarint", v))
    return ops


def apply_ops(writer, ops):
    for op in ops:
        if op[0] == "bits":
            writer.write_bits(op[1], op[2])
        elif op[0] == "uvarint":
            writer.write_uvarint(op[1])
        else:
            writer.write_svarint(op[1])


def read_ops(reader, ops):
    out = []
    for op in ops:
        if op[0] == "bits":
            out.append(reader.read_bits(op[2]))
        elif op[0] == "uvarint":
            out.append(reader.read_uvarint())
        else:
            out.append(reader.read_svarint())
    return out


class TestFixedWidth(unittest.TestCase):
    def test_roundtrip_random(self):
        rng = random.Random(1234)
        for _ in range(200):
            fields = [(rng.getrandbits(w), w) for w in
                      [rng.randrange(1, 65) for _ in range(rng.randrange(1, 20))]]
            w = BitWriter()
            for v, width in fields:
                w.write_bits(v, width)
            r = BitReader(w.tobytes(), 0, w.bit_length)
            for v, width in fields:
                self.assertEqual(r.read_bits(width), v)
            self.assertEqual(r.bits_remaining, 0)

    def test_width_and_value_validation(self):
        w = BitWriter()
        for bad in (0, 65, -1):
            self.assertRaises(ValueError, w.write_bits, 0, bad)
        self.assertRaises(ValueError, w.write_bits, 1 << 10, 10)
        self.assertRaises(ValueError, w.write_bits, -1, 10)

    def test_edge_patterns(self):
        w = BitWriter()
        # runs of all-ones and all-zeros at many widths, incl. 64
        fields = []
        for width in (1, 2, 3, 7, 8, 9, 31, 32, 33, 63, 64):
            fields.append(((1 << width) - 1, width))  # all ones
            fields.append((0, width))                 # all zeros
        for v, width in fields:
            w.write_bits(v, width)
        # long runs of full 0xFF and 0x00 bytes
        w.write_bits(0xFFFFFFFFFFFFFFFF, 64)
        w.write_bits(0, 64)
        r = BitReader(w.tobytes(), 0, w.bit_length)
        for v, width in fields:
            self.assertEqual(r.read_bits(width), v)
        self.assertEqual(r.read_bits(64), 0xFFFFFFFFFFFFFFFF)
        self.assertEqual(r.read_bits(64), 0)

    def test_known_bit_layout(self):
        # MSB-first within byte: 0b1 (1 bit) then 0b101 (3 bits) -> 0b1011_0000
        w = BitWriter()
        w.write_bits(0b1, 1).write_bits(0b101, 3)
        # stream bits: 1,1,0,1 -> byte 0b1101_0000
        self.assertEqual(w.tobytes(), b"\xd0")
        self.assertEqual(w.bit_length, 4)


class TestVarint(unittest.TestCase):
    def test_known_encodings(self):
        cases = {0: b"\x00", 1: b"\x01", 127: b"\x7f",
                 128: b"\x80\x01", 300: b"\xac\x02",
                 U64_MAX: b"\xff" * 9 + b"\x01"}
        for u, enc in cases.items():
            w = BitWriter()
            w.write_uvarint(u)
            self.assertEqual(w.tobytes(), enc, u)
            self.assertEqual(BitReader(enc).read_uvarint(), u)

    def test_shortest_form_enforced(self):
        # value, encoding, redundant bytes
        bad = [
            (0, b"\x80\x00", 1),
            (0, b"\x80\x80\x00", 2),
            (1, b"\x81\x00", 1),
            (127, b"\xff\x00", 1),
            (128, b"\x80\x81\x00", 1),
            (300, b"\xac\x82\x80\x00", 2),
        ]
        for u, enc, extra in bad:
            with self.assertRaises(NonCanonicalError) as cm:
                BitReader(enc).read_uvarint()
            self.assertEqual(cm.exception.extra_bytes, extra, (u, enc))

    def test_roundtrip_boundaries(self):
        vals = [0, 1, 2, 127, 128, 129, 2**14 - 1, 2**14, 2**21,
                2**32 - 1, 2**32, 2**63 - 1, 2**63, U64_MAX]
        for u in vals:
            w = BitWriter()
            w.write_uvarint(u)
            self.assertEqual(BitReader(w.tobytes()).read_uvarint(), u)
            # encoding length must be minimal
            self.assertEqual(len(w.tobytes()), max(1, (u.bit_length() + 6) // 7))

    def test_zigzag_mapping(self):
        self.assertEqual([zigzag_encode(n) for n in (0, -1, 1, -2, 2)],
                         [0, 1, 2, 3, 4])
        self.assertEqual(zigzag_encode(I64_MAX), U64_MAX - 1)
        self.assertEqual(zigzag_encode(I64_MIN), U64_MAX)
        for n in (0, 1, -1, I64_MAX, I64_MIN, 2**40, -(2**40)):
            self.assertEqual(zigzag_decode(zigzag_encode(n)), n)

    def test_svarint_roundtrip(self):
        rng = random.Random(7)
        vals = [0, 1, -1, I64_MAX, I64_MIN] + [
            rng.randrange(I64_MIN, I64_MAX) for _ in range(500)
        ]
        w = BitWriter()
        for v in vals:
            w.write_svarint(v)
        r = BitReader(w.tobytes(), 0, w.bit_length)
        for v in vals:
            self.assertEqual(r.read_svarint(), v)

    def test_varint_not_byte_aligned(self):
        # varints starting at odd bit offsets
        w = BitWriter()
        w.write_bits(0b101, 3)
        w.write_uvarint(300)
        w.write_bits(0b11, 2)
        w.write_svarint(-12345)
        r = BitReader(w.tobytes(), 0, w.bit_length)
        self.assertEqual(r.read_bits(3), 0b101)
        self.assertEqual(r.read_uvarint(), 300)
        self.assertEqual(r.read_bits(2), 0b11)
        self.assertEqual(r.read_svarint(), -12345)

    def test_overlong_prefix(self):
        # short runaway prefix, no terminator yet -> keep asking for bits
        r = BitReader(b"\x80" * 5)
        with self.assertRaises(NeedMoreBits) as cm:
            r.read_uvarint()
        self.assertEqual(cm.exception.needed, 8)
        # runaway continuation bits beyond the 64-bit group limit -> cut off
        r = BitReader(b"\x80" * 200)
        self.assertRaises(VarintOverflow, r.read_uvarint)
        # terminator arrives but payload exceeds 64 bits -> overflow
        r = BitReader(b"\x80" * 10 + b"\x01")  # 11 groups > 10 allowed
        self.assertRaises(VarintOverflow, r.read_uvarint)
        # 10 groups, value 2**64 -> overflow by value
        r = BitReader(b"\x80" * 9 + b"\x02")
        self.assertRaises(VarintOverflow, r.read_uvarint)
        # 10 groups, value 2**63 -> fine
        r = BitReader(b"\x80" * 9 + b"\x01")
        self.assertEqual(r.read_uvarint(), 2**63)
        # larger limit accepts longer payloads
        r = BitReader(b"\xff" * 9 + b"\x03")  # 65-bit payload
        self.assertEqual(r.read_uvarint(max_bits=65), (1 << 65) - 1)

    def test_need_more_bits_count(self):
        w = BitWriter()
        w.write_bits(0, 10)  # 10 bits available
        r = BitReader(w.tobytes(), 0, 10)
        with self.assertRaises(NeedMoreBits) as cm:
            r.read_bits(64)
        self.assertEqual(cm.exception.needed, 54)
        self.assertEqual(r.bits_remaining, 10)  # cursor untouched
        # partial varint group: 5 bits fed -> 3 more needed
        r = BitReader(b"\xff", 0, 5)
        with self.assertRaises(NeedMoreBits) as cm:
            r.read_uvarint()
        self.assertEqual(cm.exception.needed, 3)


class TestDifferential(unittest.TestCase):
    """Bit-exact comparison against the independent bit-by-bit reference."""

    def test_every_bit_matches_reference(self):
        rng = random.Random(20260924)
        for trial in range(60):
            ops = random_ops(rng, rng.randrange(1, 40))
            fast, ref = BitWriter(), RefWriter()
            apply_ops(fast, ops)
            apply_ops(ref, ops)
            # identical byte output and identical bit count
            self.assertEqual(fast.tobytes(), ref.to_bytes(), trial)
            self.assertEqual(fast.bit_length, len(ref.bits))
            # compare every single output bit through the real reader
            r = BitReader(fast.tobytes(), 0, fast.bit_length)
            for i in range(len(ref.bits)):
                self.assertEqual(r.read_bits(1), ref.bits[i],
                                 "bit %d of trial %d" % (i, trial))
            # and values decoded by the reference reader match the program
            rr = RefReader(ref.bits)
            for op in ops:
                if op[0] == "bits":
                    self.assertEqual(rr.read_bits(op[2]), op[1])
                elif op[0] == "uvarint":
                    self.assertEqual(rr.read_uvarint(), op[1])
                else:
                    self.assertEqual(rr.read_svarint(), op[1])
            self.assertEqual(rr.remaining, 0)


class TestStreaming(unittest.TestCase):
    """Chunked/bit-cut decoding must equal one-shot decoding exactly."""

    def _decode_with_retries(self, reader, ops):
        out = []
        for op in ops:
            while True:
                try:
                    if op[0] == "bits":
                        out.append(reader.read_bits(op[2]))
                    elif op[0] == "uvarint":
                        out.append(reader.read_uvarint())
                    else:
                        out.append(reader.read_svarint())
                    break
                except NeedMoreBits:
                    return out, False  # caller feeds more, then resumes
        return out, True

    def test_bit_cut_fragmentation(self):
        rng = random.Random(99)
        for trial in range(80):
            ops = random_ops(rng, rng.randrange(1, 30))
            w = BitWriter()
            apply_ops(w, ops)
            data, total = w.tobytes(), w.bit_length

            # one-shot reference result
            expect = read_ops(BitReader(data, 0, total), ops)

            # random bit-level segmentation of the same stream
            cuts = sorted(rng.sample(range(total + 1),
                                     min(total, rng.randrange(1, 12))))
            segs, prev = [], 0
            for c in cuts + [total]:
                segs.append((prev, c - prev))
                prev = c

            r = BitReader()
            got = []
            oi = 0  # next op to decode
            for off, length in segs:
                byte_off = off >> 3
                bit_off = off & 7
                nbytes = (bit_off + length + 7) >> 3
                r.feed(data[byte_off : byte_off + nbytes], bit_off, length)
                # drain whatever is decodable so far
                while oi < len(ops):
                    op = ops[oi]
                    try:
                        if op[0] == "bits":
                            got.append(r.read_bits(op[2]))
                        elif op[0] == "uvarint":
                            got.append(r.read_uvarint())
                        else:
                            got.append(r.read_svarint())
                        oi += 1
                    except NeedMoreBits:
                        break
            self.assertEqual(oi, len(ops), "trial %d: stream incomplete" % trial)
            self.assertEqual(got, [op[1] for op in ops], trial)
            self.assertEqual(got, expect, trial)
            self.assertEqual(r.bits_remaining, 0)

    def test_byte_by_byte_feeding(self):
        rng = random.Random(5)
        ops = random_ops(rng, 200)
        w = BitWriter()
        apply_ops(w, ops)
        data, total = w.tobytes(), w.bit_length
        expect = read_ops(BitReader(data, 0, total), ops)
        r = BitReader()
        for b in data:
            r.feed(bytes([b]))
        self.assertEqual(read_ops(r, ops), expect)


class TestSizeComparison(unittest.TestCase):
    def test_size_vs_fixed64(self):
        rng = random.Random(42)
        dists = {
            "tiny (0..9)": [rng.randrange(10) for _ in range(1000)],
            "u8": [rng.randrange(256) for _ in range(1000)],
            "u32": [rng.getrandbits(32) for _ in range(1000)],
            "u64": [rng.getrandbits(64) for _ in range(1000)],
            "signed small (-50..50)": [rng.randrange(-50, 51) for _ in range(1000)],
        }
        print("\n  %-24s %12s %12s %8s" % ("distribution", "varint(B)", "fixed64(B)", "ratio"))
        for name, vals in dists.items():
            w = BitWriter()
            for v in vals:
                if v < 0:
                    w.write_svarint(v)
                else:
                    w.write_uvarint(v)
            varint_bytes = (w.bit_length + 7) // 8
            fixed = 8 * len(vals)
            print("  %-24s %12d %12d %8.2f" % (name, varint_bytes, fixed,
                                               varint_bytes / fixed))
            # roundtrip through the same stream to keep the test honest
            r = BitReader(w.tobytes(), 0, w.bit_length)
            for v in vals:
                got = r.read_svarint() if v < 0 else r.read_uvarint()
                self.assertEqual(got, v)
        self.assertLess(varint_bytes, fixed)  # last dist must beat fixed64


if __name__ == "__main__":
    unittest.main(verbosity=2)
