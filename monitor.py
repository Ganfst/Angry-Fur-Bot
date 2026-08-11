"""
Nucleo de monitoreo: supervisor de vigilancias concurrentes + modo clasico CLI.

MonitorSupervisor es el registro central de watchers en marcha. Lo usan tanto
el bot multi-usuario (bot.py) como el panel de escritorio (gui.py):

  - Guarda los watchers indexados por (owner, watcher_id), donde `owner` es el
    chat_id de Telegram en modo bot, o "local" en modo escritorio. Asi las
    vigilancias de un usuario nunca se mezclan con las de otro.
  - Todas las operaciones estan protegidas por un lock, porque se llaman desde
    el event loop de asyncio, desde hilos de la GUI y desde el propio hilo del
    watcher (al morir).
  - Aplica topes (por usuario y global) para que nadie pueda levantar cientos
    de hilos de audio y tumbar la maquina.
  - Los watchers se reconectan solos ante errores del dispositivo, asi que el
    supervisor solo los crea, los para y los consulta.

Ejecutado directamente (`python monitor.py`) funciona como en la version
original: un solo usuario, dispositivos fijos definidos en config_classic.json,
sin bot interactivo.
"""

import logging
import sys
import threading
import time

import audio_devices
import storage
import telegram_config
from audio_backends import AppWatcher, DeviceWatcher, WatcherState, fmt_duration
from notifier import get_notifier

logger = logging.getLogger("angryfurbot.monitor")

# Tope global de vigilancias simultaneas en esta maquina (todos los usuarios).
MAX_TOTAL_WATCHERS = 64


class SupervisorError(Exception):
    """No se pudo arrancar una vigilancia (limite alcanzado, dispositivo
    inexistente, etc.). El mensaje es apto para mostrar al usuario."""


