# ==========================================
# BODRUM BOT - TO'LIQ TO'G'RILANGAN VERSION
# ==========================================

import os
import logging
import asyncio
import psycopg2
import psycopg2.extras
from datetime import datetime
from typing import Optional, Dict, Any, List

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters
)
from aiohttp import web
import json
import base64
import threading

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
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")

# ⭐ MUHIM: ADMIN_CHAT_ID ni int ga o'tkazish
try:
    ADMIN_CHAT_ID_INT = int(ADMIN_CHAT_ID) if ADMIN_CHAT_ID else 0
except ValueError:
    logger.error(f"❌ ADMIN_CHAT_ID noto'g'ri formatda: {ADMIN_CHAT_ID}")
    ADMIN_CHAT_ID_INT = 0

logger.info(f"🔧 ADMIN_CHAT_ID: {ADMIN_CHAT_ID}, parsed: {ADMIN_CHAT_ID_INT}")

# Global application
application = None

# Vaqtinchalik saqlash
pending_confirmations = {}
confirmations_lock = threading.Lock()

# ==========================================
# DATABASE FUNCTIONS
# ==========================================

def init_database():
    """Jadval va column'larni avtomatik yaratish/yangilash"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # 1. Orders jadvali
        cur.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id SERIAL PRIMARY KEY,
                order_id VARCHAR(100) UNIQUE NOT NULL,
                name VARCHAR(255),
                phone VARCHAR(20),
                items JSONB,
                total INTEGER,
                status VARCHAR(50) DEFAULT 'pending_verification',
                payment_status VARCHAR(50) DEFAULT 'pending_verification',
                payment_method VARCHAR(50) DEFAULT 'payme',
                location VARCHAR(255),
                tg_id BIGINT,
                notified BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                accepted_at TIMESTAMP,
                rejected_at TIMESTAMP,
                paid_at TIMESTAMP,
                confirmed_at TIMESTAMP,
                admin_note TEXT
            )
        """)
        
        # 2. Users jadvali
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                tg_id BIGINT UNIQUE NOT NULL,
                name VARCHAR(255),
                phone VARCHAR(20),
                username VARCHAR(100),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 3. Yangi column'larni qo'shish
        new_columns = [
            ('screenshot', 'TEXT'),
            ('screenshot_name', 'VARCHAR(255)'),
            ('initiated_from', 'VARCHAR(50) DEFAULT \'webapp\'')
        ]
        
        for col_name, col_type in new_columns:
            try:
                cur.execute("""
                    SELECT column_name FROM information_schema.columns 
                    WHERE table_name = 'orders' AND column_name = %s
                """, (col_name,))
                
                if not cur.fetchone():
                    cur.execute(f"ALTER TABLE orders ADD COLUMN {col_name} {col_type}")
                    logger.info(f"✅ Column '{col_name}' qo'shildi")
                    
            except Exception as e:
                logger.warning(f"⚠️ Column '{col_name}' qo'shishda xato: {e}")
        
        # 4. Index'lar
        cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_order_id ON orders(order_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_tg_id ON users(tg_id)")
        
        conn.commit()
        cur.close()
        logger.info("✅ Database initialized successfully")
        return True
        
    except Exception as e:
        logger.error(f"❌ Database init error: {e}")
        return False
    finally:
        if conn:
            conn.close()

def get_db_connection():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL not set!")
    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        return conn
    except Exception as e:
        logger.error(f"Database connection error: {e}")
        raise

# ==========================================
# USER PROFILE FUNCTIONS
# ==========================================

def save_user_profile(tg_id: int, name: str, phone: str, username: str = None) -> bool:
    """Foydalanuvchi profilini saqlash yoki yangilash"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("""
            INSERT INTO users (tg_id, name, phone, username, updated_at)
            VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (tg_id) 
            DO UPDATE SET 
                name = EXCLUDED.name,
                phone = EXCLUDED.phone,
                username = EXCLUDED.username,
                updated_at = CURRENT_TIMESTAMP
            RETURNING *
        """, (tg_id, name, phone, username))
        
        result = cur.fetchone()
        conn.commit()
        cur.close()
        
        logger.info(f"✅ Profil saqlandi: {tg_id} - {name} - {phone}")
        return True
        
    except Exception as e:
        logger.error(f"❌ Profil saqlash xatosi: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            conn.close()

def get_user_profile(tg_id: int) -> Optional[Dict[str, Any]]:
    """Foydalanuvchi profilini olish"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("SELECT * FROM users WHERE tg_id = %s", (tg_id,))
        result = cur.fetchone()
        cur.close()
        
        if result:
            profile = dict(result)
            # ⭐ DATETIME FORMATLASH
            for key in ['created_at', 'updated_at']:
                if profile.get(key) and hasattr(profile[key], 'isoformat'):
                    profile[key] = profile[key].isoformat()
            return profile
        return None
        
    except Exception as e:
        logger.error(f"❌ Profil olish xatosi: {e}")
        return None
    finally:
        if conn:
            conn.close()

def get_user_orders(tg_id: int) -> List[Dict[str, Any]]:
    """Foydalanuvchining barcha buyurtmalarini olish"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM orders 
            WHERE tg_id = %s 
            ORDER BY created_at DESC
        """, (tg_id,))
        results = cur.fetchall()
        cur.close()
        
        orders = []
        for row in results:
            order_dict = dict(row)
            # Date formatlash
            for key in ['created_at', 'accepted_at', 'rejected_at', 'paid_at', 'confirmed_at']:
                if order_dict.get(key) and hasattr(order_dict[key], 'isoformat'):
                    order_dict[key] = order_dict[key].isoformat()
            orders.append(order_dict)
        
        return orders
        
    except Exception as e:
        logger.error(f"❌ Buyurtmalarni olish xatosi: {e}")
        return []
    finally:
        if conn:
            conn.close()

