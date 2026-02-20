import psycopg2
import psycopg2.extras
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
from datetime import datetime
import os
from dotenv import load_dotenv
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