class MonitorSupervisor:
    def __init__(self, notifier=None, max_per_owner=None, max_total=MAX_TOTAL_WATCHERS):
        self.notifier = notifier
        self.max_per_owner = max_per_owner or storage.MAX_WATCHERS_PER_USER
        self.max_total = max_total
        self._watchers = {}
        self._lock = threading.RLock()

    # -- consulta ---------------------------------------------------------

    @staticmethod
    def _key(owner, watcher_id):
        return (str(owner), int(watcher_id))

    def get(self, owner, watcher_id):
        try:
            key = self._key(owner, watcher_id)
        except (TypeError, ValueError):
            return None
        with self._lock:
            return self._watchers.get(key)

    def all_for(self, owner):
        owner = str(owner)
        with self._lock:
            return {wid: w for (o, wid), w in self._watchers.items() if o == owner}

    def count_for(self, owner):
        return len(self.all_for(owner))

    def total(self):
        with self._lock:
            return len(self._watchers)

    def owners(self):
        with self._lock:
            return sorted({owner for owner, _ in self._watchers})

    def stats(self):
        with self._lock:
            watchers = list(self._watchers.values())
        counts = {}
        for w in watchers:
            counts[w.state] = counts.get(w.state, 0) + 1
        return {
            "total": len(watchers),
            "owners": len({w.owner for w in watchers}),
            "by_state": counts,
            "alerting": counts.get(WatcherState.ALERTING, 0),
        }

    # -- construccion -----------------------------------------------------

    def build(self, owner, cfg, chat_id=None, cooldown=None, notify_recovery=True,
              on_status=None, mute_check=None):
        """Crea (sin arrancar) el watcher que corresponde a una config de
        storage: {id, type, target, threshold, duration, interval, cooldown}."""
        w_type = cfg.get("type")
        watcher_id = cfg.get("id")
        target = str(cfg.get("target", "")).strip()
        if not target:
            raise SupervisorError("La vigilancia no tiene objetivo.")

        effective_cooldown = cooldown if cooldown is not None else cfg.get("cooldown")
        common = dict(
            watcher_id=watcher_id,
            target_desc=target,
            threshold=storage.clamp_threshold(cfg.get("threshold")),
            duration=storage.clamp_duration(cfg.get("duration")),
            cooldown=storage.clamp_cooldown(effective_cooldown),
            interval=storage.clamp_interval(cfg.get("interval")),
            chat_id=str(chat_id if chat_id is not None else owner),
            notifier=self.notifier,
            on_status=on_status,
            paused=bool(cfg.get("paused")),
            notify_recovery=notify_recovery,
            owner=str(owner),
            mute_check=mute_check,
        )

        if w_type == "app":
            return AppWatcher(
                process_name_contains=target,
                label=f"App {target}",
                **common,
            )

        if w_type == "speaker":
            kind, label = audio_devices.SPEAKER, f"Salida {target}"
        elif w_type == "mic":
            kind, label = audio_devices.MIC, f"Microfono {target}"
        else:
            raise SupervisorError(f"Tipo de vigilancia desconocido: {w_type!r}")

        return DeviceWatcher(
            device_name=target,
            device_kind=kind,
            channels=None,  # None = todos los canales del dispositivo
            label=label,
            **common,
        )

    # -- ciclo de vida ----------------------------------------------------

    def start(self, owner, cfg, **kwargs):
        """Crea, registra y arranca un watcher. Si ya habia uno con ese id
        para ese owner, lo reemplaza (para aplicar cambios de configuracion)."""
        owner = str(owner)
        watcher_id = int(cfg["id"])
        key = self._key(owner, watcher_id)

        with self._lock:
            existing = self._watchers.get(key)
            if existing is None:
                if len(self._watchers) >= self.max_total:
                    raise SupervisorError(
                        f"Esta maquina ya tiene {self.max_total} vigilancias activas. "
                        "Espera a que alguien libere alguna."
                    )
                mine = sum(1 for o, _ in self._watchers if o == owner)
                if mine >= self.max_per_owner:
                    raise SupervisorError(
                        f"Llegaste al limite de {self.max_per_owner} vigilancias activas."
                    )

        watcher = self.build(owner, cfg, **kwargs)

        with self._lock:
            previous = self._watchers.pop(key, None)
            self._watchers[key] = watcher
        if previous is not None:
            previous.stop()
        watcher.start()
        logger.info("Vigilancia #%s de %s iniciada (%s)", watcher_id, owner, watcher.target_desc)
        return watcher

    def stop(self, owner, watcher_id):
        with self._lock:
            watcher = self._watchers.pop(self._key(owner, watcher_id), None)
        if watcher is not None:
            watcher.stop()
            logger.info("Vigilancia #%s de %s detenida", watcher_id, owner)
        return watcher

    def stop_owner(self, owner):
        owner = str(owner)
        with self._lock:
            keys = [k for k in self._watchers if k[0] == owner]
            watchers = [self._watchers.pop(k) for k in keys]
        for w in watchers:
            w.stop()
        return watchers

    def stop_all(self, join_timeout=2.0):
        with self._lock:
            watchers = list(self._watchers.values())
            self._watchers.clear()
        for w in watchers:
            w.stop()
        deadline = time.monotonic() + join_timeout
        for w in watchers:
            w.join(timeout=max(0.0, deadline - time.monotonic()))
        return watchers

    def prune(self):
        """Quita del registro los watchers cuyo hilo ya termino."""
        with self._lock:
            dead = [k for k, w in self._watchers.items() if not w.is_alive()]
            for k in dead:
                self._watchers.pop(k, None)
        return dead

    # -- cambios en caliente ----------------------------------------------

    def apply(self, owner, watcher_id, **fields):
        watcher = self.get(owner, watcher_id)
        if watcher is None:
            return None
        watcher.apply_settings(**fields)
        return watcher

    def apply_to_owner(self, owner, **fields):
        for watcher in self.all_for(owner).values():
            watcher.apply_settings(**fields)

    def set_paused(self, owner, watcher_id, paused):
        watcher = self.get(owner, watcher_id)
        if watcher is None:
            return None
        watcher.pause() if paused else watcher.resume()
        return watcher


