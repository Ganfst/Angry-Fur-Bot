"""
Nivel de audio por proceso (app especifica) usando Windows Core Audio / pycaw.

Windows expone, para cada sesion de audio (cada app que este reproduciendo
sonido), una interfaz IAudioMeterInformation con el pico de volumen actual
(0.0 a 1.0). Este modulo la consulta via comtypes, porque pycaw no la envuelve
de fabrica, y ademas:

  - Suma correctamente varias sesiones del mismo proceso: un navegador abre una
    sesion por pestana/render, asi que mirar solo la primera hacia que el bot
    creyera que hay silencio mientras otra pestana sonaba. Ahora se toma el
    pico maximo entre todas las sesiones de ese proceso.
  - Cachea un "snapshot" {proceso -> pico} unos milisegundos. Con varios
    usuarios vigilando apps distintas, todos los watchers reutilizan la misma
    enumeracion en vez de recorrer COM cada uno por su cuenta.
  - Ofrece `com_initialized()` y `executor()` para llamar a estas funciones
    desde hilos de trabajo y desde asyncio sin bloquear ni pelearse con el
    modo de apartamento COM que ya haya inicializado `soundcard`.

Requiere Windows + pycaw + comtypes (ver requirements.txt).
"""

import ctypes
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from ctypes import POINTER, c_float

logger = logging.getLogger("angryfurbot.app_audio")

# -- Convivencia entre pycaw (COM/STA) y soundcard (COM/MTA) ---------------
#
# Las dos librerias inicializan COM al importarse, pero en modos incompatibles
# y solo en el hilo donde ocurre el import:
#
#   soundcard  -> CoInitializeEx(MTA) al importar. Como el MTA es unico para
#                 todo el proceso, cualquier hilo que use COM sin inicializarlo
#                 se une a el implicitamente: por eso los hilos de captura
#                 pueden grabar sin hacer nada especial.
#   comtypes   -> CoInitialize(STA) al importar. Si ese hilo ya estaba en MTA,
#                 el import falla con RPC_E_CHANGED_MODE y pycaw queda inutil
#                 en TODO el proceso (la vigilancia de apps reportaria silencio
#                 permanente). Y al reves: si comtypes gana la carrera en el
#                 hilo principal, soundcard no llega a crear el MTA del proceso
#                 y son los hilos de captura los que se quedan sin COM.
#
# La solucion es no meterlos nunca en el mismo hilo: comtypes se importa la
# primera vez desde el hilo dedicado de este modulo (recien creado, por tanto
# libre para ser STA), y soundcard se importa desde el hilo principal
# (audio_devices.preload()), que vive todo el proceso y sostiene el MTA.
# STA y MTA conviven sin problema mientras cada uno se quede en su hilo.

_comtypes = None
COM_AVAILABLE = False
COM_IMPORT_ERROR = None
_import_lock = threading.RLock()


def _import_comtypes():
    """Importa comtypes en el hilo actual. Debe llamarse solo desde un hilo
    que no haya tocado COM todavia (el worker dedicado)."""
    global _comtypes, COM_AVAILABLE, COM_IMPORT_ERROR
    if _comtypes is not None or COM_IMPORT_ERROR is not None:
        return _comtypes
    try:
        import comtypes
    except Exception as e:  # ImportError en otros SO, OSError si COM se queja
        COM_IMPORT_ERROR = e
        logger.warning("No se pudo cargar comtypes (vigilancia por app deshabilitada): %s", e)
        return None
    _comtypes = comtypes
    COM_AVAILABLE = True
    return comtypes


def ensure_comtypes():
    """Devuelve el modulo comtypes, importandolo en el hilo dedicado la
    primera vez para que caiga siempre en un hilo COM limpio."""
    if _comtypes is not None or COM_IMPORT_ERROR is not None:
        return _comtypes
    with _import_lock:
        if _comtypes is not None or COM_IMPORT_ERROR is not None:
            return _comtypes
        if on_com_worker():
            return _import_comtypes()
        try:
            return executor().submit(_import_comtypes).result(timeout=30)
        except Exception as e:
            logger.warning("No se pudo inicializar COM para pycaw: %s", e)
            return None

# IID de IAudioMeterInformation (constante fija de Windows Core Audio)
IID_IAudioMeterInformation = "{C02216F6-8C67-4B5B-9D00-D008E73E0064}"

