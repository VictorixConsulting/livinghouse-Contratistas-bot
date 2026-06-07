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
    ReplyKeyboardRemove, ReplyKeyboardMarkup
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

KB_CONTRATISTA = ReplyKeyboardMarkup(
    [["📋 Mis trabajos"], ["✅ Terminé uno", "💰 Mi cuenta"]],
    resize_keyboard=True, is_persistent=True
)

OFICIO, FVE, PRODUCT_NAME, PICK_PRODUCT, PRICE_TYPE, SPECIAL_PRICE, PHOTO = range(7)
PAY_WORKER, PAY_AMOUNT, PAY_CONFIRM = range(10, 13)
CLOSE_WORKER, CLOSE_CONFIRM = range(20, 22)
TFIN_PICK, TFIN_FOTO = range(30, 32)
PREREQS_BOT = {
    "esqueleteria": [], "carpinteria": [], "corte": [], "costura": [],
    "pintado": ["carpinteria", "esqueleteria"],
    "tapizado": ["esqueleteria", "carpinteria", "corte", "costura", "pintado"],
}


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

def find_prices(product_name: str, oficio: str):
    """Devuelve todas las coincidencias de productos en el oficio.
    1) Match exacto → solo esa
    2) Coincidencia parcial por palabras clave → todas las que coincidan
    """
    # 1) Match exacto
    r = supabase.table("price_list").select("*").eq("active", True)\
        .eq("oficio", oficio).ilike("product_name", product_name).execute()
    if r.data:
        return r.data

    # 2) Coincidencia parcial: buscar por palabras clave del nombre
    matches = {}  # usamos dict por id para evitar duplicados
    for kw in product_name.upper().split():
        if len(kw) < 3:
            continue
        r = supabase.table("price_list").select("*").eq("active", True)\
            .eq("oficio", oficio).ilike("product_name", f"%{kw}%").execute()
        for item in (r.data or []):
            matches[item["id"]] = item

    return list(matches.values())


def find_price(product_name: str, oficio: str):
    """Compatibilidad: devuelve solo la primera coincidencia."""
    results = find_prices(product_name, oficio)
    return results[0] if results else None

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
        "   El contratista reporta una entrega terminada. Selecciona oficio, FVE, producto, precio y foto. "
        "La entrega queda 'pendiente' hasta que un verificador la apruebe.\n\n"
        "2) VERIFICACIÓN (comando /pendientes):\n"
        "   Verificadores aprueban, rechazan o modifican entregas.\n\n"
        "3) PRODUCCIÓN EN CURSO:\n"
        "   Cada entrega aprobada se suma a la 'producción en curso' del contratista. "
        "Esto NO es deuda todavía — es solo trabajo acumulado pendiente de cierre formal.\n\n"
        "4) CIERRE DE PRODUCCIÓN (comando /cierre, solo verificadores):\n"
        "   Cuando el verificador decide cerrar un período (semanal, quincenal, cuando quiera), "
        "toda la producción en curso se convierte en DEUDA FORMAL. La producción en curso vuelve a cero "
        "y empieza a acumular de nuevo. La deuda formal es lo que oficialmente se le debe al contratista.\n\n"
        "5) PAGO (comando /pagar, solo verificadores):\n"
        "   Los pagos SOLO se hacen sobre la deuda formal (lo ya cerrado). NO se puede pagar producción "
        "en curso que aún no ha sido cerrada. Pueden ser pagos totales o abonos parciales. "
        "El saldo no cubierto queda como deuda hasta el siguiente pago.\n\n"
        "DOS CONCEPTOS CLAVE QUE NUNCA DEBES CONFUNDIR:\n"
        "  🟢 Producción en curso = lo que va acumulado desde el último cierre (no es deuda aún)\n"
        "  📌 Deuda formal = lo que ya fue cerrado y aún no se ha pagado (sí es lo que se le debe)\n\n"
        "COMANDOS DISPONIBLES ACTUALMENTE:\n"
        "  • /reportar - registrar una entrega (contratistas)\n"
        "  • /pendientes - revisar entregas por aprobar (verificadores)\n"
        "  • /resumen - resumen general de producción\n"
        "  • /precios - lista de precios por oficio\n"
        "  • /cuenta - estado completo: producción en curso + deuda formal + total pagado\n"
        "  • /produccion - producción en curso del período actual (desde último cierre)\n"
        "  • /produccion anteriores - lista de cierres pasados con fechas y montos\n"
        "  • /produccion <nombre> - producción en curso de un contratista específico (verificadores)\n"
        "  • /cierre - cerrar la producción de un contratista, convirtiéndola en deuda formal (verificadores)\n"
        "  • /pagar - pagar sobre la deuda formal (verificadores)\n"
        "  • /historial - pagos pasados realizados\n\n"
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
            f"Usa los botones de abajo 👇\n"
            f"• 📋 *Mis trabajos* — lo que tienes asignado, en orden\n"
            f"• ✅ *Terminé uno* — marcar un trabajo terminado\n"
            f"• 💰 *Mi cuenta* — tu producción, deuda y pagos\n\n"
            f"_Livinghouse · Sistema de producción_",
            parse_mode="Markdown",
            reply_markup=KB_CONTRATISTA
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
    matches = find_prices(product_name, oficio)

    # CASO 1: varias coincidencias → mostrar lista para elegir
    if len(matches) > 1:
        context.user_data["price_matches"] = matches
        buttons = []
        for i, m in enumerate(matches[:10]):  # máximo 10 botones
            label = f"{m['product_name'][:35]} — {fmt_price(m['precio_total'])}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"pick_{i}")])
        buttons.append([InlineKeyboardButton("📐 Ninguno, medida especial", callback_data="pick_none")])
        await update.message.reply_text(
            f"📦 *{product_name}*\n🏷️ {oficio_label}\n\n"
            f"Encontré {len(matches)} productos que coinciden. ¿Cuál es?",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown"
        )
        return PICK_PRODUCT

    # CASO 2: una sola coincidencia → seguir flujo normal
    price_match = matches[0] if matches else None
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
        msg = (f"📦 *{price_match['product_name']}*\n🏷️ {oficio_label}\n\n💡 Precio en lista:\n{detalle}\n\n"
               f"Paso 4️⃣ — ¿Precio de lista o medida especial?")
    else:
        msg = (f"📦 *{product_name}*\n🏷️ {oficio_label}\n\n"
               f"⚠️ No está en la lista de {oficio_label}.\n\nPaso 4️⃣ — ¿Precio de lista o medida especial?")

    await update.message.reply_text(msg, reply_markup=keyboard, parse_mode="Markdown")
    return PRICE_TYPE