def format_price(price: int) -> str:
    return f"{price:,}".replace(",", " ")

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
            for key in ['created_at', 'accepted_at', 'rejected_at', 'paid_at', 'confirmed_at']:
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
        initiated_from = data.get('initiated_from', 'webapp')
        
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
            screenshot, screenshot_name, initiated_from
        ))
        
        result = cur.fetchone()
        conn.commit()
        cur.close()
        
        if result:
            order_dict = dict(result)
            for key in ['created_at', 'accepted_at', 'rejected_at', 'paid_at', 'confirmed_at']:
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
        elif status == 'confirmed':
            update_data['confirmed_at'] = datetime.utcnow().isoformat()
        
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
            for key in ['created_at', 'accepted_at', 'rejected_at', 'paid_at', 'confirmed_at']:
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

# ==========================================
# CORS HEADERS
# ==========================================

def get_cors_headers():
    return {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type, Authorization, X-Requested-With',
        'Access-Control-Max-Age': '86400',
    }

# ==========================================
# TELEGRAM BOT FUNCTIONS
# ==========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start handler - profil tekshirish bilan"""
    user = update.effective_user
    is_admin = user.id == ADMIN_CHAT_ID_INT
    
    # Admin uchun
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
        return
    
    # ODDIY FOYDALANUVCHI - avval profilni tekshirish
    profile = get_user_profile(user.id)
    
    if profile and profile.get('phone'):
        logger.info(f"✅ Mavjud profil topildi: {user.id}")
        await send_welcome_back(update, context, profile)
    else:
        logger.info(f"❌ Yangi foydalanuvchi: {user.id}")
        await request_phone(update, context)

async def request_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Telefon raqam so'rash"""
    user = update.effective_user
    
    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Telefon raqamni yuborish", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    
    await update.message.reply_text(
        f"👋 Salom, <b>{user.first_name}</b>!\n\n"
        f"🍽️ <b>BODRUM</b> restoraniga xush kelibsiz!\n\n"
        f"📱 Buyurtma berish uchun telefon raqamingizni yuboring:",
        reply_markup=keyboard,
        parse_mode='HTML'
    )

