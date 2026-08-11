"""
Bot de Telegram multi-usuario del Monitor de Audio (AngryFurBot).

Cualquiera que le escriba /start queda registrado y puede configurar, de forma
totalmente independiente de los demas usuarios:

  - su microfono
  - su salida de audio (todo lo que suene por los altavoces)
  - una app concreta (Spotify, un navegador, un juego...)

Si el audio vigilado queda por debajo del umbral durante mas tiempo del
configurado, el bot avisa y sigue avisando cada `cooldown` segundos (30s o
menos) hasta que el sonido vuelve, momento en el que manda el aviso de
recuperacion.

Notas de arquitectura:

  - Ninguna llamada bloqueante ocurre en el event loop. Enumerar dispositivos
    (soundcard) y sesiones de audio (pycaw) puede tardar cientos de
    milisegundos y ademas toca COM, asi que se despacha a executors dedicados
    (uno por libreria, para no mezclar modos de apartamento COM).
  - Las alertas salen por una cola propia (notifier.py) que respeta el limite
    de ~1 mensaje/segundo por chat de Telegram; los hilos de audio nunca
    esperan a la red.
  - El estado de cada usuario vive en users_data/<chat_id>.json y las
    vigilancias en marcha en un MonitorSupervisor compartido.

Uso:
    1. Pon tu bot_token en config.json (o en la variable ANGRYFURBOT_TOKEN)
    2. python bot.py
    3. Cada usuario le escribe /start a tu bot desde Telegram
"""

import asyncio
import functools
import logging
import re
import sys
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

import app_audio
import audio_devices
import storage
import telegram_config
from audio_backends import STATE_EMOJI, WatcherState, fmt_duration
from monitor import MonitorSupervisor, SupervisorError
from notifier import get_notifier

# Los nombres de dispositivos y procesos pueden traer caracteres fuera del
# codepage de la consola de Windows; si la salida esta redirigida a un archivo,
# escribirlos lanzaria UnicodeEncodeError en mitad de un log.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)] if sys.stderr else [logging.NullHandler()],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("angryfurbot.bot")

CONFIG = telegram_config.load_bot_config()
NOTIFIER = get_notifier(CONFIG.token)
SUPERVISOR = MonitorSupervisor(
    notifier=NOTIFIER, max_per_owner=CONFIG.max_watchers_per_user
)

TYPE_LABELS = {"mic": "🎙️ Microfono", "speaker": "🔊 Salida de audio", "app": "🎵 App"}

THRESHOLD_PRESETS = [
    ("🐜 Muy sensible", 0.005),
    ("🎯 Normal", 0.01),
    ("🛡️ Poco sensible", 0.02),
    ("🧱 Insensible", 0.05),
]
DURATION_PRESETS = [10, 15, 30, 60, 120]
COOLDOWN_PRESETS = [10, 15, 20, 30]      # nunca mas de 30s, por requisito
INTERVAL_PRESETS = [0.25, 0.5, 1.0, 2.0]
MUTE_PRESETS = [15, 30, 60, 240]         # minutos

PAGE_SIZE = 8
LIVE_REFRESH_SECONDS = 3
LIVE_MAX_TICKS = 30                      # ~90 s de refresco automatico

# Tareas de refresco en vivo por mensaje: (chat_id, message_id) -> asyncio.Task
LIVE_TASKS = {}

HELP_TEXT = (
    "🎧 *AngryFurBot* — ayuda\n\n"
    "Lo normal es usar los botones de /start\\. Tambien hay comandos:\n\n"
    "*Ver que hay disponible*\n"
    "/devices — microfonos y salidas de audio\n"
    "/apps — apps con audio activo ahora mismo\n\n"
    "*Crear vigilancias*\n"
    "/watch\\_mic `texto` \\[umbral\\] \\[segundos\\]\n"
    "/watch\\_speaker `texto` \\[umbral\\] \\[segundos\\]\n"
    "/watch\\_app `proceso` \\[umbral\\] \\[segundos\\]\n\n"
    "*Gestionar*\n"
    "/list — tus vigilancias\n"
    "/status — estado en vivo de todas\n"
    "/stop `id` · /stopall\n"
    "/setthreshold `id` `valor` — umbral de silencio\n"
    "/setduration `id` `segundos` — silencio antes del primer aviso\n"
    "/setcooldown `segundos` — cada cuanto se repite el aviso \\(max 30\\)\n"
    "/mute `minutos` · /unmute — pausa solo los avisos\n\n"
    "/test — mensaje de prueba\n"
    "/id — tu chat\\_id\n"
    "/help — esta ayuda"
)


# ---------------------------------------------------------------------------
# MarkdownV2: escapar primero, poner formato despues
# ---------------------------------------------------------------------------

_MD2_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"
_MD2_RE = re.compile(f"([{re.escape(_MD2_SPECIAL)}])")


def md(text):
    """Escapa texto plano para MarkdownV2."""
    return _MD2_RE.sub(r"\\\1", str(text))


def bold(text):
    return f"*{md(text)}*"


def italic(text):
    return f"_{md(text)}_"


def code(text):
    """Bloque `codigo`: dentro solo hay que escapar ` y \\."""
    return "`" + str(text).replace("\\", "\\\\").replace("`", "\\`") + "`"


def level_bar(level, threshold, width=12):
    """Barra de nivel donde el umbral cae a un tercio de la barra, para que se
    vea de un vistazo si el audio esta por encima o por debajo."""
    reference = max(float(threshold) * 3.0, 1e-6)
    filled = int(round(min(1.0, max(0.0, float(level) / reference)) * width))
    marker = max(1, int(round(width / 3.0)))
    cells = []
    for i in range(width):
        if i < filled:
            cells.append("▓")
        elif i == marker - 1:
            cells.append("┊")  # marca visual del umbral
        else:
            cells.append("░")
    return "".join(cells)


# ---------------------------------------------------------------------------
# Ejecucion de trabajo bloqueante fuera del event loop
# ---------------------------------------------------------------------------

async def run_devices(fn, *args, **kwargs):
    """soundcard en su hilo dedicado."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        audio_devices.executor(), functools.partial(fn, *args, **kwargs)
    )


async def run_apps(fn, *args, **kwargs):
    """pycaw/COM en su hilo dedicado (nunca el mismo que soundcard)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        app_audio.executor(), functools.partial(fn, *args, **kwargs)
    )


# ---------------------------------------------------------------------------
# Ayudas de dominio
# ---------------------------------------------------------------------------

def user_of(update):
    return str(update.effective_chat.id)


def mute_checker(chat_id):
    chat_id = str(chat_id)
    return lambda: storage.is_muted(chat_id)


def start_watcher(chat_id, watcher_cfg, user_data=None):
    """Arranca (o reinicia) una vigilancia ya guardada en storage."""
    user_data = user_data or storage.load_user(chat_id)
    return SUPERVISOR.start(
        chat_id,
        watcher_cfg,
        chat_id=chat_id,
        cooldown=storage.effective_cooldown(user_data, watcher_cfg),
        notify_recovery=user_data.get("notify_recovery", True),
        mute_check=mute_checker(chat_id),
    )


