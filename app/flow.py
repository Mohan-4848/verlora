"""Menu-driven WhatsApp ordering: hardcoded steps with reply buttons and list menus.

    greeting ─► type a product / browse categories ─► pick from list ─► quantity
       ─► add more | cart | checkout ─► address ─► payment (COD / UPI) ─► summary ─► confirm
       ─► order placed ─► store accepts on the dashboard ─► status updates on WhatsApp

AI is used in exactly one place: when plain database search can't match what the customer typed
(another language/script, vague requests, several items in one message) → understand.understand().
Everything else — prices, stock, cart, checkout, orders — is deterministic code.
"""
import asyncio
import json
import logging
import re

from . import config, db, events, messaging, notify, store, tenancy, understand

log = logging.getLogger("flow")
_locks: dict[int, asyncio.Lock] = {}

GREETINGS = {"hi", "hii", "hiii", "hlo", "hloo", "hlw", "hello", "helo", "hello there", "hey", "hai", "hy", "yo",
             "good morning", "good evening", "gm", "menu", "start", "home", "namaste", "namaskar", "namaskaram",
             "main menu", "0", "restart", "back"}
YES = {"yes", "y", "ok", "okay", "confirm", "confirm order", "haan", "han", "ha", "avunu", "sure", "done", "place order"}
NO = {"no", "n", "cancel", "nahi", "vaddu", "stop"}
STATUS_LABEL = {"pending": "⏳ Waiting for store", "accepted": "✅ Accepted", "preparing": "👨‍🍳 Preparing",
                "packed": "📦 Packed",
                "out_for_delivery": "🛵 Out for delivery", "delivered": "🎉 Delivered",
                "rejected": "❌ Rejected", "cancelled": "🛑 Cancelled"}
MULTI_ITEM = re.compile(r",|\n|&|\+|\b(and|aur|or|inka|mariyu|also|plus)\b", re.I)
# splits "2 milk and 1 bread", "milk, eggs, bread", "doodh aur bread" into separate items
SPLIT_ITEMS = re.compile(r"\s*(?:,|\n|&|\+|\band\b|\baur\b|\binka\b|\bmariyu\b|\balso\b|\bplus\b)\s*", re.I)


def split_items(text: str) -> list[str]:
    return [p.strip() for p in SPLIT_ITEMS.split(text or "") if p and p.strip()]


PAGE = 9   # list rows per page (WhatsApp max is 10; the 10th is "More items")
MAX_PICK = 20   # CheckboxGroup option limit in a WhatsApp Flow
QTY_CHOICES = [1, 2, 3, 4, 5, 6, 8, 10, 12, 15]
# "1 3 4", "1,3", "1x2 3x5", "2 x 3 and 4" — several items picked by number from the last list
SELECTION = re.compile(r"^\s*\d{1,2}(\s*[x×*]\s*\d{1,3})?((\s*(,|&|and|\s)\s*)\d{1,2}(\s*[x×*]\s*\d{1,3})?)*\s*$", re.I)


def _norm(t: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (t or "").lower())).strip()


# ---------------- state ----------------

def _state(c: dict) -> tuple[str, dict]:
    try:
        return c.get("flow_state") or "idle", json.loads(c.get("flow_data") or "{}")
    except ValueError:
        return "idle", {}


def _save(customer_id: int, state: str, data: dict | None = None):
    data = dict(data or {})
    # keep the last search results across steps so "➕ Add more" can reopen the picker
    prev = _state(db.get_customer(customer_id))[1]
    for k in ("results", "heading"):
        if k not in data and k in prev:
            data[k] = prev[k]
    db.run("UPDATE customers SET flow_state=?, flow_data=? WHERE id=?", (state, json.dumps(data), customer_id))


# ---------------- senders ----------------

async def _text(c, body):
    await messaging.send_to_customer(c, body, "agent")


async def _buttons(c, body, buttons):
    await messaging.send_to_customer(c, body, "agent", buttons=buttons)


async def _list(c, body, button, rows, section="Options"):
    await messaging.send_to_customer(c, body, "agent", menu={"button": button, "sections": [
        {"title": section, "rows": rows[:10]}]})