async def send_welcome_back(update: Update, context: ContextTypes.DEFAULT_TYPE, profile: Dict):
    """Qaytganda xush kelibsiz xabari"""
    name = profile.get('name', 'Foydalanuvchi')
    phone = profile.get('phone', '')
    
    formatted_phone = phone
    if len(phone) == 9:
        formatted_phone = f"{phone[:2]} {phone[2:5]} {phone[5:7]} {phone[7:]}"
    
    keyboard = [
        [InlineKeyboardButton("🍽️ Menyuni ko'rish", web_app=WebAppInfo(url=WEBAPP_URL))]
    ]
    
    await update.message.reply_text(
        f"👋 Salom, <b>{name}</b>!\n\n"
        f"🍽️ <b>BODRUM</b> restoraniga xush kelibsiz!\n\n"
        f"📞 Telefon: +998 {formatted_phone}\n\n"
        f"🛒 Menyudan buyurtma berishingiz mumkin:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def contact_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Contact (telefon raqam) qabul qilish - VA SAQLASH"""
    user = update.effective_user
    contact = update.message.contact
    
    if not contact:
        return
    
    phone = contact.phone_number
    
    if phone.startswith('+'):
        phone = phone[1:]
    
    if phone.startswith('998'):
        phone = phone[3:]
    
    phone = phone[-9:] if len(phone) > 9 else phone
    
    success = save_user_profile(
        tg_id=user.id,
        name=user.first_name or "Foydalanuvchi",
        phone=phone,
        username=user.username
    )
    
    if success:
        logger.info(f"✅ Yangi profil saqlandi: {user.id} - {phone}")
        
        await update.message.reply_text(
            "✅ <b>Ma'lumotlar saqlandi!</b>",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode='HTML'
        )
        
        await send_webapp_button(update, context, user.id, phone)
    else:
        await update.message.reply_text(
            "❌ Xatolik yuz berdi. Iltimos, qayta urinib ko'ring.",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode='HTML'
        )

async def send_webapp_button(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, phone: str = None):
    """Web App tugmasini yuborish"""
    user = update.effective_user
    name = user.first_name or "Foydalanuvchi"
    
    formatted_phone = phone or "---"
    if phone and len(phone) == 9:
        formatted_phone = f"{phone[:2]} {phone[2:5]} {phone[5:7]} {phone[7:]}"
    
    keyboard = [
        [InlineKeyboardButton("🍽️ Menyuni ko'rish", web_app=WebAppInfo(url=WEBAPP_URL))]
    ]
    
    await update.message.reply_text(
        f"✅ <b>Ma'lumotlar saqlandi!</b>\n\n"
        f"👤 Ism: {name}\n"
        f"📞 Telefon: +998 {formatted_phone}\n\n"
        f"🍽️ Endi menyudan buyurtma berishingiz mumkin:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def send_payment_confirmation_request(context: ContextTypes.DEFAULT_TYPE, order_id: str, tg_id: int, total: int, items: list = None):
    """Mijozga to'lov tasdiqlash so'rovi yuborish"""
    try:
        items_text = ""
        if items and len(items) > 0:
            items_text = "\n\n🍽 Mahsulotlar:\n" + "\n".join([f"• {i.get('name')} x{i.get('qty')}" for i in items[:5]])
            if len(items) > 5:
                items_text += f"\n... va yana {len(items) - 5} ta"
        
        keyboard = [
            [
                InlineKeyboardButton("✅ Ha, to'lov qildim", callback_data=f"confirm_payment_{order_id}"),
                InlineKeyboardButton("❌ Bekor qilish", callback_data=f"cancel_payment_{order_id}")
            ]
        ]
        
        message = await context.bot.send_message(
            chat_id=tg_id,
            text=f"""💳 <b>To'lov tasdiqlash</b>

🆔 Buyurtma: #{order_id[-6:]}
💵 Summa: {format_price(total)} so'm{items_text}

<b>To'lovni amalga oshirdingizmi?</b>

Agar to'lov muvaffaqiyatli bo'lgan bo'lsa, "Ha, to'lov qildim" tugmasini bosing va skrinshot yuboring.""",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
        
        with confirmations_lock:
            if order_id not in pending_confirmations:
                pending_confirmations[order_id] = {}
            pending_confirmations[order_id]['tg_id'] = tg_id
            pending_confirmations[order_id]['message_id'] = message.message_id
            pending_confirmations[order_id]['total'] = total
            pending_confirmations[order_id]['items'] = items
            pending_confirmations[order_id]['status'] = 'waiting_confirmation'
            pending_confirmations[order_id]['name'] = None
        
        logger.info(f"✅ Bot tasdiqlash so'rovi yuborildi: {order_id}")
        return True
        
    except Exception as e:
        logger.error(f"❌ Bot tasdiqlash so'rovi xatosi: {e}")
        return False

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback query handler"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    user = update.effective_user
    
    # To'lov tasdiqlash
    if data.startswith("confirm_payment_"):
        order_id = data.replace("confirm_payment_", "")
        
        order = get_order(order_id)
        if order:
            try:
                await query.edit_message_text(
                    f"""✅ <b>Allaqachon tasdiqlangan!</b>

🆔 Buyurtma: #{order_id[-6:]}

Buyurtma qabul qilindi.""",
                    parse_mode='HTML'
                )
            except Exception as e:
                logger.warning(f"Message edit error: {e}")
                await context.bot.send_message(
                    chat_id=user.id,
                    text="✅ Buyurtma allaqachon tasdiqlangan!"
                )
            return
        
        with confirmations_lock:
            if order_id not in pending_confirmations:
                pending_confirmations[order_id] = {}
            pending_confirmations[order_id]['bot_confirmed'] = True
            pending_confirmations[order_id]['tg_id'] = user.id
            pending_confirmations[order_id]['status'] = 'waiting_screenshot'
            pending_confirmations[order_id]['name'] = user.first_name or "Foydalanuvchi"
        
        try:
            await query.edit_message_text(
                f"""📸 <b>Skrinshot yuboring</b>

🆔 Buyurtma: #{order_id[-6:]}

Iltimos, Payme to'lov skrinshotini yuboring.

⏳ Skrinshotni kutib turganda buyurtma saqlanadi...""",
                parse_mode='HTML'
            )
        except Exception as e:
            logger.warning(f"Message edit error: {e}")
            await context.bot.send_message(
                chat_id=user.id,
                text="📸 Iltimos, Payme to'lov skrinshotini yuboring."
            )
        
        return
    
    # Bekor qilish
    if data.startswith("cancel_payment_"):
        order_id = data.replace("cancel_payment_", "")
        
        try:
            await query.edit_message_text(
                f"""❌ <b>Buyurtma bekor qilindi</b>

🆔 Buyurtma: #{order_id[-6:]}

Yangi buyurtma uchun /start ni bosing.""",
                parse_mode='HTML'
            )
        except Exception as e:
            logger.warning(f"Message edit error: {e}")
            await context.bot.send_message(
                chat_id=user.id,
                text="❌ Buyurtma bekor qilindi."
            )
        
        with confirmations_lock:
            if order_id in pending_confirmations:
                del pending_confirmations[order_id]
        
        return
    
    # Accept/Reject (Admin)
    if data.startswith(("accept_", "reject_")):
        action, order_id = data.split("_", 1)
        order = get_order(order_id)
        
        if not order:
            try:
                await query.edit_message_text("❌ Buyurtma topilmadi!")
            except Exception as e:
                logger.warning(f"Message edit error: {e}")
                await context.bot.send_message(chat_id=user.id, text="❌ Buyurtma topilmadi!")
            return
        
        new_status = 'accepted' if action == 'accept' else 'rejected'
        updated_order = update_order_status(order_id, new_status)
        
        if updated_order:
            status_text = "✅ <b>QABUL QILINDI</b>" if action == 'accept' else "❌ <b>BEKOR QILINDI</b>"
            
            items = order.get('items', [])
            if isinstance(items, str):
                items = json.loads(items)
            
            items_text = "\n".join([f"• {i.get('name')} x{i.get('qty')}" for i in items]) if items else "Ma'lumot yo'q"
            
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
            
            try:
                await query.edit_message_text(message, parse_mode='HTML')
            except Exception as e:
                logger.warning(f"Message edit error: {e}")
                await context.bot.send_message(chat_id=user.id, text=message, parse_mode='HTML')
            
            # Mijozga xabar
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
            try:
                await query.edit_message_text("❌ Xatolik yuz berdi!")
            except Exception as e:
                logger.warning(f"Message edit error: {e}")
                await context.bot.send_message(chat_id=user.id, text="❌ Xatolik yuz berdi!")

async def handle_screenshot_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bot dan skrinshot qabul qilish"""
    user_id = update.effective_user.id
    
    order_id = None
    pending_data = None
    
    with confirmations_lock:
        for oid, data in pending_confirmations.items():
            if data.get('tg_id') == user_id and data.get('status') == 'waiting_screenshot':
                order_id = oid
                pending_data = data
                break
    
    if not order_id or not pending_data:
        await update.message.reply_text(
            "⚠️ Avval buyurtma yarating. /start ni bosing.",
            parse_mode='HTML'
        )
        return
    
    if not update.message.photo:
        await update.message.reply_text(
            "⚠️ Iltimos, rasm (skrinshot) yuboring.",
            parse_mode='HTML'
        )
        return
    
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    
    photo_bytes = await file.download_as_bytearray()
    screenshot_base64 = "data:image/jpeg;base64," + base64.b64encode(photo_bytes).decode()
    
    total = pending_data.get('total', 0)
    items = pending_data.get('items', [])
    name = pending_data.get('name') or update.effective_user.first_name or "Foydalanuvchi"
    
    profile = get_user_profile(user_id)
    phone = profile.get('phone', '000000000') if profile else '000000000'
    
    order_data = {
        'orderId': order_id,
        'name': name,
        'phone': phone,
        'items': items,
        'total': total,
        'status': 'pending_verification',
        'paymentStatus': 'pending_verification',
        'paymentMethod': 'payme',
        'location': None,
        'tgId': user_id,
        'notified': False,
        'screenshot': screenshot_base64,
        'screenshotName': f"bot_{photo.file_id}.jpg",
        'initiated_from': 'bot'
    }
    
    order = create_order(order_data)
    
    if order:
        await update.message.reply_text(
            f"""✅ <b>Buyurtma qabul qilindi!</b>

🆔 Buyurtma: #{order_id[-6:]}
💵 Summa: {format_price(total)} so'm

⏳ Admin tekshiruvida...
Buyurtma holatini "Mening buyurtmalarim" bo'limidan kuzatib borishingiz mumkin.""",
            parse_mode='HTML'
        )
        
        # ⭐⭐⭐ ADMIN GA XABAR YUBORISH
        await notify_admin(context, order)
        
        with confirmations_lock:
            if order_id in pending_confirmations:
                del pending_confirmations[order_id]
    else:
        await update.message.reply_text(
            "❌ Xatolik yuz berdi. Iltimos, qayta urinib ko'ring yoki /start ni bosing.",
            parse_mode='HTML'
        )

# ⭐⭐⭐ ASOSIY TO'G'RILASH - Admin ga xabar yuborish
async def notify_admin(context_or_bot, order: Dict):
    """Admin ga xabar yuborish"""
    try:
        # context yoki bot obyektini aniqlash
        if hasattr(context_or_bot, 'bot'):
            bot = context_or_bot.bot
        else:
            bot = context_or_bot
        
        logger.info(f"🔔 notify_admin chaqirildi, order_id: {order.get('order_id')}, ADMIN_CHAT_ID: {ADMIN_CHAT_ID_INT}")
        
        if not ADMIN_CHAT_ID_INT:
            logger.error("❌ ADMIN_CHAT_ID o'rnatilmagan!")
            return False
        
        items = order.get('items', [])
        if isinstance(items, str):
            items = json.loads(items)
        
        items_text = "\n".join([f"• {i.get('name')} x{i.get('qty')}" for i in items]) if items else "Ma'lumot yo'q"
        
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

        keyboard = [
            [
                InlineKeyboardButton("✅ Qabul qilish", callback_data=f"accept_{order.get('order_id')}"),
                InlineKeyboardButton("❌ Bekor qilish", callback_data=f"reject_{order.get('order_id')}")
            ],
            [InlineKeyboardButton("🌐 Admin Panel", web_app=WebAppInfo(url=f"{WEBAPP_URL}/admin.html"))]
        ]
        
        # Skrinshot bilan yuborish
        if has_screenshot and order.get('screenshot'):
            try:
                screenshot_data = order.get('screenshot')
                if screenshot_data.startswith('data:image'):
                    image_data = base64.b64decode(screenshot_data.split(',')[1])
                    
                    await bot.send_photo(
                        chat_id=ADMIN_CHAT_ID_INT,
                        photo=image_data,
                        caption=message,
                        parse_mode='HTML',
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                    logger.info(f"✅ Admin ga skrinshot bilan xabar yuborildi: {order.get('order_id')}")
                else:
                    await bot.send_message(
                        chat_id=ADMIN_CHAT_ID_INT,
                        text=message,
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        parse_mode='HTML'
                    )
            except Exception as e:
                logger.error(f"❌ Skrinshot yuborish xatosi: {e}")
                await bot.send_message(
                    chat_id=ADMIN_CHAT_ID_INT,
                    text=message,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='HTML'
                )
        else:
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID_INT,
                text=message,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='HTML'
            )
            logger.info(f"✅ Admin ga matnli xabar yuborildi: {order.get('order_id')}")
        
        # notified = true
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("UPDATE orders SET notified = true WHERE order_id = %s", (order.get('order_id'),))
            conn.commit()
            cur.close()
            logger.info(f"✅ notified=true qilindi: {order.get('order_id')}")
        except Exception as e:
            logger.error(f"❌ Mark notified error: {e}")
        finally:
            if conn:
                conn.close()
        
        return True
        
    except Exception as e:
        logger.error(f"❌ notify_admin xatosi: {e}")
        import traceback
        traceback.print_exc()
        return False

# ==========================================
# HTTP API HANDLERS
# ==========================================

async def health_handler(request):
    return web.json_response({
        "status": "ok", 
        "service": "bodrum-bot",
        "timestamp": datetime.utcnow().isoformat()
    }, headers=get_cors_headers())

# ⭐⭐⭐ ASOSIY TO'G'RILASH - Buyurtma yaratishda admin ga xabar
async def create_order_handler(request):
    try:
        data = await request.json()
        logger.info(f"📝 Yangi buyurtma: {data.get('orderId')}")
        
        order = create_order(data)
        
        if order:
            logger.info(f"✅ Buyurtma yaratildi: {order['order_id']}")
            
            # ⭐⭐⭐ ADMIN GA XABAR YUBORISH
            global application
            if application:
                try:
                    logger.info(f"🔔 Admin ga xabar yuborilmoqda...")
                    success = await notify_admin(application, order)
                    if success:
                        logger.info(f"✅ Admin ga xabar yuborildi")
                    else:
                        logger.warning(f"⚠️ Admin ga xabar yuborilmadi")
                except Exception as e:
                    logger.error(f"❌ Admin ga xabar yuborish xatosi: {e}")
            else:
                logger.warning(f"⚠️ Application mavjud emas, admin ga xabar yuborilmaydi")
            
            return web.json_response(order, status=201, headers=get_cors_headers())
        else:
            return web.json_response({"error": "Failed to create order"}, status=500, headers=get_cors_headers())
        
    except Exception as e:
        logger.error(f"API create order error: {e}")
        import traceback
        traceback.print_exc()
        return web.json_response({"error": str(e)}, status=500, headers=get_cors_headers())

async def get_order_handler(request):
    try:
        order_id = request.match_info['order_id']
        order = get_order(order_id)
        
        if not order:
            return web.json_response({"error": "Not found"}, status=404, headers=get_cors_headers())
        
        return web.json_response(order, headers=get_cors_headers())
    except Exception as e:
        logger.error(f"API get order error: {e}")
        return web.json_response({"error": str(e)}, status=500, headers=get_cors_headers())

async def confirm_payment_handler(request):
    """Web App dan to'lov tasdiqlash"""
    try:
        data = await request.json()
        order_id = data.get('orderId')
        tg_id = data.get('tgId')
        
        logger.info(f"✅ Web App dan tasdiqlash: {order_id}")
        
        # Bot dagi xabarni o'chirish
        global application
        if application and tg_id:
            try:
                with confirmations_lock:
                    pending_data = pending_confirmations.get(order_id, {})
                    message_id = pending_data.get('message_id')
                    if message_id:
                        try:
                            await application.bot.delete_message(chat_id=tg_id, message_id=message_id)
                        except:
                            pass
                    if order_id in pending_confirmations:
                        del pending_confirmations[order_id]
            except Exception as e:
                logger.error(f"Xabar o'chirish xatosi: {e}")
        
        return web.json_response({
            "success": True,
            "message": "Payment confirmed"
        }, headers=get_cors_headers())
        
    except Exception as e:
        logger.error(f"Confirm payment error: {e}")
        return web.json_response({"error": str(e)}, status=500, headers=get_cors_headers())

async def send_bot_confirmation_handler(request):
    """Bot dan to'lov tasdiqlash xabarini yuborish"""
    try:
        data = await request.json()
        order_id = data.get('orderId')
        tg_id = data.get('tgId')
        total = data.get('total', 0)
        items = data.get('items', [])
        
        logger.info(f"📤 Bot tasdiqlash so'rovi: {order_id}, tg_id: {tg_id}")
        
        global application
        if application and tg_id:
            success = await send_payment_confirmation_request(application, order_id, tg_id, total, items)
            
            if success:
                return web.json_response({
                    "success": True,
                    "message": "Confirmation request sent"
                }, headers=get_cors_headers())
            else:
                return web.json_response({
                    "success": False,
                    "error": "Failed to send message"
                }, status=500, headers=get_cors_headers())
        else:
            return web.json_response({
                "success": False,
                "error": "Bot not available"
            }, status=500, headers=get_cors_headers())
        
    except Exception as e:
        logger.error(f"Send bot confirmation error: {e}")
        return web.json_response({"error": str(e)}, status=500, headers=get_cors_headers())

async def check_bot_confirmation_handler(request):
    """Tekshirish - bot dan tasdiqlanganmi"""
    try:
        data = await request.json()
        order_id = data.get('orderId')
        
        with confirmations_lock:
            data = pending_confirmations.get(order_id, {})
            bot_confirmed = data.get('bot_confirmed', False)
            status = data.get('status', '')
        
        return web.json_response({
            "bot_confirmed": bot_confirmed,
            "status": status,
            "order_id": order_id
        }, headers=get_cors_headers())
        
    except Exception as e:
        logger.error(f"Check bot confirmation error: {e}")
        return web.json_response({"error": str(e)}, status=500, headers=get_cors_headers())

async def save_user_profile_api(request):
    """Foydalanuvchi profilini saqlash (Web App dan)"""
    try:
        data = await request.json()
        tg_id_raw = data.get('tgId')
        name = data.get('name', 'Foydalanuvchi')
        phone = data.get('phone', '')
        username = data.get('username', '')
        
        print(f"💾 Profil saqlanmoqda: tg_id={tg_id_raw}, name={name}, phone={phone}")
        
        if not tg_id_raw:
            return web.json_response({
                "success": False,
                "error": "tgId required"
            }, status=400, headers=get_cors_headers())
        
        try:
            tg_id = int(tg_id_raw)
        except (ValueError, TypeError):
            return web.json_response({
                "success": False,
                "error": "Invalid tgId format"
            }, status=400, headers=get_cors_headers())
        
        if not phone or len(phone) != 9:
            return web.json_response({
                "success": False,
                "error": "Valid phone required (9 digits)"
            }, status=400, headers=get_cors_headers())
        
        success = save_user_profile(tg_id, name, phone, username)
        
        if success:
            profile = get_user_profile(tg_id)
            orders = get_user_orders(tg_id)
            
            return web.json_response({
                "success": True,
                "profile": profile,
                "orders": orders
            }, headers=get_cors_headers())
        else:
            return web.json_response({
                "success": False,
                "error": "Failed to save profile"
            }, status=500, headers=get_cors_headers())
            
    except Exception as e:
        logger.error(f"Save user profile API error: {e}")
        import traceback
        traceback.print_exc()
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500, headers=get_cors_headers())

async def get_user_profile_api(request):
    """Foydalanuvchi profilini olish (Telegram ID orqali)"""
    try:
        data = await request.json()
        tg_id_raw = data.get('tgId')
        
        print(f"🔍 API: Profil so'raldi, raw tg_id: {tg_id_raw}, type: {type(tg_id_raw)}")
        
        if not tg_id_raw:
            return web.json_response({
                "success": False, 
                "error": "tgId required"
            }, status=400, headers=get_cors_headers())
        
        try:
            tg_id = int(tg_id_raw)
        except (ValueError, TypeError) as e:
            print(f"❌ tgId conversion error: {e}")
            return web.json_response({
                "success": False,
                "error": "Invalid tgId format"
            }, status=400, headers=get_cors_headers())
        
        profile = get_user_profile(tg_id)
        orders = get_user_orders(tg_id)
        
        print(f"✅ API: Profil: {profile is not None}, Buyurtmalar: {len(orders)}")
        
        return web.json_response({
            "success": True,
            "profile": profile,
            "orders": orders
        }, headers=get_cors_headers())
        
    except Exception as e:
        logger.error(f"Get user profile API error: {e}")
        import traceback
        traceback.print_exc()
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500, headers=get_cors_headers())

