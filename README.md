# AngryFurBot — detector de silencio con avisos por Telegram

Vigila tu **micrófono**, tu **salida de audio** (todo lo que suene por los
altavoces) o una **app concreta** (Spotify, un navegador, un juego…) en
Windows. Si el audio baja del umbral durante más tiempo del configurado, te
avisa por Telegram y **sigue avisando cada 30 segundos o menos** mientras el
silencio continúe, hasta mandarte el aviso de **sonido restaurado**.

Es **multi-usuario**: cualquiera que le escriba `/start` al bot queda
registrado y configura sus propias vigilancias — su dispositivo o app, su
umbral, sus tiempos — sin ver ni tocar las de los demás. Cada usuario tiene su
archivo en `users_data/` y cada vigilancia corre en su propio hilo.

> ⚠️ **Seguridad — léelo antes de nada**
>
> 1. Si un `bot_token` real llegó a estar escrito en `config.json` o
>    `config_classic.json` y ese archivo se compartió, se subió a git o se
>    envió por cualquier vía, **revócalo ya**: `/revoke` en @BotFather y luego
>    `/token` para generar uno nuevo. Con el token, cualquiera controla tu bot.
>    Mejor aún: no lo guardes en disco y usa la variable de entorno
>    `ANGRYFURBOT_TOKEN`.
> 2. El bot expone los **dispositivos y procesos de la máquina donde corre**.
>    Si no quieres que un desconocido que encuentre tu bot pueda ver qué
>    programas tienes abiertos, restringe el acceso con `allowed_chat_ids`
>    (ver más abajo). Sin esa lista, el bot es abierto y lo avisa al arrancar.

---

## 1. Instalación

```bash
pip install -r requirements.txt
```

| Paquete | Para qué |
|---|---|
| `soundcard` | captura de micrófonos y de la salida (WASAPI *loopback*) |
| `numpy` | nivel RMS y pico de cada bloque de audio |
| `requests` | envío de alertas por la API HTTP de Telegram |
| `pycaw` + `comtypes` | nivel de audio **por aplicación** |
| `psutil` | lista de procesos para el selector de apps |
| `python-telegram-bot` (20.x) | el bot interactivo |
| `pystray` + `Pillow` | *opcionales*: bandeja del sistema en la GUI |

Requiere **Windows** (el modo *loopback* y las sesiones de audio por proceso
son exclusivos de WASAPI). Probado con Python 3.11–3.14.

Comprueba que todo está bien sin necesidad de tocar nada:

```bash
python selftest.py      # prueba el motor entero, sin audio ni red
python list_devices.py  # nombres exactos de tus dispositivos y apps
```

## 2. Crear el bot de Telegram

1. Habla con **@BotFather** en Telegram.
2. `/newbot`, ponle nombre y copia el **token**.
3. Guárdalo en `config.json` o, mejor, en una variable de entorno:

```powershell
$env:ANGRYFURBOT_TOKEN = "123456789:AA..."
```

## 3. Configurar `config.json`

```json
{
  "bot_token": "TU_BOT_TOKEN_AQUI",
  "default_threshold": 0.01,
  "default_duration_seconds": 15,
  "default_cooldown_seconds": 30,
  "allowed_chat_ids": [],
  "admin_chat_ids": [],
  "max_watchers_per_user": 8
}
```

- `allowed_chat_ids`: si lo dejas vacío, cualquiera puede usar el bot. Pon ahí
  los ids permitidos (cada usuario ve el suyo con `/id`) para hacerlo privado.
- `admin_chat_ids`: quién puede usar `/stats` (estado del servidor).
- El resto son solo valores por defecto: cada vigilancia puede tener su propio
  umbral, duración, intervalo de chequeo y cooldown.
- El cooldown se recorta automáticamente a **30 s como máximo**, por diseño.

## 4. Ejecutar el bot (modo multi-usuario)

```bash
python bot.py
```

Déjalo corriendo (o conviértelo en tarea programada / servicio con `nssm`). Si
se reinicia, **retoma solo** las vigilancias guardadas de todos los usuarios,
respetando cuáles estaban pausadas.

## 5. Usarlo desde Telegram

Escríbele **`/start`** y aparece el panel:

