"""
Pruebas automaticas del nucleo, sin audio, sin Telegram y sin Windows.

Cubre lo que de verdad puede romperse en silencio:

  - la maquina de estados de silencio (avisos, repeticiones, recuperacion,
    histeresis, pausa),
  - el almacenamiento por usuario (clamps, migracion de esquema, escrituras
    concurrentes que antes podian duplicar ids),
  - el despachador de Telegram (429, 403, reintentos) con un transporte falso,
  - el supervisor de concurrencia (limites por usuario y globales),
  - las utilidades de nivel (RMS/pico con numpy).

No hace falta ni soundcard ni pycaw ni conexion: todo lo externo se simula.

Uso:
    python selftest.py
"""

import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

# Con la salida redirigida, Windows usa cp1252 y los emoji del informe
# reventarian con UnicodeEncodeError.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition), detail))
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail and not condition else ""))
    return bool(condition)


def section(title):
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------------------
# 1. Maquina de estados de silencio
# ---------------------------------------------------------------------------

def test_detector():
    from audio_backends import SilenceDetector, WatcherState

    section("Deteccion de silencio")

    d = SilenceDetector(threshold=0.01, duration=15, cooldown=30)

    check("con sonido no pasa nada", d.update(0.5, 0.0) == [])
    check("estado activo con sonido", d.state == WatcherState.ACTIVE)

    check("silencio corto no avisa", d.update(0.0, 1.0) == [])
    check("estado 'en silencio' antes del aviso", d.state == WatcherState.SILENCE)
    check("14s de silencio todavia no avisan", d.update(0.0, 15.0) == [])

    events = d.update(0.0, 16.5)
    check("avisa al superar la duracion", [e[0] for e in events] == ["alert"],
          f"eventos={events}")
    check("estado alertando", d.state == WatcherState.ALERTING)
    check("segundos reportados correctos", abs(events[0][1] - 15.5) < 0.01,
          f"elapsed={events[0][1]}")

    check("no repite antes del cooldown", d.update(0.0, 40.0) == [])
    events = d.update(0.0, 47.0)
    check("repite al pasar el cooldown", [e[0] for e in events] == ["repeat"])
    check("cuenta 2 avisos", d.alerts_sent == 2, f"alerts_sent={d.alerts_sent}")

    # Histeresis: hace falta superar umbral*1.15 para dar por recuperado.
    check("nivel justo sobre el umbral sigue contando como silencio",
          d.update(0.0105, 48.0) == [])
    events = d.update(0.05, 49.0)
    check("recupera con sonido claro", [e[0] for e in events] == ["recovered"])
    check("vuelve a estado activo", d.state == WatcherState.ACTIVE)
    check("olvida el silencio anterior", d.silence_start is None)

    # Segundo ciclo completo, para verificar que el reinicio deja todo limpio.
    d.update(0.0, 50.0)
    events = d.update(0.0, 70.0)
    check("segundo ciclo vuelve a avisar", [e[0] for e in events] == ["alert"])
    check("acumula 3 avisos", d.alerts_sent == 3)

    d.reset()
    check("reset limpia el estado", d.silence_start is None and not d.alerted)

    # Cooldown de 10s: tres repeticiones en 35s.
    fast = SilenceDetector(threshold=0.01, duration=5, cooldown=10)
    fast.update(0.0, 0.0)
    repeats = 0
    alerts = 0
    for tick in range(1, 40):
        for kind, _elapsed in fast.update(0.0, float(tick)):
            alerts += kind == "alert"
            repeats += kind == "repeat"
    check("un unico primer aviso", alerts == 1, f"alerts={alerts}")
    check("repite cada 10s (3 veces en 34s)", repeats == 3, f"repeats={repeats}")


# ---------------------------------------------------------------------------
# 2. Niveles de audio
# ---------------------------------------------------------------------------

