"""位级对拍脚本: varbit 库 vs 逐位参考实现。

对随机整数序列:
1. 比较库与参考实现产出的每一个输出位;
2. 比较库与参考实现解码出的每一个值;
3. 把编码结果切成随机大小的位块喂入流式解码器,
   验证跨块读取与一次性读取完全一致;
4. 在随机断点快照续读游标, 序列化后换一个读取器按任意位块喂到
   游标处恢复, 继续解码, 验证与一次性读取完全一致;
5. 给出变长编码与定长 64 位编码的体积对比。

用法: python3 bitdiff.py [iterations] [seed]
"""

import random
import sys

import varbit as vb
from reference import RefWriter, RefReader


def bytes_to_bits(data, nbits):
    return [(data[i >> 3] >> (7 - (i & 7))) & 1 for i in range(nbits)]


def random_ops(rng, count):
    ops = []
    for _ in range(count):
        kind = rng.choice("uusbb")
        if kind == "u":
            tier = rng.randrange(65)
            ops.append(("u", rng.getrandbits(tier) if tier else 0))
        elif kind == "s":
            tier = rng.randrange(64)
            n = rng.getrandbits(tier) if tier else 0
            if rng.random() < 0.5:
                n = -n - 1
            ops.append(("s", n))
        else:
            width = rng.randrange(1, 65)
            ops.append(("b", rng.getrandbits(width), width))
    return ops


def apply_write(writer, ops):
    for op in ops:
        if op[0] == "u":
            writer.write_uvarint(op[1])
        elif op[0] == "s":
            writer.write_svarint(op[1])
        else:
            writer.write_bits(op[1], op[2])


def read_op(reader, op):
    if op[0] == "u":
        return reader.read_uvarint()
    if op[0] == "s":
        return reader.read_svarint()
    return reader.read_bits(op[2])


def feed_in_random_bit_chunks(reader, bits, rng):
    pos = 0
    while pos < len(bits):
        take = min(rng.randrange(1, 18), len(bits) - pos)
        chunk = 0
        for bit in bits[pos:pos + take]:
            chunk = (chunk << 1) | bit
        reader.feed_bits(chunk, take)
        pos += take


def run_round(rng, iterations):
    for it in range(iterations):
        ops = random_ops(rng, rng.randrange(1, 40))

        lib_writer = vb.BitWriter()
        ref_writer = RefWriter()
        apply_write(lib_writer, ops)
        apply_write(ref_writer, ops)

        data = lib_writer.to_bytes()
        lib_bits = bytes_to_bits(data, lib_writer.bit_length)

        # 1. 每一个输出位必须与逐位参考实现一致
        assert lib_bits == ref_writer.bits, "bit mismatch at iter %d" % it
        # 末尾填充位必须为 0
        assert all(b == 0 for b in bytes_to_bits(data, len(data) * 8)[len(lib_bits):])

        # 2. 库解码与参考解码、原始值三者一致(一次性读取)
        lib_reader = vb.BitReader(data)
        ref_reader = RefReader(ref_writer.bits)
        for op in ops:
            got = read_op(lib_reader, op)
            want = op[1]
            assert got == want, "roundtrip mismatch %r != %r" % (got, want)
            assert read_op(ref_reader, op) == want, "ref decode mismatch"
        assert lib_reader.available < 8  # 只剩填充位

        # 3. 随机位块流式喂入, 结果必须与一次性读取一致
        stream = vb.BitReader()
        feed_in_random_bit_chunks(stream, lib_bits, rng)
        for op in ops:
            assert read_op(stream, op) == op[1], "chunked decode mismatch"

        # 4. 游标续读对拍: 随机断点快照游标并序列化, 换读取器按任意
        #    位块喂到游标处恢复, 边喂边读, 结果必须与一次性读取一致
        split = rng.randrange(len(ops) + 1)
        r1 = vb.BitReader()
        feed_in_random_bit_chunks(r1, lib_bits, rng)
        for op in ops[:split]:
            read_op(r1, op)
        state = r1.cursor().to_dict()
        assert state["position"] == r1.position
        assert state["block_index"] * 8 + state["bit_offset"] == r1.position

        r2 = vb.BitReader()
        feed_in_random_bit_chunks(r2, lib_bits[:state["position"]], rng)
        r2.restore(vb.BitCursor.from_dict(state))
        assert r2.position == r1.position
        fed = state["position"]
        for op in ops[split:]:
            while True:
                try:
                    got = read_op(r2, op)
                    break
                except vb.InsufficientBits:
                    take = min(rng.randrange(1, 18), len(lib_bits) - fed)
                    assert take > 0
                    chunk = 0
                    for bit in lib_bits[fed:fed + take]:
                        chunk = (chunk << 1) | bit
                    r2.feed_bits(chunk, take)
                    fed += take
            assert got == op[1], "cursor resume mismatch %r != %r" % (got, op[1])


