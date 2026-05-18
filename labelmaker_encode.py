"""
Original implementation for CubePrint.
Protocol details from the public Brother P-touch CBP specification.
"""

import ptcbp
from PIL import Image, ImageOps


# The PT-P300BT prints at 180 dpi across 128 dots (the full printable width).
# Each raster line is therefore 128 bits = 16 bytes.
_LINE_BYTES = 16
_LINE_WIDTH_PX = 128


def encode_raster_transfer(data: bytes, nocomp: bool = False):
    """Yield one bytes object per raster line, ready to write to the serial port.

    Blank lines (all zero) are encoded as a single ``Z`` zerofill command.
    Non-blank lines are encoded as ``G <len16LE> <payload>`` where the payload
    is either raw bytes (``nocomp=True``) or packbits RLE (``nocomp=False``).

    *data* must be a flat 1-bpp byte string whose total length is a multiple of
    ``_LINE_BYTES`` (16).  Use :func:`read_png` to obtain data in this format.
    """
    blank = bytes(_LINE_BYTES)
    compress = 'none' if nocomp else 'rle'

    for offset in range(0, len(data), _LINE_BYTES):
        line = data[offset: offset + _LINE_BYTES]
        if line == blank:
            yield ptcbp.serialize_control('zerofill')
        else:
            yield ptcbp.serialize_data(line, compress)


def read_png(path: str, transform: bool = True, padding: bool = True, dither: bool = True) -> bytes:
    """Open an image file and return raw 1-bpp bytes suitable for :func:`encode_raster_transfer`.

    Processing pipeline:

    1. Open the image with Pillow (any format Pillow supports).
    2. Convert to 1 bpp using Floyd-Steinberg dithering (or no dithering when
       ``dither=False``).
    3. Invert the image so that black pixels in the source become printed dots.
    4. If ``transform`` is ``True``: rotate -90 degrees and mirror horizontally
       so that the image feeds correctly through the tape printer.
    5. If ``padding`` is ``True``: centre the image horizontally in a
       128-pixel-wide canvas, padding with white (unprinted) columns as needed.
    6. Return the raw bytes of the processed image (Pillow ``tobytes()``).
    """
    image = Image.open(path)

    # Convert to 1 bpp; dither in L mode then back to '1' avoids Pillow quirks
    dither_mode = Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE
    mono = image.convert('L').convert('1', dither=dither_mode)

    # Invert: source black → printed dot
    mono = ImageOps.invert(mono.convert('L')).convert('1')

    if transform:
        mono = mono.rotate(-90, expand=True)
        mono = ImageOps.mirror(mono)

    if padding:
        w, h = mono.size
        canvas = Image.new('1', (_LINE_WIDTH_PX, h), color=0)
        x_offset = (_LINE_WIDTH_PX - w) // 2
        canvas.paste(mono, (x_offset, 0))
        mono = canvas

    return mono.tobytes()
