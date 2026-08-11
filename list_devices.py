"""
Lista los dispositivos de audio y las apps con sonido de esta maquina.

Sirve para saber que nombre exacto escribir en config_classic.json o en los
comandos /watch_mic, /watch_speaker y /watch_app del bot, y para calibrar el
umbral de silencio viendo el nivel real que reporta cada cosa.

Uso:
    python list_devices.py            # dispositivos + apps con audio
    python list_devices.py --watch    # refresca los niveles en vivo
    python list_devices.py --procs    # ademas, procesos abiertos sin audio
"""

import argparse
import sys
import time

import app_audio
import audio_devices

# Al redirigir la salida a un archivo o a otro programa, Windows deja de usar
# UTF-8 y cae al codepage local (cp1252), donde los emoji revientan con
# UnicodeEncodeError. Con esto, "python list_devices.py > salida.txt" funciona.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def bar(level, width=24):
    filled = int(round(min(1.0, max(0.0, level)) * width))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def print_devices():
    print("=" * 68)
    print("DISPOSITIVOS DE AUDIO")
    print("=" * 68)
    try:
        mics = audio_devices.list_microphones(refresh=True)
        outs = audio_devices.list_loopback_devices()
    except audio_devices.DeviceError as e:
        print(f"  ERROR: {e}")
        return

    print("\n🎙️  Microfonos (entradas)")
    if not mics:
        print("   (ninguno)")
    for device in mics:
        star = "  <- predeterminado" if device.is_default else ""
        print(f"   - {device.name}   [{device.channels} canal(es)]{star}")

    print("\n🔊  Salidas de audio (capturadas en modo loopback)")
    if not outs:
        print("   (ninguna)")
    for device in outs:
        star = "  <- predeterminada" if device.is_default else ""
        print(f"   - {device.name}   [{device.channels} canal(es)]{star}")

    print(
        "\n   Usa el nombre completo (o una parte) en config_classic.json"
        "\n   o en /watch_mic y /watch_speaker."
    )


def print_apps(show_processes=False):
    print()
    print("=" * 68)
    print("APPS CON SESION DE AUDIO")
    print("=" * 68)
    sessions = app_audio.list_app_sessions_detailed()
    if not sessions:
        print("   (ninguna app tiene audio activo ahora mismo)")
    for entry in sessions:
        # "sesion activa" = Windows la marca como reproduciendo; el pico puede
        # ser 0 igualmente si en ese instante no sale sonido.
        state = "sesion activa" if entry["active"] else "sesion inactiva"
        print(f"   {bar(entry['peak'])} {entry['peak']:.4f}  {entry['name']}  ({state})")

    if show_processes:
        print("\n   Otros procesos abiertos (todavia sin audio):")
        active = {name.lower() for name, _peak in app_audio.list_active_app_sessions()}
        for name in app_audio.list_running_processes(limit=40):
            if name.lower() not in active:
                print(f"     - {name}")

    print("\n   Usa el nombre del proceso (ej. spotify.exe) en /watch_app.")
    print("   El nivel que ves aqui es el que compara el umbral de silencio.")


def watch_loop(interval=0.5):
    print("Niveles en vivo (Ctrl+C para salir)\n")
    try:
        while True:
            sessions = app_audio.list_app_sessions_detailed()
            lines = [f"{time.strftime('%H:%M:%S')}  apps con audio: {len(sessions)}"]
            for entry in sessions[:12]:
                lines.append(f"  {bar(entry['peak'])} {entry['peak']:.4f}  {entry['name']}")
            sys.stdout.write("\033[2J\033[H" + "\n".join(lines) + "\n")
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nFin.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true", help="refresca los niveles en vivo")
    parser.add_argument("--procs", action="store_true", help="incluye procesos sin audio activo")
    args = parser.parse_args()
    audio_devices.preload()

    if args.watch:
        watch_loop()
        return 0

    print_devices()
    print_apps(show_processes=args.procs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