async def got_picked_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cuando hay varias coincidencias y el usuario elige una."""
    query = update.callback_query
    await query.answer()
    delivery = context.user_data["delivery"]
    oficio_label = delivery["oficio_label"]

    if query.data == "pick_none":
        # No coincide ninguna → ir a medida especial
        delivery["price_match"] = None
        await query.edit_message_text(
            f"📐 *Medida especial*\n\n"
            f"¿Cuánto cobras por *{delivery['product_name']}*?\n_Solo el número, ej: 85000_",
            parse_mode="Markdown"
        )
        return SPECIAL_PRICE

    # Usuario eligió un producto específico
    idx = int(query.data.replace("pick_", ""))
    matches = context.user_data.get("price_matches", [])
    if idx >= len(matches):
        await query.edit_message_text("⚠️ Selección inválida. Usa /cancelar y vuelve a intentar.")
        return ConversationHandler.END

    chosen = matches[idx]
    delivery["price_match"] = chosen
    delivery["product_name"] = chosen["product_name"]  # actualizar al nombre real

    oficio = delivery["oficio"]
    if oficio == "corte_costura" and chosen.get("precio_corte"):
        detalle = (f"   ├ Corte:   {fmt_price(chosen.get('precio_corte'))}\n"
                  f"   ├ Costura: {fmt_price(chosen.get('precio_costura'))}\n"
                  f"   └ *Total:  {fmt_price(chosen['precio_total'])}*")
    else:
        detalle = f"   └ *Total: {fmt_price(chosen['precio_total'])}*"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋  Precio de lista", callback_data="type_standard")],
        [InlineKeyboardButton("📐  Medida especial", callback_data="type_special")],
    ])
    await query.edit_message_text(
        f"📦 *{chosen['product_name']}*\n🏷️ {oficio_label}\n\n💡 Precio en lista:\n{detalle}\n\n"
        f"Paso 4️⃣ — ¿Precio de lista o medida especial?",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
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
                                     reply_markup=KB_CONTRATISTA if get_worker(update.effective_user.id) else ReplyKeyboardRemove())
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

def get_produccion_en_curso(worker_id: int):
    """Entregas aprobadas que aún NO están en un cierre. Producción acumulada en curso."""
    try:
        return supabase.table("deliveries")\
            .select("*")\
            .eq("worker_id", worker_id)\
            .eq("status", "approved")\
            .is_("closure_id", "null")\
            .order("created_at", desc=False)\
            .execute().data or []
    except Exception as e:
        logger.error(f"Error obteniendo producción en curso: {e}")
        return []


def get_total_cerrado(worker_id: int) -> float:
    """Suma de todos los cierres formales que ha tenido un contratista."""
    try:
        cierres = supabase.table("closures")\
            .select("monto")\
            .eq("worker_id", worker_id)\
            .execute().data or []
        return sum(float(c.get("monto", 0) or 0) for c in cierres)
    except Exception as e:
        logger.error(f"Error sumando cierres: {e}")
        return 0.0


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


def get_deuda_formal(worker_id: int) -> float:
    """Deuda formal = total cerrado - total pagado. Es lo que oficialmente se le debe."""
    return max(0.0, get_total_cerrado(worker_id) - get_total_pagado(worker_id))


def get_estado_contratista(worker_id: int):
    """Estado completo de un contratista:
       - produccion_en_curso: entregas aprobadas aún sin cerrar
       - total_produccion_en_curso: suma de eso
       - deuda_formal: lo que se le debe oficialmente (ya cerrado y no pagado)
       - total_cerrado, total_pagado
    """
    entregas = get_produccion_en_curso(worker_id)
    total_produccion = sum(float(d.get("final_price", 0) or 0) for d in entregas)
    total_cerrado = get_total_cerrado(worker_id)
    total_pagado = get_total_pagado(worker_id)
    deuda_formal = max(0.0, total_cerrado - total_pagado)
    return {
        "produccion_en_curso":        entregas,
        "total_produccion_en_curso":  total_produccion,
        "total_cerrado":              total_cerrado,
        "total_pagado":               total_pagado,
        "deuda_formal":               deuda_formal,
    }


def get_workers_with_deuda_formal():
    """Contratistas con deuda formal > 0 (algo cerrado y aún sin pagar)."""
    try:
        all_workers = supabase.table("workers").select("*").eq("activo", True).execute().data or []
        result = []
        for w in all_workers:
            estado = get_estado_contratista(w["id"])
            if estado["deuda_formal"] > 0:
                result.append({**w, "estado": estado})
        return result
    except Exception as e:
        logger.error(f"Error listando contratistas con deuda: {e}")
        return []


def get_workers_with_produccion_en_curso():
    """Contratistas con producción acumulada aún sin cerrar."""
    try:
        all_workers = supabase.table("workers").select("*").eq("activo", True).execute().data or []
        result = []
        for w in all_workers:
            estado = get_estado_contratista(w["id"])
            if estado["total_produccion_en_curso"] > 0:
                result.append({**w, "estado": estado})
        return result
    except Exception as e:
        logger.error(f"Error listando contratistas con producción: {e}")
        return []


# ═══════════════════════════════════════════════════════════════
# COMANDO /cierre — convertir producción en curso en deuda formal
# ═══════════════════════════════════════════════════════════════

async def cierre_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_verifier(update.effective_user.id):
        await update.message.reply_text("⚠️ Solo los verificadores pueden hacer cierres.")
        return ConversationHandler.END

    workers = get_workers_with_produccion_en_curso()
    if not workers:
        await update.message.reply_text("✅ No hay contratistas con producción en curso por cerrar.")
        return ConversationHandler.END

    keyboard = []
    for w in workers:
        total = w["estado"]["total_produccion_en_curso"]
        keyboard.append([InlineKeyboardButton(
            f"{w['name']} — {fmt_price(total)}",
            callback_data=f"close_w_{w['id']}"
        )])
    keyboard.append([InlineKeyboardButton("❌ Cancelar", callback_data="close_cancel")])

    await update.message.reply_text(
        "📋 *Cierre de producción*\n\n"
        "Esto convierte la producción acumulada en deuda formal "
        "(el contratista podrá cobrarla y empieza un período nuevo).\n\n"
        "¿De quién vas a cerrar la producción?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return CLOSE_WORKER


async def cierre_got_worker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "close_cancel":
        await query.edit_message_text("❌ Cierre cancelado.")
        return ConversationHandler.END

    worker_id = int(query.data.replace("close_w_", ""))
    worker = supabase.table("workers").select("*").eq("id", worker_id).execute().data
    if not worker:
        await query.edit_message_text("⚠️ Contratista no encontrado.")
        return ConversationHandler.END

    worker = worker[0]
    estado = get_estado_contratista(worker_id)

    if estado["total_produccion_en_curso"] <= 0:
        await query.edit_message_text("⚠️ Este contratista no tiene producción en curso por cerrar.")
        return ConversationHandler.END

    context.user_data["close_worker_id"]   = worker_id
    context.user_data["close_worker_name"] = worker["name"]
    context.user_data["close_estado"]      = estado

    detalle = (
        f"📋 *Confirmar cierre — {worker['name']}*\n\n"
        f"📦 Entregas a cerrar: {len(estado['produccion_en_curso'])}\n"
        f"💵 Total a convertir en deuda: *{fmt_price(estado['total_produccion_en_curso'])}*\n"
    )
    if estado["deuda_formal"] > 0:
        nueva_deuda = estado["deuda_formal"] + estado["total_produccion_en_curso"]
        detalle += f"📌 Deuda actual: {fmt_price(estado['deuda_formal'])}\n"
        detalle += f"📌 Deuda después del cierre: *{fmt_price(nueva_deuda)}*\n"

    detalle += "\nUna vez confirmado, este cierre no se puede deshacer fácilmente. ¿Continuar?"

    keyboard = [
        [InlineKeyboardButton("✅ Confirmar cierre", callback_data="close_confirm_yes")],
        [InlineKeyboardButton("❌ Cancelar",         callback_data="close_confirm_no")],
    ]
    await query.edit_message_text(detalle, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return CLOSE_CONFIRM


async def cierre_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "close_confirm_no":
        await query.edit_message_text("❌ Cierre cancelado.")
        context.user_data.clear()
        return ConversationHandler.END

    worker_id   = context.user_data.get("close_worker_id")
    worker_name = context.user_data.get("close_worker_name")
    estado      = context.user_data.get("close_estado")
    verifier    = get_verifier(update.effective_user.id)

    try:
        # Determinar fecha_inicio del período: fin del último cierre o created_at de la primera entrega
        ultimo_cierre = supabase.table("closures")\
            .select("fecha_fin")\
            .eq("worker_id", worker_id)\
            .order("fecha_fin", desc=True)\
            .limit(1)\
            .execute().data
        if ultimo_cierre:
            fecha_inicio = ultimo_cierre[0]["fecha_fin"]
        else:
            fecha_inicio = estado["produccion_en_curso"][0]["created_at"]

        # Crear el cierre
        closure = supabase.table("closures").insert({
            "worker_id":      worker_id,
            "monto":          estado["total_produccion_en_curso"],
            "fecha_inicio":   fecha_inicio,
            "registrado_por": verifier["id"] if verifier else None,
        }).execute().data[0]

        # Asociar todas las entregas en curso a este cierre
        for d in estado["produccion_en_curso"]:
            supabase.table("deliveries").update({"closure_id": closure["id"]}).eq("id", d["id"]).execute()

        nueva_deuda = estado["deuda_formal"] + estado["total_produccion_en_curso"]
        respuesta = (
            f"✅ *Cierre registrado*\n\n"
            f"👤 {worker_name}\n"
            f"📦 Entregas cerradas: {len(estado['produccion_en_curso'])}\n"
            f"💵 Monto cerrado: {fmt_price(estado['total_produccion_en_curso'])}\n\n"
            f"📌 *Deuda formal actualizada: {fmt_price(nueva_deuda)}*"
        )
        await query.edit_message_text(respuesta, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error en cierre: {e}")
        await query.edit_message_text(f"⚠️ Error al registrar el cierre: {e}")

    context.user_data.clear()
    return ConversationHandler.END


async def cierre_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Cierre cancelado.")
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════
# COMANDO /pagar — registrar pago sobre DEUDA FORMAL únicamente
# ═══════════════════════════════════════════════════════════════

async def pagar_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_verifier(update.effective_user.id):
        await update.message.reply_text("⚠️ Solo los verificadores pueden registrar pagos.")
        return ConversationHandler.END

    workers = get_workers_with_deuda_formal()
    if not workers:
        await update.message.reply_text(
            "✅ No hay contratistas con deuda formal por pagar.\n\n"
            "Recuerda: solo se puede pagar sobre producción que ya haya sido cerrada con /cierre."
        )
        return ConversationHandler.END

    keyboard = []
    for w in workers:
        total = w["estado"]["deuda_formal"]
        keyboard.append([InlineKeyboardButton(
            f"{w['name']} — {fmt_price(total)}",
            callback_data=f"pay_w_{w['id']}"
        )])
    keyboard.append([InlineKeyboardButton("❌ Cancelar", callback_data="pay_cancel")])

    await update.message.reply_text(
        "💰 *Registrar pago a contratista*\n\n"
        "Solo se paga sobre la deuda formal (producción ya cerrada).\n\n"
        "¿A quién le vas a pagar?",
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
    estado = get_estado_contratista(worker_id)

    context.user_data["pay_worker_id"]   = worker_id
    context.user_data["pay_worker_name"] = worker["name"]
    context.user_data["pay_estado"]      = estado

    detalle = (
        f"💼 *Deuda formal de {worker['name']}*\n\n"
        f"💵 Total cerrado: {fmt_price(estado['total_cerrado'])}\n"
        f"💸 Total pagado: {fmt_price(estado['total_pagado'])}\n\n"
        f"*DEUDA ACTUAL: {fmt_price(estado['deuda_formal'])}*\n"
    )
    if estado["total_produccion_en_curso"] > 0:
        detalle += f"\n_Nota: hay {fmt_price(estado['total_produccion_en_curso'])} de producción en curso, sin cerrar todavía._\n"

    detalle += f"\n¿Cuánto le vas a pagar? Escribe el monto en números (ej: `{int(estado['deuda_formal'])}` para pago total o menos para abono):"

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

    estado = context.user_data.get("pay_estado", {})
    deuda = estado.get("deuda_formal", 0)

    if monto > deuda:
        await update.message.reply_text(
            f"⚠️ El monto ({fmt_price(monto)}) es mayor a la deuda formal ({fmt_price(deuda)}). "
            "Para pagar producción que aún no está cerrada, primero haz un /cierre."
        )
        return PAY_AMOUNT

    saldo_pendiente = deuda - monto
    context.user_data["pay_monto"] = monto
    context.user_data["pay_saldo_pendiente"] = saldo_pendiente

    tipo = "PAGO TOTAL ✅" if saldo_pendiente == 0 else f"ABONO PARCIAL (queda debiendo {fmt_price(saldo_pendiente)})"

    resumen_txt = (
        f"📋 *Confirma el pago a {context.user_data['pay_worker_name']}*\n\n"
        f"Deuda actual: {fmt_price(deuda)}\n"
        f"Monto a pagar: *{fmt_price(monto)}*\n"
        f"Tipo: {tipo}\n"
    )
    if saldo_pendiente > 0:
        resumen_txt += f"\n💡 La deuda restante de {fmt_price(saldo_pendiente)} se mantiene hasta el próximo pago."

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

    worker_id   = context.user_data.get("pay_worker_id")
    worker_name = context.user_data.get("pay_worker_name")
    estado      = context.user_data.get("pay_estado")
    monto       = context.user_data.get("pay_monto")
    saldo_pendiente = context.user_data.get("pay_saldo_pendiente")
    verifier    = get_verifier(update.effective_user.id)

    try:
        supabase.table("payments").insert({
            "worker_id":       worker_id,
            "total_facturado": estado["total_cerrado"],
            "monto_pagado":    monto,
            "saldo_pendiente": saldo_pendiente,
            "saldo_previo":    0,
            "registrado_por":  verifier["id"] if verifier else None,
        }).execute()

        respuesta = (
            f"✅ *Pago registrado*\n\n"
            f"👤 {worker_name}\n"
            f"💵 Pagado: {fmt_price(monto)}\n"
        )
        if saldo_pendiente > 0:
            respuesta += f"\n📌 Deuda restante: *{fmt_price(saldo_pendiente)}*"
        else:
            respuesta += "\n🎉 ¡Deuda totalmente saldada!"

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
# COMANDO /cuenta — consultar estado actual
# ═══════════════════════════════════════════════════════════════

async def cuenta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    worker = get_worker(user_id)
    if worker:
        estado = get_estado_contratista(worker["id"])
        msg = f"💼 *Tu estado actual*\n\n"
        msg += f"🟢 *Producción en curso (sin cerrar):* {fmt_price(estado['total_produccion_en_curso'])}\n"
        msg += f"   {len(estado['produccion_en_curso'])} entregas aprobadas\n\n"
        msg += f"📌 *Deuda formal (ya cerrada):* {fmt_price(estado['deuda_formal'])}\n"
        msg += f"   Cerrado histórico: {fmt_price(estado['total_cerrado'])} | Recibido: {fmt_price(estado['total_pagado'])}\n"

        if estado["produccion_en_curso"]:
            msg += "\n_Últimas entregas en curso:_\n"
            for d in estado["produccion_en_curso"][-8:]:
                fecha = d["created_at"][:10]
                msg += f"  • {fecha} — {d['product_name'][:35]} — {fmt_price(float(d.get('final_price', 0) or 0))}\n"

        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    if is_verifier(user_id):
        all_workers = supabase.table("workers").select("*").eq("activo", True).execute().data or []
        relevantes = []
        for w in all_workers:
            estado = get_estado_contratista(w["id"])
            if estado["total_produccion_en_curso"] > 0 or estado["deuda_formal"] > 0:
                relevantes.append((w, estado))

        if not relevantes:
            await update.message.reply_text("✅ Ningún contratista tiene producción ni deuda actualmente.")
            return

        msg = "💼 *Estado de contratistas*\n\n"
        total_curso = 0
        total_deuda = 0
        for w, e in relevantes:
            total_curso += e["total_produccion_en_curso"]
            total_deuda += e["deuda_formal"]
            msg += f"👤 *{w['name']}*\n"
            if e["total_produccion_en_curso"] > 0:
                msg += f"   🟢 En curso: {fmt_price(e['total_produccion_en_curso'])}\n"
            if e["deuda_formal"] > 0:
                msg += f"   📌 Deuda formal: {fmt_price(e['deuda_formal'])}\n"
            msg += "\n"

        msg += f"━━━━━━━━━━━━━━━━━\n"
        msg += f"🟢 Producción en curso total: *{fmt_price(total_curso)}*\n"
        msg += f"📌 Deuda formal total: *{fmt_price(total_deuda)}*"
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    await update.message.reply_text("⚠️ No estás registrado en el sistema.")


# ═══════════════════════════════════════════════════════════════
# COMANDO /produccion — producción en curso o de cierres anteriores
# ═══════════════════════════════════════════════════════════════

async def produccion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    worker = get_worker(user_id)
    es_verificador = is_verifier(user_id)
    args = [a.lower() for a in (context.args or [])]
    ver_anteriores = "anteriores" in args or "anterior" in args or "pasadas" in args or "historial" in args
    args_sin_kw = [a for a in args if a not in ("anteriores", "anterior", "pasadas", "historial")]

    async def _mostrar_uno(w):
        if ver_anteriores:
            cierres = supabase.table("closures")\
                .select("*")\
                .eq("worker_id", w["id"])\
                .order("fecha_fin", desc=True)\
                .limit(10)\
                .execute().data or []
            if not cierres:
                await update.message.reply_text(f"📭 {w['name']} aún no tiene cierres anteriores.")
                return
            msg = f"📜 *Cierres anteriores de {w['name']}*\n\n"
            for c in cierres:
                ini = c["fecha_inicio"][:10]
                fin = c["fecha_fin"][:10]
                msg += f"• Del {ini} al {fin}\n  💵 {fmt_price(float(c['monto']))}\n\n"
            total = sum(float(c['monto']) for c in cierres)
            msg += f"━━━━━━━━━━━━━━━━━\n*Total cerrado mostrado: {fmt_price(total)}*"
            await update.message.reply_text(msg, parse_mode="Markdown")
        else:
            estado = get_estado_contratista(w["id"])
            ultimo_cierre = supabase.table("closures")\
                .select("fecha_fin")\
                .eq("worker_id", w["id"])\
                .order("fecha_fin", desc=True)\
                .limit(1)\
                .execute().data
            desde = ultimo_cierre[0]["fecha_fin"][:10] if ultimo_cierre else "(inicio)"
            msg = f"🟢 *Producción en curso — {w['name']}*\n"
            msg += f"_Desde el último cierre ({desde}) hasta hoy_\n\n"
            if not estado["produccion_en_curso"]:
                msg += "_Sin entregas aprobadas en este período._"
            else:
                for d in estado["produccion_en_curso"]:
                    fecha = d["created_at"][:10]
                    msg += f"• {fecha} — {d['product_name'][:35]} — {fmt_price(float(d.get('final_price', 0) or 0))}\n"
                msg += f"\n*Total acumulado: {fmt_price(estado['total_produccion_en_curso'])}*"
                if estado["deuda_formal"] > 0:
                    msg += f"\n_(Además hay {fmt_price(estado['deuda_formal'])} de deuda formal por cobrar.)_"
            await update.message.reply_text(msg, parse_mode="Markdown")

    # Contratista: ve la suya
    if worker and not es_verificador:
        await _mostrar_uno(worker)
        return

    # Verificador con argumento de contratista
    if es_verificador and args_sin_kw:
        nombre = " ".join(args_sin_kw)
        workers_all = supabase.table("workers").select("*").execute().data or []
        match = next((w for w in workers_all if nombre in w["name"].lower()), None)
        if not match:
            await update.message.reply_text(f"⚠️ No encontré un contratista con nombre similar a '{nombre}'.")
            return
        await _mostrar_uno(match)
        return

    # Verificador sin argumento: resumen general
    if es_verificador:
        if ver_anteriores:
            cierres = supabase.table("closures")\
                .select("*, workers(name)")\
                .order("fecha_fin", desc=True)\
                .limit(15)\
                .execute().data or []
            if not cierres:
                await update.message.reply_text("📭 Aún no se han registrado cierres.")
                return
            msg = "📜 *Últimos cierres registrados*\n\n"
            for c in cierres:
                nombre = (c.get("workers") or {}).get("name", "?")
                ini = c["fecha_inicio"][:10]
                fin = c["fecha_fin"][:10]
                msg += f"• {fin} — {nombre} — {fmt_price(float(c['monto']))} _(del {ini} al {fin})_\n"
            msg += "\n💡 Para ver cierres de un contratista: `/produccion anteriores <nombre>`"
            await update.message.reply_text(msg, parse_mode="Markdown")
            return

        # Producción en curso de todos
        workers = get_workers_with_produccion_en_curso()
        if not workers:
            await update.message.reply_text("📭 Nadie tiene producción en curso ahora mismo.")
            return
        msg = "🟢 *Producción en curso (todos)*\n\n"
        total = 0
        for w in workers:
            e = w["estado"]
            total += e["total_produccion_en_curso"]
            msg += f"👤 {w['name']} — {fmt_price(e['total_produccion_en_curso'])} ({len(e['produccion_en_curso'])} entregas)\n"
        msg += f"\n━━━━━━━━━━━━━━━━━\n*Total general en curso: {fmt_price(total)}*"
        msg += "\n💡 `/produccion <nombre>` para detalle | `/produccion anteriores` para cierres pasados"
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    await update.message.reply_text("⚠️ No estás registrado en el sistema.")


# ═══════════════════════════════════════════════════════════════
# COMANDO /historial — pagos pasados (sigue igual)
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


# ═══════════════════════════════════════════════════════════════
# /MISTRABAJOS — bandeja del contratista (Fase 2a, solo lectura)
# ═══════════════════════════════════════════════════════════════

def _fmt_fecha_entrega(iso):
    if not iso:
        return "sin fecha"
    try:
        d = datetime.strptime(str(iso)[:10], "%Y-%m-%d").date()
        dias = (d - datetime.now().date()).days
        dl = d.strftime("%d/%m")
        if dias < 0:  return f"⚠️ {dl} (atrasado {abs(dias)}d)"
        if dias == 0: return f"{dl} (hoy)"
        if dias == 1: return f"{dl} (mañana)"
        return f"{dl} (en {dias} días)"
    except Exception:
        return str(iso)[:10]

def _nombre_producto(t):
    it = t.get("pedido_items") or {}
    prod = it.get("productos") or {}
    return prod.get("nombre") or it.get("nombre_referencia") or "Producto"

def _precios_mano_obra(tareas):
    """Devuelve {(producto_id, oficio_id): precio} para los productos de las tareas."""
    prod_ids = list({(t.get("pedido_items") or {}).get("producto_id")
                     for t in tareas if (t.get("pedido_items") or {}).get("producto_id")})
    pmo = {}
    if prod_ids:
        try:
            r = supabase.table("producto_mano_obra").select("producto_id,oficio_id,precio").in_("producto_id", prod_ids).execute()
            for row in (r.data or []):
                pmo[(row["producto_id"], row["oficio_id"])] = row["precio"]
        except Exception as e:
            logger.error(f"_precios_mano_obra: {e}")
    return pmo

async def mistrabajos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    worker = get_worker(update.effective_user.id)
    if not worker:
        await update.message.reply_text("❌ No estás registrado en el sistema.")
        return
    try:
        r = supabase.table("tareas").select(
            "*, pedido_items(nombre_referencia,cantidad,fecha_entrega,producto_id,notas_especiales,"
            "productos(nombre),pedidos(cliente,fve))"
        ).eq("worker_id", worker["id"]).neq("estado", "terminada").execute()
        tareas = r.data or []
    except Exception as e:
        logger.error(f"mistrabajos: {e}")
        await update.message.reply_text("⚠️ No pude leer tus trabajos en este momento.")
        return

    pmo = _precios_mano_obra(tareas)
    fkey = lambda t: ((t.get("pedido_items") or {}).get("fecha_entrega") or "9999-12-31")
    pendientes = sorted([t for t in tareas if t.get("estado") == "pendiente"], key=fkey)
    bloqueadas = sorted([t for t in tareas if t.get("estado") == "bloqueada"], key=fkey)

    lines = [f"👷 *{worker['name']}*\n"]
    buttons = []
    if pendientes:
        lines.append(f"📋 *PARA TRABAJAR ({len(pendientes)})* — en orden de prioridad")
        for i, t in enumerate(pendientes, 1):
            it = t.get("pedido_items") or {}; ped = it.get("pedidos") or {}
            pr = pmo.get((it.get("producto_id"), t.get("oficio_id")))
            precio_txt = fmt_price(pr) if pr is not None else "💲 por confirmar"
            lines.append(f"\n*{i}. {_nombre_producto(t)}* ·x{int(it.get('cantidad') or 1)}  _{t.get('oficio_nombre','')}_")
            lines.append(f"   👤 {ped.get('cliente','')} · FVE {ped.get('fve','')}")
            lines.append(f"   📅 {_fmt_fecha_entrega(it.get('fecha_entrega'))}   💰 {precio_txt}")
            if it.get("notas_especiales"):
                lines.append(f"   📝 {str(it['notas_especiales'])[:90]}")
            buttons.append([
                InlineKeyboardButton(f"👁 Ver #{i}", callback_data=f"mtver_{t['id']}"),
                InlineKeyboardButton(f"✅ Terminé #{i}", callback_data=f"mtfin_{t['id']}"),
            ])
    else:
        lines.append("📋 No tienes trabajos pendientes ahora mismo. 🎉")

    if bloqueadas:
        lines.append(f"\n⏳ *EN CAMINO ({len(bloqueadas)})* — esperando que termine otro oficio")
        for t in bloqueadas:
            it = t.get("pedido_items") or {}
            lines.append(f"   • {_nombre_producto(t)} _{t.get('oficio_nombre','')}_ — te avisaré cuando esté listo")

    lines.append("\n_Toca 👁 Ver para los detalles de cada trabajo._")
    markup = InlineKeyboardMarkup(buttons) if buttons else None
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=markup)

async def mt_ver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        tid = int(q.data.split("_")[1])
        r = supabase.table("tareas").select(
            "*, pedido_items(nombre_referencia,cantidad,fecha_entrega,producto_id,notas_especiales,"
            "productos(nombre),pedidos(cliente,fve))"
        ).eq("id", tid).execute()
        if not r.data:
            await q.message.reply_text("No encontré ese trabajo.")
            return
        t = r.data[0]; it = t.get("pedido_items") or {}; ped = it.get("pedidos") or {}
        pr = None
        if it.get("producto_id"):
            prq = supabase.table("producto_mano_obra").select("precio").eq("producto_id", it["producto_id"]).eq("oficio_id", t.get("oficio_id")).execute()
            if prq.data:
                pr = prq.data[0]["precio"]
        txt = (f"📄 *{_nombre_producto(t)}*  ·x{int(it.get('cantidad') or 1)}\n"
               f"🔧 Oficio: {t.get('oficio_nombre','')}\n"
               f"👤 Cliente: {ped.get('cliente','')}\n"
               f"🧾 FVE: {ped.get('fve','')}\n"
               f"📅 Entrega: {_fmt_fecha_entrega(it.get('fecha_entrega'))}\n"
               f"💰 Pago: {fmt_price(pr) if pr is not None else 'por confirmar con el verificador'}")
        if it.get("notas_especiales"):
            txt += f"\n\n📝 *Indicaciones:*\n{it['notas_especiales']}"
        await q.message.reply_text(txt, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"mt_ver: {e}")
        await q.message.reply_text("⚠️ No pude abrir ese trabajo.")

# ── Flujo "Terminé un trabajo" (Fase 2b) ──
async def terminar_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    worker = get_worker(update.effective_user.id)
    if not worker:
        await update.message.reply_text("❌ No estás registrado."); return ConversationHandler.END
    try:
        r = supabase.table("tareas").select(
            "id,oficio_nombre,pedido_items(nombre_referencia,productos(nombre))"
        ).eq("worker_id", worker["id"]).eq("estado", "pendiente").execute()
        tareas = r.data or []
    except Exception as e:
        logger.error(f"terminar_menu: {e}"); await update.message.reply_text("⚠️ Error al leer tus trabajos."); return ConversationHandler.END
    if not tareas:
        await update.message.reply_text("No tienes trabajos pendientes por marcar. 🎉", reply_markup=KB_CONTRATISTA)
        return ConversationHandler.END
    buttons = []
    for t in tareas:
        it = t.get("pedido_items") or {}
        nombre = (it.get("productos") or {}).get("nombre") or it.get("nombre_referencia") or "Producto"
        buttons.append([InlineKeyboardButton(f"{nombre[:28]} · {t.get('oficio_nombre','')}", callback_data=f"mtpick_{t['id']}")])
    buttons.append([InlineKeyboardButton("❌ Cancelar", callback_data="mtpick_cancel")])
    await update.message.reply_text("¿Cuál trabajo terminaste?", reply_markup=InlineKeyboardMarkup(buttons))
    return TFIN_PICK

async def terminar_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "mtpick_cancel":
        await q.edit_message_text("Cancelado."); return ConversationHandler.END
    context.user_data["fin_tarea_id"] = int(q.data.split("_")[1])
    await q.edit_message_text("📸 Envía la *foto* del trabajo terminado.", parse_mode="Markdown")
    return TFIN_FOTO

async def terminar_desde_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    context.user_data["fin_tarea_id"] = int(q.data.split("_")[1])
    await q.message.reply_text("📸 Envía la *foto* del trabajo terminado.", parse_mode="Markdown")
    return TFIN_FOTO

async def terminar_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("📸 Necesito una *foto* del trabajo terminado, o /cancelar para salir.", parse_mode="Markdown")
        return TFIN_FOTO
    worker = get_worker(update.effective_user.id)
    tid = context.user_data.get("fin_tarea_id")
    if not worker or not tid:
        await update.message.reply_text("Algo salió mal. Abre 📋 Mis trabajos e intenta de nuevo.", reply_markup=KB_CONTRATISTA)
        return ConversationHandler.END
    photo = update.message.photo[-1]
    try:
        r = supabase.table("tareas").select("*, pedido_items(nombre_referencia,producto_id,pedidos(fve,cliente))").eq("id", tid).execute()
        if not r.data:
            await update.message.reply_text("No encontré ese trabajo."); return ConversationHandler.END
        t = r.data[0]; it = t.get("pedido_items") or {}; ped = it.get("pedidos") or {}
        nombre_prod = it.get("nombre_referencia") or "Producto"; prod_id = it.get("producto_id")
        if prod_id:
            pq = supabase.table("productos").select("nombre").eq("id", prod_id).execute()
            if pq.data: nombre_prod = pq.data[0]["nombre"]
        precio = None
        if prod_id:
            pm = supabase.table("producto_mano_obra").select("precio").eq("producto_id", prod_id).eq("oficio_id", t.get("oficio_id")).execute()
            if pm.data: precio = pm.data[0]["precio"]
        precio_pendiente = precio is None
        final_price = float(precio) if precio is not None else 0
        file = await context.bot.get_file(photo.file_id)
        rec = {"worker_id": worker["id"], "fve": ped.get("fve", ""), "product_name": nombre_prod,
               "is_special": precio_pendiente, "final_price": final_price,
               "photo_file_id": photo.file_id, "photo_url": file.file_path, "status": "pending",
               "notes": f"Oficio: {t.get('oficio_nombre','')}", "tarea_id": tid,
               "precio_pendiente": precio_pendiente, "created_at": datetime.utcnow().isoformat()}
        did = supabase.table("deliveries").insert(rec).execute().data[0]["id"]
        supabase.table("tareas").update({"estado": "terminada", "fecha_terminada": datetime.utcnow().isoformat()}).eq("id", tid).execute()
        # desbloquear dependientes
        sib = supabase.table("tareas").select("id,oficio_nombre,estado,worker_id").eq("pedido_item_id", t["pedido_item_id"]).execute().data or []
        present = [s["oficio_nombre"] for s in sib]
        estado_de = {s["oficio_nombre"]: ("terminada" if s["id"] == tid else s["estado"]) for s in sib}
        desbloqueados = []
        for s in sib:
            if s["id"] == tid or s["estado"] != "bloqueada": continue
            pre = [p for p in PREREQS_BOT.get(s["oficio_nombre"], []) if p in present]
            if all(estado_de.get(p) == "terminada" for p in pre):
                supabase.table("tareas").update({"estado": "pendiente"}).eq("id", s["id"]).execute()
                desbloqueados.append(s)
        if all((s["id"] == tid or s["estado"] == "terminada") for s in sib):
            supabase.table("pedido_items").update({"estado": "terminado"}).eq("id", t["pedido_item_id"]).execute()
        # avisar a verificadores
        tipo = "🔴 *PRECIO POR AUTORIZAR*" if precio_pendiente else "🟢 Precio de lista"
        cap = (f"🏭 *NUEVA ENTREGA (desde orden)*\n{'─'*28}\n👷 *{worker['name']}*\n"
               f"🏷️ Oficio: {t.get('oficio_nombre','')}\n📋 FVE: `{ped.get('fve','')}`\n📦 *{nombre_prod}*\n"
               f"💰 *{fmt_price(final_price) if not precio_pendiente else 'por definir'}*\n{tipo}\nID: `{did}`")
        btns = [[InlineKeyboardButton("✅ Aprobar", callback_data=f"approve_{did}"),
                 InlineKeyboardButton("❌ Rechazar", callback_data=f"reject_{did}")]]
        if precio_pendiente:
            btns.append([InlineKeyboardButton("✏️ Aprobar con precio", callback_data=f"modify_{did}")])
        for v in get_verifiers():
            try:
                await context.bot.send_photo(chat_id=v["telegram_id"], photo=photo.file_id, caption=cap,
                                             reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")
            except Exception as e:
                logger.error(f"notif verificador: {e}")
        # avisar a contratistas desbloqueados
        for s in desbloqueados:
            if s.get("worker_id"):
                wq = supabase.table("workers").select("telegram_id").eq("id", s["worker_id"]).execute()
                if wq.data and wq.data[0].get("telegram_id"):
                    try:
                        await context.bot.send_message(chat_id=wq.data[0]["telegram_id"],
                            text=f"✅ Ya puedes empezar *{s['oficio_nombre']}* de {nombre_prod}.\nÁbrelo en 📋 Mis trabajos.", parse_mode="Markdown")
                    except Exception as e:
                        logger.error(f"notif desbloqueo: {e}")
        msg = f"✅ *¡Trabajo enviado!*\n📦 {nombre_prod} — {t.get('oficio_nombre','')}\n"
        msg += ("💰 Precio por confirmar con el verificador.\n" if precio_pendiente else f"💰 {fmt_price(final_price)}\n")
        msg += "⏳ Pendiente de aprobación. Te aviso cuando lo aprueben 👍"
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=KB_CONTRATISTA)
    except Exception as e:
        logger.error(f"terminar_foto: {e}")
        await update.message.reply_text("⚠️ No pude registrar el trabajo. Intenta de nuevo.", reply_markup=KB_CONTRATISTA)
    context.user_data.pop("fin_tarea_id", None)
    return ConversationHandler.END

async def terminar_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("fin_tarea_id", None)
    await update.message.reply_text("Cancelado.", reply_markup=KB_CONTRATISTA)
    return ConversationHandler.END


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("reportar", reportar_start)],
        states={
            OFICIO:        [CallbackQueryHandler(got_oficio,      pattern=r"^oficio_")],
            FVE:           [MessageHandler(filters.TEXT & ~filters.COMMAND, got_fve)],
            PRODUCT_NAME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, got_product_name)],
            PICK_PRODUCT:  [CallbackQueryHandler(got_picked_product, pattern=r"^pick_")],
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

    close_conv = ConversationHandler(
        entry_points=[CommandHandler("cierre", cierre_start)],
        states={
            CLOSE_WORKER:  [CallbackQueryHandler(cierre_got_worker, pattern=r"^close_(w_|cancel)")],
            CLOSE_CONFIRM: [CallbackQueryHandler(cierre_confirm,    pattern=r"^close_confirm_")],
        },
        fallbacks=[CommandHandler("cancelar", cierre_cancel)],
        allow_reentry=True,
    )

    fin_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(terminar_desde_card, pattern=r"^mtfin_"),
            MessageHandler(filters.Regex(r"^✅ Terminé uno$"), terminar_menu),
        ],
        states={
            TFIN_PICK: [CallbackQueryHandler(terminar_picked, pattern=r"^mtpick_")],
            TFIN_FOTO: [MessageHandler(filters.PHOTO, terminar_foto),
                        MessageHandler(filters.TEXT & ~filters.COMMAND, terminar_foto)],
        },
        fallbacks=[CommandHandler("cancelar", terminar_cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(pay_conv)
    app.add_handler(close_conv)
    app.add_handler(fin_conv)
    app.add_handler(CommandHandler("start",      start))
    app.add_handler(CommandHandler("mistotal",   mis_total))
    app.add_handler(CommandHandler("pendientes", pendientes))
    app.add_handler(CommandHandler("resumen",    resumen))
    app.add_handler(CommandHandler("precios",    precios))
    app.add_handler(CommandHandler("cuenta",     cuenta))
    app.add_handler(CommandHandler("produccion", produccion))
    app.add_handler(CommandHandler("historial",  historial))
    app.add_handler(CommandHandler("mistrabajos", mistrabajos))
    app.add_handler(CallbackQueryHandler(mt_ver, pattern=r"^mtver_"))
    app.add_handler(CallbackQueryHandler(handle_verification, pattern=r"^(approve|reject|modify)_"))
    app.add_handler(MessageHandler(filters.Regex(r"^📋 Mis trabajos$"), mistrabajos))
    app.add_handler(MessageHandler(filters.Regex(r"^💰 Mi cuenta$"), cuenta))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ai_question))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice_question))

    logger.info("🏭 Bot Livinghouse iniciado y escuchando...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
