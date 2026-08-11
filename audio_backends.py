"""
Motor de deteccion de silencio: un watcher por cada dispositivo o app vigilada.

Piezas:

  - SilenceDetector: la maquina de estados pura (sin hilos, sin audio, sin
    red). Recibe niveles y devuelve eventos: "alert", "repeat", "recovered".
    Al no depender de hardware, se puede probar entera en segundos --
    ver selftest.py.
  - BaseWatcher: hilo que muestrea audio, alimenta el detector y despacha las
    alertas al notificador. Incluye reconexion automatica con backoff: si
    desconectas los auriculares o la app se cierra, el watcher no muere, se
    reintenta y avisa cuando vuelve.
  - DeviceWatcher: microfono o salida de audio completa (WASAPI loopback via
    `soundcard`), midiendo RMS y pico de cada bloque con numpy.
  - AppWatcher: una app concreta (Spotify, un navegador, un juego) leyendo el
    medidor de su sesion de audio en Windows (pycaw), sin importar que otras
    apps esten sonando a la vez.

Cada watcher corre en su propio hilo, no en el event loop de asyncio que usa
python-telegram-bot: las lecturas de audio son bloqueantes, y aislarlas en
hilos permite que N vigilancias de M usuarios distintos convivan sin frenar
al bot ni entre ellas.
"""

import logging
import threading
import time
import warnings

import numpy as np

import app_audio
import audio_devices
from notifier import escape_html, get_notifier

logger = logging.getLogger("angryfurbot.watcher")

# "data discontinuity in recording" es un aviso benigno de soundcard: ocurre
# cuando hay un hueco al leer el buffer (el sistema estuvo ocupado un instante).
# La libreria se recupera sola; solo ensuciaba la consola.
warnings.filterwarnings("ignore", message="data discontinuity in recording")

# Cuanto puede durar como maximo un bloque de captura. Aunque el usuario pida
# un intervalo de chequeo grande, seguimos leyendo en bloques cortos para que
# detener o pausar una vigilancia sea instantaneo.
MAX_BLOCK_SECONDS = 1.0

# Segundos que debe aguantar una conexion para considerarse "sana" y reiniciar
# el backoff de reconexion.
HEALTHY_RUN_SECONDS = 30.0
MAX_RECONNECT_DELAY = 30.0


class WatcherState:
    STARTING = "starting"
    ACTIVE = "active"
    SILENCE = "silence"      # en silencio, todavia dentro del margen de gracia
    ALERTING = "alerting"    # silencio confirmado, ya se alerto
    PAUSED = "paused"
    STOPPED = "stopped"
    ERROR = "error"


STATE_EMOJI = {
    WatcherState.STARTING: "⚙️",
    WatcherState.ACTIVE: "🟢",
    WatcherState.SILENCE: "🟠",
    WatcherState.ALERTING: "🔴",
    WatcherState.PAUSED: "🟡",
    WatcherState.STOPPED: "⚪",
    WatcherState.ERROR: "⛔",
}

STATE_LABEL = {
    WatcherState.STARTING: "Iniciando",
    WatcherState.ACTIVE: "Activo",
    WatcherState.SILENCE: "En silencio",
    WatcherState.ALERTING: "Silencio detectado",
    WatcherState.PAUSED: "Pausado",
    WatcherState.STOPPED: "Detenido",
    WatcherState.ERROR: "Error",
}


def fmt_duration(seconds):
    """42s / 1m 12s / 2h 05m — para mensajes y tarjetas."""
    try:
        seconds = int(max(0, float(seconds)))
    except (TypeError, ValueError):
        return "0s"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def analyse_block(data):
    """RMS y pico de un bloque de audio. Devuelve (rms, peak) en 0.0-1.0."""
    if data is None:
        return 0.0, 0.0
    array = np.asarray(data, dtype=np.float64)
    if array.size == 0:
        return 0.0, 0.0
    # Un driver que entrega NaN/inf (pasa con algunos cables virtuales) no debe
    # envenenar la media: se tratan como silencio.
    array = np.nan_to_num(array, nan=0.0, posinf=0.0, neginf=0.0)
    rms = float(np.sqrt(np.mean(np.square(array))))
    peak = float(np.max(np.abs(array)))
    return rms, peak


# ---------------------------------------------------------------------------
# Maquina de estados de silencio (logica pura y testeable)
# ---------------------------------------------------------------------------

