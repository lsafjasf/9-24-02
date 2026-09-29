"""varbit -- 变长整数与位打包编解码库(仅标准库)。

编码约定
--------
* 位序: 字节内高位在前(MSB first); 多位整数按高位到低位依次写入位流。
* 位宽: 支持 1..64 位任意宽度的打包 / 解包。
* 整数语义固定为 64 位:
    - 无符号: [0, 2**64 - 1]
    - 有符号: [-2**63, 2**63 - 1], 采用 ZigZag 映射后按无符号变长编码。
* 变长整数: 无符号 LEB128(每字节 7 位有效载荷, 最高位为续位), 只产生
  最短编码; 解码方拒绝非最短编码并报告多余字节数, 拒绝超过 64 位的编码。
* 流式: BitReader 只依赖已喂入的内存数据, 不需要知道整体长度; 数据不足
  时抛出 InsufficientBits, 其 needed 属性给出至少还需要的位数。

说明: Python 整数是任意精度的, 但本库所有整数语义都通过显式掩码与移位
限定在 64 位以内, 不依赖任意精度来代替位运算。
"""

MASK64 = (1 << 64) - 1
UINT64_MAX = MASK64
INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1

_MAX_VARINT_BYTES = 10  # ceil(64 / 7)


class InsufficientBits(Exception):
    """数据不足。needed 为完成下一个字节至少还需要的位数。"""

    def __init__(self, needed):
        self.needed = needed
        super().__init__("need at least %d more bit(s)" % needed)


class NonShortestEncodingError(Exception):
    """非最短变长编码。

    extra_bytes: 多余的字节数; start_bit / end_bit: 多余字节区域的位
    偏移区间 [start_bit, end_bit)(从 0 计)。LEB128 的冗余是最高有效
    端的零载荷组, 即编码尾部的若干字节: 除最后一字节为 0x00 外,
    其余均为 0x80, 可据此逐位核对。
    """

    def __init__(self, extra_bytes, start_bit=None, end_bit=None):
        self.extra_bytes = extra_bytes
        self.start_bit = start_bit
        self._end_bit = end_bit
        if start_bit is None:
            super().__init__("non-shortest varint: %d extra byte(s)" % extra_bytes)
        else:
            super().__init__(
                "non-shortest varint: %d extra byte(s) from bit %d to bit %d"
                % (extra_bytes, start_bit, self.end_bit))

    @property
    def end_bit(self):
        if self._end_bit is not None:
            return self._end_bit
        if self.start_bit is None:
            return None
        return self.start_bit + 8 * self.extra_bytes


class VarintOverflowError(Exception):
    """变长整数超过 64 位。"""


class BitCursor:
    """续读游标: 记录已消费的位数与所在块, 支持跨块任意位边界恢复读取。

    position: 已消费的总位数; block_index: 所在字节块下标;
    bit_offset: 块内位偏移(0..7)。to_dict/from_dict 可持久化,
    之后用 BitReader.restore 从游标处继续读。
    """

    __slots__ = ("position",)

    def __init__(self, position=0):
        if position < 0:
            raise ValueError("cursor position must be >= 0")
        self.position = position

    @property
    def block_index(self):
        return self.position >> 3

    @property
    def bit_offset(self):
        return self.position & 7

    def to_dict(self):
        return {"position": self.position,
                "block_index": self.block_index,
                "bit_offset": self.bit_offset}

    @classmethod
    def from_dict(cls, state):
        return cls(int(state["position"]))

    def __eq__(self, other):
        return isinstance(other, BitCursor) and self.position == other.position

    def __repr__(self):
        return "BitCursor(position=%d, block=%d, bit_offset=%d)" % (
            self.position, self.block_index, self.bit_offset)


