import os
import telebot
import requests
import sqlite3
import threading
import time
from datetime import datetime, timedelta
import re

# ==============================
# 🔑 CONFIGURACIÓN
# ==============================
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
API_KEY   = os.getenv("OPENROUTER_API_KEY")
CHAT_ID   = os.getenv("CHAT_ID")          # Tu chat ID personal (string está bien)
MODEL     = "meta-llama/llama-3.3-70b-instruct:free"  # ✅ modelo free válido en OpenRouter

bot     = telebot.TeleBot(BOT_TOKEN)
db_lock = threading.Lock()

# ==============================
# 🗄️ BASE DE DATOS
# ==============================
# ✅ FIX: cada hilo usa su propia conexión (evita corrupción con check_same_thread)
def get_conn():
    conn = sqlite3.connect("memoria.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db_lock:
        conn = get_conn()
        c    = conn.cursor()
        c.executescript("""
            CREATE TABLE IF NOT EXISTS memoria (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id   INTEGER NOT NULL,
                role      TEXT    NOT NULL,
                content   TEXT    NOT NULL,
                timestamp INTEGER DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS recordatorios (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id  INTEGER NOT NULL,
                mensaje  TEXT    NOT NULL,
                tiempo   INTEGER NOT NULL,
                enviado  INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS datos (
                user_id  INTEGER NOT NULL,
                clave    TEXT    NOT NULL,
                valor    TEXT    NOT NULL,
                PRIMARY KEY (user_id, clave)
            );
        """)
        conn.commit()
        conn.close()

init_db()

# ==============================
# 🧠 MEMORIA DE CONVERSACIÓN
# ==============================
def guardar_mensaje(user_id, role, content):
    try:
        with db_lock:
            conn = get_conn()
            conn.execute(
                "INSERT INTO memoria (user_id, role, content) VALUES (?, ?, ?)",
                (user_id, role, content)
            )
            # Mantener solo los últimos 40 mensajes por usuario
            conn.execute("""
                DELETE FROM memoria WHERE user_id = ? AND id NOT IN (
                    SELECT id FROM memoria WHERE user_id = ? ORDER BY id DESC LIMIT 40
                )
            """, (user_id, user_id))
            conn.commit()
            conn.close()
    except Exception as e:
        enviar_notificacion(f"Error guardando memoria: {e}")

def obtener_historial(user_id, limite=20):
    try:
        with db_lock:
            conn = get_conn()
            filas = conn.execute(
                "SELECT role, content FROM memoria WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, limite)
            ).fetchall()
            conn.close()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(filas)]
    except Exception as e:
        enviar_notificacion(f"Error leyendo memoria: {e}")
        return []

# ==============================
# 💾 GUARDAR / LEER DATOS
# ==============================
def guardar_dato(user_id, clave, valor):
    with db_lock:
        conn = get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO datos (user_id, clave, valor) VALUES (?, ?, ?)",
            (user_id, clave.lower(), valor)
        )
        conn.commit()
        conn.close()

def leer_dato(user_id, clave):
    with db_lock:
        conn = get_conn()
        row = conn.execute(
            "SELECT valor FROM datos WHERE user_id=? AND clave=?",
            (user_id, clave.lower())
        ).fetchone()
        conn.close()
    return row["valor"] if row else None

def listar_datos(user_id):
    with db_lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT clave, valor FROM datos WHERE user_id=?",
            (user_id,)
        ).fetchall()
        conn.close()
    return [(r["clave"], r["valor"]) for r in rows]

def borrar_dato(user_id, clave):
    with db_lock:
        conn = get_conn()
        conn.execute("DELETE FROM datos WHERE user_id=? AND clave=?", (user_id, clave.lower()))
        conn.commit()
        conn.close()

# ==============================
# ⏰ RECORDATORIOS
# ==============================
def crear_recordatorio(user_id, mensaje, tiempo_unix):
    with db_lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO recordatorios (user_id, mensaje, tiempo) VALUES (?, ?, ?)",
            (user_id, mensaje, int(tiempo_unix))
        )
        conn.commit()
        conn.close()

