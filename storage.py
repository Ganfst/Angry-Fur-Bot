"""
Almacenamiento por usuario de la configuracion del bot (multi-tenant).

Cada persona que le escribe al bot (chat_id de Telegram) tiene su propio
archivo JSON en users_data/<chat_id>.json con sus propias vigilancias
(dispositivo o app, umbral RMS, duracion, intervalo de chequeo, cooldown,
pausada o no) y sus propias preferencias globales. Ninguna escritura de un
usuario toca el archivo de otro, asi que varias personas usan el mismo bot
de forma totalmente independiente.

Concurrencia:
  - Un RLock por chat_id serializa las lecturas/escrituras de ese usuario.
    Las operaciones compuestas (leer -> modificar -> guardar) se hacen dentro
    de `transaction()`, que mantiene el lock tomado durante todo el ciclo.
    Sin eso, dos /watch simultaneos del mismo usuario podian leer el mismo
    `next_id` y pisarse la vigilancia reciente.
  - Las escrituras son atomicas (archivo temporal + os.replace), asi que un
    corte de luz o un Ctrl+C a mitad de guardado no deja un JSON truncado.

Este modulo es la unica fuente de verdad de los limites de configuracion
(umbral, duracion, intervalo, cooldown maximo de 30s); el resto del proyecto
importa estos clamps en vez de repetir numeros magicos.
"""

import json
import os
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path


def app_dir():
    """Carpeta donde viven los datos y la configuracion.

    Empaquetado con PyInstaller, `__file__` apunta al directorio temporal que
    se borra al salir; escribir alli los datos de usuario significaria perder
    todas las vigilancias en cada arranque. Con el ejecutable congelado se usa
    la carpeta del .exe, que es la que el usuario ve y conserva."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_dir():
    """Carpeta de los recursos de solo lectura empaquetados (iconos).

    No es lo mismo que app_dir(): en modo onefile PyInstaller descomprime los
    recursos en una carpeta temporal (sys._MEIPASS) que no es donde esta el
    .exe. Los datos del usuario van a app_dir(); los recursos se leen de aqui."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base)
    return Path(__file__).resolve().parent


DATA_DIR = app_dir() / "users_data"

SCHEMA_VERSION = 2

# -- limites de configuracion (fuente unica de verdad) ----------------------

# Requisito del proyecto: las notificaciones se repiten cada 30s o menos.
DEFAULT_COOLDOWN_SECONDS = 30.0
MIN_COOLDOWN_SECONDS = 5.0
MAX_COOLDOWN_SECONDS = 30.0

DEFAULT_THRESHOLD = 0.01
MIN_THRESHOLD = 0.0001
MAX_THRESHOLD = 1.0

DEFAULT_DURATION_SECONDS = 15.0
MIN_DURATION_SECONDS = 1.0
MAX_DURATION_SECONDS = 3600.0

# Cada cuanto se lee el nivel de audio. Mas bajo = deteccion mas fina y mas
# CPU; mas alto = mas barato. 0.5s es imperceptible para el usuario.
DEFAULT_INTERVAL_SECONDS = 0.5
MIN_INTERVAL_SECONDS = 0.05
MAX_INTERVAL_SECONDS = 10.0

# Techo por usuario: cada vigilancia es un hilo + un cliente de audio, asi que
# conviene evitar que una sola persona levante cientos y tumbe la maquina.
MAX_WATCHERS_PER_USER = 8

WATCHER_TYPES = ("mic", "speaker", "app")

TYPE_LABELS = {
    "mic": "Microfono",
    "speaker": "Salida de audio",
    "app": "App",
}


class StorageError(Exception):
    """Error de negocio al guardar (p.ej. se alcanzo el limite de vigilancias)."""


# Un RLock por chat_id evita que dos escrituras concurrentes del mismo usuario
# se pisen; un lock global protege la creacion de esos locks.
_locks_guard = threading.Lock()
_user_locks = {}


def _lock_for(chat_id):
    chat_id = str(chat_id)
    with _locks_guard:
        lock = _user_locks.get(chat_id)
        if lock is None:
            # RLock (no Lock) para que transaction() pueda anidar helpers que
            # a su vez toman el lock del mismo usuario sin bloquearse solos.
            lock = _user_locks[chat_id] = threading.RLock()
        return lock


# ---------------------------------------------------------------------------
# Validacion / clamps
# ---------------------------------------------------------------------------

def _clamp(value, low, high, default):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return float(default)
    if value != value:  # NaN
        return float(default)
    return max(low, min(high, value))


def clamp_cooldown(seconds):
    return _clamp(seconds, MIN_COOLDOWN_SECONDS, MAX_COOLDOWN_SECONDS, DEFAULT_COOLDOWN_SECONDS)


def clamp_threshold(value):
    return _clamp(value, MIN_THRESHOLD, MAX_THRESHOLD, DEFAULT_THRESHOLD)