# CoInitialize devuelve este HRESULT si el hilo ya inicializo COM en otro modo
# de apartamento (pasa cuando `soundcard` ya corrio en el mismo hilo). No es un
# error real para nosotros: COM ya esta listo, solo que en el otro modo.
RPC_E_CHANGED_MODE = -2147417850

# Estados de sesion de audio de Windows (AudioSessionState)
STATE_INACTIVE, STATE_ACTIVE, STATE_EXPIRED = 0, 1, 2

# Cada cuanto, como maximo, se vuelve a recorrer COM para refrescar los picos.
SNAPSHOT_TTL = 0.2

_IAudioMeterInformation = None
_meter_lock = threading.Lock()


def _meter_interface():
    """Declara la interfaz COM la primera vez que se usa."""
    global _IAudioMeterInformation
    comtypes = ensure_comtypes()
    if comtypes is None:
        raise RuntimeError(
            f"comtypes no esta disponible, no se puede medir audio por app: {COM_IMPORT_ERROR}"
        )
    with _meter_lock:
        if _IAudioMeterInformation is not None:
            return _IAudioMeterInformation
        from comtypes import GUID, HRESULT, STDMETHOD

        class IAudioMeterInformation(comtypes.IUnknown):
            """Solo se declara GetPeakValue, que es el unico metodo que usamos.
            El orden de la vtable COM debe respetarse, pero como es el primer
            metodo despues de IUnknown, no hace falta declarar los siguientes
            (GetMeteringChannelCount, GetChannelsPeakValues...) para llamarlo."""

            _iid_ = GUID(IID_IAudioMeterInformation)
            _methods_ = [
                STDMETHOD(HRESULT, "GetPeakValue", [POINTER(c_float)]),
            ]

        _IAudioMeterInformation = IAudioMeterInformation
        return _IAudioMeterInformation


def is_changed_mode(exc):
    """True si el error es RPC_E_CHANGED_MODE (COM ya inicializado en otro
    modo de apartamento en este hilo)."""
    return isinstance(exc, OSError) and getattr(exc, "winerror", None) == RPC_E_CHANGED_MODE


@contextmanager
def com_initialized():
    """Inicializa COM en el hilo actual si hace falta.

    Si el hilo ya tenia COM en otro modo (RPC_E_CHANGED_MODE) se continua
    igual: las llamadas siguen funcionando, solo cambia el modelo de
    apartamento. Lo unico que no se puede permitir es que el *import* de
    comtypes ocurra en esa situacion, y de eso se encarga ensure_comtypes()."""
    comtypes = ensure_comtypes()
    if comtypes is None:
        yield False
        return
    initialized = False
    try:
        comtypes.CoInitialize()
        initialized = True
    except Exception as e:
        if not is_changed_mode(e):
            logger.debug("CoInitialize fallo (%s); se continua igual", e)
    try:
        yield initialized
    finally:
        if initialized:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass


_executor = None
_executor_lock = threading.Lock()
_thread_local = threading.local()


def _com_thread_init():
    """Inicializador del hilo dedicado: aqui es donde comtypes se importa por
    primera vez, sobre un hilo virgen que puede entrar en STA sin conflicto."""
    _thread_local.is_com_worker = True
    comtypes = _import_comtypes()
    if comtypes is not None:
        try:
            comtypes.CoInitialize()
        except Exception:
            pass


def on_com_worker():
    return getattr(_thread_local, "is_com_worker", False)


def executor():
    """Hilo unico y persistente con COM ya inicializado, para que asyncio
    pueda listar apps sin bloquear el event loop del bot."""
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="pycaw", initializer=_com_thread_init
            )
        return _executor


def shutdown_executor():
    global _executor
    with _executor_lock:
        if _executor is not None:
            # cancel_futures descarta lo que este encolado: al cerrar la app no
            # tiene sentido esperar a un refresco de la lista de procesos.
            _executor.shutdown(wait=False, cancel_futures=True)
            _executor = None


# ---------------------------------------------------------------------------
# Snapshot compartido de picos por proceso
# ---------------------------------------------------------------------------

_snapshot = {"time": -1e9, "data": {}, "error": None}
_snapshot_lock = threading.Lock()     # protege la lectura/escritura del dict
_compute_lock = threading.Lock()      # serializa el recorrido COM (uno a la vez)


