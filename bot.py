import os
import telebot
import requests
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import re

# ==============================
# 🔑 CONFIGURACIÓN
# ==============================
BOT_TOKEN  = os.getenv("TELEGRAM_TOKEN")
API_KEY    = os.getenv("OPENROUTER_API_KEY")
CHAT_ID    = os.getenv("CHAT_ID")
MODEL      = "openrouter/free"
API_SECRET = os.getenv("API_SECRET", "super-secret-123") # Para Webhooks de integraciones (Zapier, Make)

# ✅ Railway expone la URL pública del servicio en esta variable automáticamente
WEBHOOK_URL = os.getenv("RAILWAY_PUBLIC_DOMAIN")

bot     = telebot.TeleBot(BOT_TOKEN)
app     = Flask(__name__)
db_lock = threading.Lock()

# ==============================
# 🗄️ BASE DE DATOS
# ==============================
def get_conn():
    conn = sqlite3.connect("memoria.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db_lock:
        conn = get_conn()
        conn.executescript("""
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
            
            -- [NUEVO] Tareas y proyectos
            CREATE TABLE IF NOT EXISTS proyectos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                nombre TEXT NOT NULL,
                estado TEXT DEFAULT 'Activo'
            );
            CREATE TABLE IF NOT EXISTS tareas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                proyecto_id INTEGER,
                descripcion TEXT NOT NULL,
                estado TEXT DEFAULT 'Pendiente',
                fecha_limite INTEGER
            );
            
            -- [NUEVO] Finanzas
            CREATE TABLE IF NOT EXISTS transacciones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                tipo TEXT NOT NULL,
                monto REAL NOT NULL,
                categoria TEXT,
                descripcion TEXT,
                fecha INTEGER DEFAULT (strftime('%s','now'))
            );

            -- [NUEVO] CRM
            CREATE TABLE IF NOT EXISTS clientes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                nombre TEXT NOT NULL,
                empresa TEXT,
                contacto TEXT,
                notas TEXT,
                estado_pipeline TEXT DEFAULT 'Lead'
            );

            -- [NUEVO] Notas Avanzadas
            CREATE TABLE IF NOT EXISTS notas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                titulo TEXT NOT NULL,
                contenido TEXT,
                fecha_creacion INTEGER DEFAULT (strftime('%s','now'))
            );

            -- [NUEVO] Eventos / Agenda
            CREATE TABLE IF NOT EXISTS eventos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                titulo TEXT NOT NULL,
                fecha_inicio INTEGER,
                fecha_fin INTEGER
            );
        """)
        conn.commit()
        conn.close()

init_db()

# ==============================
# 🔨 FUNCIONES AUXILIARES BD
# ==============================
def db_read(query, args=()):
    with db_lock:
        conn = get_conn()
        res = conn.execute(query, args).fetchall()
        conn.close()
    return res

def db_write(query, args=()):
    with db_lock:
        conn = get_conn()
        cursor = conn.execute(query, args)
        conn.commit()
        last_id = cursor.lastrowid
        conn.close()
    return last_id

# ==============================
# 🧠 MEMORIA DE CONVERSACIÓN
# ==============================
def guardar_mensaje(user_id, role, content):
    try:
        db_write("INSERT INTO memoria (user_id, role, content) VALUES (?, ?, ?)", (user_id, role, content))
        db_write("""
            DELETE FROM memoria WHERE user_id = ? AND id NOT IN (
                SELECT id FROM memoria WHERE user_id = ? ORDER BY id DESC LIMIT 40
            )""", (user_id, user_id))
    except Exception as e:
        enviar_notificacion(f"Error guardando memoria: {e}")

def obtener_historial(user_id, limite=20):
    try:
        filas = db_read("SELECT role, content FROM memoria WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limite))
        return [{"role": r["role"], "content": r["content"]} for r in reversed(filas)]
    except Exception as e:
        enviar_notificacion(f"Error leyendo memoria: {e}")
        return []

# ==============================
# ⏰ RECORDATORIOS Y TIEMPO
# ==============================
def crear_recordatorio(user_id, mensaje, tiempo_unix):
    db_write("INSERT INTO recordatorios (user_id, mensaje, tiempo) VALUES (?, ?, ?)", (user_id, mensaje, int(tiempo_unix)))

def listar_recordatorios(user_id):
    return db_read("SELECT id, mensaje, tiempo FROM recordatorios WHERE user_id=? AND enviado=0 ORDER BY tiempo", (user_id,))

def hilo_recordatorios():
    while True:
        try:
            ahora = int(time.time())
            pendientes = db_read("SELECT id, user_id, mensaje FROM recordatorios WHERE enviado=0 AND tiempo <= ?", (ahora,))
            for rec in pendientes:
                try:
                    bot.send_message(rec["user_id"], f"⏰ *Recordatorio:*\n{rec['mensaje']}", parse_mode="Markdown")
                    db_write("UPDATE recordatorios SET enviado=1 WHERE id=?", (rec["id"],))
                except Exception as e:
                    print(f"Error enviando recordatorio {rec['id']}: {e}")
        except Exception as e:
            print(f"Error en hilo_recordatorios: {e}")
        time.sleep(30)

threading.Thread(target=hilo_recordatorios, daemon=True).start()

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
            delta = {"minutes": timedelta(minutes=n), "hours": timedelta(hours=n),
                     "days": timedelta(days=n), "weeks": timedelta(weeks=n)}[unidad]
            return (ahora + delta).timestamp()
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
        data = {"chat_id": CHAT_ID, "text": f"🚨 *NOTIFICACIÓN DEL SISTEMA:*\n{mensaje}", "parse_mode": "Markdown"}
        requests.post(url, json=data, timeout=10)
    except Exception as e:
        print("Error enviando notificación:", e)

# ==============================
# 🤖 IA (OpenRouter)
# ==============================
SYSTEM_PROMPT = """Eres el asistente personal avanzado nivel empresarial del usuario conectado vía Telegram. Tienes memoria e interacciones pasadas del usuario.

NUEVAS CAPACIDADES EMPRESARIALES:
1. Gestionar Proyectos y Tareas: Puedes indicar comandos como /proyecto_nuevo <nombre>, /tarea_nueva <proy_id> <desc>, /tareas <proy_id>.
2. Finanzas: Puedes registrar ingresos o gastos: /gasto <monto> <cat> <desc>, /ingreso <monto> <cat> <desc>, o consultar /finanzas.
3. CRM y Clientes: Para manejar prospectos: /cliente_nuevo <nombre> | <empresa> | <contacto>, /clientes.
4. Notas Largas: /nota_nueva <titulo> | <contenido>
5. Agenda: /evento_nuevo <título> | DD/MM HH:MM, o /agenda
6. Recordatorios normales: /recordar en X tiempo.

Si el usuario te pide que registres algo (ej. "Anota que gasté 100 en comida", "Añade el proyecto Alpha", "Registra al cliente Google"), indícale el comando exacto que debe usar, y dáselo formateado para que sea fácil de copiar y pegar en Telegram. Sé profesional pero cálido."""

def preguntar_ia(user_id, mensaje):
    historial = obtener_historial(user_id)
    messages  = ([{"role": "system", "content": SYSTEM_PROMPT}] + historial + [{"role": "user", "content": mensaje}])
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type":  "application/json"}
    data = {"model": MODEL, "messages": messages, "max_tokens": 800}
    try:
        response = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=data, timeout=30)
        if response.status_code != 200:
            return "Lo siento, hubo un error con la IA 😢"
        respuesta = response.json()["choices"][0]["message"]["content"]
        guardar_mensaje(user_id, "user", mensaje)
        guardar_mensaje(user_id, "assistant", respuesta)
        return respuesta
    except Exception as e:
        print("Error conectando con IA:", e)
        return "Error conectando con la IA 😢"

# ==============================
# 💬 COMANDOS BASICOS
# ==============================
@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    texto = (
        "👋 *¡Sistema Empresarial Iniciado!*\n"
        "Soy tu bot asistente con suites corporativas.\n\n"
        "📋 *MÓDULOS DISPONIBLES:*\n"
        "🎯 *Proyectos:* `/proyectos`, `/proyecto_nuevo`, `/tareas`, `/tarea_nueva`, `/tarea_completar`\n"
        "💰 *Finanzas:* `/finanzas`, `/gasto`, `/ingreso`\n"
        "🤝 *CRM:* `/clientes`, `/cliente_nuevo`, `/cliente_ver`\n"
        "📅 *Agenda:* `/agenda`, `/evento_nuevo`, `/recordar`, `/recordatorios`\n"
        "📝 *Notas:* `/notas`, `/nota_nueva`, `/nota_ver`\n\n"
        "Escríbele a la IA libremente para que te dicte los comandos si no los recuerdas."
    )
    bot.reply_to(message, texto, parse_mode="Markdown")

@bot.message_handler(commands=["recordar"])
def cmd_recordar(message):
    partes = message.text.split(None, 1)
    if len(partes) < 2: return bot.reply_to(message, "📌 Uso: `/recordar en 30 minutos Tomar agua`", parse_mode="Markdown")
    tiempo = parsear_tiempo(partes[1])
    if not tiempo: return bot.reply_to(message, "⚠️ No entendí el formato de tiempo.", parse_mode="Markdown")
    crear_recordatorio(message.from_user.id, partes[1], tiempo)
    hora_str = datetime.fromtimestamp(tiempo).strftime("%d/%m %H:%M")
    bot.reply_to(message, f"✅ Recordatorio creado para *{hora_str}*", parse_mode="Markdown")

# ==============================
# 🎯 MÓDULO: TAREAS Y PROYECTOS
# ==============================
@bot.message_handler(commands=["proyecto_nuevo"])
def cmd_proyecto_nuevo(message):
    nombre = message.text.replace("/proyecto_nuevo", "").strip()
    if not nombre: return bot.reply_to(message, "⚠️ Falta el nombre: `/proyecto_nuevo Mi Proyecto`", parse_mode="Markdown")
    pid = db_write("INSERT INTO proyectos (user_id, nombre) VALUES (?, ?)", (message.from_user.id, nombre))
    bot.reply_to(message, f"✅ Proyecto creado (ID: `{pid}`): *{nombre}*", parse_mode="Markdown")

@bot.message_handler(commands=["proyectos"])
def cmd_proyectos(message):
    proys = db_read("SELECT id, nombre, estado FROM proyectos WHERE user_id=?", (message.from_user.id,))
    if not proys: return bot.reply_to(message, "No tienes proyectos activos.")
    texto = "🎯 *TUS PROYECTOS*\n\n" + "\n".join([f"`[{p['id']}]` {p['nombre']} ({p['estado']})" for p in proys])
    bot.reply_to(message, texto, parse_mode="Markdown")

@bot.message_handler(commands=["tarea_nueva"])
def cmd_tarea_nueva(message):
    partes = message.text.replace("/tarea_nueva", "").strip().split(None, 1)
    if len(partes) < 2 or not partes[0].isdigit():
        return bot.reply_to(message, "⚠️ Uso: `/tarea_nueva <ID_Proyecto> <Descripción>`", parse_mode="Markdown")
    pid, desc = partes[0], partes[1]
    tid = db_write("INSERT INTO tareas (user_id, proyecto_id, descripcion) VALUES (?, ?, ?)", (message.from_user.id, pid, desc))
    bot.reply_to(message, f"✅ Tarea creada (ID: `{tid}`) para el proyecto `{pid}`", parse_mode="Markdown")

@bot.message_handler(commands=["tareas"])
def cmd_tareas(message):
    pid = message.text.replace("/tareas", "").strip()
    if not pid.isdigit(): return bot.reply_to(message, "⚠️ Indica el ID: `/tareas <ID_Proyecto>`", parse_mode="Markdown")
    tareas = db_read("SELECT id, descripcion, estado FROM tareas WHERE user_id=? AND proyecto_id=?", (message.from_user.id, pid))
    if not tareas: return bot.reply_to(message, "No hay tareas.")
    texto = f"📋 *TAREAS DEL PROYECTO {pid}*\n\n" + "\n".join([f"`[{t['id']}]` {t['descripcion']} - _{t['estado']}_" for t in tareas])
    bot.reply_to(message, texto, parse_mode="Markdown")

@bot.message_handler(commands=["tarea_completar"])
def cmd_tarea_completar(message):
    tid = message.text.replace("/tarea_completar", "").strip()
    if not tid.isdigit(): return bot.reply_to(message, "⚠️ Indica el ID: `/tarea_completar <ID_Tarea>`", parse_mode="Markdown")
    db_write("UPDATE tareas SET estado='Completada' WHERE id=? AND user_id=?", (tid, message.from_user.id))
    bot.reply_to(message, f"🎯 Tarea `{tid}` completada con éxito.")

# ==============================
# 💰 MÓDULO: FINANZAS
# ==============================
@bot.message_handler(commands=["gasto"])
def cmd_gasto(message):
    partes = message.text.replace("/gasto", "").strip().split(None, 2)
    if len(partes) < 3: return bot.reply_to(message, "Uso: `/gasto <monto> <categoría> <descripción>`", parse_mode="Markdown")
    monto, cat, desc = partes
    db_write("INSERT INTO transacciones (user_id, tipo, monto, categoria, descripcion) VALUES (?, 'gasto', ?, ?, ?)", (message.from_user.id, float(monto.replace(',','.')), cat, desc))
    bot.reply_to(message, f"💸 *Gasto anotado!* - {monto} en {cat}", parse_mode="Markdown")

@bot.message_handler(commands=["ingreso"])
def cmd_ingreso(message):
    partes = message.text.replace("/ingreso", "").strip().split(None, 2)
    if len(partes) < 3: return bot.reply_to(message, "Uso: `/ingreso <monto> <categoría> <descripción>`", parse_mode="Markdown")
    monto, cat, desc = partes
    db_write("INSERT INTO transacciones (user_id, tipo, monto, categoria, descripcion) VALUES (?, 'ingreso', ?, ?, ?)", (message.from_user.id, float(monto.replace(',','.')), cat, desc))
    bot.reply_to(message, f"💵 *Ingreso anotado!* + {monto} en {cat}", parse_mode="Markdown")

@bot.message_handler(commands=["finanzas"])
def cmd_finanzas(message):
    gastos = sum([t['monto'] for t in db_read("SELECT monto FROM transacciones WHERE tipo='gasto' AND user_id=?", (message.from_user.id,))])
    ingresos = sum([t['monto'] for t in db_read("SELECT monto FROM transacciones WHERE tipo='ingreso' AND user_id=?", (message.from_user.id,))])
    bot.reply_to(message, f"📊 *Resumen Financiero*\n\n📈 Ingresos: `${ingresos}`\n📉 Gastos: `${gastos}`\n💰 Balance Total: `${ingresos - gastos}`", parse_mode="Markdown")

# ==============================
# 🤝 MÓDULO: CRM CLIENTES
# ==============================
@bot.message_handler(commands=["cliente_nuevo"])
def cmd_cliente_nuevo(message):
    datos = message.text.replace("/cliente_nuevo", "").split("|")
    if len(datos) < 3: return bot.reply_to(message, "Uso: `/cliente_nuevo Juan | ACME Corp | juan@acme.com`", parse_mode="Markdown")
    nombre, emp, cont = [d.strip() for d in datos]
    cid = db_write("INSERT INTO clientes (user_id, nombre, empresa, contacto) VALUES (?, ?, ?, ?)", (message.from_user.id, nombre, emp, cont))
    bot.reply_to(message, f"🤝 *Cliente Creado* (ID: `{cid}`)\n{nombre} de {emp}", parse_mode="Markdown")

@bot.message_handler(commands=["clientes"])
def cmd_clientes(message):
    clientes = db_read("SELECT id, nombre, empresa, estado_pipeline FROM clientes WHERE user_id=?", (message.from_user.id,))
    if not clientes: return bot.reply_to(message, "No hay clientes en el CRM.")
    texto = "🤝 *CRM - CLIENTES*\n" + "\n".join([f"`[{c['id']}]` *{c['nombre']}* ({c['empresa']}) - Estado: _{c['estado_pipeline']}_" for c in clientes])
    bot.reply_to(message, texto, parse_mode="Markdown")

# ==============================
# 📝 MÓDULO: NOTAS AVANZADAS
# ==============================
@bot.message_handler(commands=["nota_nueva"])
def cmd_nota_nueva(message):
    datos = message.text.replace("/nota_nueva", "").split("|", 1)
    if len(datos) < 2: return bot.reply_to(message, "Uso: `/nota_nueva Titulo | Contenido largo de la nota`", parse_mode="Markdown")
    tit, cont = [d.strip() for d in datos]
    nid = db_write("INSERT INTO notas (user_id, titulo, contenido) VALUES (?, ?, ?)", (message.from_user.id, tit, cont))
    bot.reply_to(message, f"📝 *Nota creada* (ID: `{nid}`) - {tit}", parse_mode="Markdown")

@bot.message_handler(commands=["notas"])
def cmd_notas(message):
    notas = db_read("SELECT id, titulo, fecha_creacion FROM notas WHERE user_id=?", (message.from_user.id,))
    if not notas: return bot.reply_to(message, "No tienes notas.")
    texto = "📝 *TUS NOTAS*\n\n" + "\n".join([f"`[{n['id']}]` - {n['titulo']}" for n in notas])
    bot.reply_to(message, texto, parse_mode="Markdown")

@bot.message_handler(commands=["nota_ver"])
def cmd_nota_ver(message):
    nid = message.text.replace("/nota_ver", "").strip()
    if not nid.isdigit(): return bot.reply_to(message, "⚠️ Uso: `/nota_ver <ID>`", parse_mode="Markdown")
    nota = db_read("SELECT titulo, contenido FROM notas WHERE id=? AND user_id=?", (nid, message.from_user.id))
    if not nota: return bot.reply_to(message, "Nota no encontrada.")
    bot.reply_to(message, f"📓 *{nota[0]['titulo']}*\n\n{nota[0]['contenido']}", parse_mode="Markdown")

# ==============================
# 📅 MÓDULO: EVENTOS / AGENDA
# ==============================
@bot.message_handler(commands=["evento_nuevo"])
def cmd_evento_nuevo(message):
    datos = message.text.replace("/evento_nuevo", "").split("|")
    if len(datos) < 2: return bot.reply_to(message, "Uso: `/evento_nuevo Junta Directiva | 25/11 15:30`", parse_mode="Markdown")
    tit, dt = datos[0].strip(), datos[1].strip()
    try:
        ts = int(datetime.strptime(dt, "%d/%m %H:%M").replace(year=datetime.now().year).timestamp())
        eid = db_write("INSERT INTO eventos (user_id, titulo, fecha_inicio) VALUES (?, ?, ?)", (message.from_user.id, tit, ts))
        bot.reply_to(message, f"📅 Evento guardado (ID: `{eid}`)", parse_mode="Markdown")
    except ValueError:
        bot.reply_to(message, "⚠️ Formato de fecha inválido. Usa: `DD/MM HH:MM`", parse_mode="Markdown")

@bot.message_handler(commands=["agenda"])
def cmd_agenda(message):
    eventos = db_read("SELECT id, titulo, fecha_inicio FROM eventos WHERE user_id=? ORDER BY fecha_inicio ASC", (message.from_user.id,))
    if not eventos: return bot.reply_to(message, "Agenda vacía.")
    texto = "📅 *PRÓXIMOS EVENTOS*\n\n" + "\n".join([f"• *{datetime.fromtimestamp(e['fecha_inicio']).strftime('%d/%m %H:%M')}* - {e['titulo']}" for e in eventos if e['fecha_inicio']])
    bot.reply_to(message, texto, parse_mode="Markdown")


# ==============================
# 💬 IA - RESPUESTA GLOBAL
# ==============================
@bot.message_handler(func=lambda m: True)
def responder(message):
    try:
        bot.send_chat_action(message.chat.id, "typing")
        respuesta = preguntar_ia(message.from_user.id, message.text)
        bot.reply_to(message, respuesta)
    except Exception as e:
        enviar_notificacion(f"Error general: {e}")
        bot.reply_to(message, "Algo falló 😢 Intenta de nuevo.")

# ==============================
# 🌐 WEBHOOK (Flask) y API INTEGRACIÓN
# ==============================
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    json_data = request.get_json()
    update    = telebot.types.Update.de_json(json_data)
    bot.process_new_updates([update])
    return "OK", 200

@app.route("/", methods=["GET"])
def health():
    return "Bot Activo y Modulos Empresariales en linea ✅", 200

@app.route("/api/notificar", methods=["POST"])
def api_notificar():
    """Endpoint para llamadas externas desde Zapier, Make, IFTTT, páginas web"""
    auth = request.headers.get("Authorization")
    if not auth or auth != f"Bearer {API_SECRET}":
        return jsonify({"error": "No autorizado"}), 401
    
    data = request.get_json()
    mensaje = data.get("mensaje", "Ningún mensaje provisto")
    usuario_destino = data.get("user_id", CHAT_ID) # Usa CHAT_ID del env si es general
    
    try:
        if usuario_destino:
            bot.send_message(usuario_destino, f"🔌 *Integración Externa:*\n{mensaje}", parse_mode="Markdown")
            return jsonify({"status": "enviado"}), 200
        else:
            return jsonify({"error": "CHAT_ID no configurado y user_id no provisto"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ==============================
# 🚀 INICIO
# ==============================
if __name__ == "__main__":
    print("🤖 Bot iniciando con webhook empresarial...")
    webhook_url = f"https://{WEBHOOK_URL}/{BOT_TOKEN}"
    bot.remove_webhook()
    time.sleep(1)
    bot.set_webhook(url=webhook_url)
    print(f"✅ Webhook registrado: {webhook_url}")
    print(f"🔌 API Integraciones Webhook: https://{WEBHOOK_URL}/api/notificar (Req Bearer {API_SECRET})")

    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