def _ptitle(p: dict, limit: int = 24) -> str:
    """WhatsApp list titles max out at 24 chars — keep the size visible so variants stay distinguishable."""
    name, variant = p["name"], p.get("variant") or ""
    if p.get("brand") and name.lower().startswith(p["brand"].lower() + " "):
        short = name[len(p["brand"]) + 1:]          # "Amul Gold Full Cream Milk" → "Gold Full Cream Milk"
    else:
        short = name
    for candidate in (f"{name} {variant}", f"{short} {variant}"):
        if len(candidate.strip()) <= limit:
            return candidate.strip()
    room = limit - len(variant) - 2
    return f"{short[:room]}… {variant}" if variant else short[:limit - 1] + "…"


def _pdesc(p: dict) -> str:
    parts = [store.money(p["price"]), p.get("variant") or "", p.get("brand") or ""]
    if 0 < p["available"] <= 5:
        parts.append(f"only {p['available']} left")
    return " · ".join(x for x in parts if x)


def _cart_lines(cart: dict) -> str:
    lines = [f"• {i['quantity']} × {i['item']} — {store.money(i['line_total'])}" for i in cart["items"]]
    fee = store.money(cart["delivery_fee"]) if cart["delivery_fee"] else "FREE"
    out = "\n".join(lines) + f"\n\nSubtotal: {store.money(cart['subtotal'])}\nDelivery: {fee}\n*Total: {store.money(cart['total'])}*"
    if cart.get("free_delivery_hint"):
        out += f"\n_💡 {cart['free_delivery_hint']}_"
    if cart.get("warnings"):
        out += "\n\n⚠️ " + "\n⚠️ ".join(cart["warnings"])
    return out


def _address(c: dict) -> str | None:
    if c.get("address"):
        return c["address"] + (f" (near {c['landmark']})" if c.get("landmark") else "")
    if c.get("latitude") is not None:
        return f"📍 Location pin ({c['latitude']:.5f}, {c['longitude']:.5f})"
    return None


# ---------------- search (the only AI touch-point) ----------------

async def find_products(text: str) -> tuple[list[dict], str]:
    """→ (products in this shop, intent). Database first; the AI only when that finds nothing or several items are listed."""
    q = text.strip()
    parts = split_items(q)
    multi = len(parts) > 1
    if not multi:
        hits = store.search_products(q, limit=10)
        if hits:
            return hits, "order"              # plain DB search was enough — no AI call
    else:
        found, seen = [], set()
        for part in parts:                    # a list of items: look each one up
            hits = store.search_products(part, limit=5)
            if not hits:
                break
            found += [p for p in hits if p["id"] not in seen and not seen.add(p["id"])]
        else:
            return found[:10], "order"        # every item found without the AI
    s = db.get_settings()
    meaning = await understand.understand(q, store.catalogue_vocabulary(),
                                          f"{s['store_name']} (a {s.get('business_type') or 'local shop'})")
    seen, out = set(), []
    for term in meaning["items"]:
        for p in store.search_products(term, limit=4):
            if p["id"] not in seen:
                seen.add(p["id"])
                out.append(p)
    if not out and multi:
        out = store.search_products(q, limit=10)   # AI unavailable: best-effort plain search
    return out[:10], meaning["intent"]


# ---------------- screens ----------------

async def main_menu(c, greet=True):
    s = db.get_settings()
    cart = store.cart(c["id"])
    body = (f"{config.GREETING}\n\n🏪 *{s['store_name']}* · 🕒 {s['store_hours']} · 🛵 {s['delivery_eta']}\n\n"
            if greet else "")
    if s.get("welcome_message"):
        body += s["welcome_message"].strip() + "\n\n"
    examples = store.example_names(3)
    if examples and greet:
        body += "Just type what you need — e.g. " + ", ".join(f"_{x}_" for x in examples) + " — or open the menu 👇"
    elif examples:
        body += ("*What would you like to buy?*\nJust type a product name — e.g. "
                 + ", ".join(f"_{x}_" for x in examples) + " — or open the menu 👇")
    else:
        body += "🛍️ We're still adding our products here — please check back soon!\nYou can track past orders or message us from the menu 👇"
    rows = [
        {"id": "m:browse", "title": "📂 Browse categories", "description": "See everything we sell"},
        {"id": "m:cart", "title": "🧺 My cart",
         "description": f"{cart['item_count']} items · {store.money(cart['total'])}" if cart["items"] else "Empty"},
        {"id": "m:orders", "title": "📦 My orders", "description": "Track, cancel or reorder"},
        {"id": "m:store", "title": "💬 Talk to the store", "description": "Chat with our staff"},
    ]
    shop = db.get_shop()
    if c["channel"] == "whatsapp" and not shop["wa_phone_number_id"] and tenancy.has_many_shops():
        rows.append({"id": "m:switch", "title": "🏪 Switch shop", "description": "Order from another shop"})
    _save(c["id"], "search")
    await _list(c, body, "Menu", rows, "Main menu")


