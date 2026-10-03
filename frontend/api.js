// VyaparAI portal ↔ backend (FastAPI in app/main.py).
// Converts backend rows into the shapes the portal's views render, and streams live events (SSE).

// Same origin when served by the backend; config.js points a published copy (GitHub Pages) at the backend;
// Vite dev server (npm run dev → :5173) talks to :8000 directly.
export const API_BASE = window.VYAPAR_API_BASE
  || (location.port === '5173' || location.protocol === 'file:' ? 'http://localhost:8000' : '');

// Login token: the backend also sets a cookie, but a portal on another site (GitHub Pages) can't rely on
// cross-site cookies, so the token is kept here and sent as "Authorization: Bearer".
const TOKEN_KEY = 'vyapar_token';
export const saveToken = (t) => { try { if (t) localStorage.setItem(TOKEN_KEY, t); } catch {} };
export const clearToken = () => { try { localStorage.removeItem(TOKEN_KEY); } catch {} };
const readToken = () => { try { return localStorage.getItem(TOKEN_KEY); } catch { return null; } };

function authHeaders(extra = {}) {
  const token = readToken();
  return {
    'ngrok-skip-browser-warning': '1',          // free ngrok tunnels otherwise answer browsers with a warning page
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...extra,
  };
}

async function request(method, path, body) {
  const res = await fetch(API_BASE + path, {
    method,
    headers: authHeaders(body ? { 'Content-Type': 'application/json' } : {}),
    body: body ? JSON.stringify(body) : undefined,
    credentials: 'include',
  });
  if (res.status === 401 && !path.startsWith('/api/auth/')) {
    clearToken();
    location.href = './login.html';            // session expired / logged out → back to login
    throw new Error('Please log in');
  }
  if (!res.ok) {
    let detail = res.statusText, errors = null;
    try { const body = await res.json(); detail = body.detail || detail; errors = body.errors || null; } catch {}
    const err = new Error(detail);
    err.status = res.status;
    err.errors = errors;
    throw err;
  }
  return res.json();
}

export const api = {
  get: (p) => request('GET', p),
  post: (p, b) => request('POST', p, b || {}),
  put: (p, b) => request('PUT', p, b),
  patch: (p, b) => request('PATCH', p, b),
  del: (p) => request('DELETE', p),
};

// ---------------------------------------------------------------- dates

