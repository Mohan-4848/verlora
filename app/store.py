"""Store domain logic: catalogue search, cart, pricing, orders. Shared by the AI agent and the dashboard.
All money maths happens here – the LLM never computes prices."""
import difflib
import hashlib
import json
import re

from . import db, events, notify

ACTIVE_STATUSES = ("pending", "accepted", "preparing", "packed", "out_for_delivery")
TRANSITIONS = {
    "pending": {"accepted", "rejected", "cancelled"},
    "accepted": {"preparing", "packed", "out_for_delivery", "delivered", "cancelled"},
    "preparing": {"packed", "out_for_delivery", "delivered", "cancelled"},
    "packed": {"out_for_delivery", "delivered", "cancelled"},
    "out_for_delivery": {"delivered"},
}
STOCK_RESTORING = {"rejected", "cancelled"}
STOCK_DEDUCTING = {"out_for_delivery", "delivered"}   # stock leaves the shop when the order is sent (or delivered)

_STOP = {"a", "an", "the", "of", "and", "some", "pack", "packet", "packets", "please", "want", "need",
         "give", "me", "i", "for", "to", "kg", "g", "gm", "ml", "l", "ltr", "litre", "liter", "x",
         # Hindi / Telugu filler words ("mujhe … chahiye", "naaku … kavali")
         "mujhe", "chahiye", "chaiye", "bhejo", "bhej", "do", "dena", "de", "hai", "kya", "bhaiya", "bhai",
         "naaku", "naku", "kavali", "kaavali", "kavaali", "ivvandi", "pampandi", "kuda", "kooda", "anna", "andi"}


# What customers can still buy: physical stock minus what open (not yet dispatched) orders are holding.
AVAILABLE = "(stock - COALESCE(reserved, 0))"
PRODUCT_COLS = f"*, {AVAILABLE} AS available"


def is_open() -> bool:
    return db.get_settings().get("is_open", "1") != "0"


def closed_reason() -> str:
    return db.get_settings().get("close_reason") or "Temporarily unavailable"


def money(v: float) -> str:
    return f"₹{v:,.2f}".replace(".00", "")


def _norm(s: str) -> str:
    return re.sub(r"[.,!?;:()\[\]{}\"'/\\|\-_*+]+", " ", (s or "").lower())


def product_label(p: dict) -> str:
    return f"{p['name']} ({p['variant']})" if p.get("variant") else p["name"]


def public_product(p: dict) -> dict:
    return {"product_id": p["id"], "name": p["name"], "brand": p["brand"], "variant": p["variant"],
            "price": p["price"], "category": p["category"],
            "availability": "out_of_stock" if p["available"] <= 0 else ("low_stock" if p["available"] <= 5 else "in_stock"),
            "stock_left": p["available"]}


# ---------------- catalogue ----------------

def categories() -> list[dict]:
    return db.all_(f"""SELECT category, COUNT(*) AS products,
                             GROUP_CONCAT(DISTINCT name) AS examples
                      FROM (SELECT * FROM products WHERE shop_id=? AND active=1 AND {AVAILABLE}>0 ORDER BY id)
                      GROUP BY category ORDER BY category""", (db.shop_id(),))


def available_count() -> int:
    """Products customers can order right now (listed, not deleted, in stock)."""
    return db.one(f"SELECT COUNT(*) AS n FROM products WHERE shop_id=? AND active=1 AND {AVAILABLE}>0", (db.shop_id(),))["n"]


def example_names(n: int = 3) -> list[str]:
    """A few real in-stock product names for prompts like "type a product name — e.g. …"."""
    rows = db.all_(f"SELECT DISTINCT name FROM products WHERE shop_id=? AND active=1 AND {AVAILABLE}>0 ORDER BY RANDOM() LIMIT ?",
                   (db.shop_id(), n))
    return [r["name"] for r in rows]


def catalogue_vocabulary(limit: int = 150) -> list[str]:
    """Distinct names of what the shop sells right now — helps the AI map free text onto real products."""
    rows = db.all_(f"SELECT DISTINCT name FROM products WHERE shop_id=? AND active=1 AND {AVAILABLE}>0 ORDER BY name LIMIT ?",
                   (db.shop_id(), limit))
    return [r["name"] for r in rows]


def products_in_category(category: str) -> list[dict]:
    return db.all_(f"SELECT {PRODUCT_COLS} FROM products WHERE shop_id=? AND active=1 AND lower(category)=lower(?) ORDER BY name, price",
                   (db.shop_id(), category))


