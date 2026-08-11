"""
Carga centralizada de la configuracion de Telegram.

Hay dos modos de uso del proyecto y cada uno tiene su archivo:

  - config.json          -> modo bot multi-usuario (bot.py). Solo necesita el
                            bot_token; cada usuario se registra solo al enviar
                            /start y sus ajustes viven en users_data/.
  - config_classic.json  -> modo local de un solo usuario (gui.py, monitor.py).
                            Necesita bot_token + chat_id fijos.

En ambos casos, las variables de entorno tienen prioridad sobre el archivo:

    set ANGRYFURBOT_TOKEN=123456:AA...
    set ANGRYFURBOT_CHAT_ID=5075087755

Asi se puede desplegar el bot en un servidor sin dejar el token escrito en
disco. Si el token vive en un archivo, no lo compartas ni lo subas a git: con
el token cualquiera controla tu bot (revocalo con /revoke en @BotFather).
"""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from storage import (
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_DURATION_SECONDS,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_THRESHOLD,
    MAX_WATCHERS_PER_USER,
    app_dir,
    clamp_cooldown,
    clamp_duration,
    clamp_interval,
    clamp_threshold,
)

# Junto al .py en desarrollo, junto al .exe si se empaqueta (ver storage.app_dir)
BASE_DIR = app_dir()
BOT_CONFIG_PATH = BASE_DIR / "config.json"
CLASSIC_CONFIG_PATH = BASE_DIR / "config_classic.json"

ENV_TOKEN = "ANGRYFURBOT_TOKEN"
ENV_CHAT_ID = "ANGRYFURBOT_CHAT_ID"

# Formato de token de BotFather: <id numerico>:<35 caracteres>
_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_\-]{20,}$")


class ConfigError(Exception):
    """Configuracion ausente o invalida, con un mensaje que se le puede
    mostrar tal cual al usuario final."""


def redact(token):
    """Version del token segura para logs: 8991148894:AA...TfQ"""
    if not token:
        return "(vacio)"
    token = str(token)
    if ":" not in token:
        return token[:4] + "..."
    bot_id, secret = token.split(":", 1)
    return f"{bot_id}:{secret[:2]}...{secret[-3:]}" if len(secret) > 6 else f"{bot_id}:***"


def looks_like_token(token):
    return bool(token) and bool(_TOKEN_RE.match(str(token).strip()))


def _read_json(path):
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path.name} no es un JSON valido (linea {e.lineno}): {e.msg}") from e
    except OSError as e:
        raise ConfigError(f"No se pudo leer {path.name}: {e}") from e
    return data if isinstance(data, dict) else {}


def _as_id_set(values):
    result = set()
    for v in values or []:
        try:
            result.add(str(int(str(v).strip())))
        except (TypeError, ValueError):
            continue
    return frozenset(result)


@dataclass(frozen=True)
class BotConfig:
    """Ajustes del bot multi-usuario (config.json)."""

    token: str
    default_threshold: float = DEFAULT_THRESHOLD
    default_duration: float = DEFAULT_DURATION_SECONDS
    default_cooldown: float = DEFAULT_COOLDOWN_SECONDS
    default_interval: float = DEFAULT_INTERVAL_SECONDS
    # Vacio = cualquiera que encuentre el bot puede usarlo. Como el bot expone
    # los dispositivos y procesos de ESTA maquina, en un equipo compartido
    # conviene poner aqui los chat_id permitidos.
    allowed_chat_ids: frozenset = field(default_factory=frozenset)
    admin_chat_ids: frozenset = field(default_factory=frozenset)
    max_watchers_per_user: int = MAX_WATCHERS_PER_USER

    @property
    def is_open(self):
        return not self.allowed_chat_ids

    def is_allowed(self, chat_id):
        return self.is_open or str(chat_id) in self.allowed_chat_ids

    def is_admin(self, chat_id):
        return str(chat_id) in self.admin_chat_ids


