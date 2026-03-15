# ==========================================
# BODRUM BOT - Payme callback bilan
# ==========================================

import os
import logging
import asyncio
import psycopg2
import psycopg2.extras
from datetime import datetime
from typing import Optional, Dict, Any, List
import requests
import json

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

load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

PORT = int(os.getenv("PORT", "3000"))
DATABASE_URL = os.getenv("DATABASE_PUBLIC_URL") or os.getenv("DATABASE_URL")
TOKEN = os.getenv("TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")

try:
    ADMIN_CHAT_ID_INT = int(ADMIN_CHAT_ID) if ADMIN_CHAT_ID else 0
except ValueError:
    ADMIN_CHAT_ID_INT = 0

application = None

# ==========================================
# DATABASE FUNKSIYALARI
# ==========================================

def get_db_connection():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL not set!")
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def init_database():
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
        
        conn.commit()
        cur.close()
        logger.info("✅ Database initialized")
        return True
    except Exception as e:
        logger.error(f"❌ Database init error: {e}")
        return False
    finally:
        if conn:
            conn.close()

def create_order(data: Dict) -> Optional[Dict]:
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        items = data.get('items', [])
        items_json = json.dumps(items) if isinstance(items, list) else items
        
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
                location, tg_id, created_at,
                initiated_from, source
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
        """, (
            data.get('orderId'), data.get('name'), data.get('phone'),
            items_json, data.get('total'), data.get('status', 'pending_payment'),
            data.get('paymentStatus', 'pending'), data.get('paymentMethod', 'payme'),
            data.get('location'), tg_id, datetime.utcnow(),
            data.get('initiated_from', 'website'), data.get('source', 'website')
        ))
        
        result = cur.fetchone()
        conn.commit()
        cur.close()
        
        if result:
            return dict(result)
        return None
    except Exception as e:
        logger.error(f"Create order error: {e}")
        if conn:
            conn.rollback()
        return None
    finally:
        if conn:
            conn.close()

def get_order(order_id: str) -> Optional[Dict]:
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
        result = cur.fetchone()
        cur.close()
        return dict(result) if result else None
    except Exception as e:
        logger.error(f"Get order error: {e}")
        return None
    finally:
        if conn:
            conn.close()

def update_order_status(order_id: str, status: str, **kwargs) -> Optional[Dict]:
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        update_data = {'status': status}
        if status == 'accepted':
            update_data['accepted_at'] = datetime.utcnow()
        elif status == 'rejected':
            update_data['rejected_at'] = datetime.utcnow()
        
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
        
        return dict(result) if result else None
    except Exception as e:
        logger.error(f"Update error: {e}")
        if conn:
            conn.rollback()
        return None
    finally:
        if conn:
            conn.close()

def format_price(price: int) -> str:
    return f"{price:,}".replace(",", " ")

# ==========================================
# TELEGRAM FUNKSIYALARI
# ==========================================

def send_telegram_message(chat_id: int, text: str, parse_mode: str = 'HTML') -> bool:
    if not TOKEN:
        return False
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        payload = {
            'chat_id': chat_id,
            'text': text,
            'parse_mode': parse_mode
        }
        response = requests.post(url, json=payload, timeout=10)
        return response.json().get('ok', False)
    except Exception as e:
        logger.error(f"Send message error: {e}")
        return False

def send_telegram_location(chat_id: int, lat: float, lng: float) -> bool:
    if not TOKEN:
        return False
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendLocation"
        payload = {
            'chat_id': chat_id,
            'latitude': lat,
            'longitude': lng
        }
        response = requests.post(url, json=payload, timeout=10)
        return response.json().get('ok', False)
    except Exception as e:
        logger.error(f"Send location error: {e}")
        return False

# ==========================================
# PAYME CALLBACK HANDLER (ENG MUHIM)
# ==========================================

async def notify_admin_and_customer(order: Dict):
    """Admin va mijozga xabar yuborish"""
    try:
        if not ADMIN_CHAT_ID_INT:
            return
        
        items = order.get('items', [])
        if isinstance(items, str):
            items = json.loads(items)
        
        items_text = "\n".join([f"• {i.get('name')} x{i.get('qty')}" for i in items]) if items else "Ma'lumot yo'q"
        
        # Telefon formatlash
        phone = order.get('phone', '')
        if len(phone) == 9:
            phone_display = f"+998 {phone[:2]} {phone[2:5]} {phone[5:7]} {phone[7:]}"
        else:
            phone_display = phone
        
        # Joylashuv
        location_text = ""
        location_coords = None
        if order.get('location'):
            if ',' in str(order['location']):
                try:
                    lat, lng = str(order['location']).split(',')
                    lat = float(lat.strip())
                    lng = float(lng.strip())
                    location_text = f"\n📍 Joylashuv: https://maps.google.com/?q={lat},{lng}"
                    location_coords = (lat, lng)
                except:
                    location_text = f"\n📍 Manzil: {order['location']}"
            else:
                location_text = f"\n📍 Manzil: {order['location']}"
        
        # Admin ga xabar
        admin_message = f"""⚡ YANGI BUYURTMA (Avtomatik)!

