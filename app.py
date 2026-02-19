import psycopg2
import psycopg2.extras
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
from datetime import datetime
import os

# PostgreSQL ulanish
DATABASE_URL = os.getenv("DATABASE_URL")

def get_db():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

# Baza yaratish
def init_db():
    conn = get_db()
    cur = conn.cursor()
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
            location VARCHAR(100),
            tg_id BIGINT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    ''')
    conn.commit()
    cur.close()
    conn.close()

TOKEN = os.getenv("TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID"))

MENU_URL = "https://bodrumbot.github.io/11/index.html"
ADMIN_URL = "https://bodrumbot.github.io/11/admin.html"

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_CHAT_ID

async def check_new_orders(context: ContextTypes.DEFAULT_TYPE):
    """Har 5 sekundda yangi buyurtmalarni tekshirish"""
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
            
            message = f"""✅ <b>YANGI BUYURTMA!</b>
👤 Mijoz: {name}
📞 Telefon: +998{phone}
💰 Summa: {total:,} so'm

🍔 Mahsulotlar:"""
            
            items = order['items']
            for item in items:
                message += f"\n• {item['name']} x{item['qty']}"
            
            keyboard = [
                [
                    InlineKeyboardButton("✅ Qabul", callback_data=f"accept_{order['order_id']}"),
                    InlineKeyboardButton("❌ Bekor", callback_data=f"reject_{order['order_id']}")
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
        else:
            cur.execute("""
                UPDATE orders 
                SET status = 'rejected', rejected_at = NOW() 
                WHERE order_id = %s
            """, (order_id,))
            text = f"\n\n❌ <b>BEKOR QILINDI</b>"
        
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
    
    keyboard = [[InlineKeyboardButton("🍽️ Menyu", web_app=WebAppInfo(url=MENU_URL))]]
    
    await update.message.reply_text(
        f"👋 Salom, {user.first_name}!\n\n🍽️ BODRUM restorani",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

def main():
    init_db()
    
    application = Application.builder().token(TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    # Har 5 sekundda tekshirish
    job_queue = application.job_queue
    job_queue.run_repeating(check_new_orders, interval=5, first=1)
    
    print("🤖 Bot ishga tushdi...")
    application.run_polling()

if __name__ == '__main__':
    main()