"""varbit 自测: 边界值、最短编码拒绝、超长前缀、流式与整块一致性。

运行: python3 test_streaming.py -v
"""
import random
import unittest

import varbit as vb


def encode(op_writer):
    w = vb.BitWriter()
    op_writer(w)
    return w.to_bytes(), w.bit_length


class EdgeValueTest(unittest.TestCase):
    def roundtrip_u(self, value):
        data, _ = encode(lambda w: w.write_uvarint(value))
        self.assertEqual(vb.BitReader(data).read_uvarint(), value)

    def roundtrip_s(self, value):
        data, _ = encode(lambda w: w.write_svarint(value))
        self.assertEqual(vb.BitReader(data).read_svarint(), value)

    def test_unsigned_edges(self):
        for v in (0, 1, 2, 127, 128, 300, 16383, 16384,
                  2**31 - 1, 2**31, 2**32, 2**63 - 1, 2**63, vb.UINT64_MAX):
            self.roundtrip_u(v)

    def test_signed_edges(self):
        for n in (0, 1, -1, 2, -2, 63, 64, -64, -65,
                  vb.INT64_MAX, vb.INT64_MIN, vb.INT64_MIN + 1):
            self.roundtrip_s(n)

    def test_zigzag_mapping(self):
        known = {0: 0, -1: 1, 1: 2, -2: 3, 2: 4,
                 vb.INT64_MAX: 2**64 - 2, vb.INT64_MIN: 2**64 - 1}
        for n, u in known.items():
            self.assertEqual(vb.zigzag_encode(n), u)
            self.assertEqual(vb.zigzag_decode(u), n)

    def test_out_of_range_rejected(self):
        w = vb.BitWriter()
        with self.assertRaises(ValueError):
            w.write_uvarint(2**64)
        with self.assertRaises(ValueError):
            w.write_uvarint(-1)
        with self.assertRaises(ValueError):
            w.write_svarint(2**63)
        with self.assertRaises(ValueError):
            w.write_svarint(-2**63 - 1)