export const ymd = (d) => {
  const x = new Date(d);
  return `${x.getFullYear()}-${String(x.getMonth() + 1).padStart(2, '0')}-${String(x.getDate()).padStart(2, '0')}`;
};
export const todayStr = () => ymd(new Date());
export const addDays = (dateStr, n) => {
  const d = new Date(`${dateStr}T12:00:00`);
  d.setDate(d.getDate() + n);
  return ymd(d);
};
export const timeAgo = (ts) => {
  const s = (Date.now() - ts) / 1000;
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} hr ago`;
  return new Date(ts).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' });
};
const clock = (ts) => new Date(ts).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' });

// ---------------------------------------------------------------- formatting

export const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
export const rupees = (v) => `₹${Number(v || 0).toLocaleString('en-IN', { maximumFractionDigits: 2 })}`;

export function formatPhone(phone) {
  if (!phone) return '—';
  if (phone.startsWith('sim-')) return `Simulator · ${phone.split('-').pop()}`;
  const d = phone.replace(/\D/g, '');
  return d.length === 12 && d.startsWith('91') ? `+91 ${d.slice(2, 7)} ${d.slice(7)}` : `+${d}`;
}

/** WhatsApp text (*bold*, _italic_, links, newlines) → safe HTML */
export function waToHtml(text) {
  return esc(text)
    .replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>')
    .replace(/(^|[\s(>])\*([^*\n]+)\*/g, '$1<strong>$2</strong>')
    .replace(/(^|[\s(>])_([^_\n]+)_/g, '$1<em>$2</em>')
    .replace(/\n/g, '<br>');
}

export const initials = (str) => {
  const parts = (str || 'Store').trim().split(/\s+/);
  return (parts.length === 1 ? parts[0].slice(0, 2) : parts[0][0] + parts[1][0]).toUpperCase();
};

// ---------------------------------------------------------------- products

export const mapProduct = (p) => ({
  id: String(p.id),
  name: p.name,
  category: p.category,
  brand: p.brand || '',
  variant: p.variant || '',
  price: p.price,
  quantity: p.stock,
  isAvailable: !!p.active,
  description: p.description || '',
  keywords: p.keywords || '',
});

// ---------------------------------------------------------------- orders

// backend status ↔ portal workflow status
export const STATUS_TO_UI = {
  pending: 'NEW', accepted: 'ACCEPTED', preparing: 'PREPARING', packed: 'READY',
  out_for_delivery: 'OUT_FOR_DELIVERY', delivered: 'DELIVERED', rejected: 'REJECTED', cancelled: 'CANCELLED',
};
export const STATUS_TO_API = Object.fromEntries(Object.entries(STATUS_TO_UI).map(([k, v]) => [v, k]));

function paymentLabel(o) {
  if (o.payment_status === 'refund_due') return 'Refund due';
  if (o.payment_status === 'refunded') return 'Refunded';
  if (o.payment_method === 'UPI') return o.payment_status === 'paid' ? 'Paid via UPI' : 'UPI · awaiting payment';
  return o.payment_status === 'paid' ? 'Cash on Delivery · Paid' : 'Cash on Delivery';
}

export function mapOrder(o) {
  const ts = new Date(o.created_at).getTime();
  const address = [o.address, o.landmark && `near ${o.landmark}`].filter(Boolean).join(', ');
  return {
    id: String(o.id),
    code: o.code,
    orderNumber: o.code,
    date: (o.created_at || '').slice(0, 10),
    customerId: o.customer_id,
    customerName: o.customer_name || o.wa_name || 'WhatsApp customer',
    customerPhone: formatPhone(o.phone),
    rawPhone: o.phone,
    customerAddress: address || (o.latitude != null ? '📍 Shared location pin' : '—'),
    lat: o.latitude,
    lng: o.longitude,
    items: (o.items || []).map((i) => ({ productId: String(i.product_id), name: i.name, variant: i.variant, qty: i.quantity, price: i.price })),
    subtotal: o.subtotal,
    deliveryFee: o.delivery_fee,
    totalAmount: o.total,
    time: clock(ts),
    timestamp: ts,
    status: STATUS_TO_UI[o.status] || o.status,
    paymentMethod: o.payment_method,
    paymentState: o.payment_status,
    paymentStatus: paymentLabel(o),
    rejectReason: o.reject_reason,
    eta: o.eta,
    timeline: o.timeline || [],
  };
}

export function groupOrdersByDate(list) {
  const byDate = {};
  for (const o of list) (byDate[o.date] ||= []).push(o);
  return byDate;
}

// ---------------------------------------------------------------- notifications

export const mapNotification = (n) => {
  const ts = new Date(n.created_at).getTime();
  return {
    id: String(n.id), type: n.type, title: n.title, message: n.message,
    time: `${clock(ts)} · ${timeAgo(ts)}`, timestamp: ts, isRead: !!n.is_read,
    orderId: n.order_id ? String(n.order_id) : null, productId: n.product_id ? String(n.product_id) : null,
    customerId: n.customer_id,
  };
};

// ---------------------------------------------------------------- store profile ↔ settings

export const mapStore = (s) => ({
  name: s.store_name,
  businessType: s.business_type || 'Kirana / General Store',
  customBusinessType: s.custom_business_type || '',
  phone: s.store_phone || '',
  address: s.store_address || '',
  city: s.store_city || '',
  pincode: s.store_pincode || '',
  description: s.store_description || '',
  openingTime: s.opening_time || '07:00',
  closingTime: s.closing_time || '22:00',
  logoInitials: initials(s.store_name),
  logoBg: s.logo_bg || '#059669',
  logoUrl: s.logo_image || '',
  isOpen: s.is_open !== '0',
  closeReason: s.close_reason || '',
  closeCustomReason: '',
  coordinates: s.coordinates || '',
  deliveryEta: s.delivery_eta,
  deliveryFee: s.delivery_fee,
  freeDeliveryAbove: s.free_delivery_above,
  minOrder: s.min_order,
  upiId: s.upi_id,
  welcomeMessage: s.welcome_message || '',
});

export const storeToSettings = (st) => ({
  store_name: st.name,
  business_type: st.businessType,
  custom_business_type: st.customBusinessType || '',
  store_phone: st.phone,
  store_address: st.address,
  store_city: st.city,
  store_pincode: st.pincode,
  store_description: st.description,
  opening_time: st.openingTime,
  closing_time: st.closingTime,
  logo_bg: st.logoBg,
  coordinates: st.coordinates || '',
});

// ---------------------------------------------------------------- live events

// Server-Sent Events read with fetch (EventSource can't send the Authorization / ngrok headers).
export function connectEvents(onEvent, onStatus) {
  let stopped = false;
  let controller;
  const run = async () => {
    while (!stopped) {
      controller = new AbortController();
      try {
        const res = await fetch(API_BASE + '/api/events', {
          headers: authHeaders({ Accept: 'text/event-stream' }), credentials: 'include', signal: controller.signal,
        });
        if (res.status === 401) { clearToken(); location.href = './login.html'; return; }
        if (!res.ok || !res.body) throw new Error(`events ${res.status}`);
        onStatus?.(true);
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let cut;
          while ((cut = buffer.indexOf('\n\n')) >= 0) {
            const chunk = buffer.slice(0, cut);
            buffer = buffer.slice(cut + 2);
            const data = chunk.split('\n').filter((l) => l.startsWith('data:')).map((l) => l.slice(5).trimStart()).join('\n');
            if (!data) continue;    // comments / pings
            try {
              const msg = JSON.parse(data);
              onEvent(msg.event, msg.data);
            } catch (err) { console.error('bad event', err); }
          }
        }
      } catch (err) {
        if (stopped) return;
      }
      onStatus?.(false);
      await new Promise((r) => setTimeout(r, 3000));   // reconnect
    }
  };
  run();
  return () => { stopped = true; controller?.abort(); };
}
