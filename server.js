const express = require('express');
const { Pool } = require('pg');
const http = require('http');
const { Server } = require('socket.io');
const cors = require('cors');

const app = express();
const server = http.createServer(app);
const io = new Server(server, {
  cors: {
    origin: [
      'https://bodrumbot.github.io',
      'https://bodrumbot.github.io/111',
      'http://localhost:3000',
      'http://localhost:5000',
      'http://127.0.0.1:5500',
      '*'
    ],
    methods: ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    credentials: true,
    allowedHeaders: ["Content-Type", "Authorization", "Accept", "Origin", "X-Requested-With"]
  },
  allowEIO3: true,
  pingTimeout: 60000,
  pingInterval: 25000
});

// ==========================================
// CORS - BARCHA SO'ROVLAR UCHUN
// ==========================================
app.use((req, res, next) => {
  const allowedOrigins = [
    'https://bodrumbot.github.io',
    'https://bodrumbot.github.io/111',
    'http://localhost:3000',
    'http://localhost:5000',
    'http://127.0.0.1:5500'
  ];
  
  const origin = req.headers.origin;
  if (allowedOrigins.includes(origin) || !origin) {
    res.header('Access-Control-Allow-Origin', origin || '*');
  }
  
  res.header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS');
  res.header('Access-Control-Allow-Headers', 'Content-Type, Authorization, Accept, Origin, X-Requested-With');
  res.header('Access-Control-Allow-Credentials', 'true');
  res.header('Access-Control-Max-Age', '86400');
  
  if (req.method === 'OPTIONS') {
    return res.sendStatus(204);
  }
  next();
});

