"""
LIVINGHOUSE · BOT DE CONTROL DE PRODUCCIÓN
==========================================
Bot de Telegram para gestionar entregas de contratistas,
verificación de productos y generación de cuentas de cobro.

Oficios soportados: costura, corte, tapiceria, pintura, esqueleteria, carpinteria

Comandos contratistas:
  /start      - Registrar / ver bienvenida
  /reportar   - Registrar un producto terminado
  /mistotal   - Ver acumulado de la semana actual
  /cancelar   - Cancelar reporte en curso

Solo verificadores:
  /resumen [nombre] [semana|quincena] - Ver cuenta de cobro
  /pendientes                          - Ver entregas sin aprobar
  /precios                             - Buscar precios
"""

import os
import logging
from datetime import datetime, timedelta

from dotenv import load_dotenv
from supabase import create_client, Client
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, ReplyKeyboardRemove
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

# ─── CONFIGURACIÓN ────────────────────────────────────────────
BOT_TOKEN    = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Oficios disponibles
OFICIOS = {
    "corte_costura": "✂️ Corte y Costura",
    "tapiceria":     "🛋️ Tapicería",
    "carpinteria":   "🪚 Carpintería",
    "esqueleteria":  "🔧 Esqueletería",
    "pintura":       "🎨 Pintura",
}

# Estados de la conversación /reportar
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
    """Busca el precio más parecido en la lista maestra para el oficio dado."""
    # Búsqueda exacta con oficio
    r = supabase.table("price_list").select("*").eq("active", True)\
        .eq("oficio", oficio)\
        .ilike("product_name", product_name).execute()
    if r.data:
        return r.data[0]
    # Búsqueda parcial
    keywords = product_name.upper().split()
    for kw in keywords:
        if len(kw) < 4:
            continue
        r = supabase.table("price_list").select("*").eq("active", True)\
            .eq("oficio", oficio)\
            .ilike("product_name", f"%{kw}%").execute()
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
# /START
# ═══════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    worker = get_worker(user.id)

    if worker:
        await update.message.reply_text(
            f"👋 ¡Hola *{worker['name']}*!\n\n"
            f"🏭 Área: {worker.get('area', 'Sin área').capitalize()}\n\n"
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
            f"Recibirás notificaciones automáticas cuando un contratista reporte.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"👋 Hola {user.first_name}.\n\n"
            f"⚠️ No estás registrado en el sistema *Livinghouse*.\n"
            f"Comunícate con el administrador para que te registre.\n\n"
            f"Tu ID de Telegram es: `{user.id}`\n"
            f"_(compártelo con el admin)_",
            parse_mode="Markdown"
        )


# ═══════════════════════════════════════════════════════════════
# /REPORTAR — FLUJO DE ENTREGA
# ═══════════════════════════════════════════════════════════════

async def reportar_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    worker = get_worker(update.effective_user.id)
    if not worker:
        await update.message.reply_text(
            "❌ No estás registrado en el sistema.\n"
            "Contacta al administrador con tu ID: "
            f"`{update.effective_user.id}`",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["worker"] = worker
    context.user_data["delivery"] = {}

    # Mostrar teclado de oficios
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=f"oficio_{key}")]
        for key, label in OFICIOS.items()
    ])

    await update.message.reply_text(
        "📦 *Nuevo reporte de producto terminado*\n\n"
        "Paso 1️⃣ — ¿Qué *oficio* realizaste en este producto?\n\n"
        "Envía /cancelar para salir.",
        parse_mode="Markdown",
        reply_markup=keyboard
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
        f"_Ejemplo: FVE 2118 o escribe sin la sigla_\n\n"
        f"Envía /cancelar para salir.",
        parse_mode="Markdown"
    )
    return FVE