def load_bot_config(path=None):
    path = Path(path) if path else BOT_CONFIG_PATH
    raw = _read_json(path)

    token = (os.environ.get(ENV_TOKEN) or raw.get("bot_token") or "").strip()
    if not token or token.upper().startswith("TU_BOT_TOKEN"):
        raise ConfigError(
            f"Falta el bot_token.\n"
            f"  - Pon tu token en {path.name}:  {{\"bot_token\": \"123456:AA...\"}}\n"
            f"  - O exporta la variable de entorno {ENV_TOKEN}.\n"
            f"Consigue el token con /newbot en @BotFather."
        )
    if not looks_like_token(token):
        raise ConfigError(
            f"El bot_token no tiene el formato de BotFather "
            f"(<numero>:<letras>). Valor leido: {redact(token)}"
        )

    max_watchers = raw.get("max_watchers_per_user", MAX_WATCHERS_PER_USER)
    try:
        max_watchers = max(1, min(50, int(max_watchers)))
    except (TypeError, ValueError):
        max_watchers = MAX_WATCHERS_PER_USER

    return BotConfig(
        token=token,
        default_threshold=clamp_threshold(raw.get("default_threshold", DEFAULT_THRESHOLD)),
        default_duration=clamp_duration(raw.get("default_duration_seconds", DEFAULT_DURATION_SECONDS)),
        default_cooldown=clamp_cooldown(raw.get("default_cooldown_seconds", DEFAULT_COOLDOWN_SECONDS)),
        default_interval=clamp_interval(raw.get("default_interval_seconds", DEFAULT_INTERVAL_SECONDS)),
        allowed_chat_ids=_as_id_set(raw.get("allowed_chat_ids")),
        admin_chat_ids=_as_id_set(raw.get("admin_chat_ids")),
        max_watchers_per_user=max_watchers,
    )


@dataclass
class ClassicConfig:
    """Ajustes del modo local de un solo usuario (config_classic.json)."""

    token: str = ""
    chat_id: str = ""
    cooldown: float = DEFAULT_COOLDOWN_SECONDS
    raw: dict = field(default_factory=dict)

    @property
    def is_complete(self):
        return bool(self.token) and bool(self.chat_id)

    def describe_problem(self):
        """Mensaje corto y accionable para mostrar en la GUI si falta algo."""
        if not self.token:
            return (
                "Falta el bot_token de Telegram.\n"
                f"Ponlo en {CLASSIC_CONFIG_PATH.name} (telegram.bot_token) "
                f"o en la variable de entorno {ENV_TOKEN}."
            )
        if not looks_like_token(self.token):
            return f"El bot_token no parece valido: {redact(self.token)}"
        if not self.chat_id:
            return (
                "Falta el chat_id de Telegram.\n"
                "Ejecuta 'python test_telegram.py' para averiguar el tuyo."
            )
        return ""


def load_classic_config(path=None):
    path = Path(path) if path else CLASSIC_CONFIG_PATH
    raw = _read_json(path)
    telegram = raw.get("telegram") if isinstance(raw.get("telegram"), dict) else {}

    token = (os.environ.get(ENV_TOKEN) or telegram.get("bot_token") or "").strip()
    chat_id = str(os.environ.get(ENV_CHAT_ID) or telegram.get("chat_id") or "").strip()
    if token.upper().startswith("TU_BOT_TOKEN"):
        token = ""

    return ClassicConfig(
        token=token,
        chat_id=chat_id,
        cooldown=clamp_cooldown(telegram.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS)),
        raw=raw,
    )


def save_classic_credentials(token, chat_id, cooldown=None, path=None):
    """Guarda token/chat_id en config_classic.json conservando el resto del
    archivo (secciones monitor_microphone / monitor_speaker del modo clasico)."""
    path = Path(path) if path else CLASSIC_CONFIG_PATH
    raw = _read_json(path)
    telegram = raw.get("telegram") if isinstance(raw.get("telegram"), dict) else {}
    telegram["bot_token"] = str(token).strip()
    telegram["chat_id"] = str(chat_id).strip()
    if cooldown is not None:
        telegram["cooldown_seconds"] = clamp_cooldown(cooldown)
    telegram.setdefault("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS)
    raw["telegram"] = telegram
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    return raw