def test_levels():
    import numpy as np
    from audio_backends import analyse_block, fmt_duration

    section("Medicion de nivel")

    silence = np.zeros((1024, 2), dtype=np.float32)
    rms, peak = analyse_block(silence)
    check("silencio da 0", rms == 0.0 and peak == 0.0)

    tone = np.sin(np.linspace(0, 40 * np.pi, 4096, dtype=np.float64)).reshape(-1, 1)
    rms, peak = analyse_block(tone)
    check("RMS de un seno ~0.707", abs(rms - 0.7071) < 0.01, f"rms={rms}")
    check("pico de un seno ~1.0", abs(peak - 1.0) < 0.01, f"peak={peak}")

    dirty = np.array([[float("nan")], [float("inf")], [0.5]])
    rms, peak = analyse_block(dirty)
    check("NaN/inf no envenenan la medida", rms == rms and peak == 0.5, f"rms={rms} peak={peak}")

    check("bloque vacio no rompe", analyse_block(np.zeros((0, 2))) == (0.0, 0.0))
    check("None no rompe", analyse_block(None) == (0.0, 0.0))

    check("formato de duracion corto", fmt_duration(42) == "42s")
    check("formato de duracion medio", fmt_duration(72) == "1m 12s")
    check("formato de duracion largo", fmt_duration(3900) == "1h 05m")


# ---------------------------------------------------------------------------
# 3. Almacenamiento por usuario
# ---------------------------------------------------------------------------

def test_storage():
    import json

    import storage

    section("Almacenamiento por usuario")

    tmp = Path(tempfile.mkdtemp(prefix="angryfur-test-"))
    original_dir = storage.DATA_DIR
    storage.DATA_DIR = tmp
    try:
        check("cooldown se recorta a 30s", storage.clamp_cooldown(120) == 30.0)
        check("cooldown minimo de 5s", storage.clamp_cooldown(0) == 5.0)
        check("cooldown invalido usa el defecto", storage.clamp_cooldown("abc") == 30.0)
        check("umbral se acota", storage.clamp_threshold(50) == 1.0)
        check("intervalo se acota", storage.clamp_interval(0.0001) == storage.MIN_INTERVAL_SECONDS)

        w = storage.add_watcher("111", "app", "spotify.exe", threshold=0.02, duration=20)
        check("crea vigilancia con id 1", w["id"] == 1)
        check("guarda el umbral", w["threshold"] == 0.02)
        check("cooldown por vigilancia empieza vacio", w["cooldown"] is None)

        user = storage.load_user("111")
        check("cooldown efectivo hereda del global",
              storage.effective_cooldown(user, w) == user["cooldown_seconds"])
        storage.update_watcher("111", 1, cooldown=10)
        check("cooldown propio manda",
              storage.effective_cooldown(user, storage.get_watcher("111", 1)) == 10.0)

        storage.update_watcher("111", 1, threshold=99)
        check("update aplica el clamp", storage.get_watcher("111", 1)["threshold"] == 1.0)

        # Aislamiento entre usuarios
        storage.add_watcher("222", "mic", "Microfono X")
        check("cada usuario tiene su archivo",
              len(storage.load_user("111")["watchers"]) == 1
              and len(storage.load_user("222")["watchers"]) == 1)
        check("se listan ambos usuarios", set(storage.list_all_user_ids()) == {"111", "222"})

        # Concurrencia: N hilos creando vigilancias a la vez no deben repetir id
        storage.remove_all_watchers("333")
        errors = []

        def worker(index):
            try:
                storage.add_watcher("333", "app", f"app{index}.exe")
            except storage.StorageError:
                pass
            except Exception as e:  # cualquier otro fallo si es un bug real
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(storage.MAX_WATCHERS_PER_USER)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        watchers = storage.load_user("333")["watchers"]
        ids = [x["id"] for x in watchers]
        check("sin errores en escrituras concurrentes", not errors, str(errors))
        check("ids unicos con escrituras concurrentes", len(ids) == len(set(ids)), f"ids={ids}")
        check("se guardaron todas", len(watchers) == storage.MAX_WATCHERS_PER_USER, f"n={len(watchers)}")

        try:
            storage.add_watcher("333", "app", "una-mas.exe")
            over_limit = False
        except storage.StorageError:
            over_limit = True
        check("respeta el limite por usuario", over_limit)

        # Migracion desde el esquema v1 (sin interval, cooldown ni version)
        legacy = {
            "chat_id": "444",
            "cooldown_seconds": 300,
            "watchers": [{"id": 7, "type": "app", "target": "vlc.exe",
                          "threshold": 0.01, "duration": 15}],
            "next_id": 2,
        }
        (tmp / "444.json").write_text(json.dumps(legacy), encoding="utf-8")
        migrated = storage.load_user("444")
        check("migra el cooldown fuera de rango", migrated["cooldown_seconds"] == 30.0)
        check("anade interval por defecto", migrated["watchers"][0]["interval"] > 0)
        check("next_id se corrige por encima del mayor id", migrated["next_id"] == 8,
              f"next_id={migrated['next_id']}")
        new = storage.add_watcher("444", "app", "otra.exe")
        check("no reutiliza un id existente", new["id"] == 8)

        # Un JSON corrupto no debe tumbar nada
        (tmp / "555.json").write_text("{esto no es json", encoding="utf-8")
        recovered = storage.load_user("555")
        check("JSON corrupto se recupera vacio", recovered["watchers"] == [])
        check("el archivo corrupto se aparta", (tmp / "555.bad.json").exists())

        # Silenciado temporal
        storage.set_muted_until("111", time.time() + 60)
        check("detecta silenciado", storage.is_muted("111"))
        storage.set_muted_until("111", 0)
        check("detecta no silenciado", not storage.is_muted("111"))
    finally:
        storage.DATA_DIR = original_dir
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 4. Notificador de Telegram (transporte simulado)
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or str(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("sin json")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, data=None, timeout=None):
        self.calls.append({"url": url, "data": data})
        return self.responses.pop(0) if self.responses else FakeResponse(200, {"ok": True})

    def close(self):
        pass


