"""
Envio de notificaciones a Telegram desde hilos de audio, sin bloquearlos.

Por que existe este modulo:

  - Los watchers corren en hilos que leen bloques de audio en tiempo real. Si
    llamaran a `requests.post` directamente, el hilo quedaria parado hasta 10s
    esperando a la red mientras el buffer de audio se desborda (la libreria
    soundcard avisa con "data discontinuity in recording"). Aqui el watcher
    solo encola el mensaje y sigue midiendo.
  - Telegram limita a ~1 mensaje por segundo y por chat, y responde 429 con un
    `retry_after` cuando te pasas. El hilo enviador respeta ese ritmo y
    reintenta con backoff, en vez de perder alertas.
  - Un solo objeto Notifier por token sirve a todos los usuarios del bot.

No depende de python-telegram-bot a proposito: usa la API HTTP directamente,
asi que puede llamarse desde cualquier hilo sin tener que cruzar al event loop
de asyncio del bot.
"""

import html
import logging
import queue
import threading
import time
from dataclasses import dataclass, field

import requests

logger = logging.getLogger("angryfurbot.notifier")

API_BASE = "https://api.telegram.org/bot{token}/{method}"

# Ritmo minimo entre mensajes al MISMO chat (Telegram: ~1 msg/s por chat).
PER_CHAT_INTERVAL = 1.1
# Ritmo minimo global entre mensajes (Telegram: ~30 msg/s en total).
GLOBAL_INTERVAL = 0.05

MAX_ATTEMPTS = 4
QUEUE_SIZE = 500
REQUEST_TIMEOUT = 15


def escape_html(text):
    """Escapa texto para parse_mode=HTML de Telegram."""
    return html.escape(str(text), quote=False)


@dataclass(order=False)
class _Message:
    chat_id: str
    text: str
    parse_mode: str = None
    attempts: int = 0
    created_at: float = field(default_factory=time.monotonic)