def listar_recordatorios(user_id):
    with db_lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT id, mensaje, tiempo FROM recordatorios WHERE user_id=? AND enviado=0 ORDER BY tiempo",
            (user_id,)
        ).fetchall()
        conn.close()
    return rows

def cancelar_recordatorio(rec_id, user_id):
    with db_lock:
        conn = get_conn()
        conn.execute("DELETE FROM recordatorios WHERE id=? AND user_id=?", (rec_id, user_id))
        conn.commit()
        conn.close()

# Hilo en segundo plano que revisa recordatorios cada 30 segundos
def hilo_recordatorios():
    while True:
        try:
            ahora = int(time.time())
            with db_lock:
                conn = get_conn()
                pendientes = conn.execute(
                    "SELECT id, user_id, mensaje FROM recordatorios WHERE enviado=0 AND tiempo <= ?",
                    (ahora,)
                ).fetchall()
                conn.close()

            for rec in pendientes:
                try:
                    bot.send_message(
                        rec["user_id"],
                        f"⏰ *Recordatorio:*\n{rec['mensaje']}",
                        parse_mode="Markdown"
                    )
                    with db_lock:
                        conn = get_conn()
                        conn.execute("UPDATE recordatorios SET enviado=1 WHERE id=?", (rec["id"],))
                        conn.commit()
                        conn.close()
                except Exception as e:
                    print(f"Error enviando recordatorio {rec['id']}: {e}")
        except Exception as e:
            print(f"Error en hilo_recordatorios: {e}")
        time.sleep(30)

threading.Thread(target=hilo_recordatorios, daemon=True).start()

# Parsea expresiones como "en 10 minutos", "en 2 horas", "en 1 día"
def parsear_tiempo(texto):
    texto = texto.lower().strip()
    ahora = datetime.now()

    patrones = [
        (r"en\s+(\d+)\s+minuto",  "minutes"),
        (r"en\s+(\d+)\s+hora",    "hours"),
        (r"en\s+(\d+)\s+día",     "days"),
        (r"en\s+(\d+)\s+dia",     "days"),
        (r"en\s+(\d+)\s+semana",  "weeks"),
    ]

    for patron, unidad in patrones:
        m = re.search(patron, texto)
        if m:
            n = int(m.group(1))
            delta = {
                "minutes": timedelta(minutes=n),
                "hours":   timedelta(hours=n),
                "days":    timedelta(days=n),
                "weeks":   timedelta(weeks=n),
            }[unidad]
            return (ahora + delta).timestamp()

    # Hora específica: "a las HH:MM"
    m = re.search(r"a\s+las\s+(\d{1,2}):(\d{2})", texto)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        objetivo = ahora.replace(hour=h, minute=mi, second=0, microsecond=0)
        if objetivo <= ahora:
            objetivo += timedelta(days=1)
        return objetivo.timestamp()

    return None

# ==============================
# 🔔 NOTIFICACIONES
# ==============================
def enviar_notificacion(mensaje):
    try:
        url  = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = {"chat_id": CHAT_ID, "text": f"🚨 *ERROR:*\n{mensaje}", "parse_mode": "Markdown"}
        requests.post(url, json=data, timeout=10)
    except Exception as e:
        print("Error enviando notificación:", e)

# ==============================
# 🤖 IA (OpenRouter)
# ==============================
SYSTEM_PROMPT = """Eres un asistente personal conectado a Telegram. Tienes memoria de las conversaciones anteriores.

Capacidades reales que tienes:
- Guardar datos con /guardar <clave> <valor>
- Ver datos guardados con /ver <clave> o /datos
- Crear recordatorios con /recordar <tiempo> <mensaje> (ej: /recordar en 30 minutos Tomar agua)
- Ver recordatorios activos con /recordatorios
- Cancelar recordatorios con /cancelar <id>

Cuando el usuario quiera guardar algo o poner un recordatorio, indícale el comando exacto a usar.
Sé conciso, útil y amigable. Responde siempre en el idioma del usuario."""