- **📋 Mis vigilancias** — una fila por vigilancia con su estado de un vistazo
  (🟢 activo · 🟠 en silencio · 🔴 alertando · 🟡 pausado · ⚪ detenido ·
  ⛔ error). Al tocarla se abre su ficha.
- **➕ Nueva** — flujo guiado: tipo → **lista de dispositivos o apps detectadas**
  (con buscador y paginación; nada de escribir nombres de proceso a mano) →
  espera antes de avisar → sensibilidad → cada cuánto repetir.
- **📊 Estado en vivo**, **⏸️ Pausar todo / ▶️ Reanudar todo**, **🎧 Dispositivos**,
  **🎵 Apps activas**, **⚙️ Ajustes**, **❓ Ayuda**.

### La ficha de una vigilancia

```
🎵 App · vigilancia #3
🔴 Silencio detectado

🎯 spotify.exe
📈 ▓▓░┊░░░░░░░░  0.0031
🚦 Umbral 0.0100 · chequeo cada 0.5s
⏱️ En silencio desde hace 42s
🔔 3 avisos ahora · 11 en total
🔁 Se repite cada 30s
🕒 En marcha desde hace 12m
```

Con botones para **pausar**, **actualizar**, **📡 en vivo** (la tarjeta se
refresca sola durante un minuto y medio), cambiar **umbral / espera / repetición
/ intervalo de chequeo** (por presets o escribiendo el valor) y **eliminar**.

### Ajustes por usuario

Cooldown global, aviso de recuperación on/off, **😴 silenciar avisos** 15–240
minutos (sigue vigilando, solo calla) y borrar todo.

### Comandos (siguen funcionando todos)

```
/devices  /apps                      ver qué hay disponible
/watch_mic <texto> [umbral] [seg]    vigilar un micrófono
/watch_speaker <texto> [umbral] [seg]  vigilar la salida de audio
/watch_app <proceso> [umbral] [seg]  vigilar una app
/list  /status  /stop <id>  /stopall
/setthreshold <id> <valor>   /setduration <id> <seg>
/setcooldown <seg>           (máx. 30)
/mute [min]  /unmute         silenciar avisos temporalmente
/test  /id  /help  /stats
```

## 6. Calibrar el umbral

El umbral es el nivel (0–1) por debajo del cual se considera silencio.

- **Nunca detecta silencio** aunque no suene nada → súbelo (p. ej. `0.02`).
- **Detecta silencio falso** con audio bajito → bájalo (p. ej. `0.005`).
- Para saber el número exacto: mira **🎵 Apps activas** en el bot, la pestaña
  **Apps** de la GUI o `python list_devices.py --watch` mientras suena algo, y
  pon el umbral algo por debajo de lo que veas.

---

## Panel de escritorio (`gui.py`)

```bash
python gui.py
```

- Pestañas **Dispositivos** y **Apps** (con buscador), VU-metro en vivo y botón
  de pausa por fila, y VU general + resumen de estados en la cabecera.
- **⚙ Ajustes**: token, chat_id, cooldown, intervalo, aviso de recuperación,
  «al cerrar minimizar a la bandeja» y «empezar a monitorear al abrir», con un
  botón **Probar Telegram**. Se guarda en `config_classic.json`.
- **🗕 Bandeja**: sigue corriendo en segundo plano; desde el icono se puede
  mostrar el panel, iniciar/detener o salir.
- Los umbrales y qué está marcado se recuerdan en `gui_state.json`.

Usa `config_classic.json` (`telegram.bot_token`, `telegram.chat_id`,
`telegram.cooldown_seconds`) o las variables `ANGRYFURBOT_TOKEN` /
`ANGRYFURBOT_CHAT_ID`. `python test_telegram.py` te dice tu `chat_id`.

## Modo clásico de consola (`monitor.py`)

Un solo usuario, dispositivos fijos, sin bot interactivo:

```bash
python monitor.py
```

Lee las secciones `monitor_microphone` / `monitor_speaker` de
`config_classic.json`. Si dejas `device_name_contains` vacío, usa el
dispositivo predeterminado del sistema. No vigila apps concretas (para eso,
`bot.py` o `gui.py`).

## Empaquetado a .exe

```bash
pip install pyinstaller
pyinstaller gui.spec     # dist/AngryFurBot.exe       (ventana)
pyinstaller bot.spec     # dist/AngryFurBot-bot.exe   (consola)
```

