# -*- mode: python ; coding: utf-8 -*-
#
# Empaquetado del panel de escritorio:  pyinstaller gui.spec
#
# Genera un unico archivo dist/AngryFurBot.exe (modo onefile). Al ejecutarlo,
# Windows descomprime el contenido en una carpeta temporal, pero la
# configuracion y el estado (config_classic.json, gui_state.json) se leen y
# escriben JUNTO AL .EXE, no en esa carpeta temporal: de eso se encargan
# storage.app_dir() (datos del usuario) y storage.resource_dir() (recursos
# empaquetados, como el icono).
#
# Las credenciales de Telegram se pueden rellenar desde el propio panel, en
# Ajustes, o dejarse en las variables ANGRYFURBOT_TOKEN / ANGRYFURBOT_CHAT_ID.

ICON = 'blui.ico'

a = Analysis(
    ['gui.py'],
    pathex=[],
    binaries=[],
    datas=[(ICON, '.')],     # el icono tambien se usa en la ventana y la bandeja
    hiddenimports=[
        # comtypes genera codigo COM en tiempo de ejecucion y PyInstaller no
        # siempre detecta estos submodulos por analisis estatico.
        'comtypes',
        'comtypes.stream',
        'pycaw',
        'pycaw.pycaw',
        'pycaw.utils',
        'soundcard',
        'soundcard.mediafoundation',
        'psutil',
        'pystray._win32',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['telegram'],   # la GUI no usa python-telegram-bot, solo la API HTTP
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='AngryFurBot',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,           # aplicacion de ventana, sin consola detras
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[ICON],
)
