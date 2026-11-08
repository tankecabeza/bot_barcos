import os
import re
import html
import json
import time
import uuid
import base64
import sqlite3
import smtplib
import threading
import logging
import urllib3
from email.message import EmailMessage
from zoneinfo import ZoneInfo
from datetime import datetime
import requests
from bs4 import BeautifulSoup
import telebot
from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    BotCommand, MenuButtonCommands
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==============================================================================
# CONFIGURACIÓN (desde variables de entorno, NO escribas claves en el código)
# ==============================================================================
def _cargar_env():
    """Lee el archivo .env (junto a este script) y lo vuelca en las variables de entorno."""
    ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(ruta):
        return
    with open(ruta, encoding="utf-8-sig") as f:
        for linea in f:
            linea = linea.strip()
            if not linea or linea.startswith("#") or "=" not in linea:
                continue
            clave, valor = linea.split("=", 1)
            os.environ.setdefault(clave.strip(), valor.strip().strip('"').strip("'"))

_cargar_env()

for _v in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
    if not os.environ.get(_v):
        raise SystemExit(f"Falta {_v}. Crea un archivo .env junto a bot.py (mira .env.example).")

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
EMAIL_DESTINO = os.environ.get("EMAIL_DESTINO") or "aberlanga@guardiacivil.es"
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
MAIL_PAGE_URL = os.environ.get("MAIL_PAGE_URL", "").strip()

