"""varbit: bit-level stream packing and shortest-form varint codec.

Format definition (bit level, no byte-alignment assumption):
  * Bits are packed MSB-first within each byte (network bit order).
  * Fixed-width fields: 1..64 bits, big-endian bit order.
  * Unsigned varint: a sequence of 8-bit groups. Each group is
    1 continuation flag (MSB) + 7 payload bits. Payload is little-endian
    across groups: the first group carries the least significant 7 bits.
    When byte-aligned this is exactly LEB128.
  * The shortest encoding is canonical: the final group must be non-zero
    (except the value 0 itself, encoded as a single 0x00 group).
  * Signed integers use zigzag mapping on top of the unsigned varint:
    0->0, -1->1, 1->2, -2->3, ...

Streaming: BitReader accepts arbitrarily fragmented input (chunks may be
cut at any bit boundary) via feed(data, bit_offset, bit_length). Reads
that run out of data raise NeedMoreBits reporting how many more bits are
required; the reader position is left untouched so the read can simply
be retried after feeding more data.

Only shifts/masks on Python ints are used; no arbitrary-precision
shortcuts (e.g. int.to_bytes for whole fields) replace bit operations.
"""

__all__ = [
    "BitWriter",
    "BitReader",
    "NeedMoreBits",
    "NonCanonicalError",
    "VarintOverflow",
    "zigzag_encode",
    "zigzag_decode",
]

MAX_WIDTH = 64


class NeedMoreBits(Exception):
    """Raised when a read needs more input. ``needed`` = bits still missing."""

    def __init__(self, needed):
        super().__init__("need %d more bit(s)" % needed)
        self.needed = needed


class NonCanonicalError(Exception):
    """Raised when a varint is not in shortest form.

    ``extra_bytes`` is the number of redundant (byte-sized) groups.
    """

    def __init__(self, extra_bytes):
        super().__init__("non-shortest varint: %d redundant byte(s)" % extra_bytes)
        self.extra_bytes = extra_bytes


class VarintOverflow(Exception):
    """Raised when a varint exceeds the configured payload bit limit."""

    def __init__(self, max_bits):
        super().__init__("varint payload exceeds %d bits" % max_bits)
        self.max_bits = max_bits


def zigzag_encode(n):
    """Map a signed integer to an unsigned one (pure bit operations)."""
    # For n < 0, n >> n.bit_length() is -1 (all ones), so the XOR flips
    # the shifted bits; for n >= 0 it is 0. Works for any magnitude.
    return (n << 1) ^ (n >> max(n.bit_length(), 0))


def zigzag_decode(u):
    if u < 0:
        raise ValueError("zigzag input must be non-negative")
    return (u >> 1) ^ -(u & 1)


