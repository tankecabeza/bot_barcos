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
from urllib.parse import quote, urlencode
from email.message import EmailMessage
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta
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
# CONFIGURACIÓN (desde GitHub Actions Secrets / variables de entorno o .env local)
# ==============================================================================
def _cargar_env():
    """Lee el archivo .env (si se ejecuta en local) y lo vuelca en os.environ."""
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
        raise SystemExit(f"Falta {_v}. Configúralo en GitHub Secrets o en un archivo .env local.")

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
EMAIL_DESTINO = os.environ.get("EMAIL_DESTINO") or "aberlanga@guardiacivil.es"
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASS = os.environ.get("SMTP_PASS", "").strip()
MAIL_PAGE_URL = os.environ.get("MAIL_PAGE_URL", "").strip()

WEB_URL = "https://www.puertomalaga.com/es/informacion-atraques/"
AJAX_URL = "https://www.puertomalaga.com/wp-admin/admin-ajax.php"
MAPA_PUERTO_URL = "https://www.vesselfinder.com/?lat=36.7145&lon=-4.4173&zoom=15"

DATA_DIR = os.environ.get("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(DATA_DIR, "atraques_malaga_limpio.db")

TIP_POR_DEFECTO = (os.environ.get("TIP_POR_DEFECTO") or "F10173Y").strip().upper()
TIP_AUTOMATICO = os.environ.get("TIP_AUTOMATICO", "0") == "1"
INTERVALO_COMPROBACION = 600  # segundos (600 = cada 10 minutos)

# Control para turnos en GitHub Actions (ej: 350 min por tramo)
DURACION_MAX_MIN = int(os.environ.get("DURACION_MAX_MIN", "0"))  # 0 = sin límite
PRIMERA_PASADA_SILENCIOSA = os.environ.get("PRIMERA_PASADA_SILENCIOSA", "0") == "1"

# Los servidores de GitHub van en UTC: forzamos hora de Madrid
TZ = ZoneInfo("Europe/Madrid")

def ahora():
    return datetime.now(TZ)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
bot = telebot.TeleBot(TELEGRAM_TOKEN)

sesiones_sigo = {}
correos_pendientes = {}  # token -> {"asunto":..., "cuerpo":..., "buque":...}

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

            if len(fila) == 3 and all(str(x).isdigit() for x in fila):
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

            url_vf = f"https://www.vesselfinder.com/vessels?name={quote(buque)}"

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
# 3. NOTIFICACIÓN DE BARCO EN TELEGRAM (CON LAS 3 OPCIONES EN BOTONES)
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
        f"🎯 <b>Destino:</b> {html.escape(datos['destino'])}"
    )

    markup = InlineKeyboardMarkup(row_width=1)
    # 1. Ver información del buque en VesselFinder
    markup.add(InlineKeyboardButton("🔎 Ver información en VesselFinder", url=datos["vesselfinder_url"]))
    # 2. Ver el mapa en vivo centrado directamente en el Puerto de Málaga
    markup.add(InlineKeyboardButton("🗺️ Ver mapa del puerto en vivo", url=MAPA_PUERTO_URL))
    # 3. Crear Hecho SIGO
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
    listar_barcos(message.chat.id, (ahora() - timedelta(days=1)).strftime("%d/%m/%Y"))

@bot.message_handler(commands=["manana"])
def cmd_manana(message):
    listar_barcos(message.chat.id, (ahora() + timedelta(days=1)).strftime("%d/%m/%Y"))

# ==============================================================================
# 5. ASISTENTE INTERACTIVO HECHO SIGO (botones bajo el mensaje, siempre visibles)
#    En cada paso puedes pulsar el botón o escribir el dato a mano.
# ==============================================================================
ACTUACIONES = {
    "vis": "visita de entrada e inspección de documentación",
    "avi": "avistamiento e inspección de documentación",
}

def _quitar_botones(call):
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass

def _es_comando(message):
    """Si el usuario escribe un /comando en mitad del asistente, lo cancelamos."""
    if message.text and message.text.startswith("/"):
        sesiones_sigo.pop(message.chat.id, None)
        bot.clear_step_handler_by_chat_id(message.chat.id)
        bot.send_message(message.chat.id, "❌ Asistente cancelado. Vuelve a enviar el comando.")
        return True
    return False

def _sin_sesion(chat_id):
    bot.send_message(chat_id, "❌ La sesión ha expirado. Vuelve a pulsar el botón del barco.")

def _texto_o_repetir(message, siguiente_handler):
    """Devuelve el texto escrito, o None (y vuelve a esperar) si no es texto."""
    if _es_comando(message):
        return None
    if not message.text:
        msg = bot.send_message(message.chat.id, "Escribe el dato como texto, por favor.")
        bot.register_next_step_handler(msg, siguiente_handler)
        return None
    return message.text.strip()

# ---------- Paso 1: fecha y hora ----------
@bot.callback_query_handler(func=lambda call: call.data.startswith("sigo_"))
def iniciar_asistente_sigo(call):
    barco_id = int(call.data.split("_")[1])
    datos_barco = obtener_barco_por_id(barco_id)

    if not datos_barco:
        bot.answer_callback_query(call.id, "Error: No se encontraron los datos del barco.")
        return

    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id
    bot.clear_step_handler_by_chat_id(chat_id)

    hora_ahora = ahora().strftime("%H:%M")
    sesion = {"barco": datos_barco, "hora_ahora": hora_ahora, "hora_web": None}
    sesiones_sigo[chat_id] = sesion

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(f"🕒 HOY a las {hora_ahora} (ahora)", callback_data="f_ahora"))
    if "a las " in datos_barco["entrada"]:
        hora_web = datos_barco["entrada"].split("a las ")[-1].strip()
        if hora_web:
            sesion["hora_web"] = hora_web
            markup.add(InlineKeyboardButton(f"🕒 HOY a las {hora_web} (hora de la web)", callback_data="f_web"))

    msg = bot.send_message(
        chat_id,
        f"📝 <b>Hecho SIGO del buque {html.escape(datos_barco['buque'])}</b>\n\n"
        f"1️⃣ <b>FECHA Y HORA:</b> pulsa un botón o escribe la hora a mano:\n"
        f"• Si es de hoy, solo la hora (ej: <code>05:45</code>)\n"
        f"• Si es de otro día: <code>10:30 horas del día 22 de Noviembre de 2026</code>\n\n"
        f"(/cancelar para salir)",
        parse_mode="HTML",
        reply_markup=markup
    )
    bot.register_next_step_handler(msg, paso_recibir_fecha_hora)

