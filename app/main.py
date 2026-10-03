import asyncio
import html
import json
import logging
import re

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agent, auth, config, db, events, flow, llm, messaging, notify, store, tenancy, whatsapp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

app = FastAPI(title="WhatsApp Order Agent for Local Stores")
db.init()
app.add_middleware(auth.ShopSessionMiddleware)   # selects the logged-in user's shop for every request
# the portal can also run on Vite's dev server (npm run dev → :5173) and call this API cross-origin
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# ---------------- auth: every /api route below works on the logged-in user's shop only ----------------

def dashboard_auth():
    if not auth.current_user():
        raise HTTPException(401, "Please log in")


def _secure(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


async def _me_payload() -> dict:
    user = auth.current_user()
    shop = db.get_shop()
    return {"user": {k: user[k] for k in ("id", "name", "email", "phone", "role")},
            "shop": {k: shop[k] for k in ("id", "name", "slug", "code_prefix")} |
                    {"has_own_number": bool(shop["wa_phone_number_id"] and shop["wa_access_token"]),
                     "wa_phone_number_id": shop["wa_phone_number_id"] or ""},
            "join": await tenancy.join_info(shop)}


class RegisterBody(BaseModel):
    shop_name: str
    business_type: str = "Kirana / General Store"
    owner_name: str
    email: str
    phone: str = ""
    password: str
    city: str = ""
    pincode: str = ""
    address: str = ""


class LoginBody(BaseModel):
    email: str
    password: str


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@app.post("/api/auth/register")
async def register(body: RegisterBody, request: Request):
    email = body.email.strip().lower()
    errors = {}
    if len(body.shop_name.strip()) < 2:
        errors["shop_name"] = "Enter your shop's name"
    if len(body.owner_name.strip()) < 2:
        errors["owner_name"] = "Enter your name"
    if not EMAIL_RE.match(email):
        errors["email"] = "Enter a valid email"
    elif db.one("SELECT 1 FROM users WHERE email=?", (email,)):
        errors["email"] = "An account with this email already exists — log in instead"
    if len(body.password) < 8:
        errors["password"] = "Use at least 8 characters"
    if body.pincode and not re.fullmatch(r"\d{6}", body.pincode.strip()):
        errors["pincode"] = "PIN code must be 6 digits"
    if errors:
        return JSONResponse({"detail": "Please fix the highlighted fields", "errors": errors}, status_code=400)

    shop = db.create_shop(body.shop_name, {
        "business_type": body.business_type, "store_city": body.city.strip(), "store_pincode": body.pincode.strip(),
        "store_address": body.address.strip(), "store_phone": body.phone.strip(),
    })
    cur = db.run("INSERT INTO users(shop_id, name, email, phone, password_hash, role, created_at) VALUES(?,?,?,?,?, 'owner', ?)",
                 (shop["id"], body.owner_name.strip(), email, body.phone.strip(), auth.hash_password(body.password), db.now()))
    db.use_shop(shop["id"])
    notify.add("store_status", f"🎉 Welcome to VyaparAI, {shop['name']}!",
               "Next: add your products in Items / Inventory — the WhatsApp bot shows them to customers as soon as they're "
               f"in stock. Customers order by sending “join {shop['slug']}” on WhatsApp.")
    token = auth.create_session(cur.lastrowid)
    auth._current_user.set(auth.user_for_token(token))
    resp = JSONResponse(await _me_payload())
    auth.set_cookie(resp, token, _secure(request))
    return resp


@app.post("/api/auth/login")
async def login(body: LoginBody, request: Request):
    user = db.one("SELECT u.* FROM users u JOIN shops s ON s.id=u.shop_id AND s.active=1 WHERE u.email=?",
                  (body.email.strip().lower(),))
    if not user or not auth.verify_password(body.password, user["password_hash"]):
        await asyncio.sleep(0.4)   # slow down password guessing a little
        raise HTTPException(401, "Wrong email or password")
    token = auth.create_session(user["id"])
    auth._current_user.set(auth.user_for_token(token))
    db.use_shop(user["shop_id"])
    resp = JSONResponse(await _me_payload())
    auth.set_cookie(resp, token, _secure(request))
    return resp


@app.post("/api/auth/logout")
async def logout(request: Request):
    auth.end_session(request.scope.get("state", {}).get("session_token"))
    resp = JSONResponse({"ok": True})
    auth.clear_cookie(resp)
    return resp


@app.get("/api/auth/me", dependencies=[Depends(dashboard_auth)])
async def me():
    return await _me_payload()


class PasswordBody(BaseModel):
    current_password: str
    new_password: str


@app.put("/api/auth/password", dependencies=[Depends(dashboard_auth)])
async def change_password(body: PasswordBody, request: Request):
    user = db.one("SELECT * FROM users WHERE id=?", (auth.current_user()["id"],))
    if not auth.verify_password(body.current_password, user["password_hash"]):
        raise HTTPException(400, "Current password is wrong")
    if len(body.new_password) < 8:
        raise HTTPException(400, "New password needs at least 8 characters")
    db.run("UPDATE users SET password_hash=? WHERE id=?", (auth.hash_password(body.new_password), user["id"]))
    # sign out other devices, keep this one
    db.run("DELETE FROM sessions WHERE user_id=? AND token<>?", (user["id"], request.scope.get("state", {}).get("session_token")))
    return {"ok": True}


class AccountBody(BaseModel):
    name: str | None = None
    phone: str | None = None


@app.put("/api/auth/account", dependencies=[Depends(dashboard_auth)])
async def update_account(body: AccountBody):
    uid = auth.current_user()["id"]
    if body.name and body.name.strip():
        db.run("UPDATE users SET name=? WHERE id=?", (body.name.strip(), uid))
    if body.phone is not None:
        db.run("UPDATE users SET phone=? WHERE id=?", (body.phone.strip(), uid))
    auth._current_user.set({**auth.current_user(), **db.one("SELECT name, phone FROM users WHERE id=?", (uid,))})
    return await _me_payload()


class OwnNumberBody(BaseModel):
    phone_number_id: str = ""
    access_token: str = ""


@app.put("/api/shop/whatsapp", dependencies=[Depends(dashboard_auth)])
async def set_own_number(body: OwnNumberBody):
    """Optional: connect this shop's own WhatsApp Business number (otherwise it uses the shared number)."""
    pid, tok = body.phone_number_id.strip(), body.access_token.strip()
    if pid and db.one("SELECT 1 FROM shops WHERE wa_phone_number_id=? AND id<>?", (pid, db.shop_id())):
        raise HTTPException(400, "That WhatsApp number is already connected to another shop")
    if pid and tok:
        db.run("UPDATE shops SET wa_phone_number_id=?, wa_access_token=? WHERE id=?", (pid, tok, db.shop_id()))
    elif not pid:
        db.run("UPDATE shops SET wa_phone_number_id=NULL, wa_access_token=NULL WHERE id=?", (db.shop_id(),))
    else:
        db.run("UPDATE shops SET wa_phone_number_id=? WHERE id=?", (pid, db.shop_id()))
    return await _me_payload()


# ---------------- inbound pipeline (WhatsApp + simulator) ----------------

async def handle_inbound(m: dict, channel: str, shop_id: int | None = None):
    """One inbound message → the right shop → stored → answered. Runs in its own task, so the shop it
    selects stays local to this message."""
    joined = False
    if shop_id is None:
        shop_id, joined = tenancy.resolve(m)
        if shop_id is None:
            await tenancy.send_shop_picker(m["from"])
            return
    db.use_shop(shop_id)
    if channel == "whatsapp" and m.get("wa_id"):
        _spawn(whatsapp.mark_read_typing(m["wa_id"]))
    if joined:   # "join <code>" or a tap in the shop list → open that shop's welcome menu
        m = {**m, "type": "text", "reply_id": None, "text": "hi", "joined_shop": db.get_shop()["name"]}
    customer = db.get_or_create_customer(m["from"], m.get("name"), channel)
    kind, body = "text", m.get("text") or ""
    if m.get("joined_shop"):
        body = f"🏪 Joined {m['joined_shop']}"
    plain = m.get("text")   # what the menu flow reads (voice → transcript, photo → items seen)

    if m["type"] == "location":
        loc = m["location"]
        addr = ", ".join(x for x in (loc.get("name"), loc.get("address")) if x)
        db.run("UPDATE customers SET latitude=?, longitude=?, address=COALESCE(NULLIF(?, ''), address) WHERE id=?",
               (loc.get("latitude"), loc.get("longitude"), addr, customer["id"]))
        kind = "location"
        body = f"📍 Shared location: {addr + ' ' if addr else ''}({loc.get('latitude')}, {loc.get('longitude')})"
    elif m.get("media_id"):
        try:
            data, mime = await whatsapp.download_media(m["media_id"])
            if m["type"] == "audio":
                kind = "voice"
                transcript = await llm.understand_media(
                    data, mime, "Transcribe this voice message exactly, in its original language and script. "
                                "Output only the transcript.")
                body, plain = f"🎤 {transcript}", transcript
            elif m["type"] == "image":
                kind = "image"
                seen = await llm.understand_media(
                    data, mime, "A shop's customer sent this photo. If it is a shopping list or shows products, "
                                "list each item with quantity/size. Otherwise describe it in one line.")
                body = f"🖼️ [Photo] {seen}" + (f"\nCaption: {m['text']}" if m.get("text") else "")
                plain = m.get("text") or seen
            else:
                body = f"[Sent a {m['type']}]" + (f" {m['text']}" if m.get("text") else "")
        except Exception as e:
            log.warning("media handling failed: %r", e)
            body, plain = f"[Sent a {m['type']} that couldn't be processed]", None
    elif m["type"] not in ("text", "interactive", "button"):
        body, plain = f"[Sent unsupported {m['type']} message]", None

    stored = db.add_message(customer["id"], "in", "customer", body, kind=kind, wa_id=m.get("wa_id"))
    if stored is None:
        return   # duplicate webhook delivery
    events.publish("message", {**stored, "phone": customer["phone"], "channel": channel})
    if config.FLOW_MODE == "ai":
        await agent.respond(customer["id"])
    else:
        await flow.handle(customer["id"], plain, m.get("reply_id"), kind)


def _spawn(coro):
    task = asyncio.create_task(coro)
    task.add_done_callback(lambda t: t.exception() and log.error("background task failed", exc_info=t.exception()))


# ---------------- WhatsApp webhook ----------------

@app.get("/webhook")
def verify_webhook(request: Request):
    p = request.query_params
    if p.get("hub.mode") == "subscribe" and p.get("hub.verify_token") == config.VERIFY_TOKEN:
        log.info("Webhook verified with Meta")
        return Response(content=p.get("hub.challenge"), media_type="text/plain")
    return Response("Verification failed", status_code=403)


def _remember_waba_id(payload: dict):
    """Webhook entries carry the WhatsApp Business Account id — needed once by `python -m app.setup_flow`."""
    waba = next((e.get("id") for e in payload.get("entry", []) if e.get("id")), None)
    path = config.ROOT / "data" / "waba_id.txt"
    if waba and not path.exists():
        path.write_text(waba)
        log.info("Recorded WhatsApp Business Account id %s", waba)


@app.post("/webhook")
async def receive_webhook(request: Request):
    raw = await request.body()
    if not whatsapp.verify_signature(raw, request.headers.get("x-hub-signature-256")):
        return Response("bad signature", status_code=401)
    try:
        payload = json.loads(raw)
    except ValueError:
        return {"status": "ignored"}
    _remember_waba_id(payload)
    for m in whatsapp.parse_inbound(payload):
        log.info("📩 %s (%s) [%s]: %s", m.get("name"), m["from"], m["type"], m.get("text"))
        _spawn(handle_inbound(m, "whatsapp"))
    return {"status": "ok"}   # ack fast; Meta retries slow webhooks


# ---------------- dashboard: live events ----------------

@app.get("/api/events", dependencies=[Depends(dashboard_auth)])
async def sse(request: Request):
    q = events.subscribe(db.shop_id())

    async def stream():
        try:
            yield "retry: 3000\n\n"
            while not await request.is_disconnected():
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=20)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            events.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------- dashboard: orders ----------------