def search_products(query: str, category: str | None = None, limit: int = 8) -> list[dict]:
    rows = db.all_(f"SELECT {PRODUCT_COLS} FROM products WHERE shop_id=? AND active=1", (db.shop_id(),))
    if category:
        rows = [r for r in rows if r["category"].lower() == category.lower()] or rows
    q = _norm(query)
    tokens = [t for t in q.split() if t not in _STOP and not t.isdigit()]
    if not tokens:
        tokens = q.split()
    scored = []
    any_exact = False   # did any word match a product/keyword word exactly (not just fuzzily)?
    for r in rows:
        name_toks = set(_norm(f"{r['name']} {r['brand'] or ''}").split())
        kw_toks = set(_norm(r["keywords"]).split())
        cat_toks = set(_norm(r["category"]).split())
        var = _norm(r["variant"] or "")
        vocab = list(name_toks | kw_toks)
        score = 0.0
        for t in tokens:
            if t in name_toks:
                score += 3
                any_exact = True
            elif t in kw_toks:
                score += 2.5
                any_exact = True
            elif t in cat_toks:
                score += 1
            elif len(t) >= 4 and difflib.get_close_matches(t, vocab, n=1, cutoff=0.8):
                score += 1.5           # typo tolerance: "tamoto", "bred"
            elif len(t) >= 3 and any(v.startswith(t) for v in vocab):
                score += 1
            if any(ch.isdigit() for ch in t) and t in var.replace(" ", ""):
                score += 0.5           # "500ml", "5kg" pick the matching variant
        if q.strip() and q.strip() in _norm(r["name"]):
            score += 2
            any_exact = True
        if score > 0:
            scored.append((score, r["available"] > 0, r))
    if len(tokens) >= 2 and not any_exact:
        # a sentence where no word really matched ("kuch thanda peene ko" ≈ "anda") — fuzzy hits are noise here;
        # return nothing so the caller can ask the AI what was meant
        return []
    scored.sort(key=lambda x: (-x[0], not x[1], x[2]["name"], x[2]["price"]))
    if scored:
        best = scored[0][0]
        scored = [s for s in scored if s[0] >= best * 0.5]   # drop weak tail matches
    return [s[2] for s in scored[:limit]]


def get_product(product_id: int) -> dict | None:
    return db.one(f"SELECT {PRODUCT_COLS} FROM products WHERE id=? AND shop_id=?", (product_id, db.shop_id()))


# ---------------- cart & pricing ----------------

def delivery_fee_for(subtotal: float) -> float:
    s = db.get_settings()
    if subtotal <= 0 or subtotal >= float(s["free_delivery_above"]):
        return 0.0
    return float(s["delivery_fee"])


def cart(customer_id: int) -> dict:
    rows = db.all_("""SELECT ci.product_id, ci.quantity, p.name, p.variant, p.price,
                             p.stock - COALESCE(p.reserved, 0) AS available, p.active
                      FROM cart_items ci JOIN products p ON p.id = ci.product_id
                      WHERE ci.customer_id=? ORDER BY ci.added_at""", (customer_id,))
    items, warnings = [], []
    for r in rows:
        line = round(r["price"] * r["quantity"], 2)
        items.append({"product_id": r["product_id"], "item": product_label(r), "unit_price": r["price"],
                      "quantity": r["quantity"], "line_total": line})
        if not r["active"] or r["available"] <= 0:
            warnings.append(f"{product_label(r)} is now out of stock")
        elif r["available"] < r["quantity"]:
            warnings.append(f"Only {r['available']} left of {product_label(r)}")
    subtotal = round(sum(i["line_total"] for i in items), 2)
    fee = delivery_fee_for(subtotal)
    s = db.get_settings()
    out = {"items": items, "item_count": sum(i["quantity"] for i in items), "subtotal": subtotal,
           "delivery_fee": fee, "total": round(subtotal + fee, 2)}
    if items and fee:
        out["free_delivery_hint"] = f"Add {money(float(s['free_delivery_above']) - subtotal)} more for free delivery"
    if warnings:
        out["warnings"] = warnings
    return out