def _session_peak(session, meter_cls):
    try:
        meter = session._ctl.QueryInterface(meter_cls)
        value = c_float()
        meter.GetPeakValue(ctypes.byref(value))
        return float(value.value)
    except Exception:
        return None


def _collect():
    """Recorre las sesiones de audio y devuelve {nombre_lower: info}."""
    from pycaw.pycaw import AudioUtilities

    meter_cls = _meter_interface()
    sessions = AudioUtilities.GetAllSessions()

    data = {}
    for session in sessions:
        try:
            proc = session.Process
        except Exception:
            proc = None
        if proc is None:
            continue  # sonidos del sistema, sin proceso asociado
        try:
            pname = proc.name()
            pid = proc.pid
        except Exception:
            continue

        try:
            state = int(session.State)
        except Exception:
            state = STATE_INACTIVE
        if state == STATE_EXPIRED:
            continue

        peak = _session_peak(session, meter_cls)
        key = pname.lower()
        entry = data.get(key)
        if entry is None:
            entry = data[key] = {
                "name": pname,
                "peak": 0.0,
                "sessions": 0,
                "active": False,
                "pids": [],
                "measurable": False,
            }
        entry["sessions"] += 1
        entry["active"] = entry["active"] or state == STATE_ACTIVE
        if pid not in entry["pids"]:
            entry["pids"].append(pid)
        if peak is not None:
            entry["measurable"] = True
            # Varias sesiones del mismo proceso (pestanas de un navegador):
            # nos quedamos con la que mas suene.
            entry["peak"] = max(entry["peak"], peak)
    return data


def _collect_safe():
    """Recorre las sesiones en este hilo; si COM esta en un modo incompatible,
    reintenta en el hilo dedicado (que siempre esta bien inicializado)."""
    try:
        with com_initialized():
            return _collect()
    except Exception as e:
        if not is_changed_mode(e) or on_com_worker():
            raise
        logger.debug("COM en modo incompatible en este hilo; se usa el hilo dedicado")
        return executor().submit(_collect).result(timeout=15)


def snapshot(max_age=SNAPSHOT_TTL, refresh=False):
    """Devuelve {nombre_lower: {...}} con los picos actuales por proceso.

    Varios watchers a la vez comparten el mismo recorrido: el primero que
    encuentra el snapshot viejo lo recalcula y el resto reutiliza el resultado.
    Lanza la excepcion original si COM/pycaw fallan."""
    now = time.monotonic()
    with _snapshot_lock:
        if not refresh and (now - _snapshot["time"]) < max_age:
            return _snapshot["data"]

    with _compute_lock:
        # Otro hilo pudo refrescarlo mientras esperabamos el lock.
        now = time.monotonic()
        with _snapshot_lock:
            if not refresh and (now - _snapshot["time"]) < max_age:
                return _snapshot["data"]
        try:
            data = _collect_safe()
            error = None
        except Exception as e:
            data, error = {}, e
        with _snapshot_lock:
            _snapshot["time"] = time.monotonic()
            _snapshot["data"] = data
            _snapshot["error"] = error
        if error is not None:
            raise error
        return data


# ---------------------------------------------------------------------------
# API publica
# ---------------------------------------------------------------------------

def find_process_entry(name_contains, data=None):
    """Busca la entrada del snapshot cuyo proceso coincida con el texto dado.
    Prioriza la coincidencia exacta ('spotify.exe') sobre la parcial
    ('spotify'), y entre parciales prefiere la que mas suene."""
    needle = str(name_contains).strip().lower()
    if not needle:
        return None
    if data is None:
        data = snapshot()

    exact = data.get(needle)
    if exact is not None:
        return exact

    matches = [entry for key, entry in data.items() if needle in key]
    if not matches:
        return None
    return max(matches, key=lambda e: e["peak"])


def get_peak_for_process(name_contains):
    """Pico de volumen actual (0.0-1.0) de una app, o None si no tiene ninguna
    sesion de audio ahora mismo (app cerrada o sin reproducir nada).

    Para el detector de silencio, None y 0.0 significan lo mismo: no suena."""
    try:
        entry = find_process_entry(name_contains)
    except Exception as e:
        logger.debug("No se pudo leer el pico de %s: %s", name_contains, e)
        return None
    return entry["peak"] if entry else None