class StatusBody(BaseModel):
    status: str
    note: str | None = None
    eta: str | None = None


class PaymentBody(BaseModel):
    status: str


@app.get("/api/stats", dependencies=[Depends(dashboard_auth)])
async def stats():
    today, sid = db.now()[:10], db.shop_id()
    t = db.one("""SELECT COUNT(*) AS orders, COALESCE(SUM(CASE WHEN status NOT IN ('rejected','cancelled') THEN total END),0) AS revenue
                  FROM orders WHERE shop_id=? AND substr(created_at,1,10)=?""", (sid, today))
    n = lambda sql: db.one(sql, (sid,))["n"]   # noqa: E731
    return {
        "orders_today": t["orders"], "revenue_today": t["revenue"],
        "pending": n("SELECT COUNT(*) AS n FROM orders WHERE shop_id=? AND status='pending'"),
        "in_progress": n("SELECT COUNT(*) AS n FROM orders WHERE shop_id=? AND status IN ('accepted','preparing','packed','out_for_delivery')"),
        "low_stock": n("SELECT COUNT(*) AS n FROM products WHERE shop_id=? AND active=1 AND COALESCE(deleted,0)=0 AND stock<=5"),
        "customers": n("SELECT COUNT(*) AS n FROM customers WHERE shop_id=?"),
        "attention": n("SELECT COUNT(*) AS n FROM customers WHERE shop_id=? AND needs_attention=1"),
    }