def test_notifier():
    import notifier as notifier_mod

    section("Notificador de Telegram")

    n = notifier_mod.TelegramNotifier("123:ABC")
    n._session = FakeSession([FakeResponse(200, {"ok": True})])
    ok, retry, permanent, error = n._post(notifier_mod._Message("55", "hola", "HTML"))
    check("envio correcto", ok and error is None)
    sent = n._session.calls[0]["data"]
    check("manda chat_id y texto", sent["chat_id"] == "55" and sent["text"] == "hola")
    check("respeta el parse_mode", sent["parse_mode"] == "HTML")

    n._session = FakeSession([FakeResponse(429, {"parameters": {"retry_after": 7}})])
    ok, retry, permanent, _error = n._post(notifier_mod._Message("55", "hola"))
    check("429 pide esperar lo indicado", (not ok) and retry == 7 and not permanent, f"retry={retry}")

    blocked = []
    n.on_blocked = blocked.append
    n._session = FakeSession([FakeResponse(403, {"description": "bot was blocked by the user"})])
    ok, _retry, permanent, _error = n._post(notifier_mod._Message("55", "hola"))
    check("403 es definitivo", (not ok) and permanent)
    check("403 avisa de usuario bloqueado", blocked == ["55"])

    n._session = FakeSession([FakeResponse(500, None, "boom")])
    ok, _retry, permanent, _error = n._post(notifier_mod._Message("55", "hola"))
    check("500 se reintenta", (not ok) and not permanent)

    # Cola completa: dos chats distintos para no pagar el ritmo por chat.
    n2 = notifier_mod.TelegramNotifier("123:ABC")
    n2.start()
    n2._session = FakeSession([])
    n2.send("1", "uno")
    n2.send("2", "dos")
    n2.stop(timeout=5)
    check("la cola se vacia al cerrar", n2.pending() == 0)
    check("cuenta los enviados", n2.stats["sent"] == 2, f"stats={n2.stats}")

    check("escape HTML", notifier_mod.escape_html("<b>&x</b>") == "&lt;b&gt;&amp;x&lt;/b&gt;")


