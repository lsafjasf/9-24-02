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


def _random_ops(rng, count):
    ops = []
    for _ in range(count):
        kind = rng.choice("uusb")
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
    return ops


def _write_ops(ops):
    w = vb.BitWriter()
    for op in ops:
        if op[0] == "u":
            w.write_uvarint(op[1])
        elif op[0] == "s":
            w.write_svarint(op[1])
        else:
            w.write_bits(op[1], op[2])
    return w.to_bytes(), w.bit_length


def _read_op(reader, op):
    if op[0] == "u":
        return reader.read_uvarint()
    if op[0] == "s":
        return reader.read_svarint()
    return reader.read_bits(op[2])


def _bits_of(data, nbits):
    return [(data[i >> 3] >> (7 - (i & 7))) & 1 for i in range(nbits)]


def _feed_bits_chunked(reader, bits, rng, max_chunk=17):
    pos = 0
    while pos < len(bits):
        take = min(rng.randrange(1, max_chunk), len(bits) - pos)
        chunk = 0
        for bit in bits[pos:pos + take]:
            chunk = (chunk << 1) | bit
        reader.feed_bits(chunk, take)
        pos += take


class CursorTest(unittest.TestCase):
    def test_cursor_tracks_block_and_bit(self):
        r = vb.BitReader(b"\xde\xad\xbe\xef")
        r.read_bits(3)
        cur = r.cursor()
        self.assertEqual((cur.position, cur.block_index, cur.bit_offset),
                         (3, 0, 3))
        r.read_bits(9)
        cur = r.cursor()
        self.assertEqual((cur.position, cur.block_index, cur.bit_offset),
                         (12, 1, 4))

    def test_cursor_serialization(self):
        cur = vb.BitCursor(12345)
        state = cur.to_dict()
        self.assertEqual(state, {"position": 12345,
                                 "block_index": 12345 >> 3,
                                 "bit_offset": 12345 & 7})
        self.assertEqual(vb.BitCursor.from_dict(state), cur)
        with self.assertRaises(ValueError):
            vb.BitCursor(-1)

    def test_restore_rejects_unfed_position(self):
        r = vb.BitReader(b"\x00")
        with self.assertRaises(ValueError):
            r.restore(vb.BitCursor(9))
        r.feed(b"\x00")
        r.restore(vb.BitCursor(9))  # 现在已喂够, 合法
        self.assertEqual(r.position, 9)

    def test_resume_matches_oneshot(self):
        # 对拍: 任意断点快照游标 -> 序列化 -> 换读取器按随机位块喂到
        # 游标处恢复 -> 交错喂剩余位继续读, 结果必须与一次性读取一致。
        rng = random.Random(20260929)
        for _ in range(300):
            ops = _random_ops(rng, rng.randrange(1, 25))
            data, nbits = _write_ops(ops)
            bits = _bits_of(data, nbits)
            base_reader = vb.BitReader(data)
            oneshot = [_read_op(base_reader, op) for op in ops]

            split = rng.randrange(len(ops) + 1)
            r1 = vb.BitReader()
            _feed_bits_chunked(r1, bits, rng)
            got = [_read_op(r1, op) for op in ops[:split]]
            state = r1.cursor().to_dict()

            r2 = vb.BitReader()
            _feed_bits_chunked(r2, bits[:state["position"]], rng)
            r2.restore(vb.BitCursor.from_dict(state))
            self.assertEqual(r2.position, state["position"])
            fed = state["position"]
            for op in ops[split:]:
                while True:
                    try:
                        got.append(_read_op(r2, op))
                        break
                    except vb.InsufficientBits:
                        take = min(rng.randrange(1, 11), nbits - fed)
                        self.assertGreater(take, 0)
                        chunk = 0
                        for bit in bits[fed:fed + take]:
                            chunk = (chunk << 1) | bit
                        r2.feed_bits(chunk, take)
                        fed += take
            self.assertEqual(got, oneshot)


