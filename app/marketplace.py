"""Product-first ordering on the shared WhatsApp number.

The customer says what they want; we look in every open shop:
  • found in one shop        → straight into that shop's product picker
  • found in several shops   → "which shop?" list (matches, lowest price, city) → that shop's picker
  • found nowhere            → say so, offer to browse shops
While a customer is mid-order (cart not empty, or in checkout) they stay with their current shop.
"""
import logging
import re

from . import config, db, flow, store, tenancy, understand, whatsapp

log = logging.getLogger("marketplace")

# steps of the shop flow that must not be interrupted by a cross-shop search
STAY_STATES = {"qty", "address", "address_choice", "payment", "confirm", "store_chat"}
SHOP_COMMANDS = {"cart", "my cart", "basket", "view cart", "checkout", "order now", "buy", "place order", "orders",
                 "my orders", "track", "track order", "status", "order status", "where is my order", "cod", "cash", "upi",
                 "gpay", "phonepe", "paytm"} | flow.YES | flow.NO


def _open_shops() -> list[dict]:
    shops = []
    for s in tenancy.active_shops():
        is_open = db.one("SELECT value FROM shop_settings WHERE shop_id=? AND key='is_open'", (s["id"],))
        if not is_open or is_open["value"] != "0":
            shops.append(s)
    return shops


def _in_shop(shop_id: int, fn, *args):
    token = db.use_shop(shop_id)
    try:
        return fn(*args)
    finally:
        db._current_shop.reset(token)


def _search_everywhere(query: str) -> list[tuple[dict, list[dict]]]:
    found = []
    for shop in _open_shops():
        hits = [p for p in _in_shop(shop["id"], store.search_products, query, None, 10) if p["available"] > 0]
        if hits:
            found.append((shop, hits))
    if found:
        # drop shops whose best match is much weaker than the best overall
        # ("something cold to drink": Coca-Cola matches "cold"+"drink", Vicks only "cold")
        best = max(p["_score"] for _, hits in found for p in hits)
        found = [(shop, [p for p in hits if p["_score"] >= best * 0.6]) for shop, hits in found]
        found = [(shop, hits) for shop, hits in found if hits]
    return found


async def _find(text: str) -> tuple[list[tuple[dict, list[dict]]], str]:
    """→ ([(shop, products)…], intent). Database first across open shops; the AI only when that finds nothing."""
    parts = flow.split_items(text)
    if len(parts) <= 1:
        results = _search_everywhere(text)
        if results:
            return results, "order"
    else:                                     # a list of items: look each one up in every shop
        merged = _merge([_search_everywhere(part) for part in parts])
        if merged is not None:
            return merged, "order"
    shops = _open_shops()
    vocabulary = sorted({name for s in shops for name in _in_shop(s["id"], store.catalogue_vocabulary, 80)})
    meaning = await understand.understand(text, vocabulary, "local shops on this WhatsApp number", generic=True)
    by_shop: dict[int, tuple[dict, dict]] = {}
    for term in meaning["items"]:
        for shop, hits in _search_everywhere(term):
            entry = by_shop.setdefault(shop["id"], (shop, {}))
            for p in hits:
                entry[1][p["id"]] = p
    return [(shop, list(hits.values())) for shop, hits in by_shop.values()], meaning["intent"]


def _merge(per_item: list[list[tuple[dict, list[dict]]]]) -> list[tuple[dict, list[dict]]] | None:
    """Combine per-item search results by shop. None if some item wasn't found anywhere (→ ask the AI)."""
    if not per_item or any(not r for r in per_item):
        return None
    by_shop: dict[int, tuple[dict, dict]] = {}
    for results in per_item:
        for shop, hits in results:
            entry = by_shop.setdefault(shop["id"], (shop, {}))
            for p in hits:
                entry[1][p["id"]] = p
    return [(shop, list(hits.values())) for shop, hits in by_shop.values()]


def _shop_rows(text: str, results: list[tuple[dict, list[dict]]]) -> list[dict]:
    results = sorted(results, key=lambda r: min(p["price"] for p in r[1]))[:10]
    return [{"id": f"mk:shop:{shop['id']}:{text[:80]}", "title": shop["name"][:24],
             "description": " · ".join(x for x in (
                 f"{len(hits)} match{'es' if len(hits) > 1 else ''}", f"from {store.money(min(p['price'] for p in hits))}",
                 shop.get("city")) if x)[:72]}
            for shop, hits in results]


