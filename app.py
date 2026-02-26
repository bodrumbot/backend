import os
import logging
import asyncio
import psycopg2
import psycopg2.extras
from datetime import datetime
from typing import Optional, Dict, Any, List

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters
)
from aiohttp import web
import aiohttp_cors
import json
import base64

load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==========================================
# ENVIRONMENT VARIABLES
# ==========================================

PORT = int(os.getenv("PORT", "3000"))

DATABASE_URL = (os.getenv("DATABASE_PUBLIC_URL") or 
                os.getenv("DATABASE_URL") or 
                os.getenv("DATABASE_PRIVATE_URL") or
                os.getenv("POSTGRES_URL"))

TOKEN = os.getenv("TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
WEBAPP_URL = os.getenv("WEBAPP_URL", "")

RAILWAY_PUBLIC_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
RAILWAY_STATIC_URL = os.getenv("RAILWAY_STATIC_URL", "")

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
if not WEBHOOK_URL and RAILWAY_PUBLIC_DOMAIN:
    WEBHOOK_URL = f"https://{RAILWAY_PUBLIC_DOMAIN}"
elif not WEBHOOK_URL and RAILWAY_STATIC_URL:
    WEBHOOK_URL = RAILWAY_STATIC_URL

logger.info(f"🔧 Configuration:")
logger.info(f"   PORT: {PORT}")
logger.info(f"   WEBHOOK_URL: {WEBHOOK_URL}")
logger.info(f"   WEBAPP_URL: {WEBAPP_URL}")
logger.info(f"   DATABASE_URL set: {bool(DATABASE_URL)}")
logger.info(f"   TOKEN set: {bool(TOKEN)}")
logger.info(f"   ADMIN_CHAT_ID: {ADMIN_CHAT_ID}")

application = None

# ==========================================
# DATABASE FUNCTIONS
# ==========================================

def get_db_connection():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL not set!")
    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        return conn
    except Exception as e:
        logger.error(f"Database connection error: {e}")
        raise

def format_price(price: int) -> str:
    return f"{price:,}".replace(",", " ")

def check_column_exists(table_name, column_name):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT 1 FROM information_schema.columns 
            WHERE table_name = %s AND column_name = %s
        """, (table_name, column_name))
        result = cur.fetchone()
        cur.close()
        return result is not None
    except Exception as e:
        logger.error(f"Check column error: {e}")
        return False
    finally:
        if conn:
            conn.close()

def get_order(order_id: str) -> Optional[Dict[str, Any]]:
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
        result = cur.fetchone()
        cur.close()
        
        if result:
            order_dict = dict(result)
            for key in ['created_at', 'accepted_at', 'rejected_at', 'paid_at', 'initiated_at']:
                if order_dict.get(key) and hasattr(order_dict[key], 'isoformat'):
                    order_dict[key] = order_dict[key].isoformat()
            return order_dict
        return None
    except Exception as e:
        logger.error(f"Get order error: {e}")
        return None
    finally:
        if conn:
            conn.close()

def check_order_initiated(order_id: str) -> Optional[Dict]:
    """Tekshirish - buyurtma allaqachon boshlanganmi"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT order_id, initiated_from, initiated_at, status, name, phone, items, total
            FROM orders 
            WHERE order_id = %s
        """, (order_id,))
        result = cur.fetchone()
        cur.close()
        return dict(result) if result else None
    except Exception as e:
        logger.error(f"Check order initiated error: {e}")
        return None
    finally:
        if conn:
            conn.close()

def mark_order_initiated(order_id: str, source: str) -> bool:
    """Buyurtma qayerdan boshlanganini belgilash"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            UPDATE orders 
            SET initiated_from = %s, initiated_at = %s
            WHERE order_id = %s
            RETURNING *
        """, (source, datetime.utcnow().isoformat(), order_id))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        logger.error(f"Mark order initiated error: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            conn.close()

def create_order(data: Dict) -> Optional[Dict]:
    """Yangi buyurtma yaratish"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        items = data.get('items', [])
        if isinstance(items, list):
            items_json = json.dumps(items)
        else:
            items_json = items
        
        screenshot = data.get('screenshot')
        screenshot_name = data.get('screenshotName', '')
        
        has_screenshot_col = check_column_exists('orders', 'screenshot')
        has_screenshot_name_col = check_column_exists('orders', 'screenshot_name')
        has_initiated_col = check_column_exists('orders', 'initiated_from')
        
        logger.info(f"📊 Database columns: screenshot={has_screenshot_col}, initiated_from={has_initiated_col}")
        
        if has_screenshot_col and has_screenshot_name_col and has_initiated_col:
            # Full insert
            cur.execute("""
                INSERT INTO orders (
                    order_id, name, phone, items, total, 
                    status, payment_status, payment_method, 
                    location, tg_id, notified, created_at,
                    screenshot, screenshot_name, initiated_from
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
            """, (
                data.get('orderId'), data.get('name'), data.get('phone'),
                items_json, data.get('total'), data.get('status', 'pending_verification'),
                data.get('paymentStatus', 'pending_verification'), data.get('paymentMethod', 'payme'),
                data.get('location'), data.get('tgId'), False, datetime.utcnow(),
                screenshot, screenshot_name, data.get('initiated_from')
            ))
        elif has_screenshot_col and has_screenshot_name_col:
            # Without initiated_from
            cur.execute("""
                INSERT INTO orders (
                    order_id, name, phone, items, total, 
                    status, payment_status, payment_method, 
                    location, tg_id, notified, created_at,
                    screenshot, screenshot_name
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
            """, (
                data.get('orderId'), data.get('name'), data.get('phone'),
                items_json, data.get('total'), data.get('status', 'pending_verification'),
                data.get('paymentStatus', 'pending_verification'), data.get('paymentMethod', 'payme'),
                data.get('location'), data.get('tgId'), False, datetime.utcnow(),
                screenshot, screenshot_name
            ))
        else:
            # Basic insert
            logger.warning("⚠️ Using basic insert without screenshot columns")
            cur.execute("""
                INSERT INTO orders (
                    order_id, name, phone, items, total, 
                    status, payment_status, payment_method, 
                    location, tg_id, notified, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
            """, (
                data.get('orderId'), data.get('name'), data.get('phone'),
                items_json, data.get('total'), data.get('status', 'pending_verification'),
                data.get('paymentStatus', 'pending_verification'), data.get('paymentMethod', 'payme'),
                data.get('location'), data.get('tgId'), False, datetime.utcnow()
            ))
        
        result = cur.fetchone()
        conn.commit()
        cur.close()
        
        if result:
            order_dict = dict(result)
            for key in ['created_at', 'accepted_at', 'rejected_at', 'paid_at', 'initiated_at']:
                if order_dict.get(key) and hasattr(order_dict[key], 'isoformat'):
                    order_dict[key] = order_dict[key].isoformat()
            return order_dict
        return None
        
    except Exception as e:
        logger.error(f"Create order error: {e}")
        if conn:
            conn.rollback()
        return None
    finally:
        if conn:
            conn.close()

def get_all_orders() -> List[Dict]:
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM orders 
            ORDER BY created_at DESC 
            LIMIT 100
        """)
        orders = cur.fetchall()
        cur.close()
        
        result = []
        for order in orders:
            order_dict = dict(order)
            for key in ['created_at', 'accepted_at', 'rejected_at', 'paid_at', 'initiated_at']:
                if order_dict.get(key) and hasattr(order_dict[key], 'isoformat'):
                    order_dict[key] = order_dict[key].isoformat()
            result.append(order_dict)
        return result
    except Exception as e:
        logger.error(f"Get all orders error: {e}")
        return []
    finally:
        if conn:
            conn.close()

def update_order_status(order_id: str, status: str, **kwargs) -> Optional[Dict[str, Any]]:
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        update_data = {'status': status}
        if status == 'accepted':
            update_data['accepted_at'] = datetime.utcnow().isoformat()
        elif status == 'rejected':
            update_data['rejected_at'] = datetime.utcnow().isoformat()
        
        for key, val in kwargs.items():
            if val is not None:
                update_data[key] = val
        
        fields = []
        values = []
        for key, val in update_data.items():
            fields.append(f"{key} = %s")
            values.append(val)
        values.append(order_id)
        
        query = f"UPDATE orders SET {', '.join(fields)} WHERE order_id = %s RETURNING *"
        cur.execute(query, values)
        result = cur.fetchone()
        conn.commit()
        cur.close()
        
        if result:
            order_dict = dict(result)
            for key in ['created_at', 'accepted_at', 'rejected_at', 'paid_at', 'initiated_at']:
                if order_dict.get(key) and hasattr(order_dict[key], 'isoformat'):
                    order_dict[key] = order_dict[key].isoformat()
            return order_dict
        return None
        
    except Exception as e:
        logger.error(f"Update error: {e}")
        if conn:
            conn.rollback()
        return None
    finally:
        if conn:
            conn.close()

def get_new_orders(since: str = None) -> List[Dict]:
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        if since:
            cur.execute("""
                SELECT * FROM orders 
                WHERE status IN ('pending_verification', 'payment_pending')
                AND created_at > %s
                ORDER BY created_at DESC
            """, (since,))
        else:
            cur.execute("""
                SELECT * FROM orders 
                WHERE status IN ('pending_verification', 'payment_pending')
                ORDER BY created_at DESC
            """)
        
        orders = cur.fetchall()
        cur.close()
        
        result = []
        for order in orders:
            order_dict = dict(order)
            for key in ['created_at', 'accepted_at', 'rejected_at', 'paid_at', 'initiated_at']:
                if order_dict.get(key) and hasattr(order_dict[key], 'isoformat'):
                    order_dict[key] = order_dict[key].isoformat()
            result.append(order_dict)
        return result
    except Exception as e:
        logger.error(f"Get new orders error: {e}")
        return []
    finally:
        if conn:
            conn.close()

# ==========================================
# ORDER CHECKING
# ==========================================

last_notified_orders = set()
last_check_time = None

async def check_new_orders(context: ContextTypes.DEFAULT_TYPE):
    global last_check_time
    
    try:
        orders = get_new_orders(last_check_time)
        
        if orders:
            logger.info(f"📊 Yangi buyurtmalar: {len(orders)} ta")
            
            for order in orders:
                order_id = order['order_id']
                
                if order_id in last_notified_orders:
                    continue
                
                if not order.get('notified'):
                    await notify_admin(context, order)
                    last_notified_orders.add(order_id)
        
        last_check_time = datetime.utcnow().isoformat()
        
    except Exception as e:
        logger.error(f"❌ Tekshirish xatosi: {e}")

async def notify_admin(context: ContextTypes.DEFAULT_TYPE, order: Dict):
    try:
        items = order.get('items', [])
        if isinstance(items, str):
            items = json.loads(items)
        
        items_text = "\n".join([f"• {i.get('name')} x{i.get('qty')}" for i in items])
        
        has_screenshot = order.get('screenshot') or order.get('screenshot_name')
        screenshot_indicator = " 📸" if has_screenshot else ""
        
        initiated = order.get('initiated_from')
        source_text = ""
        if initiated == 'webapp':
            source_text = "\n📱 <b>Web App</b> orqali"
        elif initiated == 'bot':
            source_text = "\n🤖 <b>Bot</b> orqali"
        
        message = f"""🛎️ <b>YANGI BUYURTMA!{screenshot_indicator}</b>{source_text}

🆔 Buyurtma: #{order.get('order_id', 'N/A')[-6:]}
👤 Mijoz: {order.get('name')}
📞 Telefon: +998 {order.get('phone')}
💵 Summa: {format_price(order.get('total', 0))} so'm
💳 To'lov: {order.get('payment_method', 'N/A').upper()} ✅

🍽 Mahsulotlar:
{items_text}

⏰ {datetime.now().strftime('%H:%M:%S')}"""

        if initiated == 'bot':
            keyboard = [
                [
                    InlineKeyboardButton("✅ Qabul qilish", callback_data=f"accept_{order.get('order_id')}"),
                    InlineKeyboardButton("❌ Bekor qilish", callback_data=f"reject_{order.get('order_id')}")
                ],
                [InlineKeyboardButton("🌐 Admin Panel", web_app=WebAppInfo(url=f"{WEBAPP_URL}/admin.html"))]
            ]
        else:
            keyboard = [
                [
                    InlineKeyboardButton("✅ Qabul qilish", callback_data=f"accept_{order.get('order_id')}"),
                    InlineKeyboardButton("❌ Bekor qilish", callback_data=f"reject_{order.get('order_id')}")
                ],
                [
                    InlineKeyboardButton("📸 Skrinshot so'rash", callback_data=f"request_screenshot_{order.get('order_id')}")
                ],
                [InlineKeyboardButton("🌐 Admin Panel", web_app=WebAppInfo(url=f"{WEBAPP_URL}/admin.html"))]
            ]
        
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
        
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("UPDATE orders SET notified = true WHERE order_id = %s", (order.get('order_id'),))
            conn.commit()
            cur.close()
        except Exception as e:
            logger.error(f"Mark notified error: {e}")
        finally:
            if conn:
                conn.close()
        
        logger.info(f"✅ Admin ga xabar yuborildi: {order.get('order_id')}")
        
    except Exception as e:
        logger.error(f"❌ Xabar yuborish xatosi: {e}")

# ==========================================
# TELEGRAM HANDLERS
# ==========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_admin = user.id == ADMIN_CHAT_ID
    
    if is_admin:
        keyboard = [
            [InlineKeyboardButton("🍽️ Menyu", web_app=WebAppInfo(url=WEBAPP_URL))],
            [InlineKeyboardButton("⚙️ Admin Panel", web_app=WebAppInfo(url=f"{WEBAPP_URL}/admin.html"))]
        ]
        await update.message.reply_text(
            f"👋 Salom, Admin <b>{user.first_name}</b>!\n\n"
            f"🤖 Bot faol. Yangi buyurtmalar avtomatik keladi.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
    else:
        keyboard = [[InlineKeyboardButton("🍽️ Menyuni ko'rish", web_app=WebAppInfo(url=WEBAPP_URL))]]
        await update.message.reply_text(
            f"👋 Salom, <b>{user.first_name}</b>!\n\n"
            f"🍽️ <b>BODRUM</b> restoraniga xush kelibsiz!",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    # Payment done from bot
    if data.startswith("payment_done_"):
        await handle_payment_confirmation(update, context)
        return
    
    # Cancel payment
    if data.startswith("cancel_payment_"):
        await handle_cancel_payment(update, context)
        return
    
    # Request screenshot (admin)
    if data.startswith("request_screenshot_"):
        await handle_request_screenshot(update, context)
        return
    
    # Accept/Reject
    if data.startswith(("accept_", "reject_")):
        action, order_id = data.split("_", 1)
        order = get_order(order_id)
        
        if not order:
            await query.edit_message_text("❌ Buyurtma topilmadi!")
            return
        
        new_status = 'accepted' if action == 'accept' else 'rejected'
        updated_order = update_order_status(order_id, new_status)
        
        if updated_order:
            status_text = "✅ <b>QABUL QILINDI</b>" if action == 'accept' else "❌ <b>BEKOR QILINDI</b>"
            
            items = order.get('items', [])
            if isinstance(items, str):
                items = json.loads(items)
            
            items_text = "\n".join([f"• {i.get('name')} x{i.get('qty')}" for i in items])
            
            has_screenshot = order.get('screenshot') or order.get('screenshot_name')
            screenshot_info = "\n\n📸 Skrinshot: Mavjud" if has_screenshot else ""
            
            message = f"""{status_text}

🆔 Buyurtma: #{order_id[-6:]}
👤 Mijoz: {order.get('name')}
📞 Telefon: +998 {order.get('phone')}
💵 Summa: {format_price(order.get('total', 0))} so'm

🍽 Mahsulotlar:
{items_text}{screenshot_info}

⏰ {datetime.now().strftime('%H:%M:%S')}"""
            
            await query.edit_message_text(message, parse_mode='HTML')
            
            tg_id = order.get('tg_id')
            if tg_id:
                try:
                    if action == 'accept':
                        msg = f"✅ Buyurtmangiz #{order_id[-6:]} qabul qilindi!\n\nTez orada yetkazib beramiz! 🚀"
                    else:
                        msg = f"❌ Buyurtmangiz #{order_id[-6:]} bekor qilindi.\n\nQo'llab-quvvatlash: +998901234567"
                    
                    await context.bot.send_message(chat_id=tg_id, text=msg)
                except Exception as e:
                    logger.error(f"Mijozga xabar yuborishda xato: {e}")
        else:
            await query.edit_message_text("❌ Xatolik yuz berdi!")

async def handle_payment_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bot da 'Ha, to'lov qildim' bosilganda"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    order_id = data.replace("payment_done_", "")
    
    # Tekshirish - allaqachon web app dan boshlanganmi
    order = check_order_initiated(order_id)
    
    if order and order.get('initiated_from') == 'webapp':
        await query.edit_message_text(
            "⚠️ Bu buyurtma allaqachon Web App orqali boshlangan.\n"
            "Iltimos, skrinshotni Web App da yuklang.",
            parse_mode='HTML'
        )
        return
    
    if order and order.get('initiated_from') == 'bot':
        await query.answer("Allaqachon boshlangan!", show_alert=True)
        return
    
    # Bot da boshlash
    mark_order_initiated(order_id, 'bot')
    
    keyboard = [[InlineKeyboardButton("❌ Bekor qilish", callback_data=f"cancel_payment_{order_id}")]]
    
    await query.edit_message_text(
        f"✅ <b>To'lov tasdiqlandi!</b>\n\n"
        f"🆔 Buyurtma: #{order_id[-6:]}\n\n"
        f"📸 <b>Endi skrinshot yuboring:</b>\n"
        f"Payme dan to'lov muvaffaqiyatli bo'lgani haqida skrinshot yuboring.\n\n"
        f"⏳ Kutmoqda...",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )
    
    context.user_data['awaiting_screenshot'] = order_id

async def handle_request_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin skrinshot so'rasa"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    order_id = data.replace("request_screenshot_", "")
    
    order = get_order(order_id)
    if not order:
        await query.answer("Buyurtma topilmadi!", show_alert=True)
        return
    
    tg_id = order.get('tg_id')
    if not tg_id:
        await query.answer("Mijoz ID topilmadi!", show_alert=True)
        return
    
    # Mijozga xabar
    try:
        keyboard = [[
            InlineKeyboardButton("✅ Ha, to'lov qildim", callback_data=f"payment_done_{order_id}"),
            InlineKeyboardButton("❌ Bekor qilish", callback_data=f"cancel_payment_{order_id}")
        ]]
        
        await context.bot.send_message(
            chat_id=tg_id,
            text=f"📸 <b>Skrinshot talab qilindi</b>\n\n"
                 f"🆔 Buyurtma: #{order_id[-6:]}\n\n"
                 f"Iltimos, Payme to'lov skrinshotini yuboring yoki to'lov qilgan bo'lsangiz 'Ha' ni bosing.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
        
        await query.answer("Mijozga xabar yuborildi!", show_alert=True)
        
    except Exception as e:
        logger.error(f"Screenshot request error: {e}")
        await query.answer("Xatolik yuz berdi!", show_alert=True)

async def handle_cancel_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    order_id = data.replace("cancel_payment_", "")
    
    if 'awaiting_screenshot' in context.user_data:
        del context.user_data['awaiting_screenshot']
    
    await query.edit_message_text(
        f"❌ Buyurtma #{order_id[-6:]} bekor qilindi.\n"
        f"Yangi buyurtma uchun /start ni bosing."
    )

async def handle_screenshot_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bot da skrinshot qabul qilish"""
    if 'awaiting_screenshot' not in context.user_data:
        return
    
    order_id = context.user_data['awaiting_screenshot']
    
    if update.message.photo:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        
        photo_bytes = await file.download_as_bytearray()
        screenshot_base64 = "data:image/jpeg;base64," + base64.b64encode(photo_bytes).decode()
        
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                UPDATE orders 
                SET screenshot = %s, screenshot_name = %s, status = 'pending_verification'
                WHERE order_id = %s
                RETURNING *
            """, (screenshot_base64, f"bot_{photo.file_id}.jpg", order_id))
            result = cur.fetchone()
            conn.commit()
            cur.close()
            
            if result:
                await update.message.reply_text(
                    "✅ <b>Skrinshot qabul qilindi!</b>\n\n"
                    f"🆔 Buyurtma: #{order_id[-6:]}\n"
                    "⏳ Admin tekshiruvida...\n\n"
                    "Tasdiqlangach yetkazib beramiz!",
                    parse_mode='HTML'
                )
                
                # Admin ga xabar
                order = dict(result)
                items = json.loads(order['items']) if isinstance(order['items'], str) else order['items']
                items_text = "\n".join([f"• {i.get('name')} x{i.get('qty')}" for i in items])
                
                admin_msg = f"""🛎️ <b>YANGI BUYURTMA! 📸</b>

🆔 Buyurtma: #{order_id[-6:]}
👤 Mijoz: {order.get('name')}
📞 Telefon: +998 {order.get('phone')}
💵 Summa: {format_price(order.get('total', 0))} so'm
📱 Manba: <b>Bot orqali</b>

🍽 Mahsulotlar:
{items_text}

⏰ {datetime.now().strftime('%H:%M:%S')}"""

                keyboard = [
                    [
                        InlineKeyboardButton("✅ Qabul qilish", callback_data=f"accept_{order_id}"),
                        InlineKeyboardButton("❌ Bekor qilish", callback_data=f"reject_{order_id}")
                    ],
                    [InlineKeyboardButton("🌐 Admin Panel", web_app=WebAppInfo(url=f"{WEBAPP_URL}/admin.html"))]
                ]
                
                await context.bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=admin_msg,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='HTML'
                )
                
        except Exception as e:
            logger.error(f"Screenshot save error: {e}")
            await update.message.reply_text("❌ Xatolik yuz berdi. Iltimos qayta urinib ko'ring.")
        finally:
            if conn:
                conn.close()
        
        del context.user_data['awaiting_screenshot']
        
    else:
        await update.message.reply_text("📸 Iltimos, rasm yuboring (skrinshot).")

async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_CHAT_ID:
        return
    
    await update.message.reply_text("🔍 Tekshirilmoqda...")
    await check_new_orders(context)
    await update.message.reply_text("✅ Tekshirish tugadi!")

# ==========================================
# HTTP API HANDLERS
# ==========================================

async def health_handler(request):
    return web.json_response({
        "status": "ok", 
        "service": "bodrum-bot",
        "timestamp": datetime.utcnow().isoformat(),
        "database_url_set": bool(DATABASE_URL),
        "webhook_url": WEBHOOK_URL
    })

async def get_orders_handler(request):
    try:
        orders = get_all_orders()
        return web.json_response(orders)
    except Exception as e:
        logger.error(f"API get orders error: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def get_new_orders_handler(request):
    try:
        orders = get_new_orders()
        return web.json_response(orders)
    except Exception as e:
        logger.error(f"API get new orders error: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def create_order_handler(request):
    try:
        data = await request.json()
        logger.info(f"📝 Yangi buyurtma: {data.get('orderId')}")
        
        order = create_order(data)
        
        if order:
            logger.info(f"✅ Buyurtma yaratildi: {order['order_id']}")
            return web.json_response(order, status=201)
        else:
            return web.json_response({"error": "Failed to create order"}, status=500)
        
    except Exception as e:
        logger.error(f"API create order error: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def get_order_handler(request):
    try:
        order_id = request.match_info['order_id']
        order = get_order(order_id)
        
        if not order:
            return web.json_response({"error": "Not found"}, status=404)
        
        return web.json_response(order)
    except Exception as e:
        logger.error(f"API get order error: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def check_order_initiated_handler(request):
    """Tekshirish - buyurtma allaqachon boshlanganmi"""
    try:
        order_id = request.match_info['order_id']
        order = check_order_initiated(order_id)
        
        if not order:
            return web.json_response({"error": "Not found"}, status=404)
        
        return web.json_response({
            "order_id": order.get('order_id'),
            "initiated_from": order.get('initiated_from'),
            "initiated_at": order.get('initiated_at'),
            "status": order.get('status')
        })
        
    except Exception as e:
        logger.error(f"API check initiated error: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def mark_order_initiated_handler(request):
    """Buyurtma boshlanganini belgilash"""
    try:
        data = await request.json()
        order_id = data.get('orderId')
        source = data.get('source')
        
        if not order_id or not source:
            return web.json_response({"error": "Missing orderId or source"}, status=400)
        
        existing = check_order_initiated(order_id)
        if existing and existing.get('initiated_from'):
            return web.json_response({
                "success": False,
                "message": "Order already initiated from " + existing.get('initiated_from'),
                "initiated_from": existing.get('initiated_from')
            })
        
        success = mark_order_initiated(order_id, source)
        
        if success:
            return web.json_response({
                "success": True,
                "message": f"Order marked as initiated from {source}"
            })
        else:
            return web.json_response({"error": "Failed to mark"}, status=500)
            
    except Exception as e:
        logger.error(f"API mark initiated error: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def update_order_handler(request):
    try:
        order_id = request.match_info['order_id']
        data = await request.json()
        
        logger.info(f"📝 Buyurtma yangilanmoqda: {order_id} - {data}")
        
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            
            updates = []
            values = []
            
            if 'status' in data:
                updates.append("status = %s")
                values.append(data['status'])
            if 'paymentStatus' in data:
                updates.append("payment_status = %s")
                values.append(data['paymentStatus'])
            if 'payment_method' in data:
                updates.append("payment_method = %s")
                values.append(data['payment_method'])
            if 'acceptedAt' in data:
                updates.append("accepted_at = %s")
                values.append(data['acceptedAt'])
            if 'rejectedAt' in data:
                updates.append("rejected_at = %s")
                values.append(data['rejectedAt'])
            if 'paidAt' in data:
                updates.append("paid_at = %s")
                values.append(data['paidAt'])
            
            if not updates:
                return web.json_response({"error": "No updates"}, status=400)
            
            values.append(order_id)
            query = f"UPDATE orders SET {', '.join(updates)} WHERE order_id = %s RETURNING *"
            
            cur.execute(query, values)
            result = cur.fetchone()
            conn.commit()
            cur.close()
            
            if not result:
                return web.json_response({"error": "Order not found"}, status=404)
            
            order_dict = dict(result)
            for key in ['created_at', 'accepted_at', 'rejected_at', 'paid_at', 'initiated_at']:
                if order_dict.get(key) and hasattr(order_dict[key], 'isoformat'):
                    order_dict[key] = order_dict[key].isoformat()
            
            logger.info(f"✅ Buyurtma yangilandi: {order_id}")
            return web.json_response(order_dict)
            
        except Exception as e:
            logger.error(f"Update handler error: {e}")
            if conn:
                conn.rollback()
            return web.json_response({"error": str(e)}, status=500)
        finally:
            if conn:
                conn.close()
                
    except Exception as e:
        logger.error(f"API update order error: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def webhook_handler(request):
    global application
    
    if application:
        try:
            data = await request.json()
            update = Update.de_json(data, application.bot)
            await application.process_update(update)
        except Exception as e:
            logger.error(f"Webhook processing error: {e}")
    
    return web.Response(text='OK')

# ==========================================
# WEBHOOK INIT
# ==========================================

async def init_webhook(app):
    global application
    
    if not TOKEN:
        logger.error("❌ TOKEN o'rnatilmagan!")
        return
    
    if not WEBHOOK_URL:
        logger.warning("⚠️ WEBHOOK_URL o'rnatilmagan! Polling mode ishlatiladi.")
        application = Application.builder().token(TOKEN).build()
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("test", test_command))
        application.add_handler(CallbackQueryHandler(callback_handler))
        application.add_handler(MessageHandler(filters.PHOTO, handle_screenshot_upload))
        
        job_queue = application.job_queue
        job_queue.run_repeating(check_new_orders, interval=10, first=5)
        
        await application.initialize()
        await application.start()
        logger.info("🤖 Bot polling mode da ishga tushdi!")
        return
    
    webhook_url = WEBHOOK_URL.rstrip('/')
    if not webhook_url.startswith('https://'):
        webhook_url = 'https://' + webhook_url
    
    full_webhook_url = f"{webhook_url}/webhook"
    
    logger.info(f"🔧 Webhook URL: {full_webhook_url}")
    
    try:
        temp_app = Application.builder().token(TOKEN).build()
        await temp_app.bot.delete_webhook(drop_pending_updates=True)
        await temp_app.shutdown()
        logger.info("✅ Eski webhook o'chirildi")
    except Exception as e:
        logger.warning(f"⚠️ Eski webhook o'chirishda xato: {e}")
    
    application = Application.builder().token(TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("test", test_command))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.PHOTO, handle_screenshot_upload))
    
    job_queue = application.job_queue
    job_queue.run_repeating(check_new_orders, interval=10, first=5)
    
    await application.initialize()
    await application.start()
    
    try:
        await application.bot.set_webhook(full_webhook_url)
        logger.info(f"✅ Webhook o'rnatildi: {full_webhook_url}")
    except Exception as e:
        logger.error(f"❌ Webhook xato: {e}")
    
    logger.info("🤖 Bot webhook mode da ishga tushdi!")

async def shutdown(app):
    global application
    if application:
        try:
            await application.stop()
            await application.shutdown()
            logger.info("🛑 Bot to'xtatildi")
        except Exception as e:
            logger.error(f"Shutdown xato: {e}")

# ==========================================
# MAIN
# ==========================================

def main():
    global application
    
    logger.info("🔧 Railway Webhook + HTTP API mode")
    
    if not PORT:
        logger.error("❌ PORT o'rnatilmagan!")
        return
    
    app = web.Application()
    
    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=True,
            expose_headers="*",
            allow_headers="*",
            allow_methods="*"
        )
    })
    
    # Routes
    app.router.add_get('/', health_handler)
    app.router.add_get('/health', health_handler)
    app.router.add_get('/api/orders', get_orders_handler)
    app.router.add_get('/api/orders/new', get_new_orders_handler)
    app.router.add_post('/api/orders', create_order_handler)
    app.router.add_get('/api/orders/{order_id}', get_order_handler)
    app.router.add_get('/api/orders/{order_id}/initiated', check_order_initiated_handler)
    app.router.add_post('/api/orders/initiated', mark_order_initiated_handler)
    app.router.add_put('/api/orders/{order_id}', update_order_handler)
    app.router.add_post('/webhook', webhook_handler)
    
    for route in list(app.router.routes()):
        cors.add(route)
    
    app.on_startup.append(init_webhook)
    app.on_cleanup.append(shutdown)
    
    host = '0.0.0.0'
    
    logger.info(f"🚀 Server ishga tushmoqda: {host}:{PORT}")
    
    web.run_app(
        app, 
        host=host, 
        port=PORT,
        access_log=logger,
        print=lambda *args: logger.info(' '.join(str(a) for a in args))
    )

if __name__ == "__main__":
    main()