# ---------------------------------------------------------------------------
# 5. Supervisor de concurrencia
# ---------------------------------------------------------------------------

class FakeWatcher:
    def __init__(self, owner, watcher_id, target="fake.exe"):
        self.owner = str(owner)
        self.watcher_id = watcher_id
        self.target_desc = target
        self.label = f"Fake #{watcher_id}"
        self.state = "active"
        self.started = False
        self.stopped = False
        self.settings = {}

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def join(self, timeout=None):
        pass

    def is_alive(self):
        return self.started and not self.stopped

    def apply_settings(self, **fields):
        self.settings.update(fields)


def test_supervisor():
    from monitor import MonitorSupervisor, SupervisorError

    section("Supervisor de concurrencia")

    supervisor = MonitorSupervisor(notifier=None, max_per_owner=3, max_total=5)
    supervisor.build = lambda owner, cfg, **kw: FakeWatcher(owner, cfg["id"], cfg["target"])

    for i in range(1, 4):
        supervisor.start("userA", {"id": i, "type": "app", "target": f"a{i}.exe"})
    check("arranca hasta el limite del usuario", supervisor.count_for("userA") == 3)

    try:
        supervisor.start("userA", {"id": 4, "type": "app", "target": "a4.exe"})
        limited = False
    except SupervisorError:
        limited = True
    check("bloquea al pasarse del limite por usuario", limited)

    supervisor.start("userB", {"id": 1, "type": "app", "target": "b1.exe"})
    check("otro usuario no se ve afectado", supervisor.count_for("userB") == 1)
    check("las vigilancias no se mezclan entre usuarios",
          set(supervisor.all_for("userA")) == {1, 2, 3} and set(supervisor.all_for("userB")) == {1})

    supervisor.start("userC", {"id": 1, "type": "app", "target": "c1.exe"})
    try:
        supervisor.start("userD", {"id": 1, "type": "app", "target": "d1.exe"})
        capped = False
    except SupervisorError:
        capped = True
    check("respeta el tope global de la maquina", capped, f"total={supervisor.total()}")

    old = supervisor.get("userA", 1)
    supervisor.start("userA", {"id": 1, "type": "app", "target": "nuevo.exe"})
    check("reiniciar una vigilancia para la anterior", old.stopped)
    check("y no duplica el registro", supervisor.count_for("userA") == 3)

    supervisor.apply("userA", 1, threshold=0.02)
    check("aplica ajustes en caliente", supervisor.get("userA", 1).settings["threshold"] == 0.02)

    supervisor.apply_to_owner("userA", cooldown=15)
    check("aplica a todas las del usuario",
          all(w.settings.get("cooldown") == 15 for w in supervisor.all_for("userA").values()))

    stopped = supervisor.stop("userA", 2)
    check("parar quita del registro", stopped.stopped and supervisor.count_for("userA") == 2)

    supervisor.stop_owner("userA")
    check("parar todas las de un usuario", supervisor.count_for("userA") == 0)
    check("sin tocar las de otros", supervisor.count_for("userB") == 1)

    supervisor.stop_all()
    check("stop_all vacia el registro", supervisor.total() == 0)


# ---------------------------------------------------------------------------
# 6. Formato de mensajes y barras
# ---------------------------------------------------------------------------