def create_watcher(chat_id, w_type, target, threshold=None, duration=None,
                   interval=None, cooldown=None):
    """Guarda + arranca. Si el arranque falla, deshace el guardado para no
    dejar vigilancias fantasma en el JSON del usuario."""
    cfg = storage.add_watcher(
        chat_id, w_type, target,
        threshold=threshold, duration=duration, interval=interval, cooldown=cooldown,
    )
    try:
        start_watcher(chat_id, cfg)
    except Exception:
        storage.remove_watcher(chat_id, cfg["id"])
        raise
    return cfg


def watcher_view(chat_id, wid):
    """Une lo guardado en disco con el estado en vivo del hilo."""
    cfg = storage.get_watcher(chat_id, wid)
    if cfg is None:
        return None, None
    watcher = SUPERVISOR.get(chat_id, wid)
    snap = watcher.snapshot() if watcher else None
    return cfg, snap


def state_of(cfg, snap):
    if snap is None or not snap["alive"]:
        return WatcherState.STOPPED
    if cfg.get("paused"):
        return WatcherState.PAUSED
    return snap["state"]


def live_state(chat_id, cfg):
    """Estado de una vigilancia sin releer el JSON del usuario (para bucles)."""
    watcher = SUPERVISOR.get(chat_id, cfg["id"])
    return state_of(cfg, watcher.snapshot() if watcher else None), watcher


def summary_counts(chat_id, data=None):
    data = data or storage.load_user(chat_id)
    counts = {"active": 0, "alerting": 0, "paused": 0, "stopped": 0, "error": 0, "silence": 0}
    for cfg in data["watchers"]:
        state, _watcher = live_state(chat_id, cfg)
        if state in (WatcherState.ACTIVE, WatcherState.STARTING):
            counts["active"] += 1
        elif state == WatcherState.ALERTING:
            counts["alerting"] += 1
        elif state == WatcherState.SILENCE:
            counts["silence"] += 1
        elif state == WatcherState.PAUSED:
            counts["paused"] += 1
        elif state == WatcherState.ERROR:
            counts["error"] += 1
        else:
            counts["stopped"] += 1
    return counts


# ---------------------------------------------------------------------------
# Renderizado
# ---------------------------------------------------------------------------

def render_dashboard(chat_id):
    data = storage.load_user(chat_id)
    counts = summary_counts(chat_id, data)
    total = len(data["watchers"])

    lines = [
        "🐾 " + bold("AngryFurBot"),
        italic("Monitor de silencio en tiempo real"),
        "",
    ]
    if total == 0:
        lines.append(md("Todavia no vigilas nada. Pulsa ➕ Nueva vigilancia."))
    else:
        chips = []
        if counts["active"]:
            chips.append(f"🟢 {counts['active']} activa")
        if counts["silence"]:
            chips.append(f"🟠 {counts['silence']} en silencio")
        if counts["alerting"]:
            chips.append(f"🔴 {counts['alerting']} alertando")
        if counts["paused"]:
            chips.append(f"🟡 {counts['paused']} en pausa")
        if counts["error"]:
            chips.append(f"⛔ {counts['error']} con error")
        if counts["stopped"]:
            chips.append(f"⚪ {counts['stopped']} detenida")
        lines.append(md(" · ".join(chips)))
        lines.append(md(f"📦 {total} de {CONFIG.max_watchers_per_user} vigilancias"))

    lines.append(md(f"🔁 Repite el aviso cada {int(data['cooldown_seconds'])}s"))
    lines.append(md("🔔 Aviso de recuperacion: " + ("si" if data.get("notify_recovery", True) else "no")))

    muted_until = float(data.get("muted_until") or 0)
    if muted_until > time.time():
        remaining = fmt_duration(muted_until - time.time())
        lines.append(md(f"😴 Avisos silenciados {remaining} mas"))

    lines.append("")
    lines.append(md("Elige una opcion:"))
    return "\n".join(lines)


def render_card(chat_id, wid):
    cfg, snap = watcher_view(chat_id, wid)
    if cfg is None:
        return md("⚠️ Esa vigilancia ya no existe.")

    state = state_of(cfg, snap)
    emoji = STATE_EMOJI.get(state, "•")
    state_text = {
        WatcherState.ACTIVE: "Activo, hay sonido",
        WatcherState.STARTING: "Iniciando",
        WatcherState.SILENCE: "En silencio (aun sin avisar)",
        WatcherState.ALERTING: "Silencio detectado",
        WatcherState.PAUSED: "En pausa",
        WatcherState.STOPPED: "Detenida",
        WatcherState.ERROR: "Error de captura",
    }.get(state, state)

    level = snap["level"] if snap else 0.0
    cooldown = storage.effective_cooldown(storage.load_user(chat_id), cfg)

    lines = [
        f"{TYPE_LABELS.get(cfg['type'], cfg['type'])} {md('·')} " + bold(f"vigilancia #{cfg['id']}"),
        f"{emoji} " + bold(state_text),
        "",
        "🎯 " + code(cfg["target"]),
        "📈 " + md(level_bar(level, cfg["threshold"])) + "  " + code(f"{level:.4f}"),
        md(f"🚦 Umbral {cfg['threshold']:.4f} · chequeo cada {cfg['interval']:g}s"),
    ]

    if state in (WatcherState.SILENCE, WatcherState.ALERTING) and snap:
        lines.append(md(f"⏱️ En silencio desde hace {fmt_duration(snap['silence_elapsed'])}"))
    else:
        lines.append(md(f"⏱️ Avisa tras {fmt_duration(cfg['duration'])} de silencio"))

    alerts = snap["alerts_sent"] if snap else 0
    total_alerts = int(cfg.get("alerts_total", 0)) + alerts
    lines.append(md(f"🔔 {alerts} avisos ahora · {total_alerts} en total"))
    lines.append(md(f"🔁 Se repite cada {int(cooldown)}s"))

    if snap and snap["alive"]:
        lines.append(md(f"🕒 En marcha desde hace {fmt_duration(snap['uptime'])}"))
    if state == WatcherState.ERROR and snap and snap["error"]:
        lines.append("⛔ " + md(snap["error"]))
        lines.append(md("🔁 Reintentando automaticamente..."))

    return "\n".join(lines)


def render_status(chat_id):
    data = storage.load_user(chat_id)
    if not data["watchers"]:
        return md("No tienes ninguna vigilancia configurada.")
    lines = ["📊 " + bold("Estado en vivo"), ""]
    for cfg in data["watchers"]:
        state, watcher = live_state(chat_id, cfg)
        emoji = STATE_EMOJI.get(state, "•")
        level = watcher.last_level if watcher else 0.0
        lines.append(
            f"{emoji} " + bold(f"#{cfg['id']}") + " " + code(cfg["target"])
        )
        lines.append("   " + md(level_bar(level, cfg["threshold"], width=10)) + " " + code(f"{level:.4f}"))
    lines.append("")
    lines.append(italic("Toca una vigilancia para ver su ficha completa."))
    return "\n".join(lines)


