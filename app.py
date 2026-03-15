# ==========================================
# BODRUM BOT - FAQAT POLLING TIZIMI
# Payme callback O'CHIRILDI, faqat polling
# ==========================================

import os
import logging
import asyncio
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import requests

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
    JobQueue
)
from aiohttp import web
import json

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

try:
    ADMIN_CHAT_ID_INT = int(ADMIN_CHAT_ID) if ADMIN_CHAT_ID else 0
except ValueError:
    logger.error(f"❌ ADMIN_CHAT_ID noto'g'ri formatda: {ADMIN_CHAT_ID}")
    ADMIN_CHAT_ID_INT = 0

logger.info(f"🔧 ADMIN_CHAT_ID: {ADMIN_CHAT_ID}, parsed: {ADMIN_CHAT_ID_INT}")

# Global application
application = None

# ⭐ POLLING sozlamalari
POLLING_INTERVAL = 10  # soniyalar
PENDING_PAYMENTS = {}  # {order_id: {"tg_id": ..., "total": ..., "start_time": ...}}
MAX_POLLING_MINUTES = 15  # 15 daqiqa kutish

# ==========================================
# DATABASE FUNCTIONS
# ==========================================

def init_database():
    """Jadval va column'larni avtomatik yaratish/yangilash"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Orders jadvali
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
                initiated_from VARCHAR(50) DEFAULT 'website',
                source VARCHAR(50) DEFAULT 'website'
            )
        """)
        
        # YANGI USTUNLARNI TEKSHIRISH VA QO'SHISH
        columns_to_check = [
            ('initiated_from', 'VARCHAR(50) DEFAULT \'website\''),
            ('source', 'VARCHAR(50) DEFAULT \'website\''),
            ('auto_accepted', 'BOOLEAN DEFAULT FALSE'),
            ('transaction_id', 'VARCHAR(100)'),
            ('paid_at', 'TIMESTAMP'),           # ⭐ QO'SHILDI
            ('confirmed_at', 'TIMESTAMP'),      # ⭐ QO'SHILDI
            ('accepted_at', 'TIMESTAMP'),       # ⭐ QO'SHILDI
            ('rejected_at', 'TIMESTAMP')        # ⭐ QO'SHILDI
        ]
        
        for col_name, col_type in columns_to_check:
            cur.execute(f"""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = 'orders' AND column_name = '{col_name}'
            """)
            if not cur.fetchone():
                cur.execute(f"""
                    ALTER TABLE orders 
                    ADD COLUMN {col_name} {col_type}
                """)
                logger.info(f"✅ '{col_name}' ustuni qo'shildi")
        
        # Users jadvali
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
        
        # Index'lar
        cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_order_id ON orders(order_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_payment_status ON orders(payment_status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_tg_id ON users(tg_id)")
        
        conn.commit()
        cur.close()
        logger.info("✅ Database initialized successfully")
        return True
        
    except Exception as e:
        logger.error(f"❌ Database init error: {e}")
        import traceback
        traceback.print_exc()
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
# TELEGRAM BOT API DIRECT FUNCTIONS
# ==========================================

def send_telegram_message(chat_id: int, text: str, parse_mode: str = 'HTML') -> bool:
    """Direct API call to send message"""
    if not TOKEN:
        logger.error("❌ TOKEN not set")
        return False
    
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        payload = {
            'chat_id': chat_id,
            'text': text,
            'parse_mode': parse_mode
        }
        response = requests.post(url, json=payload, timeout=10)
        result = response.json()
        
        if result.get('ok'):
            logger.info(f"✅ Message sent to {chat_id}")
            return True
        else:
            logger.error(f"❌ Telegram API error: {result}")
            return False
    except Exception as e:
        logger.error(f"❌ send_telegram_message error: {e}")
        return False

def send_telegram_location(chat_id: int, latitude: float, longitude: float) -> bool:
    """Direct API call to send location"""
    if not TOKEN:
        return False
    
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendLocation"
        payload = {
            'chat_id': chat_id,
            'latitude': latitude,
            'longitude': longitude
        }
        response = requests.post(url, json=payload, timeout=10)
        return response.json().get('ok', False)
    except Exception as e:
        logger.error(f"❌ send_telegram_location error: {e}")
        return False

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
        
        tg_id = data.get('tgId') or data.get('tg_id')
        if tg_id:
            try:
                tg_id = int(tg_id)
            except:
                tg_id = None
        
        source = data.get('source', 'website')
        initiated_from = data.get('initiated_from', 'website')
        
        cur.execute("""
            INSERT INTO orders (
                order_id, name, phone, items, total, 
                status, payment_status, payment_method, 
                location, tg_id, notified, created_at,
                initiated_from, source
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
        """, (
            data.get('orderId'), data.get('name'), data.get('phone'),
            items_json, data.get('total'), data.get('status', 'pending_payment'),
            data.get('paymentStatus', 'pending'), data.get('paymentMethod', 'payme'),
            data.get('location'), tg_id, False, datetime.utcnow(),
            initiated_from, source
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
        
        # ⭐ Har bir timestamp ustuni uchun alohida tekshirish
        timestamp_fields = {
            'accepted': 'accepted_at',
            'rejected': 'rejected_at', 
            'confirmed': 'confirmed_at'
        }
        
        if status in timestamp_fields:
            field_name = timestamp_fields[status]
            # Ustun mavjudligini tekshirish
            cur.execute(f"""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = 'orders' AND column_name = '{field_name}'
            """)
            if cur.fetchone():
                update_data[field_name] = datetime.utcnow().isoformat()
        
        # paid_at alohida tekshirish
        if kwargs.get('paid_at'):
            cur.execute("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = 'orders' AND column_name = 'paid_at'
            """)
            if cur.fetchone():
                update_data['paid_at'] = kwargs.get('paid_at')
        
        # Qolgan fieldlar
        for key, val in kwargs.items():
            if val is not None and key not in ['paid_at']:  # paid_at alohida qo'shildi
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
# ⭐ FAQAT POLLING - Payme callback O'CHIRILDI
# ==========================================

async def check_payment_status(order_id: str) -> Dict[str, Any]:
    """
    ⭐ FAQAT DATABASE DAN TEKSHIRISH - Payme callback yo'q!
    Frontend to'lov qilganini database ga yozadi, biz shu yerda tekshiramiz
    """
    try:
        order = get_order(order_id)
        if not order:
            return {"success": False, "error": "Order not found"}
        
        # Database dan tekshirish - frontend to'lov qilganini yozgan bo'lishi kerak
        conn = get_db_connection()
        cur = conn.cursor()
        
        # ⭐ AVVAL ustun mavjudligini tekshiramiz
        cur.execute("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'orders' AND column_name = 'paid_at'
        """)
        has_paid_at = cur.fetchone() is not None
        
        if has_paid_at:
            cur.execute("""
                SELECT payment_status, transaction_id, paid_at 
                FROM orders 
                WHERE order_id = %s
            """, (order_id,))
        else:
            # ⭐ Eski versiya - paid_at ustuni yo'q
            cur.execute("""
                SELECT payment_status, transaction_id, created_at as paid_at 
                FROM orders 
                WHERE order_id = %s
            """, (order_id,))
            
        result = cur.fetchone()
        cur.close()
        conn.close()
        
        if result:
            # ⭐ transaction_id mavjud bo'lsa, to'lov qilingan deb hisoblaymiz
            if result['transaction_id'] or result['payment_status'] == 'paid':
                return {
                    "success": True,
                    "paid": True,
                    "transaction_id": result['transaction_id'],
                    "paid_at": result['paid_at'].isoformat() if result['paid_at'] else None
                }
        
        # Vaqt tekshiruvi - 15 daqiqa o'tgan bo'lsa bekor qilish
        created_at = order.get('created_at')
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at.replace('Z', '+00:00').replace('+00:00', ''))
        
        if datetime.utcnow() - created_at > timedelta(minutes=MAX_POLLING_MINUTES):
            return {"success": True, "paid": False, "expired": True}
        
        return {"success": True, "paid": False, "pending": True}
        
    except Exception as e:
        logger.error(f"❌ To'lov tekshiruvi xatosi: {e}")
        import traceback
        traceback.print_exc()
        return {"success": False, "error": str(e)}

async def polling_payment_check(context: ContextTypes.DEFAULT_TYPE):
    """
    ⭐ HAR 10 SONIYADA KUTILAYOTGAN TO'LOVLARNI TEKSHIRISH
    """
    global PENDING_PAYMENTS
    
    logger.info(f"🔍 Polling tekshiruvi. Kutilayotgan: {len(PENDING_PAYMENTS)}")
    
    # Database dan ham tekshirish
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM orders 
            WHERE status = 'pending_payment' 
            AND created_at > CURRENT_TIMESTAMP - INTERVAL '30 minutes'
        """)
        pending_orders = cur.fetchall()
        cur.close()
        
        for row in pending_orders:
            order = dict(row)
            order_id = order['order_id']
            
            if order_id not in PENDING_PAYMENTS:
                PENDING_PAYMENTS[order_id] = {
                    "tg_id": order.get('tg_id'),
                    "total": order.get('total'),
                    "start_time": order.get('created_at'),
                    "checks": 0
                }
                
    except Exception as e:
        logger.error(f"Database polling xatosi: {e}")
    finally:
        if conn:
            conn.close()
    
    # Har bir kutilayotgan to'lovni tekshirish
    completed_orders = []
    expired_orders = []
    
    for order_id, data in list(PENDING_PAYMENTS.items()):
        try:
            data['checks'] = data.get('checks', 0) + 1
            
            result = await check_payment_status(order_id)
            
            if result.get('paid'):
                logger.info(f"✅ To'lov topildi: {order_id}")
                
                # ⭐ AVVAL ustun mavjudligini tekshiramiz
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute("""
                    SELECT column_name 
                    FROM information_schema.columns 
                    WHERE table_name = 'orders' AND column_name = 'paid_at'
                """)
                has_paid_at = cur.fetchone() is not None
                cur.close()
                conn.close()
                
                if has_paid_at:
                    updated = update_order_status(
                        order_id, 
                        'accepted',
                        payment_status='paid',
                        paid_at=datetime.utcnow().isoformat(),
                        auto_accepted=True,
                        transaction_id=result.get('transaction_id')
                    )
                else:
                    # ⭐ Eski versiya - paid_at ustuni yo'q
                    updated = update_order_status(
                        order_id, 
                        'accepted',
                        payment_status='paid',
                        auto_accepted=True,
                        transaction_id=result.get('transaction_id')
                    )
                
                if updated:
                    await notify_admin_and_customer(updated, data.get('tg_id') is not None)
                    completed_orders.append(order_id)
                    
            elif result.get('expired') or data['checks'] > 90:  # 15 daqiqa
                logger.info(f"⏰ To'lov muddati tugadi: {order_id}")
                
                update_order_status(order_id, 'rejected', payment_status='expired')
                expired_orders.append(order_id)
                
                if data.get('tg_id'):
                    try:
                        await context.bot.send_message(
                            chat_id=data['tg_id'],
                            text=f"⏰ <b>Buyurtma bekor qilindi</b>\n\n"
                                 f"🆔 Buyurtma: #{order_id[-6:]}\n"
                                 f"💳 To'lov 15 daqiqa ichida amalga oshirilmadi.\n\n"
                                 f"Qayta buyurtma berish uchun /start ni bosing.",
                            parse_mode='HTML'
                        )
                    except Exception as e:
                        logger.error(f"Mijozga xabar yuborish xatosi: {e}")
                        
        except Exception as e:
            logger.error(f"Tekshiruv xatosi {order_id}: {e}")
    
    # Tozalash
    for order_id in completed_orders + expired_orders:
        if order_id in PENDING_PAYMENTS:
            del PENDING_PAYMENTS[order_id]
    
    if completed_orders or expired_orders:
        logger.info(f"✅ Yakunlangan: {len(completed_orders)}, ⏰ Tugagan: {len(expired_orders)}")

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
    """Start handler"""
    user = update.effective_user
    is_admin = user.id == ADMIN_CHAT_ID_INT
    
    logger.info(f"🚀 /start - User: {user.id}, Admin: {is_admin}")
    
    if is_admin:
        keyboard = [
            [InlineKeyboardButton("🛎️ Yangi buyurtmalar", callback_data="show_new_orders")],
            [InlineKeyboardButton("📊 Statistika", callback_data="admin_stats")],
            [InlineKeyboardButton("🍽️ Menyu ko'rish", web_app=WebAppInfo(url=WEBAPP_URL))],
            [InlineKeyboardButton("⚙️ Admin Panel", web_app=WebAppInfo(url=f"{WEBAPP_URL}/admin.html"))]
        ]
        
        welcome_text = f"""👋 <b>Salom, Admin {user.first_name}!</b>

🤖 <b>BODRUM</b> admin paneliga xush kelibsiz!

⏰ {datetime.now().strftime('%H:%M:%S')}"""
        
        await update.message.reply_text(
            welcome_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
        return
    
    # Oddiy foydalanuvchi
    profile = get_user_profile(user.id)
    
    if profile and profile.get('phone'):
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
    """Yangi buyurtmalar ro'yxatini ko'rsatish"""
    query = update.callback_query
    await query.answer()
    
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
            "📭 <b>Hozircha yangi buyurtmalar yo'q</b>",
            parse_mode='HTML'
        )
        return
    
    for order in new_orders:
        items = order.get('items', [])
        if isinstance(items, str):
            items = json.loads(items)
        
        items_text = "\n".join([f"• {i.get('name')} x{i.get('qty')}" for i in items]) if items else "Ma'lumot yo'q"
        
        raw_phone = order.get('phone', '')
        phone_display = format_phone_display(raw_phone)
        
        location = order.get('location')
        location_text = ""
        location_coords = None
        if location and ',' in str(location):
            try:
                lat, lng = str(location).split(',')
                lat = float(lat.strip())
                lng = float(lng.strip())
                location_text = f"\n📍 <b>Joylashuv:</b> <a href='https://maps.google.com/?q={lat},{lng}'>Xaritada ko'rish</a>"
                location_coords = (lat, lng)
            except:
                pass
        
        source = order.get('source', 'website')
        source_text = "🌐 Sayt" if source == 'website' else "🤖 WebApp"
        
        polling_status = ""
        if order.get('status') == 'pending_payment':
            time_passed = ""
            created = order.get('created_at')
            if created:
                try:
                    created_dt = datetime.fromisoformat(created.replace('Z', '+00:00').replace('+00:00', ''))
                    minutes = (datetime.utcnow() - created_dt).total_seconds() / 60
                    time_passed = f" ({int(minutes)} daqiqa o'tdi)"
                except:
                    pass
            polling_status = f"\n⏳ <b>Status:</b> To'lov kutilmoqda{time_passed}"

        message = f"""🛎️ <b>YANGI BUYURTMA!</b>

🆔 Buyurtma: #{order.get('order_id', 'N/A')[-6:]}
👤 Mijoz: {order.get('name')}
📞 Telefon: {phone_display}
💵 Summa: {format_price(order.get('total', 0))} so'm
💳 To'lov: {order.get('payment_method', 'N/A').upper()} ⏳
📱 Manba: {source_text}{location_text}{polling_status}

🍽 Mahsulotlar:
{items_text}

⏰ {order.get('created_at', datetime.now().isoformat())[:19]}

<i>⏳ To'lov avtomatik tekshirilmoqda (har 10 soniyada)</i>"""

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
        
        if location_coords:
            try:
                await context.bot.send_location(
                    chat_id=update.effective_user.id,
                    latitude=location_coords[0],
                    longitude=location_coords[1],
                    reply_to_message_id=sent_message.message_id
                )
            except Exception as e:
                logger.error(f"❌ Joylashuv yuborish xatosi: {e}")
    
    await query.edit_message_text(
        f"📋 <b>{len(new_orders)} ta yangi buyurtma</b> yuborildi.\n"
        f"⏳ To'lovlar avtomatik tekshirilmoqda.",
        parse_mode='HTML'
    )

async def contact_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Contact qabul qilish"""
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
        await update.message.reply_text(
            "✅ <b>Ma'lumotlar saqlandi!</b>",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode='HTML'
        )
        
        keyboard = [
            [InlineKeyboardButton("🍽️ Menyuni ko'rish", web_app=WebAppInfo(url=WEBAPP_URL))]
        ]
        
        formatted_phone = f"{phone[:2]} {phone[2:5]} {phone[5:7]} {phone[7:]}"
        
        await update.message.reply_text(
            f"👋 Salom, <b>{user.first_name}</b>!\n\n"
            f"🍽️ <b>BODRUM</b> restoraniga xush kelibsiz!\n\n"
            f"📞 Telefon: +998 {formatted_phone}\n\n"
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
    
    if data == "admin_stats":
        await show_stats(update, context)
        return
    
    if data == "show_new_orders":
        await show_new_orders_list(update, context)
        return
    
    if data == "my_orders":
        await show_user_orders(update, context)
        return
    
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

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Statistika ko'rsatish"""
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
        
        cur.execute("SELECT COUNT(*) FROM orders WHERE status IN ('pending', 'pending_payment')")
        new_count = cur.fetchone()['count']
        
        cur.execute("SELECT COUNT(*), COALESCE(SUM(total), 0) FROM orders WHERE status = 'accepted' AND DATE(accepted_at) = %s", (today,))
        today_result = cur.fetchone()
        today_count = today_result['count']
        today_sum = today_result['coalesce'] or 0
        
        cur.execute("SELECT COUNT(*), COALESCE(SUM(total), 0) FROM orders WHERE status = 'accepted'")
        total_result = cur.fetchone()
        total_count = total_result['count']
        total_sum = total_result['coalesce'] or 0
        
        global PENDING_PAYMENTS
        polling_count = len(PENDING_PAYMENTS)
        
        cur.close()
        conn.close()
        
        stats_text = f"""📊 <b>STATISTIKA</b>

🕐 <b>Bugun ({today}):</b>
• Buyurtmalar: {today_count} ta
• Summa: {format_price(today_sum)} so'm

⏳ <b>Kutilayotgan:</b>
• Yangi buyurtmalar: {new_count} ta
• Polling tekshiruvida: {polling_count} ta

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
            "📭 <b>Sizda hali buyurtmalar yo'q</b>\n\n"
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
    
    orders_text = "📋 <b>SIZNING BUYURTMALARINGIZ</b>\n\n"
    
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
        
        orders_text += f"{i}. {status_emoji} <b>#{order_id_short}</b> - {total} so'm\n"
        orders_text += f"   📅 {created} | {status_text}\n\n"
    
    if len(orders) > 5:
        orders_text += f"... va yana {len(orders) - 5} ta buyurtma\n"
    
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

async def notify_admin_and_customer(order: Dict, is_webapp: bool = False):
    """Admin va mijozga avtomatik xabar yuborish"""
    try:
        logger.info(f"🔔 notify_admin_and_customer: {order.get('order_id')}, is_webapp: {is_webapp}")
        
        if not ADMIN_CHAT_ID_INT:
            logger.error("❌ ADMIN_CHAT_ID o'rnatilmagan!")
            return False
        
        items = order.get('items', [])
        if isinstance(items, str):
            items = json.loads(items)
        
        items_text = "\n".join([f"• {i.get('name')} x{i.get('qty')}" for i in items]) if items else "Ma'lumot yo'q"
        
        raw_phone = order.get('phone', '')
        phone_display = format_phone_display(raw_phone)
        
        location = order.get('location')
        location_text = ""
        location_coords = None
        
        if location and ',' in str(location):
            try:
                lat, lng = str(location).split(',')
                lat = float(lat.strip())
                lng = float(lng.strip())
                location_text = f"\n📍 <b>Joylashuv:</b> <a href='https://maps.google.com/?q={lat},{lng}'>Xaritada ko\'rish</a>"
                location_coords = (lat, lng)
            except Exception as e:
                logger.warning(f"Joylashuv parse xatosi: {e}")
                location_text = f"\n📍 <b>Manzil:</b> {location}"
        elif location:
            location_text = f"\n📍 <b>Manzil:</b> {location}"
        
        source = order.get('source', 'website')
        source_icon = "🤖 WebApp" if source == 'webapp' else "🌐 Sayt"
        
        auto_text = "⚡ <b>AVTOMATIK</b>" if order.get('auto_accepted') else "✅ <b>QABUL QILINDI</b>"
        
        admin_message = f"""{auto_text}

🆔 Buyurtma: #{order.get('order_id', 'N/A')[-6:]}
👤 Mijoz: {order.get('name')}
📞 Telefon: {phone_display}
💵 Summa: {format_price(order.get('total', 0))} so'm
💳 To'lov: PAYME ✅
📱 Manba: {source_icon}{location_text}

🍽 Mahsulotlar:
{items_text}

⏰ {datetime.now().strftime('%H:%M:%S')}

<i>✅ To'lov muvaffaqiyatli - buyurtma qabul qilindi</i>"""

        admin_sent = send_telegram_message(ADMIN_CHAT_ID_INT, admin_message)
        
        if location_coords and admin_sent:
            send_telegram_location(ADMIN_CHAT_ID_INT, location_coords[0], location_coords[1])
        
        if is_webapp:
            tg_id = order.get('tg_id')
            if tg_id:
                customer_message = f"""✅ <b>Buyurtmangiz qabul qilindi!</b>

🆔 Buyurtma: #{order.get('order_id', 'N/A')[-6:]}
💵 Summa: {format_price(order.get('total', 0))} so'm
💳 To'lov: Payme ✅

Buyurtmangiz muvaffaqiyatli to'landi va qabul qilindi!

Tez orada yetkazib beramiz! 🚀

⏰ {datetime.now().strftime('%H:%M:%S')}"""
                
                send_telegram_message(int(tg_id), customer_message)
                logger.info(f"✅ Mijozga xabar yuborildi: {tg_id}")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ notify_admin_and_customer xatosi: {e}")
        import traceback
        traceback.print_exc()
        return False

# ==========================================
# HTTP API HANDLERS - ⭐ PAYME CALLBACK O'CHIRILDI
# ==========================================

async def health_handler(request):
    return web.json_response({
        "status": "ok", 
        "service": "bodrum-bot",
        "timestamp": datetime.utcnow().isoformat(),
        "polling_active": True,
        "pending_payments": len(PENDING_PAYMENTS)
    }, headers=get_cors_headers())

async def create_order_handler(request):
    try:
        data = await request.json()
        logger.info(f"📝 Yangi buyurtma: {data.get('orderId')}")
        
        if not data.get('name'):
            data['name'] = 'Mijoz'
        if not data.get('phone'):
            data['phone'] = '000000000'
        
        order = create_order(data)
        
        if order:
            global PENDING_PAYMENTS
            PENDING_PAYMENTS[order['order_id']] = {
                "tg_id": data.get('tgId') or data.get('tg_id'),
                "total": data.get('total'),
                "start_time": datetime.utcnow().isoformat(),
                "checks": 0
            }
            
            logger.info(f"✅ Buyurtma yaratildi va polling ga qo'shildi: {order['order_id']}")
            return web.json_response({
                **order,
                "polling_started": True,
                "message": "Buyurtma yaratildi. To'lov 15 daqiqa ichida amalga oshirilishi kerak."
            }, status=201, headers=get_cors_headers())
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
        
        global PENDING_PAYMENTS
        polling_info = PENDING_PAYMENTS.get(order_id, {})
        
        return web.json_response({
            **order,
            "polling_status": {
                "active": order_id in PENDING_PAYMENTS,
                "checks": polling_info.get('checks', 0),
                "elapsed_minutes": polling_info.get('elapsed_minutes', 0)
            }
        }, headers=get_cors_headers())
    except Exception as e:
        logger.error(f"API get order error: {e}")
        return web.json_response({"error": str(e)}, status=500, headers=get_cors_headers())

async def check_payment_handler(request):
    """⭐ Frontend dan to'lov statusini so'rash"""
    try:
        order_id = request.match_info['order_id']
        result = await check_payment_status(order_id)
        return web.json_response(result, headers=get_cors_headers())
    except Exception as e:
        logger.error(f"Check payment error: {e}")
        return web.json_response({"error": str(e)}, status=500, headers=get_cors_headers())

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

async def save_user_profile_api(request):
    """Foydalanuvchi profilini saqlash"""
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
    """Foydalanuvchi profilini olish"""
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

# ⭐ PAYME CALLBACK HANDLER O'CHIRILDI - FAQAT POLLING ISHLATILADI

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
    
    # ⭐ POLLING JOBS - HAR 10 SONIYADA
    job_queue = application.job_queue
    
    job_queue.run_repeating(
        polling_payment_check,
        interval=POLLING_INTERVAL,
        first=5,
        name="payment_polling"
    )
    
    job_queue.run_repeating(
        lambda ctx: asyncio.create_task(cleanup_old_payments()),
        interval=60,
        first=30,
        name="cleanup_old"
    )
    
    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("myorders", myorders_command))
    
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
    logger.info(f"⏰ Polling har {POLLING_INTERVAL} soniyada ishga tushadi")

async def cleanup_old_payments():
    """Eski to'lovlarni tozalash"""
    global PENDING_PAYMENTS
    current_time = datetime.utcnow()
    to_remove = []
    
    for order_id, data in PENDING_PAYMENTS.items():
        try:
            start_time = data.get('start_time')
            if isinstance(start_time, str):
                start_time = datetime.fromisoformat(start_time.replace('Z', '+00:00').replace('+00:00', ''))
            
            if (current_time - start_time).total_seconds() > 1800:
                to_remove.append(order_id)
        except:
            pass
    
    for order_id in to_remove:
        del PENDING_PAYMENTS[order_id]
        logger.info(f"🧹 Eski to'lov o'chirildi: {order_id}")

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
    logger.info("⏰ FAQAT POLLING tizimi faollashdi! Payme callback O'CHIRILDI")
    
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
    
    # ⭐ POLLING API - Payme callback yo'q!
    app.router.add_get('/api/orders/{order_id}/payment-status', check_payment_handler)
    
    # CORS preflight
    app.router.add_options('/api/orders', options_handler)
    app.router.add_options('/api/orders/{order_id}', options_handler)
    app.router.add_options('/api/orders/{order_id}/payment-status', options_handler)
    
    # User profile API
    app.router.add_post('/api/user/profile', get_user_profile_api)
    app.router.add_options('/api/user/profile', options_handler)
    
    app.router.add_post('/api/user/save-profile', save_user_profile_api)
    app.router.add_options('/api/user/save-profile', options_handler)
    
    # Webhook
    app.router.add_post('/webhook', webhook_handler)
    
    # ⭐ PAYME CALLBACK ROUTE O'CHIRILDI
    
    app.on_startup.append(init_webhook)
    app.on_cleanup.append(shutdown)
    
    logger.info(f"🚀 Server ishga tushmoqda: 0.0.0.0:{PORT}")
    logger.info(f"⏰ Polling interval: {POLLING_INTERVAL} soniya")
    logger.info("❌ Payme callback o'chirildi - faqat polling ishlatiladi")
    
    web.run_app(app, host='0.0.0.0', port=PORT)

if __name__ == "__main__":
    main()