// Express CORS
app.use(cors({
  origin: function(origin, callback) {
    const allowedOrigins = [
      'https://bodrumbot.github.io',
      'https://bodrumbot.github.io/111',
      'http://localhost:3000',
      'http://localhost:5000',
      'http://127.0.0.1:5500'
    ];
    
    if (!origin || allowedOrigins.indexOf(origin) !== -1) {
      callback(null, true);
    } else {
      callback(new Error('CORS policy violation'));
    }
  },
  methods: ['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
  allowedHeaders: ['Content-Type', 'Authorization', 'Accept', 'Origin', 'X-Requested-With'],
  credentials: true,
  preflightContinue: false,
  optionsSuccessStatus: 204
}));

app.use(express.json());

// ==========================================
// PostgreSQL ulanish
// ==========================================
const pool = new Pool({
  connectionString: process.env.DATABASE_PUBLIC_URL || process.env.DATABASE_URL,
  ssl: { 
    rejectUnauthorized: false 
  }
});

let listenClient = null;

// ==========================================
// BAZA YARATISH VA TRIGGERLAR
// ==========================================
async function initDB() {
  const client = await pool.connect();
  try {
    await client.query(`
      CREATE TABLE IF NOT EXISTS orders (
        id SERIAL PRIMARY KEY,
        order_id VARCHAR(50) UNIQUE NOT NULL,
        name VARCHAR(100) NOT NULL,
        phone VARCHAR(20) NOT NULL,
        items JSONB NOT NULL DEFAULT '[]',
        total INTEGER NOT NULL DEFAULT 0,
        status VARCHAR(20) DEFAULT 'pending_payment',
        payment_status VARCHAR(20) DEFAULT 'pending',
        payment_method VARCHAR(20) DEFAULT 'payme',
        location VARCHAR(100),
        tg_id BIGINT,
        created_at TIMESTAMP DEFAULT NOW(),
        accepted_at TIMESTAMP,
        rejected_at TIMESTAMP,
        notified BOOLEAN DEFAULT FALSE
      );
      
      CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
      CREATE INDEX IF NOT EXISTS idx_orders_payment ON orders(payment_status);
      CREATE INDEX IF NOT EXISTS idx_orders_order_id ON orders(order_id);
      CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at DESC);
    `);

    await client.query(`
      CREATE OR REPLACE FUNCTION notify_order_change()
      RETURNS TRIGGER AS $$
      BEGIN
        IF TG_OP = 'INSERT' THEN
          PERFORM pg_notify('order_events', json_build_object(
            'operation', 'INSERT',
            'data', row_to_json(NEW)
          )::text);
          RETURN NEW;
        ELSIF TG_OP = 'UPDATE' THEN
          PERFORM pg_notify('order_events', json_build_object(
            'operation', 'UPDATE',
            'data', row_to_json(NEW),
            'old_data', row_to_json(OLD)
          )::text);
          RETURN NEW;
        ELSIF TG_OP = 'DELETE' THEN
          PERFORM pg_notify('order_events', json_build_object(
            'operation', 'DELETE',
            'data', row_to_json(OLD)
          )::text);
          RETURN OLD;
        END IF;
        RETURN NULL;
      END;
      $$ LANGUAGE plpgsql;
    `);

    await client.query(`
      DROP TRIGGER IF EXISTS orders_notify_trigger ON orders;
      CREATE TRIGGER orders_notify_trigger
        AFTER INSERT OR UPDATE OR DELETE ON orders
        FOR EACH ROW
        EXECUTE FUNCTION notify_order_change();
    `);

    console.log('✅ Baza va triggerlar tayyor');
  } catch (error) {
    console.error('❌ Baza yaratish xato:', error);
    throw error;
  } finally {
    client.release();
  }
}

// ==========================================
// REAL-TIME LISTEN/NOTIFY
// ==========================================
async function startPgListener() {
  if (listenClient) {
    try {
      await listenClient.end();
    } catch (e) {}
  }
  
  listenClient = new Pool({
    connectionString: process.env.DATABASE_PUBLIC_URL || process.env.DATABASE_URL,
    ssl: { rejectUnauthorized: false },
    max: 1
  });
  
  const client = await listenClient.connect();
  
  await client.query('LISTEN order_events');
  console.log('🔔 PostgreSQL LISTEN boshlandi');
  
  client.on('notification', async (msg) => {
    try {
      const payload = JSON.parse(msg.payload);
      console.log('📨 PostgreSQL notification:', payload.operation, payload.data?.order_id);
      
      io.emit('order_change', payload);
      io.to('admin_room').emit('admin_order_update', payload);
      
      if (payload.operation === 'INSERT' && payload.data?.status === 'pending_payment') {
        io.to('admin_room').emit('new_order_alert', payload.data);
      }
      
      if (payload.operation === 'UPDATE' && 
          payload.old_data?.payment_status !== payload.data?.payment_status &&
          payload.data?.payment_status === 'paid') {
        io.emit('payment_success', payload.data);
        io.to(`order_${payload.data.order_id}`).emit('payment_success', payload.data);
      }
    } catch (error) {
      console.error('Notification xato:', error);
    }
  });
  
  client.on('error', async (err) => {
    console.error('LISTEN connection xato:', err);
    setTimeout(startPgListener, 5000);
  });
  
  client.on('end', async () => {
    console.log('LISTEN connection uzildi, qayta ulanmoqda...');
    setTimeout(startPgListener, 5000);
  });
}

// ==========================================
// API ROUTES
// ==========================================

// Health check
app.get('/health', (req, res) => {
  res.json({ 
    status: 'ok', 
    timestamp: new Date().toISOString(),
    database: 'connected',
    cors: 'enabled'
  });
});

// Test CORS
app.get('/test-cors', (req, res) => {
  res.json({ 
    message: 'CORS is working!',
    origin: req.headers.origin,
    timestamp: new Date().toISOString()
  });
});

// Yangi buyurtma
app.post('/api/orders', async (req, res) => {
  const { order_id, name, phone, items, total, location, tg_id, payment_method = 'payme' } = req.body;
  
  console.log('📥 Yangi buyurtma:', order_id, name, total);
  
  try {
    const result = await pool.query(
      `INSERT INTO orders (order_id, name, phone, items, total, location, tg_id, status, payment_status, payment_method)
       VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending_payment', 'pending', $8)
       RETURNING *`,
      [order_id, name, phone, JSON.stringify(items), total, location, tg_id, payment_method]
    );
    
    console.log('✅ Buyurtma saqlandi:', result.rows[0].order_id);
    res.json({ success: true, order: result.rows[0] });
  } catch (error) {
    console.error('❌ Order yaratish xato:', error);
    res.status(500).json({ error: error.message });
  }
});

// Barcha buyurtmalar
app.get('/api/orders', async (req, res) => {
  try {
    const result = await pool.query('SELECT * FROM orders ORDER BY created_at DESC');
    res.json(result.rows);
  } catch (error) {
    console.error('❌ Orders olish xato:', error);
    res.status(500).json({ error: error.message });
  }
});

// Bitta buyurtma
app.get('/api/orders/:order_id', async (req, res) => {
  try {
    const result = await pool.query('SELECT * FROM orders WHERE order_id = $1', [req.params.order_id]);
    if (result.rows.length === 0) {
      return res.status(404).json({ error: 'Buyurtma topilmadi' });
    }
    res.json(result.rows[0]);
  } catch (error) {
    console.error('❌ Order olish xato:', error);
    res.status(500).json({ error: error.message });
  }
});

// Status yangilash
app.post('/api/orders/:order_id/status', async (req, res) => {
  const { status, payment_status } = req.body;
  
  console.log('📝 Status yangilash:', req.params.order_id, status, payment_status);
  
  try {
    let query = 'UPDATE orders SET ';
    const params = [];
    let paramCount = 1;
    
    if (status) {
      query += `status = $${paramCount}, `;
      params.push(status);
      paramCount++;
      
      if (status === 'accepted') {
        query += `accepted_at = NOW(), `;
      } else if (status === 'rejected') {
        query += `rejected_at = NOW(), `;
      }
    }
    
    if (payment_status) {
      query += `payment_status = $${paramCount}, `;
      params.push(payment_status);
      paramCount++;
    }
    
    query += `notified = FALSE WHERE order_id = $${paramCount} RETURNING *`;
    params.push(req.params.order_id);
    
    const result = await pool.query(query, params);
    
    if (result.rows.length === 0) {
      return res.status(404).json({ error: 'Buyurtma topilmadi' });
    }
    
    console.log('✅ Status yangilandi:', result.rows[0].order_id);
    res.json({ success: true, order: result.rows[0] });
  } catch (error) {
    console.error('❌ Status yangilash xato:', error);
    res.status(500).json({ error: error.message });
  }
});

// Payme callback
app.post('/api/payment/callback', async (req, res) => {
  const { order_id, status } = req.body;
  
  console.log('💰 Payme callback:', order_id, status);
  
  try {
    const result = await pool.query(
      `UPDATE orders 
       SET payment_status = $1, 
           status = CASE WHEN $1 = 'paid' THEN 'pending' ELSE status END,
           notified = FALSE
       WHERE order_id = $2 
       RETURNING *`,
      [status, order_id]
    );
    
    if (result.rows.length === 0) {
      return res.status(404).json({ error: 'Buyurtma topilmadi' });
    }
    
    console.log('✅ Payme callback muvaffaqiyatli');
    res.json({ success: true, order: result.rows[0] });
  } catch (error) {
    console.error('❌ Payme callback xato:', error);
    res.status(500).json({ error: error.message });
  }
});

// ==========================================
// WEBSOCKET
// ==========================================

io.on('connection', (socket) => {
  console.log('✅ Client connected:', socket.id);
  
  socket.on('join_admin', () => {
    socket.join('admin_room');
    console.log('👑 Admin joined:', socket.id);
  });
  
  socket.on('join_order', (orderId) => {
    socket.join(`order_${orderId}`);
    console.log('📦 Order room joined:', orderId);
  });
  
  socket.on('disconnect', (reason) => {
    console.log('❌ Client disconnected:', socket.id, reason);
  });
  
  socket.on('error', (error) => {
    console.error('Socket xato:', error);
  });
});

// ==========================================
// TELEGRAM BOT CHECK
// ==========================================

async function checkNewOrders() {
  try {
    const result = await pool.query(
      `SELECT * FROM orders 
       WHERE payment_status = 'paid' 
       AND status = 'pending' 
       AND (notified = FALSE OR notified IS NULL)`
    );
    
    for (const order of result.rows) {
      io.to('admin_room').emit('payment_paid', order);
      io.to(`order_${order.order_id}`).emit('payment_success', order);
      
      await pool.query('UPDATE orders SET notified = TRUE WHERE id = $1', [order.id]);
      console.log('✅ Yangi to\'langan buyurtma:', order.order_id);
    }
  } catch (error) {
    console.error('❌ Tekshirish xato:', error);
  }
}

setInterval(checkNewOrders, 5000);

// ==========================================
// SERVER START
// ==========================================

const PORT = process.env.PORT || 3000;

async function start() {
  try {
    await initDB();
    await startPgListener();
    
    server.listen(PORT, () => {
      console.log(`🚀 Server ${PORT} portda ishlamoqda`);
      console.log(`🌐 Frontend URL: https://bodrumbot.github.io/111`);
      console.log(`🔌 WebSocket enabled`);
      console.log(`📡 CORS enabled for: https://bodrumbot.github.io`);
    });
  } catch (error) {
    console.error('❌ Server start xato:', error);
    process.exit(1);
  }
}

start().catch(console.error);

process.on('SIGTERM', async () => {
  console.log('SIGTERM received, shutting down...');
  if (listenClient) await listenClient.end();
  await pool.end();
  server.close(() => {
    console.log('Server yopildi');
    process.exit(0);
  });
});