def render_settings(chat_id):
    data = storage.load_user(chat_id)
    muted_until = float(data.get("muted_until") or 0)
    lines = [
        "⚙️ " + bold("Ajustes"),
        "",
        md(f"🔁 Cooldown global: {int(data['cooldown_seconds'])}s"),
        md("🔔 Aviso de recuperacion: " + ("activado" if data.get("notify_recovery", True) else "desactivado")),
    ]
    if muted_until > time.time():
        lines.append(md(f"😴 Silenciado {fmt_duration(muted_until - time.time())} mas"))
    else:
        lines.append(md("😴 Avisos: activos"))
    lines.append("")
    lines.append(italic("El cooldown global se aplica a las vigilancias que no tengan uno propio."))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Teclados
# ---------------------------------------------------------------------------

def kb_dashboard(chat_id):
    data = storage.load_user(chat_id)
    any_running = any(not w.get("paused") for w in data["watchers"])
    toggle = ("⏸️ Pausar todo", "c:pauseall:1") if any_running else ("▶️ Reanudar todo", "c:pauseall:0")
    rows = [
        [InlineKeyboardButton("📋 Mis vigilancias", callback_data="m:list"),
         InlineKeyboardButton("➕ Nueva", callback_data="n:start")],
        [InlineKeyboardButton("📊 Estado en vivo", callback_data="m:status"),
         InlineKeyboardButton(toggle[0], callback_data=toggle[1])],
        [InlineKeyboardButton("🎧 Dispositivos", callback_data="m:dev"),
         InlineKeyboardButton("🎵 Apps activas", callback_data="m:apps")],
        [InlineKeyboardButton("⚙️ Ajustes", callback_data="m:cfg"),
         InlineKeyboardButton("❓ Ayuda", callback_data="m:help")],
    ]
    return InlineKeyboardMarkup(rows)


def kb_back(target="m:home", label="⬅️ Volver"):
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=target)]])


def kb_watcher_list(chat_id):
    data = storage.load_user(chat_id)
    rows = []
    for cfg in data["watchers"]:
        state, _watcher = live_state(chat_id, cfg)
        emoji = STATE_EMOJI.get(state, "•")
        target = cfg["target"]
        if len(target) > 22:
            target = target[:21] + "…"
        rows.append([InlineKeyboardButton(
            f"{emoji} #{cfg['id']} {target}", callback_data=f"w:c:{cfg['id']}"
        )])
    rows.append([InlineKeyboardButton("➕ Nueva vigilancia", callback_data="n:start")])
    rows.append([InlineKeyboardButton("⬅️ Volver", callback_data="m:home")])
    return InlineKeyboardMarkup(rows)


def kb_card(chat_id, wid, live=False):
    cfg = storage.get_watcher(chat_id, wid)
    paused = bool(cfg and cfg.get("paused"))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Reanudar" if paused else "⏸️ Pausar", callback_data=f"w:t:{wid}"),
         InlineKeyboardButton("🔄 Actualizar", callback_data=f"w:c:{wid}"),
         InlineKeyboardButton("🛑 Parar live" if live else "📡 En vivo",
                              callback_data=f"w:stoplive:{wid}" if live else f"w:live:{wid}")],
        [InlineKeyboardButton("🚦 Umbral", callback_data=f"w:thr:{wid}"),
         InlineKeyboardButton("⏱️ Espera", callback_data=f"w:dur:{wid}"),
         InlineKeyboardButton("🔁 Repetir", callback_data=f"w:cd:{wid}")],
        [InlineKeyboardButton("⚡ Chequeo", callback_data=f"w:int:{wid}"),
         InlineKeyboardButton("🗑️ Eliminar", callback_data=f"w:d:{wid}")],
        [InlineKeyboardButton("⬅️ Mis vigilancias", callback_data="m:list")],
    ])


def kb_settings(chat_id):
    data = storage.load_user(chat_id)
    recovery_on = data.get("notify_recovery", True)
    muted = float(data.get("muted_until") or 0) > time.time()
    rows = [
        [InlineKeyboardButton("🔁 Cooldown global", callback_data="c:cdmenu")],
        [InlineKeyboardButton(
            "🔔 Aviso de recuperacion: " + ("ON" if recovery_on else "OFF"),
            callback_data=f"c:rec:{0 if recovery_on else 1}")],
        [InlineKeyboardButton("🔊 Quitar silencio" if muted else "😴 Silenciar avisos",
                              callback_data="c:unmute" if muted else "c:mutemenu")],
        [InlineKeyboardButton("🗑️ Borrar todas mis vigilancias", callback_data="c:wipe")],
        [InlineKeyboardButton("⬅️ Volver", callback_data="m:home")],
    ]
    return InlineKeyboardMarkup(rows)


def kb_values(prefix, values, formatter=str, back="m:home", extra_rows=None, per_row=4):
    rows = []
    row = []
    for value in values:
        row.append(InlineKeyboardButton(formatter(value), callback_data=f"{prefix}:{value}"))
        if len(row) == per_row:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    for extra in (extra_rows or []):
        rows.append(extra)
    rows.append([InlineKeyboardButton("⬅️ Volver", callback_data=back)])
    return InlineKeyboardMarkup(rows)


def kb_type_select():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎙️ Microfono", callback_data="n:type:mic")],
        [InlineKeyboardButton("🔊 Salida de audio (todo el sistema)", callback_data="n:type:speaker")],
        [InlineKeyboardButton("🎵 Una app concreta", callback_data="n:type:app")],
        [InlineKeyboardButton("⬅️ Cancelar", callback_data="m:home")],
    ])


