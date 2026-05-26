"""
LIVINGHOUSE · BOT DE CONTROL DE PRODUCCIÓN
==========================================
Oficios soportados: corte_costura, tapiceria, pintura, esqueleteria, carpinteria
"""

import os
import logging
import requests as http_requests
from datetime import datetime, timedelta

from dotenv import load_dotenv
from supabase import create_client, Client
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler,
    filters, ContextTypes
)

load_dotenv()
logging.basicConfig(
    format="%(asctime)s · %(name)s · %(levelname)s · %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN    = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

OFICIOS = {
    "corte_costura": "✂️ Corte y Costura",
    "tapiceria":     "🛋️ Tapicería",
    "carpinteria":   "🪚 Carpintería",
    "esqueleteria":  "🔧 Esqueletería",
    "pintura":       "🎨 Pintura",
}

OFICIO, FVE, PRODUCT_NAME, PRICE_TYPE, SPECIAL_PRICE, PHOTO = range(6)
PAY_WORKER, PAY_AMOUNT, PAY_CONFIRM = range(10, 13)


# ═══════════════════════════════════════════════════════════════
# HELPERS DE BASE DE DATOS
# ═══════════════════════════════════════════════════════════════

def get_worker(telegram_id: int):
    r = supabase.table("workers").select("*").eq("telegram_id", telegram_id).eq("activo", True).execute()
    return r.data[0] if r.data else None

def get_verifiers():
    r = supabase.table("verifiers").select("*").eq("active", True).execute()
    return r.data

def is_verifier(telegram_id: int) -> bool:
    r = supabase.table("verifiers").select("id").eq("telegram_id", telegram_id).execute()
    return bool(r.data)

def get_verifier(telegram_id: int):
    r = supabase.table("verifiers").select("*").eq("telegram_id", telegram_id).execute()
    return r.data[0] if r.data else None

def find_price(product_name: str, oficio: str):
    r = supabase.table("price_list").select("*").eq("active", True)\
        .eq("oficio", oficio).ilike("product_name", product_name).execute()
    if r.data:
        return r.data[0]
    for kw in product_name.upper().split():
        if len(kw) < 4:
            continue
        r = supabase.table("price_list").select("*").eq("active", True)\
            .eq("oficio", oficio).ilike("product_name", f"%{kw}%").execute()
        if r.data:
            return r.data[0]
    return None

def get_delivery(delivery_id: int):
    r = supabase.table("deliveries").select("*, workers(*)").eq("id", delivery_id).execute()
    return r.data[0] if r.data else None

def fmt_price(value) -> str:
    if value is None:
        return "-"
    return f"${float(value):,.0f}".replace(",", ".")


# ═══════════════════════════════════════════════════════════════
# ASISTENTE IA — GEMINI
# ═══════════════════════════════════════════════════════════════

def get_context_data() -> str:
    try:
        since = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        deliveries = supabase.table("deliveries")\
            .select("*, workers(name)")\
            .gte("created_at", since)\
            .order("created_at", desc=True)\
            .execute().data
        workers = supabase.table("workers").select("*").eq("activo", True).execute().data

        lines = [
            "=== SISTEMA LIVINGHOUSE ===",
            "Livinghouse es una fábrica de muebles en Manizales, Colombia.",
            "Cristhian es el dueño y administra todo el sistema desde el bot de Telegram.",
            "",
            "OFICIOS QUE MANEJA LA FÁBRICA (6):",
            "  ✂️ Corte y Costura",
            "  🛋️ Tapicería",
            "  🪚 Carpintería",
            "  🔧 Esqueletería",
            "  🎨 Pintura",
            "",
            "ROLES Y FLUJO DE TRABAJO:",
            "  - CONTRATISTAS: trabajadores externos que producen muebles.",
            "    Reportan cada trabajo terminado con el comando /reportar (selecciona oficio, FVE, producto, foto).",
            "  - VERIFICADORES (como Cindy y Juan David): revisan reportes con /pendientes y aprueban/rechazan/modifican.",
            "    Cada producto reportado tiene un precio según oficio. Cuando un verificador aprueba, se registra para pago.",
            "",
            "COMANDOS DISPONIBLES:",
            "  /reportar - inicia el flujo para reportar una entrega (selección por botones)",
            "  /pendientes - lista entregas que esperan aprobación",
            "  /resumen - resumen de producción y pagos",
            "  /precios - consulta la lista de precios por oficio",
            "",
            "=== DATOS REALES (últimos 30 días) ===",
        ]

        resumen_workers = {}
        for d in deliveries:
            nombre = d.get("workers", {}).get("name", "Desconocido")
            if nombre not in resumen_workers:
                resumen_workers[nombre] = {"total": 0, "cantidad": 0, "aprobadas": 0}
            resumen_workers[nombre]["cantidad"] += 1
            if d["status"] == "approved":
                resumen_workers[nombre]["total"] += float(d.get("final_price", 0))
                resumen_workers[nombre]["aprobadas"] += 1

        lines.append("\nRESUMEN POR CONTRATISTA:")
        if resumen_workers:
            for nombre, datos in resumen_workers.items():
                lines.append(f"  - {nombre}: {datos['cantidad']} entregas ({datos['aprobadas']} aprobadas), total aprobado: ${datos['total']:,.0f}")
        else:
            lines.append("  (sin entregas registradas aún)")

        lines.append("\nÚLTIMAS 20 ENTREGAS:")
        if deliveries:
            for d in deliveries[:20]:
                nombre = d.get("workers", {}).get("name", "?")
                fecha = d["created_at"][:10]
                oficio = d.get("notes", "").replace("Oficio: ", "") if d.get("notes") and "Oficio:" in str(d.get("notes", "")) else ""
                lines.append(f"  - {fecha} | {nombre} | {d['product_name'][:40]} | {oficio} | ${float(d.get('final_price',0)):,.0f} | {d['status']}")
        else:
            lines.append("  (sin entregas registradas aún)")

        pend = [d for d in deliveries if d["status"] == "pending"]
        lines.append(f"\nENTREGAS PENDIENTES DE APROBACIÓN: {len(pend)}")
        for d in pend[:5]:
            nombre = d.get("workers", {}).get("name", "?")
            lines.append(f"  - {nombre} | {d['product_name'][:40]} | ${float(d.get('final_price',0)):,.0f}")

        lines.append(f"\nCONTRATISTAS ACTIVOS REGISTRADOS ({len(workers)}):")
        if workers:
            for w in workers:
                lines.append(f"  - {w['name']}")
        else:
            lines.append("  (aún no se han registrado contratistas en el sistema)")

        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error obteniendo contexto: {e}")
        return "No se pudo obtener datos de la base de datos."


def build_system_prompt(user_name: str, context: str) -> str:
    """Construye el prompt de sistema con rol, flujos y contexto operativo."""
    return (
        f"Eres el asistente conversacional de Livinghouse, una fábrica de muebles en Manizales, Colombia. "
        f"Estás hablando con {user_name}, quien tiene acceso administrativo al sistema (es dueño o verificador, "
        f"con permisos completos sobre el bot). NUNCA le digas que contacte a otro encargado: él ES el encargado.\n\n"
        "TU ROL:\n"
        "Responder con calidez, claridad y brevedad en español. Usa emojis con moderación.\n\n"
        "⚠️ REGLA CRÍTICA — NO INVENTAR NI SIMULAR EL BOT:\n"
        "TÚ ERES SOLO UN ASISTENTE DE INFORMACIÓN. NO ERES EL BOT que ejecuta comandos.\n"
        "NUNCA puedes:\n"
        "  • Reportar entregas, registrar pagos, aprobar nada, o modificar la base de datos.\n"
        "  • Simular el flujo del bot escribiendo cosas como 'Selecciona tu oficio: ...' "
        "o mostrando opciones en texto. Esos botones reales solo aparecen cuando el usuario "
        "escribe el comando con la barra invertida.\n"
        "  • Inventar detalles de trabajos, nombres, FVEs, productos o cualquier dato que no esté "
        "en la sección de DATOS REALES más abajo.\n"
        "  • Confirmar acciones que no se hicieron. Por ejemplo, NUNCA digas '¡Entendido! "
        "Vamos a reportar...' porque tú no puedes reportar nada.\n\n"
        "Si el usuario escribe palabras como 'reportar', 'pagar', 'aprobar', 'cuenta', 'historial' "
        "SIN la barra invertida, tu ÚNICA respuesta posible es decirle: "
        "'Para esa acción necesitas usar el comando con barra al inicio, por ejemplo /reportar'. "
        "PUNTO. No expliques el flujo, no muestres oficios, no muestres opciones. Solo apunta al comando.\n\n"
        "Si el usuario te pregunta INFORMACIÓN (cuánto se produjo, cuánto se debe, qué entregas hay, "
        "etc.) entonces sí respondes basándote en los DATOS REALES más abajo.\n\n"
        "=== FLUJO COMPLETO DE PRODUCCIÓN ===\n\n"
        "1) REPORTE DEL CONTRATISTA (comando /reportar):\n"
        "   El contratista (Joselyn y otros que se vayan registrando) entra al bot y reporta una entrega terminada. "
        "El flujo guiado por botones es:\n"
        "   a. Selecciona su oficio (corte/costura, tapicería, carpintería, esqueletería, pintura)\n"
        "   b. Indica la FVE (Factura de Venta a la que pertenece el trabajo)\n"
        "   c. Escribe el nombre del producto entregado\n"
        "   d. El bot busca el precio en la lista del oficio correspondiente. Si existe, lo asigna automáticamente. "
        "Si no aparece, permite ingresar un precio especial.\n"
        "   e. El contratista envía una foto del producto terminado como evidencia.\n"
        "   f. La entrega queda en estado 'pendiente' y se notifica a los verificadores.\n\n"
        "2) VERIFICACIÓN (comando /pendientes):\n"
        "   Los verificadores (Cindy, Juan David, y otros administradores) reciben la notificación con la foto y los datos. "
        "Pueden:\n"
        "   • APROBAR la entrega (queda lista para cuenta de cobro)\n"
        "   • RECHAZAR (con un motivo escrito que se envía al contratista)\n"
        "   • MODIFICAR el precio o detalles antes de aprobar\n\n"
        "3) ACUMULACIÓN DE CUENTA DE COBRO:\n"
        "   Cada entrega aprobada se va sumando al saldo pendiente de pago del contratista. "
        "Esto forma su 'cuenta de cobro' actual — lo que la fábrica le debe en este momento.\n\n"
        "4) PAGO Y CORTE DE CUENTA (a futuro):\n"
        "   Cuando la fábrica le paga a un contratista (por ejemplo cada quincena), un verificador debe registrar "
        "ese pago en el sistema. Al registrarlo, todas las entregas aprobadas hasta ese momento quedan marcadas "
        "como 'pagadas' y la cuenta de cobro del contratista se reinicia en cero. A partir de ahí, vuelve a "
        "acumular las próximas entregas aprobadas hasta el siguiente pago. "
        "Esta funcionalidad de registrar pagos y hacer el corte de cuenta forma parte de la siguiente fase "
        "del sistema (el dashboard web), todavía no está implementada en el bot.\n\n"
        "COMANDOS DISPONIBLES ACTUALMENTE:\n"
        "  • /reportar - inicia el flujo para registrar una entrega\n"
        "  • /pendientes - lista entregas que esperan aprobación\n"
        "  • /resumen - resumen general de producción\n"
        "  • /precios - consulta la lista de precios por oficio\n"
        "  • /cuenta - consulta el saldo actual por cobrar (contratista ve el suyo; verificador ve el de todos)\n"
        "  • /pagar - (solo verificadores) registra un pago a un contratista, con soporte para abonos parciales\n"
        "  • /historial - ver pagos pasados (contratista ve los suyos; verificador puede usar /historial <nombre> para ver los de alguien específico)\n\n"
        "Si te piden información que no está en los datos (fotos individuales, archivos, detalles fuera de los "
        "registros), reconócelo con naturalidad sin inventar. Si te saludan informalmente, responde breve "
        "y vuelve a estar disponible — no repitas la introducción completa en cada mensaje.\n\n"
        f"{context}"
    )


async def ask_openrouter(question: str, user_name: str) -> str:
    """Llama a OpenRouter con DeepSeek (gratis)."""
    if not OPENROUTER_API_KEY:
        return None
    try:
        context = get_context_data()
        system = build_system_prompt(user_name, context)
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://livinghouse.bot",
            "X-Title": "Livinghouse Bot",
        }
        payload = {
            "model": "openrouter/free",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": question},
            ],
        }
        logger.info("ask_openrouter: llamando...")
        response = http_requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers, json=payload, timeout=60
        )
        logger.info(f"ask_openrouter: response status = {response.status_code}")
        if response.status_code != 200:
            logger.error(f"ask_openrouter: respuesta no-200: {response.text[:800]}")
            return None
        return response.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"Error con OpenRouter: {type(e).__name__}: {e}")
        return None


