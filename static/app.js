// Store console: orders, chats, inventory, simulator, settings. Live via Server-Sent Events.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const rupee = (v) => "₹" + Number(v || 0).toLocaleString("en-IN", { maximumFractionDigits: 2 });
const STATUS_LABEL = { pending: "New", accepted: "Accepted", packed: "Packed", out_for_delivery: "Out for delivery", delivered: "Delivered", rejected: "Rejected", cancelled: "Cancelled" };

const state = { view: "orders", orderFilter: "active", orders: [], customers: [], products: [], selectedCustomer: null, freshOrders: new Set() };

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined });
  if (!res.ok) { const e = await res.json().catch(() => ({})); throw new Error(e.detail || res.statusText); }
  return res.json();
}

function toast(text) {
  const t = document.createElement("div");
  t.className = "toast"; t.textContent = text;
  $("#toasts").append(t);
  setTimeout(() => t.remove(), 5000);
}

function beep() {
  try {
    const ctx = new AudioContext(), o = ctx.createOscillator(), g = ctx.createGain();
    o.connect(g); g.connect(ctx.destination); o.frequency.value = 880; g.gain.value = 0.08;
    o.start(); o.frequency.setValueAtTime(1175, ctx.currentTime + 0.12); o.stop(ctx.currentTime + 0.25);
  } catch {}
}

function ago(iso) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short" });
}
const hhmm = (iso) => new Date(iso).toLocaleTimeString("en-IN", { hour: "numeric", minute: "2-digit" });