async def categories(c):
    rows = [{"id": f"cat:{r['category']}", "title": r["category"],
             "description": f"{r['products']} items · " + ", ".join((r["examples"] or "").split(",")[:3])}
            for r in store.categories()][:10]
    _save(c["id"], "search")
    if not rows:
        return await _buttons(c, "🛍️ Nothing is in stock right now — please check back soon!",
                              [("m:store", "💬 Talk to store"), ("m:menu", "🏠 Menu")])
    await _list(c, "📂 *Pick a category*\n(or just type what you're looking for)", "Categories", rows, "Categories")


async def show_products(c, products: list[dict], heading: str, offset: int = 0):
    available = [p for p in products if p["active"] and p["available"] > 0]
    sold_out = [store.product_label(p) for p in products if p["available"] <= 0]
    if not available:
        await _buttons(c, f"{heading}\n\n😕 Sorry, {', '.join(sold_out) or 'these items'} "
                          f"{'is' if len(sold_out) == 1 else 'are'} out of stock right now.\nTry another product name.",
                       [("m:browse", "📂 Categories"), ("m:menu", "🏠 Menu")])
        _save(c["id"], "search")
        return
    data = {"results": [p["id"] for p in available], "heading": heading}
    if len(available) == 1:
        return await ask_quantity(c, available[0]["id"], data)

    page = available[:MAX_PICK]
    body = heading + "\n\n" + "\n".join(f"• {store.product_label(p)} — {store.money(p['price'])}" for p in page)
    if sold_out:
        body += f"\n\n❌ Out of stock: {', '.join(sold_out)}"
    body += "\n\n👉 Tap *Choose items* and tick everything you want."
    data["page"] = [p["id"] for p in page]
    _save(c["id"], "pick_product", data)
    options = [{"id": str(p["id"]), "title": _ptitle(p, 30), "description": _pdesc(p)} for p in page]
    fallback_rows = [{"id": f"p:{p['id']}", "title": _ptitle(p), "description": _pdesc(p)} for p in page[:10]]
    await messaging.send_to_customer(c, body, "agent", checklist={
        "button": "Choose items", "heading": re.sub(r"[*_]", "", heading)[:80], "options": options,
        "fallback": {"button": "Choose item", "sections": [{"title": "Available now", "rows": fallback_rows}]}})


async def search_and_show(c, text: str):
    products, intent = await find_products(text)
    if not products:
        _save(c["id"], "search")
        if intent == "greeting":
            return await main_menu(c, greet=True)
        if c["channel"] == "whatsapp" and tenancy.has_many_shops():
            from . import marketplace          # not here? offer the shops that do have it
            if await marketplace.offer_elsewhere(c, text):
                return
        if not store.available_count():
            return await _buttons(c, "🛍️ This shop hasn't added any products yet — please check back soon!",
                                  [("m:store", "💬 Talk to store"), ("m:menu", "🏠 Menu")])
        examples = store.example_names(3)
        await _buttons(c, f"😕 Sorry, we couldn't find *{text.strip()[:60]}*.\n"
                          f"Try another name{' (e.g. ' + ', '.join(examples) + ')' if examples else ''} or browse our categories.",
                       [("m:browse", "📂 Categories"), ("m:menu", "🏠 Menu")])
        return
    where = f" at *{db.get_shop()['name']}*" if c["channel"] == "whatsapp" and tenancy.has_many_shops() else ""
    await show_products(c, products, f"🔍 Results for *{text.strip()[:60]}*{where}")


async def ask_quantity(c, product_id: int, data: dict):
    p = store.get_product(product_id)
    if not p or not p["active"]:
        await _text(c, "😕 That item is no longer available.")
        return await main_menu(c, greet=False)
    if p["available"] <= 0:
        return await search_and_show(c, p["name"])
    data["product_id"] = product_id
    _save(c["id"], "qty", data)
    left = f"\n_Only {p['available']} left_" if p["available"] <= 5 else ""
    at = f"🏪 *{db.get_shop()['name']}*\n" if c["channel"] == "whatsapp" and tenancy.has_many_shops() else ""
    rows = [{"id": f"q:{n}", "title": str(n), "description": store.money(p["price"] * n)}
            for n in QTY_CHOICES if n <= p["available"]]
    await _list(c, f"{at}*{store.product_label(p)}*\n💰 {store.money(p['price'])} each{left}\n\nHow many would you like?",
                "Choose quantity", rows, "Quantity")


