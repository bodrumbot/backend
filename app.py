import psycopg2
import psycopg2.extras
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
from datetime import datetime
import os
from dotenv import load_dotenv
import asyncio
import threading
import socketio
import requests

load_dotenv()

# PostgreSQL ulanish
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("DATABASE_PUBLIC_URL")

def get_db():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL topilmadi!")
    
    # Railway internal URL lokalda ishlamaydi
    if 'railway.internal' in DATABASE_URL:
        # Railway'da ishlaganda
        return psycopg2.connect(DATABASE_URL, sslmode='require')
    else:
        # Lokalda ishlaganda (localhost yoki public URL)
        return psycopg2.connect(DATABASE_URL)

# Web App URL (Railway'da deploy qilingan)
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://bodrumbot.github.io/11")
ADMIN_URL = f"{WEBAPP_URL}/admin.html"

TOKEN = os.getenv("TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID"))

def init_db():
    """Baza jadvallarini yaratish (Web App bilan bir xil struktura)"""
    conn = get_db()
    cur = conn.cursor()
    
    # Orders jadvali (Web App bilan bir xil)
    cur.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            order_id VARCHAR(50) UNIQUE,
            name VARCHAR(100),
            phone VARCHAR(20),
            items JSONB,
            total INTEGER,
            status VARCHAR(20) DEFAULT 'pending_payment',
            payment_status VARCHAR(20) DEFAULT 'pending',
            payment_method VARCHAR(20) DEFAULT 'payme',
            location VARCHAR(100),
            tg_id BIGINT,
            created_at TIMESTAMP DEFAULT NOW(),
            accepted_at TIMESTAMP,
            rejected_at TIMESTAMP,
            notified BOOLEAN DEFAULT FALSE
        )
    ''')
    
    # Adminlar jadvali
    cur.execute('''
        CREATE TABLE IF NOT EXISTS admins (
            id SERIAL PRIMARY KEY,
            tg_id BIGINT UNIQUE,
            username VARCHAR(100),
            added_at TIMESTAMP DEFAULT NOW()
        )
    ''')
    
    conn.commit()
    cur.close()
    conn.close()
    print("✅ Baza tayyor")

def is_admin(user_id: int) -> bool:
    """Tekshirish: foydalanuvchi adminmi?"""
    if user_id == ADMIN_CHAT_ID:
        return True
    
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM admins WHERE tg_id = %s", (user_id,))
        result = cur.fetchone()
        cur.close()
        conn.close()
        return result is not None
    except:
        return False

async def check_new_orders(context: ContextTypes.DEFAULT_TYPE):
    """Har 5 sekundda yangi to'langan buyurtmalarni tekshirish"""
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # To'langan va hali xabar yuborilmagan buyurtmalar
        cur.execute("""
            SELECT * FROM orders 
            WHERE payment_status = 'paid' 
            AND status = 'pending'
            AND (notified IS NULL OR notified = FALSE)
        """)
        
        orders = cur.fetchall()
        
        for order in orders:
            name = order['name']
            phone = order['phone']
            total = order['total']
            
            message = f"""✅ <b>YANGI TO'LANGAN BUYURTMA!</b>
👤 Mijoz: {name}
📞 Telefon: +998{phone}
💰 Summa: {total:,} so'm

🍔 Mahsulotlar:"""
            
            items = order['items']
            if isinstance(items, list):
                for item in items:
                    message += f"\n• {item.get('name', 'Nomalum')} x{item.get('qty', 1)}"
            elif isinstance(items, dict):
                for item_name, qty in items.items():
                    message += f"\n• {item_name} x{qty}"
            
            keyboard = [
                [
                    InlineKeyboardButton("✅ Qabul", callback_data=f"accept_{order['order_id']}"),
                    InlineKeyboardButton("❌ Bekor", callback_data=f"reject_{order['order_id']}")
                ],
                [
                    InlineKeyboardButton("🌐 Admin Panel", url=ADMIN_URL)
                ]
            ]
            
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=message,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='HTML'
            )
            
            # Notified qilish
            cur.execute("UPDATE orders SET notified = TRUE WHERE id = %s", (order['id'],))
            conn.commit()
            
            # Web App'ga ham xabar yuborish (Socket.io orqali)
            try:
                requests.post(f"{WEBAPP_URL}/api/notify", json={
                    "order_id": order['order_id'],
                    "status": "paid"
                }, timeout=2)
            except:
                pass
        
        cur.close()
        conn.close()
        
    except Exception as e:
        print(f"Xato: {e}")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    try:
        data = query.data
        action, order_id = data.split('_', 1)
        
        conn = get_db()
        cur = conn.cursor()
        
        if action == 'accept':
            cur.execute("""
                UPDATE orders 
                SET status = 'accepted', accepted_at = NOW() 
                WHERE order_id = %s
            """, (order_id,))
            text = f"\n\n✅ <b>QABUL QILINDI</b>"
            
            # Web App'ga ham xabar yuborish
            try:
                requests.post(f"{WEBAPP_URL}/api/orders/{order_id}/status", json={
                    "status": "accepted"
                }, timeout=2)
            except:
                pass
                
        elif action == 'reject':
            cur.execute("""
                UPDATE orders 
                SET status = 'rejected', rejected_at = NOW() 
                WHERE order_id = %s
            """, (order_id,))
            text = f"\n\n❌ <b>BEKOR QILINDI</b>"
            
            # Web App'ga ham xabar yuborish
            try:
                requests.post(f"{WEBAPP_URL}/api/orders/{order_id}/status", json={
                    "status": "rejected"
                }, timeout=2)
            except:
                pass
        else:
            return
        
        conn.commit()
        cur.close()
        conn.close()
        
        await query.edit_message_text(
            text=query.message.text + text,
            parse_mode='HTML'
        )
            
    except Exception as e:
        print(f"Callback xato: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # Admin uchun alohida tugmalar
    if is_admin(user.id):
        keyboard = [
            [InlineKeyboardButton("🍽️ Menyu", web_app=WebAppInfo(url=WEBAPP_URL))],
            [InlineKeyboardButton("⚙️ Admin Panel", web_app=WebAppInfo(url=ADMIN_URL))],
            [InlineKeyboardButton("📊 Statistika", callback_data="stats")]
        ]
        await update.message.reply_text(
            f"👋 Salom, Admin {user.first_name}!\n\n🍽️ BODRUM restorani boshqaruv paneli",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        keyboard = [[InlineKeyboardButton("🍽️ Menyu", web_app=WebAppInfo(url=WEBAPP_URL))]]
        await update.message.reply_text(
            f"👋 Salom, {user.first_name}!\n\n🍽️ BODRUM restorani",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Statistikani ko'rsatish"""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    
    await query.answer()
    
    try:
        conn = get_db()
        cur = conn.cursor()
        
        # Bugungi statistika
        cur.execute("""
            SELECT 
                COUNT(*) as total_orders,
                COALESCE(SUM(total), 0) as total_sum,
                COUNT(CASE WHEN status = 'accepted' THEN 1 END) as accepted,
                COUNT(CASE WHEN status = 'pending' THEN 1 END) as pending
            FROM orders 
            WHERE DATE(created_at) = CURRENT_DATE
        """)
        
        stats = cur.fetchone()
        
        # Oxirgi 7 kunlik statistika
        cur.execute("""
            SELECT 
                DATE(created_at) as date,
                COUNT(*) as count,
                COALESCE(SUM(total), 0) as sum
            FROM orders 
            WHERE created_at >= CURRENT_DATE - INTERVAL '7 days'
            AND payment_status = 'paid'
            GROUP BY DATE(created_at)
            ORDER BY date DESC
        """)
        
        weekly = cur.fetchall()
        
        cur.close()
        conn.close()
        
        weekly_text = "\n".join([
            f"📅 {row[0].strftime('%d.%m')}: {row[1]} ta ({row[2]:,} so'm)"
            for row in weekly[:5]
        ])
        
        text = f"""📊 <b>Bugungi statistika:</b>

📝 Jami buyurtmalar: {stats[0]}
✅ Qabul qilingan: {stats[2]}
⏳ Kutilmoqda: {stats[3]}
💰 Jami summa: {stats[1]:,} so'm

📈 Oxirgi 7 kun:
{weekly_text}"""
        
        keyboard = [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        
    except Exception as e:
        print(f"Statistika xato: {e}")

async def back_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Orqaga qaytish"""
    query = update.callback_query
    await query.answer()
    
    # Start funksiyasini chaqirish
    await start(update, context)

def main():
    init_db()
    
    application = Application.builder().token(TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler, pattern="^(accept|reject)_"))
    application.add_handler(CallbackQueryHandler(stats_handler, pattern="^stats$"))
    application.add_handler(CallbackQueryHandler(back_handler, pattern="^back_to_start$"))
    
    # Har 5 sekundda tekshirish
    job_queue = application.job_queue
    job_queue.run_repeating(check_new_orders, interval=5, first=1)
    
    print("🤖 Bot ishga tushdi...")
    print(f"🌐 Web App URL: {WEBAPP_URL}")
    print(f"👑 Admin URL: {ADMIN_URL}")
    application.run_polling()

if __name__ == "__main__":
    main()