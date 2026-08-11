"""
Panel de control de escritorio del Monitor de Audio.

Permite elegir desde una ventana que microfono, que salida de audio (todo lo
que suene por los altavoces, via loopback) o que app concreta vigilar, muestra
un VU-metro en vivo por cada objetivo y puede seguir corriendo minimizado en la
bandeja del sistema.

Comparte el motor con el bot: los mismos DeviceWatcher/AppWatcher y el mismo
MonitorSupervisor, asi que la deteccion y las alertas se comportan igual en
ambos frentes.

Diseño:
    Grid con pesos fijos: cabecera y barra de control quedan ancladas; las
    listas viven en un Canvas con scroll propio, asi que agregar filas nunca
    empuja los botones fuera de vista.

Requiere Windows. La bandeja del sistema es opcional: sin `pystray`/`Pillow`
la GUI funciona igual, solo sin ese boton.

Uso:
    python gui.py
"""

import json
import logging
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import app_audio
import audio_devices
import storage
import telegram_config
from audio_backends import STATE_EMOJI, WatcherState, fmt_duration
from monitor import MonitorSupervisor
from notifier import get_notifier

try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False

# Empaquetada como aplicacion de ventana (console=False), PyInstaller deja
# sys.stderr en None: un StreamHandler normal escribiria sobre nada y logging
# se pondria a quejarse en cada mensaje. Si no hay consola, los logs se tiran.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)] if sys.stderr else [logging.NullHandler()],
)
logger = logging.getLogger("angryfurbot.gui")

ICON_PATH = storage.resource_dir() / "blui.ico"
STATE_PATH = storage.app_dir() / "gui_state.json"
APP_STATE_KEY = "_app"

# ---------------------------------------------------------------------------
# Paleta / estilo: flat, alto contraste, acento indigo.
# ---------------------------------------------------------------------------

BG = "#f3f4f9"
SURFACE = "#ffffff"
BORDER = "#e2e4ee"
TEXT_PRIMARY = "#1c1f2b"
TEXT_MUTED = "#8a8fa3"
ACCENT = "#5b5bf0"
ACCENT_HOVER = "#4747d6"
ACCENT_SOFT = "#eceaff"
DANGER = "#e5484d"
DANGER_SOFT = "#fde8e8"

COLOR_STOPPED = "#9aa0b4"
COLOR_ACTIVE = "#1fa855"
COLOR_WARNING = "#f2a900"
COLOR_SILENT = "#e5484d"
COLOR_PAUSED = "#6b7086"

# Estado del watcher -> (texto, color de letra, color de fondo de la pill)
STATUS_META = {
    WatcherState.STOPPED: ("Detenido", COLOR_STOPPED, "#f1f2f6"),
    WatcherState.STARTING: ("Iniciando", COLOR_STOPPED, "#f1f2f6"),
    WatcherState.ACTIVE: ("Activo", COLOR_ACTIVE, "#e8f8ee"),
    WatcherState.SILENCE: ("Silencio", COLOR_WARNING, "#fdf3dc"),
    WatcherState.ALERTING: ("Alertando", COLOR_SILENT, "#fce6e6"),
    WatcherState.PAUSED: ("Pausado", COLOR_PAUSED, "#eceef3"),
    WatcherState.ERROR: ("Error", COLOR_SILENT, "#fce6e6"),
}

FONT_TITLE = ("Segoe UI", 16, "bold")
FONT_SUBTITLE = ("Segoe UI", 9)
FONT_CARD_TITLE = ("Segoe UI", 10, "bold")
FONT_LABEL = ("Segoe UI", 8)
FONT_SMALL = ("Segoe UI", 8)
FONT_BTN = ("Segoe UI", 10, "bold")
FONT_PILL = ("Segoe UI", 8, "bold")


def load_gui_state():
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def save_gui_state(state):
    try:
        tmp = STATE_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        tmp.replace(STATE_PATH)
    except OSError as e:
        logger.warning("No se pudo guardar el estado de la GUI: %s", e)


class ScrollableFrame(ttk.Frame):
    """Contenedor con scroll propio: listas largas no empujan el resto de la
    ventana. El contenido real se agrega a `.inner`."""

    def __init__(self, parent, **kwargs):
        super().__init__(parent, style="Surface.TFrame", **kwargs)

        self.canvas = tk.Canvas(self, highlightthickness=0, bg=BG, bd=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas, style="Surface.TFrame")

        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas_window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vsb.set)

        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vsb.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._on_mousewheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _on_inner_configure(self, _event):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self.canvas.itemconfig(self.canvas_window, width=event.width)

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")


