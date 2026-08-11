"""
Prueba rapida de la configuracion de Telegram.

Verifica que el bot_token sea valido y muestra los chat_id disponibles (los de
cualquier mensaje que le hayas enviado al bot). Usalo si te sale el error
"chat not found" o si no sabes cual es tu chat_id.

Lee el token de config_classic.json o de la variable de entorno
ANGRYFURBOT_TOKEN (esta ultima manda).

Uso:
    1. Abre Telegram, busca tu bot y enviale un mensaje cualquiera (ej: "hola").
    2. Ejecuta: python test_telegram.py
"""

import requests

import telegram_config

TIMEOUT = 15


def main():
    config = telegram_config.load_classic_config()
    token = config.token
    configured_chat_id = config.chat_id

    if not token:
        print(
            "❌ No hay bot_token.\n"
            f"   Ponlo en {telegram_config.CLASSIC_CONFIG_PATH.name} "
            f"(telegram.bot_token) o en la variable {telegram_config.ENV_TOKEN}."
        )
        return 1

    print(f"Token leido: {telegram_config.redact(token)}")

    # 1. Verificar que el token es valido
    try:
        me = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=TIMEOUT).json()
    except requests.RequestException as e:
        print(f"❌ No se pudo contactar con Telegram: {e}")
        return 1

    if not me.get("ok"):
        print("❌ El bot_token parece invalido:")
        print(f"   {me.get('description', me)}")
        return 1

    bot_username = me["result"]["username"]
    print(f"✅ Token valido. Tu bot es: @{bot_username}")

    # 2. Buscar chat_ids a partir de los mensajes recientes
    try:
        updates = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates", timeout=TIMEOUT
        ).json()
    except requests.RequestException as e:
        print(f"❌ No se pudieron obtener los updates: {e}")
        return 1

    if not updates.get("ok"):
        print(f"❌ No se pudieron obtener los updates: {updates.get('description', updates)}")
        return 1

    results = updates.get("result", [])
    if not results:
        print(
            f"\n⚠️  No hay mensajes registrados todavia.\n"
            f"   1. Abre Telegram y busca @{bot_username}\n"
            f"   2. Envíale cualquier mensaje (ej: 'hola')\n"
            f"   3. Vuelve a correr este script.\n"
            f"   (Si el bot esta corriendo con bot.py, para el primero: se lleva\n"
            f"    los updates y aqui no queda nada que ver.)"
        )
        return 0

    chat_ids = {}
    for update in results:
        msg = (
            update.get("message")
            or update.get("channel_post")
            or update.get("edited_message")
            or (update.get("callback_query") or {}).get("message")
        )
        if not msg:
            continue
        chat = msg["chat"]
        chat_ids[chat["id"]] = (
            chat.get("title") or chat.get("username") or chat.get("first_name", "")
        )

    print("\n📋 Chats encontrados:")
    for chat_id, name in chat_ids.items():
        marker = "   <-- el que tienes configurado" if str(chat_id) == str(configured_chat_id) else ""
        print(f"   chat_id: {chat_id}   ({name}){marker}")

    print(f"\nchat_id configurado: {configured_chat_id or '(ninguno)'}")
    if not configured_chat_id:
        print("ℹ️  Copia el chat_id de arriba en config_classic.json (telegram.chat_id).")
    elif str(configured_chat_id) not in {str(c) for c in chat_ids}:
        print("❌ Ese chat_id NO aparece arriba. Copia el correcto en config_classic.json.")
    else:
        print("✅ Ese chat_id es correcto y deberia funcionar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