async def add_to_cart(c, data: dict, qty: int):
    pid = data.get("product_id")
    if not pid:
        return await main_menu(c, greet=False)
    r = store.set_cart_qty(c["id"], pid, qty, add=True)
    if not r["ok"]:
        await _text(c, f"⚠️ {r['error']}" + (f"\nYou can order up to {r['max_quantity']}." if r.get("max_quantity") else ""))
        return await ask_quantity(c, pid, data)
    cart = r["cart"]
    _save(c["id"], "added", {k: data[k] for k in ("results", "page", "heading") if k in data})
    hint = f"\n_💡 {cart['free_delivery_hint']}_" if cart.get("free_delivery_hint") else ""
    await _buttons(c, f"✅ Added *{qty} × {r['item']}*\n🧺 Cart: {cart['item_count']} items · *{store.money(cart['total'])}*{hint}",
                   [("cart:more", "➕ Add more"), ("m:cart", "🧺 View cart"), ("cart:checkout", "✅ Checkout")])


def parse_selection(text: str) -> list[tuple[int, int]] | None:
    """'1 3 4' → [(1,1),(3,1),(4,1)];  '1x2, 3x5' → [(1,2),(3,5)].  None if the text isn't a selection."""
    if not SELECTION.match(text):
        return None
    return [(int(i), int(q or 1)) for i, q in re.findall(r"(\d{1,2})(?:\s*[x×*]\s*(\d{1,3}))?", text)]


async def add_many(c, data: dict, picks: list[tuple[int, int]]):
    page = data.get("page", [])
    added, problems = [], []
    for idx, qty in picks:
        if not 1 <= idx <= len(page):
            problems.append(f"#{idx} isn't in the list")
            continue
        r = store.set_cart_qty(c["id"], page[idx - 1], qty, add=True)
        if r["ok"]:
            added.append(f"• {qty} × {r['item']}")
        else:
            problems.append(f"#{idx}: {r['error']}")
    cart = store.cart(c["id"])
    body = ("✅ *Added to cart:*\n" + "\n".join(added)) if added else "😕 Nothing was added."
    if problems:
        body += "\n\n⚠️ " + "\n⚠️ ".join(problems)
    if cart["items"]:
        body += f"\n\n🧺 Cart: {cart['item_count']} items · *{store.money(cart['total'])}*"
        if cart.get("free_delivery_hint"):
            body += f"\n_💡 {cart['free_delivery_hint']}_"
    _save(c["id"], "added", {k: data[k] for k in ("results", "page", "heading") if k in data})
    await _buttons(c, body, [("cart:more", "➕ Add more"), ("m:cart", "🧺 View cart"), ("cart:checkout", "✅ Checkout")])


async def add_more(c, data: dict):
    results = [store.get_product(i) for i in data.get("results", [])]
    results = [p for p in results if p]
    if len(results) > 1:
        return await show_products(c, results, "➕ Pick more from your last search — or type a new product name 🔍")
    _save(c["id"], "search")
    examples = store.example_names(2)
    await _buttons(c, "🔍 Type the next product you need" + (f" (e.g. {', '.join(f'_{x}_' for x in examples)})" if examples else ""),
                   [("m:browse", "📂 Categories"), ("m:cart", "🧺 View cart")])


async def show_cart(c):
    cart = store.cart(c["id"])
    if not cart["items"]:
        _save(c["id"], "search")
        return await _buttons(c, "🧺 Your cart is empty.\nType a product name to start shopping 🔍",
                              [("m:browse", "📂 Categories"), ("m:menu", "🏠 Menu")])
    _save(c["id"], "cart")
    await _buttons(c, "🧺 *Your cart*\n\n" + _cart_lines(cart),
                   [("cart:checkout", "✅ Checkout"), ("cart:more", "➕ Add more"), ("cart:edit", "✏️ Edit cart")])


