"""
Original implementation for CubePrint.
Protocol details from the public Brother P-touch CBP specification.
"""

import argparse
import sys
import time

import ptcbp
import ptstatus
import serial

from labelmaker_encode import encode_raster_transfer, read_png


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

# Maps human-readable tape-type names to the media-type byte used in the
# set_print_parameters command.  Use 'any' (0x00) to skip cassette validation,
# which is required for third-party tapes whose sensor chip is absent.
MEDIA_TYPE_MAP = {
    'laminated':     0x01,
    'non-laminated': 0x03,
    'heatshrink':    0x11,
    'any':           0x00,
}

# Progress bar characters (index 0 = blank/zerofill, 1-8 = data density)
_BARS = '123456789'


# ---------------------------------------------------------------------------
# Low-level printer control
# ---------------------------------------------------------------------------

def reset_printer(port) -> None:
    """Flush any pending state on the printer and select the PT-CBP command set."""
    port.write(b'\x00' * 64)
    port.write(ptcbp.serialize_control('reset'))
    port.write(ptcbp.serialize_control('use_command_set', ptcbp.CommandSet.ptcbp))


def configure_printer(
    port,
    raster_lines: int,
    tape_dim: tuple,
    compress: bool = True,
    chaining: bool = False,
    auto_cut: bool = False,
    end_margin: int = 0,
    check_media_type: bool = False,
    check_width: bool = True,
) -> None:
    """Reset the printer and send all job-level configuration commands.

    Parameters
    ----------
    port:
        An open serial.Serial (or compatible) port object.
    raster_lines:
        Total number of raster lines in the image (image height after rotation).
    tape_dim:
        ``(media_type_byte, width_mm, length_mm)`` tuple.  ``length_mm`` may be
        0 for continuous tape.
    compress:
        If ``True``, select RLE compression for subsequent data lines.
    chaining:
        If ``True``, omit the no-page-chaining flag so multiple labels can be
        printed back-to-back without cutting between them.
    auto_cut:
        If ``True``, set the auto-cut flag in the page mode register.
    end_margin:
        Trailing margin in dot units (0 = default).
    check_media_type:
        If ``True``, include the media-type field in the active-fields bitmask,
        causing the printer to validate the loaded cassette type.
    check_width:
        Reserved for future use; ignored in the current implementation.
    """
    reset_printer(port)

    media_type, width_mm, length_mm = tape_dim

    # Build the active-fields bitmask.  Width is always sent so the printer
    # knows how many dot columns to use.  Media type is only validated when the
    # caller has confirmed a Brother-genuine cassette is loaded.
    active_fields = (
        ptcbp.PrintParameterField.quality |
        ptcbp.PrintParameterField.recovery |
        ptcbp.PrintParameterField.width
    )
    if check_media_type:
        active_fields |= ptcbp.PrintParameterField.media_type

    port.write(ptcbp.serialize_control_obj(
        'set_print_parameters',
        ptcbp.PrintParameters(
            active_fields=active_fields,
            media_type=media_type,
            width_mm=width_mm,
            length_mm=length_mm,
            length_px=raster_lines,
            is_follow_up=0,
            sbz=0,
        ),
    ))

    page_mode = 0
    page_mode_adv = 0

    if auto_cut:
        page_mode |= ptcbp.PageMode.auto_cut
    if not chaining:
        page_mode_adv |= ptcbp.PageModeAdvanced.no_page_chaining

    port.write(ptcbp.serialize_control('set_page_mode_advanced', page_mode_adv))
    port.write(ptcbp.serialize_control('set_page_mode', page_mode))
    port.write(ptcbp.serialize_control('set_page_margin', end_margin))
    port.write(ptcbp.serialize_control(
        'compression',
        ptcbp.CompressionType.rle if compress else ptcbp.CompressionType.none,
    ))


# ---------------------------------------------------------------------------
# Status helper
# ---------------------------------------------------------------------------

def _read_status(port, timeout: float = 2.0):
    """Request a status packet and return the parsed result, or ``None`` on timeout."""
    old_timeout = port.timeout
    port.timeout = timeout
    try:
        port.write(ptcbp.serialize_control('get_status'))
        port.flush()
        raw = port.read(32)
        if len(raw) == 32:
            return ptstatus.unpack_status(raw)
        print(f'=> Status read: got {len(raw)}/32 bytes (printer may not respond over BT)')
        return None
    finally:
        port.timeout = old_timeout