@app.get("/api/orders", dependencies=[Depends(dashboard_auth)])
async def list_orders(status: str | None = None, limit: int = 100):
    if status:
        rows = db.all_("SELECT id FROM orders WHERE shop_id=? AND status=? ORDER BY id DESC LIMIT ?", (db.shop_id(), status, limit))
    else:
        rows = db.all_("SELECT id FROM orders WHERE shop_id=? ORDER BY id DESC LIMIT ?", (db.shop_id(), limit))
    return [store.order_detail(r["id"]) for r in rows]


@app.get("/api/orders/{order_id}", dependencies=[Depends(dashboard_auth)])
async def get_order(order_id: int):
    o = store.order_detail(order_id)
    if not o:
        raise HTTPException(404)
    return o


@app.post("/api/orders/{order_id}/status", dependencies=[Depends(dashboard_auth)])
async def update_order_status(order_id: int, body: StatusBody):
    r = store.change_status(order_id, body.status, "store", body.note, body.eta)
    if not r["ok"]:
        raise HTTPException(400, r["error"])
    _spawn(messaging.notify_order_update(r["order"], body.status, body.note))
    return r["order"]


@app.post("/api/orders/{order_id}/payment", dependencies=[Depends(dashboard_auth)])
async def update_payment(order_id: int, body: PaymentBody):
    r = store.set_payment_status(order_id, body.status, "store")
    if not r["ok"]:
        raise HTTPException(400, r["error"])
    _spawn(messaging.notify_order_update(r["order"], f"payment_{body.status}"))
    return r["order"]