async def edit_cart(c):
    cart = store.cart(c["id"])
    if not cart["items"]:
        return await show_cart(c)
    rows = [{"id": f"e:{i['product_id']}", "title": i["item"][:24],
             "description": f"Qty {i['quantity']} · {store.money(i['line_total'])} — tap to change"} for i in cart["items"][:9]]
    rows.append({"id": "rm:all", "title": "🗑️ Clear whole cart"})
    await _list(c, "✏️ *Edit cart*\nPick an item to change its quantity or remove it.", "Edit items", rows, "Cart items")


async def edit_item(c, product_id: int):
    item = next((i for i in store.cart(c["id"])["items"] if i["product_id"] == product_id), None)
    if not item:
        return await show_cart(c)
    p = store.get_product(product_id)
    rows = [{"id": f"sq:{product_id}:{n}", "title": str(n), "description": store.money(item["unit_price"] * n)}
            for n in QTY_CHOICES[:9] if n <= max(p["available"], item["quantity"])]
    rows.append({"id": f"sq:{product_id}:0", "title": "❌ Remove from cart"})
    await _list(c, f"*{item['item']}*\nIn cart: {item['quantity']}. Choose the new quantity.", "Set quantity", rows, "Quantity")


async def add_selected(c, data: dict, product_ids: list[int]):
    """Customer ticked several items in the checkbox picker → add 1 of each."""
    added, problems = [], []
    for pid in product_ids:
        r = store.set_cart_qty(c["id"], pid, 1, add=True)
        (added.append(f"• 1 × {r['item']}") if r["ok"] else problems.append(r["error"]))
    cart = store.cart(c["id"])
    body = ("✅ *Added to cart:*\n" + "\n".join(added)) if added else "😕 Nothing was added."
    if problems:
        body += "\n\n⚠️ " + "\n⚠️ ".join(problems)
    if cart["items"]:
        body += f"\n\n🧺 Cart: {cart['item_count']} items · *{store.money(cart['total'])}*"
        if cart.get("free_delivery_hint"):
            body += f"\n_💡 {cart['free_delivery_hint']}_"
        body += "\n\nNeed more than 1 of something? Tap *✏️ Change qty*."
    _save(c["id"], "added", {k: data[k] for k in ("results", "page", "heading") if k in data})
    await _buttons(c, body, [("cart:edit", "✏️ Change qty"), ("cart:more", "➕ Add more"), ("cart:checkout", "✅ Checkout")])


async def checkout(c):
    cart = store.cart(c["id"])
    if not cart["items"] or cart.get("warnings"):
        return await show_cart(c)
    addr = _address(c)
    if addr:
        _save(c["id"], "address_choice")
        return await _buttons(c, f"🚚 *Delivery address*\n{addr}\n\nDeliver here?",
                              [("addr:saved", "📍 Yes, deliver here"), ("addr:new", "✏️ New address")])
    await ask_address(c)


async def ask_address(c):
    _save(c["id"], "address")
    await _text(c, "📝 Please type your *delivery address*\n(house/flat no., street, area, landmark)\n\n"
                   "or share your 📍 *location* (📎 → Location).")


async def ask_payment(c):
    _save(c["id"], "payment")
    await _buttons(c, "💳 *How would you like to pay?*",
                   [("pay:COD", "💵 Cash on Delivery"), ("pay:UPI", "📲 UPI / GPay")])


async def review(c, method: str):
    c = db.get_customer(c["id"])
    r = store.review_checkout(c, method)
    if not r["ok"]:
        await _text(c, f"⚠️ {r['error'].split(' – ')[0]}")
        return await (ask_address(c) if "address" in r["error"].lower() else show_cart(c))
    s = db.get_settings()
    pay = "💵 Cash on Delivery" if method == "COD" else "📲 UPI (payment link after confirming)"
    body = (f"🧾 *Order summary*\n\n{_cart_lines(r)}\n\n"
            f"👤 {r['deliver_to']['name'] or ''}\n📍 {_address(c)}\n💳 {pay}\n🕒 Delivery in {s['delivery_eta']}\n\n"
            "*Confirm your order?*")
    _save(c["id"], "confirm", {"payment": method})
    await _buttons(c, body, [("ord:confirm", "✅ Confirm order"), ("cart:edit", "✏️ Edit cart"), ("ord:cancel", "❌ Cancel")])