def test_messages():
    from audio_backends import BaseWatcher, WatcherState

    section("Mensajes de alerta")

    sent = []

    class Recorder:
        def send(self, chat_id, text, parse_mode=None):
            sent.append((chat_id, text, parse_mode))
            return True

    class Dummy(BaseWatcher):
        def _run_loop(self):
            pass

    w = Dummy(
        watcher_id=1, label="App spotify.exe", target_desc="spotify.exe",
        threshold=0.01, duration=15, cooldown=30, chat_id="99",
        notifier=Recorder(),
    )

    w._process_level(0.5)
    check("con sonido no manda nada", not sent)
    check("estado activo", w.state == WatcherState.ACTIVE)

    # Se fuerza el reloj del detector para no esperar 15 segundos reales.
    w.detector.update(0.0, time.time() - 100)
    w._process_level(0.0)
    check("manda la alerta de silencio", len(sent) == 1, f"sent={len(sent)}")
    check("usa HTML", sent[0][2] == "HTML")
    check("nombra el objetivo", "spotify.exe" in sent[0][1])
    check("dice que es silencio", "Silencio detectado" in sent[0][1])

    w._process_level(0.5)
    check("manda la recuperacion", len(sent) == 2 and "Sonido restaurado" in sent[1][1])

    # Con el modo "no molestar" no se envia nada, pero se sigue midiendo.
    muted = Dummy(
        watcher_id=2, label="App vlc.exe", target_desc="vlc.exe",
        threshold=0.01, duration=15, cooldown=30, chat_id="99",
        notifier=Recorder(), mute_check=lambda: True,
    )
    before = len(sent)
    muted.detector.update(0.0, time.time() - 100)
    muted._process_level(0.0)
    check("silenciado no envia alertas", len(sent) == before)
    check("pero sigue contando el silencio", muted.detector.alerted)

    # Escape de HTML en nombres raros
    tricky = Dummy(
        watcher_id=3, label="App <raro> & cia", target_desc="a<b>&.exe",
        threshold=0.01, duration=1, cooldown=30, chat_id="99", notifier=Recorder(),
    )
    tricky.detector.update(0.0, time.time() - 100)
    tricky._process_level(0.0)
    check("escapa los caracteres de HTML", "&lt;b&gt;" in sent[-1][1], sent[-1][1])


def test_bot_rendering():
    """Solo si python-telegram-bot esta instalado: comprueba el escapado de
    MarkdownV2, que es la fuente clasica de errores 400 de Telegram."""
    import importlib.util
    import os

    if importlib.util.find_spec("telegram") is None:
        print("\n=== Render del bot (omitido: falta python-telegram-bot) ===")
        return

    os.environ.setdefault("ANGRYFURBOT_TOKEN", "123456789:AAtest_token_para_selftest_0123456789")

    section("Render del bot")
    import bot

    check("escapa puntos y guiones", bot.md("a.b-c") == "a\\.b\\-c")
    check("escapa parentesis", bot.md("(x)") == "\\(x\\)")
    check("negrita envuelve texto escapado", bot.bold("a.b") == "*a\\.b*")
    check("codigo escapa las comillas", bot.code("a`b") == "`a\\`b`")

    bar = bot.level_bar(0.03, 0.01)
    check("barra llena con nivel alto", bar.startswith("▓"), bar)
    quiet = bot.level_bar(0.0, 0.01)
    check("barra vacia con silencio", "▓" not in quiet, quiet)

    args = ["Realtek", "Audio", "0.02", "30"]
    target, threshold, duration = bot.parse_watch_args(args, 0.01, 15)
    check("separa nombre con espacios de los numeros",
          target == "Realtek Audio" and threshold == 0.02 and duration == 30,
          f"{target!r} {threshold} {duration}")
    target, threshold, duration = bot.parse_watch_args(["spotify.exe"], 0.01, 15)
    check("usa los valores por defecto si no hay numeros",
          target == "spotify.exe" and threshold == 0.01 and duration == 15)


def main():
    print("Autotest de AngryFurBot (sin audio, sin red, sin Windows)")
    test_detector()
    test_levels()
    test_storage()
    test_notifier()
    test_supervisor()
    test_messages()
    test_bot_rendering()

    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    failed = [name for name, ok, _d in RESULTS if not ok]
    print(f"\n{'=' * 60}")
    print(f"{passed}/{len(RESULTS)} comprobaciones correctas")
    if failed:
        print("Fallos:")
        for name in failed:
            print(f"  - {name}")
        return 1
    print("Todo correcto ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