def kb_candidates(flow):
    """Lista paginada de dispositivos/apps con buscador."""
    candidates = flow.get("candidates", [])
    page = flow.get("page", 0)
    pages = max(1, (len(candidates) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    flow["page"] = page

    rows = []
    for index in range(page * PAGE_SIZE, min(len(candidates), (page + 1) * PAGE_SIZE)):
        label = candidates[index]["label"]
        if len(label) > 34:
            label = label[:33] + "…"
        rows.append([InlineKeyboardButton(label, callback_data=f"n:pick:{index}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"n:page:{page - 1}"))
    if pages > 1:
        nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="n:noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"n:page:{page + 1}"))
    if nav:
        rows.append(nav)

    tools = [InlineKeyboardButton("🔄 Refrescar", callback_data="n:refresh")]
    if flow.get("type") == "app":
        tools.append(InlineKeyboardButton("🔎 Buscar", callback_data="n:search"))
        tools.append(InlineKeyboardButton("✏️ Escribir", callback_data="n:manual"))
    rows.append(tools)
    rows.append([InlineKeyboardButton("⬅️ Cancelar", callback_data="n:start")])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Candidatos para el flujo de creacion
# ---------------------------------------------------------------------------

async def build_candidates(w_type, query=None, refresh=False):
    """Devuelve [{'label':..., 'value':...}] para el selector."""
    if w_type == "app":
        suggestions = await run_apps(app_audio.suggest_targets, limit=40, query=query)
        items = []
        for item in suggestions:
            icon = "🔊" if item["playing"] else "▫️"
            level = f"  ({item['peak']:.3f})" if item["playing"] else ""
            items.append({"label": f"{icon} {item['name']}{level}", "value": item["name"]})
        return items

    kind = audio_devices.SPEAKER if w_type == "speaker" else audio_devices.MIC
    devices = await run_devices(audio_devices.list_devices, kind, refresh=refresh)
    return [{"label": d.label(), "value": d.name} for d in devices]


# ---------------------------------------------------------------------------
# Vistas informativas
# ---------------------------------------------------------------------------

async def devices_view():
    try:
        mics = await run_devices(audio_devices.list_microphones, refresh=True)
        outs = await run_devices(audio_devices.list_loopback_devices)
    except Exception as e:
        return md(f"❌ No se pudieron listar los dispositivos: {e}")

    lines = ["🎧 " + bold("Dispositivos disponibles"), "", "🎙️ " + bold("Microfonos")]
    lines += [md("• " + d.name + (" ⭐" if d.is_default else "")) for d in mics] or [italic("ninguno")]
    lines += ["", "🔊 " + bold("Salidas de audio (loopback)")]
    lines += [md("• " + d.name + (" ⭐" if d.is_default else "")) for d in outs] or [italic("ninguna")]
    lines += ["", italic("⭐ = predeterminado del sistema")]
    return "\n".join(lines)


async def apps_view():
    try:
        sessions = await run_apps(app_audio.list_app_sessions_detailed)
    except Exception as e:
        return md(f"❌ No se pudieron listar las apps: {e}")

    if not sessions:
        return md("🎵 Ninguna app tiene sesion de audio ahora mismo.\n"
                  "Abre algo que suene y vuelve a pulsar Apps activas.")

    lines = ["🎵 " + bold("Apps con sesion de audio"), ""]
    for entry in sessions:
        icon = "🔊" if entry["active"] else "▫️"
        lines.append(f"{icon} " + code(entry["name"]) + " " + md(f"nivel {entry['peak']:.3f}"))
    lines += ["", italic("El nivel que ves aqui te dice que umbral poner.")]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Edicion segura de mensajes + refresco en vivo
# ---------------------------------------------------------------------------

async def safe_edit(query, text, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2):
    try:
        await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except BadRequest as e:
        message = str(e).lower()
        if "not modified" in message:
            return
        if "can't parse entities" in message:
            # Red de seguridad: si algun nombre raro rompe el MarkdownV2,
            # se manda en texto plano en vez de perder la respuesta.
            logger.warning("MarkdownV2 invalido, se envia en texto plano: %s", e)
            plain = re.sub(r"\\([_*\[\]()~`>#+\-=|{}.!\\])", r"\1", text)
            await query.edit_message_text(plain, reply_markup=reply_markup)
            return
        raise


def cancel_live(chat_id, message_id):
    task = LIVE_TASKS.pop((str(chat_id), int(message_id)), None)
    if task and not task.done():
        task.cancel()
        return True
    return False


async def live_loop(context, chat_id, message_id, wid):
    """Refresca la tarjeta cada pocos segundos durante un rato acotado, para
    no editar mensajes eternamente contra los limites de Telegram."""
    try:
        for _tick in range(LIVE_MAX_TICKS):
            await asyncio.sleep(LIVE_REFRESH_SECONDS)
            if storage.get_watcher(chat_id, wid) is None:
                break
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=render_card(chat_id, wid),
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=kb_card(chat_id, wid, live=True),
                )
            except BadRequest as e:
                if "not modified" in str(e).lower():
                    continue
                break
            except TelegramError:
                break
    except asyncio.CancelledError:
        raise
    finally:
        LIVE_TASKS.pop((str(chat_id), int(message_id)), None)
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id,
                reply_markup=kb_card(chat_id, wid, live=False),
            )
        except TelegramError:
            pass


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------

async def cmd_start(update, context):
    chat_id = user_of(update)
    user = update.effective_user
    storage.ensure_user(chat_id, username=(user.username or user.full_name) if user else "")
    await update.message.reply_text(
        render_dashboard(chat_id),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=kb_dashboard(chat_id),
    )


async def cmd_help(update, context):
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_id(update, context):
    await update.message.reply_text(
        "🆔 " + code(user_of(update)), parse_mode=ParseMode.MARKDOWN_V2
    )


async def cmd_devices(update, context):
    await update.message.reply_text(
        await devices_view(), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_back()
    )


async def cmd_apps(update, context):
    await update.message.reply_text(
        await apps_view(), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_back()
    )


def parse_watch_args(args, default_threshold, default_duration):
    """[texto...] [umbral] [duracion]: el nombre puede llevar espacios, asi que
    umbral y duracion solo se leen si los ultimos tokens son numeros."""
    tokens = list(args)
    while tokens:
        try:
            float(tokens[-1].replace(",", "."))
        except ValueError:
            break
        tokens = tokens[:-1]
    numeric = [t.replace(",", ".") for t in args[len(tokens):]]

    threshold, duration = default_threshold, default_duration
    if len(numeric) >= 1:
        duration = float(numeric[-1])
    if len(numeric) >= 2:
        threshold = float(numeric[-2])
    return " ".join(tokens).strip(), threshold, duration


async def _cmd_watch(update, context, w_type):
    usage = {
        "mic": "/watch_mic <texto del microfono> [umbral] [segundos]",
        "speaker": "/watch_speaker <texto de la salida> [umbral] [segundos]",
        "app": "/watch_app <proceso, ej: spotify.exe> [umbral] [segundos]",
    }[w_type]
    if not context.args:
        await update.message.reply_text(f"Uso: {usage}")
        return

    target, threshold, duration = parse_watch_args(
        context.args, CONFIG.default_threshold, CONFIG.default_duration
    )
    if not target:
        await update.message.reply_text("Falta el nombre del dispositivo o de la app.")
        return

    chat_id = user_of(update)
    if w_type in ("mic", "speaker"):
        kind = audio_devices.SPEAKER if w_type == "speaker" else audio_devices.MIC
        device = await run_devices(audio_devices.resolve, target, kind)
        if device is None:
            await update.message.reply_text(
                f"❌ No encontre ningun dispositivo que contenga '{target}'. "
                "Usa /devices para ver los nombres exactos."
            )
            return
        target = str(device.name)

    try:
        cfg = create_watcher(chat_id, w_type, target, threshold=threshold, duration=duration)
    except (storage.StorageError, SupervisorError) as e:
        await update.message.reply_text(f"❌ {e}")
        return

    await update.message.reply_text(
        render_card(chat_id, cfg["id"]),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=kb_card(chat_id, cfg["id"]),
    )


async def cmd_watch_mic(update, context):
    await _cmd_watch(update, context, "mic")


async def cmd_watch_speaker(update, context):
    await _cmd_watch(update, context, "speaker")


async def cmd_watch_app(update, context):
    await _cmd_watch(update, context, "app")


async def cmd_list(update, context):
    chat_id = user_of(update)
    if not storage.load_user(chat_id)["watchers"]:
        await update.message.reply_text(
            "No tienes ninguna vigilancia todavia.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("➕ Crear la primera", callback_data="n:start")]]
            ),
        )
        return
    await update.message.reply_text(
        "📋 " + bold("Tus vigilancias"),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=kb_watcher_list(chat_id),
    )


async def cmd_status(update, context):
    chat_id = user_of(update)
    await update.message.reply_text(
        render_status(chat_id),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=kb_watcher_list(chat_id),
    )


async def cmd_stop(update, context):
    chat_id = user_of(update)
    if not context.args:
        await update.message.reply_text("Uso: /stop <id> (usa /list para ver los ids)")
        return
    try:
        wid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("El id debe ser un numero.")
        return
    SUPERVISOR.stop(chat_id, wid)
    removed = storage.remove_watcher(chat_id, wid)
    await update.message.reply_text(
        f"🛑 Vigilancia #{wid} detenida y eliminada." if removed else f"No existe la vigilancia #{wid}."
    )


async def cmd_stopall(update, context):
    chat_id = user_of(update)
    SUPERVISOR.stop_owner(chat_id)
    removed = storage.remove_all_watchers(chat_id)
    await update.message.reply_text(f"🛑 {len(removed)} vigilancia(s) detenidas y eliminadas.")


async def cmd_setthreshold(update, context):
    chat_id = user_of(update)
    if len(context.args) < 2:
        await update.message.reply_text("Uso: /setthreshold <id> <valor entre 0 y 1>")
        return
    try:
        wid = int(context.args[0])
        value = float(context.args[1].replace(",", "."))
    except ValueError:
        await update.message.reply_text("El id debe ser entero y el umbral un numero.")
        return
    if storage.get_watcher(chat_id, wid) is None:
        await update.message.reply_text(f"No existe la vigilancia #{wid}.")
        return
    cfg = storage.update_watcher(chat_id, wid, threshold=value)
    SUPERVISOR.apply(chat_id, wid, threshold=cfg["threshold"])
    await update.message.reply_text(f"✅ Umbral de #{wid} = {cfg['threshold']:.4f}")


async def cmd_setduration(update, context):
    chat_id = user_of(update)
    if len(context.args) < 2:
        await update.message.reply_text("Uso: /setduration <id> <segundos>")
        return
    try:
        wid = int(context.args[0])
        value = float(context.args[1].replace(",", "."))
    except ValueError:
        await update.message.reply_text("El id debe ser entero y la duracion un numero.")
        return
    if storage.get_watcher(chat_id, wid) is None:
        await update.message.reply_text(f"No existe la vigilancia #{wid}.")
        return
    cfg = storage.update_watcher(chat_id, wid, duration=value)
    SUPERVISOR.apply(chat_id, wid, duration=cfg["duration"])
    await update.message.reply_text(f"✅ #{wid} avisara tras {cfg['duration']:g}s de silencio.")


async def cmd_setcooldown(update, context):
    chat_id = user_of(update)
    if not context.args:
        await update.message.reply_text("Uso: /setcooldown <segundos> (maximo 30)")
        return
    try:
        value = float(context.args[0].replace(",", "."))
    except ValueError:
        await update.message.reply_text("El valor debe ser un numero.")
        return
    data = storage.set_cooldown(chat_id, value)
    apply_cooldowns(chat_id, data)
    await update.message.reply_text(
        f"✅ Repetire los avisos cada {int(data['cooldown_seconds'])}s mientras siga el silencio."
    )


async def cmd_mute(update, context):
    chat_id = user_of(update)
    minutes = 30.0
    if context.args:
        try:
            minutes = max(1.0, min(1440.0, float(context.args[0].replace(",", "."))))
        except ValueError:
            await update.message.reply_text("Uso: /mute <minutos>")
            return
    storage.set_muted_until(chat_id, time.time() + minutes * 60)
    await update.message.reply_text(
        f"😴 Avisos silenciados {int(minutes)} min. Sigo vigilando; usa /unmute para reactivarlos."
    )


async def cmd_unmute(update, context):
    storage.set_muted_until(user_of(update), 0)
    await update.message.reply_text("🔊 Avisos reactivados.")


async def cmd_test(update, context):
    chat_id = user_of(update)
    NOTIFIER.send(
        chat_id,
        "🔔 <b>Mensaje de prueba</b>\nSi lees esto, las alertas te llegaran bien.",
        parse_mode="HTML",
    )
    await update.message.reply_text("Mensaje de prueba encolado ✅")


async def cmd_stats(update, context):
    chat_id = user_of(update)
    if not CONFIG.is_admin(chat_id):
        await update.message.reply_text("Solo para administradores.")
        return
    stats = SUPERVISOR.stats()
    users = storage.global_stats()
    await update.message.reply_text(
        "📈 " + bold("Estado del servidor") + "\n"
        + md(f"👥 {users['users']} usuarios · {users['watchers']} vigilancias guardadas") + "\n"
        + md(f"🧵 {stats['total']} hilos activos de {stats['owners']} usuarios") + "\n"
        + md(f"🔴 {stats['alerting']} alertando ahora") + "\n"
        + md(f"📨 cola Telegram: {NOTIFIER.pending()} pendientes, "
             f"{NOTIFIER.stats['sent']} enviados, {NOTIFIER.stats['failed']} fallidos"),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


def apply_cooldowns(chat_id, data=None):
    """Reaplica el cooldown efectivo a todos los watchers vivos del usuario."""
    data = data or storage.load_user(chat_id)
    for cfg in data["watchers"]:
        SUPERVISOR.apply(chat_id, cfg["id"], cooldown=storage.effective_cooldown(data, cfg))


# ---------------------------------------------------------------------------
# Callbacks (botones)
# ---------------------------------------------------------------------------

async def cb_dispatch(update, context):
    query = update.callback_query
    chat_id = str(query.message.chat_id)
    message_id = query.message.message_id
    data = query.data or ""
    await query.answer()

    # Cualquier navegacion corta el refresco en vivo de ese mensaje, para que
    # la tarea de fondo no sobrescriba lo que el usuario acaba de abrir.
    if not data.startswith("w:live:"):
        cancel_live(chat_id, message_id)

    try:
        handler = _ROUTES.get(data.split(":")[0])
        if handler is None:
            await safe_edit(query, render_dashboard(chat_id), kb_dashboard(chat_id))
            return
        await handler(update, context, query, chat_id, data)
    except (storage.StorageError, SupervisorError) as e:
        await safe_edit(query, md(f"❌ {e}"), kb_back())


async def route_menu(update, context, query, chat_id, data):
    action = data.split(":")[1]

    if action == "home":
        await safe_edit(query, render_dashboard(chat_id), kb_dashboard(chat_id))
    elif action == "list":
        if not storage.load_user(chat_id)["watchers"]:
            await safe_edit(query, md("Todavia no vigilas nada. Crea la primera con ➕."),
                            InlineKeyboardMarkup([
                                [InlineKeyboardButton("➕ Nueva vigilancia", callback_data="n:start")],
                                [InlineKeyboardButton("⬅️ Volver", callback_data="m:home")],
                            ]))
        else:
            await safe_edit(query, "📋 " + bold("Tus vigilancias"), kb_watcher_list(chat_id))
    elif action == "status":
        await safe_edit(query, render_status(chat_id), kb_watcher_list(chat_id))
    elif action == "dev":
        await safe_edit(query, await devices_view(), kb_back())
    elif action == "apps":
        await safe_edit(query, await apps_view(), kb_back())
    elif action == "cfg":
        await safe_edit(query, render_settings(chat_id), kb_settings(chat_id))
    elif action == "help":
        await safe_edit(query, HELP_TEXT, kb_back())
    else:
        await safe_edit(query, render_dashboard(chat_id), kb_dashboard(chat_id))


async def route_watcher(update, context, query, chat_id, data):
    parts = data.split(":")
    action = parts[1]
    wid = int(parts[2])
    value = parts[3] if len(parts) > 3 else None

    if storage.get_watcher(chat_id, wid) is None and action != "c":
        await safe_edit(query, md("⚠️ Esa vigilancia ya no existe."), kb_watcher_list(chat_id))
        return

    if action == "c":
        await safe_edit(query, render_card(chat_id, wid), kb_card(chat_id, wid))

    elif action == "t":
        cfg = storage.get_watcher(chat_id, wid)
        new_paused = not cfg.get("paused", False)
        storage.set_watcher_paused(chat_id, wid, new_paused)
        if SUPERVISOR.set_paused(chat_id, wid, new_paused) is None and not new_paused:
            # No habia hilo (estaba detenida): se arranca al reanudar.
            start_watcher(chat_id, storage.get_watcher(chat_id, wid))
        await safe_edit(query, render_card(chat_id, wid), kb_card(chat_id, wid))

    elif action == "live":
        cancel_live(chat_id, query.message.message_id)
        await safe_edit(query, render_card(chat_id, wid), kb_card(chat_id, wid, live=True))
        task = asyncio.create_task(
            live_loop(context, chat_id, query.message.message_id, wid)
        )
        LIVE_TASKS[(chat_id, query.message.message_id)] = task

    elif action == "stoplive":
        cancel_live(chat_id, query.message.message_id)
        await safe_edit(query, render_card(chat_id, wid), kb_card(chat_id, wid))

    elif action == "d":
        await safe_edit(query, md(f"¿Eliminar la vigilancia #{wid}?"), InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Si, eliminar", callback_data=f"w:D:{wid}"),
             InlineKeyboardButton("❌ Cancelar", callback_data=f"w:c:{wid}")],
        ]))

    elif action == "D":
        SUPERVISOR.stop(chat_id, wid)
        storage.remove_watcher(chat_id, wid)
        await safe_edit(query, md(f"🗑️ Vigilancia #{wid} eliminada."), kb_watcher_list(chat_id))

    elif action == "thr":
        cfg = storage.get_watcher(chat_id, wid)
        await safe_edit(
            query,
            md(f"🚦 Umbral de #{wid}: actualmente {cfg['threshold']:.4f}\n\n"
               "Por debajo de este nivel se considera silencio. "
               "Mira el nivel real en 🎵 Apps activas o en la ficha en vivo."),
            kb_values(
                f"w:setthr:{wid}",
                [value for _label, value in THRESHOLD_PRESETS],
                formatter=lambda v: next(f"{label} {v}" for label, val in THRESHOLD_PRESETS if val == v),
                back=f"w:c:{wid}",
                extra_rows=[[InlineKeyboardButton("✏️ Escribir un valor", callback_data=f"w:askthr:{wid}")]],
                per_row=1,
            ),
        )

    elif action == "dur":
        cfg = storage.get_watcher(chat_id, wid)
        await safe_edit(
            query,
            md(f"⏱️ Espera de #{wid}: actualmente {cfg['duration']:g}s\n\n"
               "Cuanto silencio aguanto antes del primer aviso."),
            kb_values(f"w:setdur:{wid}", DURATION_PRESETS, formatter=lambda v: f"{v}s",
                      back=f"w:c:{wid}",
                      extra_rows=[[InlineKeyboardButton("✏️ Escribir un valor",
                                                        callback_data=f"w:askdur:{wid}")]]),
        )

    elif action == "cd":
        user = storage.load_user(chat_id)
        cfg = storage.get_watcher(chat_id, wid)
        current = storage.effective_cooldown(user, cfg)
        own = "propio" if cfg.get("cooldown") else "heredado del global"
        await safe_edit(
            query,
            md(f"🔁 Repeticion de #{wid}: cada {int(current)}s ({own})\n\n"
               "Cada cuanto insisto mientras siga el silencio. Maximo 30s."),
            kb_values(f"w:setcd:{wid}", COOLDOWN_PRESETS, formatter=lambda v: f"{v}s",
                      back=f"w:c:{wid}",
                      extra_rows=[[InlineKeyboardButton("🌐 Usar el global",
                                                        callback_data=f"w:setcd:{wid}:auto")]]),
        )

    elif action == "int":
        cfg = storage.get_watcher(chat_id, wid)
        await safe_edit(
            query,
            md(f"⚡ Chequeo de #{wid}: cada {cfg['interval']:g}s\n\n"
               "Cada cuanto mido el nivel. Mas bajo = deteccion mas fina y algo mas de CPU."),
            kb_values(f"w:setint:{wid}", INTERVAL_PRESETS, formatter=lambda v: f"{v:g}s",
                      back=f"w:c:{wid}"),
        )

    elif action in ("setthr", "setdur", "setcd", "setint"):
        field = {"setthr": "threshold", "setdur": "duration",
                 "setcd": "cooldown", "setint": "interval"}[action]
        raw = None if value == "auto" else float(value)
        cfg = storage.update_watcher(chat_id, wid, **{field: raw})
        if field == "cooldown":
            SUPERVISOR.apply(chat_id, wid,
                             cooldown=storage.effective_cooldown(storage.load_user(chat_id), cfg))
        else:
            SUPERVISOR.apply(chat_id, wid, **{field: cfg[field]})
        await safe_edit(query, render_card(chat_id, wid), kb_card(chat_id, wid))

    elif action in ("askthr", "askdur"):
        context.user_data["await"] = {
            "kind": "threshold" if action == "askthr" else "duration",
            "wid": wid,
        }
        what = "un umbral entre 0.0001 y 1" if action == "askthr" else "los segundos de espera"
        await safe_edit(query, md(f"✏️ Escribeme {what} y lo aplico a #{wid}."),
                        kb_back(f"w:c:{wid}", "⬅️ Cancelar"))