async def confirm(c):
    r = store.place_order(db.get_customer(c["id"]))
    if not r["ok"]:
        await _text(c, f"⚠️ {r['error']}")
        return await show_cart(c)
    o = r["order"]
    body = (f"🎉 *Order placed!*\n\nOrder ID: *{o['code']}*\nTotal: *{store.money(o['total'])}* "
            f"({'Cash on Delivery' if o['payment_method'] == 'COD' else 'UPI'})\n\n"
            "⏳ Waiting for the store to accept. You'll get updates here:\n"
            "Accepted → Packed → Out for delivery → Delivered")
    if o["payment_method"] == "UPI":
        body += f"\n\n💳 *Pay now:* {config.PUBLIC_BASE_URL}/pay/{o['code']}"
    else:
        body += "\n\n💵 Please pay cash or UPI at delivery."
    _save(c["id"], "idle")
    await _buttons(c, body, [(f"o:{o['code']}", "📦 Track order"), ("m:menu", "🏠 Menu")])


async def my_orders(c):
    orders = store.customer_orders(c["id"], 10)
    if not orders:
        return await _buttons(c, "📦 You haven't placed any orders yet.\nType a product name to start shopping 🔍",
                              [("m:browse", "📂 Categories"), ("m:menu", "🏠 Menu")])
    rows = [{"id": f"o:{o['code']}", "title": f"{o['code']} · {store.money(o['total'])}",
             "description": f"{STATUS_LABEL.get(o['status'], o['status'])} · {o['created_at'][:10]}"} for o in orders]
    await _list(c, "📦 *Your orders*\nPick one to track, cancel or reorder.", "View orders", rows, "Recent orders")


async def order_detail(c, code: str):
    o = store.order_by_code(c["id"], code)
    if not o:
        return await my_orders(c)
    items = "\n".join(f"• {i['quantity']} × {i['name']} ({i['variant']}) — {store.money(i['line_total'])}" for i in o["items"])
    body = (f"📦 *Order {o['code']}*\nStatus: *{STATUS_LABEL.get(o['status'], o['status'])}*\n\n{items}\n\n"
            f"*Total: {store.money(o['total'])}* · {o['payment_method']} ({o['payment_status'].replace('_', ' ')})\n"
            f"🕒 Placed {o['created_at'].replace('T', ' ')[:16]}")
    if o["status"] in ("accepted", "packed", "out_for_delivery") and o["eta"]:
        body += f" · ETA {o['eta']}"
    if o["reject_reason"]:
        body += f"\nNote: {o['reject_reason']}"
    if o["payment_method"] == "UPI" and o["payment_status"] == "pending" and o["status"] not in ("rejected", "cancelled"):
        body += f"\n\n💳 Pay: {config.PUBLIC_BASE_URL}/pay/{o['code']}"
    buttons = []
    if o["status"] in ("pending", "accepted"):
        buttons.append((f"oc:{o['code']}", "❌ Cancel order"))
    buttons += [(f"or:{o['code']}", "🔁 Reorder"), ("m:menu", "🏠 Menu")]
    await _buttons(c, body, buttons)


async def ask_cancel(c, code: str):
    await _buttons(c, f"Cancel order *{code}*?", [(f"ocy:{code}", "Yes, cancel it"), (f"o:{code}", "No, keep it")])


async def cancel_order(c, code: str):
    o = store.order_by_code(c["id"], code)
    if not o or o["status"] not in ("pending", "accepted"):
        await _text(c, "⚠️ This order can't be cancelled any more — it's already on its way. Use *Talk to the store*.")
        return await main_menu(c, greet=False)
    store.change_status(o["id"], "cancelled", "customer", "Cancelled by customer on WhatsApp")
    await _buttons(c, f"🛑 Order *{code}* has been cancelled.", [("m:menu", "🏠 Menu")])


async def reorder(c, code: str):
    o = store.order_by_code(c["id"], code)
    if not o:
        return await my_orders(c)
    skipped = []
    for i in o["items"]:
        r = store.set_cart_qty(c["id"], i["product_id"], i["quantity"], add=True)
        if not r["ok"]:
            skipped.append(f"{i['name']} ({i['variant']})")
    if skipped:
        await _text(c, "⚠️ Not available right now: " + ", ".join(skipped))
    await show_cart(c)


