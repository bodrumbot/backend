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

# Environment variables - DATABASE_PUBLIC_URL ishlatish!
DATABASE_URL = os.getenv("DATABASE_PUBLIC_URL") or os.getenv("DATABASE_URL")
TOKEN = os.getenv("TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
ADMIN_URL = f"{WEBAPP_URL}/admin.html"
PORT = int(os.getenv("PORT", "8080"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

# Global application
application = None

# ==========================================
# DATABASE FUNCTIONS (Simple, no pool)
# ==========================================

def get_db_connection():
    """Simple connection - har bir so'rovda yangi connection"""
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL not set!")
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def format_price(price: int) -> str:
    return f"{price:,}".replace(",", " ")

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
            # datetime larni string ga
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
    """Yangi buyurtma yaratish"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # items ni JSON formatga o'tkazish
        items = data.get('items', [])
        if isinstance(items, list):
            items_json = json.dumps(items)
        else:
            items_json = items
        
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
            data.get('status', 'payment_pending'),
            data.get('paymentStatus', 'pending'),
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
            # datetime larni string ga
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
        
        # Qo'shimcha yangilanishlar
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
                WHERE payment_status = 'paid' 
                AND status = 'payment_pending'
                AND created_at > %s
                ORDER BY created_at DESC
            """, (since,))
        else:
            cur.execute("""
                SELECT * FROM orders 
                WHERE payment_status = 'paid' 
                AND status = 'payment_pending'
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
        
        items_text = "\\n".join([f"• {i.get('name')} x{i.get('qty')}" for i in items])
        
        message = f"""🛎️ <b>YANGI BUYURTMA!</b>

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
            [InlineKeyboardButton("🌐 Admin Panel", web_app=WebAppInfo(url=ADMIN_URL))]
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
            [InlineKeyboardButton("⚙️ Admin Panel", web_app=WebAppInfo(url=ADMIN_URL))]
        ]
        await update.message.reply_text(
            f"👋 Salom, Admin <b>{user.first_name}</b>!\\n\\n"
            f"🤖 Bot faol. Yangi buyurtmalar avtomatik keladi.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
    else:
        keyboard = [[InlineKeyboardButton("🍽️ Menyuni ko'rish", web_app=WebAppInfo(url=WEBAPP_URL))]]
        await update.message.reply_text(
            f"👋 Salom, <b>{user.first_name}</b>!\\n\\n"
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
            
            items_text = "\\n".join([f"• {i.get('name')} x{i.get('qty')}" for i in items])
            
            message = f"""{status_text}

🆔 Buyurtma: #{order_id[-6:]}
👤 Mijoz: {order.get('name')}
📞 Telefon: +998 {order.get('phone')}
💵 Summa: {format_price(order.get('total', 0))} so'm

🍽 Mahsulotlar:
{items_text}

⏰ {datetime.now().strftime('%H:%M:%S')}"""
            
            await query.edit_message_text(message, parse_mode='HTML')
            
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
    return web.json_response({
        "status": "ok", 
        "service": "bodrum-bot",
        "mode": "webhook",
        "database_url_set": bool(DATABASE_URL)
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
    
    # WEBHOOK_URL tekshirish
    if not WEBHOOK_URL:
        logger.error("❌ WEBHOOK_URL o'rnatilmagan!")
        raise ValueError("WEBHOOK_URL not set")
    
    webhook_url = WEBHOOK_URL.rstrip('/')
    if not webhook_url.startswith('https://'):
        webhook_url = 'https://' + webhook_url
    
    full_webhook_url = f"{webhook_url}/webhook"
    
    logger.info(f"🔧 Webhook URL: {full_webhook_url}")
    logger.info(f"🔧 Database URL set: {bool(DATABASE_URL)}")
    
    # Eski webhook ni o'chirish
    temp_app = Application.builder().token(TOKEN).build()
    await temp_app.bot.delete_webhook(drop_pending_updates=True)
    await temp_app.shutdown()
    
    # Yangi application
    application = Application.builder().token(TOKEN).build()
    
    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("test", test_command))
    application.add_handler(CallbackQueryHandler(callback_handler))
    
    # Job queue
    job_queue = application.job_queue
    job_queue.run_repeating(check_new_orders, interval=10, first=5)
    
    # Start
    await application.initialize()
    await application.start()
    
    # Webhook o'rnatish
    try:
        await application.bot.set_webhook(full_webhook_url)
        logger.info(f"✅ Webhook o'rnatildi: {full_webhook_url}")
    except Exception as e:
        logger.error(f"❌ Webhook xato: {e}")
        raise
    
    logger.info("🤖 Bot webhook mode da ishga tushdi!")

async def shutdown(app):
    global application
    if application:
        await application.stop()
        await application.shutdown()
        logger.info("🛑 Bot to'xtatildi")

# ==========================================
# MAIN
# ==========================================

def main():
    global application
    
    use_webhook = bool(WEBHOOK_URL and WEBHOOK_URL.strip())
    
    if use_webhook:
        logger.info("🔧 Webhook + HTTP API mode")
        
        app = web.Application()
        
        # CORS
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
        app.router.add_get('/api/orders', get_orders_handler)
        app.router.add_get('/api/orders/new', get_new_orders_handler)
        app.router.add_post('/api/orders', create_order_handler)
        app.router.add_get('/api/orders/{order_id}', get_order_handler)
        app.router.add_put('/api/orders/{order_id}', update_order_handler)
        app.router.add_post('/webhook', webhook_handler)
        
        # CORS
        for route in list(app.router.routes()):
            cors.add(route)
        
        # Lifecycle
        app.on_startup.append(init_webhook)
        app.on_cleanup.append(shutdown)
        
        # Run
        web.run_app(app, host='0.0.0.0', port=PORT)
        
    else:
        # Polling mode (fallback)
        logger.info("🔧 Polling mode")
        
        application = Application.builder().token(TOKEN).build()
        
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("test", test_command))
        application.add_handler(CallbackQueryHandler(callback_handler))
        
        job_queue = application.job_queue
        job_queue.run_repeating(check_new_orders, interval=10, first=5)
        
        logger.info("🤖 Bot polling mode da!")
        application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