async def got_fve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fve = update.message.text.strip().upper()
    context.user_data["delivery"]["fve"] = fve

    await update.message.reply_text(
        f"✅ FVE: *{fve}*\n\n"
        f"Paso 3️⃣ — ¿Cuál es el *nombre del producto*?\n\n"
        f"_Ejemplo: Sofa Cama Montreal Tipo 2_",
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
        [InlineKeyboardButton("📋  Precio de lista",  callback_data="type_standard")],
        [InlineKeyboardButton("📐  Medida especial",  callback_data="type_special")],
    ])

    if price_match:
        # Para corte_costura mostrar desglose, para otros solo total
        if oficio == "corte_costura" and price_match.get("precio_corte"):
            precio_detalle = (
                f"   ├ Corte:   {fmt_price(price_match.get('precio_corte'))}\n"
                f"   ├ Costura: {fmt_price(price_match.get('precio_costura'))}\n"
                f"   └ *Total:  {fmt_price(price_match['precio_total'])}*"
            )
        else:
            precio_detalle = f"   └ *Total: {fmt_price(price_match['precio_total'])}*"

        msg = (
            f"📦 Producto: *{product_name}*\n"
            f"🏷️ Oficio: {oficio_label}\n\n"
            f"💡 Encontré en la lista de precios:\n"
            f"{precio_detalle}\n\n"
            f"Paso 4️⃣ — ¿Es precio de lista o medida especial?"
        )
    else:
        msg = (
            f"📦 Producto: *{product_name}*\n"
            f"🏷️ Oficio: {oficio_label}\n\n"
            f"⚠️ Este producto *no está en la lista* de {oficio_label}.\n\n"
            f"Paso 4️⃣ — ¿Es precio de lista o medida especial?"
        )

    await update.message.reply_text(msg, reply_markup=keyboard, parse_mode="Markdown")
    return PRICE_TYPE


async def got_price_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    choice = query.data
    delivery = context.user_data["delivery"]
    price_match = delivery.get("price_match")

    if choice == "type_standard":
        delivery["is_special"] = False
        if price_match:
            delivery["final_price"] = float(price_match["precio_total"])
            delivery["price_list_id"] = price_match["id"]
            await query.edit_message_text(
                f"✅ Precio de lista aplicado: *{fmt_price(price_match['precio_total'])}*\n\n"
                f"Paso 5️⃣ — Envía la *foto del producto terminado* 📸\n\n"
                f"_(La foto es la prueba visual de entrega)_",
                parse_mode="Markdown"
            )
            return PHOTO
        else:
            await query.edit_message_text(
                "⚠️ El producto no está en la lista, así que necesitas indicar el precio.\n\n"
                "¿Cuánto cobras por este producto?\n"
                "_Solo el número, ejemplo: 85000_",
                parse_mode="Markdown"
            )
            delivery["is_special"] = True
            return SPECIAL_PRICE
    else:
        delivery["is_special"] = True
        await query.edit_message_text(
            "📐 *Precio especial*\n\n"
            "¿Cuánto estás solicitando por este producto?\n"
            "_Solo el número, ejemplo: 120000_\n\n"
            "⚠️ Esto requiere aprobación explícita del supervisor.",
            parse_mode="Markdown"
        )
        return SPECIAL_PRICE


async def got_special_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().replace("$", "").replace(".", "").replace(",", "").replace(" ", "")
    try:
        price = float(raw)
        if price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ No entendí ese valor. Escribe solo el número.\n"
            "_Ejemplo: 120000_",
            parse_mode="Markdown"
        )
        return SPECIAL_PRICE

    context.user_data["delivery"]["requested_price"] = price
    context.user_data["delivery"]["final_price"] = price

    await update.message.reply_text(
        f"💰 Precio solicitado: *{fmt_price(price)}*\n\n"
        f"Paso 5️⃣ — Ahora envía la *foto del producto terminado* 📸\n\n"
        f"_(La foto es la prueba visual de entrega)_",
        parse_mode="Markdown"
    )
    return PHOTO