async def route_config(update, context, query, chat_id, data):
    parts = data.split(":")
    action = parts[1]
    value = parts[2] if len(parts) > 2 else None

    if action == "cdmenu":
        await safe_edit(query, md("🔁 Cada cuanto repito el aviso mientras siga el silencio:"),
                        kb_values("c:cd", COOLDOWN_PRESETS, formatter=lambda v: f"{v}s", back="m:cfg"))

    elif action == "cd":
        data_user = storage.set_cooldown(chat_id, float(value))
        apply_cooldowns(chat_id, data_user)
        await safe_edit(query, render_settings(chat_id), kb_settings(chat_id))

    elif action == "rec":
        enabled = value == "1"
        storage.set_notify_recovery(chat_id, enabled)
        SUPERVISOR.apply_to_owner(chat_id, notify_recovery=enabled)
        await safe_edit(query, render_settings(chat_id), kb_settings(chat_id))

    elif action == "mutemenu":
        await safe_edit(query, md("😴 ¿Cuanto tiempo silencio los avisos? Sigo vigilando igual."),
                        kb_values("c:mute", MUTE_PRESETS, formatter=lambda v: f"{v} min", back="m:cfg"))

    elif action == "mute":
        storage.set_muted_until(chat_id, time.time() + float(value) * 60)
        await safe_edit(query, render_settings(chat_id), kb_settings(chat_id))

    elif action == "unmute":
        storage.set_muted_until(chat_id, 0)
        await safe_edit(query, render_settings(chat_id), kb_settings(chat_id))

    elif action == "pauseall":
        paused = value == "1"
        storage.set_all_paused(chat_id, paused)
        for cfg in storage.load_user(chat_id)["watchers"]:
            if SUPERVISOR.set_paused(chat_id, cfg["id"], paused) is None and not paused:
                start_watcher(chat_id, cfg)
        await safe_edit(query, render_dashboard(chat_id), kb_dashboard(chat_id))

    elif action == "wipe":
        await safe_edit(query, md("¿Seguro que quieres borrar TODAS tus vigilancias?"),
                        InlineKeyboardMarkup([
                            [InlineKeyboardButton("✅ Si, borrar todo", callback_data="c:wipeyes"),
                             InlineKeyboardButton("❌ Cancelar", callback_data="m:cfg")],
                        ]))

    elif action == "wipeyes":
        SUPERVISOR.stop_owner(chat_id)
        removed = storage.remove_all_watchers(chat_id)
        await safe_edit(query, md(f"🗑️ {len(removed)} vigilancia(s) eliminadas."), kb_dashboard(chat_id))