class SilenceDetector:
    """Decide cuando avisar de silencio y cuando avisar de recuperacion.

    Reglas:
      - Nivel por debajo del umbral durante `duration` segundos -> "alert".
      - Mientras siga en silencio, un "repeat" cada `cooldown` segundos.
      - Al volver el sonido despues de haber alertado -> "recovered".

    Histeresis: una vez en silencio hace falta superar `threshold *
    release_ratio` para darlo por recuperado. Evita el parpadeo
    alerta/recuperado cuando el nivel oscila justo en el umbral.
    """

    def __init__(self, threshold, duration, cooldown, release_ratio=1.15):
        self.threshold = float(threshold)
        self.duration = float(duration)
        self.cooldown = float(cooldown)
        self.release_ratio = float(release_ratio)

        self.state = WatcherState.STARTING
        self.level = 0.0
        self.silence_start = None
        self.silence_elapsed = 0.0
        self.alerted = False
        self.alerts_sent = 0
        self.last_alert = 0.0
        self._silent = False

    def configure(self, threshold=None, duration=None, cooldown=None):
        if threshold is not None:
            self.threshold = float(threshold)
        if duration is not None:
            self.duration = float(duration)
        if cooldown is not None:
            self.cooldown = float(cooldown)

    def reset(self):
        """Olvida el silencio acumulado (al pausar/reanudar o reconectar)."""
        self.silence_start = None
        self.silence_elapsed = 0.0
        self.alerted = False
        self._silent = False

    def is_silent(self, level):
        limit = self.threshold * self.release_ratio if self._silent else self.threshold
        return level < limit

    def update(self, level, now):
        """Alimenta el detector con un nivel nuevo. Devuelve la lista de
        eventos [(tipo, segundos_en_silencio), ...] que el llamador debe
        despachar."""
        level = 0.0 if level is None else float(level)
        self.level = level
        events = []

        if self.is_silent(level):
            self._silent = True
            if self.silence_start is None:
                self.silence_start = now
            elapsed = self.silence_elapsed = max(0.0, now - self.silence_start)

            if not self.alerted:
                if elapsed >= self.duration:
                    self.alerted = True
                    self.last_alert = now
                    self.alerts_sent += 1
                    self.state = WatcherState.ALERTING
                    events.append(("alert", elapsed))
                else:
                    self.state = WatcherState.SILENCE
            else:
                if (now - self.last_alert) >= self.cooldown:
                    self.last_alert = now
                    self.alerts_sent += 1
                    events.append(("repeat", elapsed))
                self.state = WatcherState.ALERTING
        else:
            if self.alerted:
                elapsed = max(0.0, now - self.silence_start) if self.silence_start else 0.0
                events.append(("recovered", elapsed))
            self._silent = False
            self.silence_start = None
            self.silence_elapsed = 0.0
            self.alerted = False
            self.state = WatcherState.ACTIVE

        return events


# ---------------------------------------------------------------------------
# Watcher base
# ---------------------------------------------------------------------------

