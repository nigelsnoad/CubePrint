#!/usr/bin/env python3
"""
Print to PT-P300BT using IOBluetooth RFCOMM directly.
Bypasses the macOS virtual serial port which is write-only.
"""

import sys
import time
import threading
import objc
from Foundation import NSObject, NSRunLoop, NSDate
import IOBluetooth

from labelmaker_encode import encode_raster_transfer
from labelmaker import configure_printer, reset_printer, MEDIA_TYPE_MAP
import ptcbp

PRINTER_MAC = "98:6E:E8:4C:11:92"


class RFCOMMDelegate(NSObject):
    """Receives callbacks from the IOBluetooth RFCOMM channel."""

    def init(self):
        self = objc.super(RFCOMMDelegate, self).init()
        self._received = bytearray()
        self._event = threading.Event()
        self._channel_open = threading.Event()
        self._error = None
        return self

    def rfcommChannelOpenComplete_status_(self, channel, status):
        if status == 0:
            print("=> RFCOMM channel opened")
            self._channel_open.set()
        else:
            self._error = f"RFCOMM open failed with status {status}"
            self._channel_open.set()

    def rfcommChannelData_data_length_(self, channel, data, length):
        import ctypes
        buf = (ctypes.c_uint8 * length).from_address(ctypes.addressof(data.contents))
        self._received.extend(bytes(buf))
        self._event.set()

    def rfcommChannelClosed_(self, channel):
        print("=> RFCOMM channel closed")
        self._event.set()

    def rfcommChannelWriteComplete_refcon_status_(self, channel, refcon, status):
        pass  # writes are fire-and-forget

    def wait_open(self, timeout=10.0):
        return self._channel_open.wait(timeout)

    def read_bytes(self, n_bytes, timeout=3.0):
        deadline = time.time() + timeout
        while len(self._received) < n_bytes:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            self._event.clear()
            # Spin the run loop briefly to allow callbacks
            NSRunLoop.currentRunLoop().runUntilDate_(
                NSDate.dateWithTimeIntervalSinceNow_(0.05)
            )
            self._event.wait(0.05)
        result = bytes(self._received[:n_bytes])
        del self._received[:n_bytes]
        return result

    def drain(self, timeout=0.5):
        """Read and discard any pending bytes."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            NSRunLoop.currentRunLoop().runUntilDate_(
                NSDate.dateWithTimeIntervalSinceNow_(0.05)
            )
        return bytes(self._received)


class BTChannel:
    """Wraps an IOBluetooth RFCOMM channel with a serial-port-like interface."""

    def __init__(self, channel, delegate):
        self._ch = channel
        self._delegate = delegate

    def write(self, data):
        if isinstance(data, (bytes, bytearray)):
            data = bytes(data)
        status = self._ch.writeSync_length_(data, len(data))
        if status != 0:
            raise IOError(f"RFCOMM write failed: {status}")

    def flush(self):
        NSRunLoop.currentRunLoop().runUntilDate_(
            NSDate.dateWithTimeIntervalSinceNow_(0.01)
        )

    def read(self, n, timeout=3.0):
        return self._delegate.read_bytes(n, timeout)

    @property
    def timeout(self):
        return None

    @timeout.setter
    def timeout(self, value):
        pass  # ignored — reads always use explicit timeout


def _mac_str_to_device(mac_str):
    """Convert a MAC address string to an IOBluetoothDevice using ctypes."""
    import ctypes, ctypes.util
    from Foundation import NSAutoreleasePool

    pool = NSAutoreleasePool.alloc().init()

    libobjc = ctypes.CDLL(ctypes.util.find_library('objc'))
    iobluetooth = ctypes.CDLL('/System/Library/Frameworks/IOBluetooth.framework/IOBluetooth')

    libobjc.objc_getClass.restype = ctypes.c_void_p
    libobjc.sel_registerName.restype = ctypes.c_void_p
    libobjc.objc_msgSend.restype = ctypes.c_void_p

    class BTAddrStruct(ctypes.Structure):
        _fields_ = [("data", ctypes.c_uint8 * 6)]

    # Create NSString for MAC address
    NSStringCls = libobjc.objc_getClass(b'NSString')
    sel_utf8 = libobjc.sel_registerName(b'stringWithUTF8String:')
    libobjc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p]
    mac_ns = libobjc.objc_msgSend(NSStringCls, sel_utf8, mac_str.lower().encode())

    # Parse MAC address string into BluetoothDeviceAddress struct
    bt_addr = BTAddrStruct()
    iobluetooth.IOBluetoothNSStringToDeviceAddress.restype = ctypes.c_int
    iobluetooth.IOBluetoothNSStringToDeviceAddress.argtypes = [ctypes.c_void_p, ctypes.POINTER(BTAddrStruct)]
    ret = iobluetooth.IOBluetoothNSStringToDeviceAddress(mac_ns, ctypes.byref(bt_addr))
    if ret != 0:
        raise RuntimeError(f"IOBluetoothNSStringToDeviceAddress failed: {ret}")

    # Get IOBluetoothDevice via withAddress: (requires Bluetooth permission)
    DevCls = libobjc.objc_getClass(b'IOBluetoothDevice')
    sel_wa = libobjc.sel_registerName(b'withAddress:')
    _send = libobjc.objc_msgSend
    _send.restype = ctypes.c_void_p
    _send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(BTAddrStruct)]
    dev_ptr = _send(DevCls, sel_wa, ctypes.byref(bt_addr))
    if not dev_ptr:
        raise RuntimeError(f"Device not found: {mac_str}")

    # Wrap raw pointer as PyObjC object
    DevObjC = objc.lookUpClass('IOBluetoothDevice')
    device = objc.objc_object(c_instance=dev_ptr)
    del pool
    return device


def open_rfcomm(mac_str, channel_id=1, timeout=15.0):
    """Open an RFCOMM channel to the device at mac_str."""
    device = _mac_str_to_device(mac_str)
    if device is None:
        raise RuntimeError(f"Device not found: {mac_str}")

    print(f"=> Connecting to {device.name()} ({mac_str})...")

    delegate = RFCOMMDelegate.alloc().init()

    # Find SPP channel via SDP
    spp_uuid = IOBluetooth.IOBluetoothSDPUUID.uuidWithBytes_length_(
        bytes([0x00, 0x00, 0x11, 0x01,
               0x00, 0x00, 0x10, 0x00,
               0x80, 0x00, 0x00, 0x80, 0x5F, 0x9B, 0x34, 0xFB]), 16
    )
    services = device.getServiceRecordForUUID_(spp_uuid)
    if services:
        ch_attr = services.getAttributeDataAtIndex_(0)
        if ch_attr:
            channel_id = int.from_bytes(ch_attr.bytes(), 'big')
            print(f"=> Found SPP on RFCOMM channel {channel_id}")

    status = device.openRFCOMMChannelAsync_withChannelID_delegate_(
        None, channel_id, delegate
    )

    if not delegate.wait_open(timeout):
        raise RuntimeError("Timed out waiting for RFCOMM channel to open")
    if delegate._error:
        raise RuntimeError(delegate._error)

    # Get the opened channel
    channels = device.getRFCOMMChannels()
    if not channels or len(channels) == 0:
        raise RuntimeError("No RFCOMM channels found after opening")

    ch = channels[0]
    return BTChannel(ch, delegate), delegate


def do_print_job_bt(bt, delegate, data, tape_width=12, media_type_name='any',
                    no_feed=False, auto_cut=False, end_margin=0, nocomp=False,
                    no_print=False):

    media_type = MEDIA_TYPE_MAP.get(media_type_name, 0x00)
    bypass_checks = media_type == 0x00
    raster_lines = len(data) // 16

    print(f"=> Querying printer status...")
    bt.write(b"\x00" * 64)
    bt.write(ptcbp.serialize_control('reset'))
    bt.flush()
    NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.3))

    bt.write(ptcbp.serialize_control('get_status'))
    bt.flush()
    NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.3))

    status_bytes = delegate.read_bytes(32, timeout=3.0)
    if len(status_bytes) == 32:
        import ptstatus
        status = ptstatus.unpack_status(status_bytes)
        ptstatus.print_status(status)
        print(f"=> Tape: {status.tape_width}mm, type 0x{status.tape_type:02x}")
        if not bypass_checks:
            tape_width = status.tape_width
            media_type = status.tape_type
    else:
        print(f"=> Got {len(status_bytes)}/32 status bytes — using supplied values")

    print(f"=> Configuring: {tape_width}mm, media={media_type_name}, {raster_lines} lines")
    configure_printer(
        bt, raster_lines, (media_type, tape_width, 0),
        chaining=no_feed,
        auto_cut=auto_cut,
        end_margin=end_margin,
        compress=not nocomp,
        check_media_type=not bypass_checks,
        check_width=not bypass_checks,
    )
    bt.flush()

    print(f"=> Sending image data ({raster_lines} lines)...")
    sys.stdout.write('[')
    for line in encode_raster_transfer(data, nocomp):
        sys.stdout.write('.' if line[0:1] == b'Z' else '*')
        sys.stdout.flush()
        bt.write(line)
    sys.stdout.write(']\n')
    bt.flush()

    if not no_print:
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.2))
        print("=> Sending print command...")
        bt.write(ptcbp.serialize_control('print'))
        bt.flush()
        print("=> Waiting for print to complete...")
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(6.0))

        # Try to read completion status
        completion = delegate.read_bytes(32, timeout=2.0)
        if len(completion) == 32:
            import ptstatus
            ptstatus.print_status(ptstatus.unpack_status(completion))
        print("=> Done.")
    else:
        print("=> Image sent (no-print mode).")


if __name__ == '__main__':
    import argparse
    from labelmaker_encode import read_png

    p = argparse.ArgumentParser(description='Print to PT-P300BT via IOBluetooth RFCOMM')
    p.add_argument('--mac', default=PRINTER_MAC, help='Printer Bluetooth MAC address')
    p.add_argument('--tape-width', type=int, default=12)
    p.add_argument('--media-type', choices=list(MEDIA_TYPE_MAP), default='any')
    p.add_argument('-n', '--no-print', action='store_true')
    p.add_argument('-F', '--no-feed', action='store_true')
    p.add_argument('-C', '--nocomp', action='store_true')
    p.add_argument('-m', '--end-margin', type=int, default=0)
    p.add_argument('-i', '--image', help='PNG image to print directly')
    args = p.parse_args()

    if args.image:
        data = read_png(args.image)
    else:
        p.error('Use -i IMAGE for now (text rendering: use printlabel.py -S to save PNG first)')

    bt, delegate = open_rfcomm(args.mac)
    try:
        do_print_job_bt(
            bt, delegate, data,
            tape_width=args.tape_width,
            media_type_name=args.media_type,
            no_feed=args.no_feed,
            end_margin=args.end_margin,
            nocomp=args.nocomp,
            no_print=args.no_print,
        )
    finally:
        bt._ch.closeChannel()