async def talk_to_store(c):
    db.run("UPDATE customers SET needs_attention=1 WHERE id=?", (c["id"],))
    db.add_message(c["id"], "in", "system", "⚠️ Customer asked to talk to the store", kind="alert")
    events.publish("attention", {"customer_id": c["id"], "phone": c["phone"], "message": "wants to talk to the store"})
    notify.add("customer_message", f"💬 {c.get('name') or c.get('wa_name') or c['phone']} wants to talk",
               "Reply from Customer Chats in the portal.", customer_id=c["id"])
    _save(c["id"], "store_chat")
    await _text(c, "💬 You're now chatting with our store team. Type your message and they'll reply here.\n"
                   "Type *menu* anytime to go back.")


# ---------------- dispatcher ----------------

async def _on_tap(c, state, data, rid: str):
    head, _, arg = rid.partition(":")
    if rid == "m:menu":
        return await main_menu(c, greet=False)
    if rid == "m:browse":
        return await categories(c)
    if rid == "m:cart":
        return await show_cart(c)
    if rid == "m:orders":
        return await my_orders(c)
    if rid == "m:store":
        return await talk_to_store(c)
    if rid == "m:switch":
        tenancy.forget(c["phone"])
        _save(c["id"], "idle")
        return await tenancy.send_shop_picker(c["phone"], "🔄 Sure — pick a shop.")
    if head == "cat":
        return await show_products(c, store.products_in_category(arg), f"📂 *{arg}*")
    if head == "more":
        products = [p for p in (store.get_product(i) for i in data.get("results", [])) if p]
        return await show_products(c, products, data.get("heading") or "More items", int(arg))
    if head == "p":
        return await ask_quantity(c, int(arg), data)
    if head == "multi":
        ids = [int(x) for x in arg.split(",") if x.strip().isdigit()]
        return await (add_selected(c, data, ids) if ids else show_products(
            c, [p for p in (store.get_product(i) for i in data.get("results", [])) if p], data.get("heading") or "Choose items"))
    if head == "e":
        return await edit_item(c, int(arg))
    if head == "sq":
        pid, _, qty = arg.partition(":")
        r = store.set_cart_qty(c["id"], int(pid), int(qty))
        if not r["ok"]:
            await _text(c, f"⚠️ {r['error']}")
        return await show_cart(c)
    if head == "q":
        return await add_to_cart(c, data, int(arg))
    if rid == "cart:more":
        return await add_more(c, data)
    if rid == "cart:checkout":
        return await checkout(c)
    if rid == "cart:edit":
        return await edit_cart(c)
    if head == "rm":
        if arg == "all":
            store.clear_cart(c["id"])
        else:
            store.set_cart_qty(c["id"], int(arg), 0)
        return await show_cart(c)
    if rid == "addr:saved":
        return await ask_payment(c)
    if rid == "addr:new":
        return await ask_address(c)
    if head == "pay":
        return await review(c, arg)
    if rid == "ord:confirm":
        return await confirm(c)
    if rid == "ord:cancel":
        _save(c["id"], "search")
        return await _buttons(c, "👍 Okay, order not placed. Your cart is saved.", [("m:cart", "🧺 View cart"), ("m:menu", "🏠 Menu")])
    if head == "o":
        return await order_detail(c, arg)
    if head == "oc":
        return await ask_cancel(c, arg)
    if head == "ocy":
        return await cancel_order(c, arg)
    if head == "or":
        return await reorder(c, arg)
    return await main_menu(c, greet=False)