class BaseWatcher(threading.Thread):
    """Hilo de vigilancia. Las subclases solo implementan `_run_loop()`."""

    kind = "base"

    def __init__(
        self,
        watcher_id,
        label,
        target_desc,
        threshold,
        duration,
        cooldown,
        chat_id,
        token=None,
        notifier=None,
        on_status=None,
        paused=False,
        interval=None,
        notify_recovery=True,
        owner=None,
        mute_check=None,
    ):
        super().__init__(daemon=True, name=f"watcher-{owner or chat_id}-{watcher_id}")
        self.watcher_id = watcher_id
        self.owner = str(owner if owner is not None else chat_id)
        self.label = label
        self.target_desc = target_desc
        self.chat_id = str(chat_id)
        self.interval = float(interval) if interval else 0.5
        self.notify_recovery = bool(notify_recovery)
        self.on_status = on_status
        # mute_check() -> True para no enviar alertas (modo "no molestar" del
        # usuario) sin dejar de medir ni de actualizar el estado en pantalla.
        self.mute_check = mute_check

        if notifier is None and token:
            notifier = get_notifier(token)
        self.notifier = notifier

        self.detector = SilenceDetector(
            threshold=threshold,
            duration=duration,
            cooldown=min(float(cooldown), 30.0),  # requisito: nunca mas de 30s
        )

        self._stop_event = threading.Event()
        self._paused = threading.Event()
        if paused:
            self._paused.set()

        # Estado publicado hacia la UI (lo escribe el hilo del watcher y lo leen
        # el bot y la GUI; son asignaciones atomicas de valores simples).
        self.state = WatcherState.STARTING
        self.last_level = 0.0
        self.last_rms = 0.0
        self.last_peak = 0.0
        self.error = ""
        self.started_at = time.time()
        self.connected_at = None
        self.alerts_sent = 0
        self._failure_reported = False
        self._muted_skips = 0

    # -- configuracion en caliente ---------------------------------------

    @property
    def threshold(self):
        return self.detector.threshold

    @threshold.setter
    def threshold(self, value):
        self.detector.threshold = float(value)

    @property
    def duration(self):
        return self.detector.duration

    @duration.setter
    def duration(self, value):
        self.detector.duration = float(value)

    @property
    def cooldown(self):
        return self.detector.cooldown

    @cooldown.setter
    def cooldown(self, value):
        self.detector.cooldown = min(float(value), 30.0)

    def apply_settings(self, threshold=None, duration=None, cooldown=None,
                       interval=None, notify_recovery=None):
        """Cambia ajustes sin reiniciar el hilo (el usuario toca un boton y el
        cambio se aplica en la siguiente lectura)."""
        self.detector.configure(threshold=threshold, duration=duration, cooldown=cooldown)
        if interval is not None:
            self.interval = float(interval)
        if notify_recovery is not None:
            self.notify_recovery = bool(notify_recovery)

    # -- control ---------------------------------------------------------

    def stop(self):
        self._stop_event.set()

    def should_stop(self):
        return self._stop_event.is_set()

    def pause(self):
        self._paused.set()
        self.detector.reset()
        self._set_state(WatcherState.PAUSED)

    def resume(self):
        self._paused.clear()
        self.detector.reset()
        self._set_state(WatcherState.STARTING)

    def is_paused(self):
        return self._paused.is_set()

    # -- estado publicado -------------------------------------------------

    def _set_state(self, state):
        self.state = state
        self._notify_status()

    def _notify_status(self):
        if self.on_status:
            try:
                self.on_status(self)
            except Exception:
                logger.debug("Callback de estado fallo", exc_info=True)

    @property
    def last_status(self):
        """Texto corto de estado (compatibilidad con la version anterior)."""
        if self.state == WatcherState.ALERTING:
            return f"silencio ({fmt_duration(self.detector.silence_elapsed)})"
        if self.state == WatcherState.SILENCE:
            return f"silencio ({fmt_duration(self.detector.silence_elapsed)})"
        if self.state == WatcherState.ERROR:
            return f"error: {self.error}"
        return STATE_LABEL.get(self.state, self.state).lower()

    @property
    def silence_elapsed(self):
        return self.detector.silence_elapsed

    @property
    def uptime(self):
        return time.time() - self.started_at

    def snapshot(self):
        """Foto consistente del estado, pensada para pintar la UI."""
        return {
            "id": self.watcher_id,
            "kind": self.kind,
            "label": self.label,
            "target": self.target_desc,
            "state": self.state,
            "emoji": STATE_EMOJI.get(self.state, "•"),
            "state_label": STATE_LABEL.get(self.state, self.state),
            "level": self.last_level,
            "rms": self.last_rms,
            "peak": self.last_peak,
            "threshold": self.detector.threshold,
            "duration": self.detector.duration,
            "cooldown": self.detector.cooldown,
            "interval": self.interval,
            "silence_elapsed": self.detector.silence_elapsed,
            "alerts_sent": self.alerts_sent,
            "uptime": self.uptime,
            "error": self.error,
            "alive": self.is_alive(),
            "paused": self.is_paused(),
        }

    # -- procesamiento ----------------------------------------------------

    def _process_level(self, level, rms=None, peak=None):
        level = 0.0 if level is None else float(level)
        self.last_level = level
        self.last_rms = float(rms) if rms is not None else level
        self.last_peak = float(peak) if peak is not None else level

        if self.is_paused():
            if self.state != WatcherState.PAUSED:
                self.detector.reset()
                self._set_state(WatcherState.PAUSED)
            else:
                self._notify_status()
            return

        for kind, elapsed in self.detector.update(level, time.time()):
            if kind == "alert":
                self._dispatch(self._alert_message(elapsed, repeat=False))
            elif kind == "repeat":
                self._dispatch(self._alert_message(elapsed, repeat=True))
            elif kind == "recovered" and self.notify_recovery:
                self._dispatch(self._recovery_message(elapsed))
        self.alerts_sent = self.detector.alerts_sent

        self.state = self.detector.state
        self._notify_status()

    def _muted(self):
        if not self.mute_check:
            return False
        try:
            return bool(self.mute_check())
        except Exception:
            return False

    def _dispatch(self, text):
        if self._muted():
            self._muted_skips += 1
            logger.info("[%s] alerta omitida: el usuario tiene las alertas silenciadas", self.label)
            return
        if not self.notifier:
            logger.warning("[%s] sin notificador configurado: %s", self.label, text)
            return
        # Avisar es lo secundario: vigilar es lo principal. Si el notificador
        # falla (cola rota, callback del llamador que revienta...) se registra
        # y se sigue midiendo, en vez de tumbar el hilo de vigilancia entero.
        try:
            self.notifier.send(self.chat_id, text, parse_mode="HTML")
        except Exception as e:
            logger.error("[%s] no se pudo encolar la alerta: %s", self.label, e)

    # -- textos de las alertas --------------------------------------------

    def _header(self):
        return f"<b>{escape_html(self.label)}</b>"

    def _extra_context(self):
        """Gancho para que las subclases anadan una linea de contexto."""
        return ""

    def _alert_message(self, elapsed, repeat=False):
        title = "🔇 <b>Silencio detectado</b>" if not repeat else "🔁 <b>Sigue en silencio</b>"
        lines = [
            title,
            f"🎯 {self._header()} · <code>{escape_html(self.target_desc)}</code>",
            f"⏱️ Lleva <b>{fmt_duration(elapsed)}</b> sin sonido "
            f"(aviso tras {fmt_duration(self.detector.duration)})",
            f"📉 Nivel {self.last_level:.4f} · umbral {self.detector.threshold:.4f}",
        ]
        extra = self._extra_context()
        if extra:
            lines.append(extra)
        if repeat:
            lines.append(
                f"🔔 Aviso #{self.detector.alerts_sent} · se repite cada "
                f"{fmt_duration(self.detector.cooldown)}"
            )
        return "\n".join(lines)

    def _recovery_message(self, elapsed):
        return "\n".join([
            "✅ <b>Sonido restaurado</b>",
            f"🎯 {self._header()} · <code>{escape_html(self.target_desc)}</code>",
            f"🔇 Estuvo <b>{fmt_duration(elapsed)}</b> en silencio",
            f"📈 Nivel actual {self.last_level:.4f}",
        ])

    # -- ciclo de vida del hilo -------------------------------------------

    def _on_connected(self):
        """Lo llama la subclase cuando la captura ya esta abierta."""
        self.connected_at = time.monotonic()
        self.error = ""
        if self._failure_reported:
            self._failure_reported = False
            self._dispatch(
                "🔌 <b>Vigilancia restablecida</b>\n"
                f"🎯 {self._header()} · <code>{escape_html(self.target_desc)}</code>"
            )
        self._set_state(WatcherState.PAUSED if self.is_paused() else WatcherState.STARTING)

    def _report_failure(self, exc, consecutive):
        """Avisa por Telegram solo si el fallo persiste, para no spamear ante
        un hipo momentaneo del driver."""
        if consecutive < 2 or self._failure_reported:
            return
        self._failure_reported = True
        self._dispatch(
            "⛔ <b>No se puede vigilar</b>\n"
            f"🎯 {self._header()} · <code>{escape_html(self.target_desc)}</code>\n"
            f"⚠️ {escape_html(exc)}\n"
            "🔁 Se seguira reintentando automaticamente."
        )

    def run(self):
        logger.info("[%s] iniciando vigilancia de '%s'", self.label, self.target_desc)
        backoff = 1.0
        consecutive = 0
        while not self.should_stop():
            start = time.monotonic()
            try:
                self._run_loop()
                break  # salida limpia: nos pidieron parar
            except Exception as e:
                if self.should_stop():
                    break
                ran_for = time.monotonic() - start
                message = f"{type(e).__name__}: {e}"
                self.error = message
                self._set_state(WatcherState.ERROR)
                if ran_for >= HEALTHY_RUN_SECONDS:
                    consecutive, backoff = 1, 1.0
                else:
                    consecutive += 1
                logger.warning(
                    "[%s] fallo (%d seguidos): %s. Reintento en %.0fs",
                    self.label, consecutive, message, backoff,
                )
                self._report_failure(message, consecutive)
                if self._stop_event.wait(backoff):
                    break
                backoff = min(MAX_RECONNECT_DELAY, backoff * 2)
        self._set_state(WatcherState.STOPPED)
        logger.info("[%s] vigilancia detenida", self.label)

    def _run_loop(self):
        raise NotImplementedError

    def _sleep(self, seconds):
        """Espera interrumpible: parar o pausar responde al instante."""
        return self._stop_event.wait(max(0.01, seconds))