def set_cart_qty(customer_id: int, product_id: int, quantity: int, add: bool = False) -> dict:
    p = get_product(product_id)
    if not p or not p["active"]:
        return {"ok": False, "error": f"Product {product_id} not found. Search the catalogue first."}
    existing = db.one("SELECT quantity FROM cart_items WHERE customer_id=? AND product_id=?", (customer_id, product_id))
    new_qty = quantity + (existing["quantity"] if existing and add else 0)
    if new_qty <= 0:
        db.run("DELETE FROM cart_items WHERE customer_id=? AND product_id=?", (customer_id, product_id))
        return {"ok": True, "removed": product_label(p), "cart": cart(customer_id)}
    if p["available"] <= 0:
        return {"ok": False, "error": f"{product_label(p)} is out of stock.",
                "alternatives": [public_product(a) for a in search_products(p["name"] + " " + p["category"])
                                 if a["id"] != p["id"] and a["available"] > 0][:3]}
    if new_qty > p["available"]:
        return {"ok": False, "error": f"Only {p['available']} available for {product_label(p)}.",
                "max_quantity": p["available"]}
    if new_qty > 50:
        return {"ok": False, "error": "Quantity looks too large for a single order (max 50 per item). Please confirm with the customer."}
    db.run("INSERT INTO cart_items(customer_id, product_id, quantity, added_at) VALUES(?,?,?,?) "
           "ON CONFLICT(customer_id, product_id) DO UPDATE SET quantity=excluded.quantity",
           (customer_id, product_id, new_qty, db.now()))
    return {"ok": True, "item": product_label(p), "quantity_in_cart": new_qty, "cart": cart(customer_id)}


def clear_cart(customer_id: int):
    db.run("DELETE FROM cart_items WHERE customer_id=?", (customer_id,))


# ---------------- checkout ----------------

def _checkout_hash(customer: dict, c: dict, payment: str) -> str:
    raw = json.dumps([[(i["product_id"], i["quantity"], i["unit_price"]) for i in c["items"]],
                      customer.get("address"), customer.get("landmark"), payment], sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def review_checkout(customer: dict, payment_method: str) -> dict:
    """Step 1 of checkout: validates and returns the summary the customer must confirm."""
    payment_method = (payment_method or "").upper()
    if payment_method not in ("COD", "UPI"):
        return {"ok": False, "error": "payment_method must be COD or UPI – ask the customer."}
    c = cart(customer["id"])
    if not c["items"]:
        return {"ok": False, "error": "Cart is empty."}
    if c.get("warnings"):
        return {"ok": False, "error": "Fix cart stock problems first.", "warnings": c["warnings"]}
    s = db.get_settings()
    if c["subtotal"] < float(s["min_order"] or 0):
        return {"ok": False, "error": f"Minimum order is {money(float(s['min_order']))}."}
    if not customer.get("address") and customer.get("latitude") is None:
        return {"ok": False, "error": "Delivery address missing – ask for full address (or a location pin)."}
    token = _checkout_hash(customer, c, payment_method)
    if token != customer.get("checkout_token"):   # re-reviewing an unchanged order keeps the original review point
        db.run("UPDATE customers SET checkout_token=?, checkout_payment=?, checkout_msg_id=? WHERE id=?",
               (token, payment_method, _last_customer_msg_id(customer["id"]), customer["id"]))
    return {"ok": True, "awaiting_customer_confirmation": True,
            "deliver_to": {"name": customer.get("name") or customer.get("wa_name"),
                           "address": customer.get("address") or "Shared location pin",
                           "landmark": customer.get("landmark")},
            "payment_method": payment_method, "eta": s["delivery_eta"], **c}


def _last_customer_msg_id(customer_id: int) -> int:
    return db.one("SELECT COALESCE(MAX(id), 0) AS m FROM messages WHERE customer_id=? AND sender='customer'",
                  (customer_id,))["m"]


def place_order(customer: dict, require_reply: bool = True) -> dict:
    """Step 2 of checkout: only succeeds if nothing changed since review_checkout."""
    if not is_open():
        return {"ok": False, "error": f"The store is closed right now ({closed_reason()}). Please try again later."}
    payment = customer.get("checkout_payment")
    c = cart(customer["id"])
    if not customer.get("checkout_token") or not payment:
        return {"ok": False, "error": "Call review_order first and get the customer's confirmation."}
    if _checkout_hash(customer, c, payment) != customer["checkout_token"]:
        return {"ok": False, "error": "Cart/address/payment changed since review. Call review_order again."}
    if require_reply and _last_customer_msg_id(customer["id"]) <= (customer.get("checkout_msg_id") or 0):
        return {"ok": False, "error": "The customer hasn't replied to the order summary yet. Show it and wait for their confirmation."}

    s = db.get_settings()
    old_available = {i["product_id"]: get_product(i["product_id"])["available"] for i in c["items"]}
    try:
        order_id = _create_order(customer, c, payment, s)
    except OutOfStock as e:
        return {"ok": False, "error": f"{e} just went out of stock – update the cart."}

    order = order_detail(order_id)
    who = order["customer_name"] or order["phone"]
    notify.add("new_order", f"🔔 New order {order['code']}",
               f"{who} ordered {sum(i['quantity'] for i in order['items'])} item(s) worth {money(order['total'])} "
               f"({'Cash on Delivery' if payment == 'COD' else 'UPI'}).", order_id=order_id, customer_id=customer["id"])
    for pid, before in old_available.items():
        notify.stock_changed(get_product(pid), before)
    events.publish("order_new", order)
    events.publish("inventory", {})
    return {"ok": True, "order": order}


class OutOfStock(Exception):
    pass


def _create_order(customer: dict, c: dict, payment: str, s: dict) -> int:
    with db.tx() as con:
        for i in c["items"]:
            cur = con.execute(f"UPDATE products SET reserved=COALESCE(reserved, 0)+?, updated_at=? WHERE id=? AND shop_id=? "
                              f"AND {AVAILABLE}>=? AND active=1", (i["quantity"], db.now(), i["product_id"], db.shop_id(), i["quantity"]))
            if cur.rowcount != 1:
                raise OutOfStock(i["item"])   # tx() rolls back the stock already reserved
        cur = con.execute(
            "INSERT INTO orders(shop_id, customer_id, status, subtotal, delivery_fee, total, payment_method, payment_status, "
            "customer_name, address, landmark, latitude, longitude, eta, created_at, updated_at, stock_deducted) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (db.shop_id(), customer["id"], "pending", c["subtotal"], c["delivery_fee"], c["total"], payment, "pending",
             customer.get("name") or customer.get("wa_name"), customer.get("address"), customer.get("landmark"),
             customer.get("latitude"), customer.get("longitude"), s["delivery_eta"], db.now(), db.now()))
        order_id = cur.lastrowid
        code = f"{(db.get_shop() or {}).get('code_prefix') or 'OR'}{1000 + order_id}"   # ids are global → codes unique
        con.execute("UPDATE orders SET code=? WHERE id=?", (code, order_id))
        for i in c["items"]:
            p = get_product(i["product_id"])
            con.execute("INSERT INTO order_items(order_id, product_id, name, variant, price, quantity, line_total) "
                        "VALUES(?,?,?,?,?,?,?)", (order_id, p["id"], p["name"], p["variant"], i["unit_price"],
                                                  i["quantity"], i["line_total"]))
        con.execute("INSERT INTO order_events(order_id, status, note, actor, created_at) VALUES(?,?,?,?,?)",
                    (order_id, "pending", "Order placed on WhatsApp", "customer", db.now()))
        con.execute("DELETE FROM cart_items WHERE customer_id=?", (customer["id"],))
        con.execute("UPDATE customers SET checkout_token=NULL, checkout_payment=NULL, checkout_msg_id=NULL WHERE id=?", (customer["id"],))
    return order_id