def list_active_app_sessions(refresh=True):
    """[(nombre_proceso, pico)] de las apps con sesion de audio ahora mismo,
    ordenadas por nivel descendente. Alimenta /apps y los selectores de la UI."""
    try:
        data = snapshot(refresh=refresh)
    except Exception as e:
        logger.warning("No se pudieron listar las sesiones de audio: %s", e)
        return []
    return sorted(
        ((entry["name"], entry["peak"]) for entry in data.values()),
        key=lambda item: item[1],
        reverse=True,
    )


def list_app_sessions_detailed(refresh=True):
    try:
        data = snapshot(refresh=refresh)
    except Exception as e:
        logger.warning("No se pudieron listar las sesiones de audio: %s", e)
        return []
    return sorted(data.values(), key=lambda e: (not e["active"], -e["peak"], e["name"].lower()))


# Apps que la gente suele querer vigilar: se muestran primero en los selectores.
MEDIA_HINTS = (
    "spotify", "chrome", "firefox", "msedge", "brave", "opera", "vivaldi",
    "vlc", "wmplayer", "mpc-hc", "mpc-be", "potplayer", "aimp", "foobar",
    "itunes", "applemusic", "musicbee", "deezer", "tidal", "winamp",
    "discord", "teams", "zoom", "obs", "obs64", "steam", "netflix",
    "youtube", "twitch", "audacity", "reaper", "ableton", "fl64",
)

# Ruido de sistema que nunca interesa como objetivo de vigilancia.
SYSTEM_NOISE = {
    "system", "registry", "idle", "memory compression", "system idle process",
    "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
    "lsass.exe", "svchost.exe", "fontdrvhost.exe", "dwm.exe", "conhost.exe",
    "dllhost.exe", "sihost.exe", "ctfmon.exe", "taskhostw.exe", "runtimebroker.exe",
    "searchindexer.exe", "searchhost.exe", "wmiprvse.exe", "spoolsv.exe",
    "audiodg.exe", "securityhealthservice.exe", "securityhealthsystray.exe",
    "textinputhost.exe", "shellexperiencehost.exe", "startmenuexperiencehost.exe",
    "widgets.exe", "widgetservice.exe", "lockapp.exe", "useroobebroker.exe",
    "backgroundtaskhost.exe", "applicationframehost.exe", "wudfhost.exe",
    "msmpeng.exe", "nissrv.exe", "sppsvc.exe", "trustedinstaller.exe",
}


def _media_rank(name):
    lowered = name.lower()
    for index, hint in enumerate(MEDIA_HINTS):
        if hint in lowered:
            return index
    return len(MEDIA_HINTS)


def list_running_processes(limit=25, query=None, include_system=False):
    """Procesos en ejecucion que el usuario podria querer vigilar, aunque
    todavia no esten sonando (ej. abrio Spotify pero no le dio play).

    Ordena primero las apps multimedia conocidas y filtra el ruido del sistema;
    antes se cortaba la lista con los primeros N procesos que devolvia el SO,
    que solian ser todos servicios de Windows."""
    try:
        import psutil
    except ImportError:
        logger.debug("psutil no esta instalado: no se listan procesos")
        return []

    needle = str(query).strip().lower() if query else ""
    seen = set()
    names = []
    for proc in psutil.process_iter(["name"]):
        try:
            name = proc.info.get("name")
        except Exception:
            continue
        if not name:
            continue
        lowered = name.lower()
        if lowered in seen:
            continue
        if not include_system and lowered in SYSTEM_NOISE:
            continue
        if needle and needle not in lowered:
            continue
        seen.add(lowered)
        names.append(name)

    names.sort(key=lambda n: (_media_rank(n), n.lower()))
    return names[:limit] if limit else names


def suggest_targets(limit=20, query=None):
    """Lista combinada para el selector de apps: primero las que estan sonando
    (con su nivel), luego el resto de procesos abiertos."""
    active = list_active_app_sessions()
    active_names = {name.lower() for name, _peak in active}
    suggestions = [
        {"name": name, "peak": peak, "playing": True}
        for name, peak in active
        if not query or str(query).lower() in name.lower()
    ]
    for name in list_running_processes(limit=limit * 2, query=query):
        if name.lower() in active_names:
            continue
        suggestions.append({"name": name, "peak": 0.0, "playing": False})
    return suggestions[:limit]
