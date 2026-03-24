import os
import telebot
import requests
import sqlite3
import threading
import time

# ==============================
# 🔑 CONFIGURACIÓN
# ==============================
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
API_KEY = os.getenv("OPENROUTER_API_KEY")

# ⚠️ TU CHAT ID PERSONAL (obtenlo hablando con @userinfobot)
CHAT_ID = os.getenv("CHAT_ID")

bot = telebot.TeleBot(BOT_TOKEN)
db_lock = threading.Lock()

# ==============================
# 🗄️ BASE DE DATOS
# ==============================
conn = sqlite3.connect("memoria.db", check_same_thread=False)
cursor = conn.cursor()

with db_lock:
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS memoria (
        user_id INTEGER,
        role TEXT,
        content TEXT
    )
    """)
    conn.commit()

# ==============================
# 🧠 MEMORIA (ARREGLADA)
# ==============================
def guardar_mensaje(user_id, role, content):
    try:
        with db_lock:
            cursor.execute(
                "INSERT INTO memoria (user_id, role, content) VALUES (?, ?, ?)",
                (user_id, role, content)
            )
            conn.commit()  # 🔥 clave: asegura persistencia real
    except Exception as e:
        enviar_notificacion(f"Error guardando memoria: {e}")

def obtener_historial(user_id):
    try:
        with db_lock:
            cursor.execute(
                "SELECT role, content FROM memoria WHERE user_id=? ORDER BY ROWID DESC LIMIT 10",
                (user_id,)
            )
            filas = cursor.fetchall()

        return [{"role": r, "content": c} for r, c in reversed(filas)]

    except Exception as e:
        enviar_notificacion(f"Error leyendo memoria: {e}")
        return []

# ==============================
# 🔔 NOTIFICACIONES TELEGRAM
# ==============================
def enviar_notificacion(mensaje):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = {
            "chat_id": CHAT_ID,
            "text": f"🚨 NOTIFICACIÓN:\n{mensaje}"
        }
        requests.post(url, json=data)
    except Exception as e:
        print("Error enviando notificación:", e)

# ==============================
# 🤖 IA
# ==============================
def preguntar_ia(user_id, mensaje):
    url = "https://openrouter.ai/api/v1/chat/completions"

    historial = obtener_historial(user_id)

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }

    data = {
        "model": "openrouter/free",
        "messages": historial + [
           {"role": "system", "content": 
"Eres un asistente personal REAL conectado a Telegram. "
"PUEDES enviar recordatorios, notificaciones y actuar fuera del chat. "
"NUNCA digas que no puedes hacer acciones externas. "
"Cuando el usuario pida recordatorios o alertas, responde normalmente como si puedes hacerlo."
}
        ]
    }

    try:
        response = requests.post(url, headers=headers, json=data, timeout=20)

        if response.status_code != 200:
            enviar_notificacion(f"Error API: {response.text}")
            return "Error con la IA 😢"

        resultado = response.json()

        if "choices" not in resultado:
            enviar_notificacion("Respuesta inválida IA")
            return "Error procesando IA 😢"

        respuesta = resultado["choices"][0]["message"]["content"]

        # 🔥 GUARDADO CORRECTO
        guardar_mensaje(user_id, "user", mensaje)
        guardar_mensaje(user_id, "assistant", respuesta)

        return respuesta

    except Exception as e:
        enviar_notificacion(f"Error IA: {e}")
        return "Error conectando con IA 😢"

# ==============================
# 💬 MENSAJES
# ==============================
@bot.message_handler(func=lambda message: True)
def responder(message):
    try:
        user_id = message.from_user.id
        texto = message.text

        respuesta = preguntar_ia(user_id, texto)

        bot.reply_to(message, respuesta)

    except Exception as e:
        enviar_notificacion(f"Error general: {e}")
        bot.reply_to(message, "Algo falló 😢")

# ==============================
# 🚀 INICIO
# ==============================
bot.remove_webhook()
bot.infinity_polling()