Los dos son **un único archivo** (modo *onefile*) con el icono `blui.ico`.

Deja la configuración **junto al .exe** — `config_classic.json` para el panel,
`config.json` para el bot — o usa las variables de entorno. Es importante:
Windows descomprime el ejecutable en una carpeta temporal que se borra al
salir, pero el programa lee y escribe la configuración, `gui_state.json` y
`users_data/` en la carpeta donde está el .exe (`storage.app_dir()`), así que
no se pierde nada al reiniciar. El icono, en cambio, viaja dentro del paquete
(`storage.resource_dir()`).

En el panel no hace falta ni crear el archivo a mano: se abre, se pulsa
**⚙ Ajustes**, se pegan el token y el chat_id, y **Probar Telegram** confirma
que llegan los mensajes.

> El `config_classic.json` que dejes junto al .exe contiene tu token. No
> comprimas ni compartas esa carpeta tal cual.

---

## Cómo está organizado

| Archivo | Responsabilidad |
|---|---|
| `bot.py` | bot de Telegram multi-usuario (UI, comandos, callbacks) |
| `gui.py` | panel de escritorio + bandeja del sistema |
| `monitor.py` | `MonitorSupervisor` (registro de vigilancias concurrentes) + modo clásico |
| `audio_backends.py` | `SilenceDetector` + `DeviceWatcher` / `AppWatcher` |
| `audio_devices.py` | enumeración y resolución de dispositivos (soundcard) |
| `app_audio.py` | nivel de audio por proceso (pycaw / Core Audio) |
| `storage.py` | configuración por usuario, con transacciones y límites |
| `notifier.py` | cola de salida hacia Telegram con reintentos y límites de ritmo |
| `telegram_config.py` | carga de `config.json` / `config_classic.json` y variables de entorno |
| `list_devices.py` | utilidad de consola para ver dispositivos, apps y niveles |
| `test_telegram.py` | comprueba el token y te dice tu `chat_id` |
| `selftest.py` | pruebas del motor sin audio, sin red y sin Windows |
| `gui.spec` / `bot.spec` | empaquetado a .exe con PyInstaller (icono `blui.ico`) |

## Notas técnicas

- **Loopback**: la salida se captura con el modo *loopback* de WASAPI, que
  Windows expone como un micrófono virtual por cada altavoz.
- **Niveles**: en dispositivos se calcula el RMS y el pico de cada bloque con
  numpy; en apps se lee `IAudioMeterInformation` de la sesión de audio del
  proceso. Si una app tiene varias sesiones (pestañas de un navegador), se toma
  la que más suene.
- **Histeresis**: una vez en silencio hace falta superar `umbral × 1.15` para
  darlo por recuperado, para que un nivel que oscila justo en el umbral no
  genere una ristra de alertas y recuperaciones.
- **Concurrencia**: cada vigilancia es un hilo independiente del event loop de
  `asyncio`; enumerar dispositivos o apps (que es lento y toca COM) se hace en
  executors dedicados, así que el bot nunca se queda bloqueado.
- **COM**: `soundcard` inicializa COM en modo MTA y `comtypes` en STA, y no
  pueden hacerlo en el mismo hilo. `soundcard` se carga desde el hilo principal
  (`audio_devices.preload()`, que ancla el MTA del proceso) y `comtypes` desde
  un hilo dedicado. Si se mezclan, uno de los dos deja de funcionar en todo el
  proceso.
- **Reconexión**: si un dispositivo desaparece (desenchufas los auriculares) el
  watcher no muere: reintenta con *backoff* hasta 30 s, avisa si el fallo
  persiste y manda «vigilancia restablecida» al recuperarse.
- **Alertas**: salen por una cola con reintentos, respetando el `retry_after`
  de los 429 y el límite de ~1 mensaje por segundo y chat, así que los hilos de
  audio nunca esperan a la red.
- **Límites**: 8 vigilancias por usuario y 64 en total por máquina
  (configurables), para que nadie tumbe el equipo.
- **Datos**: `users_data/<chat_id>.json`, con escritura atómica y un lock por
  usuario. Un JSON corrupto se aparta como `.bad.json` y se empieza limpio en
  vez de tirar el bot.