async def ask_claude(question: str, user_name: str) -> str:
    """Llama a la API de Claude (Anthropic) con el contexto de Livinghouse."""
    logger.info(f"ask_claude: ANTHROPIC_API_KEY presente = {bool(ANTHROPIC_API_KEY)}")
    if not ANTHROPIC_API_KEY:
        logger.warning("ask_claude: no hay ANTHROPIC_API_KEY, retornando None")
        return None  # señal para probar Gemini
    try:
        context = get_context_data()
        system = build_system_prompt(user_name, context)
        headers = {
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 1024,
            "system": system,
            "messages": [{"role": "user", "content": question}],
        }
        logger.info("ask_claude: llamando a Anthropic API...")
        response = http_requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers, json=payload, timeout=30
        )
        logger.info(f"ask_claude: response status = {response.status_code}")
        if response.status_code != 200:
            logger.error(f"ask_claude: respuesta no-200: {response.text[:500]}")
        response.raise_for_status()
        data = response.json()
        return data["content"][0]["text"]
    except Exception as e:
        logger.error(f"Error con Claude: {type(e).__name__}: {e}")
        return "⚠️ No pude procesar tu pregunta en este momento."


async def ask_gemini(question: str, user_name: str) -> str:
    # Probar OpenRouter (DeepSeek gratis) primero
    if OPENROUTER_API_KEY:
        result = await ask_openrouter(question, user_name)
        if result is not None:
            return result

    # Si OpenRouter falla, probar Gemini
    if GEMINI_API_KEY:
        try:
            context = get_context_data()
            system = build_system_prompt(user_name, context)
            prompt = f"{system}\n\nPregunta del usuario: {question}"

            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={GEMINI_API_KEY}"
            logger.info(f"ask_gemini: llamando, prompt tiene {len(prompt)} caracteres")
            response = http_requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=30)
            logger.info(f"ask_gemini: response status = {response.status_code}")
            if response.status_code == 200:
                return response.json()["candidates"][0]["content"]["parts"][0]["text"]
            logger.error(f"ask_gemini: respuesta no-200: {response.text[:800]}")
        except Exception as e:
            logger.error(f"Error con Gemini: {type(e).__name__}: {e}")

    # Si Gemini falla, intentar Claude como respaldo
    if ANTHROPIC_API_KEY:
        result = await ask_claude(question, user_name)
        if result is not None:
            return result

    return "⚠️ No pude procesar tu pregunta en este momento."