# ---------------- dashboard: inventory ----------------

class ProductBody(BaseModel):
    name: str | None = None
    brand: str | None = None
    category: str | None = None
    variant: str | None = None
    price: float | None = None
    mrp: float | None = None
    stock: int | None = None
    keywords: str | None = None
    description: str | None = None
    active: bool | None = None


@app.get("/api/products", dependencies=[Depends(dashboard_auth)])
async def list_products():
    return db.all_("SELECT * FROM products WHERE shop_id=? AND COALESCE(deleted, 0)=0 ORDER BY category, name, price",
                   (db.shop_id(),))


@app.post("/api/products", dependencies=[Depends(dashboard_auth)])
async def create_product(body: ProductBody):
    if not (body.name and body.category and body.price is not None):
        raise HTTPException(400, "name, category and price are required")
    cur = db.run("INSERT INTO products(shop_id, name, brand, category, variant, price, mrp, stock, keywords, description, active, "
                 "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (db.shop_id(), body.name, body.brand, body.category, body.variant, body.price, body.mrp or body.price,
                  body.stock or 0, body.keywords or "", body.description or "",
                  int(body.active if body.active is not None else True), db.now(), db.now()))
    p = store.get_product(cur.lastrowid)
    notify.add("product_added", f"📦 New product: {store.product_label(p)}",
               f"Added at {store.money(p['price'])} with {p['stock']} in stock — the WhatsApp bot can offer it now.",
               product_id=p["id"])
    events.publish("inventory", {})
    return p


