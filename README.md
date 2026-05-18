# CubePrint

A macOS app and command-line toolkit for printing labels on the **Brother PT-P300BT** ("P-touch Cube") Bluetooth label printer.

---

## Why this exists

The PT-P300BT communicates over Classic Bluetooth (SPP/RFCOMM). On modern macOS the virtual serial port (`/dev/cu.PT-P300BT*`) is write-only — reads return nothing, so you never get a status response and print jobs silently fail.

[Ircama's PT-P300BT](https://github.com/Ircama/PT-P300BT) provided invaluable inspiration and protocol documentation, but it couldn't run on macOS due to this Bluetooth limitation. CubePrint is a ground-up macOS implementation that works around the problem with a small Swift helper (`bt_rfcomm`) that talks directly to `IOBluetooth` RFCOMM, giving true bidirectional communication. On top of that sits a native macOS `.app` with live label preview, font picker, templates, and batch printing.

---

## Features

- **Live preview** — see exactly what will print before you commit
- **Font picker** — search across all system fonts; Bold/Italic automatically resolved to the correct font variant file
- **Custom font browser** — pick any static TTF/OTF file directly; remembered across sessions
- **Google Sans Code** — included (OFL licence); mono and proportional variants, Regular/Bold/Italic/BoldItalic
- **Tape presets** — 12 mm laminated and 6 mm heatshrink (HS-211) out of the box
- **Fixed label length** — set an exact length in mm with centred text
- **Margin control** — configure leading/trailing margin in mm (important for heatshrink tube)
- **Templates** — save and reload any combination of settings
- **Batch printing** — load a `.txt` file (one label per line), confirm count, print the whole run with minimal tape waste between labels
- **`bt_rfcomm` Swift bridge** — handles the macOS Classic Bluetooth gap; compiled binary included, source provided to rebuild

---

## Requirements

- macOS 12 or later
- Python 3.10+
- Xcode Command Line Tools (only needed to recompile `bt_rfcomm` from source)
- The PT-P300BT paired in **System Settings → Bluetooth** before first use

---

## Setup

```bash
git clone https://github.com/nigelsnoad/CubePrint.git
cd CubePrint

# Create a virtual environment and install dependencies
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`bt_rfcomm` is included as a pre-compiled binary so you can start immediately.
To rebuild it yourself (requires Xcode CLT):

```bash
make bt_rfcomm
```

---

## Running the app

Double-click `CubePrint.app`, or from the terminal:

```bash
open CubePrint.app
# or
.venv/bin/python3 gui.py
```

---

## Fonts

### System fonts
The font picker searches `/System/Library/Fonts`, `/Library/Fonts`, and `~/Library/Fonts` automatically. Type in the search box to filter.

### Bundled font — Google Sans Code
`Google_Sans_Code/static/` contains eight static variants (Regular, Bold, Italic, BoldItalic × Mono and Proportional). These are included under the [SIL Open Font Licence 1.1](Google_Sans_Code/OFL.txt).

### Custom fonts
Click **Browse…** next to the font picker to load any TTF, OTF, or TTC file from anywhere on your Mac. The path is remembered and the font reappears on next launch.

> **Static fonts only.** Variable-font `.ttf` files (the kind with a `fvar` table) are not supported — Pillow's `ImageFont.truetype()` cannot use them. Download the "Static" zip from Google Fonts, or pick a traditional single-weight TTF.

Bold/Italic variants are resolved automatically: if you load `MyFont-Regular.ttf` and tick **Bold**, CubePrint looks for `MyFont-Bold.ttf` in the same folder.

---

## Command-line usage

The CLI is a two-step pipeline: **render** with `printlabel.py`, then **send** with `bt_serial.py`.

### Render a label to PNG

```bash
.venv/bin/python3 printlabel.py \
  --tape-width 12 \
  --fixed-font-size 32 \
  -n -S output.png \
  /dev/null /path/to/font.ttf "My Label"
```

`/dev/null` is the dummy COM port (required positional argument; ignored when `-n` is set).  
`-n` skips printing; `-S output.png` saves the rendered image.

### Send a PNG to the printer

```bash
.venv/bin/python3 bt_serial.py \
  --mac 98:6E:E8:4C:11:92 \
  --tape-width 12 \
  --media-type laminated \
  -i output.png
```

Replace the MAC address with your printer's (visible in System Settings → Bluetooth).

### Full option reference

```
printlabel.py --help
bt_serial.py  --help
```

---

## How it works

### Bluetooth bridge (`bt_rfcomm`)

A small Swift command-line tool with `NSBluetoothAlwaysUsageDescription` embedded in its `__TEXT,__info_plist` section. This satisfies macOS TCC and allows it to open an IOBluetooth RFCOMM channel directly. It accepts a batch of hex bytes, sends them to the printer, waits for a response, and returns the response as hex on stdout.

```
bt_rfcomm <mac_address> <hex_bytes> [bytes_to_read] [timeout_secs]
```

### Print pipeline

1. `printlabel.py` renders text to a PNG using Pillow and a TTF/OTF font
2. `bt_serial.py` wraps `bt_rfcomm` with a serial-port-like interface
3. `labelmaker.py` configures the printer (media type, tape width, compression) via the PT-CBP raster protocol
4. `labelmaker_encode.py` converts the PNG to 1 bpp raster lines with PackBits RLE compression

### Protocol

The PT-P300BT uses Brother's PT-CBP (P-touch Control Block Protocol). Key commands:

| Command | Bytes |
|---------|-------|
| Reset | `\x1b@` |
| Use PT-CBP mode | `\x1bia\x01` |
| Get status | `\x1biS` |
| Set print parameters | `\x1biz` + 10 bytes |
| Set page mode | `\x1biM` + 1 byte |
| Set page margin | `\x1bid` + uint16 LE |
| Raster data (RLE) | `G` + uint16 LE + payload |
| Blank raster line | `Z` |
| Print + feed | `\x1a` |

---

## Tape settings reference

| Tape | `--tape-width` | `--media-type` | Suggested font size |
|------|---------------|----------------|---------------------|
| 12 mm laminated (TZe-231) | 12 | `laminated` | 32–48 pt |
| 6 mm heatshrink (HS-211) | 6 | `heatshrink` | 20–22 pt |

---

## Project layout

```
CubePrint/
├── gui.py                  # macOS GUI (tkinter)
├── printlabel.py           # Text → PNG renderer
├── bt_serial.py            # PNG → printer (via bt_rfcomm)
├── bt_rfcomm               # Compiled Swift Bluetooth bridge
├── bt_rfcomm.swift         # Source for the above
├── labelmaker.py           # PT-CBP printer configuration
├── labelmaker_encode.py    # PNG → 1 bpp raster lines
├── ptcbp.py                # PT-CBP command serialisation
├── ptstatus.py             # Status packet parser
├── Makefile                # Rebuilds bt_rfcomm
├── requirements.txt
├── Google_Sans_Code/       # Bundled font (OFL)
└── CubePrint.app/          # Runnable macOS app bundle
```

---

## Acknowledgements

Protocol reverse-engineering and original Python implementation by
[Ircama](https://github.com/Ircama/PT-P300BT) — without that groundwork,
understanding the PT-CBP wire format would have taken much longer.
CubePrint's protocol files are an independent reimplementation based on
the same public specification.

---

## License

MIT — see [LICENSE](LICENSE).