async def ask_gemini_audio(audio_bytes: bytes, user_name: str, mime_type: str = "audio/ogg") -> str:
    """Envía audio a Gemini para que lo transcriba y responda con base en el contexto."""
    if not GEMINI_API_KEY:
        return "⚠️ La clave de Gemini no está configurada."
    try:
        import base64
        context = get_context_data()
        prompt = f"""Eres el asistente del sistema de producción de Livinghouse.
El usuario te envió una nota de voz. Primero transcribe MENTALMENTE el audio en español, 
luego responde su pregunta basándote ÚNICAMENTE en los datos que se proporcionan.
Responde en español, claro y conciso, con emojis apropiados.

Quien pregunta: {user_name}

{context}

Instrucción: Escucha el audio, entiende lo que pregunta el usuario, y respóndele directamente
con base en los datos. No incluyas la transcripción literal, solo la respuesta."""

        audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
        payload = {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": mime_type, "data": audio_b64}}
                ]
            }]
        }
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
        response = http_requests.post(url, json=payload, timeout=60)
        response.raise_for_status()
        return response.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        logger.error(f"Error con Gemini audio: {e}")
        return "⚠️ No pude procesar tu nota de voz en este momento."


# ═══════════════════════════════════════════════════════════════
# /START
# ═══════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    worker = get_worker(user.id)
    if worker:
        await update.message.reply_text(
            f"👋 ¡Hola *{worker['name']}*!\n\n"
            f"*¿Qué puedes hacer?*\n"
            f"• /reportar — registrar un producto terminado\n"
            f"• /mistotal — ver tu acumulado semanal\n"
            f"• /cancelar — cancelar un reporte en curso\n\n"
            f"_Livinghouse · Sistema de producción_",
            parse_mode="Markdown"
        )
    elif is_verifier(user.id):
        await update.message.reply_text(
            f"👋 ¡Hola *{user.first_name}*! Eres verificador.\n\n"
            f"*¿Qué puedes hacer?*\n"
            f"• /pendientes — ver entregas sin aprobar\n"
            f"• /resumen [nombre] [semana|quincena]\n"
            f"• /precios [término] — buscar precios\n\n"
            f"También puedes escribirme preguntas en lenguaje natural 🤖",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"👋 Hola {user.first_name}.\n\n"
            f"⚠️ No estás registrado en el sistema *Livinghouse*.\n"
            f"Tu ID de Telegram es: `{user.id}`\n_(compártelo con el admin)_",
            parse_mode="Markdown"
        )


# ═══════════════════════════════════════════════════════════════
# /REPORTAR
# ═══════════════════════════════════════════════════════════════

async def reportar_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    worker = get_worker(update.effective_user.id)
    if not worker:
        await update.message.reply_text(
            "❌ No estás registrado.\nContacta al administrador con tu ID: "
            f"`{update.effective_user.id}`", parse_mode="Markdown"
        )
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["worker"] = worker
    context.user_data["delivery"] = {}

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=f"oficio_{key}")]
        for key, label in OFICIOS.items()
    ])
    await update.message.reply_text(
        "📦 *Nuevo reporte de producto terminado*\n\n"
        "Paso 1️⃣ — ¿Qué *oficio* realizaste?\n\nEnvía /cancelar para salir.",
        parse_mode="Markdown", reply_markup=keyboard
    )
    return OFICIO


async def got_oficio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    oficio_key = query.data.replace("oficio_", "")
    oficio_label = OFICIOS.get(oficio_key, oficio_key)
    context.user_data["delivery"]["oficio"] = oficio_key
    context.user_data["delivery"]["oficio_label"] = oficio_label
    await query.edit_message_text(
        f"✅ Oficio: *{oficio_label}*\n\n"
        f"Paso 2️⃣ — ¿Cuál es el número de *FVE u ODP*?\n\n"
        f"_Ejemplo: FVE 2118_\n\nEnvía /cancelar para salir.",
        parse_mode="Markdown"
    )
    return FVE


async def got_fve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fve = update.message.text.strip().upper()
    context.user_data["delivery"]["fve"] = fve
    await update.message.reply_text(
        f"✅ FVE: *{fve}*\n\nPaso 3️⃣ — ¿Cuál es el *nombre del producto*?\n\n_Ejemplo: Sofa Cama Montreal_",
        parse_mode="Markdown"
    )
    return PRODUCT_NAME


async def got_product_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    product_name = update.message.text.strip()
    delivery = context.user_data["delivery"]
    delivery["product_name"] = product_name
    oficio = delivery["oficio"]
    oficio_label = delivery["oficio_label"]
    price_match = find_price(product_name, oficio)
    delivery["price_match"] = price_match

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋  Precio de lista", callback_data="type_standard")],
        [InlineKeyboardButton("📐  Medida especial", callback_data="type_special")],
    ])

    if price_match:
        if oficio == "corte_costura" and price_match.get("precio_corte"):
            detalle = (f"   ├ Corte:   {fmt_price(price_match.get('precio_corte'))}\n"
                      f"   ├ Costura: {fmt_price(price_match.get('precio_costura'))}\n"
                      f"   └ *Total:  {fmt_price(price_match['precio_total'])}*")
        else:
            detalle = f"   └ *Total: {fmt_price(price_match['precio_total'])}*"
        msg = (f"📦 *{product_name}*\n🏷️ {oficio_label}\n\n💡 Precio en lista:\n{detalle}\n\n"
               f"Paso 4️⃣ — ¿Precio de lista o medida especial?")
    else:
        msg = (f"📦 *{product_name}*\n🏷️ {oficio_label}\n\n"
               f"⚠️ No está en la lista de {oficio_label}.\n\nPaso 4️⃣ — ¿Precio de lista o medida especial?")

    await update.message.reply_text(msg, reply_markup=keyboard, parse_mode="Markdown")
    return PRICE_TYPE