def preguntar_ia(user_id, mensaje):
    historial = obtener_historial(user_id)

    # ✅ FIX 1: system va PRIMERO
    # ✅ FIX 2: el mensaje actual se incluye en la llamada
    messages = (
        [{"role": "system", "content": SYSTEM_PROMPT}]
        + historial
        + [{"role": "user", "content": mensaje}]
    )

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type":  "application/json",
        "HTTP-Referer":  "https://github.com/tu-usuario/bot-asistente",  # recomendado por OpenRouter
    }

    data = {
        "model":    MODEL,
        "messages": messages,
        "max_tokens": 800,
    }

    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=data,
            timeout=30
        )

        if response.status_code != 200:
            enviar_notificacion(f"Error API ({response.status_code}): {response.text[:300]}")
            return "Lo siento, hubo un error con la IA 😢"

        resultado = response.json()

        if "choices" not in resultado or not resultado["choices"]:
            enviar_notificacion(f"Respuesta inesperada: {str(resultado)[:300]}")
            return "No pude procesar la respuesta 😢"

        respuesta = resultado["choices"][0]["message"]["content"]

        # ✅ FIX 3: guardar AMBOS mensajes correctamente
        guardar_mensaje(user_id, "user",      mensaje)
        guardar_mensaje(user_id, "assistant", respuesta)

        return respuesta

    except requests.Timeout:
        return "La IA tardó demasiado en responder ⏳ Intenta de nuevo."
    except Exception as e:
        enviar_notificacion(f"Error IA: {e}")
        return "Error conectando con la IA 😢"

# ==============================
# 💬 COMANDOS
# ==============================
@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    texto = (
        "👋 *¡Hola! Soy tu asistente personal.*\n\n"
        "📋 *Comandos disponibles:*\n"
        "• `/recordar en 30 minutos Tomar agua` — crea un recordatorio\n"
        "• `/recordatorios` — ver recordatorios activos\n"
        "• `/cancelar <id>` — cancelar un recordatorio\n"
        "• `/guardar <clave> <valor>` — guardar un dato\n"
        "• `/ver <clave>` — leer un dato guardado\n"
        "• `/datos` — listar todos tus datos\n"
        "• `/borrar <clave>` — eliminar un dato\n"
        "• `/limpiar` — borrar historial de conversación\n\n"
        "O simplemente *escríbeme* y te respondo 🤖"
    )
    bot.reply_to(message, texto, parse_mode="Markdown")

@bot.message_handler(commands=["recordar"])
def cmd_recordar(message):
    # Formato: /recordar en X minutos/horas/días <mensaje>
    partes = message.text.split(None, 1)
    if len(partes) < 2:
        bot.reply_to(message, "📌 Uso: `/recordar en 30 minutos Tomar agua`", parse_mode="Markdown")
        return

    texto_completo = partes[1]

    # Intentar extraer el tiempo del inicio del texto
    tiempo = parsear_tiempo(texto_completo)
    if not tiempo:
        bot.reply_to(
            message,
            "⚠️ No entendí el tiempo. Ejemplos:\n"
            "`/recordar en 10 minutos Reunión`\n"
            "`/recordar en 2 horas Llamar al médico`\n"
            "`/recordar en 1 día Pagar factura`\n"
            "`/recordar a las 18:30 Gym`",
            parse_mode="Markdown"
        )
        return

    # El mensaje es el texto original (el tiempo se interpreta pero el mensaje es el completo)
    crear_recordatorio(message.from_user.id, texto_completo, tiempo)
    hora_str = datetime.fromtimestamp(tiempo).strftime("%d/%m/%Y %H:%M")
    bot.reply_to(message, f"✅ Recordatorio creado para el *{hora_str}*", parse_mode="Markdown")

@bot.message_handler(commands=["recordatorios"])
def cmd_ver_recordatorios(message):
    recs = listar_recordatorios(message.from_user.id)
    if not recs:
        bot.reply_to(message, "📭 No tienes recordatorios activos.")
        return

    lineas = ["📋 *Tus recordatorios activos:*\n"]
    for rec in recs:
        hora = datetime.fromtimestamp(rec["tiempo"]).strftime("%d/%m %H:%M")
        lineas.append(f"• `#{rec['id']}` — {hora}\n  _{rec['mensaje']}_")

    bot.reply_to(message, "\n".join(lineas), parse_mode="Markdown")