class BitWriter:
    """位流写入器: 1..64 位任意宽度打包, 末尾不足一字节时补 0。"""

    def __init__(self):
        self._out = bytearray()
        self._acc = 0    # 未满一字节的残余位
        self._nbits = 0  # _acc 中的位数, 0..7

    def write_bits(self, value, width):
        if not 1 <= width <= 64:
            raise ValueError("width must be in 1..64")
        if value < 0 or value >= (1 << width):
            raise ValueError("value 0x%x does not fit in %d bit(s)" % (value, width))
        self._acc = (self._acc << width) | value
        self._nbits += width
        while self._nbits >= 8:
            self._nbits -= 8
            self._out.append((self._acc >> self._nbits) & 0xFF)
            self._acc &= (1 << self._nbits) - 1
        return self

    def write_uvarint(self, value):
        write_uvarint(self, value)
        return self

    def write_svarint(self, n):
        write_svarint(self, n)
        return self

    @property
    def bit_length(self):
        return (len(self._out) << 3) + self._nbits

    def to_bytes(self):
        if self._nbits == 0:
            return bytes(self._out)
        return bytes(self._out) + bytes([(self._acc << (8 - self._nbits)) & 0xFF])

    def layout(self, fields=None):
        """导出当前已写入内容的位布局文本(见 format_layout)。"""
        return format_layout(self.to_bytes(), self.bit_length, fields)


class BitReader:
    """流式位流读取器: 可按字节或按位喂入, 不依赖整体长度。"""

    def __init__(self, data=b""):
        self._buf = bytearray()
        self._pos = 0  # 已消费的位数
        self._end = 0  # 已喂入的总位数
        if data:
            self.feed(data)

    def feed(self, data):
        """喂入整块字节。"""
        for b in data:
            self.feed_bits(b, 8)

    def feed_bits(self, value, width):
        """按位喂入, 允许块边界落在任意位中间。"""
        if not 1 <= width <= 64:
            raise ValueError("width must be in 1..64")
        value &= (1 << width) - 1
        remaining = width
        while remaining:
            off = self._end & 7
            if off == 0:
                self._buf.append(0)
            take = min(remaining, 8 - off)
            chunk = (value >> (remaining - take)) & ((1 << take) - 1)
            self._buf[-1] |= chunk << (8 - off - take)
            self._end += take
            remaining -= take

    @property
    def position(self):
        return self._pos

    def reset(self, position):
        self._pos = position

    def cursor(self):
        """快照当前读取位置为续读游标(见 BitCursor)。"""
        return BitCursor(self._pos)

    def restore(self, cursor):
        """从游标恢复读取位置。游标位置不能超过已喂入的位数。"""
        position = cursor.position if isinstance(cursor, BitCursor) else int(cursor)
        if not 0 <= position <= self._end:
            raise ValueError(
                "cursor position %d outside fed data (%d bit(s))"
                % (position, self._end))
        self._pos = position

    @property
    def available(self):
        """当前可读位数。"""
        return self._end - self._pos

    def read_bits(self, width):
        if not 1 <= width <= 64:
            raise ValueError("width must be in 1..64")
        if self.available < width:
            raise InsufficientBits(width - self.available)
        value = 0
        remaining = width
        while remaining:
            off = self._pos & 7
            take = min(remaining, 8 - off)
            chunk = (self._buf[self._pos >> 3] >> (8 - off - take)) & ((1 << take) - 1)
            value = (value << take) | chunk
            self._pos += take
            remaining -= take
        return value

    def try_read_bits(self, width):
        """返回 (value, 0); 数据不足时返回 (None, 还缺的位数), 不消费数据。"""
        if self.available < width:
            return None, width - self.available
        return self.read_bits(width), 0

    def read_uvarint(self):
        return read_uvarint(self)

    def read_svarint(self):
        return read_svarint(self)


def _shortest_size(value):
    size = 1
    while value >= (1 << (7 * size)):
        size += 1
    return size


def write_uvarint(writer, value):
    """无符号 LEB128, 最短编码。"""
    if value < 0 or value > UINT64_MAX:
        raise ValueError("uvarint out of 64-bit unsigned range: %d" % value)
    while True:
        payload = value & 0x7F
        value >>= 7
        writer.write_bits(payload | (0x80 if value else 0), 8)
        if not value:
            return