async def route_new(update, context, query, chat_id, data):
    parts = data.split(":")
    action = parts[1]
    value = parts[2] if len(parts) > 2 else None
    flow = context.user_data.setdefault("flow", {})

    if action == "noop":
        return

    if action == "start":
        context.user_data["flow"] = {}
        context.user_data.pop("await", None)
        await safe_edit(query, md("➕ ¿Que quieres vigilar?"), kb_type_select())
        return

    if action == "type":
        flow.clear()
        flow.update({"type": value, "page": 0, "query": None})
        await show_candidates(query, flow, refresh=True)
        return

    if action in ("refresh", "page"):
        if action == "page":
            flow["page"] = int(value)
            await safe_edit(query, candidates_text(flow), kb_candidates(flow))
        else:
            await show_candidates(query, flow, refresh=True)
        return

    if action == "search":
        context.user_data["await"] = {"kind": "search", "message_id": query.message.message_id}
        await safe_edit(query, md("🔎 Escribeme parte del nombre de la app (ej: spot)."),
                        kb_back("n:start", "⬅️ Cancelar"))
        return

    if action == "manual":
        context.user_data["await"] = {"kind": "manual", "message_id": query.message.message_id}
        await safe_edit(query, md("✏️ Escribeme el nombre exacto del proceso (ej: spotify.exe)."),
                        kb_back("n:start", "⬅️ Cancelar"))
        return

    if action == "pick":
        candidates = flow.get("candidates", [])
        index = int(value)
        if index >= len(candidates):
            await safe_edit(query, md("La lista cambio. Vuelve a empezar con ➕."), kb_back())
            return
        flow["target"] = candidates[index]["value"]
        await ask_duration(query, flow)
        return

    if action == "dur":
        flow["duration"] = float(value)
        await ask_threshold(query, flow)
        return

    if action == "thr":
        flow["threshold"] = float(value)
        await ask_cooldown(query, flow)
        return

    if action == "cd":
        flow["cooldown"] = float(value)
        await finish_flow(query, context, chat_id, flow)
        return


