// payme-callback.js - Payme dan to'lov xabari kelganda avtomatik qabul qilish
// Railway PostgreSQL bilan ishlaydi (Supabase emas)

const { Pool } = require('pg');

// Railway PostgreSQL ulanish
const pool = new Pool({
  connectionString: process.env.DATABASE_PUBLIC_URL || process.env.DATABASE_URL,
  ssl: {
    rejectUnauthorized: false // Railway uchun kerak
  }
});

module.exports = async (req, res) => {
  // CORS
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');

  if (req.method === 'OPTIONS') return res.status(200).end();
  if (req.method !== 'POST') return res.status(405).json({ error: 'Method not allowed' });

  const client = await pool.connect();

  try {
    const { order_id, status, transaction_id } = req.body;

    console.log('💰 Payme callback:', { order_id, status, transaction_id });

    if (status === 'success' || status === 'completed') {
      // 1. Buyurtma ma'lumotlarini olish
      const getOrderQuery = `
        SELECT * FROM orders 
        WHERE order_id = $1
      `;
      const orderResult = await client.query(getOrderQuery, [order_id]);

      if (orderResult.rows.length === 0) {
        throw new Error('Buyurtma topilmadi');
      }

      const order = orderResult.rows[0];

      // 2. ⭐ AVTOMATIK QABUL QILISH - skrinshot talab qilmaydi!
      const updateQuery = `
        UPDATE orders 
        SET 
          payment_status = 'paid',
          status = 'accepted',
          accepted_at = CURRENT_TIMESTAMP,
          paid_at = CURRENT_TIMESTAMP,
          transaction_id = $1,
          notified = false,
          auto_accepted = true
        WHERE order_id = $2
        RETURNING *
      `;

      const updateResult = await client.query(updateQuery, [transaction_id, order_id]);
      const updatedOrder = updateResult.rows[0];

      // 3. Admin ga avtomatik xabar yuborish
      await notifyAdminAutoAccepted(updatedOrder);

      // 4. Mijozga xabar yuborish
      await notifyCustomerAutoAccepted(updatedOrder);

      res.json({ 
        success: true, 
        message: 'To\'lov muvaffaqiyatli, buyurtma avtomatik qabul qilindi',
        order: updatedOrder
      });

    } else {
      // To'lov muvaffaqiyatsiz
      const rejectQuery = `
        UPDATE orders 
        SET 
          payment_status = 'failed',
          status = 'rejected',
          rejected_at = CURRENT_TIMESTAMP
        WHERE order_id = $1
      `;
      await client.query(rejectQuery, [order_id]);

      res.json({ success: true, message: 'To\'lov muvaffaqiyatsiz' });
    }

  } catch (error) {
    console.error('❌ Payme callback xatosi:', error);
    res.status(500).json({ error: error.message });
  } finally {
    client.release();
  }
};

// Admin ga avtomatik qabul qilingan buyurtma haqida xabar
async function notifyAdminAutoAccepted(order) {
  const botToken = process.env.TOKEN; // app.py dagi TOKEN
  const adminChatId = process.env.ADMIN_CHAT_ID;

  if (!botToken || !adminChatId) {
    console.log('⚠️ Bot token yoki admin ID topilmadi');
    return;
  }

  let items = order.items;
  if (typeof items === 'string') {
    try {
      items = JSON.parse(items);
    } catch (e) {
      items = [];
    }
  }

  const itemsText = items.map(i => `• ${i.name} x${i.qty}`).join('\n');

  // Telefon raqamini formatlash
  let phone = order.phone || '';
  if (phone.startsWith('998')) phone = phone.substring(3);
  phone = phone.slice(-9);
  const phoneDisplay = `+998 ${phone.slice(0, 2)} ${phone.slice(2, 5)} ${phone.slice(5, 7)} ${phone.slice(7)}`;

  // Joylashuv
  let locationText = '';
  if (order.location) {
    if (order.location.includes(',')) {
      const [lat, lng] = order.location.split(',');
      locationText = `\n📍 <b>Joylashuv:</b> <a href='https://maps.google.com/?q=${lat.trim()},${lng.trim()}'>Xaritada ko\'rish</a>`;
    } else {
      locationText = `\n📍 <b>Manzil:</b> ${order.location}`;
    }
  }

  const message = `⚡ <b>AVTOMATIK QABUL QILINDI!</b>

🆔 Buyurtma: #${order.order_id.slice(-6)}
👤 Mijoz: ${order.name}
📞 Telefon: ${phoneDisplay}
💵 Summa: ${parseInt(order.total).toLocaleString()} so\'m
💳 To\'lov: Payme ✅ (Avtomatik)${locationText}

🍽 Mahsulotlar:
${itemsText}

⏰ ${new Date().toLocaleTimeString('uz-UZ')}

<i>✅ To\'lov muvaffaqiyatli - buyurtma avtomatik qabul qilindi</i>`;

  try {
    await fetch(`https://api.telegram.org/bot${botToken}/sendMessage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        chat_id: adminChatId,
        text: message,
        parse_mode: 'HTML'
      })
    });

    // Joylashuvni alohida yuborish
    if (order.location && order.location.includes(',')) {
      const [lat, lng] = order.location.split(',');
      await fetch(`https://api.telegram.org/bot${botToken}/sendLocation`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          chat_id: adminChatId,
          latitude: parseFloat(lat.trim()),
          longitude: parseFloat(lng.trim())
        })
      });
    }

    console.log('✅ Admin ga xabar yuborildi');
  } catch (error) {
    console.error('❌ Admin ga xabar yuborish xatosi:', error);
  }
}

// Mijozga avtomatik qabul qilingan xabar yuborish
async function notifyCustomerAutoAccepted(order) {
  const botToken = process.env.TOKEN;

  if (!botToken || !order.tg_id) {
    console.log('⚠️ Bot token yoki mijoz tg_id topilmadi');
    return;
  }

  const message = `✅ <b>Buyurtmangiz qabul qilindi!</b>

🆔 Buyurtma: #${order.order_id.slice(-6)}
💵 Summa: ${parseInt(order.total).toLocaleString()} so\'m
💳 To\'lov: Payme ✅

Buyurtmangiz muvaffaqiyatli to\'landi va avtomatik qabul qilindi!

Tez orada yetkazib beramiz! 🚀

⏰ ${new Date().toLocaleTimeString('uz-UZ')}`;

  try {
    await fetch(`https://api.telegram.org/bot${botToken}/sendMessage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        chat_id: order.tg_id,
        text: message,
        parse_mode: 'HTML'
      })
    });

    console.log('✅ Mijoz ga xabar yuborildi:', order.tg_id);
  } catch (error) {
    console.error('❌ Mijoz ga xabar yuborish xatosi:', error);
  }
}