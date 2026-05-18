"""
Original implementation for CubePrint.
Protocol details from the public Brother P-touch CBP specification.
"""

import struct
import enum
from collections import namedtuple

import packbits


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------

PrintParameters = namedtuple(
    'PrintParameters',
    ('active_fields', 'media_type', 'width_mm', 'length_mm', 'length_px', 'is_follow_up', 'sbz'),
)


class CommandSet(enum.IntEnum):
    ptcbp = 1


class CompressionType(enum.IntEnum):
    none = 0
    rle = 2


class PageMode(enum.IntFlag):
    auto_cut = 0x40
    mirror = 0x80


class PageModeAdvanced(enum.IntFlag):
    no_page_chaining = 0x08


class PrintParameterField(enum.IntFlag):
    media_type = 0x02
    width = 0x04
    quality = 0x40
    recovery = 0x80


# ---------------------------------------------------------------------------
# Control serialisation helpers
# ---------------------------------------------------------------------------

def serialize_control(mnemonic: str, *args) -> bytes:
    """Serialise a named control command with optional scalar arguments.

    Example usage::

        serialize_control('nop')
        serialize_control('use_command_set', CommandSet.ptcbp)
        serialize_control('set_page_margin', 14)
    """
    return _COMMANDS[mnemonic](*args)


def serialize_control_obj(mnemonic: str, params_namedtuple=None) -> bytes:
    """Serialise a control command whose parameters are supplied as a namedtuple.

    The namedtuple is unpacked positionally before being passed to the
    underlying command builder, so field order must match the spec.
    """
    if params_namedtuple is None:
        return _COMMANDS[mnemonic]()
    return _COMMANDS[mnemonic](*params_namedtuple)


def serialize_data(data: bytes, compress: str = 'none') -> bytes:
    """Serialise a raster data line, optionally applying RLE (packbits) compression.

    *compress* must be ``'none'`` or ``'rle'``.
    The resulting bytes include the ``G`` opcode, a 2-byte little-endian length,
    and the (optionally compressed) payload.
    """
    if compress == 'rle':
        payload = packbits.encode(data)
    elif compress == 'none':
        payload = bytes(data)
    else:
        raise ValueError(f'Unknown compression type: {compress!r}')
    return b'G' + struct.pack('<H', len(payload)) + payload


# ---------------------------------------------------------------------------
# Individual command builders
# Each function returns bytes ready to write to the serial port.
# ---------------------------------------------------------------------------

def _nop() -> bytes:
    """No-operation — used to pad the init sequence."""
    return b'\x00'


def _reset() -> bytes:
    """Reset the printer to its power-on state."""
    return b'\x1b@'


def _get_status() -> bytes:
    """Request a 32-byte status packet from the printer."""
    return b'\x1biS'


def _use_command_set(command_set) -> bytes:
    """Select the active command set (1 = PT-CBP)."""
    return b'\x1bia' + struct.pack('<B', int(command_set))


def _set_print_parameters(active_fields, media_type, width_mm, length_mm,
                           length_px, is_follow_up, sbz) -> bytes:
    """Transmit the print job parameters to the printer.

    ``active_fields`` is a bitmask of ``PrintParameterField`` flags that tells
    the printer which of the following fields to validate against the loaded
    cassette.
    """
    payload = struct.pack(
        '<4BI2B',
        int(active_fields),
        int(media_type),
        int(width_mm),
        int(length_mm),
        int(length_px),
        int(is_follow_up),
        int(sbz),
    )
    return b'\x1biz' + payload


def _set_page_mode(flags) -> bytes:
    """Set page-level print flags (auto-cut, mirror)."""
    return b'\x1biM' + struct.pack('<B', int(flags))


def _set_page_mode_advanced(flags) -> bytes:
    """Set advanced page-level flags (e.g. no-page-chaining)."""
    return b'\x1biK' + struct.pack('<B', int(flags))


def _set_page_margin(dots) -> bytes:
    """Set the end margin in dot units (uint16 little-endian)."""
    return b'\x1bid' + struct.pack('<H', int(dots))


def _compression(compression_type) -> bytes:
    """Declare the compression scheme used for subsequent data lines."""
    return b'M' + struct.pack('<B', int(compression_type))


def _zerofill() -> bytes:
    """Send a blank raster line (all dots off) without transferring 16 zero bytes."""
    return b'Z'


def _print_page() -> bytes:
    """Finalise and print the current page (form-feed, 0x0C)."""
    return b'\x0c'


def _print() -> bytes:
    """Trigger the final print-and-feed command (0x1A)."""
    return b'\x1a'


# ---------------------------------------------------------------------------
# Dispatch table  (mnemonic -> builder function)
# ---------------------------------------------------------------------------

_COMMANDS = {
    'nop':                    _nop,
    'reset':                  _reset,
    'get_status':             _get_status,
    'use_command_set':        _use_command_set,
    'set_print_parameters':   _set_print_parameters,
    'set_page_mode':          _set_page_mode,
    'set_page_mode_advanced': _set_page_mode_advanced,
    'set_page_margin':        _set_page_margin,
    'compression':            _compression,
    'zerofill':               _zerofill,
    'print_page':             _print_page,
    'print':                  _print,
}
