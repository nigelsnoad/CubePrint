#!/usr/bin/env python3
"""BTLabelPrinter — GUI for the PT-P300BT label printer."""

import os, sys, subprocess, tempfile, threading, fcntl, io, contextlib
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk

if getattr(sys, 'frozen', False):
    # Running inside PyInstaller bundle
    _RES           = Path(sys._MEIPASS)
    PROJECT_DIR    = _RES
    PYTHON         = sys.executable
    PRINTLABEL     = str(_RES / 'printlabel.py')
    BT_SERIAL      = str(_RES / 'bt_serial.py')
    FONTS_DIR      = _RES / 'fonts'
    _APP_SUPPORT   = Path.home() / 'Library' / 'Application Support' / 'CubePrint'
    TEMPLATES_FILE = _APP_SUPPORT / 'templates.json'
    SETTINGS_FILE  = _APP_SUPPORT / 'settings.json'
else:
    PROJECT_DIR    = Path(__file__).parent.resolve()
    sys.path.insert(0, str(PROJECT_DIR))
    PYTHON         = str(PROJECT_DIR / '.venv/bin/python3')
    PRINTLABEL     = str(PROJECT_DIR / 'printlabel.py')
    BT_SERIAL      = str(PROJECT_DIR / 'bt_serial.py')
    FONTS_DIR      = PROJECT_DIR / 'fonts'
    TEMPLATES_FILE = PROJECT_DIR / 'templates' / 'templates.json'
    SETTINGS_FILE  = PROJECT_DIR / 'templates' / 'settings.json'

TAPE_PRESETS = [
    ("12mm Laminated (White)",  {'tape_width': 12, 'media_type': 'laminated'}),
    ("6mm Heatshrink (HS-211)", {'tape_width': 6,  'media_type': 'heatshrink'}),
]

FONT_EXTS = {'.ttf', '.otf', '.ttc'}

# ── font discovery ────────────────────────────────────────────────────────────

def _scan_dir(d, results, seen):
    d = Path(d)
    if not d.is_dir():
        return
    for p in sorted(d.rglob('*')):
        if p.suffix.lower() not in FONT_EXTS:
            continue
        name = p.stem.replace('-', ' ').replace('_', ' ')
        if name not in seen:
            results.append((name, str(p)))
            seen.add(name)

def scan_fonts():
    results, seen = [], set()
    # Bundled / user-dropped fonts in the local fonts/ directory
    # Special-case display names for the included Google Sans Code variants
    static = FONTS_DIR / 'Google_Sans_Code' / 'static'
    for name, stem in [
        ('Google Sans Code',      'GoogleSansCode-Regular'),
        ('Google Sans Code Prop', 'GoogleSansCode_Proportional-Regular'),
    ]:
        p = static / f'{stem}.ttf'
        if p.exists():
            results.append((name, str(p)))
            seen.add(name)
    # Any other fonts dropped into fonts/ (excluding the Google_Sans_Code subtree
    # already handled above)
    if FONTS_DIR.is_dir():
        for sub in sorted(FONTS_DIR.iterdir()):
            if sub.name == 'Google_Sans_Code':
                continue  # already handled
            _scan_dir(sub if sub.is_dir() else sub.parent, results, seen)
    # System fonts
    for d in ('/System/Library/Fonts', '/Library/Fonts',
              str(Path.home() / 'Library/Fonts')):
        _scan_dir(d, results, seen)
    return results  # [(display_name, path), ...]


_STYLE_STRIP = [
    '-Regular', '_Regular', ' Regular',
    '-Normal',  '_Normal',  ' Normal',
]
_BOLD_SUFFIXES   = ['-Bold', 'Bold', '_Bold', ' Bold']
_ITALIC_SUFFIXES = ['-Italic', 'Italic', '_Italic', ' Italic',
                    '-Oblique', 'Oblique', '_Oblique', ' Oblique']
_BOLDITALIC_SUFFIXES = [
    '-BoldItalic', 'BoldItalic', '_BoldItalic', ' BoldItalic',
    '-Bold-Italic', '-BoldOblique', 'BoldOblique',
]

def find_font_variant(base_path, bold, italic):
    """Return path to Bold/Italic/BoldItalic variant, falling back to base_path."""
    if not bold and not italic:
        return base_path
    p = Path(base_path)
    stem = p.stem
    for s in _STYLE_STRIP:
        if stem.endswith(s):
            stem = stem[:-len(s)]
            break
    candidates = _BOLDITALIC_SUFFIXES if (bold and italic) \
                 else (_BOLD_SUFFIXES if bold else _ITALIC_SUFFIXES)
    for suf in candidates:
        for ext in (p.suffix, '.ttf', '.otf'):
            variant = p.parent / f'{stem}{suf}{ext}'
            if variant.exists():
                return str(variant)
    return base_path


# ── render helper ─────────────────────────────────────────────────────────────