async def offer_elsewhere(c: dict, text: str) -> bool:
    """Called from inside a shop that doesn't sell the item: list the other shops that do. → True if offered."""
    here = db.shop_id()
    results, _ = await _find(text)
    results = [(shop, hits) for shop, hits in results if shop["id"] != here]
    if not results:
        return False
    cart = store.cart(c["id"])
    body = f"🔎 *{db.get_shop()['name']}* doesn't have *{text.strip()[:60]}*, but {'this shop does' if len(results) == 1 else f'{len(results)} other shops do'} 👇"
    if cart["items"]:
        n = cart["item_count"]
        body += f"\n\n🧺 Your cart here ({n} item{'s' if n != 1 else ''}) is saved — you can come back to it anytime."
    from . import messaging
    await messaging.send_to_customer(c, body, "agent", menu={
        "button": "Choose shop", "sections": [{"title": "Shops with this item", "rows": _shop_rows(text, results)}]})
    return True


# ---------------- replies sent before a shop is chosen (shared-number credentials) ----------------

async def _send(fn, *args):
    token = db.use_shop(None)
    try:
        await fn(*args)
    finally:
        db._current_shop.reset(token)


def _examples(n: int = 3) -> list[str]:
    names = []
    for shop in _open_shops():
        names += _in_shop(shop["id"], store.example_names, 2)
    seen, out = set(), []
    for name in names:
        if name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out[:n]


async def welcome(phone: str, current: int | None):
    body = config.GREETING
    buttons = [("mk:browse", "🏪 Browse shops")]
    shop = db.get_shop(current) if current else None
    if shop:
        buttons.append((f"mk:cont:{shop['id']}", f"↩️ {shop['name']}"[:20]))
    await _send(whatsapp.send_buttons, phone, body, buttons)


async def _not_found(phone: str, text: str):
    examples = _examples()
    await _send(whatsapp.send_buttons, phone,
                f"😕 Sorry, none of our shops has *{text[:60]}* right now."
                + (f"\nTry something like {', '.join(examples)}." if examples else ""),
                [("mk:browse", "🏪 Browse shops")])


async def _choose_shop(phone: str, text: str, results: list[tuple[dict, list[dict]]]):
    await _send(whatsapp.send_list, phone,
                f"🔎 *{text[:60]}* is available at {len(results)} shops.\nWhich shop would you like to order from?",
                "Choose shop", [{"title": "Shops with this item", "rows": _shop_rows(text, results)}])


# ---------------- routing ----------------

def _stay_with(current: int | None, phone: str, t: str) -> bool:
    """Keep the conversation in the current shop (mid-order, checkout steps, shop commands, number replies)."""
    if not current:
        return False
    c = db.one("SELECT id, flow_state FROM customers WHERE shop_id=? AND phone=?", (current, phone))
    if not c:
        return False
    cart_items = db.one("SELECT COUNT(*) AS n FROM cart_items WHERE customer_id=?", (c["id"],))["n"]
    return bool(cart_items) or (c["flow_state"] in STAY_STATES) or t in SHOP_COMMANDS or bool(re.fullmatch(r"[\d\s,x×*]+", t))


async def route(m: dict, current: int | None) -> tuple[int, dict] | None:
    """→ (shop_id, message for that shop's flow), or None when we already replied (welcome / shop choice / not found)."""
    phone = m["from"]
    rid = m.get("reply_id") or ""
    text = (m.get("text") or "").strip()
    t = flow._norm(text)

    if rid.startswith("mk:"):
        parts = rid.split(":", 3)
        if parts[1] == "browse":
            await _send(tenancy.send_shop_picker, phone, "🏪 Here are the shops on this number.")
            return None
        if parts[1] in ("shop", "cont") and len(parts) >= 3 and parts[2].isdigit():
            sid = int(parts[2])
            if not db.one("SELECT 1 FROM shops WHERE id=? AND active=1", (sid,)):
                await welcome(phone, None)
                return None
            tenancy.remember(phone, sid)
            query = parts[3] if parts[1] == "shop" and len(parts) > 3 else "menu"
            return sid, {**m, "type": "text", "reply_id": None, "text": query}
    if rid or m["type"] != "text" or not text:
        if current:
            return current, m          # taps inside the shop's menus, locations, voice notes…
        await welcome(phone, None)
        return None
    if _stay_with(current, phone, t):
        return current, m
    if t in flow.GREETINGS:
        await welcome(phone, current)
        return None

    results, intent = await _find(text)
    if not results and intent == "greeting":
        await welcome(phone, current)
        return None
    if not results:
        if current:
            return current, m          # let the current shop answer ("not found here", its menu…)
        await _not_found(phone, text)
        return None
    if len(results) == 1:
        sid = results[0][0]["id"]
        tenancy.remember(phone, sid)
        return sid, m
    await _choose_shop(phone, text, results)
    return None