async def _on_text(c, state, data, text: str):
    t = _norm(text)
    first_contact = not db.one("SELECT 1 FROM messages WHERE customer_id=? AND direction='out' LIMIT 1", (c["id"],))
    if t in GREETINGS or (first_contact and len(t.split()) <= 2 and not store.search_products(t, limit=1)):
        return await main_menu(c, greet=True)
    if state == "store_chat":
        events.publish("attention", {"customer_id": c["id"], "phone": c["phone"], "message": text[:200]})
        if not data.get("acked"):
            _save(c["id"], "store_chat", {"acked": True})
            await _text(c, "✅ Sent to the store team. They'll reply shortly.")
        return
    if t in ("cart", "my cart", "basket", "view cart"):
        return await show_cart(c)
    if t in ("checkout", "order now", "buy", "place order") and state not in ("confirm",):
        return await checkout(c)
    if t in ("orders", "my orders", "track", "track order", "status", "order status", "where is my order"):
        latest = store.customer_orders(c["id"], 1)
        return await (order_detail(c, latest[0]["code"]) if latest else my_orders(c))

    # several numbers ("1 3 4") or any "AxB" quantity ("2x3") = multi-select from the last list;
    # a single plain number keeps its meaning (item number in a list, quantity on the quantity step)
    picks = parse_selection(text) if state in ("pick_product", "added", "qty") and data.get("page") else None
    if picks and (len(picks) > 1 or re.search(r"[x×*]", text, re.I)):
        return await add_many(c, data, picks)
    if state == "added" and t.isdigit() and data.get("page"):
        n = int(t)
        if 1 <= n <= len(data["page"]):
            return await ask_quantity(c, data["page"][n - 1], data)
    if state == "qty":
        m = re.match(r"^\s*(\d{1,3})\b", text)
        if m:
            return await add_to_cart(c, data, int(m.group(1)))
    if state == "address_choice":
        if t in YES:
            return await ask_payment(c)
        if len(text.strip()) < 10:
            return await checkout(c)          # re-show "deliver here?" buttons
    if state in ("address", "address_choice"):
        if len(text.strip()) < 10:
            return await _text(c, "🙏 That address looks too short. Please include house/flat no., street and area.")
        db.run("UPDATE customers SET address=?, name=COALESCE(name, wa_name) WHERE id=?", (text.strip()[:300], c["id"]))
        await _text(c, "✅ Address saved.")
        return await ask_payment(c)
    if state == "payment":
        if any(w in t for w in ("cod", "cash")):
            return await review(c, "COD")
        if any(w in t for w in ("upi", "gpay", "google pay", "phonepe", "paytm", "online")):
            return await review(c, "UPI")
        return await ask_payment(c)
    if state == "confirm":
        if t in YES:
            return await confirm(c)
        if t in NO:
            return await _on_tap(c, state, data, "ord:cancel")
        return await review(c, data.get("payment", "COD"))
    if state == "pick_product" and t.isdigit():
        page = data.get("page", [])
        n = int(t)
        if 1 <= n <= len(page):
            return await ask_quantity(c, page[n - 1], data)
    return await search_and_show(c, text)


async def _on_location(c, state):
    if state in ("address", "address_choice"):
        await _text(c, "✅ Location saved. Tip: you can also type your flat/house number if it's an apartment.")
        return await ask_payment(c)
    if store.cart(c["id"])["items"]:
        return await _buttons(c, "📍 Location saved for delivery.", [("cart:checkout", "✅ Checkout"), ("m:cart", "🧺 View cart")])
    await _text(c, "📍 Location saved for delivery. Now type a product name to start shopping 🔍")


def _allowed_while_closed(state: str, reply_id: str | None, text: str | None) -> bool:
    """While the store is closed customers can still track/cancel orders and talk to staff — but not shop."""
    if reply_id:
        return reply_id in ("m:orders", "m:store", "m:switch") or reply_id.split(":")[0] in ("o", "oc", "ocy")
    t = _norm(text)
    return state == "store_chat" or t in ("orders", "my orders", "track", "track order", "status", "order status",
                                          "where is my order")


async def _closed_notice(c):
    s = db.get_settings()
    await _buttons(c, f"🔴 *{s['store_name']}* isn't taking orders right now.\nReason: {store.closed_reason()}\n\n"
                      f"We'll be back soon 🙏 You can still track your orders or message us.",
                   [("m:orders", "📦 My orders"), ("m:store", "💬 Talk to store")]
                   + ([("m:switch", "🏪 Switch shop")]
                      if c["channel"] == "whatsapp" and not db.get_shop()["wa_phone_number_id"] and tenancy.has_many_shops()
                      else []))


async def handle(customer_id: int, text: str | None, reply_id: str | None = None, kind: str = "text"):
    lock = _locks.setdefault(customer_id, asyncio.Lock())
    async with lock:
        c = db.get_customer(customer_id)
        if c["bot_paused"]:
            return
        state, data = _state(c)
        try:
            if not store.is_open() and not _allowed_while_closed(state, reply_id, text):
                return await _closed_notice(c)
            if kind == "location":
                await _on_location(c, state)
            elif reply_id:
                await _on_tap(c, state, data, reply_id)
            elif text and text.strip():
                await _on_text(c, state, data, text)
            else:
                await _text(c, "🙏 Sorry, I can only read text, voice notes and locations here. Type *menu* to start.")
        except Exception:
            log.exception("flow error for customer %s", customer_id)
            await _text(c, "⚠️ Something went wrong on our side. Type *menu* to start again.")