🆔 Buyurtma: #{order['order_id'][-6:]}
👤 Mijoz: {order.get('name')}
📞 Telefon: {phone_display}
💵 Summa: {format_price(order.get('total', 0))} so'm
💳 To'lov: Payme ✅ (Avtomatik)
{location_text}

🍽 Mahsulotlar:
{items_text}

⏰ {datetime.now().strftime('%H:%M:%S')}"""
        
        send_telegram_message(ADMIN_CHAT_ID_INT, admin_message)
        
        if location_coords:
            send_telegram_location(ADMIN_CHAT_ID_INT, location_coords[0], location_coords[1])
        
        # Mijozga xabar
        if order.get('tg_id'):
            customer_message = f"""✅ Buyurtmangiz qabul qilindi!

🆔 Buyurtma: #{order['order_id'][-6:]}
💵 Summa: {format_price(order.get('total', 0))} so'm
💳 To'lov: Payme ✅

Buyurtmangiz muvaffaqiyatli to'landi va avtomatik qabul qilindi!

Tez orada yetkazib beramiz! 🚀

⏰ {datetime.now().strftime('%H:%M:%S')}"""
            
            send_telegram_message(int(order['tg_id']), customer_message)
        
        logger.info(f"✅ Xabarlar yuborildi: {order['order_id']}")
        return True
        
    except Exception as e:
        logger.error(f"Xabar yuborish xatosi: {e}")
        return False

# ⭐ PAYME CALLBACK HANDLER
async def payme_callback_handler(request):
    """Payme dan to'lov xabari kelganda"""
    try:
        data = await request.json()
        logger.info(f"💰 Payme callback: {data}")
        
        order_id = data.get('order_id')
        status = data.get('status')
        transaction_id = data.get('transaction_id')
        
        if not order_id:
            return web.json_response({"error": "order_id required"}, status=400)
        
        order = get_order(order_id)
        if not order:
            return web.json_response({"error": "Order not found"}, status=404)
        
        if status == 'success' or status == 'completed':
            # Buyurtmani avtomatik qabul qilish
            updated = update_order_status(
                order_id, 
                'accepted',
                payment_status='paid',
                paid_at=datetime.utcnow(),
                transaction_id=transaction_id,
                auto_accepted=True,
                notified=False
            )
            
            if updated:
                logger.info(f"✅ Buyurtma avtomatik qabul qilindi: {order_id}")
                
                # Admin va mijozga xabar yuborish
                await notify_admin_and_customer(updated)
                
                return web.json_response({
                    "success": True,
                    "message": "Payment successful, order auto-accepted",
                    "order": updated
                })
        else:
            # To'lov muvaffaqiyatsiz
            update_order_status(order_id, 'rejected', payment_status='failed')
            logger.info(f"❌ To'lov muvaffaqiyatsiz: {order_id}")
            
            return web.json_response({
                "success": True,
                "message": "Payment failed"
            })
        
    except Exception as e:
        logger.error(f"Payme callback error: {e}")
        return web.json_response({"error": str(e)}, status=500)