@bot.message_handler(commands=["cancelar"])
def cmd_cancelar(message):
    partes = message.text.split()
    if len(partes) < 2 or not partes[1].isdigit():
        bot.reply_to(message, "Uso: `/cancelar <id>` (usa /recordatorios para ver los IDs)", parse_mode="Markdown")
        return
    cancelar_recordatorio(int(partes[1]), message.from_user.id)
    bot.reply_to(message, f"🗑️ Recordatorio #{partes[1]} cancelado.")

@bot.message_handler(commands=["guardar"])
def cmd_guardar(message):
    partes = message.text.split(None, 2)
    if len(partes) < 3:
        bot.reply_to(message, "Uso: `/guardar <clave> <valor>`\nEjemplo: `/guardar cumple_mama 15 de marzo`", parse_mode="Markdown")
        return
    _, clave, valor = partes
    guardar_dato(message.from_user.id, clave, valor)
    bot.reply_to(message, f"✅ Guardado: *{clave}* = `{valor}`", parse_mode="Markdown")

@bot.message_handler(commands=["ver"])
def cmd_ver(message):
    partes = message.text.split(None, 1)
    if len(partes) < 2:
        bot.reply_to(message, "Uso: `/ver <clave>`", parse_mode="Markdown")
        return
    clave = partes[1].strip()
    valor = leer_dato(message.from_user.id, clave)
    if valor:
        bot.reply_to(message, f"📌 *{clave}*: `{valor}`", parse_mode="Markdown")
    else:
        bot.reply_to(message, f"❌ No encontré nada guardado con la clave `{clave}`", parse_mode="Markdown")

@bot.message_handler(commands=["datos"])
def cmd_datos(message):
    datos = listar_datos(message.from_user.id)
    if not datos:
        bot.reply_to(message, "📭 No tienes datos guardados.\nUsa `/guardar <clave> <valor>` para guardar algo.", parse_mode="Markdown")
        return
    lineas = ["📦 *Tus datos guardados:*\n"]
    for clave, valor in datos:
        lineas.append(f"• *{clave}*: `{valor}`")
    bot.reply_to(message, "\n".join(lineas), parse_mode="Markdown")

@bot.message_handler(commands=["borrar"])
def cmd_borrar(message):
    partes = message.text.split(None, 1)
    if len(partes) < 2:
        bot.reply_to(message, "Uso: `/borrar <clave>`", parse_mode="Markdown")
        return
    clave = partes[1].strip()
    borrar_dato(message.from_user.id, clave)
    bot.reply_to(message, f"🗑️ Dato `{clave}` eliminado.", parse_mode="Markdown")

@bot.message_handler(commands=["limpiar"])
def cmd_limpiar(message):
    with db_lock:
        conn = get_conn()
        conn.execute("DELETE FROM memoria WHERE user_id=?", (message.from_user.id,))
        conn.commit()
        conn.close()
    bot.reply_to(message, "🧹 Historial de conversación borrado.")

# ==============================
# 💬 MENSAJES DE TEXTO LIBRES
# ==============================
@bot.message_handler(func=lambda m: True)
def responder(message):
    try:
        bot.send_chat_action(message.chat.id, "typing")  # muestra "escribiendo..."
        respuesta = preguntar_ia(message.from_user.id, message.text)
        bot.reply_to(message, respuesta)
    except Exception as e:
        enviar_notificacion(f"Error general: {e}")
        bot.reply_to(message, "Algo falló 😢 Intenta de nuevo.")

# ==============================
# 🚀 INICIO
# ==============================
if __name__ == "__main__":
    print("🤖 Bot iniciando...")
    bot.remove_webhook()
    time.sleep(1)
    bot.infinity_polling(timeout=30, long_polling_timeout=30)
