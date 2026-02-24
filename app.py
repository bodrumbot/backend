#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import logging
import asyncio
import psycopg2
import psycopg2.extras
from datetime import datetime
from typing import Optional, Dict, Any

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Railway PostgreSQL
DATABASE_URL = os.getenv("")
TOKEN = os.getenv("TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
ADMIN_URL = f"{WEBAPP_URL}/admin.html"

# Database connection
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def format_price(price: int) -> str:
    return f"{price:,}".replace(",", " ")

def get_order(order_id: str) -> Optional[Dict[str, Any]]:
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
        result = cur.fetchone()
        cur.close()
        conn.close()
        return dict(result) if result else None
    except Exception as e:
        logger.error(f"Get order error: {e}")
        return None

def update_order_status(order_id: str, status: str) -> Optional[Dict[str, Any]]:
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        update_data = {'status': status}
        if status == 'accepted':
            update_data['accepted_at'] = datetime.utcnow().isoformat()
        elif status == 'rejected':
            update_data['rejected_at'] = datetime.utcnow().isoformat()
        
        # Build query dynamically
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
        conn.close()
        return dict(result) if result else None
    except Exception as e:
        logger.error(f"Update error: {e}")
        return None

# Polling for new orders
last_notified_orders = set()
last_check_time = None

async def check_new_orders(context: ContextTypes.DEFAULT_TYPE):
    """Har 10 soniyada yangi buyurtmalarni tekshirish"""
    global last_check_time
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Query for paid + payment_pending orders
        if last_check_time:
            cur.execute("""
                SELECT * FROM orders 
                WHERE payment_status = 'paid' 
                AND status = 'payment_pending'
                AND created_at > %s
                ORDER BY created_at DESC
            """, (last_check_time,))
        else:
            cur.execute("""
                SELECT * FROM orders 
                WHERE payment_status = 'paid' 
                AND status = 'payment_pending'
                ORDER BY created_at DESC
            """)
        
        orders = cur.fetchall()
        cur.close()
        conn.close()
        
        if orders:
            logger.info(f"📊 Yangi buyurtmalar: {len(orders)} ta")
            
            for order in orders:
                order_id = order['order_id']
                
                if order_id in last_notified_orders:
                    continue
                
                await notify_admin(context, dict(order))
                last_notified_orders.add(order_id)
        
        last_check_time = datetime.utcnow().isoformat()
        
    except Exception as e:
        logger.error(f"❌ Tekshirish xatosi: {e}")

async def notify_admin(context: ContextTypes.DEFAULT_TYPE, order: Dict):
    """Admin ga yangi buyurtma haqida xabar yuborish"""
    try:
        items = order.get('items', [])
        if isinstance(items, str):
            import json
            items = json.loads(items)
        
        items_text = "\n".join([f"• {i.get('name')} x{i.get('qty')}" for i in items])
        
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
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE orders SET notified = true WHERE order_id = %s", (order.get('order_id'),))
        conn.commit()
        cur.close()
        conn.close()
        
        logger.info(f"✅ Admin ga xabar yuborildi: {order.get('order_id')}")
        
    except Exception as e:
        logger.error(f"❌ Xabar yuborish xatosi: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_admin = user.id == ADMIN_CHAT_ID
    
    if is_admin:
        keyboard = [
            [InlineKeyboardButton("🍽️ Menyu", web_app=WebAppInfo(url=WEBAPP_URL))],
            [InlineKeyboardButton("⚙️ Admin Panel", web_app=WebAppInfo(url=ADMIN_URL))]
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
                import json
                items = json.loads(items)
            
            items_text = "\n".join([f"• {i.get('name')} x{i.get('qty')}" for i in items])
            
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

def main():
    application = Application.builder().token(TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("test", test_command))
    application.add_handler(CallbackQueryHandler(callback_handler))
    
    # Job Queue - Har 10 soniyada tekshirish
    job_queue = application.job_queue
    job_queue.run_repeating(check_new_orders, interval=10, first=5)
    
    logger.info("🤖 Bot ishga tushdi! Polling: 10 sek")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