def candidates_text(flow):
    w_type = flow.get("type", "app")
    header = {
        "mic": "🎙️ Elige el microfono a vigilar",
        "speaker": "🔊 Elige la salida de audio a vigilar",
        "app": "🎵 Elige la app a vigilar",
    }[w_type]
    lines = [bold(header), ""]
    if flow.get("query"):
        lines.append(md(f"🔎 Filtro: {flow['query']}"))
    if w_type == "app":
        lines.append(italic("🔊 = esta sonando ahora · ▫️ = abierta sin audio"))
    else:
        lines.append(italic("⭐ = predeterminado del sistema"))
    if not flow.get("candidates"):
        lines.append("")
        lines.append(md("No encontre nada. Prueba a refrescar o a escribir el nombre."))
    return "\n".join(lines)


async def show_candidates(query, flow, refresh=False):
    try:
        flow["candidates"] = await build_candidates(
            flow.get("type", "app"), query=flow.get("query"), refresh=refresh
        )
    except Exception as e:
        logger.exception("Fallo listando candidatos")
        await safe_edit(query, md(f"❌ No se pudo leer la lista: {e}"), kb_back())
        return
    flow["page"] = 0
    await safe_edit(query, candidates_text(flow), kb_candidates(flow))


async def ask_duration(query, flow):
    await safe_edit(
        query,
        bold("🎯 " + str(flow["target"])) + "\n\n"
        + md("⏱️ ¿Cuanto silencio aguanto antes del primer aviso?"),
        kb_values("n:dur", DURATION_PRESETS, formatter=lambda v: f"{v}s", back="n:start"),
    )


async def ask_threshold(query, flow):
    await safe_edit(
        query,
        bold("🎯 " + str(flow["target"])) + "\n\n"
        + md("🚦 ¿Que tan sensible debe ser la deteccion de silencio?"),
        kb_values(
            "n:thr", [v for _l, v in THRESHOLD_PRESETS],
            formatter=lambda v: next(f"{label} ({v})" for label, val in THRESHOLD_PRESETS if val == v),
            back="n:start", per_row=1,
        ),
    )


async def ask_cooldown(query, flow):
    await safe_edit(
        query,
        bold("🎯 " + str(flow["target"])) + "\n\n"
        + md("🔁 ¿Cada cuanto repito el aviso mientras siga el silencio? (max 30s)"),
        kb_values("n:cd", COOLDOWN_PRESETS, formatter=lambda v: f"{v}s", back="n:start"),
    )


async def finish_flow(query, context, chat_id, flow):
    w_type = flow.get("type")
    target = flow.get("target")
    if not (w_type and target):
        await safe_edit(query, md("Algo salio mal con la seleccion. Empieza de nuevo con ➕."), kb_back())
        return

    cfg = create_watcher(
        chat_id, w_type, target,
        threshold=flow.get("threshold", CONFIG.default_threshold),
        duration=flow.get("duration", CONFIG.default_duration),
        cooldown=flow.get("cooldown"),
    )
    context.user_data.pop("flow", None)
    await safe_edit(query, render_card(chat_id, cfg["id"]), kb_card(chat_id, cfg["id"]))