class TelegramNotifier:
    """Cola de salida hacia Telegram con un hilo enviador propio."""

    def __init__(self, token, on_blocked=None, queue_size=QUEUE_SIZE):
        self.token = token
        self.on_blocked = on_blocked  # callback(chat_id) si el usuario bloqueo al bot
        self._queue = queue.Queue(maxsize=queue_size)
        self._thread = None
        self._stop = threading.Event()
        self._session = None
        self._last_per_chat = {}
        self._last_global = 0.0
        self._lock = threading.Lock()
        self.stats = {"queued": 0, "sent": 0, "failed": 0, "dropped": 0}

    # -- ciclo de vida --------------------------------------------------

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self
            self._stop.clear()
            self._session = requests.Session()
            self._thread = threading.Thread(
                target=self._run, name="telegram-notifier", daemon=True
            )
            self._thread.start()
        return self

    def stop(self, timeout=5.0):
        """Pide el cierre y espera a que se vacie la cola (hasta timeout)."""
        deadline = time.monotonic() + timeout
        while not self._queue.empty() and time.monotonic() < deadline:
            time.sleep(0.05)
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=max(0.1, deadline - time.monotonic()))
        session, self._session = self._session, None
        if session is not None:
            try:
                session.close()
            except Exception:
                pass

    # -- API publica ----------------------------------------------------

    def send(self, chat_id, text, parse_mode=None):
        """Encola un mensaje. No bloquea nunca. Devuelve False si la cola
        estaba llena (situacion anormal: significa que Telegram lleva mucho
        rato inalcanzable)."""
        if not chat_id or not text:
            return False
        if self._thread is None or not self._thread.is_alive():
            self.start()
        try:
            self._queue.put_nowait(_Message(str(chat_id), str(text), parse_mode))
        except queue.Full:
            self.stats["dropped"] += 1
            logger.error("Cola de Telegram llena, se descarto un mensaje para %s", chat_id)
            return False
        self.stats["queued"] += 1
        return True

    def send_now(self, chat_id, text, parse_mode=None, timeout=REQUEST_TIMEOUT):
        """Envio sincrono (bloquea). Solo para comandos interactivos como
        /test, nunca desde un hilo de audio."""
        ok, _retry_after, _permanent, error = self._post(
            _Message(str(chat_id), str(text), parse_mode), timeout=timeout
        )
        return ok, error

    def pending(self):
        return self._queue.qsize()

    # -- hilo enviador --------------------------------------------------

    def _run(self):
        logger.info("Notificador de Telegram iniciado")
        while not self._stop.is_set() or not self._queue.empty():
            try:
                msg = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                self._deliver(msg)
            except Exception as e:  # nunca dejar morir al hilo enviador
                logger.error("Error inesperado enviando a %s: %s", msg.chat_id, e)
            finally:
                self._queue.task_done()
        logger.info("Notificador de Telegram detenido")

    def _pace(self, chat_id):
        """Espera lo justo para no pasarse de los limites de Telegram."""
        now = time.monotonic()
        wait = max(
            self._last_per_chat.get(chat_id, 0.0) + PER_CHAT_INTERVAL - now,
            self._last_global + GLOBAL_INTERVAL - now,
        )
        if wait > 0:
            # Se corta si nos piden parar, para no retrasar el cierre.
            self._stop.wait(min(wait, 5.0))
        stamp = time.monotonic()
        self._last_per_chat[chat_id] = stamp
        self._last_global = stamp

    def _deliver(self, msg):
        while msg.attempts < MAX_ATTEMPTS:
            msg.attempts += 1
            self._pace(msg.chat_id)
            ok, retry_after, permanent, error = self._post(msg)
            if ok:
                self.stats["sent"] += 1
                return True
            if permanent:
                self.stats["failed"] += 1
                logger.warning(
                    "Telegram rechazo el mensaje para %s: %s (no se reintenta)",
                    msg.chat_id, error,
                )
                return False
            delay = retry_after if retry_after else min(30.0, 2.0 ** msg.attempts)
            logger.warning(
                "Fallo enviando a %s (intento %d/%d): %s. Reintento en %.0fs",
                msg.chat_id, msg.attempts, MAX_ATTEMPTS, error, delay,
            )
            if self._stop.wait(delay):
                break
        self.stats["failed"] += 1
        return False

    def _post(self, msg, timeout=REQUEST_TIMEOUT):
        """Devuelve (ok, retry_after, permanent, error)."""
        session = self._session or requests
        payload = {
            "chat_id": msg.chat_id,
            "text": msg.text,
            "disable_web_page_preview": True,
        }
        if msg.parse_mode:
            payload["parse_mode"] = msg.parse_mode
        url = API_BASE.format(token=self.token, method="sendMessage")
        try:
            resp = session.post(url, data=payload, timeout=timeout)
        except requests.RequestException as e:
            return False, None, False, f"red: {e}"

        if resp.status_code == 200:
            return True, None, False, None

        retry_after = None
        description = resp.text
        try:
            body = resp.json()
            description = body.get("description", description)
            retry_after = (body.get("parameters") or {}).get("retry_after")
        except ValueError:
            pass

        if resp.status_code == 429:
            return False, float(retry_after or 5), False, f"429 {description}"

        if resp.status_code in (401, 403):
            # 401 = token invalido/revocado. 403 = el usuario bloqueo al bot.
            if resp.status_code == 403 and self.on_blocked:
                try:
                    self.on_blocked(msg.chat_id)
                except Exception:
                    pass
            return False, None, True, f"{resp.status_code} {description}"

        if 400 <= resp.status_code < 500:
            # 400 suele ser texto mal formado (Markdown roto) o chat inexistente:
            # reintentar no ayuda.
            return False, None, True, f"{resp.status_code} {description}"

        return False, None, False, f"{resp.status_code} {description}"


# ---------------------------------------------------------------------------
# Singleton por token: bot.py, gui.py y monitor.py comparten el mismo hilo
# enviador si usan el mismo bot.
# ---------------------------------------------------------------------------

_instances = {}
_instances_lock = threading.Lock()


def get_notifier(token, on_blocked=None):
    with _instances_lock:
        notifier = _instances.get(token)
        if notifier is None:
            notifier = _instances[token] = TelegramNotifier(token, on_blocked=on_blocked)
            notifier.start()
        elif on_blocked is not None:
            notifier.on_blocked = on_blocked
        return notifier


def shutdown_all(timeout=5.0):
    with _instances_lock:
        instances = list(_instances.values())
        _instances.clear()
    for notifier in instances:
        notifier.stop(timeout=timeout)