def _run_render(args_list):
    """Run printlabel rendering. In frozen mode calls in-process; otherwise subprocess.

    args_list[0] is the script path (used as argv[0] / skipped in frozen mode).
    In subprocess mode PYTHON is prepended so the script runs as `python3 script.py …`.
    """
    if getattr(sys, 'frozen', False):
        sys.path.insert(0, str(PROJECT_DIR))
        import importlib, printlabel as _pl
        out, err = io.StringIO(), io.StringIO()
        rc = 0
        try:
            parsed = _pl.set_args().parse_args(args_list[1:])  # skip script name
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                _pl.render_image(parsed)
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 0
        except Exception as e:
            err.write(str(e))
            rc = 1
        return rc, out.getvalue(), err.getvalue()
    else:
        r = subprocess.run([PYTHON] + args_list, capture_output=True, timeout=10)
        return r.returncode, r.stdout.decode(errors='replace'), r.stderr.decode(errors='replace')


def _run_print(args_list):
    """Run bt_serial print job. In frozen mode calls in-process; otherwise subprocess.

    args_list[0] is the script path. In subprocess mode PYTHON is prepended.
    """
    if getattr(sys, 'frozen', False):
        sys.path.insert(0, str(PROJECT_DIR))
        import bt_serial as _bs
        from labelmaker_encode import read_png
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument('--mac', required=True)
        p.add_argument('--tape-width', type=int, default=12)
        p.add_argument('--media-type', default='any')
        p.add_argument('-F', '--no-feed', action='store_true')
        p.add_argument('-m', '--end-margin', type=int, default=0)
        p.add_argument('-i', '--image')
        args = p.parse_args(args_list[1:])
        out, err = io.StringIO(), io.StringIO()
        rc = 0
        try:
            data = read_png(args.image)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                _bs.do_print_job_swift(
                    args.mac, data,
                    tape_width=args.tape_width,
                    media_type_name=args.media_type,
                    no_feed=args.no_feed,
                    end_margin=args.end_margin,
                )
        except Exception as e:
            err.write(str(e))
            rc = 1
        return rc, out.getvalue(), err.getvalue()
    else:
        r = subprocess.run([PYTHON] + args_list, capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout, r.stderr


# ── wire label renderer ──────────────────────────────────────────────────────

def render_wire_label_image(text, font_path, font_size, total_mm=22.0,
                             tape_width_mm=12, cut_gap_mm=0.0):
    """Render a wire-wrap label image in display orientation.

    Text is drawn perpendicular to the tape run (rotated 90°):
    - Left block  → CCW (reads bottom-to-top when label is horizontal)
    - Right block → CW  (reads top-to-bottom)

    cut_gap_mm adds blank space at both the left and right ends of the label
    image so there is room to cut cleanly before and after.  The printer's
    natural non-printable border is sufficient for top/bottom clearance.

    Returns a PIL Image (RGB, white background) ready to save as a PNG and
    pass to read_png / bt_serial, the same as a normal label image.
    """
    from PIL import ImageFont, ImageDraw
    DPI = 180

    # Tape geometry — matches printlabel.py conventions
    printable_h  = round(tape_width_mm * 64 / 12)
    tape_h       = round(tape_width_mm * 86 / 12)
    img_h        = tape_h + 2
    print_border = (img_h - printable_h) / 2

    # No internal top/bottom margin — printer border is enough
    text_area_h  = max(4, printable_h)

    # Total label length in dots
    total_dots = max(4, round(total_mm * DPI / 25.4))

    font = ImageFont.truetype(font_path, font_size)

    # ── auto-wrap ──────────────────────────────────────────────────────────────
    # After 90° rotation, a line's *width* (upright) becomes its *height* in the
    # tape direction, so each line must have width ≤ text_area_h.
    def _wrap(raw, max_px):
        result = []
        for para in raw.replace('\\n', '\n').split('\n'):
            words = para.split()
            if not words:
                result.append('')
                continue
            current, current_w = [], 0
            for word in words:
                bb = font.getbbox(word, anchor='lt')
                ww = bb[2] - bb[0]
                sp = (font.getbbox(' ', anchor='lt')[2]) if current else 0
                if current and current_w + sp + ww > max_px:
                    result.append(' '.join(current))
                    current, current_w = [word], ww
                else:
                    current.append(word)
                    current_w += sp + ww
            if current:
                result.append(' '.join(current))
        return result or ['']

    lines = _wrap(text, text_area_h)

    # ── measure upright text block ─────────────────────────────────────────────
    line_bboxes = [font.getbbox(l, anchor='lt') if l.strip()
                   else (0, 0, 0, font_size) for l in lines]
    max_lw   = max(bb[2] - bb[0] for bb in line_bboxes) if line_bboxes else 0
    line_h   = max(bb[3] - bb[1] for bb in line_bboxes) if line_bboxes else font_size
    spacing  = round(line_h * 1.1)
    block_h  = spacing * (len(lines) - 1) + line_h if lines else 0

    # After rotation: max_lw → tape direction; block_h → label direction

    # ── draw text upright on a scratch canvas ──────────────────────────────────
    cw = max(max_lw + 2, 1)
    ch = max(block_h + 2, 1)
    scratch = Image.new('RGB', (cw, ch), 'white')
    draw    = ImageDraw.Draw(scratch)
    y_off   = 1
    for line in lines:
        if line.strip():
            draw.text((1, y_off), line, font=font, fill='black', anchor='lt')
        y_off += spacing

    # ── rotate ────────────────────────────────────────────────────────────────
    left_block  = scratch.rotate( 90, expand=True)   # CCW — reads up
    right_block = scratch.rotate(-90, expand=True)   # CW  — reads down

    # Clip to text_area_h in tape direction (safety)
    def _clip(img, max_h):
        if img.height > max_h:
            top = (img.height - max_h) // 2
            return img.crop((0, top, img.width, top + max_h))
        return img

    left_block  = _clip(left_block,  text_area_h)
    right_block = _clip(right_block, text_area_h)

    # ── compose label ─────────────────────────────────────────────────────────
    label = Image.new('RGB', (total_dots, img_h), 'white')

    def _y_pos(bh):
        return round(print_border + (text_area_h - bh) / 2)

    # Cut gap at both ends (minimum 2 px so blocks are never flush to the edge)
    PAD = max(2, round(cut_gap_mm * DPI / 25.4))

    label.paste(left_block,  (PAD, _y_pos(left_block.height)))

    rx = total_dots - right_block.width - PAD
    if rx >= 0:
        label.paste(right_block, (rx, _y_pos(right_block.height)))

    return label


# ── main app ──────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('CubePrint')
        self.resizable(False, False)
        # Set dock icon when running from source (bundle uses AppIcon.icns automatically)
        if not getattr(sys, 'frozen', False):
            _icon = PROJECT_DIR / 'docs' / 'CubePrint Icon.jpeg'
            if _icon.exists():
                try:
                    from AppKit import NSApplication, NSImage
                    NSApplication.sharedApplication().setApplicationIconImage_(
                        NSImage.alloc().initWithContentsOfFile_(str(_icon))
                    )
                except ImportError:
                    self.iconphoto(True, ImageTk.PhotoImage(Image.open(_icon).resize((64, 64))))

        self._all_fonts   = scan_fonts()
        self._font_map    = dict(self._all_fonts)
        _s = self._load_settings()
        self._custom_font_paths = _s.get('custom_fonts', [])
        self._add_custom_fonts(self._custom_font_paths)
        self._printer_mac = _s.get('printer_mac', '')
        self._last_labels_dir = _s.get('last_labels_dir',
                                       str(Path.home() / 'Documents'))
        self._after_id    = None
        self._photo       = None   # keep ref to prevent GC
        self._print_png   = None   # path reused by Print button
        self._printing    = False
        self.bold_var        = tk.BooleanVar(value=False)
        self.italic_var      = tk.BooleanVar(value=False)
        self.length_var      = tk.StringVar(value='')
        self.margin_var      = tk.StringVar(value='')
        self.wire_var        = tk.BooleanVar(value=False)
        self.wire_length_var = tk.StringVar(value='22')
        self.tmpl_var        = tk.StringVar(value='')
        self._templates   = self._load_templates()

        self._build()
        self._schedule_preview(delay=100)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build(self):
        P = dict(padx=6, pady=3)
        outer = ttk.Frame(self, padding=10)
        outer.grid(sticky='nsew')

        # --- Templates ---
        ttk.Label(outer, text='Template:').grid(row=0, column=0, sticky='e', **P)
        tmpl_frame = ttk.Frame(outer)
        tmpl_frame.grid(row=0, column=1, columnspan=2, sticky='ew', **P)
        self.tmpl_cb = ttk.Combobox(tmpl_frame, textvariable=self.tmpl_var,
                                    values=list(self._templates), state='readonly',
                                    width=20)
        self.tmpl_cb.pack(side='left', fill='x', expand=True)
        self.tmpl_cb.bind('<<ComboboxSelected>>', lambda _: self._on_tmpl_load())
        ttk.Button(tmpl_frame, text='Save…',
                   command=self._on_tmpl_save).pack(side='left', padx=(4, 2))
        ttk.Button(tmpl_frame, text='Delete',
                   command=self._on_tmpl_delete).pack(side='left')

        ttk.Separator(outer, orient='horizontal').grid(
            row=1, column=0, columnspan=3, sticky='ew', pady=(4, 2))

        # --- Printer MAC ---
        ttk.Label(outer, text='Printer:').grid(row=2, column=0, sticky='e', **P)
        mac_frame = ttk.Frame(outer)
        mac_frame.grid(row=2, column=1, columnspan=2, sticky='ew', **P)
        self.mac_var = tk.StringVar(value=self._printer_mac)
        mac_e = ttk.Entry(mac_frame, textvariable=self.mac_var, width=22)
        mac_e.pack(side='left')
        mac_e.bind('<FocusOut>', lambda _: self._on_mac_changed())
        mac_e.bind('<Return>',   lambda _: self._on_mac_changed())
        find_btn = ttk.Button(mac_frame, text='Find…', width=6,
                              command=self._find_printer_mac)
        find_btn.pack(side='left', padx=(4, 0))
        mac_help = tk.Label(mac_frame, text=' ?', cursor='hand2')
        mac_help.pack(side='left')
        mac_help.bind('<Button-1>', lambda _: self._open_help())

        # --- Tape preset ---
        ttk.Label(outer, text='Tape:').grid(row=3, column=0, sticky='e', **P)
        self.tape_var = tk.StringVar(value=TAPE_PRESETS[0][0])
        tape_cb = ttk.Combobox(outer, textvariable=self.tape_var,
                               values=[t[0] for t in TAPE_PRESETS],
                               state='readonly', width=32)
        tape_cb.grid(row=3, column=1, columnspan=2, sticky='ew', **P)
        tape_cb.bind('<<ComboboxSelected>>', lambda _: self._schedule_preview())

        # --- Font search ---
        ttk.Label(outer, text='Font:').grid(row=4, column=0, sticky='e', **P)
        self.font_search = tk.StringVar()
        search_e = ttk.Entry(outer, textvariable=self.font_search, width=22)
        search_e.grid(row=4, column=1, sticky='ew', **P)
        search_e.bind('<KeyRelease>', self._on_font_search)
        ttk.Label(outer, text='🔍').grid(row=4, column=2, sticky='w')

        all_names = [n for n, _ in self._all_fonts]
        self.font_var = tk.StringVar(value=all_names[0] if all_names else '')
        font_row = ttk.Frame(outer)
        font_row.grid(row=5, column=1, columnspan=2, sticky='ew', **P)
        self.font_cb = ttk.Combobox(font_row, textvariable=self.font_var,
                                    values=all_names, state='readonly', width=26)
        self.font_cb.pack(side='left', fill='x', expand=True)
        self.font_cb.bind('<<ComboboxSelected>>', lambda _: self._schedule_preview())
        ttk.Button(font_row, text='Browse…',
                   command=self._on_browse_font).pack(side='left', padx=(4, 0))

        # --- Bold / Italic ---
        style_frame = ttk.Frame(outer)
        style_frame.grid(row=6, column=1, sticky='w', **P)
        ttk.Checkbutton(style_frame, text='Bold', variable=self.bold_var,
                        command=self._schedule_preview).pack(side='left', padx=(0, 8))
        ttk.Checkbutton(style_frame, text='Italic', variable=self.italic_var,
                        command=self._schedule_preview).pack(side='left')
        ttk.Label(style_frame, text='  (static fonts only)').pack(side='left')

        # --- Font size ---
        ttk.Label(outer, text='Size (pt):').grid(row=7, column=0, sticky='e', **P)
        self.size_var = tk.StringVar(value='32')
        size_sp = ttk.Spinbox(outer, from_=6, to=400, textvariable=self.size_var,
                              width=7, command=lambda: self._schedule_preview())
        size_sp.grid(row=7, column=1, sticky='w', **P)
        size_sp.bind('<KeyRelease>', lambda _: self._schedule_preview())

        # --- Text ---
        ttk.Label(outer, text='Text:').grid(row=8, column=0, sticky='e', **P)
        self.text_var = tk.StringVar(value='Test')
        text_e = ttk.Entry(outer, textvariable=self.text_var, width=32)
        text_e.grid(row=8, column=1, columnspan=2, sticky='ew', **P)
        self.text_var.trace_add('write', lambda *_: self._schedule_preview())

        # --- Label length + margin ---
        ttk.Label(outer, text='Length (mm):').grid(row=9, column=0, sticky='e', **P)
        lm_frame = ttk.Frame(outer)
        lm_frame.grid(row=9, column=1, columnspan=2, sticky='w', **P)
        ttk.Entry(lm_frame, textvariable=self.length_var, width=6).pack(side='left')
        ttk.Label(lm_frame, text='  Margin/cut gap (mm):').pack(side='left')
        ttk.Entry(lm_frame, textvariable=self.margin_var, width=6).pack(side='left')
        ttk.Label(lm_frame, text='  (blank = defaults)').pack(side='left')
        self.length_var.trace_add('write', lambda *_: self._schedule_preview())
        self.margin_var.trace_add('write', lambda *_: self._schedule_preview())

        # --- Wire label mode ---
        wire_row = ttk.Frame(outer)
        wire_row.grid(row=10, column=0, columnspan=3, sticky='w', **P)
        ttk.Checkbutton(wire_row, text='Wire Label', variable=self.wire_var,
                        command=self._on_wire_toggle).pack(side='left')
        ttk.Label(wire_row, text='  Total length (mm):').pack(side='left')
        self.wire_length_entry = ttk.Entry(wire_row, textvariable=self.wire_length_var,
                                           width=6, state='disabled')
        self.wire_length_entry.pack(side='left')
        ttk.Label(wire_row, text='  (text rotated 90° at each end)').pack(side='left')
        self.wire_length_var.trace_add('write', lambda *_: self._schedule_preview())

        # --- Separator ---
        ttk.Separator(outer, orient='horizontal').grid(
            row=11, column=0, columnspan=3, sticky='ew', pady=6)

        # --- Preview ---
        PREVIEW_MAX_W = 500
        ttk.Label(outer, text='Preview:').grid(row=12, column=0, sticky='ne',
                                               padx=6, pady=3)
        preview_outer = ttk.Frame(outer, relief='sunken', borderwidth=1)
        preview_outer.grid(row=12, column=1, columnspan=2, sticky='ew',
                           padx=0, pady=3)
        self._preview_canvas = tk.Canvas(preview_outer, background='white',
                                         width=PREVIEW_MAX_W, height=80,
                                         highlightthickness=0)
        self._preview_canvas.pack(side='top', fill='x')
        self._preview_scroll = ttk.Scrollbar(preview_outer, orient='horizontal',
                                             command=self._preview_canvas.xview)
        self._preview_canvas.configure(xscrollcommand=self._preview_scroll.set)

        # --- Status + print ---
        self.status_var = tk.StringVar(value='Ready.')
        self._status_lbl = ttk.Label(outer, textvariable=self.status_var, width=40)
        self._status_lbl.grid(row=13, column=0, columnspan=2, sticky='w', padx=6, pady=6)
        btn_frame = ttk.Frame(outer)
        btn_frame.grid(row=13, column=2, sticky='e', pady=6, padx=6)
        help_lbl = tk.Label(btn_frame, text='?', cursor='hand2')
        help_lbl.pack(side='left', padx=(0, 8))
        help_lbl.bind('<Button-1>', lambda _: self._open_help())
        self.file_btn = ttk.Button(btn_frame, text='Bulk Labels…',
                                   command=self._on_print_file)
        self.file_btn.pack(side='left', padx=(0, 4))
        self.print_btn = ttk.Button(btn_frame, text='  Print  ', command=self._on_print)
        self.print_btn.pack(side='left')

    def _length_args(self):
        """Return --fixed-width / --center-text args if a length is set, else []."""
        val = self.length_var.get().strip()
        if not val:
            return []
        try:
            float(val)
        except ValueError:
            return []
        return ['--fixed-width', val, '--center-text']

    def _margin_args(self):
        """Return --h-padding N (dots) for printlabel.py if margin_var is set, else []."""
        val = self.margin_var.get().strip()
        if not val:
            return []
        try:
            mm = float(val)
        except ValueError:
            return []
        dots = max(1, round(mm * 180 / 25.4))
        return ['--h-padding', str(dots)]

    def _bt_margin_args(self):
        """Return --end-margin N (dots) for bt_serial.py if margin_var is set, else []."""
        val = self.margin_var.get().strip()
        if not val:
            return []
        try:
            mm = float(val)
        except ValueError:
            return []
        dots = max(0, round(mm * 180 / 25.4))
        return ['--end-margin', str(dots)]

    # ── templates ────────────────────────────────────────────────────────────

    def _load_templates(self):
        if TEMPLATES_FILE.exists():
            import json
            try:
                return json.loads(TEMPLATES_FILE.read_text())
            except Exception:
                pass
        return {}

    def _save_templates(self):
        import json
        TEMPLATES_FILE.parent.mkdir(parents=True, exist_ok=True)
        TEMPLATES_FILE.write_text(json.dumps(self._templates, indent=2))
        self.tmpl_cb.configure(values=list(self._templates))

    def _current_settings(self):
        return {
            'tape':        self.tape_var.get(),
            'font':        self.font_var.get(),
            'bold':        self.bold_var.get(),
            'italic':      self.italic_var.get(),
            'size':        self.size_var.get(),
            'length':      self.length_var.get(),
            'margin':      self.margin_var.get(),
            'text':        self.text_var.get(),
            'wire_label':  self.wire_var.get(),
            'wire_length': self.wire_length_var.get(),
        }

    def _apply_settings(self, s):
        if 'tape'   in s: self.tape_var.set(s['tape'])
        if 'font'   in s and s['font'] in self._font_map:
            self.font_var.set(s['font'])
        if 'bold'        in s: self.bold_var.set(s['bold'])
        if 'italic'      in s: self.italic_var.set(s['italic'])
        if 'size'        in s: self.size_var.set(str(s['size']))
        if 'length'      in s: self.length_var.set(s['length'])
        if 'margin'      in s: self.margin_var.set(s['margin'])
        if 'text'        in s: self.text_var.set(s['text'])
        if 'wire_label'  in s: self.wire_var.set(s['wire_label'])
        if 'wire_length' in s: self.wire_length_var.set(s['wire_length'])
        # Keep the wire length entry state in sync
        self._on_wire_toggle()
        self._schedule_preview()

    def _on_tmpl_load(self):
        name = self.tmpl_var.get()
        if name in self._templates:
            self._apply_settings(self._templates[name])

    def _on_tmpl_save(self):
        from tkinter.simpledialog import askstring
        name = askstring('Save Template', 'Template name:',
                         initialvalue=self.tmpl_var.get())
        if not name:
            return
        self._templates[name] = self._current_settings()
        self._save_templates()
        self.tmpl_var.set(name)

    def _on_tmpl_delete(self):
        name = self.tmpl_var.get()
        if not name or name not in self._templates:
            return
        if not messagebox.askyesno('Delete Template', f'Delete "{name}"?'):
            return
        del self._templates[name]
        self._save_templates()
        self.tmpl_var.set('')

    # ── font search ───────────────────────────────────────────────────────────

    def _on_font_search(self, _=None):
        q = self.font_search.get().lower()
        filtered = [n for n, _ in self._all_fonts if q in n.lower()]
        self.font_cb.configure(values=filtered)
        if filtered:
            self.font_var.set(filtered[0])
            self._schedule_preview()

    # ── custom font browser ───────────────────────────────────────────────────

    def _load_settings(self):
        if SETTINGS_FILE.exists():
            import json
            try:
                return json.loads(SETTINGS_FILE.read_text())
            except Exception:
                pass
        return {}

    def _save_settings(self, settings):
        import json
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2))

    def _find_printer_mac(self):
        """Run system_profiler to find paired PT-P300BT and fill the MAC field."""
        try:
            result = subprocess.run(
                ['system_profiler', 'SPBluetoothDataType'],
                capture_output=True, text=True, timeout=10)
            lines = result.stdout.splitlines()
            # Find device sections containing "PT-P300" and extract Address
            mac = None
            in_device = False
            for line in lines:
                if 'PT-P300' in line or 'PT-P' in line:
                    in_device = True
                if in_device and 'Address:' in line:
                    mac = line.split('Address:')[-1].strip()
                    break
                if in_device and line.strip() == '':
                    in_device = False
        except Exception:
            mac = None

        if mac:
            self.mac_var.set(mac)
            self._on_mac_changed()
            self.status_var.set(f'Found printer: {mac}')
        else:
            messagebox.showinfo(
                'Printer not found',
                'No PT-P300BT found in Bluetooth devices.\n\n'
                'Make sure the printer is powered on and paired in\n'
                'System Settings → Bluetooth, then try again.')

    def _open_help(self):
        import subprocess
        subprocess.run(['open', 'https://github.com/nigelsnoad/CubePrint#finding-your-printers-bluetooth-mac-address'])

    def _on_mac_changed(self):
        mac = self.mac_var.get().strip()
        self._printer_mac = mac
        s = self._load_settings()
        s['printer_mac'] = mac
        self._save_settings(s)

    def _get_printer_mac(self):
        """Return the stored printer MAC, prompting the user if not yet configured."""
        if self._printer_mac:
            return self._printer_mac
        from tkinter.simpledialog import askstring
        mac = askstring(
            'Printer MAC Address',
            'Enter the Bluetooth MAC address of your PT-P300BT\n(e.g. 98:6E:E8:4C:11:92):',
        )
        if not mac:
            return None
        mac = mac.strip()
        self._printer_mac = mac
        s = self._load_settings()
        s['printer_mac'] = mac
        self._save_settings(s)
        return mac

    def _add_custom_fonts(self, paths):
        """Register a list of font file paths, skipping any already known."""
        for path in paths:
            p = Path(path)
            if p.exists() and p.suffix.lower() in FONT_EXTS:
                name = p.stem.replace('-', ' ').replace('_', ' ')
                if name not in self._font_map:
                    self._all_fonts.append((name, str(p)))
                    self._font_map[name] = str(p)

    def _on_browse_font(self):
        path = filedialog.askopenfilename(
            title='Choose a font file  (static TTF / OTF only — no variable fonts)',
            filetypes=[
                ('Font files', '*.ttf *.otf *.ttc'),
                ('All files', '*.*'),
            ],
        )
        if not path:
            return
        p = Path(path)
        if p.suffix.lower() not in FONT_EXTS:
            messagebox.showwarning('Unsupported file',
                                   f'Please pick a .ttf, .otf, or .ttc file.')
            return
        name = p.stem.replace('-', ' ').replace('_', ' ')
        if name not in self._font_map:
            self._all_fonts.append((name, str(p)))
            self._font_map[name] = str(p)
            # Refresh combobox
            all_names = [n for n, _ in self._all_fonts]
            self.font_cb.configure(values=all_names)
            # Persist for next launch
            if str(p) not in self._custom_font_paths:
                self._custom_font_paths.append(str(p))
                s = self._load_settings()
                s['custom_fonts'] = self._custom_font_paths
                self._save_settings(s)
        # Select the chosen font and update preview
        self.font_var.set(name)
        self.font_search.set('')
        self._schedule_preview()

    # ── wire label toggle ─────────────────────────────────────────────────────

    def _on_wire_toggle(self):
        state = 'normal' if self.wire_var.get() else 'disabled'
        self.wire_length_entry.configure(state=state)
        self._schedule_preview()

    # ── preview ───────────────────────────────────────────────────────────────

    def _schedule_preview(self, delay=350):
        if self._after_id:
            self.after_cancel(self._after_id)
        self._after_id = self.after(delay, self._refresh_preview)

    def _refresh_preview(self):
        self._after_id = None

        font_name = self.font_var.get()
        font_path = self._font_map.get(font_name)
        tape_name = self.tape_var.get()
        tape_cfg  = dict(TAPE_PRESETS)[tape_name]
        text      = self.text_var.get().strip()

        try:
            size = int(self.size_var.get())
        except ValueError:
            return

        if not text or not font_path:
            self._set_preview_text('(enter text and choose a font)')
            return

        font_path = find_font_variant(font_path, self.bold_var.get(), self.italic_var.get())

        if not self._print_png:
            tmp = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
            self._print_png = tmp.name
            tmp.close()

        if self.wire_var.get():
            self._refresh_wire_preview(text, font_path, size, tape_cfg)
            return

        try:
            rc, stdout_txt, stderr_txt = _run_render(
                [PRINTLABEL,
                 '--tape-width', str(tape_cfg['tape_width']),
                 '--fixed-font-size', str(size),
                 *self._length_args(),
                 *self._margin_args(),
                 '-n', '-S', self._print_png,
                 '/dev/null', font_path, text])

            if rc != 0:
                lines = stderr_txt.strip().splitlines()
                self.status_var.set((lines[-1] if lines else 'Preview error')[:60])
                return

            warning = next((l for l in stderr_txt.splitlines() if 'WARNING' in l), None)

            img = Image.open(self._print_png)
            self._show_preview_image(img, tape_cfg, warning)

        except subprocess.TimeoutExpired:
            self.status_var.set('Preview timed out.')
        except Exception as e:
            self._set_status_error(str(e))

    def _refresh_wire_preview(self, text, font_path, size, tape_cfg):
        """Preview path for wire labels — renders in-process."""
        try:
            total_mm = float(self.wire_length_var.get() or '22')
        except ValueError:
            total_mm = 22.0
        try:
            margin_mm = float(self.margin_var.get() or '0.5')
        except ValueError:
            margin_mm = 0.5
        try:
            img = render_wire_label_image(
                text, font_path, size,
                total_mm=total_mm,
                tape_width_mm=tape_cfg['tape_width'],
                cut_gap_mm=margin_mm,
            )
        except Exception as e:
            self.status_var.set(str(e)[:60])
            return

        img.save(self._print_png)
        self._show_preview_image(img, tape_cfg, None,
                                 extra=f'wire label  ·  {total_mm:.0f}mm  ·  text at both ends')

    def _show_preview_image(self, img, tape_cfg, warning, extra=None):
        """Scale img, frame it and display in the preview canvas."""
        target_h = 96
        scale    = max(1, target_h // img.height)
        display  = img.convert('RGB').resize(
            (img.width * scale, img.height * scale), Image.NEAREST)

        bord = 10
        bg   = (255, 255, 180) if tape_cfg['tape_width'] == 6 else (255, 255, 255)
        canvas = Image.new('RGB',
                           (display.width + 2 * bord, display.height + 2 * bord), bg)
        canvas.paste(display, (bord, bord))

        self._photo = ImageTk.PhotoImage(canvas)
        self._preview_canvas.configure(height=canvas.height)
        self._preview_canvas.delete('all')
        self._preview_canvas.create_image(0, 0, anchor='nw', image=self._photo)
        self._preview_canvas.configure(
            scrollregion=(0, 0, canvas.width, canvas.height))
        if canvas.width > self._preview_canvas.winfo_width():
            self._preview_scroll.pack(side='top', fill='x')
        else:
            self._preview_scroll.pack_forget()

        length_mm = img.width * 25.4 / 180
        if warning:
            self.status_var.set(('⚠ ' + warning.replace('WARNING: ', ''))[:70])
        elif extra:
            self.status_var.set(f'{tape_cfg["tape_width"]}mm {extra}')
        else:
            self.status_var.set(
                f'{tape_cfg["tape_width"]}mm tape  ·  ~{length_mm:.0f}mm long'
                f'  ({img.width}×{img.height} px)')

    def _set_preview_text(self, msg):
        self._preview_canvas.delete('all')
        self._preview_scroll.pack_forget()
        self._preview_canvas.create_text(
            250, 40, text=msg, fill='#888', anchor='center')

    def _set_status_error(self, full_msg):
        """Show an error in the status bar (truncated) with a tooltip for the full text."""
        import sys as _sys
        print(f'CubePrint error: {full_msg}', file=_sys.stderr)
        short = full_msg[:80]
        self.status_var.set(short)
        # Attach/update a tooltip on the status label
        self._status_tooltip_msg = full_msg
        self._status_lbl.bind('<Enter>', self._show_status_tooltip)
        self._status_lbl.bind('<Leave>', self._hide_status_tooltip)

    def _show_status_tooltip(self, event=None):
        msg = getattr(self, '_status_tooltip_msg', None)
        if not msg:
            return
        x = self._status_lbl.winfo_rootx()
        y = self._status_lbl.winfo_rooty() + self._status_lbl.winfo_height() + 2
        self._tooltip_win = tw = tk.Toplevel(self)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f'+{x}+{y}')
        tk.Label(tw, text=msg, background='#ffffe0', relief='solid', borderwidth=1,
                 wraplength=500, justify='left', padx=4, pady=2).pack()

    def _hide_status_tooltip(self, event=None):
        tw = getattr(self, '_tooltip_win', None)
        if tw:
            tw.destroy()
            self._tooltip_win = None

    # ── print ─────────────────────────────────────────────────────────────────

    def _on_print(self):
        if self._printing:
            return
        if not self._print_png or not os.path.exists(self._print_png):
            self._refresh_preview()
        if not self._print_png:
            return

        tape_name = self.tape_var.get()
        tape_cfg  = dict(TAPE_PRESETS)[tape_name]
        png_to_print = self._print_png  # snapshot path
        mac = self._get_printer_mac()
        if not mac:
            return

        self._printing = True
        self.print_btn.configure(state='disabled')
        self.file_btn.configure(state='disabled')
        self.status_var.set('Connecting to printer…')
        self.update()

        def worker():
            try:
                rc, _, err_txt = _run_print(
                    [BT_SERIAL,
                     '--mac', mac,
                     '--tape-width', str(tape_cfg['tape_width']),
                     '--media-type', tape_cfg['media_type'],
                     *self._bt_margin_args(),
                     '-i', png_to_print])
                if rc == 0:
                    self.after(0, lambda: self.status_var.set('✓ Printed!'))
                else:
                    lines = err_txt.strip().splitlines()
                    msg = (lines[-1] if lines else 'Print failed')[:60]
                    self.after(0, lambda: self.status_var.set(f'✗ {msg}'))
            except subprocess.TimeoutExpired:
                self.after(0, lambda: self.status_var.set('✗ Timed out.'))
            except Exception as e:
                self.after(0, lambda: self.status_var.set(f'✗ {e}'))
            finally:
                self._printing = False
                self.after(0, lambda: self.print_btn.configure(state='normal'))
                self.after(0, lambda: self.file_btn.configure(state='normal'))

        threading.Thread(target=worker, daemon=True).start()

    # ── print from file ───────────────────────────────────────────────────────

    def _on_print_file(self):
        if self._printing:
            return
        path = filedialog.askopenfilename(
            title='Choose label list (.txt)',
            filetypes=[('Text files', '*.txt'), ('All files', '*.*')],
            initialdir=self._last_labels_dir,
        )
        if not path:
            return
        # Remember this folder for next time
        self._last_labels_dir = str(Path(path).parent)
        s = self._load_settings()
        s['last_labels_dir'] = self._last_labels_dir
        self._save_settings(s)
        lines = [l.strip() for l in Path(path).read_text(encoding='utf-8').splitlines()
                 if l.strip()]
        if not lines:
            messagebox.showinfo('Empty file', 'No labels found in that file.')
            return
        n = len(lines)
        if not messagebox.askyesno(
                'Confirm batch print',
                f'Print {n} label{"s" if n != 1 else ""}?\n\n'
                + '\n'.join(lines[:5])
                + ('\n…' if n > 5 else ''),
                icon='warning'):
            return
        self._print_labels_from_list(lines)

    def _print_labels_from_list(self, labels):
        font_name = self.font_var.get()
        font_path = self._font_map.get(font_name)
        tape_name = self.tape_var.get()
        tape_cfg  = dict(TAPE_PRESETS)[tape_name]
        try:
            size = int(self.size_var.get())
        except ValueError:
            self.status_var.set('Invalid font size.')
            return
        if not font_path:
            self.status_var.set('No font selected.')
            return
        font_path = find_font_variant(font_path, self.bold_var.get(), self.italic_var.get())

        mac = self._get_printer_mac()
        if not mac:
            return

        total = len(labels)
        self._printing = True
        self.print_btn.configure(state='disabled')
        self.file_btn.configure(state='disabled')

        def _re_enable():
            self._printing = False
            self.print_btn.configure(state='normal')
            self.file_btn.configure(state='normal')

        # Capture wire settings before entering thread
        is_wire = self.wire_var.get()
        try:
            wire_total_mm = float(self.wire_length_var.get() or '22')
        except ValueError:
            wire_total_mm = 22.0
        try:
            wire_margin_mm = float(self.margin_var.get() or '0.5')
        except ValueError:
            wire_margin_mm = 0.5

        def worker():
            try:
                for i, text in enumerate(labels, 1):
                    self.after(0, lambda i=i, t=text: self.status_var.set(
                        f'[{i}/{total}] Printing: {t[:30]}'))
                    tmp = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
                    png = tmp.name
                    tmp.close()
                    try:
                        if is_wire:
                            # Wire label: render in-process
                            try:
                                wimg = render_wire_label_image(
                                    text, font_path, size,
                                    total_mm=wire_total_mm,
                                    tape_width_mm=tape_cfg['tape_width'],
                                    cut_gap_mm=wire_margin_mm,
                                )
                                wimg.save(png)
                                rc, err_txt = 0, ''
                            except Exception as e:
                                rc, err_txt = 1, str(e)
                        else:
                            rc, _out, err_txt = _run_render(
                                [PRINTLABEL,
                                 '--tape-width', str(tape_cfg['tape_width']),
                                 '--fixed-font-size', str(size),
                                 *self._length_args(),
                                 *self._margin_args(),
                                 '-n', '-S', png,
                                 '/dev/null', font_path, text])
                        if rc != 0:
                            err = err_txt.strip().splitlines()
                            msg = (err[-1] if err else 'Render error')[:60]
                            self.after(0, lambda m=msg: self.status_var.set(f'✗ {m}'))
                            return
                        chain_args = ['--no-feed'] if i < total else []
                        rc2, _, err2_txt = _run_print(
                            [BT_SERIAL,
                             '--mac', mac,
                             '--tape-width', str(tape_cfg['tape_width']),
                             '--media-type', tape_cfg['media_type'],
                             *chain_args,
                             *self._bt_margin_args(),
                             '-i', png])
                        if rc2 != 0:
                            err2 = err2_txt.strip().splitlines()
                            msg = (err2[-1] if err2 else 'Print failed')[:60]
                            self.after(0, lambda m=msg: self.status_var.set(f'✗ {m}'))
                            return
                    finally:
                        try:
                            os.unlink(png)
                        except OSError:
                            pass
                self.after(0, lambda: self.status_var.set(
                    f'✓ Printed {total} label{"s" if total != 1 else ""}!'))
            except Exception as e:
                self.after(0, lambda: self.status_var.set(f'✗ {e}'))
            finally:
                self.after(0, _re_enable)

        threading.Thread(target=worker, daemon=True).start()


# ── entry point ───────────────────────────────────────────────────────────────

def _acquire_lock():
    """Return a held lock file handle, or None if another instance is running."""
    lock_dir = Path.home() / 'Library' / 'Application Support' / 'CubePrint'
    lock_dir.mkdir(parents=True, exist_ok=True)
    fh = open(lock_dir / 'instance.lock', 'w')
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        fh.close()
        return None

if __name__ == '__main__':
    _lock = _acquire_lock()
    if _lock is None:
        sys.exit(0)   # another instance is already running
    App().mainloop()