# ---------------- orders ----------------

def order_detail(order_id: int) -> dict | None:
    o = db.one("""SELECT o.*, c.phone, c.wa_name FROM orders o JOIN customers c ON c.id=o.customer_id
                  WHERE o.id=? AND o.shop_id=?""", (order_id, db.shop_id()))
    if not o:
        return None
    o["items"] = db.all_("SELECT * FROM order_items WHERE order_id=?", (order_id,))
    o["timeline"] = db.all_("SELECT status, note, actor, created_at FROM order_events WHERE order_id=? ORDER BY id",
                            (order_id,))
    return o


def order_by_code(customer_id: int, code: str) -> dict | None:
    o = db.one("SELECT id FROM orders WHERE customer_id=? AND upper(code)=upper(?)", (customer_id, code.strip()))
    return order_detail(o["id"]) if o else None


def customer_orders(customer_id: int, limit: int = 5) -> list[dict]:
    rows = db.all_("SELECT id FROM orders WHERE customer_id=? ORDER BY id DESC LIMIT ?", (customer_id, limit))
    return [order_detail(r["id"]) for r in rows]


def agent_order_view(o: dict) -> dict:
    return {"order_code": o["code"], "status": o["status"], "placed_at": o["created_at"],
            "items": [f"{i['quantity']} x {i['name']} ({i['variant']}) = {money(i['line_total'])}" for i in o["items"]],
            "subtotal": o["subtotal"], "delivery_fee": o["delivery_fee"], "total": o["total"],
            "payment_method": o["payment_method"], "payment_status": o["payment_status"],
            "reject_reason": o["reject_reason"], "eta": o["eta"],
            "timeline": [f"{t['created_at']}: {t['status']}" + (f" ({t['note']})" if t["note"] else "")
                         for t in o["timeline"]]}