# ==========================================
# API HANDLERLAR
# ==========================================

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
            return web.json_response(order, status=201)
        else:
            return web.json_response({"error": "Failed to create order"}, status=500)
    except Exception as e:
        logger.error(f"Create order error: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def orders_list_handler(request):
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
        
        orders = [dict(row) for row in results]
        return web.json_response(orders)
    except Exception as e:
        logger.error(f"Orders list error: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def get_user_profile_handler(request):
    try:
        data = await request.json()
        tg_id = data.get('tgId')
        
        if not tg_id:
            return web.json_response({"success": False, "error": "tgId required"}, status=400)
        
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("SELECT * FROM users WHERE tg_id = %s", (tg_id,))
        profile = cur.fetchone()
        
        cur.execute("SELECT * FROM orders WHERE tg_id = %s ORDER BY created_at DESC", (tg_id,))
        orders = cur.fetchall()
        
        cur.close()
        conn.close()
        
        return web.json_response({
            "success": True,
            "profile": dict(profile) if profile else None,
            "orders": [dict(o) for o in orders]
        })
    except Exception as e:
        logger.error(f"Get profile error: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)

def get_cors_headers():
    return {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type',
    }

async def options_handler(request):
    return web.Response(headers=get_cors_headers())

# ==========================================
# WEBHOOK HANDLER
# ==========================================

async def webhook_handler(request):
    global application
    if application:
        try:
            data = await request.json()
            update = Update.de_json(data, application.bot)
            await application.process_update(update)
        except Exception as e:
            logger.error(f"Webhook error: {e}")
    return web.Response(text='OK')

# ==========================================
# MAIN
# ==========================================

async def init_webhook(app):
    global application
    
    if not TOKEN:
        logger.error("❌ TOKEN o'rnatilmagan!")
        return
    
    init_database()
    
    webhook_url = os.getenv("WEBHOOK_URL", "")
    if not webhook_url:
        railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
        if railway_domain:
            webhook_url = f"https://{railway_domain}"
    
    application = Application.builder().token(TOKEN).build()
    
    # Bot command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("stats", stats_command))
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
    
    logger.info(f"🤖 Bot ishga tushdi! Admin: {ADMIN_CHAT_ID_INT}")

async def shutdown(app):
    global application
    if application:
        await application.stop()

# Bot command handlers
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_admin = user.id == ADMIN_CHAT_ID_INT
    
    if is_admin:
        keyboard = [
            [InlineKeyboardButton("📊 Statistika", callback_data="admin_stats")],
            [InlineKeyboardButton("🍽️ Menyu", web_app=WebAppInfo(url=WEBAPP_URL))],
            [InlineKeyboardButton("⚙️ Admin Panel", web_app=WebAppInfo(url=f"{WEBAPP_URL}/admin.html"))]
        ]
        await update.message.reply_text(
            f"👋 Salom, Admin {user.first_name}!",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        keyboard = [
            [InlineKeyboardButton("🍽️ Menyuni ko'rish", web_app=WebAppInfo(url=WEBAPP_URL))]
        ]
        await update.message.reply_text(
            f"👋 Salom, {user.first_name}! BODRUM restoraniga xush kelibsiz!",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID_INT:
        return
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    today = datetime.now().strftime('%Y-%m-%d')
    
    cur.execute("SELECT COUNT(*) FROM orders WHERE status IN ('pending', 'pending_payment')")
    new_count = cur.fetchone()['count']
    
    cur.execute("SELECT COUNT(*), COALESCE(SUM(total), 0) FROM orders WHERE status = 'accepted' AND DATE(accepted_at) = %s", (today,))
    today_result = cur.fetchone()
    
    cur.execute("SELECT COUNT(*), COALESCE(SUM(total), 0) FROM orders WHERE status = 'accepted'")
    total_result = cur.fetchone()
    
    cur.close()
    conn.close()
    
    stats_text = f"""📊 STATISTIKA

📅 Bugun: {today}
• Buyurtmalar: {today_result['count']} ta
• Summa: {format_price(today_result['coalesce'])} so'm

⏳ Kutilayotgan: {new_count} ta

📈 Jami:
• Qabul qilingan: {total_result['count']} ta
• Umumiy summa: {format_price(total_result['coalesce'])} so'm"""

    await update.message.reply_text(stats_text)

async def contact_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact = update.message.contact
    user = update.effective_user
    
    if not contact:
        return
    
    phone = contact.phone_number
    if phone.startswith('+'):
        phone = phone[1:]
    if phone.startswith('998'):
        phone = phone[3:]
    phone = phone[-9:] if len(phone) > 9 else phone
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (tg_id, name, phone, username)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (tg_id) DO UPDATE SET
            name = EXCLUDED.name,
            phone = EXCLUDED.phone,
            updated_at = CURRENT_TIMESTAMP
    """, (user.id, user.first_name, phone, user.username))
    conn.commit()
    cur.close()
    conn.close()
    
    await update.message.reply_text(
        "✅ Telefon raqam saqlandi!",
        reply_markup=ReplyKeyboardRemove()
    )

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "admin_stats":
        await stats_command(update, context)

def main():
    logger.info("🚀 Server ishga tushmoqda...")
    
    app = web.Application()
    
    # Routes
    app.router.add_get('/', lambda r: web.json_response({"status": "ok"}))
    app.router.add_post('/api/orders', create_order_handler)
    app.router.add_get('/api/orders', orders_list_handler)
    app.router.add_post('/api/payme/callback', payme_callback_handler)
    app.router.add_post('/api/user/profile', get_user_profile_handler)
    app.router.add_post('/webhook', webhook_handler)
    
    # CORS preflight
    app.router.add_options('/api/payme/callback', options_handler)
    app.router.add_options('/api/orders', options_handler)
    
    app.on_startup.append(init_webhook)
    app.on_cleanup.append(shutdown)
    
    web.run_app(app, host='0.0.0.0', port=PORT)

if __name__ == "__main__":
    main()