@app.patch("/api/products/{product_id}", dependencies=[Depends(dashboard_auth)])
async def update_product(product_id: int, body: ProductBody):
    before = store.get_product(product_id)
    if not before:
        raise HTTPException(404)
    fields = body.model_dump(exclude_none=True)
    if "active" in fields:
        fields["active"] = int(fields["active"])
    if "stock" in fields:
        fields["stock"] = max(0, fields["stock"])
    if fields:
        db.run(f"UPDATE products SET {', '.join(f'{k}=?' for k in fields)}, updated_at=? WHERE id=? AND shop_id=?",
               (*fields.values(), db.now(), product_id, db.shop_id()))
    after = store.get_product(product_id)
    if "stock" in fields:
        notify.stock_changed(after, before["stock"])
    events.publish("inventory", {})
    return after


@app.delete("/api/products/{product_id}", dependencies=[Depends(dashboard_auth)])
async def delete_product(product_id: int):
    """Hard-delete unused products; products that appear in past orders are hidden (history stays intact)."""
    if not store.get_product(product_id):
        raise HTTPException(404)
    db.run("DELETE FROM cart_items WHERE product_id=?", (product_id,))
    if db.one("SELECT 1 FROM order_items WHERE product_id=? LIMIT 1", (product_id,)):
        db.run("UPDATE products SET deleted=1, active=0, updated_at=? WHERE id=? AND shop_id=?", (db.now(), product_id, db.shop_id()))
    else:
        db.run("DELETE FROM products WHERE id=? AND shop_id=?", (product_id, db.shop_id()))
    events.publish("inventory", {})
    return {"ok": True}


# ---------------- portal: notifications ----------------

@app.get("/api/notifications", dependencies=[Depends(dashboard_auth)])
async def list_notifications(limit: int = 100):
    return db.all_("SELECT * FROM notifications WHERE shop_id=? ORDER BY id DESC LIMIT ?", (db.shop_id(), limit))


@app.post("/api/notifications/read-all", dependencies=[Depends(dashboard_auth)])
async def read_all_notifications():
    db.run("UPDATE notifications SET is_read=1 WHERE shop_id=? AND is_read=0", (db.shop_id(),))
    return {"ok": True}


@app.post("/api/notifications/{notification_id}/read", dependencies=[Depends(dashboard_auth)])
async def read_notification(notification_id: int):
    db.run("UPDATE notifications SET is_read=1 WHERE id=? AND shop_id=?", (notification_id, db.shop_id()))
    return {"ok": True}


# ---------------- portal: open / close the store ----------------

class StoreStatusBody(BaseModel):
    is_open: bool
    reason: str | None = None


@app.post("/api/store/status", dependencies=[Depends(dashboard_auth)])
async def set_store_status(body: StoreStatusBody):
    db.set_settings({"is_open": "1" if body.is_open else "0", "close_reason": "" if body.is_open else (body.reason or "")})
    if body.is_open:
        notify.add("store_status", "🟢 Store reopened", "The WhatsApp bot is taking orders again.")
    else:
        notify.add("store_status", "🔴 Store closed",
                   f"Reason: {body.reason or 'Temporarily unavailable'}. The WhatsApp bot has paused new orders.")
    events.publish("settings", db.get_settings())
    return db.get_settings()


# ---------------- dashboard: customers & chat ----------------

class TextBody(BaseModel):
    text: str


class BotBody(BaseModel):
    paused: bool