async def got_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("❌ Por favor envía una *foto*, no un archivo.")
        return PHOTO

    delivery = context.user_data["delivery"]
    worker   = context.user_data["worker"]

    photo       = update.message.photo[-1]
    is_special  = delivery.get("is_special", False)
    final_price = delivery["final_price"]
    oficio      = delivery.get("oficio", "")
    oficio_label = delivery.get("oficio_label", oficio)

    file = await context.bot.get_file(photo.file_id)
    record = {
        "worker_id":       worker["id"],
        "fve":             delivery["fve"],
        "product_name":    delivery["product_name"],
        "price_list_id":   delivery.get("price_list_id"),
        "is_special":      is_special,
        "requested_price": delivery.get("requested_price"),
        "final_price":     final_price,
        "photo_file_id":   photo.file_id,
        "photo_url":       file.file_path,
        "status":          "pending",
        "notes":           f"Oficio: {oficio_label}",
        "created_at":      datetime.utcnow().isoformat(),
    }
    result      = supabase.table("deliveries").insert(record).execute()
    delivery_id = result.data[0]["id"]

    tipo_label = (
        "🔴 *PRECIO ESPECIAL* — requiere aprobación explícita del valor"
        if is_special else
        "🟢 Precio de lista — solo confirmar entrega"
    )

    caption = (
        f"🏭 *NUEVA ENTREGA*\n"
        f"{'─' * 28}\n"
        f"👷 *{worker['name']}*\n"
        f"🏷️ Oficio: {oficio_label}\n"
        f"📋 FVE:      `{delivery['fve']}`\n"
        f"📦 Producto: *{delivery['product_name']}*\n"
        f"💰 Precio:   *{fmt_price(final_price)}*\n"
        f"🏷️ {tipo_label}\n"
        f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M')}\n"
        f"{'─' * 28}\n"
        f"ID entrega: `{delivery_id}`"
    )

    buttons = [[
        InlineKeyboardButton("✅ Aprobar",  callback_data=f"approve_{delivery_id}"),
        InlineKeyboardButton("❌ Rechazar", callback_data=f"reject_{delivery_id}"),
    ]]
    if is_special:
        buttons.append([
            InlineKeyboardButton("✏️ Aprobar con otro precio", callback_data=f"modify_{delivery_id}")
        ])
    keyboard = InlineKeyboardMarkup(buttons)

    notified = 0
    for verifier in get_verifiers():
        try:
            await context.bot.send_photo(
                chat_id=verifier["telegram_id"],
                photo=photo.file_id,
                caption=caption,
                reply_markup=keyboard,
                parse_mode="Markdown"
            )
            notified += 1
        except Exception as e:
            logger.error(f"No se pudo notificar a {verifier['name']}: {e}")

    await update.message.reply_text(
        f"✅ *¡Reporte enviado correctamente!*\n\n"
        f"🏷️ Oficio: {oficio_label}\n"
        f"📋 FVE:    {delivery['fve']}\n"
        f"📦 {delivery['product_name']}\n"
        f"💰 {fmt_price(final_price)}\n\n"
        f"{'⏳ *Esperando aprobación del precio especial.*' if is_special else '⏳ Esperando confirmación de entrega.'}\n"
        f"Te avisaré cuando lo aprueben o rechacen. 👍",
        parse_mode="Markdown"
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Reporte cancelado.\n"
        "Cuando quieras, usa /reportar para empezar de nuevo.",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════
# CALLBACKS DE VERIFICADORES
# ═══════════════════════════════════════════════════════════════

async def handle_verification(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_verifier(query.from_user.id):
        await query.answer("❌ No tienes permisos para verificar.", show_alert=True)
        return

    data = query.data
    action, delivery_id = data.split("_", 1)
    delivery_id = int(delivery_id)

    delivery  = get_delivery(delivery_id)
    verifier  = get_verifier(query.from_user.id)
    ver_name  = verifier["name"] if verifier else query.from_user.first_name

    if not delivery:
        await query.edit_message_caption(
            caption=query.message.caption + "\n\n⚠️ Entrega no encontrada.",
            parse_mode="Markdown"
        )
        return

    if delivery["status"] != "pending":
        status_map = {"approved": "✅ Ya aprobada", "rejected": "❌ Ya rechazada"}
        await query.answer(
            f"Esta entrega ya fue procesada: {status_map.get(delivery['status'], delivery['status'])}",
            show_alert=True
        )
        return

    if action == "approve":
        supabase.table("deliveries").update({
            "status":                  "approved",
            "approved_by":             ver_name,
            "approved_by_telegram_id": query.from_user.id,
            "approved_at":             datetime.utcnow().isoformat(),
        }).eq("id", delivery_id).execute()

        worker = delivery.get("workers", {})
        try:
            await context.bot.send_message(
                chat_id=worker["telegram_id"],
                text=(
                    f"✅ *¡Producto aprobado!*\n\n"
                    f"📋 FVE: {delivery['fve']}\n"
                    f"📦 {delivery['product_name']}\n"
                    f"💰 {fmt_price(delivery['final_price'])}\n"
                    f"👤 Aprobado por: *{ver_name}*\n"
                    f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M')}"
                ),
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
        context.user_data["rejection_chat_id"] = query.message.chat_id
        await query.edit_message_caption(
            caption=query.message.caption + f"\n\n{'─'*28}\n❌ Rechazando...\nEscribe el *motivo del rechazo* en el chat:",
            parse_mode="Markdown"
        )

    elif action == "modify":
        context.user_data["pending_modification"] = delivery_id
        context.user_data["mod_verifier"] = ver_name
        context.user_data["mod_chat_id"] = query.message.chat_id
        await query.edit_message_caption(
            caption=query.message.caption + f"\n\n{'─'*28}\n✏️ Escribe el *precio que apruebas*\n(solo el número, ej: 95000):",
            parse_mode="Markdown"
        )


async def handle_verifier_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_verifier(update.effective_user.id):
        return

    user_data = context.user_data

    if "pending_rejection" in user_data:
        delivery_id = user_data.pop("pending_rejection")
        ver_name    = user_data.pop("rejection_verifier", update.effective_user.first_name)
        reason      = update.message.text.strip()

        delivery = get_delivery(delivery_id)
        supabase.table("deliveries").update({
            "status":                  "rejected",
            "rejection_reason":        reason,
            "approved_by":             ver_name,
            "approved_by_telegram_id": update.effective_user.id,
            "approved_at":             datetime.utcnow().isoformat(),
        }).eq("id", delivery_id).execute()

        worker = delivery.get("workers", {})
        try:
            await context.bot.send_message(
                chat_id=worker["telegram_id"],
                text=(
                    f"❌ *Producto rechazado*\n\n"
                    f"📋 FVE: {delivery['fve']}\n"
                    f"📦 {delivery['product_name']}\n"
                    f"💬 Motivo: _{reason}_\n"
                    f"👤 Rechazado por: *{ver_name}*\n\n"
                    f"Comunícate con el supervisor para más detalles."
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"No se pudo notificar al contratista: {e}")

        await update.message.reply_text(
            f"❌ Entrega #{delivery_id} rechazada.\nMotivo: _{reason}_",
            parse_mode="Markdown"
        )

    elif "pending_modification" in user_data:
        delivery_id = user_data.pop("pending_modification")
        ver_name    = user_data.pop("mod_verifier", update.effective_user.first_name)
        raw         = update.message.text.strip().replace("$", "").replace(".", "").replace(",", "")

        try:
            new_price = float(raw)
        except ValueError:
            await update.message.reply_text("❌ No entendí ese precio. Escribe solo el número, ej: 95000")
            user_data["pending_modification"] = delivery_id
            user_data["mod_verifier"] = ver_name
            return

        delivery = get_delivery(delivery_id)
        supabase.table("deliveries").update({
            "status":                  "approved",
            "final_price":             new_price,
            "approved_by":             ver_name,
            "approved_by_telegram_id": update.effective_user.id,
            "approved_at":             datetime.utcnow().isoformat(),
            "notes":                   f"Precio modificado: {fmt_price(delivery['final_price'])} → {fmt_price(new_price)}",
        }).eq("id", delivery_id).execute()

        worker = delivery.get("workers", {})
        try:
            await context.bot.send_message(
                chat_id=worker["telegram_id"],
                text=(
                    f"✅ *Producto aprobado con precio ajustado*\n\n"
                    f"📋 FVE: {delivery['fve']}\n"
                    f"📦 {delivery['product_name']}\n"
                    f"💰 Precio solicitado: {fmt_price(delivery.get('requested_price'))}\n"
                    f"💰 *Precio aprobado: {fmt_price(new_price)}*\n"
                    f"👤 Aprobado por: *{ver_name}*"
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"No se pudo notificar al contratista: {e}")

        await update.message.reply_text(
            f"✅ Entrega #{delivery_id} aprobada con precio {fmt_price(new_price)}",
            parse_mode="Markdown"
        )


# ═══════════════════════════════════════════════════════════════
# /MISTOTAL
# ═══════════════════════════════════════════════════════════════

async def mis_total(update: Update, context: ContextTypes.DEFAULT_TYPE):
    worker = get_worker(update.effective_user.id)
    if not worker:
        await update.message.reply_text("❌ No estás registrado en el sistema.")
        return

    today      = datetime.now()
    week_start = (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")

    r = supabase.table("deliveries").select("*")\
        .eq("worker_id", worker["id"])\
        .eq("status", "approved")\
        .gte("created_at", week_start)\
        .order("created_at")\
        .execute()

    deliveries = r.data
    total      = sum(float(d["final_price"]) for d in deliveries)

    if not deliveries:
        await update.message.reply_text(
            f"📊 *Semana actual de {worker['name']}*\n\n"
            f"Aún no tienes entregas aprobadas esta semana.",
            parse_mode="Markdown"
        )
        return

    lines = [f"📊 *Semana actual · {worker['name']}*\n"]
    for d in deliveries:
        label = "🔴" if d["is_special"] else "🟢"
        fecha = d["created_at"][:10]
        oficio_info = ""
        if d.get("notes") and "Oficio:" in d["notes"]:
            oficio_info = f" · {d['notes'].replace('Oficio: ', '')}"
        lines.append(f"{label} `{d['fve']}` — {d['product_name'][:30]}{oficio_info}")
        lines.append(f"   💰 {fmt_price(d['final_price'])} · {fecha}")

    lines.append(f"\n{'─'*28}")
    lines.append(f"💰 *TOTAL APROBADO: {fmt_price(total)}*")
    lines.append(f"📦 Productos: {len(deliveries)}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════
# /PENDIENTES
# ═══════════════════════════════════════════════════════════════

async def pendientes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_verifier(update.effective_user.id):
        await update.message.reply_text("❌ No tienes permisos para este comando.")
        return

    r = supabase.table("deliveries")\
        .select("*, workers(name, area)")\
        .eq("status", "pending")\
        .order("created_at")\
        .execute()

    items = r.data
    if not items:
        await update.message.reply_text("✅ No hay entregas pendientes de aprobación.")
        return

    lines = [f"⏳ *Entregas pendientes ({len(items)})*\n"]
    for d in items:
        w     = d.get("workers", {})
        label = "🔴 ESPECIAL" if d["is_special"] else "🟢 lista"
        fecha = d["created_at"][:16].replace("T", " ")
        oficio_info = ""
        if d.get("notes") and "Oficio:" in str(d.get("notes", "")):
            oficio_info = f"\n  🏷️ {d['notes']}"
        lines.append(
            f"• *{w.get('name','?')}*\n"
            f"  `{d['fve']}` — {d['product_name'][:35]}\n"
            f"  {fmt_price(d['final_price'])} · {label} · {fecha}"
            f"{oficio_info}\n"
            f"  ID: `{d['id']}`"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════
# /RESUMEN
# ═══════════════════════════════════════════════════════════════

async def resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_verifier(update.effective_user.id):
        await update.message.reply_text("❌ No tienes permisos para este comando.")
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "📋 *Uso:*\n"
            "`/resumen [nombre] [semana|quincena]`\n\n"
            "_Ejemplos:_\n"
            "`/resumen Joselyn semana`\n"
            "`/resumen Joselyn quincena`",
            parse_mode="Markdown"
        )
        return

    worker_name = args[0]
    period      = args[1].lower() if len(args) > 1 else "semana"
    days_back   = 15 if period == "quincena" else 7
    start_date  = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")

    wr = supabase.table("workers").select("*").ilike("name", f"%{worker_name}%").execute()
    if not wr.data:
        await update.message.reply_text(f"❌ No encontré ningún contratista con nombre '{worker_name}'.")
        return

    worker = wr.data[0]

    r = supabase.table("deliveries").select("*")\
        .eq("worker_id", worker["id"])\
        .eq("status", "approved")\
        .gte("created_at", start_date)\
        .order("created_at")\
        .execute()

    deliveries = r.data
    total      = sum(float(d["final_price"]) for d in deliveries)
    specials   = [d for d in deliveries if d["is_special"]]

    period_label = f"última {period} ({start_date} → hoy)"

    lines = [
        f"🧾 *CUENTA DE COBRO*",
        f"👷 {worker['name']}",
        f"📅 {period_label}",
        f"{'─' * 30}",
    ]

    if not deliveries:
        lines.append("⚠️ No hay entregas aprobadas en este período.")
    else:
        for d in deliveries:
            label = "🔴" if d["is_special"] else "🟢"
            fecha = d["created_at"][:10]
            oficio_info = ""
            if d.get("notes") and "Oficio:" in str(d.get("notes", "")):
                oficio_info = f" · {d['notes'].replace('Oficio: ', '')}"
            lines.append(
                f"\n{label} *{d['product_name']}*{oficio_info}\n"
                f"   FVE: `{d['fve']}` · {fecha}\n"
                f"   💰 {fmt_price(d['final_price'])}"
                f"{' _(especial)_' if d['is_special'] else ''}\n"
                f"   ✅ Aprobó: {d.get('approved_by', '?')}"
            )

    lines.append(f"\n{'─' * 30}")
    lines.append(f"📦 Total entregas: {len(deliveries)}")
    lines.append(f"🔴 Precios especiales: {len(specials)}")
    lines.append(f"💰 *TOTAL BRUTO: {fmt_price(total)}*")
    lines.append(f"\n_Para exportar a Excel usa el dashboard web._")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════
# /PRECIOS
# ═══════════════════════════════════════════════════════════════

async def precios(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_verifier(update.effective_user.id):
        await update.message.reply_text("❌ Solo los verificadores pueden consultar precios.")
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "🔍 Usa `/precios [término]` para buscar.\n"
            "_Ejemplo:_ `/precios sofa cama`\n\n"
            "Oficios: corte\\_costura, tapiceria, carpinteria, esqueleteria, pintura",
            parse_mode="Markdown"
        )
        return

    term = " ".join(args)
    r    = supabase.table("price_list").select("*").eq("active", True).ilike("product_name", f"%{term}%").execute()

    if not r.data:
        await update.message.reply_text(f"❌ No encontré precios para '{term}'.")
        return

    lines = [f"📋 *Precios para '{term}'*\n"]
    for p in r.data[:12]:
        oficio_label = OFICIOS.get(p.get("oficio", ""), p.get("oficio", ""))
        if p.get("oficio") == "corte_costura" and p.get("precio_corte"):
            detalle = (f"Corte: {fmt_price(p.get('precio_corte'))} · "
                      f"Costura: {fmt_price(p.get('precio_costura'))} · ")
        else:
            detalle = ""
        lines.append(
            f"• *{p['product_name']}*\n"
            f"  {oficio_label}\n"
            f"  {detalle}*Total: {fmt_price(p['precio_total'])}*"
        )

    if len(r.data) > 12:
        lines.append(f"\n_...y {len(r.data)-12} más. Refina la búsqueda._")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("reportar", reportar_start)],
        states={
            OFICIO:        [CallbackQueryHandler(got_oficio,       pattern=r"^oficio_")],
            FVE:           [MessageHandler(filters.TEXT & ~filters.COMMAND, got_fve)],
            PRODUCT_NAME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, got_product_name)],
            PRICE_TYPE:    [CallbackQueryHandler(got_price_type,   pattern=r"^type_")],
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

    app.add_handler(CallbackQueryHandler(
        handle_verification, pattern=r"^(approve|reject|modify)_"
    ))

    # Texto libre: primero intenta IA para verificadores, luego flujo normal
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, handle_ai_question
    ))

    logger.info("🏭 Bot Livinghouse iniciado y escuchando...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════
# ASISTENTE IA — GEMINI
# ═══════════════════════════════════════════════════════════════

from google import genai as google_genai

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

def get_context_data() -> str:
    """Obtiene datos actuales de Supabase para darle contexto a Gemini."""
    try:
        # Entregas de los últimos 30 días
        since = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        
        deliveries = supabase.table("deliveries")\
            .select("*, workers(name)")\
            .gte("created_at", since)\
            .order("created_at", desc=True)\
            .execute().data

        workers = supabase.table("workers")\
            .select("*").eq("activo", True).execute().data

        # Formatear entregas para el contexto
        lines = ["=== DATOS DE PRODUCCIÓN LIVINGHOUSE (últimos 30 días) ===\n"]
        
        # Resumen por contratista
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

        # Entregas recientes
        lines.append("\nÚLTIMAS 20 ENTREGAS:")
        for d in deliveries[:20]:
            nombre = d.get("workers", {}).get("name", "?")
            fecha = d["created_at"][:10]
            oficio = ""
            if d.get("notes") and "Oficio:" in str(d.get("notes", "")):
                oficio = d["notes"].replace("Oficio: ", "")
            lines.append(
                f"  - {fecha} | {nombre} | {d['product_name'][:40]} | "
                f"{oficio} | ${float(d.get('final_price',0)):,.0f} | {d['status']}"
            )

        # Pendientes
        pendientes = [d for d in deliveries if d["status"] == "pending"]
        lines.append(f"\nENTREGAS PENDIENTES DE APROBACIÓN: {len(pendientes)}")
        for d in pendientes[:5]:
            nombre = d.get("workers", {}).get("name", "?")
            lines.append(f"  - {nombre} | {d['product_name'][:40]} | ${float(d.get('final_price',0)):,.0f}")

        # Contratistas registrados
        lines.append(f"\nCONTRATISTAS REGISTRADOS ({len(workers)}):")
        for w in workers:
            lines.append(f"  - {w['name']} (ID: {w['id']})")

        return "\n".join(lines)

    except Exception as e:
        logger.error(f"Error obteniendo contexto: {e}")
        return "No se pudo obtener datos de la base de datos."


async def ask_gemini(question: str, user_name: str) -> str:
    """Envía una pregunta a Gemini con el contexto de los datos de Livinghouse."""
    if not GEMINI_API_KEY:
        return "⚠️ La clave de Gemini no está configurada."

    try:
        context = get_context_data()
        
        system_prompt = f"""Eres el asistente inteligente del sistema de producción de Livinghouse, 
una fábrica de muebles en Manizales, Colombia.

Tu trabajo es responder preguntas sobre producción, contratistas, entregas y pagos 
basándote ÚNICAMENTE en los datos que te proporcionan. Responde en español, 
de forma clara y concisa. Usa emojis apropiados. Si no tienes los datos para 
responder algo, dilo claramente.

Quien pregunta: {user_name}

{context}"""

        client = google_genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=f"{system_prompt}\n\nPregunta: {question}"
        )
        return response.text

    except Exception as e:
        logger.error(f"Error con Gemini: {e}")
        return f"⚠️ No pude procesar tu pregunta. Error: {str(e)[:100]}"


async def handle_ai_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja preguntas en lenguaje natural de verificadores."""
    if not is_verifier(update.effective_user.id):
        return  # Solo verificadores

    text = update.message.text.strip()
    
    # Ignorar si parece un comando o respuesta a flujo de verificación
    if text.startswith("/"):
        return
    if "pending_rejection" in context.user_data or "pending_modification" in context.user_data:
        await handle_verifier_text(update, context)
        return

    # Si el mensaje parece una pregunta o consulta, responder con IA
    palabras_clave = [
        "cuánto", "cuanto", "cuál", "cual", "quién", "quien",
        "qué", "que", "cómo", "como", "cuántos", "cuantos",
        "muéstrame", "muestrame", "dame", "dime", "lista",
        "resumen", "total", "semana", "quincena", "mes",
        "pendiente", "aprobad", "rechazad", "contratista",
        "joselyn", "cuántas", "cuantas", "hoy", "ayer",
        "producto", "entrega", "precio", "oficio"
    ]
    
    text_lower = text.lower()
    es_pregunta = any(kw in text_lower for kw in palabras_clave) or "?" in text

    if not es_pregunta:
        await handle_verifier_text(update, context)
        return

    # Mostrar "escribiendo..."
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action="typing"
    )

    user_name = update.effective_user.first_name
    respuesta = await ask_gemini(text, user_name)

    await update.message.reply_text(
        f"🤖 {respuesta}",
        parse_mode="Markdown"
    )