async def orders_list_handler(request):
    """Barcha buyurtmalarni olish - FAQAT QABUL QILINGANLAR"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM orders 
            WHERE status = 'accepted' 
            ORDER BY accepted_at DESC NULLS LAST, created_at DESC 
            LIMIT 100
        """)
        results = cur.fetchall()
        cur.close()
        conn.close()
        
        orders = []
        for row in results:
            order_dict = dict(row)
            for key in ['created_at', 'accepted_at', 'rejected_at', 'paid_at', 'confirmed_at']:
                if order_dict.get(key) and hasattr(order_dict[key], 'isoformat'):
                    order_dict[key] = order_dict[key].isoformat()
            orders.append(order_dict)
        
        return web.json_response(orders, headers=get_cors_headers())
        
    except Exception as e:
        logger.error(f"Orders list error: {e}")
        return web.json_response({"error": str(e)}, status=500, headers=get_cors_headers())

async def new_orders_handler(request):
    """Yangi buyurtmalarni olish"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM orders 
            WHERE status IN ('pending', 'pending_verification', 'payment_pending') 
            ORDER BY created_at DESC
        """)
        results = cur.fetchall()
        cur.close()
        conn.close()
        
        orders = []
        for row in results:
            order_dict = dict(row)
            for key in ['created_at', 'accepted_at', 'rejected_at', 'paid_at', 'confirmed_at']:
                if order_dict.get(key) and hasattr(order_dict[key], 'isoformat'):
                    order_dict[key] = order_dict[key].isoformat()
            orders.append(order_dict)
        
        return web.json_response(orders, headers=get_cors_headers())
        
    except Exception as e:
        logger.error(f"New orders error: {e}")
        return web.json_response({"error": str(e)}, status=500, headers=get_cors_headers())