class BitPackingTest(unittest.TestCase):
    def test_all_widths(self):
        for width in range(1, 65):
            for value in (0, 1, (1 << width) - 1,
                          int("10" * (width // 2) + "1" * (width % 2), 2)):
                data, nbits = encode(lambda w: w.write_bits(value, width))
                self.assertEqual(nbits, width)
                self.assertEqual(len(data), (width + 7) // 8)
                self.assertEqual(vb.BitReader(data).read_bits(width), value)

    def test_runs_of_ones_and_zeros(self):
        w = vb.BitWriter()
        for _ in range(64):
            w.write_bits(1, 1)
        for _ in range(64):
            w.write_bits(0, 1)
        w.write_bits(vb.UINT64_MAX, 64)
        w.write_bits(0, 64)
        r = vb.BitReader(w.to_bytes())
        for _ in range(64):
            self.assertEqual(r.read_bits(1), 1)
        for _ in range(64):
            self.assertEqual(r.read_bits(1), 0)
        self.assertEqual(r.read_bits(64), vb.UINT64_MAX)
        self.assertEqual(r.read_bits(64), 0)

    def test_known_bit_layout(self):
        # MSB first: 3 位 0b101 后接 5 位 0b00011 -> 0b10100011 = 0xA3
        data, _ = encode(lambda w: w.write_bits(0b101, 3).write_bits(0b00011, 5))
        self.assertEqual(data, b"\xA3")


class ShortestEncodingTest(unittest.TestCase):
    def expect_nonshortest(self, data, extra):
        with self.assertRaises(vb.NonShortestEncodingError) as ctx:
            vb.BitReader(data).read_uvarint()
        self.assertEqual(ctx.exception.extra_bytes, extra)

    def test_nonshortest_rejected(self):
        self.expect_nonshortest(b"\x80\x00", 1)
        self.expect_nonshortest(b"\x85\x00", 1)
        self.expect_nonshortest(b"\x80\x80\x80\x00", 3)
        self.expect_nonshortest(b"\xff\xff\x00", 1)

    def test_overlong_prefix(self):
        # 9 个续位字节 + 终止 0: 10 字节编码 0, 多余 9 字节
        self.expect_nonshortest(b"\x80" * 9 + b"\x00", 9)
        # 第 10 字节仍带续位: 超过 64 位
        with self.assertRaises(vb.VarintOverflowError):
            vb.BitReader(b"\x80" * 10 + b"\x00").read_uvarint()
        # 第 10 字节有效载荷超过 1 位: 超过 64 位
        with self.assertRaises(vb.VarintOverflowError):
            vb.BitReader(b"\xff" * 9 + b"\x7f").read_uvarint()
        # 10 字节且第 10 字节载荷为 1: 恰好是 UINT64_MAX, 合法
        self.assertEqual(vb.BitReader(b"\xff" * 9 + b"\x01").read_uvarint(),
                         vb.UINT64_MAX)

    def test_error_does_not_consume(self):
        r = vb.BitReader(b"\x80\x00")
        with self.assertRaises(vb.NonShortestEncodingError):
            r.read_uvarint()
        self.assertEqual(r.available, 16)


class StreamingTest(unittest.TestCase):
    def test_insufficient_bits_reporting(self):
        data, _ = encode(lambda w: w.write_uvarint(300))  # 0xAC 0x02
        self.assertEqual(data, b"\xAC\x02")
        r = vb.BitReader()
        r.feed_bits(0b10101, 5)  # 第一个字节只到了 5 位
        with self.assertRaises(vb.InsufficientBits) as ctx:
            r.read_uvarint()
        self.assertEqual(ctx.exception.needed, 3)
        r.feed_bits(0b100, 3)  # 凑齐 0xAC, 续位置位
        with self.assertRaises(vb.InsufficientBits) as ctx:
            r.read_uvarint()
        self.assertEqual(ctx.exception.needed, 8)
        r.feed(data[1:])
        self.assertEqual(r.read_uvarint(), 300)

    def test_streaming_long_prefix_bit_by_bit(self):
        # 超长前缀逐字节到达: 终止字节到来前始终报数据不足
        r = vb.BitReader()
        for _ in range(9):
            r.feed_bits(0x80, 8)
            with self.assertRaises(vb.InsufficientBits) as ctx:
                r.read_uvarint()
            self.assertEqual(ctx.exception.needed, 8)
        r.feed_bits(0x00, 8)
        with self.assertRaises(vb.NonShortestEncodingError) as ctx:
            r.read_uvarint()
        self.assertEqual(ctx.exception.extra_bytes, 9)

    def test_streaming_equals_oneshot(self):
        rng = random.Random(20260924)
        for _ in range(300):
            ops = []
            for _ in range(rng.randrange(1, 30)):
                kind = rng.choice("usb")
                if kind == "u":
                    tier = rng.randrange(65)
                    ops.append(("u", rng.getrandbits(tier) if tier else 0))
                elif kind == "s":
                    tier = rng.randrange(64)
                    n = rng.getrandbits(tier) if tier else 0
                    ops.append(("s", -n - 1 if rng.random() < 0.5 else n))
                else:
                    width = rng.randrange(1, 65)
                    ops.append(("b", rng.getrandbits(width), width))
            w = vb.BitWriter()
            for op in ops:
                if op[0] == "u":
                    w.write_uvarint(op[1])
                elif op[0] == "s":
                    w.write_svarint(op[1])
                else:
                    w.write_bits(op[1], op[2])
            data, nbits = w.to_bytes(), w.bit_length

            def read_all(reader):
                out = []
                for op in ops:
                    if op[0] == "u":
                        out.append(reader.read_uvarint())
                    elif op[0] == "s":
                        out.append(reader.read_svarint())
                    else:
                        out.append(reader.read_bits(op[2]))
                return out

            oneshot = read_all(vb.BitReader(data))
            self.assertEqual(oneshot, [op[1] for op in ops])

            # 随机位块边界喂入, 边喂边解码, 数据不足就继续喂
            bits = [(data[i >> 3] >> (7 - (i & 7))) & 1 for i in range(nbits)]
            stream = vb.BitReader()
            fed = 0
            got = []
            for op in ops:
                while True:
                    try:
                        if op[0] == "u":
                            got.append(stream.read_uvarint())
                        elif op[0] == "s":
                            got.append(stream.read_svarint())
                        else:
                            got.append(stream.read_bits(op[2]))
                        break
                    except vb.InsufficientBits as e:
                        self.assertGreater(e.needed, 0)
                        take = min(rng.randrange(1, 13), nbits - fed)
                        self.assertGreater(take, 0)
                        chunk = 0
                        for bit in bits[fed:fed + take]:
                            chunk = (chunk << 1) | bit
                        stream.feed_bits(chunk, take)
                        fed += take
            self.assertEqual(got, oneshot)


if __name__ == "__main__":
    unittest.main()