# ---------------------------------------------------------------------------
# High-level print job
# ---------------------------------------------------------------------------

def do_print_job(ser, args, data: bytes) -> None:
    """Configure the printer and transfer *data*, then optionally trigger printing.

    *args* is the namespace returned by :func:`parse_args` (or any object with
    the same attributes: ``tape_width``, ``media_type``, ``no_feed``,
    ``auto_cut``, ``end_margin``, ``nocomp``, ``no_print``).
    """
    tape_width = getattr(args, 'tape_width', 12)
    media_type_name = getattr(args, 'media_type', 'any')
    media_type = MEDIA_TYPE_MAP.get(media_type_name, 0x00)
    # When 'any' is selected, skip all cassette validation (safe for 3rd-party tapes)
    bypass_checks = (media_type == 0x00)

    print('=> Querying printer status...')
    ser.write(b'\x00' * 64)
    ser.write(ptcbp.serialize_control('reset'))
    ser.flush()
    time.sleep(0.2)
    status = _read_status(ser)
    if status is not None:
        ptstatus.print_status(status)
        if status.err != 0:
            print('** Printer has error flags set — it may refuse to print')
    else:
        print('=> No status response (normal for macOS BT) — proceeding anyway')

    raster_lines = len(data) // 16
    print(f'=> Configuring: {tape_width} mm tape, media={media_type_name}, {raster_lines} raster lines')

    configure_printer(
        ser,
        raster_lines,
        (media_type, tape_width, 0),
        compress=not args.nocomp,
        chaining=args.no_feed,
        auto_cut=getattr(args, 'auto_cut', False),
        end_margin=args.end_margin,
        check_media_type=not bypass_checks,
        check_width=not bypass_checks,
    )

    print(f'=> Sending image data ({raster_lines} lines)...')
    sys.stdout.write('[')
    for line in encode_raster_transfer(data, args.nocomp):
        if line[0:1] == b'G':
            # Show density of the compressed line as a bar character
            sys.stdout.write(_BARS[min((len(line) - 3) // 2, 7) + 1])
        elif line[0:1] == b'Z':
            sys.stdout.write(_BARS[0])
        sys.stdout.flush()
        ser.write(line)
    sys.stdout.write(']\n')
    ser.flush()

    if not args.no_print:
        print('=> Sending print command...')
        ser.write(ptcbp.serialize_control('print'))
        ser.flush()
        time.sleep(5)  # wait for the printer to finish before the port closes
        print('=> Done.')
    else:
        print('=> Image sent (no-print mode).')


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description='Print an image on a Brother PT-series label printer.')
    p.add_argument('comport', help='Serial port or Bluetooth RFCOMM device path.')
    p.add_argument('-i', '--image', help='Image file to print.')
    p.add_argument('-n', '--no-print', action='store_true',
                   help='Configure and transfer the image but do not send the print command.')
    p.add_argument('-F', '--no-feed', action='store_true',
                   help='Disable feeding at end of print (label chaining mode).')
    p.add_argument('-a', '--auto-cut', action='store_true',
                   help='Enable auto-cut after printing.')
    p.add_argument('-m', '--end-margin', type=int, default=0,
                   help='End margin in dots (default: 0).')
    p.add_argument('-r', '--raw', action='store_true',
                   help='Send the image as-is, without rotation/padding/dithering.')
    p.add_argument('-C', '--nocomp', action='store_true',
                   help='Disable RLE compression.')
    p.add_argument('--tape-width', type=int, default=12,
                   help='Tape width in mm (default: 12).')
    p.add_argument('--media-type', choices=list(MEDIA_TYPE_MAP), default='any',
                   help='Tape media type; use "any" for third-party tapes (default: any).')
    return p, p.parse_args()


def main() -> None:
    p, args = parse_args()

    if args.image is None:
        p.error('An image file must be specified with -i / --image.')

    if args.raw:
        data = read_png(args.image, transform=False, padding=False, dither=False)
    else:
        data = read_png(args.image)

    ser = serial.Serial(args.comport)
    try:
        do_print_job(ser, args, data)
    finally:
        reset_printer(ser)


if __name__ == '__main__':
    main()