# ---------------------------------------------------------------------------
# Dispositivos (microfono / salida de audio)
# ---------------------------------------------------------------------------

class DeviceWatcher(BaseWatcher):
    """Vigila un microfono o toda la salida de audio (loopback), calculando el
    RMS y el pico de cada bloque capturado."""

    kind = "device"

    def __init__(self, device=None, device_name=None, device_kind=None,
                 samplerate=44100, blocksize=None, channels=None, **kwargs):
        super().__init__(**kwargs)
        self._device = device
        self.device_name = str(device_name or (device.name if device is not None else self.target_desc))
        self.device_kind = device_kind if device_kind in (audio_devices.MIC, audio_devices.SPEAKER) else audio_devices.MIC
        self.samplerate = int(samplerate)
        self.channels = channels
        self._forced_blocksize = blocksize
        self.kind = self.device_kind

    @property
    def blocksize(self):
        if self._forced_blocksize:
            return int(self._forced_blocksize)
        seconds = min(max(self.interval, 0.02), MAX_BLOCK_SECONDS)
        return max(256, min(65536, int(self.samplerate * seconds)))

    def _resolve_device(self):
        """Se resuelve por nombre dentro del propio hilo y en cada reconexion,
        para que desenchufar y volver a enchufar unos auriculares recupere la
        vigilancia sola en vez de dejar el hilo muerto."""
        if self._device is not None:
            device, self._device = self._device, None  # solo se usa el primer intento
            return device
        device = audio_devices.resolve(self.device_name, self.device_kind)
        if device is None:
            raise audio_devices.DeviceError(
                f"No se encontro el dispositivo '{self.device_name}'. "
                "Puede estar desconectado o haber cambiado de nombre."
            )
        return device

    def _extra_context(self):
        return f"🎧 Dispositivo: <code>{escape_html(self.device_name)}</code>"

    def _run_loop(self):
        device = self._resolve_device()
        blocksize = self.blocksize
        with device.recorder(
            samplerate=self.samplerate, channels=self.channels, blocksize=blocksize
        ) as recorder:
            self._on_connected()
            while not self.should_stop():
                data = recorder.record(numframes=blocksize)
                rms, peak = analyse_block(data)
                self._process_level(rms, rms=rms, peak=peak)
                # Si el usuario pidio un intervalo mayor que el bloque, se
                # completa la espera aqui (interrumpible).
                extra = self.interval - (blocksize / float(self.samplerate))
                if extra > 0.01 and self._sleep(extra):
                    break


