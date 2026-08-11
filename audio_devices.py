"""
Enumeracion y resolucion de dispositivos de audio (WASAPI via `soundcard`).

Dos tipos de dispositivo, tal como los ve el usuario:

  - "mic"     -> entradas reales (microfonos, line-in, cables virtuales).
  - "speaker" -> salidas, capturadas en modo *loopback*: soundcard expone cada
                 altavoz como un microfono virtual que entrega exactamente lo
                 que esta sonando por el. Es la unica forma de "escuchar" todo
                 el sonido del sistema en Windows.

La enumeracion tarda entre decenas y cientos de milisegundos, asi que se
cachea unos segundos y se ofrece un executor propio de un solo hilo para que
el bot (asyncio) pueda pedir la lista sin bloquear su event loop.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

logger = logging.getLogger("angryfurbot.devices")

CACHE_TTL = 5.0

MIC = "mic"
SPEAKER = "speaker"


class DeviceError(Exception):
    """No se pudo enumerar o abrir un dispositivo de audio."""


@dataclass(frozen=True)
class DeviceInfo:
    name: str
    kind: str            # "mic" | "speaker"
    channels: int = 2
    is_default: bool = False

    @property
    def icon(self):
        return "🎙️" if self.kind == MIC else "🔊"

    def label(self):
        star = " ⭐" if self.is_default else ""
        return f"{self.icon} {self.name}{star}"


_cache = {"time": 0.0, "devices": []}
_cache_lock = threading.Lock()

_executor = None
_executor_lock = threading.Lock()


def executor():
    """Hilo unico dedicado a soundcard. Los llamadores asincronos (bot.py)
    hacen `loop.run_in_executor(audio_devices.executor(), ...)` para no
    bloquear el event loop mientras Windows enumera dispositivos."""
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="soundcard")
        return _executor


def shutdown_executor():
    global _executor
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=False, cancel_futures=True)
            _executor = None


def _import_soundcard():
    try:
        import soundcard as sc
    except Exception as e:  # ImportError, o fallo del backend en otro SO
        raise DeviceError(
            "No se pudo cargar 'soundcard' (requiere Windows/WASAPI). "
            f"Detalle: {e}"
        ) from e
    return sc


def preload():
    """Importa soundcard cuanto antes desde el hilo principal.

    Al importarse, soundcard crea el apartamento COM multihilo (MTA) del
    proceso; los hilos de captura que se creen despues se unen a el de forma
    implicita y por eso pueden grabar sin inicializar COM ellos mismos. Si en
    cambio el import ocurriera dentro de un hilo de vigilancia, al terminar ese
    hilo el MTA podria desaparecer y dejar sin audio a los demas. Llamar a esto
    al arrancar (bot.py, gui.py, monitor.py) ancla el MTA al hilo principal,
    que vive todo el proceso.

    Devuelve True si soundcard quedo listo."""
    try:
        _import_soundcard()
        return True
    except DeviceError as e:
        logger.warning("soundcard no disponible: %s", e)
        return False


def _enumerate():
    sc = _import_soundcard()
    try:
        raw = list(sc.all_microphones(include_loopback=True))
    except Exception as e:
        raise DeviceError(f"Windows no devolvio la lista de dispositivos: {e}") from e

    default_speaker = ""
    default_mic = ""
    try:
        default_speaker = str(sc.default_speaker().name)
    except Exception:
        pass
    try:
        default_mic = str(sc.default_microphone().name)
    except Exception:
        pass

    devices = []
    for d in raw:
        loopback = bool(getattr(d, "isloopback", False))
        kind = SPEAKER if loopback else MIC
        name = str(d.name)
        try:
            channels = int(d.channels)
        except Exception:
            channels = 2 if loopback else 1
        is_default = (name == default_speaker) if loopback else (name == default_mic)
        devices.append(DeviceInfo(name=name, kind=kind, channels=channels, is_default=is_default))

    # Los predeterminados primero: es lo que el usuario quiere el 90% de las veces.
    devices.sort(key=lambda d: (not d.is_default, d.name.lower()))
    return devices


def list_devices(kind=None, refresh=False):
    """Lista los dispositivos disponibles. `kind` = "mic" | "speaker" | None."""
    now = time.monotonic()
    with _cache_lock:
        fresh = (now - _cache["time"]) < CACHE_TTL and _cache["devices"]
        if fresh and not refresh:
            devices = _cache["devices"]
        else:
            devices = None
    if devices is None:
        devices = _enumerate()
        with _cache_lock:
            _cache["time"] = time.monotonic()
            _cache["devices"] = devices
    if kind:
        return [d for d in devices if d.kind == kind]
    return list(devices)


def list_microphones(refresh=False):
    return list_devices(MIC, refresh=refresh)


def list_loopback_devices(refresh=False):
    return list_devices(SPEAKER, refresh=refresh)


def invalidate_cache():
    with _cache_lock:
        _cache["time"] = 0.0


def resolve(name, kind):
    """Devuelve el objeto de soundcard listo para grabar, buscando por nombre.

    Prioridad: coincidencia exacta -> el que empiece igual -> el que contenga
    el texto. Asi, elegir un dispositivo de la lista siempre abre exactamente
    ese, y escribir "spotify"/"headphones" a mano sigue funcionando.

    Devuelve None si no hay ninguna coincidencia.
    """
    sc = _import_soundcard()
    needle = str(name).strip().lower()
    if not needle:
        return None

    want_loopback = kind == SPEAKER
    try:
        candidates = [
            d for d in sc.all_microphones(include_loopback=True)
            if bool(getattr(d, "isloopback", False)) == want_loopback
        ]
    except Exception as e:
        raise DeviceError(f"Windows no devolvio la lista de dispositivos: {e}") from e

    for match in (
        lambda n: n == needle,
        lambda n: n.startswith(needle),
        lambda n: needle in n,
    ):
        for d in candidates:
            if match(str(d.name).lower()):
                return d
    return None


def default_device(kind):
    """Dispositivo predeterminado del sistema del tipo pedido."""
    sc = _import_soundcard()
    try:
        if kind == SPEAKER:
            return resolve(str(sc.default_speaker().name), SPEAKER)
        return sc.default_microphone()
    except Exception:
        return None


def channels_for(kind, device=None):
    if device is not None:
        try:
            return max(1, int(device.channels))
        except Exception:
            pass
    return 2 if kind == SPEAKER else 1