WEB_URL = "https://www.puertomalaga.com/es/informacion-atraques/"
AJAX_URL = "https://www.puertomalaga.com/wp-admin/admin-ajax.php"
DATA_DIR = os.environ.get("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(DATA_DIR, "atraques_malaga_limpio.db")
TIP_POR_DEFECTO = (os.environ.get("TIP_POR_DEFECTO") or "F10173Y").strip().upper()
# Si vale 1, no pregunta el TIP: usa siempre el predefinido
TIP_AUTOMATICO = os.environ.get("TIP_AUTOMATICO", "0") == "1"
INTERVALO_COMPROBACION = 600  # segundos (600 = cada 10 minutos)
# Opcionales (para ejecutarlo en GitHub Actions por turnos):
DURACION_MAX_MIN = int(os.environ.get("DURACION_MAX_MIN", "0"))  # 0 = sin límite
PRIMERA_PASADA_SILENCIOSA = os.environ.get("PRIMERA_PASADA_SILENCIOSA", "0") == "1"

# Los servidores en la nube van en UTC: forzamos hora de Madrid
TZ = ZoneInfo("Europe/Madrid")

def ahora():
    return datetime.now(TZ)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
bot = telebot.TeleBot(TELEGRAM_TOKEN)

sesiones_sigo = {}
correos_pendientes = {}  # token -> {"asunto":..., "cuerpo":...}

# ==============================================================================
# 0. MENÚ DE COMANDOS DEL BOT (el botón "Menú" junto al cuadro de texto)
# ==============================================================================
def configurar_menu_bot():
    try:
        bot.set_my_commands([
            BotCommand("start", "🏠 Inicio y ayuda"),
            BotCommand("barcos", "🚢 Barcos de hoy en el puerto"),
            BotCommand("ayer", "📅 Barcos de ayer"),
            BotCommand("manana", "📅 Barcos de mañana"),
            BotCommand("ayuda", "❓ Cómo usar el bot"),
            BotCommand("cancelar", "❌ Cancelar el Hecho SIGO en curso"),
        ])
        bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        logging.info("Menú de comandos configurado.")
    except Exception as e:
        logging.error(f"No se pudo configurar el menú: {e}")

# ==============================================================================
# 1. BASE DE DATOS LOCAL (SQLITE)
# ==============================================================================
def inicializar_bd():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS barcos_puerto (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            buque TEXT,
            bandera TEXT,
            gt TEXT,
            eslora TEXT,
            consignatario TEXT,
            entrada TEXT,
            muelle TEXT,
            procedencia TEXT,
            destino TEXT,
            fecha_consulta TEXT,
            vesselfinder_url TEXT,
            UNIQUE(buque, entrada, muelle)
        )
    """)
    conn.commit()
    conn.close()

def guardar_barco_bd(datos):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR IGNORE INTO barcos_puerto
        (buque, bandera, gt, eslora, consignatario, entrada, muelle, procedencia, destino, fecha_consulta, vesselfinder_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datos["buque"], datos["bandera"], datos["gt"], datos["eslora"],
        datos["consignatario"], datos["entrada"], datos["muelle"],
        datos["procedencia"], datos["destino"], datos["fecha"], datos["vesselfinder_url"]
    ))
    conn.commit()
    cursor.execute(
        "SELECT id FROM barcos_puerto WHERE buque = ? AND entrada = ? AND muelle = ?",
        (datos["buque"], datos["entrada"], datos["muelle"])
    )
    fila = cursor.fetchone()
    barco_id = fila[0] if fila else None
    conn.close()
    return barco_id

def barco_ya_registrado(buque, entrada, muelle):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT 1 FROM barcos_puerto WHERE buque = ? AND entrada = ? AND muelle = ?",
        (buque, entrada, muelle)
    )
    existe = cursor.fetchone() is not None
    conn.close()
    return existe

def obtener_barco_por_id(barco_id):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM barcos_puerto WHERE id = ?", (barco_id,))
    fila = cursor.fetchone()
    conn.close()
    return dict(fila) if fila else None

# ==============================================================================
# 2. CONEXIÓN DIRECTA A LA API AJAX DEL PUERTO DE MÁLAGA
# ==============================================================================
def limpiar_celda(html_str):
    if not html_str:
        return []
    texto = re.sub(r"<\s*/?\s*br\s*/?\s*>", "|||", str(html_str), flags=re.IGNORECASE)
    texto_limpio = BeautifulSoup(texto, "html.parser").get_text()
    return [parte.strip() for parte in texto_limpio.split("|||") if parte.strip()]

def extraer_barcos_puerto(fecha_str=None):
    if not fecha_str:
        fecha_str = ahora().strftime("%d/%m/%Y")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Referer": WEB_URL,
        "X-Requested-With": "XMLHttpRequest"
    }
    payload = {"action": "get_atraques_by_date", "date": fecha_str}

    lista_barcos = []
    try:
        resp = requests.post(AJAX_URL, headers=headers, data=payload, verify=False, timeout=15)
        resp.raise_for_status()
        datos_json = json.loads(resp.text.strip())

        if not isinstance(datos_json, list) or len(datos_json) <= 1:
            return lista_barcos

        for fila in datos_json[1:]:
            if not isinstance(fila, list) or len(fila) < 6:
                continue

            col_buque = limpiar_celda(fila[0])
            buque = col_buque[0].upper() if len(col_buque) > 0 else "DESCONOCIDO"
            bandera = col_buque[1].upper() if len(col_buque) > 1 else "DESCONOCIDA"

            col_gt = limpiar_celda(fila[1])
            gt = col_gt[0] if len(col_gt) > 0 else "N/A"
            eslora = col_gt[-1] if len(col_gt) > 1 else "N/A"

            col_consig = limpiar_celda(fila[2])
            consignatario = " ".join(col_consig).upper() if col_consig else "DESCONOCIDO"

            col_entrada = limpiar_celda(fila[3])
            entrada = " a las ".join(col_entrada) if len(col_entrada) > 1 else (" ".join(col_entrada) if col_entrada else fecha_str)

            col_atraque = limpiar_celda(fila[4])
            muelle = " ".join(col_atraque) if col_atraque else "SIN ESPECIFICAR"

            col_ruta = limpiar_celda(fila[5])
            procedencia = col_ruta[0].upper() if len(col_ruta) > 0 else "DESCONOCIDO"
            destino = col_ruta[1].upper() if len(col_ruta) > 1 else (col_ruta[0].upper() if len(col_ruta) == 1 else "DESCONOCIDO")

            url_vf = f"https://www.vesselfinder.com/vessels?name={buque.replace(' ', '%20')}"

            lista_barcos.append({
                "buque": buque, "bandera": bandera, "gt": gt, "eslora": eslora,
                "consignatario": consignatario, "entrada": entrada, "muelle": muelle,
                "procedencia": procedencia, "destino": destino,
                "fecha": fecha_str, "vesselfinder_url": url_vf
            })

    except Exception as e:
        logging.error(f"Error consultando API AJAX del Puerto de Málaga: {e}")

    return lista_barcos

# ==============================================================================
# 3. NOTIFICACIÓN DE BARCO EN TELEGRAM
# ==============================================================================
def enviar_alerta_nuevo_barco(barco_id, datos, chat_id=TELEGRAM_CHAT_ID):
    texto = (
        f"🚢 <b>BUQUE EN PUERTO DE MÁLAGA</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔹 <b>Buque:</b> <code>{html.escape(datos['buque'])}</code>\n"
        f"🏳️ <b>Bandera:</b> {html.escape(datos['bandera'])}\n"
        f"🏢 <b>Consignatario:</b> {html.escape(datos['consignatario'])}\n"
        f"📏 <b>Eslora:</b> {html.escape(datos['eslora'])} m | <b>GT:</b> {html.escape(datos['gt'])}\n"
        f"🕒 <b>Entrada:</b> {html.escape(datos['entrada'])}\n"
        f"⚓ <b>Muelle:</b> {html.escape(datos['muelle'])}\n"
        f"🧭 <b>Origen:</b> {html.escape(datos['procedencia'])}\n"
        f"🎯 <b>Destino:</b> {html.escape(datos['destino'])}\n\n"
        f"🛰️ <a href=\"{datos['vesselfinder_url']}\">Contrastado en VesselFinder (Ver Mapa)</a>"
    )

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("📝 Generar Hecho SIGO", callback_data=f"sigo_{barco_id}"))

    try:
        bot.send_message(chat_id, texto, parse_mode="HTML", reply_markup=markup)
    except Exception as e:
        logging.error(f"Error enviando alerta a Telegram: {e}")

# ==============================================================================
# 4. COMANDOS
# ==============================================================================
AYUDA = (
    "🚢 <b>Bot de atraques del Puerto de Málaga</b>\n\n"
    "/barcos — barcos de hoy\n"
    "/barcos 25/11/2026 — barcos de una fecha concreta\n"
    "/ayer — barcos de ayer\n"
    "/manana — barcos de mañana\n"
    "/cancelar — cancela el Hecho SIGO en curso\n\n"
    "Además, cada 10 minutos vigilo el puerto y te aviso si entra un barco nuevo. "
    "Pulsa <b>📝 Generar Hecho SIGO</b> en cualquier aviso para crear el texto."
)

@bot.message_handler(commands=["start", "ayuda", "help"])
def cmd_inicio(message):
    bot.send_message(message.chat.id, AYUDA, parse_mode="HTML")

@bot.message_handler(commands=["cancelar"])
def cmd_cancelar(message):
    sesiones_sigo.pop(message.chat.id, None)
    bot.clear_step_handler_by_chat_id(message.chat.id)
    bot.send_message(message.chat.id, "❌ Cancelado.", reply_markup=ReplyKeyboardRemove())

def listar_barcos(chat_id, fecha_consulta):
    try:
        bot.send_message(
            chat_id,
            f"🔍 Consultando atraques en el Puerto de Málaga para el día <b>{html.escape(fecha_consulta)}</b>...",
            parse_mode="HTML"
        )
        barcos = extraer_barcos_puerto(fecha_consulta)
        if not barcos:
            bot.send_message(chat_id, f"❌ No figuran barcos para la fecha {html.escape(fecha_consulta)}.")
            return
        for b in barcos:
            b_id = guardar_barco_bd(b)
            enviar_alerta_nuevo_barco(b_id, b, chat_id=chat_id)
    except Exception as e:
        logging.error(f"Error listando barcos: {e}")
        bot.send_message(chat_id, f"⚠️ Se produjo un error: {e}")

@bot.message_handler(commands=["barcos"])
def cmd_barcos(message):
    partes = message.text.split()
    fecha = partes[1] if len(partes) > 1 else ahora().strftime("%d/%m/%Y")
    listar_barcos(message.chat.id, fecha)

@bot.message_handler(commands=["ayer"])
def cmd_ayer(message):
    from datetime import timedelta
    listar_barcos(message.chat.id, (ahora() - timedelta(days=1)).strftime("%d/%m/%Y"))

@bot.message_handler(commands=["manana"])
def cmd_manana(message):
    from datetime import timedelta
    listar_barcos(message.chat.id, (ahora() + timedelta(days=1)).strftime("%d/%m/%Y"))

# ==============================================================================
# 5. ASISTENTE INTERACTIVO HECHO SIGO
# ==============================================================================
@bot.callback_query_handler(func=lambda call: call.data.startswith("sigo_"))
def iniciar_asistente_sigo(call):
    barco_id = int(call.data.split("_")[1])
    datos_barco = obtener_barco_por_id(barco_id)

    if not datos_barco:
        bot.answer_callback_query(call.id, "Error: No se encontraron los datos del barco.")
        return

    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id
    sesiones_sigo[chat_id] = {"barco": datos_barco}

    markup = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add(KeyboardButton(ahora().strftime("HOY a las %H:%M")))

    if "a las " in datos_barco["entrada"]:
        hora_web = datos_barco["entrada"].split("a las ")[-1].strip()
        markup.add(KeyboardButton(f"HOY a las {hora_web}"))

    msg = bot.send_message(
        chat_id,
        f"📝 <b>Iniciando Hecho SIGO para el buque {html.escape(datos_barco['buque'])}</b>\n\n"
        f"1️⃣ <b>FECHA Y HORA:</b> Pulsa un botón rápido o escríbela a mano:\n"
        f"• Si es de hoy, escribe solo la hora (ej: <code>05:45</code> o <code>13:30</code>)\n"
        f"• Si es de otro día, escribe hora y fecha (ej: <code>10:30 horas del día 22 de Noviembre de 2026</code>)\n\n"
        f"(/cancelar para salir)",
        parse_mode="HTML",
        reply_markup=markup
    )
    bot.register_next_step_handler(msg, paso_recibir_fecha_hora)

def _es_comando(message):
    """Si el usuario escribe un /comando en mitad del asistente, lo cancelamos."""
    if message.text and message.text.startswith("/"):
        sesiones_sigo.pop(message.chat.id, None)
        bot.send_message(message.chat.id, "❌ Asistente cancelado. Vuelve a enviar el comando.",
                         reply_markup=ReplyKeyboardRemove())
        return True
    return False

def paso_recibir_fecha_hora(message):
    if _es_comando(message):
        return
    chat_id = message.chat.id
    texto = message.text.strip()

    if texto.startswith("HOY a las"):
        hora = texto.replace("HOY a las ", "").strip()
        tramo_fecha = f"A las {hora} horas del día de hoy"
    elif "del día" in texto.lower():
        tramo_fecha = f"A las {texto}" if not texto.lower().startswith("a las") else texto
    else:
        tramo_fecha = f"A las {texto} horas del día de hoy"

    sesiones_sigo[chat_id]["tramo_fecha"] = tramo_fecha

    if TIP_AUTOMATICO:
        sesiones_sigo[chat_id]["texto_agentes"] = construir_texto_agentes([TIP_POR_DEFECTO])
        preguntar_actuacion(chat_id, numero="2️⃣")
        return

    markup_tip = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup_tip.add(KeyboardButton(TIP_POR_DEFECTO))

    msg = bot.send_message(
        chat_id,
        "2️⃣ <b>TIP DE LOS AGENTES:</b>\n"
        f"Pulsa el botón <code>{TIP_POR_DEFECTO}</code> o escribe otro(s) "
        "(si son varios, sepáralos por coma o espacio).\n"
        "Ejemplo: <code>B36491-I L29418N</code>",
        parse_mode="HTML",
        reply_markup=markup_tip
    )
    bot.register_next_step_handler(msg, paso_recibir_tips)

def construir_texto_agentes(tips):
    if len(tips) == 1:
        return f"por el agente de la Sección Fiscal del Puerto de Málaga con TIP {tips[0]}"
    elif len(tips) > 1:
        unidos = " y ".join([", ".join(tips[:-1]), tips[-1]])
        return f"por personal de la Sección Fiscal del Puerto de Málaga con TIP {unidos}"
    return "por personal de la Sección Fiscal del Puerto de Málaga"

def preguntar_actuacion(chat_id, numero="3️⃣"):
    markup = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add(KeyboardButton("Visita de entrada e inspección de documentación"))
    markup.add(KeyboardButton("Avistamiento e inspección de documentación"))

    msg = bot.send_message(
        chat_id,
        f"{numero} <b>TIPO DE ACTUACIÓN:</b>\nPulsa una opción del teclado:",
        parse_mode="HTML",
        reply_markup=markup
    )
    bot.register_next_step_handler(msg, paso_recibir_actuacion)

def paso_recibir_tips(message):
    if _es_comando(message):
        return
    chat_id = message.chat.id
    if chat_id not in sesiones_sigo:
        bot.send_message(chat_id, "❌ La sesión ha expirado. Vuelve a pulsar el botón del barco.",
                         reply_markup=ReplyKeyboardRemove())
        return
    raw_tips = message.text.replace(",", " ").split()
    tips = [t.strip().upper() for t in raw_tips if t.strip()]
    sesiones_sigo[chat_id]["texto_agentes"] = construir_texto_agentes(tips)
    preguntar_actuacion(chat_id)

def paso_recibir_actuacion(message):
    if _es_comando(message):
        return
    chat_id = message.chat.id
    if chat_id not in sesiones_sigo:
        bot.send_message(chat_id, "❌ La sesión ha expirado. Vuelve a pulsar el botón del barco.",
                         reply_markup=ReplyKeyboardRemove())
        return
    sesiones_sigo[chat_id]["actuacion"] = message.text.strip().lower()
    generar_hecho_sigo(chat_id)

def generar_hecho_sigo(chat_id):
    datos = sesiones_sigo.get(chat_id)
    if not datos:
        bot.send_message(chat_id, "❌ La sesión ha expirado. Vuelve a pulsar el botón del barco.",
                         reply_markup=ReplyKeyboardRemove())
        return

    b = datos["barco"]
    # Procedencia, destino y muelle se toman directamente de la web (sin confirmar)
    procedencia = b["procedencia"]
    destino = b["destino"]
    muelle = b["muelle"]
    if not muelle.lower().startswith("muelle"):
        muelle = f"muelle {muelle}"

    titulo_actuacion = datos["actuacion"].upper()

    texto_sigo = (
        f"CATALOGACION: H080340\n\n"
        f"” {titulo_actuacion} DEL BUQUE “ {b['buque']}’’\n\n"
        f"Descripción:\n\n"
        f"{datos['tramo_fecha']}, {datos['texto_agentes']}, se realiza la {datos['actuacion']} "
        f"del buque “{b['buque']}” con bandera de {b['bandera']}, consignado por {b['consignatario']}, "
        f"procedente de {procedencia} y con destino a {destino}. "
        f"Quedando atracado en el {muelle} de este recinto portuario."
    )

    asunto_mail = f"HECHO SIGO - H080340 - BUQUE {b['buque']}"
    token = uuid.uuid4().hex[:10]
    correos_pendientes[token] = {"asunto": asunto_mail, "cuerpo": texto_sigo}

    markup = InlineKeyboardMarkup()
    url_correo = construir_url_correo(asunto_mail, texto_sigo)
    if url_correo:
        markup.add(InlineKeyboardButton("✉️ Abrir en mi correo (ya redactado)", url=url_correo))
    if SMTP_USER and SMTP_PASS:
        markup.add(InlineKeyboardButton(f"📧 Enviar directo a {EMAIL_DESTINO}", callback_data=f"mail_{token}"))

    bot.send_message(
        chat_id,
        f"✅ <b>HECHO SIGO GENERADO</b> (toca el texto para copiarlo):\n\n"
        f"<pre>{html.escape(texto_sigo)}</pre>",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove()
    )
    if markup.keyboard:
        bot.send_message(chat_id, "¿Cómo lo envías?", reply_markup=markup)

    sesiones_sigo.pop(chat_id, None)

def construir_url_correo(asunto, cuerpo):
    """Enlace a la página de GitHub Pages que abre tu correo con todo redactado.
    Los datos van en el fragmento (#), que no se envía a ningún servidor."""
    if not MAIL_PAGE_URL:
        return None
    payload = json.dumps({"t": EMAIL_DESTINO, "s": asunto, "b": cuerpo}, ensure_ascii=False)
    codigo = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    url = f"{MAIL_PAGE_URL}#d={codigo}"
    return url if len(url) <= 2000 else None  # límite prudente para botones de Telegram

# ==============================================================================
# 6. ENVÍO DIRECTO DE CORREO (SMTP) - sustituye al enlace mailto que llegaba vacío
# ==============================================================================
def enviar_correo(asunto, cuerpo):
    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_DESTINO
    msg["Subject"] = asunto
    msg.set_content(cuerpo)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)

@bot.callback_query_handler(func=lambda call: call.data.startswith("mail_"))
def callback_enviar_correo(call):
    token = call.data.split("_", 1)[1]
    pendiente = correos_pendientes.get(token)

    if not pendiente:
        bot.answer_callback_query(call.id, "Este correo ya no está disponible. Genera el Hecho SIGO de nuevo.", show_alert=True)
        return
    if not SMTP_USER or not SMTP_PASS:
        bot.answer_callback_query(call.id, "Falta configurar SMTP_USER / SMTP_PASS en el servidor.", show_alert=True)
        return

    bot.answer_callback_query(call.id, "Enviando...")
    try:
        enviar_correo(pendiente["asunto"], pendiente["cuerpo"])
        correos_pendientes.pop(token, None)
        bot.edit_message_text(
            f"✅ Correo enviado a {EMAIL_DESTINO}\nAsunto: {pendiente['asunto']}",
            call.message.chat.id, call.message.message_id
        )
    except Exception as e:
        logging.error(f"Error enviando correo: {e}")
        bot.send_message(call.message.chat.id, f"⚠️ No se pudo enviar el correo: {e}")

# ==============================================================================
# 7. HILO DE VIGILANCIA AUTOMÁTICA
# ==============================================================================
def bucle_vigilancia_puerto():
    logging.info("Hilo de vigilancia portuaria iniciado...")
    primera_pasada = PRIMERA_PASADA_SILENCIOSA
    while True:
        try:
            barcos = extraer_barcos_puerto()
            for b in barcos:
                if not barco_ya_registrado(b["buque"], b["entrada"], b["muelle"]):
                    b_id = guardar_barco_bd(b)
                    if primera_pasada:
                        # Arranque en un servidor sin base de datos previa: se registran
                        # los barcos que ya estaban sin avisar, para no inundar el chat.
                        continue
                    logging.info(f"Nuevo barco detectado y registrado: {b['buque']} ({b['entrada']})")
                    enviar_alerta_nuevo_barco(b_id, b)
            primera_pasada = False
        except Exception as e:
            logging.error(f"Error en bucle de vigilancia: {e}")
        time.sleep(INTERVALO_COMPROBACION)

def _apagar():
    logging.info("Duración máxima alcanzada: cerrando el bot limpiamente.")
    try:
        bot.stop_polling()
    finally:
        os._exit(0)

if __name__ == "__main__":
    inicializar_bd()
    configurar_menu_bot()
    threading.Thread(target=bucle_vigilancia_puerto, daemon=True).start()
    if DURACION_MAX_MIN > 0:
        t = threading.Timer(DURACION_MAX_MIN * 60, _apagar)
        t.daemon = True
        t.start()
        logging.info(f"El bot se cerrará solo en {DURACION_MAX_MIN} minutos.")

    logging.info("Bot de Telegram conectado y escuchando...")
    bot.infinity_polling()
