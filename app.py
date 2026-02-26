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
    ContextTypes,
    filters
)
from aiohttp import web
import aiohttp_cors
import json

load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==========================================
# ENVIRONMENT VARIABLES - RAILWAY UCHUN
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
    """Database connection"""
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
    """Check if column exists in table"""
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
    """Bitta buyurtma olish"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
        result = cur.fetchone()
        cur.close()
        
        if result:
            order_dict = dict(result)
            for key in ['created_at', 'accepted_at', 'rejected_at', 'paid_at']:
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
    """Yangi buyurtma yaratish - skrinshot bilan (fallback)"""
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
        
        # Check if screenshot columns exist
        has_screenshot_col = check_column_exists('orders', 'screenshot')
        has_screenshot_name_col = check_column_exists('orders', 'screenshot_name')
        
        logger.info(f"📊 Database columns: screenshot={has_screenshot_col}, screenshot_name={has_screenshot_name_col}")
        
        if has_screenshot_col and has_screenshot_name_col:
            # Full insert with screenshot
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
                data.get('orderId'),
                data.get('name'),
                data.get('phone'),
                items_json,
                data.get('total'),
                data.get('status', 'pending_verification'),
                data.get('paymentStatus', 'pending_verification'),
                data.get('paymentMethod', 'payme'),
                data.get('location'),
                data.get('tgId'),
                False,
                datetime.utcnow(),
                screenshot,
                screenshot_name
            ))
        else:
            # Fallback: insert without screenshot columns
            logger.warning("⚠️ Screenshot columns not found, using fallback insert")
            cur.execute("""
                INSERT INTO orders (
                    order_id, name, phone, items, total, 
                    status, payment_status, payment_method, 
                    location, tg_id, notified, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
            """, (
                data.get('orderId'),
                data.get('name'),
                data.get('phone'),
                items_json,
                data.get('total'),
                data.get('status', 'pending_verification'),
                data.get('paymentStatus', 'pending_verification'),
                data.get('paymentMethod', 'payme'),
                data.get('location'),
                data.get('tgId'),
                False,
                datetime.utcnow()
            ))
        
        result = cur.fetchone()
        conn.commit()
        cur.close()
        
        if result:
            order_dict = dict(result)
            for key in ['created_at', 'accepted_at', 'rejected_at', 'paid_at']:
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
    """Barcha buyurtmalarni olish"""
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
            for key in ['created_at', 'accepted_at', 'rejected_at', 'paid_at']:
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
    """Buyurtma statusini yangilash"""
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
            for key in ['created_at', 'accepted_at', 'rejected_at', 'paid_at']:
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
    """Yangi buyurtmalarni olish"""
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
            for key in ['created_at', 'accepted_at', 'rejected_at', 'paid_at']:
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
    """Har 10 soniyada yangi buyurtmalarni tekshirish"""
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
    """Admin ga yangi buyurtma haqida xabar yuborish"""
    try:
        items = order.get('items', [])
        if isinstance(items, str):
            items = json.loads(items)
        
        items_text = "\n".join([f"• {i.get('name')} x{i.get('qty')}" for i in items])
        
        # Screenshot borligini tekshirish
        has_screenshot = order.get('screenshot') or order.get('screenshot_name')
        screenshot_indicator = " 📸" if has_screenshot else ""
        
        message = f"""🛎️ <b>YANGI BUYURTMA!{screenshot_indicator}</b>

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
        
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
        
        # Mark as notified
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
            
            # Screenshot borligini ko'rsatish
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
            await query.edit_message_text("❌ Xatolik yuz berdi!")

async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Test uchun - qo'lda tekshirish"""
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
    """Health check"""
    return web.json_response({
        "status": "ok", 
        "service": "bodrum-bot",
        "timestamp": datetime.utcnow().isoformat(),
        "database_url_set": bool(DATABASE_URL),
        "webhook_url": WEBHOOK_URL
    })

async def get_orders_handler(request):
    """Barcha buyurtmalarni olish"""
    try:
        orders = get_all_orders()
        return web.json_response(orders)
    except Exception as e:
        logger.error(f"API get orders error: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def get_new_orders_handler(request):
    """Yangi buyurtmalarni olish (polling uchun)"""
    try:
        orders = get_new_orders()
        return web.json_response(orders)
    except Exception as e:
        logger.error(f"API get new orders error: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def create_order_handler(request):
    """Yangi buyurtma yaratish"""
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
    """Bitta buyurtma ma'lumotlari"""
    try:
        order_id = request.match_info['order_id']
        order = get_order(order_id)
        
        if not order:
            return web.json_response({"error": "Not found"}, status=404)
        
        return web.json_response(order)
    except Exception as e:
        logger.error(f"API get order error: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def update_order_handler(request):
    """Buyurtma statusini yangilash"""
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
            for key in ['created_at', 'accepted_at', 'rejected_at', 'paid_at']:
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
    """Telegram webhook"""
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
    
    app.router.add_get('/', health_handler)
    app.router.add_get('/health', health_handler)
    app.router.add_get('/api/orders', get_orders_handler)
    app.router.add_get('/api/orders/new', get_new_orders_handler)
    app.router.add_post('/api/orders', create_order_handler)
    app.router.add_get('/api/orders/{order_id}', get_order_handler)
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