_ROUTES = {
    "m": route_menu,
    "w": route_watcher,
    "c": route_config,
    "n": route_new,
}


# ---------------------------------------------------------------------------
# Entrada de texto libre (valores personalizados y buscador)
# ---------------------------------------------------------------------------

async def on_text(update, context):
    chat_id = user_of(update)
    pending = context.user_data.get("await")
    text = (update.message.text or "").strip()

    if not pending:
        await update.message.reply_text(
            render_dashboard(chat_id),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=kb_dashboard(chat_id),
        )
        return

    kind = pending.get("kind")
    context.user_data.pop("await", None)

    if kind in ("threshold", "duration"):
        try:
            value = float(text.replace(",", "."))
        except ValueError:
            await update.message.reply_text("Eso no es un numero. Prueba otra vez desde la ficha.")
            return
        wid = pending["wid"]
        if storage.get_watcher(chat_id, wid) is None:
            await update.message.reply_text("Esa vigilancia ya no existe.")
            return
        cfg = storage.update_watcher(chat_id, wid, **{kind: value})
        SUPERVISOR.apply(chat_id, wid, **{kind: cfg[kind]})
        await update.message.reply_text(
            render_card(chat_id, wid),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=kb_card(chat_id, wid),
        )
        return

    if kind == "search":
        flow = context.user_data.setdefault("flow", {"type": "app"})
        flow["query"] = text
        try:
            flow["candidates"] = await build_candidates("app", query=text)
        except Exception as e:
            await update.message.reply_text(f"❌ No se pudo buscar: {e}")
            return
        flow["page"] = 0
        await update.message.reply_text(
            candidates_text(flow),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=kb_candidates(flow),
        )
        return

    if kind == "manual":
        flow = context.user_data.setdefault("flow", {"type": "app"})
        flow["target"] = text
        await update.message.reply_text(
            bold("🎯 " + text) + "\n\n" + md("⏱️ ¿Cuanto silencio aguanto antes del primer aviso?"),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=kb_values("n:dur", DURATION_PRESETS, formatter=lambda v: f"{v}s", back="n:start"),
        )
        return


# ---------------------------------------------------------------------------
# Autorizacion, arranque y cierre
# ---------------------------------------------------------------------------

async def auth_gate(update, context):
    """Si config.json define allowed_chat_ids, todo lo demas se ignora para
    quien no este en la lista. El bot expone los dispositivos y procesos de
    ESTA maquina, asi que en un equipo compartido conviene restringirlo."""
    if CONFIG.is_open:
        return
    chat = update.effective_chat
    if chat is None or CONFIG.is_allowed(chat.id):
        return
    logger.warning("Acceso denegado a chat_id=%s", chat.id)
    try:
        if update.callback_query:
            await update.callback_query.answer("No tienes acceso a este bot.", show_alert=True)
        elif update.message:
            await update.message.reply_text(
                f"⛔ Este bot es privado.\nTu chat_id es {chat.id}; pideselo al administrador."
            )
    except TelegramError:
        pass
    raise ApplicationHandlerStop


def resume_all_watchers():
    """Retoma en hilos las vigilancias guardadas de todos los usuarios. Se
    ejecuta fuera del event loop porque toca disco y crea hilos."""
    resumed = failed = 0
    for chat_id in storage.list_all_user_ids():
        data = storage.load_user(chat_id)
        for cfg in data["watchers"]:
            if not cfg.get("enabled", True):
                continue
            try:
                start_watcher(chat_id, cfg, user_data=data)
                resumed += 1
            except Exception as e:
                failed += 1
                logger.error("No se pudo retomar #%s de %s: %s", cfg["id"], chat_id, e)
    return resumed, failed


async def post_init(application):
    logger.info("Token: %s", telegram_config.redact(CONFIG.token))
    if CONFIG.is_open:
        logger.warning(
            "El bot esta ABIERTO: cualquiera que lo encuentre puede ver los "
            "dispositivos y procesos de esta maquina. Para restringirlo, anade "
            "\"allowed_chat_ids\": [tu_chat_id] en config.json."
        )
    resumed, failed = await asyncio.to_thread(resume_all_watchers)
    logger.info("Vigilancias retomadas: %d (%d fallidas)", resumed, failed)
    await application.bot.set_my_commands([
        ("start", "Abrir el panel"),
        ("list", "Mis vigilancias"),
        ("status", "Estado en vivo"),
        ("devices", "Dispositivos de audio"),
        ("apps", "Apps con audio activo"),
        ("mute", "Silenciar avisos un rato"),
        ("unmute", "Reactivar avisos"),
        ("help", "Ayuda"),
    ])


async def post_shutdown(application):
    for task in list(LIVE_TASKS.values()):
        task.cancel()
    LIVE_TASKS.clear()
    await asyncio.to_thread(SUPERVISOR.stop_all)
    await asyncio.to_thread(NOTIFIER.stop)
    audio_devices.shutdown_executor()
    app_audio.shutdown_executor()
    logger.info("Bot detenido limpiamente.")


async def on_error(update, context):
    logger.error("Error procesando una actualizacion", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await context.bot.send_message(
                update.effective_chat.id,
                "😵 Algo fallo procesando eso. Ya quedo registrado; prueba /start.",
            )
    except TelegramError:
        pass


def main():
    # Ancla el MTA de COM al hilo principal antes de crear ningun hilo de
    # captura (ver audio_devices.preload).
    audio_devices.preload()

    application = (
        Application.builder()
        .token(CONFIG.token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(TypeHandler(Update, auth_gate), group=-1)

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("id", cmd_id))
    application.add_handler(CommandHandler("devices", cmd_devices))
    application.add_handler(CommandHandler("apps", cmd_apps))
    application.add_handler(CommandHandler("watch_mic", cmd_watch_mic))
    application.add_handler(CommandHandler("watch_speaker", cmd_watch_speaker))
    application.add_handler(CommandHandler("watch_app", cmd_watch_app))
    application.add_handler(CommandHandler("list", cmd_list))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("stop", cmd_stop))
    application.add_handler(CommandHandler("stopall", cmd_stopall))
    application.add_handler(CommandHandler("setthreshold", cmd_setthreshold))
    application.add_handler(CommandHandler("setduration", cmd_setduration))
    application.add_handler(CommandHandler("setcooldown", cmd_setcooldown))
    application.add_handler(CommandHandler("mute", cmd_mute))
    application.add_handler(CommandHandler("unmute", cmd_unmute))
    application.add_handler(CommandHandler("test", cmd_test))
    application.add_handler(CommandHandler("stats", cmd_stats))
    application.add_handler(CallbackQueryHandler(cb_dispatch))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    application.add_error_handler(on_error)

    logger.info("Bot iniciado. Esperando mensajes...")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