async def update_order_handler(request):
    """Buyurtma yangilash"""
    try:
        order_id = request.match_info['order_id']
        data = await request.json()
        
        status = data.get('status')
        payment_status = data.get('paymentStatus')
        admin_note = data.get('adminNote')
        
        updated = update_order_status(
            order_id, 
            status, 
            payment_status=payment_status,
            admin_note=admin_note
        )
        
        if updated:
            return web.json_response(updated, headers=get_cors_headers())
        else:
            return web.json_response({"error": "Order not found"}, status=404, headers=get_cors_headers())
            
    except Exception as e:
        logger.error(f"Update order error: {e}")
        return web.json_response({"error": str(e)}, status=500, headers=get_cors_headers())

async def options_handler(request):
    return web.Response(headers=get_cors_headers())

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
# MAIN
# ==========================================

async def init_webhook(app):
    global application
    
    if not TOKEN:
        logger.error("❌ TOKEN o'rnatilmagan!")
        return
    
    # Database ni initsializatsiya qilish
    if not init_database():
        logger.error("❌ Database initialization failed!")
    
    webhook_url = os.getenv("WEBHOOK_URL", "")
    if not webhook_url:
        railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
        if railway_domain:
            webhook_url = f"https://{railway_domain}"
    
    application = Application.builder().token(TOKEN).build()
    
    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.CONTACT, contact_handler))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.PHOTO, handle_screenshot_upload))
    
    await application.initialize()
    await application.start()
    
    if webhook_url:
        full_webhook_url = f"{webhook_url}/webhook"
        try:
            await application.bot.set_webhook(full_webhook_url)
            logger.info(f"✅ Webhook o'rnatildi: {full_webhook_url}")
        except Exception as e:
            logger.error(f"❌ Webhook xato: {e}")
    
    logger.info(f"🤖 Bot ishga tushdi! ADMIN_CHAT_ID: {ADMIN_CHAT_ID_INT}")

