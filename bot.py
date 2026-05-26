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

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

OFICIOS = {
    "corte_costura": "✂️ Corte y Costura",
    "tapiceria":     "🛋️ Tapicería",
    "carpinteria":   "🪚 Carpintería",
    "esqueleteria":  "🔧 Esqueletería",
    "pintura":       "🎨 Pintura",
}

OFICIO, FVE, PRODUCT_NAME, PRICE_TYPE, SPECIAL_PRICE, PHOTO = range(6)


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

        lines = ["=== DATOS DE PRODUCCIÓN LIVINGHOUSE (últimos 30 días) ===\n"]

        resumen_workers = {}
        for d in deliveries:
            nombre = d.get("workers", {}).get("name", "Desconocido")
            if nombre not in resumen_workers:
                resumen_workers[nombre] = {"total": 0, "cantidad": 0, "aprobadas": 0}
            resumen_workers[nombre]["cantidad"] += 1
            if d["status"] == "approved":
                resumen_workers[nombre]["total"] += float(d.get("final_price", 0))
                resumen_workers[nombre]["aprobadas"] += 1

        lines.append("RESUMEN POR CONTRATISTA:")
        for nombre, datos in resumen_workers.items():
            lines.append(f"  - {nombre}: {datos['cantidad']} entregas ({datos['aprobadas']} aprobadas), total aprobado: ${datos['total']:,.0f}")

        lines.append("\nÚLTIMAS 20 ENTREGAS:")
        for d in deliveries[:20]:
            nombre = d.get("workers", {}).get("name", "?")
            fecha = d["created_at"][:10]
            oficio = d.get("notes", "").replace("Oficio: ", "") if d.get("notes") and "Oficio:" in str(d.get("notes", "")) else ""
            lines.append(f"  - {fecha} | {nombre} | {d['product_name'][:40]} | {oficio} | ${float(d.get('final_price',0)):,.0f} | {d['status']}")

        pend = [d for d in deliveries if d["status"] == "pending"]
        lines.append(f"\nENTREGAS PENDIENTES: {len(pend)}")
        for d in pend[:5]:
            nombre = d.get("workers", {}).get("name", "?")
            lines.append(f"  - {nombre} | {d['product_name'][:40]} | ${float(d.get('final_price',0)):,.0f}")

        lines.append(f"\nCONTRATISTAS REGISTRADOS ({len(workers)}):")
        for w in workers:
            lines.append(f"  - {w['name']}")

        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error obteniendo contexto: {e}")
        return "No se pudo obtener datos de la base de datos."


async def ask_gemini(question: str, user_name: str) -> str:
    if not GEMINI_API_KEY:
        return "⚠️ La clave de Gemini no está configurada."
    try:
        context = get_context_data()
        prompt = f"""Eres el asistente inteligente del sistema de producción de Livinghouse, 
una fábrica de muebles en Manizales, Colombia.
Responde preguntas sobre producción, contratistas, entregas y pagos basándote ÚNICAMENTE 
en los datos proporcionados. Responde en español, claro y conciso. Usa emojis apropiados.
Quien pregunta: {user_name}

{context}

Pregunta: {question}"""

        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
        response = http_requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=30)
        response.raise_for_status()
        return response.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        logger.error(f"Error con Gemini: {e}")
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

    if not es_pregunta:
        await handle_verifier_text(update, context)
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    respuesta = await ask_gemini(text, update.effective_user.first_name)
    await update.message.reply_text(f"🤖 {respuesta}", parse_mode="Markdown")


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
        await update.message.reply_text(f"🤖 {respuesta}", parse_mode="Markdown")
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

    app.add_handler(conv)
    app.add_handler(CommandHandler("start",      start))
    app.add_handler(CommandHandler("mistotal",   mis_total))
    app.add_handler(CommandHandler("pendientes", pendientes))
    app.add_handler(CommandHandler("resumen",    resumen))
    app.add_handler(CommandHandler("precios",    precios))
    app.add_handler(CallbackQueryHandler(handle_verification, pattern=r"^(approve|reject|modify)_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ai_question))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice_question))

    logger.info("🏭 Bot Livinghouse iniciado y escuchando...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