def _min_groups(u):
    """Minimal number of 7-bit groups needed for u."""
    return max(1, (u.bit_length() + 6) // 7)


class BitWriter:
    """Appends bits/fields to an in-memory buffer, MSB-first per byte."""

    def __init__(self):
        self._bytes = bytearray()
        self._used = 0  # bits used in the last byte (0 means last byte full/absent)

    @property
    def bit_length(self):
        return len(self._bytes) * 8 - ((8 - self._used) & 7)

    def write_bits(self, value, width):
        if not 1 <= width <= MAX_WIDTH:
            raise ValueError("width must be in 1..%d" % MAX_WIDTH)
        if value < 0 or (value >> width) != 0:
            raise ValueError("value 0x%x does not fit in %d bits" % (value, width))
        remaining = width
        # Top up the partially filled last byte, if any.
        if self._used:
            take = min(remaining, 8 - self._used)
            remaining -= take
            self._bytes[-1] |= ((value >> remaining) & ((1 << take) - 1)) << (
                8 - self._used - take
            )
            self._used = (self._used + take) & 7
        # Byte-aligned from here on.
        while remaining:
            take = min(remaining, 8)
            remaining -= take
            self._bytes.append(
                ((value >> remaining) & ((1 << take) - 1)) << (8 - take)
            )
            self._used = take & 7
        return self

    def write_uvarint(self, u):
        if u < 0:
            raise ValueError("uvarint must be non-negative")
        while True:
            group = u & 0x7F
            u >>= 7
            if u:
                self.write_bits(0x80 | group, 8)
            else:
                self.write_bits(group, 8)
                return self

    def write_svarint(self, n):
        return self.write_uvarint(zigzag_encode(n))

    def tobytes(self):
        """Return the packed bytes; unused tail bits of the last byte are 0."""
        return bytes(self._bytes)


class BitReader:
    """Streaming bit reader over an incrementally fed buffer."""

    def __init__(self, data=b"", bit_offset=0, bit_length=None):
        self._bytes = bytearray()
        self._used = 0  # bits used in last byte (writer-side state)
        self._pos = 0   # read cursor, in bits
        if data or bit_length:
            self.feed(data, bit_offset, bit_length)

    # ---- feeding -----------------------------------------------------

    def _append(self, value, width):
        """Append ``width`` low bits of ``value`` to the buffer."""
        remaining = width
        if self._used:
            take = min(remaining, 8 - self._used)
            remaining -= take
            self._bytes[-1] |= ((value >> remaining) & ((1 << take) - 1)) << (
                8 - self._used - take
            )
            self._used = (self._used + take) & 7
        while remaining:
            take = min(remaining, 8)
            remaining -= take
            self._bytes.append(
                ((value >> remaining) & ((1 << take) - 1)) << (8 - take)
            )
            self._used = take & 7

    def feed(self, data, bit_offset=0, bit_length=None):
        """Feed bits [bit_offset, bit_offset+bit_length) of ``data``.

        Chunks may start and end at any bit boundary, so a stream split
        anywhere (even mid-bit-field) can be reassembled losslessly.
        """
        if bit_length is None:
            bit_length = len(data) * 8 - bit_offset
        if bit_offset < 0 or bit_length < 0 or bit_offset + bit_length > len(data) * 8:
            raise ValueError("bit range outside data")
        pos = bit_offset
        remaining = bit_length
        while remaining:
            take = min(remaining, 8 - (pos & 7))
            chunk = (data[pos >> 3] >> (8 - (pos & 7) - take)) & ((1 << take) - 1)
            self._append(chunk, take)
            pos += take
            remaining -= take
        return self

    # ---- reading -----------------------------------------------------

    @property
    def bit_length(self):
        """Total number of bits fed so far."""
        return len(self._bytes) * 8 - ((8 - self._used) & 7)

    @property
    def bits_remaining(self):
        return self.bit_length - self._pos

    def _bits_at(self, pos, width):
        """Read ``width`` bits starting at absolute bit ``pos`` (no cursor move)."""
        v = 0
        remaining = width
        while remaining:
            off = pos & 7
            take = min(remaining, 8 - off)
            v = (v << take) | (
                (self._bytes[pos >> 3] >> (8 - off - take)) & ((1 << take) - 1)
            )
            pos += take
            remaining -= take
        return v

    def read_bits(self, width):
        if not 1 <= width <= MAX_WIDTH:
            raise ValueError("width must be in 1..%d" % MAX_WIDTH)
        if self.bits_remaining < width:
            raise NeedMoreBits(width - self.bits_remaining)
        v = self._bits_at(self._pos, width)
        self._pos += width
        return v

    def read_uvarint(self, max_bits=MAX_WIDTH):
        """Read one unsigned varint.

        Raises NeedMoreBits (cursor untouched) if the terminator group has
        not arrived yet, NonCanonicalError for non-shortest encodings, and
        VarintOverflow if the payload needs more than ``max_bits`` bits
        (also guards against runaway continuation prefixes).
        """
        pos = self._pos
        result = 0
        shift = 0
        groups = 0
        max_groups = (max_bits + 6) // 7
        while True:
            if self.bit_length - pos < 8:
                raise NeedMoreBits(8 - (self.bit_length - pos))
            group = self._bits_at(pos, 8)
            pos += 8
            groups += 1
            if groups > max_groups:
                raise VarintOverflow(max_bits)
            result |= (group & 0x7F) << shift
            shift += 7
            if not (group & 0x80):
                break
        if result >> max_bits:
            raise VarintOverflow(max_bits)
        extra = groups - _min_groups(result)
        if extra:
            raise NonCanonicalError(extra)
        self._pos = pos
        return result

    def read_svarint(self, max_bits=MAX_WIDTH):
        """Read a zigzag-encoded signed varint.

        ``max_bits`` bounds the *unsigned* payload, so a signed value of
        magnitude up to 2**(max_bits-1)-1 / -2**(max_bits-1) fits.
        """
        return zigzag_decode(self.read_uvarint(max_bits))

    # ---- convenience -------------------------------------------------

    def read_all(self, readers):
        """Apply ``readers`` (callables) in order; returns list of results.

        Raises NeedMoreBits if the remaining input is insufficient; the
        cursor is left at the start so the whole batch can be retried.
        """
        saved = self._pos
        out = []
        try:
            for r in readers:
                out.append(r())
        except NeedMoreBits:
            self._pos = saved
            raise
        return out