async def shutdown(app):
    global application
    if application:
        try:
            await application.stop()
            await application.shutdown()
            logger.info("🛑 Bot to'xtatildi")
        except Exception as e:
            logger.error(f"Shutdown xato: {e}")

def main():
    logger.info("🔧 Bodrum Bot starting...")
    
    if not PORT:
        logger.error("❌ PORT o'rnatilmagan!")
        return
    
    app = web.Application()
    
    # Routes
    app.router.add_get('/', health_handler)
    app.router.add_get('/health', health_handler)
    
    # API routes
    app.router.add_get('/api/orders', orders_list_handler)
    app.router.add_get('/api/orders/new', new_orders_handler)
    app.router.add_post('/api/orders', create_order_handler)
    app.router.add_get('/api/orders/{order_id}', get_order_handler)
    app.router.add_put('/api/orders/{order_id}', update_order_handler)
    
    # CORS preflight
    app.router.add_options('/api/orders', options_handler)
    app.router.add_options('/api/orders/{order_id}', options_handler)
    
    # User profile API
    app.router.add_post('/api/user/profile', get_user_profile_api)
    app.router.add_options('/api/user/profile', options_handler)
    
    # Confirmation routes
    app.router.add_post('/api/confirm-payment', confirm_payment_handler)
    app.router.add_options('/api/confirm-payment', options_handler)
    
    app.router.add_post('/api/send-bot-confirmation', send_bot_confirmation_handler)
    app.router.add_options('/api/send-bot-confirmation', options_handler)
    
    app.router.add_post('/api/check-bot-confirmation', check_bot_confirmation_handler)
    app.router.add_options('/api/check-bot-confirmation', options_handler)
    
    # Webhook
    app.router.add_post('/webhook', webhook_handler)
    
    app.on_startup.append(init_webhook)
    app.on_cleanup.append(shutdown)

    # Route qo'shish
    app.router.add_post('/api/user/save-profile', save_user_profile_api)
    app.router.add_options('/api/user/save-profile', options_handler)
    
    logger.info(f"🚀 Server ishga tushmoqda: 0.0.0.0:{PORT}")
    
    web.run_app(app, host='0.0.0.0', port=PORT)

if __name__ == "__main__":
    main()