# ---------------------------------------------------------------------------
# Apps concretas
# ---------------------------------------------------------------------------

class AppWatcher(BaseWatcher):
    """Vigila una app por su nombre de proceso ('spotify.exe', 'chrome.exe').

    Si la app no esta abierta o no tiene sesion de audio, se considera silencio
    -- que es justo lo que el usuario quiere saber: dejo de sonar."""

    kind = "app"

    def __init__(self, process_name_contains, poll_interval=None, **kwargs):
        super().__init__(**kwargs)
        self.process_name_contains = process_name_contains
        if poll_interval:
            self.interval = float(poll_interval)
        self.app_running = False

    def _extra_context(self):
        if self.app_running:
            return "🎵 La app esta abierta pero no esta reproduciendo nada."
        return "📴 La app no tiene sesion de audio (cerrada, muteada o sin reproducir)."

    def _run_loop(self):
        # COM se inicializa una vez por hilo; app_audio tolera que `soundcard`
        # ya lo hubiera puesto en otro modo de apartamento.
        with app_audio.com_initialized():
            self._on_connected()
            while not self.should_stop():
                started = time.monotonic()
                entry = app_audio.find_process_entry(self.process_name_contains)
                self.app_running = entry is not None
                peak = entry["peak"] if entry else 0.0
                self._process_level(peak, peak=peak)
                if self._sleep(self.interval - (time.monotonic() - started)):
                    break


# ---------------------------------------------------------------------------
# Compatibilidad con la version anterior
# ---------------------------------------------------------------------------

def send_telegram(token, chat_id, message, parse_mode=None):
    """Envio directo (bloqueante). Se mantiene por compatibilidad; el codigo
    nuevo deberia usar notifier.get_notifier(token).send(...), que no bloquea."""
    ok, error = get_notifier(token).send_now(chat_id, message, parse_mode=parse_mode)
    if not ok:
        logger.error("No se pudo enviar el mensaje a %s: %s", chat_id, error)
    return ok
