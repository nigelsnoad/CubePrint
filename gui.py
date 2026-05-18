#!/usr/bin/env python3
"""BTLabelPrinter — GUI for the PT-P300BT label printer."""

import os, sys, subprocess, tempfile, threading
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk

PROJECT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_DIR))

PYTHON     = str(PROJECT_DIR / '.venv/bin/python3')
PRINTLABEL = str(PROJECT_DIR / 'printlabel.py')
BT_SERIAL  = str(PROJECT_DIR / 'bt_serial.py')

TAPE_PRESETS = [
    ("12mm Laminated (White)",  {'tape_width': 12, 'media_type': 'laminated'}),
    ("6mm Heatshrink (HS-211)", {'tape_width': 6,  'media_type': 'heatshrink'}),
]

FONT_EXTS = {'.ttf', '.otf', '.ttc'}
FONTS_DIR      = PROJECT_DIR / 'fonts'
TEMPLATES_FILE = PROJECT_DIR / 'templates' / 'templates.json'
SETTINGS_FILE  = PROJECT_DIR / 'templates' / 'settings.json'

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


# ── main app ──────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('CubePrint')
        self.resizable(False, False)
        _icon = PROJECT_DIR / 'docs' / 'Cube Print.png'
        if _icon.exists():
            self.iconphoto(True, ImageTk.PhotoImage(Image.open(_icon).resize((64, 64))))

        self._all_fonts   = scan_fonts()
        self._font_map    = dict(self._all_fonts)
        _s = self._load_settings()
        self._custom_font_paths = _s.get('custom_fonts', [])
        self._add_custom_fonts(self._custom_font_paths)
        self._printer_mac = _s.get('printer_mac', '')
        self._after_id    = None
        self._photo       = None   # keep ref to prevent GC
        self._print_png   = None   # path reused by Print button
        self._printing    = False
        self.bold_var     = tk.BooleanVar(value=False)
        self.italic_var   = tk.BooleanVar(value=False)
        self.length_var   = tk.StringVar(value='')
        self.margin_var   = tk.StringVar(value='')
        self.tmpl_var     = tk.StringVar(value='')
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

        # --- Tape preset ---
        ttk.Label(outer, text='Tape:').grid(row=2, column=0, sticky='e', **P)
        self.tape_var = tk.StringVar(value=TAPE_PRESETS[0][0])
        tape_cb = ttk.Combobox(outer, textvariable=self.tape_var,
                               values=[t[0] for t in TAPE_PRESETS],
                               state='readonly', width=32)
        tape_cb.grid(row=2, column=1, columnspan=2, sticky='ew', **P)
        tape_cb.bind('<<ComboboxSelected>>', lambda _: self._schedule_preview())

        # --- Font search ---
        ttk.Label(outer, text='Font:').grid(row=3, column=0, sticky='e', **P)
        self.font_search = tk.StringVar()
        search_e = ttk.Entry(outer, textvariable=self.font_search, width=22)
        search_e.grid(row=3, column=1, sticky='ew', **P)
        search_e.bind('<KeyRelease>', self._on_font_search)
        ttk.Label(outer, text='🔍').grid(row=3, column=2, sticky='w')

        all_names = [n for n, _ in self._all_fonts]
        self.font_var = tk.StringVar(value=all_names[0] if all_names else '')
        font_row = ttk.Frame(outer)
        font_row.grid(row=4, column=1, columnspan=2, sticky='ew', **P)
        self.font_cb = ttk.Combobox(font_row, textvariable=self.font_var,
                                    values=all_names, state='readonly', width=26)
        self.font_cb.pack(side='left', fill='x', expand=True)
        self.font_cb.bind('<<ComboboxSelected>>', lambda _: self._schedule_preview())
        ttk.Button(font_row, text='Browse…',
                   command=self._on_browse_font).pack(side='left', padx=(4, 0))

        # --- Bold / Italic ---
        style_frame = ttk.Frame(outer)
        style_frame.grid(row=5, column=1, sticky='w', **P)
        ttk.Checkbutton(style_frame, text='Bold', variable=self.bold_var,
                        command=self._schedule_preview).pack(side='left', padx=(0, 8))
        ttk.Checkbutton(style_frame, text='Italic', variable=self.italic_var,
                        command=self._schedule_preview).pack(side='left')
        ttk.Label(style_frame, text='  (static fonts only)',
                  foreground='#888').pack(side='left')

        # --- Font size ---
        ttk.Label(outer, text='Size (pt):').grid(row=6, column=0, sticky='e', **P)
        self.size_var = tk.StringVar(value='32')
        size_sp = ttk.Spinbox(outer, from_=6, to=400, textvariable=self.size_var,
                              width=7, command=lambda: self._schedule_preview())
        size_sp.grid(row=6, column=1, sticky='w', **P)
        size_sp.bind('<KeyRelease>', lambda _: self._schedule_preview())

        # --- Text ---
        ttk.Label(outer, text='Text:').grid(row=7, column=0, sticky='e', **P)
        self.text_var = tk.StringVar(value='Test')
        text_e = ttk.Entry(outer, textvariable=self.text_var, width=32)
        text_e.grid(row=7, column=1, columnspan=2, sticky='ew', **P)
        self.text_var.trace_add('write', lambda *_: self._schedule_preview())

        # --- Label length + margin ---
        ttk.Label(outer, text='Length (mm):').grid(row=8, column=0, sticky='e', **P)
        lm_frame = ttk.Frame(outer)
        lm_frame.grid(row=8, column=1, columnspan=2, sticky='w', **P)
        ttk.Entry(lm_frame, textvariable=self.length_var, width=6).pack(side='left')
        ttk.Label(lm_frame, text='  Margin (mm):', foreground='#444').pack(side='left')
        ttk.Entry(lm_frame, textvariable=self.margin_var, width=6).pack(side='left')
        ttk.Label(lm_frame, text='  (blank = defaults)', foreground='#888').pack(side='left')
        self.length_var.trace_add('write', lambda *_: self._schedule_preview())
        self.margin_var.trace_add('write', lambda *_: self._schedule_preview())

        # --- Separator ---
        ttk.Separator(outer, orient='horizontal').grid(
            row=9, column=0, columnspan=3, sticky='ew', pady=6)

        # --- Preview ---
        PREVIEW_MAX_W = 500
        ttk.Label(outer, text='Preview:').grid(row=10, column=0, sticky='ne',
                                               padx=6, pady=3)
        preview_outer = ttk.Frame(outer, relief='sunken', borderwidth=1)
        preview_outer.grid(row=10, column=1, columnspan=2, sticky='ew',
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
        ttk.Label(outer, textvariable=self.status_var, width=30,
                  foreground='#444').grid(row=11, column=0, columnspan=2,
                                          sticky='w', padx=6, pady=6)
        btn_frame = ttk.Frame(outer)
        btn_frame.grid(row=11, column=2, sticky='e', pady=6, padx=6)
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
        TEMPLATES_FILE.write_text(json.dumps(self._templates, indent=2))
        self.tmpl_cb.configure(values=list(self._templates))

    def _current_settings(self):
        return {
            'tape':   self.tape_var.get(),
            'font':   self.font_var.get(),
            'bold':   self.bold_var.get(),
            'italic': self.italic_var.get(),
            'size':   self.size_var.get(),
            'length': self.length_var.get(),
            'margin': self.margin_var.get(),
            'text':   self.text_var.get(),
        }

    def _apply_settings(self, s):
        if 'tape' in s:   self.tape_var.set(s['tape'])
        if 'font' in s and s['font'] in self._font_map:
            self.font_var.set(s['font'])
        if 'bold'   in s: self.bold_var.set(s['bold'])
        if 'italic' in s: self.italic_var.set(s['italic'])
        if 'size'   in s: self.size_var.set(str(s['size']))
        if 'length' in s: self.length_var.set(s['length'])
        if 'margin' in s: self.margin_var.set(s['margin'])
        if 'text'   in s: self.text_var.set(s['text'])
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
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2))

    def _open_help(self):
        import subprocess
        subprocess.run(['open', 'https://github.com/nigelsnoad/CubePrint#finding-your-printers-bluetooth-mac-address'])

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

    # ── preview ───────────────────────────────────────────────────────────────

    def _schedule_preview(self, delay=350):
        if self._after_id:
            self.after_cancel(self._after_id)
        self._after_id = self.after(delay, self._refresh_preview)

    def _refresh_preview(self):
        self._after_id = None
        text      = self.text_var.get().strip()
        font_name = self.font_var.get()
        font_path = self._font_map.get(font_name)
        tape_name = self.tape_var.get()
        tape_cfg  = dict(TAPE_PRESETS)[tape_name]

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

        try:
            r = subprocess.run(
                [PYTHON, PRINTLABEL,
                 '--tape-width', str(tape_cfg['tape_width']),
                 '--fixed-font-size', str(size),
                 *self._length_args(),
                 *self._margin_args(),
                 '-n', '-S', self._print_png,
                 '/dev/null', font_path, text],
                capture_output=True, timeout=10)

            if r.returncode != 0:
                lines = r.stderr.decode(errors='replace').strip().splitlines()
                self.status_var.set((lines[-1] if lines else 'Preview error')[:60])
                return

            stderr_txt = r.stderr.decode(errors='replace').strip()
            warning = next((l for l in stderr_txt.splitlines() if 'WARNING' in l), None)

            img = Image.open(self._print_png)

            # Scale up to a target height while keeping integer pixels
            target_h = 96
            scale = max(1, target_h // img.height)
            display_w = img.width * scale
            display_h = img.height * scale
            display = img.convert('RGB').resize((display_w, display_h), Image.NEAREST)

            # Tape-coloured background border
            bord = 10
            bg = (255, 255, 180) if tape_cfg['tape_width'] == 6 else (255, 255, 255)
            canvas = Image.new('RGB',
                               (display_w + 2 * bord, display_h + 2 * bord), bg)
            canvas.paste(display, (bord, bord))

            self._photo = ImageTk.PhotoImage(canvas)
            self._preview_canvas.configure(height=canvas.height)
            self._preview_canvas.delete('all')
            self._preview_canvas.create_image(0, 0, anchor='nw', image=self._photo)
            self._preview_canvas.configure(scrollregion=(0, 0, canvas.width, canvas.height))
            # show scrollbar only when image is wider than canvas
            if canvas.width > self._preview_canvas.winfo_width():
                self._preview_scroll.pack(side='top', fill='x')
            else:
                self._preview_scroll.pack_forget()

            length_mm = img.width * 25.4 / 180
            if warning:
                self.status_var.set(('⚠ ' + warning.replace('WARNING: ', ''))[:70])
            else:
                self.status_var.set(
                    f'{tape_cfg["tape_width"]}mm tape  ·  ~{length_mm:.0f}mm long'
                    f'  ({img.width}×{img.height} px)')

        except subprocess.TimeoutExpired:
            self.status_var.set('Preview timed out.')
        except Exception as e:
            self.status_var.set(str(e)[:60])

    def _set_preview_text(self, msg):
        self._preview_canvas.delete('all')
        self._preview_scroll.pack_forget()
        self._preview_canvas.create_text(
            250, 40, text=msg, fill='#888', anchor='center')

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
                r = subprocess.run(
                    [PYTHON, BT_SERIAL,
                     '--mac', mac,
                     '--tape-width', str(tape_cfg['tape_width']),
                     '--media-type', tape_cfg['media_type'],
                     *self._bt_margin_args(),
                     '-i', png_to_print],
                    capture_output=True, text=True, timeout=60)
                if r.returncode == 0:
                    self.after(0, lambda: self.status_var.set('✓ Printed!'))
                else:
                    lines = r.stderr.strip().splitlines()
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
        labels_dir = PROJECT_DIR / 'labels'
        path = filedialog.askopenfilename(
            title='Choose label list (.txt)',
            filetypes=[('Text files', '*.txt'), ('All files', '*.*')],
            initialdir=str(labels_dir) if labels_dir.is_dir() else str(PROJECT_DIR),
        )
        if not path:
            return
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

        def worker():
            try:
                for i, text in enumerate(labels, 1):
                    self.after(0, lambda i=i, t=text: self.status_var.set(
                        f'[{i}/{total}] Printing: {t[:30]}'))
                    tmp = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
                    png = tmp.name
                    tmp.close()
                    try:
                        r = subprocess.run(
                            [PYTHON, PRINTLABEL,
                             '--tape-width', str(tape_cfg['tape_width']),
                             '--fixed-font-size', str(size),
                             *self._length_args(),
                             *self._margin_args(),
                             '-n', '-S', png,
                             '/dev/null', font_path, text],
                            capture_output=True, timeout=15)
                        if r.returncode != 0:
                            err = r.stderr.decode(errors='replace').strip().splitlines()
                            msg = (err[-1] if err else 'Render error')[:60]
                            self.after(0, lambda m=msg: self.status_var.set(f'✗ {m}'))
                            return
                        chain_args = ['--no-feed'] if i < total else []
                        r2 = subprocess.run(
                            [PYTHON, BT_SERIAL,
                             '--mac', mac,
                             '--tape-width', str(tape_cfg['tape_width']),
                             '--media-type', tape_cfg['media_type'],
                             *chain_args,
                             *self._bt_margin_args(),
                             '-i', png],
                            capture_output=True, text=True, timeout=60)
                        if r2.returncode != 0:
                            err2 = r2.stderr.strip().splitlines()
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

if __name__ == '__main__':
    App().mainloop()
