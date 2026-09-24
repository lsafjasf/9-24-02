"""逐位参考实现: 用 0/1 列表一次处理一个位, 逻辑尽量直白,
用于对 varbit 库做位级对拍。编码约定与 varbit 相同:
字节内 MSB first, 整数高位先写, LEB128 + ZigZag, 64 位语义。
"""

MASK64 = (1 << 64) - 1


class RefWriter:
    def __init__(self):
        self.bits = []

    def write_bits(self, value, width):
        assert 1 <= width <= 64
        assert 0 <= value < (1 << width)
        for i in range(width - 1, -1, -1):
            self.bits.append((value >> i) & 1)

    def write_uvarint(self, value):
        while True:
            payload = value & 0x7F
            value >>= 7
            self.write_bits(payload | (0x80 if value else 0), 8)
            if not value:
                return

    def write_svarint(self, n):
        self.write_uvarint(((n << 1) ^ (n >> 63)) & MASK64)


class RefReader:
    def __init__(self, bits):
        self.bits = bits
        self.pos = 0

    def read_bits(self, width):
        assert self.pos + width <= len(self.bits)
        value = 0
        for _ in range(width):
            value = (value << 1) | self.bits[self.pos]
            self.pos += 1
        return value

    def read_uvarint(self):
        value = 0
        shift = 0
        while True:
            byte = self.read_bits(8)
            value |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                return value

    def read_svarint(self):
        u = self.read_uvarint()
        return (u >> 1) ^ -(u & 1)