def clamp_duration(seconds):
    return _clamp(seconds, MIN_DURATION_SECONDS, MAX_DURATION_SECONDS, DEFAULT_DURATION_SECONDS)


def clamp_interval(seconds):
    return _clamp(seconds, MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS, DEFAULT_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Lectura / escritura en disco
# ---------------------------------------------------------------------------

def _path(chat_id):
    return DATA_DIR / f"{chat_id}.json"


def _ensure_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _default_user(chat_id):
    now = time.time()
    return {
        "chat_id": str(chat_id),
        "version": SCHEMA_VERSION,
        "cooldown_seconds": DEFAULT_COOLDOWN_SECONDS,
        "notify_recovery": True,
        "muted_until": 0.0,
        "username": "",
        "first_seen": now,
        "last_seen": now,
        "watchers": [],
        "next_id": 1,
    }


def _normalize_watcher(w):
    w_type = w.get("type")
    w["type"] = w_type if w_type in WATCHER_TYPES else "app"
    w["target"] = str(w.get("target", ""))
    w["threshold"] = clamp_threshold(w.get("threshold"))
    w["duration"] = clamp_duration(w.get("duration"))
    w["interval"] = clamp_interval(w.get("interval", DEFAULT_INTERVAL_SECONDS))
    # cooldown None = "usa el cooldown global del usuario"
    cooldown = w.get("cooldown")
    w["cooldown"] = None if cooldown in (None, "", 0) else clamp_cooldown(cooldown)
    w["enabled"] = bool(w.get("enabled", True))
    w["paused"] = bool(w.get("paused", False))
    w.setdefault("created_at", time.time())
    w.setdefault("alerts_total", 0)
    return w


def _migrate(data, chat_id):
    """Completa los campos que falten y normaliza tipos. Sirve tanto para
    archivos del esquema v1 (sin interval/cooldown por vigilancia) como para
    archivos editados a mano."""
    base = _default_user(chat_id)
    for key, value in base.items():
        data.setdefault(key, value)
    data["chat_id"] = str(chat_id)
    data["version"] = SCHEMA_VERSION
    data["cooldown_seconds"] = clamp_cooldown(data.get("cooldown_seconds"))
    data["notify_recovery"] = bool(data.get("notify_recovery", True))
    try:
        data["muted_until"] = float(data.get("muted_until") or 0.0)
    except (TypeError, ValueError):
        data["muted_until"] = 0.0

    watchers = data.get("watchers")
    if not isinstance(watchers, list):
        watchers = []
    data["watchers"] = [_normalize_watcher(w) for w in watchers if isinstance(w, dict)]

    # next_id siempre por delante del mayor id existente, incluso si alguien
    # edito el JSON a mano y dejo ids mas altos que next_id.
    max_id = max((int(w.get("id", 0)) for w in data["watchers"]), default=0)
    try:
        next_id = int(data.get("next_id", 1))
    except (TypeError, ValueError):
        next_id = 1
    data["next_id"] = max(next_id, max_id + 1, 1)
    return data


def _load_raw(chat_id):
    p = _path(chat_id)
    if not p.exists():
        return _default_user(chat_id)
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        # Un JSON corrupto no debe tumbar al bot ni borrarse en silencio:
        # se aparta con extension .bad y se empieza limpio.
        try:
            p.replace(p.with_suffix(".bad.json"))
        except OSError:
            pass
        return _default_user(chat_id)
    if not isinstance(data, dict):
        return _default_user(chat_id)
    return _migrate(data, chat_id)


def _save_raw(data):
    """Escritura atomica: archivo temporal en el mismo directorio + replace."""
    _ensure_dir()
    chat_id = str(data["chat_id"])
    p = _path(chat_id)
    fd, tmp = tempfile.mkstemp(dir=str(DATA_DIR), prefix=f".{chat_id}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextmanager
def transaction(chat_id):
    """Lee, deja modificar y guarda la config de un usuario manteniendo su
    lock tomado todo el tiempo. Usalo siempre que la modificacion dependa del
    estado previo (crear vigilancia, cambiar ajustes, etc.)."""
    chat_id = str(chat_id)
    with _lock_for(chat_id):
        data = _load_raw(chat_id)
        yield data
        _save_raw(data)


# ---------------------------------------------------------------------------
# API publica
# ---------------------------------------------------------------------------

def load_user(chat_id):
    with _lock_for(chat_id):
        return _load_raw(chat_id)


def save_user(data):
    chat_id = str(data.get("chat_id"))
    with _lock_for(chat_id):
        _save_raw(_migrate(data, chat_id))


def ensure_user(chat_id, username=""):
    """Crea (o refresca) el registro del usuario. Se llama en /start."""
    with transaction(chat_id) as data:
        if username:
            data["username"] = username
        data["last_seen"] = time.time()
        return data


def list_all_user_ids():
    if not DATA_DIR.exists():
        return []
    return sorted(p.stem for p in DATA_DIR.glob("*.json") if not p.name.endswith(".bad.json"))


def add_watcher(chat_id, w_type, target, threshold=None, duration=None,
                interval=None, cooldown=None, paused=False):
    """Crea una vigilancia para el usuario y devuelve su dict ya normalizado.

    Lanza StorageError si el usuario ya llego al limite de vigilancias."""
    if w_type not in WATCHER_TYPES:
        raise StorageError(f"Tipo de vigilancia desconocido: {w_type!r}")
    target = str(target).strip()
    if not target:
        raise StorageError("Falta el objetivo de la vigilancia.")

    with transaction(chat_id) as data:
        if len(data["watchers"]) >= MAX_WATCHERS_PER_USER:
            raise StorageError(
                f"Llegaste al limite de {MAX_WATCHERS_PER_USER} vigilancias. "
                "Elimina alguna antes de crear otra."
            )
        wid = int(data["next_id"])
        data["next_id"] = wid + 1
        watcher = _normalize_watcher({
            "id": wid,
            "type": w_type,
            "target": target,
            "threshold": threshold if threshold is not None else DEFAULT_THRESHOLD,
            "duration": duration if duration is not None else DEFAULT_DURATION_SECONDS,
            "interval": interval if interval is not None else DEFAULT_INTERVAL_SECONDS,
            "cooldown": cooldown,
            "enabled": True,
            "paused": bool(paused),
            "created_at": time.time(),
        })
        data["watchers"].append(watcher)
        return dict(watcher)


def get_watcher(chat_id, wid):
    try:
        wid = int(wid)
    except (TypeError, ValueError):
        return None
    for w in load_user(chat_id)["watchers"]:
        if int(w["id"]) == wid:
            return w
    return None


def remove_watcher(chat_id, wid):
    """Elimina una vigilancia y devuelve la que se elimino (o None)."""
    try:
        wid = int(wid)
    except (TypeError, ValueError):
        return None
    with transaction(chat_id) as data:
        removed = None
        keep = []
        for w in data["watchers"]:
            if int(w["id"]) == wid and removed is None:
                removed = w
            else:
                keep.append(w)
        data["watchers"] = keep
        return removed


def remove_all_watchers(chat_id):
    with transaction(chat_id) as data:
        removed = list(data["watchers"])
        data["watchers"] = []
        return removed


def update_watcher(chat_id, wid, **fields):
    """Actualiza campos de una vigilancia aplicando los clamps. Devuelve el
    dict actualizado o None si no existe."""
    try:
        wid = int(wid)
    except (TypeError, ValueError):
        return None
    with transaction(chat_id) as data:
        for w in data["watchers"]:
            if int(w["id"]) == wid:
                w.update(fields)
                _normalize_watcher(w)
                return dict(w)
        return None


def set_watcher_paused(chat_id, wid, paused):
    return update_watcher(chat_id, wid, paused=bool(paused))


def set_all_paused(chat_id, paused):
    with transaction(chat_id) as data:
        for w in data["watchers"]:
            w["paused"] = bool(paused)
        return [dict(w) for w in data["watchers"]]


def set_cooldown(chat_id, seconds):
    with transaction(chat_id) as data:
        data["cooldown_seconds"] = clamp_cooldown(seconds)
        return dict(data)


def set_notify_recovery(chat_id, enabled):
    with transaction(chat_id) as data:
        data["notify_recovery"] = bool(enabled)
        return dict(data)


def set_muted_until(chat_id, timestamp):
    """Silencia TODAS las alertas del usuario hasta ese instante (epoch).
    0 = no silenciado."""
    with transaction(chat_id) as data:
        try:
            data["muted_until"] = max(0.0, float(timestamp))
        except (TypeError, ValueError):
            data["muted_until"] = 0.0
        return dict(data)


def is_muted(chat_id):
    return float(load_user(chat_id).get("muted_until") or 0.0) > time.time()


def effective_cooldown(user_data, watcher):
    """Cooldown real de una vigilancia: el suyo propio si lo tiene, y si no,
    el global del usuario. Nunca supera MAX_COOLDOWN_SECONDS."""
    value = watcher.get("cooldown") if isinstance(watcher, dict) else None
    if value is None:
        value = user_data.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS)
    return clamp_cooldown(value)


def bump_alerts(chat_id, wid, count=1):
    """Contador historico de alertas (sobrevive a reinicios del bot)."""
    try:
        wid = int(wid)
    except (TypeError, ValueError):
        return
    with transaction(chat_id) as data:
        for w in data["watchers"]:
            if int(w["id"]) == wid:
                w["alerts_total"] = int(w.get("alerts_total", 0)) + int(count)
                break


def global_stats():
    """Resumen para el log de arranque / panel de administracion."""
    users = list_all_user_ids()
    watchers = 0
    for uid in users:
        watchers += len(load_user(uid)["watchers"])
    return {"users": len(users), "watchers": watchers}
