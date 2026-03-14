# ==========================================
# BODRUM BOT - AVTOMATIK TO'LOV TIZIMI
# Payme callback bilan auto-accept
# Sayt va WebApp uchun universal
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
                status VARCHAR(50) DEFAULT 'pending_payment',
                payment_status VARCHAR(50) DEFAULT 'pending',
                payment_method VARCHAR(50) DEFAULT 'payme',
                location VARCHAR(255),
                tg_id BIGINT,
                notified BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                accepted_at TIMESTAMP,
                rejected_at TIMESTAMP,
                paid_at TIMESTAMP,
                confirmed_at TIMESTAMP,
                admin_note TEXT,
                transaction_id VARCHAR(100),
                auto_accepted BOOLEAN DEFAULT FALSE,
                initiated_from VARCHAR(50) DEFAULT 'webapp'
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
        
        # 3. Index'lar
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

def format_phone_display(phone: str) -> str:
    """Telefon raqamini ko'rsatish uchun formatlash"""
    if not phone:
        return "Noma'lum"
    phone = ''.join(filter(str.isdigit, str(phone)))
    if phone.startswith('998'):
        phone = phone[3:]
    phone = phone[-9:] if len(phone) > 9 else phone
    return f"+998{phone}"

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
        
        # ⭐ tg_id ni to'g'ri formatlash
        tg_id = data.get('tgId') or data.get('tg_id')
        if tg_id:
            try:
                tg_id = int(tg_id)
            except:
                tg_id = None
        
        cur.execute("""
            INSERT INTO orders (
                order_id, name, phone, items, total, 
                status, payment_status, payment_method, 
                location, tg_id, notified, created_at,
                initiated_from
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
        """, (
            data.get('orderId'), data.get('name'), data.get('phone'),
            items_json, data.get('total'), data.get('status', 'pending_payment'),
            data.get('paymentStatus', 'pending'), data.get('paymentMethod', 'payme'),
            data.get('location'), tg_id, False, datetime.utcnow(),
            data.get('initiated_from', 'webapp')
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
        import traceback
        traceback.print_exc()
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
    """Start handler - Admin va foydalanuvchilar uchun"""
    user = update.effective_user
    is_admin = user.id == ADMIN_CHAT_ID_INT
    
    logger.info(f"🚀 /start bosildi - User: {user.id}, Username: @{user.username}, Name: {user.first_name}, Admin: {is_admin}")
    
    # Admin uchun maxsus menyu
    if is_admin:
        keyboard = [
            [InlineKeyboardButton("🛎️ Yangi buyurtmalar", callback_data="show_new_orders")],
            [InlineKeyboardButton("📊 Statistika", callback_data="admin_stats")],
            [InlineKeyboardButton("🍽️ Menyu ko'rish", web_app=WebAppInfo(url=WEBAPP_URL))],
            [InlineKeyboardButton("⚙️ Admin Panel", web_app=WebAppInfo(url=f"{WEBAPP_URL}/admin.html"))]
        ]
        
        welcome_text = f"""👋 <b>Salom, Admin {user.first_name}!</b>

🤖 <b>BODRUM</b> admin paneliga xush kelibsiz!

📋 <b>Mavjud buyruqlar:</b>
• 🛎️ Yangi buyurtmalar - Kutilayotgan buyurtmalarni ko'rish
• 📊 Statistika - Kunlik statistika
• 🍽️ Menyu - Web App orqali menyu
• ⚙️ Admin Panel - To'liq boshqaruv paneli

⏰ {datetime.now().strftime('%H:%M:%S')}"""
        
        await update.message.reply_text(
            welcome_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
        return
    
    # Oddiy foydalanuvchi uchun
    profile = get_user_profile(user.id)
    
    if profile and profile.get('phone'):
        logger.info(f"✅ Mavjud foydalanuvchi: {user.id}")
        
        name = profile.get('name', 'Foydalanuvchi')
        phone = profile.get('phone', '')
        
        formatted_phone = phone
        if len(phone) == 9:
            formatted_phone = f"{phone[:2]} {phone[2:5]} {phone[5:7]} {phone[7:]}"
        
        keyboard = [
            [InlineKeyboardButton("🍽️ Menyuni ko'rish", web_app=WebAppInfo(url=WEBAPP_URL))],
            [InlineKeyboardButton("📋 Mening buyurtmalarim", callback_data="my_orders")]
        ]
        
        await update.message.reply_text(
            f"👋 Salom, <b>{name}</b>!\n\n"
            f"🍽️ <b>BODRUM</b> restoraniga xush kelibsiz!\n\n"
            f"📞 Telefon: +998 {formatted_phone}\n\n"
            f"🛒 Menyudan buyurtma berishingiz mumkin:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
    else:
        logger.info(f"🆕 Yangi foydalanuvchi: {user.id}")
        
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

async def show_new_orders_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Yangi buyurtmalar ro'yxatini ko'rsatish - Admin uchun"""
    query = update.callback_query
    await query.answer()
    
    # Faqat to'lov kutilayotgan va yangi buyurtmalarni olish
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM orders 
            WHERE status IN ('pending_payment', 'pending', 'payment_pending')
            ORDER BY created_at DESC
        """)
        results = cur.fetchall()
        cur.close()
        
        new_orders = []
        for row in results:
            order_dict = dict(row)
            for key in ['created_at', 'accepted_at', 'rejected_at', 'paid_at', 'confirmed_at']:
                if order_dict.get(key) and hasattr(order_dict[key], 'isoformat'):
                    order_dict[key] = order_dict[key].isoformat()
            new_orders.append(order_dict)
        
    except Exception as e:
        logger.error(f"❌ Yangi buyurtmalarni olish xatosi: {e}")
        new_orders = []
    finally:
        if conn:
            conn.close()
    
    if not new_orders:
        await query.edit_message_text(
            "📭 <b>Hozircha yangi buyurtmalar yo'q</b>\n\n"
            "Barcha buyurtmalar ko'rib chiqilgan.",
            parse_mode='HTML'
        )
        return
    
    # Har bir buyurtma uchun alohida xabar
    for order in new_orders:
        items = order.get('items', [])
        if isinstance(items, str):
            items = json.loads(items)
        
        items_text = "\\n".join([f"• {i.get('name')} x{i.get('qty')}" for i in items]) if items else "Ma'lumot yo'q"
        
        # TELEFON RAQAMINI FORMATLASH
        raw_phone = order.get('phone', '')
        phone_display = format_phone_display(raw_phone)
        
        # JOYLASHUVNI TEKSHIRISH
        location = order.get('location')
        location_text = ""
        location_coords = None
        if location and ',' in str(location):
            try:
                lat, lng = str(location).split(',')
                lat = float(lat.strip())
                lng = float(lng.strip())
                location_text = f"\\n📍 <b>Joylashuv:</b> <a href='https://maps.google.com/?q={lat},{lng}'>Xaritada ko'rish</a>"
                location_coords = (lat, lng)
            except:
                pass
        
        message = f"""🛎️ <b>YANGI BUYURTMA!</b>

🆔 Buyurtma: #{order.get('order_id', 'N/A')[-6:]}
👤 Mijoz: {order.get('name')}
📞 Telefon: {phone_display}
💵 Summa: {format_price(order.get('total', 0))} so'm
💳 To'lov: {order.get('payment_method', 'N/A').upper()} ⏳{location_text}

🍽 Mahsulotlar:
{items_text}

⏰ {order.get('created_at', datetime.now().isoformat())[:19]}"""

        keyboard = [
            [
                InlineKeyboardButton("✅ Qabul qilish", callback_data=f"accept_{order.get('order_id')}"),
                InlineKeyboardButton("❌ Bekor qilish", callback_data=f"reject_{order.get('order_id')}")
            ]
        ]
        
        sent_message = await context.bot.send_message(
            chat_id=update.effective_user.id,
            text=message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
        
        # Joylashuvni alohida xabar sifatida yuborish
        if location_coords:
            try:
                await context.bot.send_location(
                    chat_id=update.effective_user.id,
                    latitude=location_coords[0],
                    longitude=location_coords[1],
                    reply_to_message_id=sent_message.message_id
                )
                logger.info(f"✅ Joylashuv yuborildi: {location_coords}")
            except Exception as e:
                logger.error(f"❌ Joylashuv yuborish xatosi: {e}")
    
    # Asl xabarni yangilash
    await query.edit_message_text(
        f"📋 <b>{len(new_orders)} ta yangi buyurtma</b> yuborildi.\\n\\n"
        f"Har bir buyurtma uchun alohida xabar yuborildi.",
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
        
        # Web App tugmasini yuborish
        keyboard = [
            [InlineKeyboardButton("🍽️ Menyuni ko'rish", web_app=WebAppInfo(url=WEBAPP_URL))]
        ]
        
        formatted_phone = f"{phone[:2]} {phone[2:5]} {phone[5:7]} {phone[7:]}"
        
        await update.message.reply_text(
            f"👋 Salom, <b>{user.first_name}</b>!\\n\\n"
            f"🍽️ <b>BODRUM</b> restoraniga xush kelibsiz!\\n\\n"
            f"📞 Telefon: +998 {formatted_phone}\\n\\n"
            f"🛒 Menyudan buyurtma berishingiz mumkin:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
    else:
        await update.message.reply_text(
            "❌ Xatolik yuz berdi. Iltimos, qayta urinib ko'ring.",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode='HTML'
        )

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback query handler"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    user = update.effective_user
    
    # Statistika
    if data == "admin_stats":
        await show_stats(update, context)
        return
    
    # Yangi buyurtmalar
    if data == "show_new_orders":
        await show_new_orders_list(update, context)
        return
    
    # Mening buyurtmalarim
    if data == "my_orders":
        await show_user_orders(update, context)
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
            
            items_text = "\\n".join([f"• {i.get('name')} x{i.get('qty')}" for i in items]) if items else "Ma'lumot yo'q"
            
            raw_phone = order.get('phone', '')
            phone_display = format_phone_display(raw_phone)
            
            message = f"""{status_text}

🆔 Buyurtma: #{order_id[-6:]}
👤 Mijoz: {order.get('name')}
📞 Telefon: {phone_display}
💵 Summa: {format_price(order.get('total', 0))} so'm

🍽 Mahsulotlar:
{items_text}

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
                        msg = f"✅ Buyurtmangiz #{order_id[-6:]} qabul qilindi!\\n\\nTez orada yetkazib beramiz! 🚀"
                    else:
                        msg = f"❌ Buyurtmangiz #{order_id[-6:]} bekor qilindi.\\n\\nQo'llab-quvvatlash: +998901234567"
                    
                    await context.bot.send_message(chat_id=tg_id, text=msg)
                except Exception as e:
                    logger.error(f"Mijozga xabar yuborishda xato: {e}")
        else:
            try:
                await query.edit_message_text("❌ Xatolik yuz berdi!")
            except Exception as e:
                logger.warning(f"Message edit error: {e}")
                await context.bot.send_message(chat_id=user.id, text="❌ Xatolik yuz berdi!")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Statistika ko'rsatish - Admin uchun"""
    user = update.effective_user
    
    if user.id != ADMIN_CHAT_ID_INT:
        await update.message.reply_text("❌ Siz admin emassiz!")
        return
    
    await show_stats(update, context)

async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Statistikani ko'rsatish"""
    user = update.effective_user
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        today = datetime.now().strftime('%Y-%m-%d')
        
        # Yangi buyurtmalar soni
        cur.execute("SELECT COUNT(*) FROM orders WHERE status IN ('pending', 'pending_payment')")
        new_count = cur.fetchone()['count']
        
        # Qabul qilinganlar (bugun)
        cur.execute("SELECT COUNT(*), COALESCE(SUM(total), 0) FROM orders WHERE status = 'accepted' AND DATE(accepted_at) = %s", (today,))
        today_result = cur.fetchone()
        today_count = today_result['count']
        today_sum = today_result['coalesce'] or 0
        
        # Jami buyurtmalar
        cur.execute("SELECT COUNT(*), COALESCE(SUM(total), 0) FROM orders WHERE status = 'accepted'")
        total_result = cur.fetchone()
        total_count = total_result['count']
        total_sum = total_result['coalesce'] or 0
        
        cur.close()
        conn.close()
        
        stats_text = f"""📊 <b>STATISTIKA</b>

🕐 <b>Bugun ({today}):</b>
• Buyurtmalar: {today_count} ta
• Summa: {format_price(today_sum)} so'm

⏳ <b>Kutilayotgan:</b>
• Yangi buyurtmalar: {new_count} ta

📈 <b>Jami:</b>
• Qabul qilingan: {total_count} ta
• Umumiy summa: {format_price(total_sum)} so'm

⏰ {datetime.now().strftime('%H:%M:%S')}"""
        
        keyboard = [
            [InlineKeyboardButton("🔄 Yangilash", callback_data="admin_stats")],
            [InlineKeyboardButton("🛎️ Yangi buyurtmalar", callback_data="show_new_orders")]
        ]
        
        if update.callback_query:
            await update.callback_query.edit_message_text(
                stats_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='HTML'
            )
        else:
            await update.message.reply_text(
                stats_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='HTML'
            )
        
    except Exception as e:
        logger.error(f"Stats error: {e}")
        text = "❌ Statistikani olishda xatolik"
        if update.callback_query:
            await update.callback_query.edit_message_text(text)
        else:
            await update.message.reply_text(text)

async def myorders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Foydalanuvchining buyurtmalari"""
    user = update.effective_user
    
    orders = get_user_orders(user.id)
    
    if not orders:
        await update.message.reply_text(
            "📭 <b>Sizda hali buyurtmalar yo'q</b>\\n\\n"
            "🍽️ Menyudan buyurtma berish uchun /start ni bosing",
            parse_mode='HTML'
        )
        return
    
    await show_user_orders(update, context, orders)

async def show_user_orders(update: Update, context: ContextTypes.DEFAULT_TYPE, orders=None):
    """Foydalanuvchi buyurtmalarini ko'rsatish"""
    user = update.effective_user
    
    if orders is None:
        orders = get_user_orders(user.id)
    
    if not orders:
        text = "📭 <b>Sizda hali buyurtmalar yo'q</b>"
        if update.callback_query:
            await update.callback_query.edit_message_text(text, parse_mode='HTML')
        else:
            await update.message.reply_text(text, parse_mode='HTML')
        return
    
    recent_orders = orders[:5]
    
    orders_text = "📋 <b>SIZNING BUYURTMALARINGIZ</b>\\n\\n"
    
    for i, order in enumerate(recent_orders, 1):
        status_emoji = {
            'accepted': '✅',
            'rejected': '❌',
            'pending': '⏳',
            'pending_payment': '💳',
            'confirmed': '✅'
        }.get(order.get('status'), '❓')
        
        status_text = {
            'accepted': 'Qabul qilingan',
            'rejected': 'Bekor qilingan',
            'pending': 'Kutilmoqda',
            'pending_payment': 'To\'lov kutilmoqda',
            'confirmed': 'Tasdiqlangan'
        }.get(order.get('status'), 'Noma\'lum')
        
        order_id_short = order.get('order_id', 'N/A')[-6:]
        total = format_price(order.get('total', 0))
        created = order.get('created_at', '')[:10]
        
        orders_text += f"{i}. {status_emoji} <b>#{order_id_short}</b> - {total} so'm\\n"
        orders_text += f"   📅 {created} | {status_text}\\n\\n"
    
    if len(orders) > 5:
        orders_text += f"... va yana {len(orders) - 5} ta buyurtma\\n"
    
    keyboard = [
        [InlineKeyboardButton("🍽️ Yangi buyurtma", web_app=WebAppInfo(url=WEBAPP_URL))]
    ]
    
    if update.callback_query:
        await update.callback_query.edit_message_text(
            orders_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
    else:
        await update.message.reply_text(
            orders_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )

# ⭐ AVTOMATIK QABUL QILISH - Admin ga xabar
async def notify_admin_auto_accepted(context_or_bot, order: Dict):
    """Admin ga avtomatik qabul qilingan buyurtma haqida xabar"""
    try:
        if hasattr(context_or_bot, 'bot'):
            bot = context_or_bot.bot
        else:
            bot = context_or_bot
        
        logger.info(f"🔔 notify_admin_auto_accepted chaqirildi, order_id: {order.get('order_id')}")
        
        if not ADMIN_CHAT_ID_INT:
            logger.error("❌ ADMIN_CHAT_ID o'rnatilmagan!")
            return False
        
        items = order.get('items', [])
        if isinstance(items, str):
            items = json.loads(items)
        
        items_text = "\\n".join([f"• {i.get('name')} x{i.get('qty')}" for i in items]) if items else "Ma'lumot yo'q"
        
        # Telefon raqamini formatlash
        raw_phone = order.get('phone', '')
        phone_display = format_phone_display(raw_phone)
        
        # Joylashuv
        location = order.get('location')
        location_text = ""
        location_coords = None
        
        if location and ',' in str(location):
            try:
                lat, lng = str(location).split(',')
                lat = float(lat.strip())
                lng = float(lng.strip())
                location_text = f"\\n📍 <b>Joylashuv:</b> <a href='https://maps.google.com/?q={lat},{lng}'>Xaritada ko\\'rish</a>"
                location_coords = (lat, lng)
            except Exception as e:
                logger.warning(f"Joylashuv parse xatosi: {e}")
                location_text = f"\\n📍 <b>Manzil:</b> {location}"
        elif location:
            location_text = f"\\n📍 <b>Manzil:</b> {location}"
        
        message = f"""⚡ <b>AVTOMATIK QABUL QILINDI!</b>

🆔 Buyurtma: #{order.get('order_id', 'N/A')[-6:]}
👤 Mijoz: {order.get('name')}
📞 Telefon: {phone_display}
💵 Summa: {format_price(order.get('total', 0))} so'm
💳 To'lov: PAYME ✅ (Avtomatik){location_text}

🍽 Mahsulotlar:
{items_text}

⏰ {datetime.now().strftime('%H:%M:%S')}

<i>✅ To'lov muvaffaqiyatli - buyurtma avtomatik qabul qilindi</i>"""

        # Tugmasiz xabar (chunki allaqachon qabul qilingan)
        sent_message = await bot.send_message(
            chat_id=ADMIN_CHAT_ID_INT,
            text=message,
            parse_mode='HTML'
        )
        
        # Joylashuvni alohida yuborish
        if location_coords:
            try:
                await bot.send_location(
                    chat_id=ADMIN_CHAT_ID_INT,
                    latitude=location_coords[0],
                    longitude=location_coords[1],
                    reply_to_message_id=sent_message.message_id
                )
                logger.info(f"✅ Joylashuv yuborildi: {location_coords}")
            except Exception as e:
                logger.error(f"❌ Joylashuv yuborish xatosi: {e}")
        
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
        logger.error(f"❌ notify_admin_auto_accepted xatosi: {e}")
        import traceback
        traceback.print_exc()
        return False

# ⭐ MIJOZGA AVTOMATIK XABAR YUBORISH
async def notify_customer_auto_accepted(bot, order: Dict):
    """Mijozga avtomatik qabul qilingan xabar yuborish"""
    try:
        tg_id = order.get('tg_id')
        if not tg_id:
            logger.warning("⚠️ Mijoz tg_id topilmadi")
            return False
        
        order_id = order.get('order_id', 'N/A')
        total = order.get('total', 0)
        
        message = f"""✅ <b>Buyurtmangiz qabul qilindi!</b>

🆔 Buyurtma: #{order_id[-6:]}
💵 Summa: {format_price(total)} so'm
💳 To'lov: Payme ✅

Buyurtmangiz muvaffaqiyatli to'landi va avtomatik qabul qilindi!

Tez orada yetkazib beramiz! 🚀

⏰ {datetime.now().strftime('%H:%M:%S')}"""
        
        await bot.send_message(
            chat_id=tg_id,
            text=message,
            parse_mode='HTML'
        )
        
        logger.info(f"✅ Mijozga xabar yuborildi: {tg_id}")
        return True
        
    except Exception as e:
        logger.error(f"❌ Mijozga xabar yuborish xatosi: {e}")
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

async def create_order_handler(request):
    try:
        data = await request.json()
        logger.info(f"📝 Yangi buyurtma: {data.get('orderId')}")
        
        order = create_order(data)
        
        if order:
            logger.info(f"✅ Buyurtma yaratildi: {order['order_id']}")
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

# ⭐ PAYME CALLBACK - AVTOMATIK QABUL QILISH
async def payme_callback_handler(request):
    """Payme dan to'lov xabari kelganda avtomatik qabul qilish"""
    try:
        data = await request.json()
        logger.info(f"💰 Payme callback: {data}")
        
        order_id = data.get('order_id')
        status = data.get('status')
        transaction_id = data.get('transaction_id')
        
        if not order_id:
            return web.json_response({"error": "order_id required"}, status=400, headers=get_cors_headers())
        
        if status == 'success' or status == 'completed':
            # Buyurtmani avtomatik qabul qilish
            conn = None
            try:
                conn = get_db_connection()
                cur = conn.cursor()
                
                # Avval buyurtma ma'lumotlarini olish
                cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
                order = cur.fetchone()
                
                if not order:
                    return web.json_response({"error": "Order not found"}, status=404, headers=get_cors_headers())
                
                order_dict = dict(order)
                
                # ⭐ AVTOMATIK QABUL QILISH
                cur.execute("""
                    UPDATE orders 
                    SET status = 'accepted',
                        payment_status = 'paid',
                        accepted_at = CURRENT_TIMESTAMP,
                        paid_at = CURRENT_TIMESTAMP,
                        transaction_id = %s,
                        auto_accepted = TRUE,
                        notified = FALSE
                    WHERE order_id = %s
                    RETURNING *
                """, (transaction_id, order_id))
                
                updated = cur.fetchone()
                conn.commit()
                
                if updated:
                    updated_dict = dict(updated)
                    for key in ['created_at', 'accepted_at', 'rejected_at', 'paid_at', 'confirmed_at']:
                        if updated_dict.get(key) and hasattr(updated_dict[key], 'isoformat'):
                            updated_dict[key] = updated_dict[key].isoformat()
                    
                    logger.info(f"✅ Buyurtma avtomatik qabul qilindi: {order_id}")
                    
                    # Admin ga xabar yuborish
                    global application
                    if application:
                        await notify_admin_auto_accepted(application, updated_dict)
                        # Mijozga ham xabar yuborish
                        await notify_customer_auto_accepted(application.bot, updated_dict)
                    
                    return web.json_response({
                        "success": True,
                        "message": "Payment successful, order auto-accepted",
                        "order": updated_dict
                    }, headers=get_cors_headers())
                
            except Exception as e:
                logger.error(f"❌ Auto-accept error: {e}")
                if conn:
                    conn.rollback()
                return web.json_response({"error": str(e)}, status=500, headers=get_cors_headers())
            finally:
                if conn:
                    conn.close()
        else:
            # To'lov muvaffaqiyatsiz
            conn = None
            try:
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute("""
                    UPDATE orders 
                    SET status = 'rejected',
                        payment_status = 'failed',
                        rejected_at = CURRENT_TIMESTAMP
                    WHERE order_id = %s
                """, (order_id,))
                conn.commit()
                conn.close()
                
                return web.json_response({
                    "success": True,
                    "message": "Payment failed, order rejected"
                }, headers=get_cors_headers())
            except Exception as e:
                logger.error(f"❌ Payment failed update error: {e}")
                return web.json_response({"error": str(e)}, status=500, headers=get_cors_headers())
            finally:
                if conn:
                    conn.close()
        
    except Exception as e:
        logger.error(f"❌ Payme callback error: {e}")
        import traceback
        traceback.print_exc()
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
            WHERE status IN ('pending', 'pending_payment', 'payment_pending') 
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
    
    # Handlers - BARCHA KOMMANDALAR
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("myorders", myorders_command))
    
    # Boshqa handlerlar
    application.add_handler(MessageHandler(filters.CONTACT, contact_handler))
    application.add_handler(CallbackQueryHandler(callback_handler))
    
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
    
    # ⭐ PAYME CALLBACK ROUTE
    app.router.add_post('/api/payme/callback', payme_callback_handler)
    app.router.add_options('/api/payme/callback', options_handler)
    
    # CORS preflight
    app.router.add_options('/api/orders', options_handler)
    app.router.add_options('/api/orders/{order_id}', options_handler)
    
    # User profile API
    app.router.add_post('/api/user/profile', get_user_profile_api)
    app.router.add_options('/api/user/profile', options_handler)
    
    app.router.add_post('/api/user/save-profile', save_user_profile_api)
    app.router.add_options('/api/user/save-profile', options_handler)
    
    # Webhook
    app.router.add_post('/webhook', webhook_handler)
    
    app.on_startup.append(init_webhook)
    app.on_cleanup.append(shutdown)
    
    logger.info(f"🚀 Server ishga tushmoqda: 0.0.0.0:{PORT}")
    
    web.run_app(app, host='0.0.0.0', port=PORT)

if __name__ == "__main__":
    main()
