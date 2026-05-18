import Foundation
import IOBluetooth

// Communicates with PT-P300BT over IOBluetooth RFCOMM.
// Usage: bt_rfcomm <mac_address> <bytes_hex>
// Output: printer status bytes as hex on stdout, error on stderr.

class RFCOMMDelegate: NSObject, IOBluetoothRFCOMMChannelDelegate {
    var received = Data()
    var opened = false
    var error: String?
    let openedSem = DispatchSemaphore(value: 0)
    let dataSem = DispatchSemaphore(value: 0)

    func rfcommChannelOpenComplete(_ rfcommChannel: IOBluetoothRFCOMMChannel!, status error: IOReturn) {
        if error == kIOReturnSuccess {
            opened = true
        } else {
            self.error = "RFCOMM open failed: \(error)"
        }
        openedSem.signal()
    }

    func rfcommChannelData(_ rfcommChannel: IOBluetoothRFCOMMChannel!, data dataPointer: UnsafeMutableRawPointer!, length dataLength: Int) {
        received.append(Data(bytes: dataPointer, count: dataLength))
        dataSem.signal()
    }

    func rfcommChannelClosed(_ rfcommChannel: IOBluetoothRFCOMMChannel!) {
        dataSem.signal()
    }
}

func hexToData(_ hex: String) -> Data? {
    var data = Data()
    var hex = hex
    while hex.count >= 2 {
        let byte = hex.prefix(2)
        hex = String(hex.dropFirst(2))
        guard let b = UInt8(byte, radix: 16) else { return nil }
        data.append(b)
    }
    return data
}

guard CommandLine.arguments.count >= 3 else {
    fputs("Usage: bt_rfcomm <mac> <hex_bytes_to_send> [bytes_to_read]\n", stderr)
    exit(1)
}

let macStr = CommandLine.arguments[1]
let hexData = CommandLine.arguments[2]
let bytesToRead = CommandLine.arguments.count >= 4 ? Int(CommandLine.arguments[3]) ?? 32 : 32
let readTimeout  = CommandLine.arguments.count >= 5 ? Double(CommandLine.arguments[4]) ?? 3.0 : 3.0

guard let sendData = hexToData(hexData) else {
    fputs("Invalid hex data\n", stderr)
    exit(1)
}

// Find device
guard let device = IOBluetoothDevice(addressString: macStr) else {
    fputs("Device not found: \(macStr)\n", stderr)
    exit(1)
}

fputs("=> Connecting to \(device.nameOrAddress ?? macStr)...\n", stderr)

// Close any existing connection (e.g. held open by /dev/cu.* serial port driver)
if device.isConnected() {
    fputs("=> Closing existing connection...\n", stderr)
    device.closeConnection()
    RunLoop.current.run(until: Date(timeIntervalSinceNow: 1.5))
}

let channelID: BluetoothRFCOMMChannelID = 1  // PT-P300BT uses RFCOMM channel 1
fputs("=> Using RFCOMM channel \(channelID)\n", stderr)

let delegate = RFCOMMDelegate()
var rfcommChannel: IOBluetoothRFCOMMChannel?

let openStatus = device.openRFCOMMChannelAsync(&rfcommChannel, withChannelID: channelID, delegate: delegate)
guard openStatus == kIOReturnSuccess else {
    fputs("openRFCOMMChannelAsync failed: \(openStatus)\n", stderr)
    exit(1)
}

// Wait for channel to open (with run loop)
let deadline = Date(timeIntervalSinceNow: 10.0)
while delegate.openedSem.wait(timeout: .now() + .milliseconds(50)) == .timedOut {
    RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.05))
    if Date() > deadline { break }
}

if let err = delegate.error {
    fputs("Error: \(err)\n", stderr)
    exit(1)
}
guard delegate.opened, let channel = rfcommChannel else {
    fputs("Failed to open RFCOMM channel\n", stderr)
    exit(1)
}

fputs("=> Channel open. Sending \(sendData.count) bytes...\n", stderr)

// Send data in chunks (RFCOMM MTU is typically 1011 bytes)
let mtu = Int(channel.getMTU())
fputs("=> MTU: \(mtu)\n", stderr)
var offset = 0
var sendBytes = [UInt8](sendData)
while offset < sendBytes.count {
    let chunkSize = min(mtu > 0 ? mtu : 512, sendBytes.count - offset)
    var chunk = Array(sendBytes[offset..<offset+chunkSize])
    let writeStatus = channel.writeSync(&chunk, length: UInt16(chunkSize))
    if writeStatus != kIOReturnSuccess {
        fputs("Write failed at offset \(offset): \(writeStatus)\n", stderr)
        break
    }
    offset += chunkSize
    RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.005))
}

// Wait for response
fputs("=> Waiting for \(bytesToRead) response bytes (timeout \(readTimeout)s)...\n", stderr)
let readDeadline = Date(timeIntervalSinceNow: readTimeout)
while delegate.received.count < bytesToRead && Date() < readDeadline {
    _ = delegate.dataSem.wait(timeout: .now() + .milliseconds(50))
    RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.05))
}

// Output received bytes as hex
let hex = delegate.received.map { String(format: "%02x", $0) }.joined()
print(hex)
fputs("=> Received \(delegate.received.count)/\(bytesToRead) bytes\n", stderr)

channel.close()
exit(0)