// WhatsApp-style formatting for message bubbles (escape first, then *bold* / _italic_ / links)
function waFormat(text) {
  return esc(text)
    .replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>')
    .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<b>$2</b>")
    .replace(/(^|[\s(])_([^_\n]+)_/g, "$1<i>$2</i>");
}

// ---------------- navigation ----------------
function show(view) {
  state.view = view;
  $$("nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === view));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + view));
  if (view === "orders") loadOrders();
  if (view === "chats") loadCustomers();
  if (view === "inventory") loadProducts();
  if (view === "settings") loadSettings();
  if (view === "simulator") loadSim();
}
$$("nav a").forEach((a) => a.addEventListener("click", () => show(a.dataset.view)));

// ---------------- stats ----------------
async function loadStats() {
  const s = await api("/api/stats");
  $("#stats").innerHTML = [
    ["New orders", s.pending], ["In progress", s.in_progress], ["Orders today", s.orders_today],
    ["Revenue today", rupee(s.revenue_today)], ["Customers", s.customers],
  ].map(([k, v]) => `<div class="card stat"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");
  $("#navPending").textContent = s.pending || "";
  $("#navAttention").textContent = s.attention || "";
  $("#navLow").textContent = s.low_stock || "";
}

// ---------------- orders ----------------
const FILTERS = {
  active: (o) => ["pending", "accepted", "packed", "out_for_delivery"].includes(o.status),
  closed: (o) => ["rejected", "cancelled"].includes(o.status),
  all: () => true,
};

async function loadOrders() {
  state.orders = await api("/api/orders?limit=200");
  renderOrders();
  loadStats();
}

function orderActions(o) {
  const b = [];
  if (o.status === "pending") {
    b.push(`<button class="btn green sm" data-act="accepted">Accept</button>`, `<button class="btn red sm" data-act="rejected">Reject</button>`);
  } else if (o.status === "accepted") {
    b.push(`<button class="btn primary sm" data-act="packed">Mark packed</button>`, `<button class="btn sm" data-act="out_for_delivery">Out for delivery</button>`, `<button class="btn red sm" data-act="cancelled">Cancel</button>`);
  } else if (o.status === "packed") {
    b.push(`<button class="btn primary sm" data-act="out_for_delivery">Out for delivery</button>`, `<button class="btn red sm" data-act="cancelled">Cancel</button>`);
  } else if (o.status === "out_for_delivery") {
    b.push(`<button class="btn green sm" data-act="delivered">Mark delivered</button>`);
  }
  if (o.payment_status === "pending" && !["rejected", "cancelled"].includes(o.status)) b.push(`<button class="btn sm" data-pay="paid">Mark paid</button>`);
  if (o.payment_status === "refund_due") b.push(`<button class="btn sm" data-pay="refunded">Mark refunded</button>`);
  b.push(`<button class="btn sm" data-chat="${o.customer_id}">💬 Chat</button>`);
  return b.join("");
}

function renderOrders() {
  const f = state.orderFilter;
  const list = state.orders.filter(FILTERS[f] || ((o) => o.status === f));
  if (!list.length) { $("#orders").innerHTML = `<div class="muted">No orders here yet. Place one from the Simulator or WhatsApp.</div>`; return; }
  $("#orders").innerHTML = list.map((o) => {
    const map = o.latitude != null ? ` · <a target="_blank" href="https://maps.google.com/?q=${o.latitude},${o.longitude}">map ↗</a>` : "";
    return `<div class="card order ${state.freshOrders.has(o.id) ? "new" : ""}" data-id="${o.id}">
      <div class="order-top"><div><span class="code">${esc(o.code)}</span> <span class="muted small">· ${ago(o.created_at)}</span></div>
        <span class="pill ${o.status}">${STATUS_LABEL[o.status] || o.status}</span></div>
      <div><div class="who"><b>${esc(o.customer_name || o.wa_name || "Customer")}</b><span class="muted">${esc(o.phone)}</span></div>
        <div class="addr">📍 ${esc(o.address || "Location pin")}${o.landmark ? " · " + esc(o.landmark) : ""}${map}</div></div>
      <div class="items">${o.items.map((i) => `<div><span>${i.quantity} × ${esc(i.name)} <span class="muted">${esc(i.variant || "")}</span></span><span>${rupee(i.line_total)}</span></div>`).join("")}
        ${o.delivery_fee ? `<div class="muted"><span>Delivery</span><span>${rupee(o.delivery_fee)}</span></div>` : ""}</div>
      <div class="totals"><span class="t">${rupee(o.total)}</span>
        <span><span class="pill">${o.payment_method}</span> <span class="pill ${o.payment_status}">${o.payment_status.replace("_", " ")}</span></span></div>
      ${o.reject_reason ? `<div class="note">${esc(o.reject_reason)}</div>` : ""}
      <div class="actions">${orderActions(o)}</div>
    </div>`;
  }).join("");
}

$("#orderFilters").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  state.orderFilter = b.dataset.f;
  $$("#orderFilters button").forEach((x) => x.classList.toggle("on", x === b));
  renderOrders();
});

$("#orders").addEventListener("click", async (e) => {
  const card = e.target.closest(".order"); if (!card) return;
  const id = Number(card.dataset.id);
  const btn = e.target.closest("button"); if (!btn) return;
  state.freshOrders.delete(id);
  try {
    if (btn.dataset.act) {
      const status = btn.dataset.act; let note = null, eta = null;
      if (status === "rejected" || status === "cancelled") {
        note = prompt(status === "rejected" ? "Reason for rejecting (sent to customer):" : "Reason for cancelling:", "Some items are unavailable right now");
        if (note === null) return;
      }
      if (status === "accepted") {
        eta = prompt("Delivery ETA (sent to customer):", "30-45 mins");
        if (eta === null) return;
      }
      btn.disabled = true;
      await api(`/api/orders/${id}/status`, { method: "POST", body: { status, note, eta } });
    } else if (btn.dataset.pay) {
      await api(`/api/orders/${id}/payment`, { method: "POST", body: { status: btn.dataset.pay } });
    } else if (btn.dataset.chat) {
      state.selectedCustomer = Number(btn.dataset.chat); show("chats"); return;
    }
    loadOrders();
  } catch (err) { toast("⚠️ " + err.message); btn.disabled = false; }
});

// ---------------- chats ----------------
async function loadCustomers() {
  state.customers = await api("/api/customers");
  renderCustomers();
  if (state.selectedCustomer) openChat(state.selectedCustomer);
}

function renderCustomers() {
  const q = $("#chatSearch").value.toLowerCase();
  const list = state.customers.filter((c) => !q || `${c.name} ${c.wa_name} ${c.phone}`.toLowerCase().includes(q));
  $("#customers").innerHTML = list.map((c) => `
    <div class="cust ${c.id === state.selectedCustomer ? "sel" : ""}" data-id="${c.id}">
      <div class="n"><span>${esc(c.name || c.wa_name || c.phone)}
        ${c.needs_attention ? '<span class="tag warn">needs reply</span>' : ""}${c.bot_paused ? '<span class="tag off">AI off</span>' : ""}${c.channel === "sim" ? '<span class="tag">sim</span>' : ""}</span>
        <small>${ago(c.last_message_at)}</small></div>
      <div class="m">${esc((c.last_message || "").replace(/[*_]/g, ""))}</div>
    </div>`).join("") || `<div class="empty">No conversations yet</div>`;
}
$("#chatSearch").addEventListener("input", renderCustomers);
$("#customers").addEventListener("click", (e) => {
  const el = e.target.closest(".cust"); if (el) openChat(Number(el.dataset.id));
});

function bubble(m, { sim = false } = {}) {
  if (m.kind === "alert") return `<div class="bubble alert">${esc(m.body)}</div>`;
  const out = sim ? m.direction === "in" : m.direction === "out";   // in the simulator the customer is "me"
  const cls = ["bubble", out ? "out" : "", !sim && m.sender === "store" ? "store" : "", !sim && m.sender === "system" ? "system" : ""].join(" ");
  const who = !sim && m.direction === "out" ? { agent: "🤖 Bot", store: "🧑 Store", system: "🔔 Update" }[m.sender] + " · " : "";
  let meta = null;
  try { meta = typeof m.meta === "string" ? JSON.parse(m.meta) : m.meta; } catch {}
  let opts = "";
  if (meta?.type === "checklist") {   // WhatsApp Flow with checkboxes (multi-select)
    const rows = meta.options.map((o) => `<label class="chk"><input type="checkbox" value="${esc(o.id)}" ${sim ? "" : "disabled"}>
      <span>${esc(o.title)}${o.description ? `<small>${esc(o.description)}</small>` : ""}</span></label>`).join("");
    opts = `<div class="btns checklist"><div class="list-label">☑️ ${esc(meta.button)}</div>${rows}
      <button class="submit" data-multi ${sim ? "" : "disabled"}>Add to cart</button></div>`;
  } else if (meta?.options?.length) {
    const rows = meta.options.map((o) => `<button data-id="${esc(o.id)}" data-title="${esc(o.title)}" ${sim ? "" : "disabled"}>
      <span>${esc(o.title)}</span>${o.description ? `<small>${esc(o.description)}</small>` : ""}</button>`).join("");
    opts = `<div class="btns ${meta.type}">${meta.type === "list" ? `<div class="list-label">☰ ${esc(meta.button)}</div>` : ""}${rows}</div>`;
  }
  return `<div class="${cls}">${waFormat(m.body)}${opts}<span class="meta">${who}${hhmm(m.created_at)}</span></div>`;
}

async function openChat(id) {
  state.selectedCustomer = id;
  renderCustomers();
  const [c, msgs] = await Promise.all([api(`/api/customers/${id}`), api(`/api/customers/${id}/messages`)]);
  const cart = c.cart.items.length ? `🛒 Cart: ${c.cart.items.map((i) => `${i.quantity}× ${esc(i.item)}`).join(", ")} — ${rupee(c.cart.total)}` : "🛒 Cart empty";
  $("#chatPane").innerHTML = `
    <div class="chat-head">
      <div><div class="t">${esc(c.name || c.wa_name || c.phone)}</div>
        <div class="s">${esc(c.phone)} · ${esc(c.address || "no address yet")} · ${c.orders.length} orders${c.language ? " · " + esc(c.language) : ""}</div></div>
      <div class="row">
        ${c.needs_attention ? '<button class="btn sm" id="resolveBtn">✓ Mark resolved</button>' : ""}
        <label class="row small muted">AI agent <span class="switch"><input type="checkbox" id="botToggle" ${c.bot_paused ? "" : "checked"}><span></span></span></label>
      </div>
    </div>
    <div class="cart-mini">${cart}</div>
    <div class="thread" id="thread">${msgs.map((m) => bubble(m)).join("")}</div>
    <form class="chat-input" id="replyForm"><input id="replyText" placeholder="Reply as store staff…" autocomplete="off"><button class="btn primary">Send</button></form>`;
  const th = $("#thread"); th.scrollTop = th.scrollHeight;
  $("#botToggle").onchange = async (e) => {
    await api(`/api/customers/${id}/bot`, { method: "POST", body: { paused: !e.target.checked } });
    toast(e.target.checked ? "AI agent resumed for this chat" : "You took over this chat — AI paused");
    loadCustomers();
  };
  if ($("#resolveBtn")) $("#resolveBtn").onclick = async () => { await api(`/api/customers/${id}/resolve`, { method: "POST" }); loadCustomers(); loadStats(); };
  $("#replyForm").onsubmit = async (e) => {
    e.preventDefault();
    const text = $("#replyText").value.trim(); if (!text) return;
    $("#replyText").value = "";
    await api(`/api/customers/${id}/messages`, { method: "POST", body: { text } });
  };
}

// ---------------- inventory ----------------
async function loadProducts() {
  state.products = await api("/api/products");
  $("#catList").innerHTML = [...new Set(state.products.map((p) => p.category))].map((c) => `<option value="${esc(c)}">`).join("");
  renderProducts();
}

function renderProducts() {
  const q = $("#invSearch").value.toLowerCase();
  const list = state.products.filter((p) => !q || `${p.name} ${p.brand} ${p.category} ${p.keywords}`.toLowerCase().includes(q));
  $("#inventory").innerHTML = list.map((p) => `
    <tr data-id="${p.id}" class="${!p.active ? "inactive" : p.stock <= 0 ? "out" : p.stock <= 5 ? "low" : ""}">
      <td><div>${esc(p.name)}</div><div class="b">${esc(p.brand || "")}</div></td>
      <td>${esc(p.variant || "")}</td><td>${esc(p.category)}</td>
      <td><input type="number" step="0.5" value="${p.price}" data-field="price"></td>
      <td><div class="stock"><button data-d="-1">−</button><input type="number" value="${p.stock}" data-field="stock"><button data-d="1">+</button>
        ${p.stock <= 0 ? '<span class="pill rejected">out</span>' : p.stock <= 5 ? '<span class="pill out_for_delivery">low</span>' : ""}</div></td>
      <td><label class="switch"><input type="checkbox" data-field="active" ${p.active ? "checked" : ""}><span></span></label></td>
    </tr>`).join("");
}
$("#invSearch").addEventListener("input", renderProducts);

async function patchProduct(id, body) {
  const p = await api(`/api/products/${id}`, { method: "PATCH", body });
  Object.assign(state.products.find((x) => x.id === id), p);
}
$("#inventory").addEventListener("change", async (e) => {
  const tr = e.target.closest("tr"), f = e.target.dataset.field; if (!tr || !f) return;
  const id = Number(tr.dataset.id);
  const value = f === "active" ? e.target.checked : Number(e.target.value);
  await patchProduct(id, { [f]: value }); renderProducts(); loadStats(); toast("Saved");
});
$("#inventory").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-d]"); if (!b) return;
  const tr = b.closest("tr"), id = Number(tr.dataset.id);
  const p = state.products.find((x) => x.id === id);
  await patchProduct(id, { stock: Math.max(0, p.stock + Number(b.dataset.d)) }); renderProducts(); loadStats();
});
$("#addProductBtn").onclick = () => $("#addProduct").classList.toggle("hidden");
$("#addProduct").onsubmit = async (e) => {
  e.preventDefault();
  const body = Object.fromEntries(new FormData(e.target));
  body.price = Number(body.price); body.stock = Number(body.stock || 0);
  try { await api("/api/products", { method: "POST", body }); e.target.reset(); e.target.classList.add("hidden"); loadProducts(); toast("Product added"); }
  catch (err) { toast("⚠️ " + err.message); }
};

// ---------------- simulator ----------------
const SUGGESTIONS = [
  "Hi",
  "milk",
  "doodh aur bread",
  "naaku biyyam kavali",
  "Flat 302, Sai Residency, Maisammaguda, near MRCET",
  "cart",
  "my orders",
  "menu",
];

async function loadSim() {
  $("#simSuggest").innerHTML = SUGGESTIONS.map((s) => `<button>${esc(s)}</button>`).join("");
  await refreshSim();
}

async function refreshSim() {
  const phone = $("#simPhone").value.trim();
  await (state.customers.length ? Promise.resolve() : api("/api/customers").then((c) => (state.customers = c)));
  const c = state.customers.find((x) => x.phone === phone);
  if (!c) { $("#simMessages").innerHTML = `<div class="bubble alert">Say hi to start a new customer conversation</div>`; return; }
  const msgs = await api(`/api/customers/${c.id}/messages`);
  $("#simMessages").innerHTML = msgs.filter((m) => m.kind !== "alert").map((m) => bubble(m, { sim: true })).join("");
  $("#simMessages").scrollTop = 1e9;
}

async function simSend(payload) {
  const phone = $("#simPhone").value.trim(), name = $("#simName").value.trim();
  $("#simTyping").classList.remove("hidden");
  await api("/api/sim/message", { method: "POST", body: { phone, name, ...payload } });
}
$("#simForm").onsubmit = (e) => {
  e.preventDefault();
  const text = $("#simText").value.trim(); if (!text) return;
  $("#simText").value = ""; simSend({ text });
};
$("#simSuggest").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) simSend({ text: b.textContent }); });
$("#simMessages").addEventListener("click", (e) => {
  const multi = e.target.closest("button[data-multi]");
  if (multi) {
    const ids = [...multi.parentElement.querySelectorAll("input:checked")].map((i) => i.value);
    if (!ids.length) { toast("Tick at least one item"); return; }
    multi.disabled = true;
    simSend({ text: `🛒 Selected ${ids.length} item(s)`, reply_id: "multi:" + ids.join(",") });
    return;
  }
  const b = e.target.closest("button[data-id]");
  if (b) simSend({ text: b.dataset.title, reply_id: b.dataset.id });
});
$("#simLoc").onclick = () => simSend({ latitude: 17.5946, longitude: 78.4412, address: "MRCET Road, Maisammaguda, Dulapally, Hyderabad" });
$("#simPhone").addEventListener("change", () => { state.customers = []; refreshSim(); });

// ---------------- settings ----------------
const SETTING_LABELS = {
  store_name: "Store name", store_address: "Store address", store_hours: "Opening hours", store_phone: "Store phone",
  delivery_eta: "Default delivery ETA", delivery_fee: "Delivery fee (₹)", free_delivery_above: "Free delivery above (₹)",
  min_order: "Minimum order (₹)", upi_id: "UPI ID for payments",
};
async function loadSettings() {
  const s = await api("/api/settings");
  $("#settingsForm").innerHTML = Object.entries(SETTING_LABELS).map(([k, l]) =>
    `<label>${l}<input name="${k}" value="${esc(s[k] ?? "")}"></label>`).join("") +
    `<div class="full"><button class="btn primary">Save settings</button></div>`;
}
$("#settingsForm").onsubmit = async (e) => {
  e.preventDefault();
  const s = await api("/api/settings", { method: "PUT", body: Object.fromEntries(new FormData(e.target)) });
  applyStoreName(s); toast("Settings saved");
};
function applyStoreName(s) { $("#storeName").textContent = s.store_name; $("#simStore").textContent = s.store_name; document.title = s.store_name + " · Console"; }

// ---------------- live events ----------------
let refreshTimer;
const debounced = (fn) => { clearTimeout(refreshTimer); refreshTimer = setTimeout(fn, 250); };

function connect() {
  const es = new EventSource("/api/events");
  es.onopen = () => { $("#liveDot").classList.add("on"); $("#liveText").textContent = "Live"; };
  es.onerror = () => { $("#liveDot").classList.remove("on"); $("#liveText").textContent = "Reconnecting…"; };
  es.onmessage = (e) => {
    const { event, data } = JSON.parse(e.data);
    if (event === "order_new") {
      state.freshOrders.add(data.id); beep();
      toast(`🛎️ New order ${data.code} · ${rupee(data.total)} from ${data.customer_name || data.phone}`);
      loadOrders();
    } else if (event === "order_updated") {
      if (state.view === "orders") loadOrders(); else loadStats();
    } else if (event === "inventory") {
      if (state.view === "inventory") loadProducts(); loadStats();
    } else if (event === "attention") {
      beep(); toast(`⚠️ ${data.phone} needs a human: ${data.message}`); loadStats();
      if (state.view === "chats") loadCustomers();
    } else if (event === "message") {
      if (state.view === "chats") {
        if (data.customer_id === state.selectedCustomer) {
          $("#thread")?.insertAdjacentHTML("beforeend", bubble(data));
          const th = $("#thread"); if (th) th.scrollTop = th.scrollHeight;
        }
        debounced(async () => { state.customers = await api("/api/customers"); renderCustomers(); });
      }
      if (state.view === "simulator" && data.phone === $("#simPhone").value.trim()) {
        if (data.direction === "out") $("#simTyping").classList.add("hidden");
        if (data.kind !== "alert") {
          $("#simMessages").querySelector(".bubble.alert")?.remove();
          $("#simMessages").insertAdjacentHTML("beforeend", bubble(data, { sim: true }));
          $("#simMessages").scrollTop = 1e9;
        }
      }
    } else if (event === "customer") {
      if (state.view === "chats") debounced(loadCustomers);
    }
  };
}

api("/api/settings").then(applyStoreName);
loadOrders();
connect();
setInterval(() => state.view === "orders" && renderOrders(), 60000);   // refresh "x min ago"