@bot.callback_query_handler(func=lambda call: call.data in ("f_ahora", "f_web"))
def cb_fecha(call):
    chat_id = call.message.chat.id
    sesion = sesiones_sigo.get(chat_id)
    if not sesion:
        bot.answer_callback_query(call.id, "La sesión ha expirado. Vuelve a pulsar el botón del barco.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    _quitar_botones(call)
    bot.clear_step_handler_by_chat_id(chat_id)
    hora = sesion["hora_ahora"] if call.data == "f_ahora" else (sesion["hora_web"] or sesion["hora_ahora"])
    procesar_fecha_hora(chat_id, f"HOY a las {hora}")

def paso_recibir_fecha_hora(message):
    texto = _texto_o_repetir(message, paso_recibir_fecha_hora)
    if texto is None:
        return
    procesar_fecha_hora(message.chat.id, texto)

def procesar_fecha_hora(chat_id, texto):
    if chat_id not in sesiones_sigo:
        _sin_sesion(chat_id)
        return

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
    else:
        preguntar_tip(chat_id)

# ---------- Paso 2: TIP ----------
def construir_texto_agentes(tips):
    if len(tips) == 1:
        return f"por el agente de la Sección Fiscal del Puerto de Málaga con TIP {tips[0]}"
    elif len(tips) > 1:
        unidos = " y ".join([", ".join(tips[:-1]), tips[-1]])
        return f"por personal de la Sección Fiscal del Puerto de Málaga con TIP {unidos}"
    return "por personal de la Sección Fiscal del Puerto de Málaga"

def preguntar_tip(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(f"✅ Usar {TIP_POR_DEFECTO}", callback_data="t_def"))
    msg = bot.send_message(
        chat_id,
        f"2️⃣ <b>TIP DE LOS AGENTES:</b>\n"
        f"Pulsa el botón para usar <code>{html.escape(TIP_POR_DEFECTO)}</code>, o escribe otro(s) "
        f"(si son varios, sepáralos por coma o espacio).\n"
        f"Ejemplo: <code>B36491-I L29418N</code>",
        parse_mode="HTML",
        reply_markup=markup
    )
    bot.register_next_step_handler(msg, paso_recibir_tips)

@bot.callback_query_handler(func=lambda call: call.data == "t_def")
def cb_tip(call):
    chat_id = call.message.chat.id
    if chat_id not in sesiones_sigo:
        bot.answer_callback_query(call.id, "La sesión ha expirado. Vuelve a pulsar el botón del barco.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    _quitar_botones(call)
    bot.clear_step_handler_by_chat_id(chat_id)
    procesar_tips(chat_id, TIP_POR_DEFECTO)

def paso_recibir_tips(message):
    texto = _texto_o_repetir(message, paso_recibir_tips)
    if texto is None:
        return
    procesar_tips(message.chat.id, texto)

def procesar_tips(chat_id, texto):
    if chat_id not in sesiones_sigo:
        _sin_sesion(chat_id)
        return
    tips = [t.strip().upper() for t in texto.replace(",", " ").split() if t.strip()]
    sesiones_sigo[chat_id]["texto_agentes"] = construir_texto_agentes(tips)
    preguntar_actuacion(chat_id)

# ---------- Paso 3: tipo de actuación ----------
def preguntar_actuacion(chat_id, numero="3️⃣"):
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("📋 Visita de entrada e inspección", callback_data="a_vis"),
        InlineKeyboardButton("🔭 Avistamiento e inspección", callback_data="a_avi"),
    )
    msg = bot.send_message(
        chat_id,
        f"{numero} <b>TIPO DE ACTUACIÓN:</b>\n"
        f"Pulsa una opción o escribe el texto a mano:",
        parse_mode="HTML",
        reply_markup=markup
    )
    bot.register_next_step_handler(msg, paso_recibir_actuacion)

@bot.callback_query_handler(func=lambda call: call.data in ("a_vis", "a_avi"))
def cb_actuacion(call):
    chat_id = call.message.chat.id
    if chat_id not in sesiones_sigo:
        bot.answer_callback_query(call.id, "La sesión ha expirado. Vuelve a pulsar el botón del barco.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    _quitar_botones(call)
    bot.clear_step_handler_by_chat_id(chat_id)
    clave = call.data.split("_")[1]
    procesar_actuacion(chat_id, ACTUACIONES[clave])

def paso_recibir_actuacion(message):
    texto = _texto_o_repetir(message, paso_recibir_actuacion)
    if texto is None:
        return
    procesar_actuacion(message.chat.id, texto.lower())

def procesar_actuacion(chat_id, actuacion):
    if chat_id not in sesiones_sigo:
        _sin_sesion(chat_id)
        return
    sesiones_sigo[chat_id]["actuacion"] = actuacion
    preguntar_procedencia(chat_id)

# ---------- Paso 4: procedencia ----------
def preguntar_procedencia(chat_id):
    b = sesiones_sigo[chat_id]["barco"]
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(f"✅ {b['procedencia']}", callback_data="p_web"))
    msg = bot.send_message(
        chat_id,
        f"4️⃣ <b>PROCEDENCIA (Puerto anterior):</b>\n"
        f"Pulsa el botón para confirmar <code>{html.escape(b['procedencia'])}</code> o escríbelo a mano:",
        parse_mode="HTML",
        reply_markup=markup
    )
    bot.register_next_step_handler(msg, paso_recibir_procedencia)

@bot.callback_query_handler(func=lambda call: call.data == "p_web")
def cb_procedencia(call):
    chat_id = call.message.chat.id
    if chat_id not in sesiones_sigo:
        bot.answer_callback_query(call.id, "La sesión ha expirado.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    _quitar_botones(call)
    bot.clear_step_handler_by_chat_id(chat_id)
    procesar_procedencia(chat_id, sesiones_sigo[chat_id]["barco"]["procedencia"])

def paso_recibir_procedencia(message):
    texto = _texto_o_repetir(message, paso_recibir_procedencia)
    if texto is None:
        return
    procesar_procedencia(message.chat.id, texto)

def procesar_procedencia(chat_id, procedencia):
    if chat_id not in sesiones_sigo:
        _sin_sesion(chat_id)
        return
    sesiones_sigo[chat_id]["procedencia"] = procedencia.strip().upper()
    preguntar_destino(chat_id)

# ---------- Paso 5: destino ----------
def preguntar_destino(chat_id):
    b = sesiones_sigo[chat_id]["barco"]
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(f"✅ {b['destino']}", callback_data="d_web"))
    msg = bot.send_message(
        chat_id,
        f"5️⃣ <b>DESTINO (Próximo puerto):</b>\n"
        f"Pulsa el botón para confirmar <code>{html.escape(b['destino'])}</code> o escríbelo a mano:",
        parse_mode="HTML",
        reply_markup=markup
    )
    bot.register_next_step_handler(msg, paso_recibir_destino)

@bot.callback_query_handler(func=lambda call: call.data == "d_web")
def cb_destino(call):
    chat_id = call.message.chat.id
    if chat_id not in sesiones_sigo:
        bot.answer_callback_query(call.id, "La sesión ha expirado.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    _quitar_botones(call)
    bot.clear_step_handler_by_chat_id(chat_id)
    procesar_destino(chat_id, sesiones_sigo[chat_id]["barco"]["destino"])

def paso_recibir_destino(message):
    texto = _texto_o_repetir(message, paso_recibir_destino)
    if texto is None:
        return
    procesar_destino(message.chat.id, texto)

def procesar_destino(chat_id, destino):
    if chat_id not in sesiones_sigo:
        _sin_sesion(chat_id)
        return
    sesiones_sigo[chat_id]["destino"] = destino.strip().upper()
    preguntar_muelle(chat_id)

# ---------- Paso 6: muelle de atraque y generación final ----------
MUELLE_OPCIONES = {
    "m_2": "muelle 2",
    "m_91": "muelle 9.1",
    "m_ln": "muelle levante NORTE",
    "m_an": "muelle AMADEA ATRAQUE NORTE",
}

def _formatear_muelle(m):
    m = m.strip()
    return m if m.lower().startswith("muelle") else f"muelle {m}"

def preguntar_muelle(chat_id):
    b = sesiones_sigo[chat_id]["barco"]
    m_web = _formatear_muelle(b["muelle"])
    sesiones_sigo[chat_id]["muelle_web"] = m_web

    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(InlineKeyboardButton(f"✅ {m_web} (Web)", callback_data="m_web"))
    markup.add(
        InlineKeyboardButton("muelle 2", callback_data="m_2"),
        InlineKeyboardButton("muelle 9.1", callback_data="m_91"),
        InlineKeyboardButton("levante NORTE", callback_data="m_ln"),
        InlineKeyboardButton("AMADEA NORTE", callback_data="m_an"),
    )
    msg = bot.send_message(
        chat_id,
        f"6️⃣ <b>MUELLE DE ATRAQUE:</b>\n"
        f"Pulsa el muelle detectado (<code>{html.escape(m_web)}</code>), uno habitual, o escríbelo a mano:",
        parse_mode="HTML",
        reply_markup=markup
    )
    bot.register_next_step_handler(msg, paso_recibir_muelle)

@bot.callback_query_handler(func=lambda call: call.data in ("m_web", "m_2", "m_91", "m_ln", "m_an"))
def cb_muelle(call):
    chat_id = call.message.chat.id
    if chat_id not in sesiones_sigo:
        bot.answer_callback_query(call.id, "La sesión ha expirado.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    _quitar_botones(call)
    bot.clear_step_handler_by_chat_id(chat_id)
    if call.data == "m_web":
        muelle = sesiones_sigo[chat_id].get("muelle_web") or _formatear_muelle(sesiones_sigo[chat_id]["barco"]["muelle"])
    else:
        muelle = MUELLE_OPCIONES[call.data]
    finalizar_sigo(chat_id, muelle)

def paso_recibir_muelle(message):
    texto = _texto_o_repetir(message, paso_recibir_muelle)
    if texto is None:
        return
    finalizar_sigo(message.chat.id, _formatear_muelle(texto))

def _construir_url_pagina_correo(destinatario, asunto, cuerpo):
    """Construye el enlace hacia docs/abrir-correo.html pasando datos en URL y Base64."""
    b64_subject = base64.urlsafe_b64encode(asunto.encode("utf-8")).decode("ascii")
    b64_body = base64.urlsafe_b64encode(cuerpo.encode("utf-8")).decode("ascii")
    params = urlencode({
        "to": destinatario,
        "subject": asunto,
        "body": cuerpo,
        "s64": b64_subject,
        "b64": b64_body,
    })
    sep = "&" if "?" in MAIL_PAGE_URL else "?"
    return f"{MAIL_PAGE_URL}{sep}{params}"

def finalizar_sigo(chat_id, muelle):
    datos = sesiones_sigo.pop(chat_id, None)
    if not datos:
        _sin_sesion(chat_id)
        return

    b = datos["barco"]
    titulo_actuacion = datos["actuacion"].upper()

    texto_sigo = (
        f"CATALOGACION: H080340\n\n"
        f"” {titulo_actuacion} DEL BUQUE “ {b['buque']}’’\n\n"
        f"Descripción:\n\n"
        f"{datos['tramo_fecha']}, {datos['texto_agentes']}, se realiza la {datos['actuacion']} "
        f"del buque “{b['buque']}” con bandera de {b['bandera']}, consignado por {b['consignatario']}, "
        f"procedente de {datos['procedencia']} y con destino a {datos['destino']}. "
        f"Quedando atracado en el {muelle} de este recinto portuario."
    )

    asunto = f"HECHO SIGO - H080340 - BUQUE {b['buque']}"
    token = uuid.uuid4().hex[:12]
    correos_pendientes[token] = {"asunto": asunto, "cuerpo": texto_sigo, "buque": b["buque"]}

    markup = InlineKeyboardMarkup(row_width=1)
    if MAIL_PAGE_URL:
        url_puente = _construir_url_pagina_correo(EMAIL_DESTINO, asunto, texto_sigo)
        markup.add(InlineKeyboardButton(f"📱 Abrir en app de correo ({EMAIL_DESTINO})", url=url_puente))
    if SMTP_USER and SMTP_PASS:
        markup.add(InlineKeyboardButton(f"📧 Enviar directo por SMTP a {EMAIL_DESTINO}", callback_data=f"mail_{token}"))

    bot.send_message(
        chat_id,
        f"✅ <b>HECHO SIGO GENERADO:</b>\n"
        f"<i>(Toca el recuadro para copiar el texto)</i>\n\n"
        f"<pre>{html.escape(texto_sigo)}</pre>",
        parse_mode="HTML",
        reply_markup=markup if (MAIL_PAGE_URL or (SMTP_USER and SMTP_PASS)) else ReplyKeyboardRemove()
    )

    # Si están configurados SMTP_USER y SMTP_PASS, también lo enviamos automáticamente
    if SMTP_USER and SMTP_PASS:
        exito, detalle = enviar_correo_smtp(asunto, texto_sigo)
        if exito:
            bot.send_message(chat_id, f"📨 Correo enviado automáticamente a <code>{html.escape(EMAIL_DESTINO)}</code>.", parse_mode="HTML")
        else:
            bot.send_message(chat_id, f"⚠️ No se pudo enviar automáticamente por SMTP ({html.escape(detalle)}). Puedes pulsar el botón o copiar el texto.", parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("mail_"))
def cb_enviar_correo(call):
    token = call.data.split("_", 1)[1]
    info = correos_pendientes.get(token)
    if not info:
        bot.answer_callback_query(call.id, "Este correo ya expiró. Genera de nuevo el SIGO.", show_alert=True)
        return
    bot.answer_callback_query(call.id, "Enviando correo...")
    exito, detalle = enviar_correo_smtp(info["asunto"], info["cuerpo"])
    if exito:
        bot.send_message(call.message.chat.id, f"📨 Correo enviado correctamente a <code>{html.escape(EMAIL_DESTINO)}</code>.", parse_mode="HTML")
    else:
        bot.send_message(call.message.chat.id, f"❌ Error SMTP: <code>{html.escape(detalle)}</code>", parse_mode="HTML")

# ==============================================================================
# 6. ENVÍO DE CORREO POR SMTP
# ==============================================================================
def enviar_correo_smtp(asunto, cuerpo):
    if not SMTP_USER or not SMTP_PASS:
        return False, "Faltan SMTP_USER o SMTP_PASS"

    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_DESTINO
    msg["Subject"] = asunto
    msg.set_content(cuerpo)

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as servidor:
            servidor.starttls()
            servidor.login(SMTP_USER, SMTP_PASS)
            servidor.send_message(msg)
        return True, "OK"
    except Exception as e:
        logging.error(f"Error enviando correo SMTP: {e}")
        return False, str(e)

# ==============================================================================
# 7. VIGILANCIA AUTOMÁTICA Y CONTROL DE TIEMPO (GITHUB ACTIONS)
# ==============================================================================
def bucle_vigilancia_puerto():
    logging.info("Hilo de vigilancia portuaria iniciado...")
    primera_vez = True
    while True:
        try:
            barcos = extraer_barcos_puerto()
            for b in barcos:
                if not barco_ya_registrado(b["buque"], b["entrada"], b["muelle"]):
                    b_id = guardar_barco_bd(b)
                    if primera_vez and PRIMERA_PASADA_SILENCIOSA:
                        logging.info(f"[Silencioso] Barco inicial registrado: {b['buque']}")
                    else:
                        logging.info(f"Nuevo barco detectado: {b['buque']} ({b['entrada']})")
                        enviar_alerta_nuevo_barco(b_id, b)
            primera_vez = False
        except Exception as e:
            logging.error(f"Error en bucle de vigilancia: {e}")

        time.sleep(INTERVALO_COMPROBACION)

def temporizador_apagado_limpio():
    """Apaga el proceso limpiamente al alcanzar DURACION_MAX_MIN para dar paso al tramo2 en GitHub Actions."""
    if DURACION_MAX_MIN <= 0:
        return
    logging.info(f"Temporizador activado: el bot se cerrará limpiamente en {DURACION_MAX_MIN} minutos.")
    time.sleep(DURACION_MAX_MIN * 60)
    logging.info("Tiempo del tramo completado. Cerrando limpiamente para dar relevo al siguiente tramo...")
    try:
        bot.stop_polling()
    except Exception:
        pass
    os._exit(0)

if __name__ == "__main__":
    inicializar_bd()
    configurar_menu_bot()

    hilo_vigilancia = threading.Thread(target=bucle_vigilancia_puerto, daemon=True)
    hilo_vigilancia.start()

    if DURACION_MAX_MIN > 0:
        hilo_timer = threading.Thread(target=temporizador_apagado_limpio, daemon=True)
        hilo_timer.start()

    logging.info("Bot de Telegram conectado y escuchando...")
    bot.infinity_polling(timeout=20, long_polling_timeout=10)
