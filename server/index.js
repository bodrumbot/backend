const express = require('express');
const { createServer } = require('http');
const { Server } = require('socket.io');
const { PrismaClient } = require('@prisma/client');
const cors = require('cors');
const path = require('path');

const prisma = new PrismaClient();
const app = express();
const httpServer = createServer(app);

// CORS sozlamalari
app.use(cors({
  origin: '*',
  methods: ['GET', 'POST', 'PUT', 'DELETE'],
  credentials: true
}));

app.use(express.json());

// Socket.io
const io = new Server(httpServer, {
  cors: {
    origin: '*',
    methods: ['GET', 'POST']
  }
});

// Static files (WebApp)
app.use(express.static(path.join(__dirname, '../webapp')));

// ===================== API ROUTES =====================

// Barcha buyurtmalarni olish
app.get('/api/orders', async (req, res) => {
  try {
    const orders = await prisma.order.findMany({
      where: {
        OR: [
          { paymentStatus: 'paid' },
          { status: 'accepted' },
          { status: 'rejected' },
          { status: 'payment_pending' }
        ]
      },
      orderBy: { createdAt: 'desc' }
    });
    res.json(orders);
  } catch (error) {
    console.error('Orders error:', error);
    res.status(500).json({ error: error.message });
  }
});

// Yangi buyurtmalarni olish (payment_pending + paid)
app.get('/api/orders/new', async (req, res) => {
  try {
    const orders = await prisma.order.findMany({
      where: {
        paymentStatus: 'paid',
        status: 'payment_pending'
      },
      orderBy: { createdAt: 'desc' }
    });
    res.json(orders);
  } catch (error) {
    res.status(500).json({ error: error.message });
  }
});

// Bitta buyurtma olish
app.get('/api/orders/:orderId', async (req, res) => {
  try {
    const order = await prisma.order.findUnique({
      where: { orderId: req.params.orderId }
    });
    if (!order) return res.status(404).json({ error: 'Not found' });
    res.json(order);
  } catch (error) {
    res.status(500).json({ error: error.message });
  }
});

// Yangi buyurtma yaratish
app.post('/api/orders', async (req, res) => {
  try {
    const order = await prisma.order.create({
      data: req.body
    });
    
    // Socket.io orqali adminlarga xabar
    io.emit('new_order', order);
    
    res.json(order);
  } catch (error) {
    console.error('Create order error:', error);
    res.status(500).json({ error: error.message });
  }
});

// Buyurtma yangilash
app.put('/api/orders/:orderId', async (req, res) => {
  try {
    const order = await prisma.order.update({
      where: { orderId: req.params.orderId },
      data: req.body
    });
    
    // Socket.io orqali yangilanish
    io.emit('order_updated', order);
    
    res.json(order);
  } catch (error) {
    res.status(500).json({ error: error.message });
  }
});

// ===================== PAYME CALLBACK =====================

app.post('/api/payme/callback', async (req, res) => {
  try {
    const { order_id, status } = req.body;
    
    console.log('💰 Payme callback:', order_id, status);
    
    if (status === 'success') {
      const order = await prisma.order.update({
        where: { orderId: order_id },
        data: {
          paymentStatus: 'paid',
          status: 'payment_pending',
          notified: false
        }
      });
      
      // Socket.io orqali xabar
      io.emit('payment_success', order);
      io.emit('new_order', order);
      
      res.json({ success: true, order });
    } else {
      await prisma.order.update({
        where: { orderId: order_id },
        data: {
          paymentStatus: 'failed',
          status: 'rejected'
        }
      });
      
      res.json({ success: true });
    }
  } catch (error) {
    console.error('Payme callback error:', error);
    res.status(500).json({ error: error.message });
  }
});

// ===================== SOCKET.IO =====================

io.on('connection', (socket) => {
  console.log('✅ Client connected:', socket.id);
  
  // Admin qo'shilganda yangi buyurtmalarni yuborish
  socket.on('admin_join', async () => {
    const newOrders = await prisma.order.findMany({
      where: {
        paymentStatus: 'paid',
        status: 'payment_pending'
      }
    });
    socket.emit('initial_orders', newOrders);
  });
  
  socket.on('disconnect', () => {
    console.log('❌ Client disconnected:', socket.id);
  });
});

// Health check
app.get('/health', (req, res) => {
  res.json({ status: 'ok', timestamp: new Date().toISOString() });
});

const PORT = process.env.PORT || 3000;
httpServer.listen(PORT, () => {
  console.log(`🚀 Server running on port ${PORT}`);
});
