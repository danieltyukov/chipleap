"""Reference model of the UART line: the bits a byte becomes, and the byte bits carry.

8N1: a start bit (0), eight data bits with the least significant first, a stop bit (1).
The cocotb tests drive and check the RTL against it.
"""

START, STOP = 0, 1


def frame(byte: int) -> list[int]:
    """The line levels for ``byte``, one per bit period."""
    if not 0 <= byte <= 0xFF:
        raise ValueError(f"not a byte: {byte}")
    return [START] + [(byte >> i) & 1 for i in range(8)] + [STOP]


def decode(bits: list[int]) -> tuple[int, bool]:
    """The byte in a ten-bit frame and whether the frame is well formed."""
    if len(bits) != 10:
        raise ValueError(f"a frame has 10 bits, not {len(bits)}")
    byte = sum(b << i for i, b in enumerate(bits[1:9]))
    return byte, bits[0] == START and bits[9] == STOP