async def got_price_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    delivery = context.user_data["delivery"]
    price_match = delivery.get("price_match")

    if query.data == "type_standard":
        delivery["is_special"] = False
        if price_match:
            delivery["final_price"] = float(price_match["precio_total"])
            delivery["price_list_id"] = price_match["id"]
            await query.edit_message_text(
                f"✅ Precio de lista: *{fmt_price(price_match['precio_total'])}*\n\n"
                f"Paso 5️⃣ — Envía la *foto del producto terminado* 📸",
                parse_mode="Markdown"
            )
            return PHOTO
        else:
            await query.edit_message_text(
                "⚠️ No está en lista. ¿Cuánto cobras?\n_Solo el número, ej: 85000_",
                parse_mode="Markdown"
            )
            delivery["is_special"] = True
            return SPECIAL_PRICE
    else:
        delivery["is_special"] = True
        await query.edit_message_text(
            "📐 *Precio especial*\n\n¿Cuánto solicitas?\n_Solo el número, ej: 120000_\n\n"
            "⚠️ Requiere aprobación del supervisor.", parse_mode="Markdown"
        )
        return SPECIAL_PRICE


async def got_special_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().replace("$","").replace(".","").replace(",","").replace(" ","")
    try:
        price = float(raw)
        if price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Escribe solo el número. _Ejemplo: 120000_", parse_mode="Markdown")
        return SPECIAL_PRICE

    context.user_data["delivery"]["requested_price"] = price
    context.user_data["delivery"]["final_price"] = price
    await update.message.reply_text(
        f"💰 Precio solicitado: *{fmt_price(price)}*\n\nPaso 5️⃣ — Envía la *foto del producto* 📸",
        parse_mode="Markdown"
    )
    return PHOTO


