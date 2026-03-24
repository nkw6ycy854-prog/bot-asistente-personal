import os
import telebot
import requests
import sqlite3
import threading
import time
import re

# ==============================
# 🔑 VARIABLES DE ENTORNO
# ==============================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

if not TELEGRAM_TOKEN:
    raise ValueError("Falta TELEGRAM_TOKEN en variables de entorno")

if not OPENROUTER_API_KEY:
    raise ValueError("Falta OPENROUTER_API_KEY en variables de entorno")

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

# ==============================
# 🗄️ BASE DE DATOS (THREAD SAFE)
# ==============================
conn = sqlite3.connect("bot.db", check_same_thread=False)
cursor = conn.cursor()
db_lock = threading.Lock()

# Crear tablas
with db_lock:
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS memoria (
        user_id INTEGER,
        role TEXT,
        content TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS recordatorios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        mensaje TEXT,
        tiempo INTEGER
    )
    """)

    conn.commit()

# ==============================
# 🧠 MEMORIA
# ==============================
def guardar_mensaje(user_id, role, content):
    with db_lock:
        cursor.execute(
            "INSERT INTO memoria (user_id, role, content) VALUES (?, ?, ?)",
            (user_id, role, content)
        )
        conn.commit()

def obtener_historial(user_id, limite=10):
    with db_lock:
        cursor.execute(
            "SELECT role, content FROM memoria WHERE user_id=? ORDER BY ROWID DESC LIMIT ?",
            (user_id, limite)
        )
        filas = cursor.fetchall()

    return [{"role": r, "content": c} for r, c in reversed(filas)]

# ==============================
# 🤖 IA
# ==============================
def preguntar_ia(user_id, mensaje):
    url = "https://openrouter.ai/api/v1/chat/completions"

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }

    historial = obtener_historial(user_id)

    data = {
        "model": "openrouter/free",
        "messages": [
            {"role": "system", "content": "Eres un asistente personal estilo pana, natural, útil y claro."}
        ] + historial + [
            {"role": "user", "content": mensaje}
        ]
    }

    try:
        response = requests.post(url, headers=headers, json=data, timeout=20)

        if response.status_code != 200:
            print("ERROR API:", response.text)
            return "La IA no respondió bien 😢"

        resultado = response.json()

        if "choices" not in resultado:
            print("Respuesta inválida:", resultado)
            return "Error procesando la respuesta 😢"

        respuesta = resultado["choices"][0]["message"]["content"]

        guardar_mensaje(user_id, "user", mensaje)
        guardar_mensaje(user_id, "assistant", respuesta)

        return respuesta

    except Exception as e:
        print("ERROR IA:", e)
        return "Error conectando con la IA 😢"

# ==============================
# ⏰ RECORDATORIOS
# ==============================
def crear_recordatorio(chat_id, texto, segundos):
    tiempo_envio = int(time.time()) + segundos

    with db_lock:
        cursor.execute(
            "INSERT INTO recordatorios (chat_id, mensaje, tiempo) VALUES (?, ?, ?)",
            (chat_id, texto, tiempo_envio)
        )
        conn.commit()

def verificar_recordatorios():
    while True:
        ahora = int(time.time())

        with db_lock:
            cursor.execute(
                "SELECT id, chat_id, mensaje FROM recordatorios WHERE tiempo <= ?",
                (ahora,)
            )
            pendientes = cursor.fetchall()

        for rec_id, chat_id, mensaje in pendientes:
            try:
                bot.send_message(chat_id, f"⏰ Recordatorio: {mensaje}")
            except Exception as e:
                print("Error enviando recordatorio:", e)

            with db_lock:
                cursor.execute("DELETE FROM recordatorios WHERE id=?", (rec_id,))
                conn.commit()

        time.sleep(5)

threading.Thread(target=verificar_recordatorios, daemon=True).start()

# ==============================
# 🔍 DETECTAR RECORDATORIOS
# ==============================
def detectar_recordatorio(mensaje, chat_id):
    texto = mensaje.lower()

    if "recuerdame" in texto or "acuérdame" in texto:
        numeros = re.findall(r'\d+', texto)

        if not numeros:
            return None

        cantidad = int(numeros[0])

        if "minuto" in texto:
            crear_recordatorio(chat_id, mensaje, cantidad * 60)
            return "⏰ Listo, te aviso en unos minutos"

        elif "hora" in texto:
            crear_recordatorio(chat_id, mensaje, cantidad * 3600)
            return "⏰ Perfecto, te lo recuerdo luego"

        elif "dia" in texto or "día" in texto:
            crear_recordatorio(chat_id, mensaje, cantidad * 86400)
            return "⏰ Hecho, te lo recuerdo ese día"

    return None

# ==============================
# 💬 MENSAJES
# ==============================
@bot.message_handler(func=lambda message: True)
def responder(message):
    try:
        if not message.text:
            return

        user_id = message.from_user.id
        texto = message.text.strip()

        # Recordatorio
        recordatorio = detectar_recordatorio(texto, message.chat.id)

        if recordatorio:
            bot.reply_to(message, recordatorio)
            return

        # IA
        respuesta = preguntar_ia(user_id, texto)
        bot.reply_to(message, respuesta)

    except Exception as e:
        print("ERROR GENERAL:", e)
        bot.reply_to(message, "Algo falló 😢 intenta de nuevo")

# ==============================
# 🚀 INICIO
# ==============================
bot.remove_webhook()
bot.infinity_polling(timeout=60, long_polling_timeout=60)