class NonShortestDetailTest(unittest.TestCase):
    def test_reports_extra_bytes_and_start_bit(self):
        # 前缀 5 位, 然后 0x85 0x80 0x00: 值 5 用 3 字节编码(最短为 1
        # 字节), 多余的 2 个零载荷字节在编码尾部, 从第 5+8=13 位开始
        w = vb.BitWriter()
        w.write_bits(0b10101, 5)
        w.write_bits(0x85, 8)
        w.write_bits(0x80, 8)
        w.write_bits(0x00, 8)
        data, nbits = w.to_bytes(), w.bit_length
        r = vb.BitReader(data)
        r.read_bits(5)
        with self.assertRaises(vb.NonShortestEncodingError) as ctx:
            r.read_uvarint()
        err = ctx.exception
        self.assertEqual(err.extra_bytes, 2)
        self.assertEqual(err.start_bit, 13)
        self.assertEqual(err.end_bit, 29)
        # 逐位核对: [start_bit, end_bit) 正是尾部的 0x80 与 0x00
        bits = _bits_of(data, nbits)
        self.assertEqual(bits[13:21], [1, 0, 0, 0, 0, 0, 0, 0])
        self.assertEqual(bits[21:29], [0, 0, 0, 0, 0, 0, 0, 0])
        self.assertIn("2", str(err))
        self.assertIn("13", str(err))

    def test_extra_region_removal_yields_shortest(self):
        # 随机前缀位 + 随机多余字节数: 删掉报告的多余位区间后,
        # 剩下的编码必须是最短编码且解码出同一个值。
        rng = random.Random(99)
        for _ in range(200):
            prefix = rng.randrange(8)
            extra = rng.randrange(1, 4)
            tier = rng.randrange(1, 50)
            value = rng.getrandbits(tier)
            shortest_len = max(1, (value.bit_length() + 6) // 7)
            w = vb.BitWriter()
            if prefix:
                w.write_bits(rng.getrandbits(prefix), prefix)
            # 最短编码的每个字节都置续位, 再追加 extra 个零载荷组
            enc = []
            v = value
            for _ in range(shortest_len):
                enc.append((v & 0x7F) | 0x80)
                v >>= 7
            for _ in range(extra):
                enc.append(0x80)
            enc[-1] &= 0x7F  # 最后一字节清续位(0x80 -> 0x00)
            for byte in enc:
                w.write_bits(byte, 8)
            data, nbits = w.to_bytes(), w.bit_length
            r = vb.BitReader(data)
            if prefix:
                r.read_bits(prefix)
            with self.assertRaises(vb.NonShortestEncodingError) as ctx:
                r.read_uvarint()
            err = ctx.exception
            self.assertEqual(err.extra_bytes, extra)
            self.assertEqual(err.start_bit, prefix + 8 * shortest_len)
            self.assertEqual(err.end_bit, prefix + 8 * (shortest_len + extra))
            # 逐位核对: 多余区域内除最后一字节为 0x00 外全是 0x80
            bits = _bits_of(data, nbits)
            for i in range(err.start_bit, err.end_bit):
                first_of_byte = (i - err.start_bit) % 8 == 0
                last_byte = i < err.end_bit - 8
                self.assertEqual(bits[i], 1 if (first_of_byte and last_byte)
                                 else 0, "bit %d" % i)
            # 删除多余位区间, 并把新的末字节续位清 0 -> 最短编码,
            # 解码必须得到同一个值且不再报错
            trimmed = bits[:err.start_bit] + bits[err.end_bit:]
            trimmed[err.start_bit - 8] = 0
            tw = vb.BitWriter()
            for bit in trimmed:
                tw.write_bits(bit, 1)
            tr = vb.BitReader(tw.to_bytes())
            if prefix:
                tr.read_bits(prefix)
            self.assertEqual(tr.read_uvarint(), value)


class LayoutExportTest(unittest.TestCase):
    def test_layout_bitstream_and_fields(self):
        w = vb.BitWriter()
        w.write_bits(0b101, 3)
        w.write_uvarint(300)          # 0xAC 0x02
        w.write_bits(0x2FF, 10)
        data, nbits = w.to_bytes(), w.bit_length
        text = w.layout([("hdr", 3), ("var", 16), ("raw", 10)])
        # 位流逐位核对: 文本中的位流行与有效位标记必须与实际比特一致
        all_bits = _bits_of(data, len(data) * 8)
        grouped = " ".join("".join(str(b) for b in all_bits[i:i + 8])
                           for i in range(0, len(all_bits), 8))
        self.assertIn(grouped, text)
        self.assertIn("hdr", text)
        self.assertIn("101", text)
        self.assertIn("value=5", text)
        self.assertIn("value=%d" % 0x2FF, text)
        # 有效位标记: ^ 的数量必须等于有效位数
        caret_line = [ln for ln in text.splitlines() if "^" in ln][0]
        self.assertEqual(caret_line.split("  (")[0].count("^"), nbits)
        # 未覆盖字段记为 <rest>
        text2 = vb.format_layout(data, nbits, [("hdr", 3)])
        self.assertIn("<rest>", text2)

    def test_layout_validates_arguments(self):
        data = b"\xAC\x02"
        with self.assertRaises(ValueError):
            vb.format_layout(data, 17)
        with self.assertRaises(ValueError):
            vb.format_layout(data, 16, [("too_long", 24)])
        with self.assertRaises(ValueError):
            vb.format_layout(data, 16, [("zero", 0)])


if __name__ == "__main__":
    unittest.main()