def layout_demo():
    print("\n== 位布局导出示例(自定义格式核对) ==")
    w = vb.BitWriter()
    w.write_bits(0b101, 3)        # 自定义头
    w.write_uvarint(300)          # 无符号变长整数
    w.write_svarint(-7)           # 有符号(ZigZag)
    w.write_bits(0x2FF, 10)       # 定宽原始位段
    print(w.layout([("header", 3), ("uvarint", 16),
                    ("svarint", 8), ("raw", 10)]))
    # 非最短编码的位级定位
    bad = vb.BitWriter()
    bad.write_bits(0b10101, 5)
    bad.write_bits(0x85, 8).write_bits(0x80, 8).write_bits(0x00, 8)
    r = vb.BitReader(bad.to_bytes())
    r.read_bits(5)
    try:
        r.read_uvarint()
    except vb.NonShortestEncodingError as e:
        print("\n非最短编码定位: %s" % e)
        print("  多余 %d 字节, 位区间 [%d, %d)" % (
            e.extra_bytes, e.start_bit, e.end_bit))


def size_report(rng):
    print("\n== 变长编码 vs 定长 64 位编码 ==")
    print("%-22s %12s %12s" % ("数值量级", "变长(字节)", "定长(字节)"))
    for bits in (1, 7, 8, 14, 21, 28, 35, 42, 49, 56, 63, 64):
        lo = 0 if bits == 1 else 1 << (bits - 1)
        hi = (1 << bits) - 1
        sample = [rng.randrange(lo, hi + 1) for _ in range(200)]
        var_bytes = 0
        for v in sample:
            w = vb.BitWriter()
            w.write_uvarint(v)
            var_bytes += (w.bit_length + 7) // 8
        print("%-22s %12.1f %12d" % (
            "[2^%d, 2^%d)" % (max(bits - 1, 0), bits),
            var_bytes / len(sample), 8))

    n = 100000
    small = [rng.randrange(0, 128) for _ in range(n)]
    mixed = [rng.getrandbits(rng.randrange(65)) for _ in range(n)]
    signed = [rng.randrange(-1000, 1001) for _ in range(n)]

    def total(values, signed_mode):
        w = vb.BitWriter()
        for v in values:
            if signed_mode:
                w.write_svarint(v)
            else:
                w.write_uvarint(v)
        return (w.bit_length + 7) // 8

    print("\n随机序列总体积 (%d 个值):" % n)
    for name, values, sgn in (("小整数 [0,128)", small, False),
                              ("混合位宽无符号", mixed, False),
                              ("有符号 [-1000,1000]", signed, True)):
        var = total(values, sgn)
        fixed = 8 * n
        print("  %-18s 变长 %8d 字节 | 定长 %8d 字节 | 节省 %.1f%%"
              % (name, var, fixed, 100.0 * (fixed - var) / fixed))


def main():
    iterations = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 42
    rng = random.Random(seed)
    run_round(rng, iterations)
    print("OK: %d 轮随机对拍通过(每一位、每个值、任意位块边界均一致)" % iterations)
    layout_demo()
    size_report(rng)


if __name__ == "__main__":
    main()
