"""refimpl: deliberately naive, bit-by-bit reference implementation.

Used only for differential testing against varbit.py. It represents the
stream as a Python list of individual 0/1 ints and processes exactly one
bit at a time, so it shares no code paths with the chunked real codec.
"""


class RefWriter:
    def __init__(self):
        self.bits = []  # list of 0/1, stream order

    def write_bits(self, value, width):
        assert 1 <= width <= 64
        assert 0 <= value < (1 << width)
        for i in range(width - 1, -1, -1):
            self.bits.append((value >> i) & 1)

    def write_uvarint(self, u):
        assert u >= 0
        while True:
            group = u & 0x7F
            u >>= 7
            cont = 1 if u else 0
            self.write_bits((cont << 7) | group, 8)
            if not cont:
                return

    def write_svarint(self, n):
        # reference zigzag, defined arithmetically on purpose
        u = 2 * n if n >= 0 else -2 * n - 1
        self.write_uvarint(u)

    def to_bytes(self):
        out = bytearray()
        for i in range(0, len(self.bits), 8):
            b = 0
            for bit in self.bits[i : i + 8]:
                b = (b << 1) | bit
            b <<= 8 - len(self.bits[i : i + 8])
            out.append(b)
        return bytes(out)


class RefReader:
    def __init__(self, bits):
        self.bits = list(bits)
        self.i = 0

    @property
    def remaining(self):
        return len(self.bits) - self.i

    def read_bit(self):
        if self.i >= len(self.bits):
            raise IndexError("out of bits")
        b = self.bits[self.i]
        self.i += 1
        return b

    def read_bits(self, width):
        v = 0
        for _ in range(width):
            v = (v << 1) | self.read_bit()
        return v

    def read_uvarint(self):
        result = 0
        shift = 0
        while True:
            cont = self.read_bit()
            payload = 0
            for _ in range(7):
                payload = (payload << 1) | self.read_bit()
            result |= payload << shift
            shift += 7
            if not cont:
                return result

    def read_svarint(self):
        u = self.read_uvarint()
        return u // 2 if u % 2 == 0 else -((u + 1) // 2)