@app.get("/api/customers", dependencies=[Depends(dashboard_auth)])
async def list_customers():
    return db.all_("""
        SELECT c.*, (SELECT body FROM messages m WHERE m.customer_id=c.id ORDER BY id DESC LIMIT 1) AS last_message,
               (SELECT created_at FROM messages m WHERE m.customer_id=c.id ORDER BY id DESC LIMIT 1) AS last_message_at,
               (SELECT COUNT(*) FROM orders o WHERE o.customer_id=c.id) AS order_count,
               (SELECT COALESCE(SUM(total),0) FROM orders o WHERE o.customer_id=c.id AND status='delivered') AS lifetime_value
        FROM customers c WHERE c.shop_id=? ORDER BY COALESCE(last_message_at, c.created_at) DESC""", (db.shop_id(),))


@app.get("/api/customers/{customer_id}/messages", dependencies=[Depends(dashboard_auth)])
async def customer_messages(customer_id: int, limit: int = 200):
    if not db.get_customer(customer_id):
        raise HTTPException(404)
    return db.all_("SELECT * FROM (SELECT * FROM messages WHERE customer_id=? ORDER BY id DESC LIMIT ?) ORDER BY id",
                   (customer_id, limit))


@app.get("/api/customers/{customer_id}", dependencies=[Depends(dashboard_auth)])
async def customer_detail(customer_id: int):
    c = db.get_customer(customer_id)
    if not c:
        raise HTTPException(404)
    return {**c, "cart": store.cart(customer_id), "orders": store.customer_orders(customer_id, 10)}


@app.post("/api/customers/{customer_id}/messages", dependencies=[Depends(dashboard_auth)])
async def store_reply(customer_id: int, body: TextBody):
    c = db.get_customer(customer_id)
    if not c or not body.text.strip():
        raise HTTPException(400)
    db.run("UPDATE customers SET needs_attention=0 WHERE id=?", (customer_id,))
    return await messaging.send_to_customer(c, body.text.strip(), sender="store")


@app.post("/api/customers/{customer_id}/bot", dependencies=[Depends(dashboard_auth)])
async def toggle_bot(customer_id: int, body: BotBody):
    if not db.get_customer(customer_id):
        raise HTTPException(404)
    db.run("UPDATE customers SET bot_paused=? WHERE id=?", (int(body.paused), customer_id))
    events.publish("customer", {"id": customer_id})
    return db.get_customer(customer_id)


@app.post("/api/customers/{customer_id}/resolve", dependencies=[Depends(dashboard_auth)])
async def resolve_attention(customer_id: int):
    if not db.get_customer(customer_id):
        raise HTTPException(404)
    db.run("UPDATE customers SET needs_attention=0 WHERE id=?", (customer_id,))
    events.publish("customer", {"id": customer_id})
    return {"ok": True}


# ---------------- dashboard: settings ----------------

@app.get("/api/settings", dependencies=[Depends(dashboard_auth)])
async def get_settings():
    return db.get_settings()


@app.put("/api/settings", dependencies=[Depends(dashboard_auth)])
async def put_settings(body: dict):
    db.set_settings(body)
    return db.get_settings()


# ---------------- simulator (test without WhatsApp) ----------------

class SimBody(BaseModel):
    phone: str
    name: str | None = None
    text: str | None = None
    reply_id: str | None = None   # id of a tapped button / list row
    latitude: float | None = None
    longitude: float | None = None
    address: str | None = None


@app.post("/api/sim/message", dependencies=[Depends(dashboard_auth)])
async def sim_message(body: SimBody):
    m = {"from": body.phone, "name": body.name, "wa_id": None, "type": "interactive" if body.reply_id else "text",
         "text": body.text, "reply_id": body.reply_id}
    if body.latitude is not None:
        m.update(type="location", location={"latitude": body.latitude, "longitude": body.longitude,
                                            "address": body.address or ""})
    _spawn(handle_inbound(m, "sim", db.shop_id()))
    return {"ok": True}


# ---------------- payment page (UPI demo) ----------------