class WatcherRow(ttk.Frame):
    """Tarjeta de la GUI: objetivo + ajustes + VU-metro en vivo + pausa."""

    def __init__(self, parent, app, w_type, target, title, state_key, saved=None):
        super().__init__(parent, style="Card.TFrame", padding=(12, 10))
        self.app = app
        self.w_type = w_type
        self.target = target
        self.title_text = title
        self.state_key = state_key
        self.watcher = None
        self.watcher_id = app.next_watcher_id()
        self.current_state = WatcherState.STOPPED

        saved = saved or {}
        self.threshold_var = tk.StringVar(value=str(saved.get("threshold", storage.DEFAULT_THRESHOLD)))
        self.duration_var = tk.StringVar(value=str(saved.get("duration", int(storage.DEFAULT_DURATION_SECONDS))))
        self.enabled_var = tk.BooleanVar(value=bool(saved.get("enabled", False)))

        # Estilo propio de progressbar para poder cambiarle el color en vivo
        # segun el nivel sin afectar a las demas filas.
        self._vu_style_name = f"VU{id(self)}.Horizontal.TProgressbar"
        self._vu_color = COLOR_ACTIVE
        ttk.Style().configure(
            self._vu_style_name, troughcolor="#eef0f6", background=COLOR_ACTIVE,
            bordercolor=BORDER, lightcolor=COLOR_ACTIVE, darkcolor=COLOR_ACTIVE, thickness=10,
        )

        self._build_ui()

        self.threshold_var.trace_add("write", lambda *_: self._on_param_change())
        self.duration_var.trace_add("write", lambda *_: self._on_param_change())
        self.enabled_var.trace_add("write", lambda *_: self.app.persist_row_state(self))

        self.bind("<Enter>", lambda _e: self.configure(style="CardHover.TFrame"))
        self.bind("<Leave>", lambda _e: self.configure(style="Card.TFrame"))

    # -- construccion ----------------------------------------------------

    def _build_ui(self):
        self.columnconfigure(0, weight=1)

        top = ttk.Frame(self, style="Card.TFrame")
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)

        ttk.Checkbutton(
            top, text=self.title_text, variable=self.enabled_var, style="CardTitle.TCheckbutton",
        ).grid(row=0, column=0, sticky="w")

        self.status_pill = tk.Label(
            top, text="Detenido", font=FONT_PILL, fg=COLOR_STOPPED, bg="#f1f2f6",
            padx=10, pady=2, bd=0,
        )
        self.status_pill.grid(row=0, column=1, sticky="e")

        bottom = ttk.Frame(self, style="Card.TFrame")
        bottom.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        bottom.columnconfigure(2, weight=1)

        ttk.Label(bottom, text="Umbral", style="CardMuted.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Entry(bottom, textvariable=self.threshold_var, width=6, style="Card.TEntry").grid(
            row=1, column=0, sticky="w", padx=(0, 10)
        )

        ttk.Label(bottom, text="Seg.", style="CardMuted.TLabel").grid(row=0, column=1, sticky="w")
        ttk.Entry(bottom, textvariable=self.duration_var, width=5, style="Card.TEntry").grid(
            row=1, column=1, sticky="w", padx=(0, 14)
        )

        vu_wrap = ttk.Frame(bottom, style="Card.TFrame")
        vu_wrap.grid(row=0, column=2, rowspan=2, sticky="ew", padx=(0, 10))
        vu_wrap.columnconfigure(0, weight=1)
        ttk.Label(vu_wrap, text="Nivel de audio", style="CardMuted.TLabel").grid(row=0, column=0, sticky="w")
        self.vu = ttk.Progressbar(vu_wrap, orient="horizontal", maximum=1.0, style=self._vu_style_name)
        self.vu.grid(row=1, column=0, sticky="ew", pady=(2, 0))

        self.level_label = ttk.Label(bottom, text="0.000", style="CardMuted.TLabel", width=6)
        self.level_label.grid(row=1, column=3, sticky="e", padx=(0, 8))

        self.pause_btn = ttk.Button(
            bottom, text="⏸", width=3, command=self.toggle_pause, state="disabled", style="Icon.TButton",
        )
        self.pause_btn.grid(row=0, column=4, rowspan=2, sticky="e")

    # -- parametros -------------------------------------------------------

    def _on_param_change(self):
        self.app.persist_row_state(self)
        params = self.get_params()
        if params and self.watcher:
            # Cambios en caliente: no hace falta parar y volver a arrancar.
            self.watcher.apply_settings(threshold=params[0], duration=params[1])

    def is_enabled(self):
        return bool(self.enabled_var.get())

    def get_params(self):
        try:
            threshold = float(str(self.threshold_var.get()).replace(",", "."))
            duration = float(str(self.duration_var.get()).replace(",", "."))
        except ValueError:
            return None
        if not (storage.MIN_THRESHOLD <= threshold <= storage.MAX_THRESHOLD):
            return None
        if duration <= 0:
            return None
        return threshold, duration

    def spec(self):
        params = self.get_params()
        if params is None:
            return None
        threshold, duration = params
        return {
            "id": self.watcher_id,
            "type": self.w_type,
            "target": self.target,
            "threshold": threshold,
            "duration": duration,
            "interval": self.app.interval,
            "paused": False,
        }

    # -- ciclo de vida ----------------------------------------------------

    def start(self):
        spec = self.spec()
        if spec is None:
            return False
        try:
            self.watcher = self.app.supervisor.start(
                "local", spec,
                chat_id=self.app.tg_config.chat_id,
                cooldown=self.app.cooldown,
                notify_recovery=self.app.notify_recovery,
                on_status=self._on_status,
            )
        except Exception as e:
            self.app.log(f"❌ {self.title_text}: {e}")
            return False
        self.pause_btn.config(state="normal", text="⏸")
        return True

    def stop(self):
        if self.watcher:
            self.app.supervisor.stop("local", self.watcher_id)
        self.watcher = None
        self.pause_btn.config(state="disabled")
        self.set_status(WatcherState.STOPPED)
        self.vu["value"] = 0
        self.level_label.config(text="0.000")

    def toggle_pause(self):
        if not self.watcher:
            return
        if self.watcher.is_paused():
            self.watcher.resume()
            self.pause_btn.config(text="⏸")
        else:
            self.watcher.pause()
            self.pause_btn.config(text="▶")

    # -- pintado ----------------------------------------------------------

    def set_status(self, state):
        self.current_state = state
        text, fg, bg = STATUS_META.get(state, STATUS_META[WatcherState.STOPPED])
        try:
            self.status_pill.config(text=text, fg=fg, bg=bg)
        except tk.TclError:
            pass

    def _on_status(self, watcher):
        """Lo llama el hilo del watcher: solo reenvia el trabajo a Tk."""
        def update():
            if not self.winfo_exists():
                return
            level = watcher.last_level
            # Escala x8 para que niveles tipicos de musica (0.05-0.15) llenen
            # la barra de forma legible.
            value = min(1.0, level * 8)
            self.vu["value"] = value

            color = COLOR_SILENT if value < 0.15 else COLOR_WARNING if value < 0.4 else COLOR_ACTIVE
            if color != self._vu_color:
                self._vu_color = color
                ttk.Style().configure(
                    self._vu_style_name, background=color, lightcolor=color, darkcolor=color
                )

            self.level_label.config(text=f"{level:.3f}")
            self.set_status(watcher.state)

        try:
            self.app.after(0, update)
        except (RuntimeError, tk.TclError):
            pass  # la ventana ya se cerro