async def got_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("❌ Por favor envía una *foto*, no un archivo.")
        return PHOTO

    delivery = context.user_data["delivery"]
    worker   = context.user_data["worker"]
    photo    = update.message.photo[-1]
    is_special = delivery.get("is_special", False)
    final_price = delivery["final_price"]
    oficio_label = delivery.get("oficio_label", "")

    file = await context.bot.get_file(photo.file_id)
    record = {
        "worker_id": worker["id"], "fve": delivery["fve"],
        "product_name": delivery["product_name"],
        "price_list_id": delivery.get("price_list_id"),
        "is_special": is_special,
        "requested_price": delivery.get("requested_price"),
        "final_price": final_price,
        "photo_file_id": photo.file_id, "photo_url": file.file_path,
        "status": "pending", "notes": f"Oficio: {oficio_label}",
        "created_at": datetime.utcnow().isoformat(),
    }
    result = supabase.table("deliveries").insert(record).execute()
    delivery_id = result.data[0]["id"]

    tipo_label = ("🔴 *PRECIO ESPECIAL* — requiere aprobación del valor"
                  if is_special else "🟢 Precio de lista — solo confirmar")
    caption = (
        f"🏭 *NUEVA ENTREGA*\n{'─'*28}\n"
        f"👷 *{worker['name']}*\n🏷️ Oficio: {oficio_label}\n"
        f"📋 FVE: `{delivery['fve']}`\n📦 *{delivery['product_name']}*\n"
        f"💰 *{fmt_price(final_price)}*\n{tipo_label}\n"
        f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M')}\n{'─'*28}\nID: `{delivery_id}`"
    )
    buttons = [[
        InlineKeyboardButton("✅ Aprobar", callback_data=f"approve_{delivery_id}"),
        InlineKeyboardButton("❌ Rechazar", callback_data=f"reject_{delivery_id}"),
    ]]
    if is_special:
        buttons.append([InlineKeyboardButton("✏️ Aprobar con otro precio", callback_data=f"modify_{delivery_id}")])

    for verifier in get_verifiers():
        try:
            await context.bot.send_photo(
                chat_id=verifier["telegram_id"], photo=photo.file_id,
                caption=caption, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"No se pudo notificar a {verifier['name']}: {e}")

    await update.message.reply_text(
        f"✅ *¡Reporte enviado!*\n\n🏷️ {oficio_label}\n📋 {delivery['fve']}\n"
        f"📦 {delivery['product_name']}\n💰 {fmt_price(final_price)}\n\n"
        f"{'⏳ *Esperando aprobación del precio especial.*' if is_special else '⏳ Esperando confirmación.'}\n"
        f"Te aviso cuando lo aprueben 👍", parse_mode="Markdown"
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Reporte cancelado.\nUsa /reportar para empezar de nuevo.",
                                     reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════
# CALLBACKS DE VERIFICADORES
# ═══════════════════════════════════════════════════════════════

async def handle_verification(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_verifier(query.from_user.id):
        await query.answer("❌ No tienes permisos.", show_alert=True)
        return

    action, delivery_id = query.data.split("_", 1)
    delivery_id = int(delivery_id)
    delivery = get_delivery(delivery_id)
    verifier = get_verifier(query.from_user.id)
    ver_name = verifier["name"] if verifier else query.from_user.first_name

    if not delivery:
        await query.edit_message_caption(caption=query.message.caption + "\n\n⚠️ Entrega no encontrada.", parse_mode="Markdown")
        return
    if delivery["status"] != "pending":
        status_map = {"approved": "✅ Ya aprobada", "rejected": "❌ Ya rechazada"}
        await query.answer(f"Ya procesada: {status_map.get(delivery['status'], delivery['status'])}", show_alert=True)
        return

    if action == "approve":
        supabase.table("deliveries").update({
            "status": "approved", "approved_by": ver_name,
            "approved_by_telegram_id": query.from_user.id,
            "approved_at": datetime.utcnow().isoformat(),
        }).eq("id", delivery_id).execute()
        worker = delivery.get("workers", {})
        try:
            await context.bot.send_message(
                chat_id=worker["telegram_id"],
                text=(f"✅ *¡Producto aprobado!*\n\n📋 FVE: {delivery['fve']}\n"
                      f"📦 {delivery['product_name']}\n💰 {fmt_price(delivery['final_price'])}\n"
                      f"👤 Aprobado por: *{ver_name}*"),
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"No se pudo notificar al contratista: {e}")
        await query.edit_message_caption(
            caption=query.message.caption + f"\n\n{'─'*28}\n✅ *APROBADO* por {ver_name}\n🕐 {datetime.now().strftime('%d/%m/%Y %H:%M')}",
            parse_mode="Markdown"
        )
    elif action == "reject":
        context.user_data["pending_rejection"] = delivery_id
        context.user_data["rejection_verifier"] = ver_name
        await query.edit_message_caption(
            caption=query.message.caption + f"\n\n{'─'*28}\n❌ Escribe el *motivo del rechazo*:",
            parse_mode="Markdown"
        )
    elif action == "modify":
        context.user_data["pending_modification"] = delivery_id
        context.user_data["mod_verifier"] = ver_name
        await query.edit_message_caption(
            caption=query.message.caption + f"\n\n{'─'*28}\n✏️ Escribe el *precio que apruebas* (ej: 95000):",
            parse_mode="Markdown"
        )


async def handle_verifier_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_verifier(update.effective_user.id):
        return
    user_data = context.user_data

    if "pending_rejection" in user_data:
        delivery_id = user_data.pop("pending_rejection")
        ver_name = user_data.pop("rejection_verifier", update.effective_user.first_name)
        reason = update.message.text.strip()
        delivery = get_delivery(delivery_id)
        supabase.table("deliveries").update({
            "status": "rejected", "rejection_reason": reason,
            "approved_by": ver_name, "approved_by_telegram_id": update.effective_user.id,
            "approved_at": datetime.utcnow().isoformat(),
        }).eq("id", delivery_id).execute()
        worker = delivery.get("workers", {})
        try:
            await context.bot.send_message(
                chat_id=worker["telegram_id"],
                text=(f"❌ *Producto rechazado*\n\n📋 FVE: {delivery['fve']}\n"
                      f"📦 {delivery['product_name']}\n💬 Motivo: _{reason}_\n"
                      f"👤 Rechazado por: *{ver_name}*"),
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"No se pudo notificar: {e}")
        await update.message.reply_text(f"❌ Entrega #{delivery_id} rechazada.\nMotivo: _{reason}_", parse_mode="Markdown")

    elif "pending_modification" in user_data:
        delivery_id = user_data.pop("pending_modification")
        ver_name = user_data.pop("mod_verifier", update.effective_user.first_name)
        raw = update.message.text.strip().replace("$","").replace(".","").replace(",","")
        try:
            new_price = float(raw)
        except ValueError:
            await update.message.reply_text("❌ Escribe solo el número, ej: 95000")
            user_data["pending_modification"] = delivery_id
            user_data["mod_verifier"] = ver_name
            return
        delivery = get_delivery(delivery_id)
        supabase.table("deliveries").update({
            "status": "approved", "final_price": new_price,
            "approved_by": ver_name, "approved_by_telegram_id": update.effective_user.id,
            "approved_at": datetime.utcnow().isoformat(),
            "notes": f"Precio modificado: {fmt_price(delivery['final_price'])} → {fmt_price(new_price)}",
        }).eq("id", delivery_id).execute()
        worker = delivery.get("workers", {})
        try:
            await context.bot.send_message(
                chat_id=worker["telegram_id"],
                text=(f"✅ *Aprobado con precio ajustado*\n\n📋 FVE: {delivery['fve']}\n"
                      f"📦 {delivery['product_name']}\n"
                      f"💰 Solicitado: {fmt_price(delivery.get('requested_price'))}\n"
                      f"💰 *Aprobado: {fmt_price(new_price)}*\n👤 Por: *{ver_name}*"),
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"No se pudo notificar: {e}")
        await update.message.reply_text(f"✅ Entrega #{delivery_id} aprobada con {fmt_price(new_price)}", parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════
# HANDLER IA — TEXTO LIBRE DE VERIFICADORES
# ═══════════════════════════════════════════════════════════════

async def send_ai_response(update: Update, respuesta: str):
    """Envía la respuesta de la IA. Intenta con Markdown; si falla, manda texto plano."""
    texto = f"🤖 {respuesta}" if respuesta else "⚠️ No pude procesar tu pregunta en este momento."
    try:
        await update.message.reply_text(texto, parse_mode="Markdown")
    except Exception as e:
        logger.warning(f"Reintentando sin Markdown: {e}")
        try:
            await update.message.reply_text(texto)
        except Exception as e2:
            logger.error(f"Error enviando respuesta: {e2}")
            await update.message.reply_text("⚠️ La respuesta no se pudo mostrar correctamente. Intenta de nuevo.")


async def handle_ai_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_verifier(update.effective_user.id):
        return

    text = update.message.text.strip()

    # Si hay flujo de verificación pendiente, procesarlo primero
    if "pending_rejection" in context.user_data or "pending_modification" in context.user_data:
        await handle_verifier_text(update, context)
        return

    palabras_clave = [
        "cuánto", "cuanto", "cuál", "cual", "quién", "quien",
        "qué", "que", "cómo", "como", "cuántos", "cuantos",
        "muéstrame", "muestrame", "dame", "dime", "lista",
        "resumen", "total", "semana", "quincena", "mes",
        "pendiente", "aprobad", "rechazad", "contratista",
        "cuántas", "cuantas", "hoy", "ayer", "producto",
        "entrega", "precio", "oficio",
    ]
    text_lower = text.lower()
    es_pregunta = any(kw in text_lower for kw in palabras_clave) or "?" in text

    # Si parece flujo de verificación específico, pasarlo a handle_verifier_text
    # Si no, dejar que la IA responda a todo
    if not es_pregunta and len(text) < 30 and any(
        kw in text_lower for kw in ["rechaz", "aprob", "modific"]
    ):
        await handle_verifier_text(update, context)
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    respuesta = await ask_gemini(text, update.effective_user.first_name)
    await send_ai_response(update, respuesta)


async def handle_voice_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recibe notas de voz de verificadores y responde con IA."""
    if not is_verifier(update.effective_user.id):
        return

    voice = update.message.voice
    if not voice:
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        file = await context.bot.get_file(voice.file_id)
        audio_bytes = await file.download_as_bytearray()
        mime_type = voice.mime_type or "audio/ogg"
        respuesta = await ask_gemini_audio(bytes(audio_bytes), update.effective_user.first_name, mime_type)
        await send_ai_response(update, respuesta)
    except Exception as e:
        logger.error(f"Error procesando audio: {e}")
        await update.message.reply_text("⚠️ No pude procesar la nota de voz.")


# ═══════════════════════════════════════════════════════════════
# /MISTOTAL
# ═══════════════════════════════════════════════════════════════

async def mis_total(update: Update, context: ContextTypes.DEFAULT_TYPE):
    worker = get_worker(update.effective_user.id)
    if not worker:
        await update.message.reply_text("❌ No estás registrado en el sistema.")
        return
    today = datetime.now()
    week_start = (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")
    r = supabase.table("deliveries").select("*").eq("worker_id", worker["id"])\
        .eq("status", "approved").gte("created_at", week_start).order("created_at").execute()
    deliveries = r.data
    total = sum(float(d["final_price"]) for d in deliveries)
    if not deliveries:
        await update.message.reply_text(f"📊 *Semana actual de {worker['name']}*\n\nAún no tienes entregas aprobadas.", parse_mode="Markdown")
        return
    lines = [f"📊 *Semana actual · {worker['name']}*\n"]
    for d in deliveries:
        label = "🔴" if d["is_special"] else "🟢"
        fecha = d["created_at"][:10]
        oficio_info = f" · {d['notes'].replace('Oficio: ','')}" if d.get("notes") and "Oficio:" in str(d.get("notes","")) else ""
        lines.append(f"{label} `{d['fve']}` — {d['product_name'][:30]}{oficio_info}")
        lines.append(f"   💰 {fmt_price(d['final_price'])} · {fecha}")
    lines.extend([f"\n{'─'*28}", f"💰 *TOTAL APROBADO: {fmt_price(total)}*", f"📦 Productos: {len(deliveries)}"])
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════
# /PENDIENTES
# ═══════════════════════════════════════════════════════════════

async def pendientes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_verifier(update.effective_user.id):
        await update.message.reply_text("❌ No tienes permisos.")
        return
    r = supabase.table("deliveries").select("*, workers(name, area)").eq("status", "pending").order("created_at").execute()
    items = r.data
    if not items:
        await update.message.reply_text("✅ No hay entregas pendientes.")
        return
    lines = [f"⏳ *Entregas pendientes ({len(items)})*\n"]
    for d in items:
        w = d.get("workers", {})
        label = "🔴 ESPECIAL" if d["is_special"] else "🟢 lista"
        fecha = d["created_at"][:16].replace("T", " ")
        oficio_info = f"\n  🏷️ {d['notes']}" if d.get("notes") and "Oficio:" in str(d.get("notes","")) else ""
        lines.append(f"• *{w.get('name','?')}*\n  `{d['fve']}` — {d['product_name'][:35]}\n  {fmt_price(d['final_price'])} · {label} · {fecha}{oficio_info}\n  ID: `{d['id']}`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════
# /RESUMEN
# ═══════════════════════════════════════════════════════════════

async def resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_verifier(update.effective_user.id):
        await update.message.reply_text("❌ No tienes permisos.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("📋 *Uso:* `/resumen [nombre] [semana|quincena]`", parse_mode="Markdown")
        return
    worker_name = args[0]
    period = args[1].lower() if len(args) > 1 else "semana"
    days_back = 15 if period == "quincena" else 7
    start_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    wr = supabase.table("workers").select("*").ilike("name", f"%{worker_name}%").execute()
    if not wr.data:
        await update.message.reply_text(f"❌ No encontré contratista '{worker_name}'.")
        return
    worker = wr.data[0]
    r = supabase.table("deliveries").select("*").eq("worker_id", worker["id"])\
        .eq("status", "approved").gte("created_at", start_date).order("created_at").execute()
    deliveries = r.data
    total = sum(float(d["final_price"]) for d in deliveries)
    lines = [f"🧾 *CUENTA DE COBRO*", f"👷 {worker['name']}", f"📅 {start_date} → hoy", f"{'─'*30}"]
    if not deliveries:
        lines.append("⚠️ No hay entregas aprobadas en este período.")
    else:
        for d in deliveries:
            label = "🔴" if d["is_special"] else "🟢"
            oficio_info = f" · {d['notes'].replace('Oficio: ','')}" if d.get("notes") and "Oficio:" in str(d.get("notes","")) else ""
            lines.append(f"\n{label} *{d['product_name']}*{oficio_info}\n   FVE: `{d['fve']}` · {d['created_at'][:10]}\n   💰 {fmt_price(d['final_price'])}{'  _(especial)_' if d['is_special'] else ''}\n   ✅ {d.get('approved_by','?')}")
    lines.extend([f"\n{'─'*30}", f"📦 Entregas: {len(deliveries)}", f"💰 *TOTAL: {fmt_price(total)}*"])
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════
# /PRECIOS
# ═══════════════════════════════════════════════════════════════

async def precios(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_verifier(update.effective_user.id):
        await update.message.reply_text("❌ Solo verificadores pueden consultar precios.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("🔍 Usa `/precios [término]`\n_Ejemplo:_ `/precios sofa cama`", parse_mode="Markdown")
        return
    term = " ".join(args)
    r = supabase.table("price_list").select("*").eq("active", True).ilike("product_name", f"%{term}%").execute()
    if not r.data:
        await update.message.reply_text(f"❌ No encontré precios para '{term}'.")
        return
    lines = [f"📋 *Precios para '{term}'*\n"]
    for p in r.data[:12]:
        oficio_label = OFICIOS.get(p.get("oficio", ""), p.get("oficio", ""))
        detalle = (f"Corte: {fmt_price(p.get('precio_corte'))} · Costura: {fmt_price(p.get('precio_costura'))} · "
                   if p.get("oficio") == "corte_costura" and p.get("precio_corte") else "")
        lines.append(f"• *{p['product_name']}*\n  {oficio_label}\n  {detalle}*Total: {fmt_price(p['precio_total'])}*")
    if len(r.data) > 12:
        lines.append(f"\n_...y {len(r.data)-12} más._")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
# LÓGICA DE PAGOS Y CUENTAS DE COBRO
# ═══════════════════════════════════════════════════════════════

def get_pending_deliveries_for_worker(worker_id: int):
    """Todas las entregas aprobadas de un contratista (ya no se marcan individualmente como pagadas)."""
    try:
        return supabase.table("deliveries")\
            .select("*")\
            .eq("worker_id", worker_id)\
            .eq("status", "approved")\
            .order("created_at", desc=False)\
            .execute().data or []
    except Exception as e:
        logger.error(f"Error obteniendo entregas aprobadas: {e}")
        return []


def get_total_pagado(worker_id: int) -> float:
    """Suma de todo lo que se le ha pagado a un contratista históricamente."""
    try:
        pagos = supabase.table("payments")\
            .select("monto_pagado")\
            .eq("worker_id", worker_id)\
            .execute().data or []
        return sum(float(p.get("monto_pagado", 0) or 0) for p in pagos)
    except Exception as e:
        logger.error(f"Error sumando pagos: {e}")
        return 0.0


def get_cuenta_actual(worker_id: int):
    """Resumen de la cuenta de cobro actual.
    Saldo = total facturado (todas las entregas aprobadas) - total pagado.
    Retorna: {entregas, total_entregas, total_pagado, total_a_cobrar}
    """
    entregas = get_pending_deliveries_for_worker(worker_id)
    total_entregas = sum(float(d.get("final_price", 0) or 0) for d in entregas)
    total_pagado = get_total_pagado(worker_id)
    total_a_cobrar = max(0.0, total_entregas - total_pagado)
    return {
        "entregas": entregas,
        "total_entregas": total_entregas,
        "total_pagado": total_pagado,
        "saldo_previo": 0.0,  # ya no se usa, se mantiene para compatibilidad
        "total_a_cobrar": total_a_cobrar,
    }


def get_workers_with_pending_balance():
    """Contratistas con entregas aprobadas sin pagar o saldo previo > 0."""
    try:
        all_workers = supabase.table("workers").select("*").eq("activo", True).execute().data or []
        result = []
        for w in all_workers:
            cuenta = get_cuenta_actual(w["id"])
            if cuenta["total_a_cobrar"] > 0:
                result.append({**w, "cuenta": cuenta})
        return result
    except Exception as e:
        logger.error(f"Error listando contratistas con saldo: {e}")
        return []


# ═══════════════════════════════════════════════════════════════
# COMANDO /pagar — registrar pago a un contratista
# ═══════════════════════════════════════════════════════════════

async def pagar_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_verifier(update.effective_user.id):
        await update.message.reply_text("⚠️ Solo los verificadores pueden registrar pagos.")
        return ConversationHandler.END

    workers = get_workers_with_pending_balance()
    if not workers:
        await update.message.reply_text("✅ No hay contratistas con saldo por pagar en este momento.")
        return ConversationHandler.END

    keyboard = []
    for w in workers:
        total = w["cuenta"]["total_a_cobrar"]
        keyboard.append([InlineKeyboardButton(
            f"{w['name']} — {fmt_price(total)}",
            callback_data=f"pay_w_{w['id']}"
        )])
    keyboard.append([InlineKeyboardButton("❌ Cancelar", callback_data="pay_cancel")])

    await update.message.reply_text(
        "💰 *Registrar pago a contratista*\n\nElige a quién le vas a pagar:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return PAY_WORKER


async def pagar_got_worker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "pay_cancel":
        await query.edit_message_text("❌ Pago cancelado.")
        return ConversationHandler.END

    worker_id = int(query.data.replace("pay_w_", ""))
    worker = supabase.table("workers").select("*").eq("id", worker_id).execute().data
    if not worker:
        await query.edit_message_text("⚠️ Contratista no encontrado.")
        return ConversationHandler.END

    worker = worker[0]
    cuenta = get_cuenta_actual(worker_id)

    context.user_data["pay_worker_id"] = worker_id
    context.user_data["pay_worker_name"] = worker["name"]
    context.user_data["pay_cuenta"] = cuenta

    detalle = f"💼 *Cuenta de cobro de {worker['name']}*\n\n"
    detalle += f"💵 Total facturado (entregas aprobadas): {fmt_price(cuenta['total_entregas'])}\n"
    if cuenta["total_pagado"] > 0:
        detalle += f"💸 Total ya pagado: {fmt_price(cuenta['total_pagado'])}\n"
    detalle += f"\n*TOTAL A COBRAR: {fmt_price(cuenta['total_a_cobrar'])}*\n\n"

    if cuenta["entregas"]:
        detalle += "_Últimas entregas:_\n"
        for d in cuenta["entregas"][-8:]:
            fecha = d["created_at"][:10]
            detalle += f"  • {fecha} — {d['product_name'][:35]} — {fmt_price(float(d.get('final_price', 0) or 0))}\n"

    detalle += f"\n¿Cuánto le vas a pagar? Escribe el monto en números (ej: `{int(cuenta['total_a_cobrar'])}` para pago total o menos para abono):"

    await query.edit_message_text(detalle, parse_mode="Markdown")
    return PAY_AMOUNT


async def pagar_got_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", "").replace(".", "").replace("$", "").replace(" ", "")
    try:
        monto = float(text)
        if monto <= 0:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("⚠️ Monto inválido. Escribe solo números (ej: 500000).")
        return PAY_AMOUNT

    cuenta = context.user_data.get("pay_cuenta", {})
    total_a_cobrar = cuenta.get("total_a_cobrar", 0)

    if monto > total_a_cobrar:
        await update.message.reply_text(
            f"⚠️ El monto ({fmt_price(monto)}) es mayor a lo que se debe ({fmt_price(total_a_cobrar)}). "
            "Escribe un valor igual o menor."
        )
        return PAY_AMOUNT

    saldo_pendiente = total_a_cobrar - monto
    context.user_data["pay_monto"] = monto
    context.user_data["pay_saldo_pendiente"] = saldo_pendiente

    tipo = "TOTAL ✅" if saldo_pendiente == 0 else f"ABONO PARCIAL (queda debiendo {fmt_price(saldo_pendiente)})"

    resumen_txt = (
        f"📋 *Confirma el pago a {context.user_data['pay_worker_name']}*\n\n"
        f"Total a cobrar: {fmt_price(total_a_cobrar)}\n"
        f"Monto a pagar:  *{fmt_price(monto)}*\n"
        f"Tipo: {tipo}\n"
    )
    if saldo_pendiente > 0:
        resumen_txt += f"\n💡 El saldo de {fmt_price(saldo_pendiente)} se arrastrará a la próxima cuenta de cobro."

    keyboard = [
        [InlineKeyboardButton("✅ Confirmar pago", callback_data="pay_confirm_yes")],
        [InlineKeyboardButton("❌ Cancelar",       callback_data="pay_confirm_no")],
    ]
    await update.message.reply_text(resumen_txt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return PAY_CONFIRM


async def pagar_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "pay_confirm_no":
        await query.edit_message_text("❌ Pago cancelado.")
        context.user_data.clear()
        return ConversationHandler.END

    worker_id = context.user_data.get("pay_worker_id")
    worker_name = context.user_data.get("pay_worker_name")
    cuenta = context.user_data.get("pay_cuenta")
    monto = context.user_data.get("pay_monto")
    saldo_pendiente = context.user_data.get("pay_saldo_pendiente")
    verifier = get_verifier(update.effective_user.id)

    try:
        # Crear el registro del pago
        payment = supabase.table("payments").insert({
            "worker_id":       worker_id,
            "total_facturado": cuenta["total_a_cobrar"],
            "monto_pagado":    monto,
            "saldo_pendiente": saldo_pendiente,
            "saldo_previo":    cuenta["saldo_previo"],
            "registrado_por":  verifier["id"] if verifier else None,
        }).execute().data[0]

        respuesta = (
            f"✅ *Pago registrado*\n\n"
            f"👤 {worker_name}\n"
            f"💵 Pagado: {fmt_price(monto)}\n"
            f"📊 Cuenta de cobro saldada: {fmt_price(cuenta['total_a_cobrar'])}\n"
        )
        if saldo_pendiente > 0:
            respuesta += f"\n📌 Saldo pendiente arrastrado: *{fmt_price(saldo_pendiente)}*"
        else:
            respuesta += "\n🎉 ¡Cuenta totalmente saldada!"

        await query.edit_message_text(respuesta, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error registrando pago: {e}")
        await query.edit_message_text(f"⚠️ Error al registrar el pago: {e}")

    context.user_data.clear()
    return ConversationHandler.END


async def pagar_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Pago cancelado.")
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════
# COMANDO /cuenta — consultar saldo actual
# ═══════════════════════════════════════════════════════════════

async def cuenta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Si es contratista, ve su propia cuenta
    worker = get_worker(user_id)
    if worker:
        cuenta = get_cuenta_actual(worker["id"])
        msg = f"💼 *Tu cuenta de cobro actual*\n\n"
        msg += f"💵 Total facturado: {fmt_price(cuenta['total_entregas'])}\n"
        if cuenta["total_pagado"] > 0:
            msg += f"💸 Ya recibido: {fmt_price(cuenta['total_pagado'])}\n"
        msg += f"\n*TOTAL POR COBRAR: {fmt_price(cuenta['total_a_cobrar'])}*\n"

        if cuenta["entregas"]:
            msg += "\n_Últimas entregas:_\n"
            for d in cuenta["entregas"][-10:]:
                fecha = d["created_at"][:10]
                msg += f"  • {fecha} — {d['product_name'][:35]} — {fmt_price(float(d.get('final_price', 0) or 0))}\n"

        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    # Si es verificador, ve cuenta de todos los que tienen saldo
    if is_verifier(user_id):
        workers = get_workers_with_pending_balance()
        if not workers:
            await update.message.reply_text("✅ Ningún contratista tiene saldo por cobrar en este momento.")
            return

        msg = "💼 *Cuentas de cobro pendientes*\n\n"
        total_general = 0
        for w in workers:
            c = w["cuenta"]
            total_general += c["total_a_cobrar"]
            msg += f"👤 *{w['name']}*\n"
            msg += f"   Facturado: {fmt_price(c['total_entregas'])}"
            if c["total_pagado"] > 0:
                msg += f" — Pagado: {fmt_price(c['total_pagado'])}"
            msg += f"\n   *Por cobrar: {fmt_price(c['total_a_cobrar'])}*\n\n"

        msg += f"━━━━━━━━━━━━━━━━━\n*Total general por pagar: {fmt_price(total_general)}*"
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    await update.message.reply_text("⚠️ No estás registrado en el sistema.")


# ═══════════════════════════════════════════════════════════════
# COMANDO /historial — pagos pasados
# ═══════════════════════════════════════════════════════════════

async def historial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    worker = get_worker(user_id)
    es_verificador = is_verifier(user_id)

    if not worker and not es_verificador:
        await update.message.reply_text("⚠️ No estás registrado en el sistema.")
        return

    # Contratista: ve su propio historial
    if worker and not es_verificador:
        pagos = supabase.table("payments")\
            .select("*")\
            .eq("worker_id", worker["id"])\
            .order("created_at", desc=True)\
            .limit(20)\
            .execute().data or []
        await _mostrar_historial(update, worker["name"], pagos)
        return

    # Verificador: si pasa argumento, ve el de ese contratista; si no, lista general
    args = context.args
    if args:
        nombre_busqueda = " ".join(args).lower()
        workers_all = supabase.table("workers").select("*").execute().data or []
        match = next((w for w in workers_all if nombre_busqueda in w["name"].lower()), None)
        if not match:
            await update.message.reply_text(f"⚠️ No encontré un contratista con nombre similar a '{nombre_busqueda}'.")
            return
        pagos = supabase.table("payments")\
            .select("*")\
            .eq("worker_id", match["id"])\
            .order("created_at", desc=True)\
            .limit(20)\
            .execute().data or []
        await _mostrar_historial(update, match["name"], pagos)
        return

    # Verificador sin argumento: historial general de últimos 20 pagos
    pagos = supabase.table("payments")\
        .select("*, workers(name)")\
        .order("created_at", desc=True)\
        .limit(20)\
        .execute().data or []
    if not pagos:
        await update.message.reply_text("📭 Aún no se han registrado pagos.")
        return
    msg = "📜 *Últimos 20 pagos registrados*\n\n"
    for p in pagos:
        fecha = p["created_at"][:10]
        nombre = (p.get("workers") or {}).get("name", "?")
        msg += f"• {fecha} — {nombre} — {fmt_price(float(p['monto_pagado']))}"
        if float(p.get("saldo_pendiente", 0)) > 0:
            msg += f" _(quedó {fmt_price(float(p['saldo_pendiente']))})_"
        msg += "\n"
    msg += "\n💡 Para ver el historial de un contratista específico: `/historial <nombre>`"
    await update.message.reply_text(msg, parse_mode="Markdown")


async def _mostrar_historial(update: Update, nombre: str, pagos: list):
    if not pagos:
        await update.message.reply_text(f"📭 {nombre} aún no tiene pagos registrados.")
        return
    msg = f"📜 *Historial de pagos de {nombre}*\n\n"
    total_recibido = 0.0
    for p in pagos:
        fecha = p["created_at"][:10]
        monto = float(p["monto_pagado"])
        total_recibido += monto
        msg += f"• {fecha}\n"
        msg += f"  Facturado: {fmt_price(float(p['total_facturado']))}\n"
        msg += f"  Pagado:    *{fmt_price(monto)}*\n"
        if float(p.get("saldo_pendiente", 0)) > 0:
            msg += f"  Saldo:     {fmt_price(float(p['saldo_pendiente']))} _(arrastrado)_\n"
        msg += "\n"
    msg += f"━━━━━━━━━━━━━━━━━\n💰 *Total recibido históricamente: {fmt_price(total_recibido)}*"
    await update.message.reply_text(msg, parse_mode="Markdown")


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("reportar", reportar_start)],
        states={
            OFICIO:        [CallbackQueryHandler(got_oficio,      pattern=r"^oficio_")],
            FVE:           [MessageHandler(filters.TEXT & ~filters.COMMAND, got_fve)],
            PRODUCT_NAME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, got_product_name)],
            PRICE_TYPE:    [CallbackQueryHandler(got_price_type,  pattern=r"^type_")],
            SPECIAL_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_special_price)],
            PHOTO:         [MessageHandler(filters.PHOTO, got_photo)],
        },
        fallbacks=[CommandHandler("cancelar", cancel)],
        allow_reentry=True,
    )

    pay_conv = ConversationHandler(
        entry_points=[CommandHandler("pagar", pagar_start)],
        states={
            PAY_WORKER:  [CallbackQueryHandler(pagar_got_worker,  pattern=r"^pay_(w_|cancel)")],
            PAY_AMOUNT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, pagar_got_amount)],
            PAY_CONFIRM: [CallbackQueryHandler(pagar_confirm,     pattern=r"^pay_confirm_")],
        },
        fallbacks=[CommandHandler("cancelar", pagar_cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(pay_conv)
    app.add_handler(CommandHandler("start",      start))
    app.add_handler(CommandHandler("mistotal",   mis_total))
    app.add_handler(CommandHandler("pendientes", pendientes))
    app.add_handler(CommandHandler("resumen",    resumen))
    app.add_handler(CommandHandler("precios",    precios))
    app.add_handler(CommandHandler("cuenta",     cuenta))
    app.add_handler(CommandHandler("historial",  historial))
    app.add_handler(CallbackQueryHandler(handle_verification, pattern=r"^(approve|reject|modify)_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ai_question))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice_question))

    logger.info("🏭 Bot Livinghouse iniciado y escuchando...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