PAY_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Pay {code}</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<style>body{{font-family:system-ui,-apple-system,sans-serif;background:#f4f5f7;margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center;color:#1d2433}}
.card{{background:#fff;border-radius:16px;padding:28px;max-width:360px;width:calc(100% - 32px);box-shadow:0 6px 30px rgba(0,0,0,.08);text-align:center}}
h1{{font-size:18px;margin:0 0 4px}}.amt{{font-size:34px;font-weight:700;margin:14px 0}}.muted{{color:#667085;font-size:14px}}
#qr{{display:flex;justify-content:center;margin:18px 0}}a.btn,button{{display:block;width:100%;padding:13px;border-radius:10px;border:0;font-size:15px;font-weight:600;margin-top:10px;cursor:pointer;text-decoration:none}}
a.btn{{background:#1a7f37;color:#fff}}button{{background:#eef0f4;color:#1d2433}}.paid{{color:#1a7f37;font-weight:700;font-size:18px;margin:18px 0}}</style></head>
<body><div class="card"><h1>{store}</h1><div class="muted">Order {code}</div><div class="amt">{amount}</div>
{body}</div>
<script>{script}</script></body></html>"""


def _upi_uri(o: dict, s: dict) -> str:
    from urllib.parse import quote
    return (f"upi://pay?pa={quote(s['upi_id'])}&pn={quote(s['store_name'])}&am={o['total']:.2f}"
            f"&cu=INR&tn={quote('Order ' + o['code'])}")


@app.get("/pay/{code}", response_class=HTMLResponse)
async def pay_page(code: str):
    o = db.one("SELECT * FROM orders WHERE upper(code)=upper(?)", (code,))
    if not o:
        raise HTTPException(404, "Order not found")
    db.use_shop(o["shop_id"])
    s = db.get_settings()
    amount = store.money(o["total"])
    if o["payment_status"] == "paid":
        body, script = '<div class="paid">✅ Payment received</div>', ""
    elif o["status"] in ("rejected", "cancelled"):
        body, script = f'<div class="muted">This order was {o["status"]}.</div>', ""
    else:
        uri = _upi_uri(o, s)
        body = (f'<div id="qr"></div><div class="muted">Scan with any UPI app<br>UPI ID: <b>{html.escape(s["upi_id"])}</b></div>'
                f'<a class="btn" href="{html.escape(uri)}">Pay with UPI app</a>'
                f'<button onclick="simulate()">Simulate successful payment (demo)</button>')
        script = (f'new QRCode(document.getElementById("qr"),{{text:{json.dumps(uri)},width:200,height:200}});'
                  f'async function simulate(){{await fetch("/pay/{o["code"]}/simulate",{{method:"POST"}});location.reload()}}')
    return PAY_HTML.format(code=o["code"], store=html.escape(s["store_name"]), amount=amount, body=body, script=script)


@app.post("/pay/{code}/simulate")
async def pay_simulate(code: str):
    """Stand-in for a payment-gateway webhook (e.g. Razorpay payment.captured) during the demo."""
    o = db.one("SELECT * FROM orders WHERE upper(code)=upper(?)", (code,))
    if not o or o["payment_status"] == "paid" or o["status"] in ("rejected", "cancelled"):
        return {"ok": False}
    db.use_shop(o["shop_id"])
    r = store.set_payment_status(o["id"], "paid", "customer", "UPI payment (demo gateway)")
    _spawn(messaging.notify_order_update(r["order"], "payment_paid"))
    return {"ok": True}


# ---------------- dashboard UI ----------------

@app.get("/health")
def health():
    return {"ok": True, "flow_mode": config.FLOW_MODE, "llm_chain": [p["name"] for p in llm._providers()],
            "shops": db.one("SELECT COUNT(*) AS n FROM shops WHERE active=1")["n"],
            "whatsapp_configured": bool(config.ACCESS_TOKEN and config.PHONE_NUMBER_ID)}


@app.get("/classic", dependencies=[Depends(dashboard_auth)])
def classic_dashboard():
    return FileResponse(config.STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")

# The store portal (frontend/) at "/" — registered last so every API route above takes precedence.
if (config.FRONTEND_DIR / "index.html").exists():
    app.mount("/", StaticFiles(directory=config.FRONTEND_DIR, html=True), name="portal")
else:
    @app.get("/", dependencies=[Depends(dashboard_auth)])
    def dashboard():
        return FileResponse(config.STATIC_DIR / "index.html")
