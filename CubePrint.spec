# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['gui.py'],
    binaries=[
        ('bt_rfcomm', '.'),
    ],
    datas=[
        ('printlabel.py',       '.'),
        ('bt_serial.py',        '.'),
        ('labelmaker.py',       '.'),
        ('labelmaker_encode.py','.'),
        ('ptcbp.py',            '.'),
        ('ptstatus.py',         '.'),
        ('docs/CubePrint Icon.jpeg', 'docs'),
        ('fonts',               'fonts'),
    ],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='CubePrint',
    debug=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=False,
    icon='CubePrint.app/Contents/Resources/AppIcon.icns',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='CubePrint',
)

app = BUNDLE(
    coll,
    name='CubePrint.app',
    icon='CubePrint.app/Contents/Resources/AppIcon.icns',
    bundle_identifier='com.local.cubeprint',
    info_plist={
        'CFBundleName':                    'CubePrint',
        'CFBundleShortVersionString':      '1.0.0',
        'CFBundleVersion':                 '1.0.0',
        'LSMinimumSystemVersion':          '12.0',
        'NSHighResolutionCapable':         True,
        'NSBluetoothAlwaysUsageDescription':
            'CubePrint uses Bluetooth to communicate with your Brother P-touch label printer.',
    },
)