def change_status(order_id: int, new_status: str, actor: str, note: str | None = None,
                  eta: str | None = None) -> dict:
    o = db.one("SELECT * FROM orders WHERE id=? AND shop_id=?", (order_id, db.shop_id()))
    if not o:
        return {"ok": False, "error": "Order not found"}
    if new_status not in TRANSITIONS.get(o["status"], set()):
        return {"ok": False, "error": f"Cannot move order from {o['status']} to {new_status}"}
    with db.tx() as con:
        con.execute("UPDATE orders SET status=?, updated_at=?, eta=COALESCE(?, eta), "
                    "reject_reason=CASE WHEN ? IN ('rejected','cancelled') THEN ? ELSE reject_reason END WHERE id=?",
                    (new_status, db.now(), eta, new_status, note, order_id))
        con.execute("INSERT INTO order_events(order_id, status, note, actor, created_at) VALUES(?,?,?,?,?)",
                    (order_id, new_status, note, actor, db.now()))
        items = con.execute("SELECT product_id, quantity FROM order_items WHERE order_id=?", (order_id,)).fetchall()
        if new_status in STOCK_DEDUCTING and not o["stock_deducted"]:
            # the goods physically leave the shop → take them off stock and drop the reservation
            for i in items:
                con.execute("UPDATE products SET stock=MAX(stock-?, 0), reserved=MAX(COALESCE(reserved, 0)-?, 0), "
                            "updated_at=? WHERE id=?", (i["quantity"], i["quantity"], db.now(), i["product_id"]))
            con.execute("UPDATE orders SET stock_deducted=1 WHERE id=?", (order_id,))
        if new_status in STOCK_RESTORING:
            for i in items:
                if o["stock_deducted"]:   # (orders from before reservations existed) put the stock back
                    con.execute("UPDATE products SET stock=stock+?, updated_at=? WHERE id=?",
                                (i["quantity"], db.now(), i["product_id"]))
                else:                     # release the hold
                    con.execute("UPDATE products SET reserved=MAX(COALESCE(reserved, 0)-?, 0), updated_at=? WHERE id=?",
                                (i["quantity"], db.now(), i["product_id"]))
            if o["payment_status"] == "paid":
                con.execute("UPDATE orders SET payment_status='refund_due' WHERE id=?", (order_id,))
    detail = order_detail(order_id)
    who = detail["customer_name"] or detail["phone"]
    if new_status == "out_for_delivery":
        notify.add("order_dispatched", f"🛵 {detail['code']} out for delivery",
                   f"On the way to {who} — {detail['address'] or 'location pin'}.", order_id=order_id)
    elif new_status == "delivered":
        notify.add("order_delivered", f"✅ {detail['code']} delivered", f"{who} · {money(detail['total'])}.", order_id=order_id)
    elif new_status == "cancelled" and actor == "customer":
        notify.add("order_cancelled", f"🛑 {detail['code']} cancelled by customer",
                   f"{who} cancelled on WhatsApp. Stock has been restored.", order_id=order_id)
    elif new_status == "rejected":
        notify.add("order_rejected", f"❌ {detail['code']} rejected", f"Reason sent to {who}: {note or '—'}", order_id=order_id)
    events.publish("order_updated", detail)
    if new_status in STOCK_RESTORING or new_status in STOCK_DEDUCTING:
        events.publish("inventory", {})
    return {"ok": True, "order": detail}


def set_payment_status(order_id: int, status: str, actor: str, note: str | None = None) -> dict:
    if status not in ("pending", "paid", "refunded", "refund_due"):
        return {"ok": False, "error": "invalid payment status"}
    o = db.one("SELECT * FROM orders WHERE id=? AND shop_id=?", (order_id, db.shop_id()))
    if not o:
        return {"ok": False, "error": "Order not found"}
    with db.tx() as con:
        con.execute("UPDATE orders SET payment_status=?, updated_at=? WHERE id=?", (status, db.now(), order_id))
        con.execute("INSERT INTO order_events(order_id, status, note, actor, created_at) VALUES(?,?,?,?,?)",
                    (order_id, f"payment_{status}", note, actor, db.now()))
    detail = order_detail(order_id)
    if status == "paid":
        notify.add("payment_received", f"💰 Payment received · {detail['code']}",
                   f"{money(detail['total'])} via {detail['payment_method']} from {detail['customer_name'] or detail['phone']}.",
                   order_id=order_id)
    events.publish("order_updated", detail)
    return {"ok": True, "order": detail}