class SettingsDialog(tk.Toplevel):
    """Credenciales de Telegram y ajustes globales, sin tener que editar JSON."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Ajustes")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()

        cfg = app.tg_config
        self.token_var = tk.StringVar(value=cfg.token)
        self.chat_var = tk.StringVar(value=cfg.chat_id)
        self.cooldown_var = tk.StringVar(value=str(int(app.cooldown)))
        self.interval_var = tk.StringVar(value=str(app.interval))
        self.recovery_var = tk.BooleanVar(value=app.notify_recovery)
        self.tray_var = tk.BooleanVar(value=app.close_to_tray)
        self.autostart_var = tk.BooleanVar(value=app.autostart)

        body = ttk.Frame(self, style="Surface.TFrame", padding=16)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        rows = [
            ("Bot token", self.token_var, 42),
            ("Chat id", self.chat_var, 20),
            ("Repetir aviso cada (s, max 30)", self.cooldown_var, 8),
            ("Chequear cada (s)", self.interval_var, 8),
        ]
        for index, (label, var, width) in enumerate(rows):
            ttk.Label(body, text=label, style="CardMuted.TLabel").grid(row=index, column=0, sticky="w", pady=4)
            ttk.Entry(body, textvariable=var, width=width, style="Card.TEntry").grid(
                row=index, column=1, sticky="ew", padx=(10, 0), pady=4
            )

        ttk.Checkbutton(body, text="Avisar tambien cuando el sonido vuelve",
                        variable=self.recovery_var, style="CardTitle.TCheckbutton").grid(
            row=len(rows), column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Checkbutton(body, text="Al cerrar, minimizar a la bandeja",
                        variable=self.tray_var, style="CardTitle.TCheckbutton").grid(
            row=len(rows) + 1, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(body, text="Empezar a monitorear al abrir el programa",
                        variable=self.autostart_var, style="CardTitle.TCheckbutton").grid(
            row=len(rows) + 2, column=0, columnspan=2, sticky="w")

        buttons = ttk.Frame(body, style="Surface.TFrame")
        buttons.grid(row=len(rows) + 3, column=0, columnspan=2, sticky="e", pady=(16, 0))
        ttk.Button(buttons, text="Probar Telegram", command=self._test, style="Secondary.TButton").pack(side="left")
        ttk.Button(buttons, text="Guardar", command=self._save, style="Primary.TButton").pack(side="left", padx=(8, 0))

    def _test(self):
        token = self.token_var.get().strip()
        chat_id = self.chat_var.get().strip()
        if not token or not chat_id:
            messagebox.showwarning("Ajustes", "Rellena token y chat_id primero.", parent=self)
            return
        notifier = get_notifier(token)
        ok, error = notifier.send_now(chat_id, "🔔 Prueba desde el panel de AngryFurBot.")
        if ok:
            messagebox.showinfo("Ajustes", "Mensaje enviado correctamente ✅", parent=self)
        else:
            messagebox.showerror("Ajustes", f"No se pudo enviar:\n{error}", parent=self)

    def _save(self):
        token = self.token_var.get().strip()
        chat_id = self.chat_var.get().strip()
        try:
            cooldown = storage.clamp_cooldown(float(self.cooldown_var.get().replace(",", ".")))
            interval = storage.clamp_interval(float(self.interval_var.get().replace(",", ".")))
        except ValueError:
            messagebox.showerror("Ajustes", "Cooldown e intervalo deben ser numeros.", parent=self)
            return

        telegram_config.save_classic_credentials(token, chat_id, cooldown)
        self.app.apply_settings(
            token=token, chat_id=chat_id, cooldown=cooldown, interval=interval,
            notify_recovery=self.recovery_var.get(), close_to_tray=self.tray_var.get(),
            autostart=self.autostart_var.get(),
        )
        self.destroy()


class AudioMonitorGUI(tk.Tk):
    def __init__(self):
        # Antes de crear hilos de captura: ancla el MTA de COM al hilo
        # principal (ver audio_devices.preload).
        audio_devices.preload()

        super().__init__()
        self.title("AngryFurBot — Panel de control")
        self.geometry("780x660")
        self.minsize(700, 540)
        self.configure(bg=BG)

        try:
            if ICON_PATH.exists():
                self.iconbitmap(str(ICON_PATH))
        except tk.TclError:
            pass

        self.tg_config = telegram_config.load_classic_config()
        self.gui_state = load_gui_state()
        app_state = self.gui_state.get(APP_STATE_KEY, {})

        self.cooldown = storage.clamp_cooldown(app_state.get("cooldown", self.tg_config.cooldown))
        self.interval = storage.clamp_interval(app_state.get("interval", storage.DEFAULT_INTERVAL_SECONDS))
        self.notify_recovery = bool(app_state.get("notify_recovery", True))
        self.close_to_tray = bool(app_state.get("close_to_tray", False))
        self.autostart = bool(app_state.get("autostart", False))

        self.notifier = get_notifier(self.tg_config.token) if self.tg_config.token else None
        self.supervisor = MonitorSupervisor(notifier=self.notifier, max_per_owner=64)

        self.device_rows = {}
        self.app_rows = {}
        self._next_id = 0
        self.running = False
        self.tray_icon = None
        self._apps_loading = False

        self._setup_style()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._tick_global_status()

        if not self.tg_config.is_complete:
            self.after(300, self._warn_missing_credentials)
        elif self.autostart:
            self.after(800, self.start_monitoring)

    def next_watcher_id(self):
        self._next_id += 1
        return self._next_id

    # -- estilo -----------------------------------------------------------

    def _setup_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", background=BG, foreground=TEXT_PRIMARY, font=FONT_SUBTITLE)
        style.configure("TFrame", background=BG)
        style.configure("Surface.TFrame", background=BG)

        style.configure("Header.TFrame", background=ACCENT)
        style.configure("HeaderTitle.TLabel", background=ACCENT, foreground="white", font=FONT_TITLE)
        style.configure("HeaderSubtitle.TLabel", background=ACCENT, foreground=ACCENT_SOFT, font=FONT_SUBTITLE)
        style.configure("HeaderPill.TLabel", background=ACCENT_HOVER, foreground="white",
                        font=FONT_PILL, padding=(10, 4))

        style.configure("Card.TFrame", background=SURFACE, bordercolor=BORDER, relief="solid", borderwidth=1)
        style.configure("CardHover.TFrame", background=SURFACE, bordercolor=ACCENT, relief="solid", borderwidth=1)
        style.configure("CardTitle.TCheckbutton", background=SURFACE, foreground=TEXT_PRIMARY, font=FONT_CARD_TITLE)
        style.map("CardTitle.TCheckbutton", background=[("active", SURFACE)])
        style.configure("CardMuted.TLabel", background=SURFACE, foreground=TEXT_MUTED, font=FONT_LABEL)
        style.configure("Card.TEntry", fieldbackground="#f7f8fb", bordercolor=BORDER)

        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background="#e7e8f2", foreground=TEXT_MUTED,
                        font=FONT_CARD_TITLE, padding=(16, 8), borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", SURFACE)], foreground=[("selected", ACCENT)])

        style.configure("Vertical.TScrollbar", background="#d7d9e6", troughcolor=BG,
                        bordercolor=BG, arrowcolor=TEXT_MUTED)

        style.configure("Primary.TButton", background=ACCENT, foreground="white",
                        font=FONT_BTN, padding=(16, 8), borderwidth=0)
        style.map("Primary.TButton",
                  background=[("disabled", "#c7c9e6"), ("active", ACCENT_HOVER)],
                  foreground=[("disabled", "#f0f0fa")])

        style.configure("Danger.TButton", background=DANGER_SOFT, foreground=DANGER,
                        font=FONT_BTN, padding=(16, 8), borderwidth=0)
        style.map("Danger.TButton",
                  background=[("disabled", "#f5f5f5"), ("active", "#f9caca")],
                  foreground=[("disabled", "#bdbdbd")])

        style.configure("Secondary.TButton", background="#eceef3", foreground=TEXT_PRIMARY,
                        font=FONT_CARD_TITLE, padding=(12, 8), borderwidth=0)
        style.map("Secondary.TButton", background=[("active", "#dfe1ec")])

        style.configure("Icon.TButton", background="#eceef3", foreground=TEXT_PRIMARY,
                        padding=(4, 2), borderwidth=0)
        style.map("Icon.TButton", background=[("active", "#dfe1ec"), ("disabled", "#f4f4f8")])

        style.configure("Footer.TFrame", background=BG)
        style.configure("StatusText.TLabel", background=BG, foreground=TEXT_MUTED, font=FONT_SUBTITLE)
        style.configure("Master.Horizontal.TProgressbar", troughcolor=ACCENT_HOVER,
                        background="white", bordercolor=ACCENT, lightcolor="white",
                        darkcolor="white", thickness=6)

    # -- persistencia -----------------------------------------------------

    def persist_row_state(self, row):
        params = row.get_params()
        self.gui_state[row.state_key] = {
            "threshold": params[0] if params else row.threshold_var.get(),
            "duration": params[1] if params else row.duration_var.get(),
            "enabled": row.is_enabled(),
        }
        save_gui_state(self.gui_state)

    def persist_app_state(self):
        self.gui_state[APP_STATE_KEY] = {
            "cooldown": self.cooldown,
            "interval": self.interval,
            "notify_recovery": self.notify_recovery,
            "close_to_tray": self.close_to_tray,
            "autostart": self.autostart,
        }
        save_gui_state(self.gui_state)

    def apply_settings(self, token=None, chat_id=None, cooldown=None, interval=None,
                       notify_recovery=None, close_to_tray=None, autostart=None):
        if token is not None or chat_id is not None:
            self.tg_config = telegram_config.load_classic_config()
            self.notifier = get_notifier(self.tg_config.token) if self.tg_config.token else None
            self.supervisor.notifier = self.notifier
        if cooldown is not None:
            self.cooldown = cooldown
        if interval is not None:
            self.interval = interval
        if notify_recovery is not None:
            self.notify_recovery = notify_recovery
        if close_to_tray is not None:
            self.close_to_tray = close_to_tray
        if autostart is not None:
            self.autostart = autostart

        # Aplica en caliente a lo que ya este corriendo.
        self.supervisor.apply_to_owner(
            "local", cooldown=self.cooldown, interval=self.interval,
            notify_recovery=self.notify_recovery,
        )
        self.persist_app_state()
        self.log("Ajustes guardados.")

    # -- UI ---------------------------------------------------------------

    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=3)
        self.rowconfigure(5, weight=1)

        self._build_header()

        notebook = ttk.Notebook(self)
        notebook.grid(row=1, column=0, sticky="nsew", padx=16, pady=(12, 8))

        devices_tab = ttk.Frame(notebook, style="Surface.TFrame", padding=(0, 8, 0, 0))
        apps_tab = ttk.Frame(notebook, style="Surface.TFrame", padding=(0, 8, 0, 0))
        notebook.add(devices_tab, text="🎚  Dispositivos")
        notebook.add(apps_tab, text="🎵  Apps")

        self._build_devices_tab(devices_tab)
        self._build_apps_tab(apps_tab)

        self._build_control_bar()

        self.status_label = ttk.Label(self, text="Detenido.", style="StatusText.TLabel")
        self.status_label.grid(row=4, column=0, sticky="w", padx=20, pady=(0, 4))

        self._build_log_panel()

    def _build_header(self):
        header = ttk.Frame(self, style="Header.TFrame", padding=(20, 16))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        ttk.Label(header, text="🔊  AngryFurBot", style="HeaderTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="Marca lo que quieras vigilar y pulsa Iniciar.",
                  style="HeaderSubtitle.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 0))

        self.global_status_pill = ttk.Label(header, text="●  Sin actividad", style="HeaderPill.TLabel")
        self.global_status_pill.grid(row=0, column=1, rowspan=2, sticky="e")

        self.master_vu = ttk.Progressbar(header, orient="horizontal", maximum=1.0,
                                         style="Master.Horizontal.TProgressbar")
        self.master_vu.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))

    def _build_control_bar(self):
        bar = ttk.Frame(self, style="Footer.TFrame", padding=(16, 4))
        bar.grid(row=3, column=0, sticky="ew")

        self.start_btn = ttk.Button(bar, text="▶  Iniciar monitoreo",
                                    command=self.start_monitoring, style="Primary.TButton")
        self.start_btn.pack(side="left")

        self.stop_btn = ttk.Button(bar, text="■  Detener", command=self.stop_monitoring,
                                   state="disabled", style="Danger.TButton")
        self.stop_btn.pack(side="left", padx=(8, 0))

        ttk.Button(bar, text="⚙  Ajustes", command=self.open_settings,
                   style="Secondary.TButton").pack(side="left", padx=(8, 0))

        if TRAY_AVAILABLE:
            ttk.Button(bar, text="🗕  Bandeja", command=self.minimize_to_tray,
                       style="Secondary.TButton").pack(side="left", padx=(8, 0))
        else:
            ttk.Label(bar, text="(instala pystray + Pillow para la bandeja)",
                      style="StatusText.TLabel").pack(side="left", padx=(8, 0))

    def _build_log_panel(self):
        wrap = ttk.Frame(self, style="Surface.TFrame", padding=(16, 0, 16, 16))
        wrap.grid(row=5, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(1, weight=1)

        ttk.Label(wrap, text="Registro de eventos", style="CardMuted.TLabel").grid(row=0, column=0, sticky="w")

        log_card = ttk.Frame(wrap, style="Card.TFrame", padding=6)
        log_card.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        log_card.columnconfigure(0, weight=1)
        log_card.rowconfigure(0, weight=1)

        self.log_text = tk.Text(log_card, height=6, state="disabled", wrap="word", bd=0,
                                bg=SURFACE, fg=TEXT_PRIMARY, font=FONT_SMALL, padx=6, pady=4)
        self.log_text.grid(row=0, column=0, sticky="nsew")

    # -- pestana de dispositivos ------------------------------------------

    def _build_devices_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)

        self.devices_scroll = ScrollableFrame(parent)
        self.devices_scroll.grid(row=0, column=0, sticky="nsew")
        self.devices_container = self.devices_scroll.inner

        ttk.Button(parent, text="🔄  Actualizar dispositivos", style="Secondary.TButton",
                   command=self.refresh_devices).grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.refresh_devices()

    def refresh_devices(self):
        try:
            devices = audio_devices.list_devices(refresh=True)
        except audio_devices.DeviceError as e:
            self.log(f"❌ {e}")
            messagebox.showerror("Error", str(e))
            return

        wanted = {}
        for device in devices:
            key = f"{device.kind}:{device.name}"
            wanted[key] = device

        # Solo se destruyen las filas que ya no existen y no estan corriendo:
        # destruir todo y volver a crear rompia las vigilancias activas.
        for key, row in list(self.device_rows.items()):
            if key not in wanted and not row.watcher:
                row.destroy()
                del self.device_rows[key]

        for key, device in wanted.items():
            row = self.device_rows.get(key)
            if row is None:
                row = WatcherRow(
                    self.devices_container, self, device.kind, device.name,
                    device.label(), key, self.gui_state.get(key),
                )
                self.device_rows[key] = row
            row.pack_forget()
            row.pack(fill="x", pady=(0, 8), padx=(0, 4))

        if not self.device_rows:
            ttk.Label(self.devices_container, text="No se detectaron dispositivos de audio.",
                      style="CardMuted.TLabel").pack(anchor="w")

    # -- pestana de apps ---------------------------------------------------

    def _build_apps_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        header = ttk.Frame(parent, style="Surface.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        header.columnconfigure(1, weight=1)

        ttk.Label(header, text="Buscar:", style="CardMuted.TLabel").grid(row=0, column=0, sticky="w")
        self.app_query_var = tk.StringVar()
        entry = ttk.Entry(header, textvariable=self.app_query_var, style="Card.TEntry")
        entry.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        entry.bind("<Return>", lambda _e: self.refresh_apps())

        self.apps_scroll = ScrollableFrame(parent)
        self.apps_scroll.grid(row=1, column=0, sticky="nsew")
        self.apps_container = self.apps_scroll.inner
        self.apps_placeholder = None

        self.refresh_apps_btn = ttk.Button(parent, text="🔄  Actualizar lista de apps",
                                           style="Secondary.TButton", command=self.refresh_apps)
        self.refresh_apps_btn.grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.refresh_apps()

    def _set_apps_placeholder(self, text):
        if self.apps_placeholder is not None:
            self.apps_placeholder.destroy()
            self.apps_placeholder = None
        if text:
            self.apps_placeholder = ttk.Label(self.apps_container, text=text, style="CardMuted.TLabel")
            self.apps_placeholder.pack(anchor="w")

    def refresh_apps(self):
        # pycaw (COM) puede tardar y ademas no debe compartir hilo con
        # soundcard: se consulta en el executor dedicado de app_audio y el
        # resultado vuelve a Tk con self.after().
        if self._apps_loading:
            return
        self._apps_loading = True
        self.refresh_apps_btn.config(state="disabled", text="🔄  Actualizando...")
        self._set_apps_placeholder("Buscando apps con audio...")

        query = self.app_query_var.get().strip() or None

        def worker():
            try:
                suggestions = app_audio.suggest_targets(limit=40, query=query)
                error = None
            except Exception as e:
                suggestions, error = [], str(e)
            try:
                self.after(0, lambda: self._apply_apps_result(suggestions, error))
            except (RuntimeError, tk.TclError):
                pass  # la ventana ya se cerro

        app_audio.executor().submit(worker)

    def _apply_apps_result(self, suggestions, error):
        self._apps_loading = False
        try:
            self.refresh_apps_btn.config(state="normal", text="🔄  Actualizar lista de apps")
        except tk.TclError:
            return

        if error:
            self._set_apps_placeholder(f"No se pudo leer la lista de apps: {error}")
            return

        wanted = {item["name"].lower(): item for item in suggestions}

        # Las apps que ya se estan vigilando se conservan aunque hayan dejado
        # de aparecer en la lista (p.ej. si dejaron de sonar un instante).
        for key, row in list(self.app_rows.items()):
            if key not in wanted and not row.watcher:
                row.destroy()
                del self.app_rows[key]

        ordered = []
        for key, row in self.app_rows.items():
            if key not in wanted:
                ordered.append((0, key, row))
        for item in suggestions:
            key = item["name"].lower()
            row = self.app_rows.get(key)
            title = ("🔊 " if item["playing"] else "▫️ ") + item["name"]
            if row is None:
                row = WatcherRow(self.apps_container, self, "app", item["name"],
                                 title, f"app:{item['name']}", self.gui_state.get(f"app:{item['name']}"))
                self.app_rows[key] = row
            ordered.append((1 if item["playing"] else 2, key, row))

        ordered.sort(key=lambda entry: (entry[0], entry[1]))
        for _rank, _key, row in ordered:
            row.pack_forget()
            row.pack(fill="x", pady=(0, 8), padx=(0, 4))

        self._set_apps_placeholder("" if self.app_rows else "(ninguna app detectada)")

    # -- control ----------------------------------------------------------

    def all_rows(self):
        return list(self.device_rows.values()) + list(self.app_rows.values())

    def log(self, message):
        try:
            self.log_text.config(state="normal")
            self.log_text.insert("end", f"[{time.strftime('%H:%M:%S')}] {message}\n")
            self.log_text.see("end")
            self.log_text.config(state="disabled")
        except tk.TclError:
            pass
        logger.info(message)

    def _warn_missing_credentials(self):
        messagebox.showwarning(
            "Falta configurar Telegram",
            self.tg_config.describe_problem() + "\n\nAbre Ajustes para rellenarlo.",
        )
        self.open_settings()

    def open_settings(self):
        SettingsDialog(self)

    def start_monitoring(self):
        if self.running:
            return
        problem = self.tg_config.describe_problem()
        if problem:
            messagebox.showerror("Falta configurar Telegram", problem)
            return

        enabled = [row for row in self.all_rows() if row.is_enabled()]
        if not enabled:
            messagebox.showwarning("Aviso", "Marca al menos un dispositivo o app para monitorear.")
            return

        started = 0
        for row in enabled:
            if row.watcher:
                continue
            if row.get_params() is None:
                self.log(f"⚠️ Umbral o duracion invalidos en {row.title_text}, se omite.")
                continue
            if row.start():
                started += 1
                self.log(f"▶ {row.title_text}: vigilancia iniciada.")

        if not started:
            return

        self.running = True
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status_label.config(text=f"Monitoreando {started} objetivo(s)...")
        self._update_tray_title()

    def stop_monitoring(self):
        if not self.running:
            return
        for row in self.all_rows():
            if row.watcher:
                row.stop()
        self.supervisor.stop_owner("local")
        self.running = False
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status_label.config(text="Detenido.")
        self.master_vu["value"] = 0
        self.log("■ Monitoreo detenido.")
        self._update_tray_title()

    def toggle_monitoring(self):
        self.stop_monitoring() if self.running else self.start_monitoring()

    # -- estado global -----------------------------------------------------

    def _tick_global_status(self):
        running = [row for row in self.all_rows() if row.watcher]
        if not running:
            self.global_status_pill.config(text="●  Sin actividad")
            self.master_vu["value"] = 0
        else:
            counts = {}
            for row in running:
                counts[row.current_state] = counts.get(row.current_state, 0) + 1
            parts = [
                f"{STATE_EMOJI.get(state, '•')} {count}"
                for state, count in sorted(counts.items())
            ]
            self.global_status_pill.config(text="  ".join(parts))
            self.master_vu["value"] = min(1.0, max(row.watcher.last_level for row in running) * 8)

            alerting = [row for row in running if row.current_state == WatcherState.ALERTING]
            if alerting:
                worst = max(alerting, key=lambda r: r.watcher.silence_elapsed)
                self.status_label.config(
                    text=f"🔴 {worst.title_text}: {fmt_duration(worst.watcher.silence_elapsed)} en silencio"
                )
            elif self.running:
                self.status_label.config(text=f"Monitoreando {len(running)} objetivo(s)...")

        self.after(1000, self._tick_global_status)

    # -- bandeja del sistema ------------------------------------------------

    def _tray_image(self):
        if ICON_PATH.exists():
            try:
                return Image.open(ICON_PATH)
            except Exception:
                pass
        img = Image.new("RGB", (64, 64), ACCENT)
        draw = ImageDraw.Draw(img)
        draw.ellipse((12, 12, 52, 52), fill="white")
        return img

    def _update_tray_title(self):
        if self.tray_icon is not None:
            try:
                self.tray_icon.title = "AngryFurBot — " + ("vigilando" if self.running else "detenido")
            except Exception:
                pass

    def minimize_to_tray(self):
        if not TRAY_AVAILABLE:
            return
        if self.tray_icon is not None:
            self.withdraw()
            return
        self.withdraw()

        def on_restore(icon=None, item=None):
            icon = icon or self.tray_icon
            if icon is not None:
                icon.stop()
            self.tray_icon = None
            self.after(0, self.deiconify)

        def on_toggle(icon, item):
            self.after(0, self.toggle_monitoring)

        def on_quit(icon, item):
            icon.stop()
            self.tray_icon = None
            self.after(0, self._quit)

        menu = pystray.Menu(
            pystray.MenuItem("Mostrar panel", on_restore, default=True),
            pystray.MenuItem("Iniciar / Detener", on_toggle),
            pystray.MenuItem("Salir", on_quit),
        )
        self.tray_icon = pystray.Icon("angryfurbot", self._tray_image(), "AngryFurBot", menu)
        self._update_tray_title()
        threading.Thread(target=self.tray_icon.run, daemon=True).start()
        self.log("Minimizado a la bandeja del sistema.")

    # -- cierre -------------------------------------------------------------

    def _on_close(self):
        if self.close_to_tray and TRAY_AVAILABLE:
            self.minimize_to_tray()
            return
        self._quit()

    def _quit(self):
        if self.running:
            self.stop_monitoring()
        self.supervisor.stop_all()
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
            self.tray_icon = None
        self.persist_app_state()
        save_gui_state(self.gui_state)
        if self.notifier is not None:
            self.notifier.stop(timeout=2)
        app_audio.shutdown_executor()
        audio_devices.shutdown_executor()
        self.destroy()


if __name__ == "__main__":
    AudioMonitorGUI().mainloop()