# ---------------------------------------------------------------------------
# Helpers de dispositivos (compatibilidad con la version anterior)
# ---------------------------------------------------------------------------

def find_device(name_substring, include_loopback=False):
    kind = audio_devices.SPEAKER if include_loopback else audio_devices.MIC
    return audio_devices.resolve(name_substring, kind)


def find_device_exact(name, include_loopback=False):
    return find_device(name, include_loopback=include_loopback)


def load_config():
    """Config del modo clasico, como diccionario (compatibilidad)."""
    return telegram_config.load_classic_config().raw


CONFIG_PATH = telegram_config.CLASSIC_CONFIG_PATH


# ---------------------------------------------------------------------------
# Modo clasico: un solo usuario, dispositivos fijos en config_classic.json
# ---------------------------------------------------------------------------

def _classic_watchers(cfg):
    """Traduce las secciones monitor_microphone / monitor_speaker del archivo
    clasico a configuraciones de vigilancia."""
    specs = []
    sections = (
        ("monitor_microphone", "mic"),
        ("monitor_speaker", "speaker"),
    )
    for index, (section_name, w_type) in enumerate(sections, start=1):
        section = cfg.get(section_name)
        if not isinstance(section, dict) or not section.get("enabled", False):
            continue
        target = str(section.get("device_name_contains", "")).strip()
        if not target:
            # Sin nombre = el dispositivo predeterminado del sistema.
            device = audio_devices.default_device(
                audio_devices.SPEAKER if w_type == "speaker" else audio_devices.MIC
            )
            if device is None:
                logger.error("No hay dispositivo predeterminado para %s", section_name)
                continue
            target = str(device.name)
        specs.append({
            "id": index,
            "type": w_type,
            "target": target,
            "threshold": section.get("silence_threshold", storage.DEFAULT_THRESHOLD),
            "duration": section.get("silence_duration_seconds", storage.DEFAULT_DURATION_SECONDS),
            "interval": section.get("interval_seconds", storage.DEFAULT_INTERVAL_SECONDS),
            "paused": False,
        })
    return specs


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)] if sys.stderr else [logging.NullHandler()],
    )

    audio_devices.preload()

    classic = telegram_config.load_classic_config()
    problem = classic.describe_problem()
    if problem:
        logger.error(problem)
        return 1

    raw = classic.raw
    notifier = get_notifier(classic.token)
    supervisor = MonitorSupervisor(notifier=notifier)

    specs = _classic_watchers(raw)
    if not specs:
        logger.error(
            "No hay secciones habilitadas en %s. Activa monitor_microphone o "
            "monitor_speaker con \"enabled\": true, o usa bot.py / gui.py.",
            telegram_config.CLASSIC_CONFIG_PATH.name,
        )
        return 1

    started = []
    for spec in specs:
        try:
            started.append(supervisor.start(
                "local", spec, chat_id=classic.chat_id, cooldown=classic.cooldown,
            ))
        except Exception as e:
            logger.error("No se pudo iniciar la vigilancia %s: %s", spec["target"], e)

    if not started:
        logger.error("Ninguna vigilancia pudo arrancar.")
        return 1

    notifier.send(
        classic.chat_id,
        "🟢 <b>Monitor de audio iniciado</b>\n"
        + "\n".join(f"🎯 <code>{w.target_desc}</code>" for w in started),
        parse_mode="HTML",
    )
    logger.info("Vigilando %d objetivo(s). Ctrl+C para salir.", len(started))

    try:
        while True:
            time.sleep(5)
            for watcher in started:
                logger.debug(
                    "[%s] %s nivel=%.4f %s",
                    watcher.label, watcher.state, watcher.last_level,
                    fmt_duration(watcher.silence_elapsed),
                )
    except KeyboardInterrupt:
        logger.info("Deteniendo monitoreo...")
    finally:
        supervisor.stop_all()
        notifier.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
