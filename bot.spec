# -*- mode: python ; coding: utf-8 -*-
#
# Empaquetado del bot multi-usuario:  pyinstaller bot.spec
#
# Genera un unico dist/AngryFurBot-bot.exe (consola, para ver los logs).
# Junto al .exe hay que dejar config.json con el bot_token, o definir la
# variable de entorno ANGRYFURBOT_TOKEN. El programa crea users_data/ en esa
# misma carpeta (no dentro del paquete temporal), asi que las vigilancias de
# cada usuario sobreviven a los reinicios: ver storage.app_dir().
#
# A proposito NO se incluye config.json dentro del ejecutable: hornear el token
# en el binario hace imposible rotarlo y lo expone a quien tenga el archivo.

ICON = 'blui.ico'

a = Analysis(
    ['bot.py'],
    pathex=[],
    binaries=[],
    datas=[(ICON, '.')],
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
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'PIL', 'pystray'],
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
    name='AngryFurBot-bot',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,          # el bot es un servicio de consola: se ven los logs
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[ICON],
)
