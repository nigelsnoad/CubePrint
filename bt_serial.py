#!/usr/bin/env python3
"""
Print to PT-P300BT via the bt_rfcomm Swift helper, which has proper
NSBluetoothAlwaysUsageDescription and can access IOBluetooth RFCOMM.

Usage: python3 bt_serial.py [same args as printlabel.py]

This replaces bt_print.py and the broken macOS BT serial port.
"""
import sys, os, subprocess, time, struct
sys.path.insert(0, os.path.dirname(__file__))

from labelmaker_encode import encode_raster_transfer, read_png
from labelmaker import configure_printer, MEDIA_TYPE_MAP
import ptcbp, ptstatus

SCRIPT_DIR = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
BT_RFCOMM_BIN = os.path.join(SCRIPT_DIR, 'bt_rfcomm')


class SwiftChannel:
    """Serial-port-like interface backed by bt_rfcomm subprocess."""

    def __init__(self, mac):
        self._mac = mac
        self._pending_write = bytearray()

    def write(self, data):
        if isinstance(data, (bytes, bytearray)):
            self._pending_write.extend(data)

    def flush(self):
        pass  # writes are batched; call send_and_recv to transmit

    def send_and_recv(self, n_response=0, read_timeout=3.0):
        """Send all buffered bytes to printer, return n_response bytes."""
        hex_data = self._pending_write.hex()
        self._pending_write.clear()
        if not hex_data:
            return b''

        cmd = [BT_RFCOMM_BIN, self._mac, hex_data, str(n_response), str(read_timeout)]
        result = subprocess.run(cmd, capture_output=True, timeout=read_timeout + 20)
        if result.returncode != 0:
            stderr = result.stderr.decode(errors='replace')
            raise RuntimeError(f"bt_rfcomm failed:\n{stderr}")

        hex_response = result.stdout.decode().strip()
        return bytes.fromhex(hex_response) if hex_response else b''

    def read(self, n, timeout=3.0):
        return self.send_and_recv(n)

    @property
    def timeout(self):
        return None

    @timeout.setter
    def timeout(self, val):
        pass


def do_print_job_swift(mac, data, tape_width=12, media_type_name='any',
                       no_feed=False, auto_cut=False, end_margin=0,
                       nocomp=False, no_print=False):
    if not os.path.isfile(BT_RFCOMM_BIN):
        raise FileNotFoundError(
            f"bt_rfcomm not found at {BT_RFCOMM_BIN}\n"
            f"Compile it with: cd {SCRIPT_DIR} && make bt_rfcomm"
        )

    media_type = MEDIA_TYPE_MAP.get(media_type_name, 0x00)
    bypass_checks = media_type == 0x00
    raster_lines = len(data) // 16
    ch = SwiftChannel(mac)

    # Phase 1: query status
    print("=> Querying printer status...")
    ch.write(b"\x00" * 64)
    ch.write(ptcbp.serialize_control('reset'))
    ch.write(ptcbp.serialize_control('get_status'))
    status_bytes = ch.send_and_recv(n_response=32)

    if len(status_bytes) == 32:
        status = ptstatus.unpack_status(status_bytes)
        ptstatus.print_status(status)
        print(f"=> Tape: {status.tape_width}mm, type 0x{status.tape_type:02x}")
        if not bypass_checks:
            tape_width = status.tape_width
            media_type = status.tape_type
    else:
        print(f"=> Got {len(status_bytes)}/32 status bytes — using supplied values")

    # Phase 2: configure + send image
    print(f"=> Configuring: {tape_width}mm, media={media_type_name}, {raster_lines} lines")
    configure_printer(
        ch, raster_lines, (media_type, tape_width, 0),
        chaining=no_feed,
        auto_cut=auto_cut,
        end_margin=end_margin,
        compress=not nocomp,
        check_media_type=not bypass_checks,
        check_width=not bypass_checks,
    )

    print(f"=> Sending image data ({raster_lines} lines)...")
    sys.stdout.write('[')
    for line in encode_raster_transfer(data, nocomp):
        sys.stdout.write('.' if line[0:1] == b'Z' else '*')
        sys.stdout.flush()
        ch.write(line)
    sys.stdout.write(']\n')

    if not no_print:
        ch.write(ptcbp.serialize_control('print'))
        print("=> Printing (waiting up to 12s)...")
        completion = ch.send_and_recv(n_response=32, read_timeout=12.0)
        if len(completion) == 32:
            ptstatus.print_status(ptstatus.unpack_status(completion))
        print("=> Done.")
    else:
        ch.send_and_recv(n_response=0)
        print("=> Image sent (no-print mode).")


if __name__ == '__main__':
    import argparse

    p = argparse.ArgumentParser(description='Print to PT-P300BT via bt_rfcomm Swift helper')
    p.add_argument('--mac', required=True, help='Printer Bluetooth MAC address')
    p.add_argument('--tape-width', type=int, default=12)
    p.add_argument('--media-type', choices=list(MEDIA_TYPE_MAP), default='any')
    p.add_argument('-n', '--no-print', action='store_true')
    p.add_argument('-F', '--no-feed', action='store_true')
    p.add_argument('-C', '--nocomp', action='store_true')
    p.add_argument('-m', '--end-margin', type=int, default=0)
    p.add_argument('-i', '--image', help='PNG to print directly')
    args = p.parse_args()

    if args.image:
        data = read_png(args.image)
    else:
        p.error('Use -i IMAGE (generate with: printlabel.py -S output.png ...)')

    do_print_job_swift(
        args.mac, data,
        tape_width=args.tape_width,
        media_type_name=args.media_type,
        no_feed=args.no_feed,
        end_margin=args.end_margin,
        nocomp=args.nocomp,
        no_print=args.no_print,
    )