def read_uvarint(reader):
    """流式解码无符号变长整数。

    数据不足抛 InsufficientBits(不消费数据); 非最短编码抛
    NonShortestEncodingError 并给出多余字节数; 超过 64 位抛
    VarintOverflowError。出错时读取位置保持不变。
    """
    mark = reader.position
    value = 0
    for i in range(_MAX_VARINT_BYTES):
        byte, needed = reader.try_read_bits(8)
        if needed:
            reader.reset(mark)
            raise InsufficientBits(needed)
        payload = byte & 0x7F
        if i == _MAX_VARINT_BYTES - 1 and (byte & 0x80 or payload > 1):
            reader.reset(mark)
            raise VarintOverflowError("varint exceeds 64 bits")
        value |= payload << (7 * i)
        if not byte & 0x80:
            if payload == 0 and i > 0:
                reader.reset(mark)
                shortest = _shortest_size(value)
                raise NonShortestEncodingError(
                    (i + 1) - shortest,
                    mark + 8 * shortest, mark + 8 * (i + 1))
            return value


def zigzag_encode(n):
    """有符号 -> 无符号 ZigZag 映射(64 位语义)。"""
    if not INT64_MIN <= n <= INT64_MAX:
        raise ValueError("svarint out of 64-bit signed range: %d" % n)
    return ((n << 1) ^ (n >> 63)) & MASK64


def zigzag_decode(u):
    if not 0 <= u <= UINT64_MAX:
        raise ValueError("zigzag input out of 64-bit unsigned range: %d" % u)
    return (u >> 1) ^ -(u & 1)


def write_svarint(writer, n):
    write_uvarint(writer, zigzag_encode(n))


def read_svarint(reader):
    return zigzag_decode(read_uvarint(reader))


def format_layout(data, nbits=None, fields=None):
    """导出位布局文本, 便于逐位核对自定义格式。

    data: 编码后的字节串; nbits: 有效位数(默认全部, 末尾视为补 0 的
    位会以空白标出); fields: 可选的 [(名称, 位宽), ...], 按顺序从第
    0 位切分并显示每段的比特与整数值, 未覆盖部分记为 <rest>。
    """
    total = len(data) * 8
    if nbits is None:
        nbits = total
    if not 0 <= nbits <= total:
        raise ValueError("nbits %d out of range [0, %d]" % (nbits, total))
    bits = [(data[i >> 3] >> (7 - (i & 7))) & 1 for i in range(total)]
    pad = total - nbits

    stream = []
    ruler = []
    for i, bit in enumerate(bits):
        if i and i % 8 == 0:
            stream.append(" ")
            ruler.append(" ")
        stream.append(str(bit))
        ruler.append("^" if i < nbits else " ")

    lines = ["== 位布局: 有效 %d 位 / 共 %d 位 (%d 字节, 补 0 %d 位) =="
             % (nbits, total, len(data), pad),
             "位流 (MSB first):",
             "  " + "".join(stream),
             "  " + "".join(ruler) + ("  (^=有效位)" if pad else "")]

    lines.append("字节视图:")
    for byte_index in range(len(data)):
        base = byte_index * 8
        pad_here = max(0, min(8, base + 8 - nbits))
        note = "  (低 %d 位补 0)" % pad_here if pad_here else ""
        lines.append("  byte %-3d bit %3d..%-3d 0x%02X  %s%s" % (
            byte_index, base, base + 7, data[byte_index],
            "".join(str(bits[base + k]) for k in range(8)), note))

    if fields:
        covered = sum(width for _, width in fields)
        if covered > nbits:
            raise ValueError(
                "fields cover %d bit(s) > nbits %d" % (covered, nbits))
        lines.append("字段视图:")
        pos = 0
        for name, width in fields:
            if width < 1:
                raise ValueError("field %r width must be >= 1" % name)
            value = 0
            for i in range(pos, pos + width):
                value = (value << 1) | bits[i]
            bitstr = "".join(str(bits[i]) for i in range(pos, pos + width))
            lines.append("  bit %3d..%-3d %-12s %s  value=%d"
                         % (pos, pos + width - 1, name, bitstr, value))
            pos += width
        if pos < nbits:
            bitstr = "".join(str(bits[i]) for i in range(pos, nbits))
            lines.append("  bit %3d..%-3d %-12s %s"
                         % (pos, nbits - 1, "<rest>", bitstr))
    return "\n".join(lines)
