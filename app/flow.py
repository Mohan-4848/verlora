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

from . import config, db, events, i18n, messaging, notify, store, tenancy, understand

log = logging.getLogger("flow")
_locks: dict[int, asyncio.Lock] = {}

GREETINGS = i18n.GREETINGS_I18N
YES = i18n.YES_I18N
NO = i18n.NO_I18N
MULTI_ITEM = re.compile(r",|\n|&|\+|\b(and|aur|or|inka|mariyu|also|plus)\b|మరియు|ఇంకా|और", re.I)
# splits "2 milk and 1 bread", "milk, eggs, bread", "doodh aur bread", "పాలు మరియు బ్రెడ్" into separate items
SPLIT_ITEMS = re.compile(r"\s*(?:,|\n|&|\+|\band\b|\baur\b|\binka\b|\bmariyu\b|\balso\b|\bplus\b|మరియు|ఇంకా|और)\s*", re.I)


def split_items(text: str) -> list[str]:
    return [p.strip() for p in SPLIT_ITEMS.split(text or "") if p and p.strip()]


PAGE = 9   # list rows per page (WhatsApp max is 10; the 10th is "More items")
MAX_PICK = 20   # CheckboxGroup option limit in a WhatsApp Flow
QTY_CHOICES = [1, 2, 3, 4, 5, 6, 8, 10, 12, 15]
# "1 3 4", "1,3", "1x2 3x5", "2 x 3 and 4" — several items picked by number from the last list
SELECTION = re.compile(r"^\s*\d{1,2}(\s*[x×*]\s*\d{1,3})?((\s*(,|&|and|\s)\s*)\d{1,2}(\s*[x×*]\s*\d{1,3})?)*\s*$", re.I)


def _norm(t: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (t or "").lower())).strip()


def _lang(c: dict) -> str:
    return i18n.normalize_language(c.get("language"))


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


def _pdesc(p: dict, lang: str = "en") -> str:
    parts = [store.money(p["price"]), p.get("variant") or "", p.get("brand") or ""]
    if 0 < p["available"] <= 5:
        parts.append(i18n.t("only_n_left", lang, n=p["available"]))
    return " · ".join(x for x in parts if x)


def _cart_lines(cart: dict, lang: str = "en") -> str:
    lines = [f"• {i['quantity']} × {i['item']} — {store.money(i['line_total'])}" for i in cart["items"]]
    fee = store.money(cart["delivery_fee"]) if cart["delivery_fee"] else i18n.t("free", lang)
    subtotal_lbl = i18n.t("subtotal", lang)
    delivery_lbl = i18n.t("delivery", lang)
    total_lbl = i18n.t("total", lang)
    out = "\n".join(lines) + f"\n\n{subtotal_lbl}: {store.money(cart['subtotal'])}\n{delivery_lbl}: {fee}\n*{total_lbl}: {store.money(cart['total'])}*"
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

async def show_language_picker(c):
    lang = _lang(c)
    _save(c["id"], "search")
    await _buttons(c, i18n.t("choose_language_prompt", lang), i18n.language_picker_buttons())


async def main_menu(c, greet=True):
    lang = _lang(c)
    s = db.get_settings()
    cart = store.cart(c["id"])
    body = (f"{config.GREETING}\n\n🏪 *{s['store_name']}* · 🕒 {s['store_hours']} · 🛵 {s['delivery_eta']}\n\n"
            if greet else "")
    if s.get("welcome_message"):
        body += s["welcome_message"].strip() + "\n\n"
    examples = store.example_names(3)
    if examples and greet:
        body += i18n.t("greeting_hint_examples", lang, examples=", ".join(f"_{x}_" for x in examples))
    elif examples:
        body += i18n.t("prompt_what_to_buy", lang, examples=", ".join(f"_{x}_" for x in examples))
    else:
        body += i18n.t("greeting_no_products", lang)

    cart_desc = (i18n.t("menu_cart_filled", lang, count=cart['item_count'], total=store.money(cart['total']))
                 if cart["items"] else i18n.t("menu_cart_empty", lang))
    rows = [
        {"id": "m:browse", "title": i18n.t("menu_browse_title", lang), "description": i18n.t("menu_browse_desc", lang)},
        {"id": "m:cart", "title": i18n.t("menu_cart_title", lang), "description": cart_desc},
        {"id": "m:orders", "title": i18n.t("menu_orders_title", lang), "description": i18n.t("menu_orders_desc", lang)},
        {"id": "m:store", "title": i18n.t("menu_store_title", lang), "description": i18n.t("menu_store_desc", lang)},
        {"id": "m:lang", "title": i18n.t("menu_lang_title", lang), "description": i18n.t("menu_lang_desc", lang)},
    ]
    shop = db.get_shop()
    if c["channel"] == "whatsapp" and not shop["wa_phone_number_id"] and tenancy.has_many_shops():
        rows.append({"id": "m:switch", "title": i18n.t("menu_switch_title", lang), "description": i18n.t("menu_switch_desc", lang)})
    _save(c["id"], "search")
    await _list(c, body, i18n.t("btn_menu", lang), rows, i18n.t("section_main_menu", lang))


async def categories(c):
    lang = _lang(c)
    rows = [{"id": f"cat:{r['category']}", "title": r["category"][:24],
             "description": f"{r['products']} items · " + ", ".join((r["examples"] or "").split(",")[:3])[:50]}
            for r in store.categories()][:10]
    _save(c["id"], "search")
    if not rows:
        return await _buttons(c, i18n.t("categories_empty", lang),
                              [("m:store", i18n.t("btn_talk_store", lang)), ("m:menu", i18n.t("btn_home_menu", lang))])
    await _list(c, i18n.t("categories_prompt", lang), i18n.t("btn_categories", lang), rows, i18n.t("btn_categories", lang))


async def show_products(c, products: list[dict], heading: str, offset: int = 0):
    lang = _lang(c)
    available = [p for p in products if p["active"] and p["available"] > 0]
    sold_out = [store.product_label(p) for p in products if p["available"] <= 0]
    if not available:
        await _buttons(c, i18n.t("out_of_stock_msg", lang, heading=heading, items=', '.join(sold_out) or 'these items'),
                       [("m:browse", i18n.t("menu_browse_title", lang)[:20]), ("m:menu", i18n.t("btn_home_menu", lang))])
        _save(c["id"], "search")
        return
    data = {"results": [p["id"] for p in available], "heading": heading}
    if len(available) == 1:
        return await ask_quantity(c, available[0]["id"], data)

    page = available[:MAX_PICK]
    body = heading + "\n\n" + "\n".join(f"• {store.product_label(p)} — {store.money(p['price'])}" for p in page)
    if sold_out:
        body += f"\n\n❌ Out of stock: {', '.join(sold_out)}"
    body += f"\n\n{i18n.t('choose_items_tip', lang)}"
    data["page"] = [p["id"] for p in page]
    _save(c["id"], "pick_product", data)
    options = [{"id": str(p["id"]), "title": _ptitle(p, 30), "description": _pdesc(p, lang)} for p in page]
    fallback_rows = [{"id": f"p:{p['id']}", "title": _ptitle(p), "description": _pdesc(p, lang)} for p in page[:10]]
    await messaging.send_to_customer(c, body, "agent", checklist={
        "button": i18n.t("btn_choose_items", lang), "heading": re.sub(r"[*_]", "", heading)[:80], "options": options,
        "fallback": {"button": i18n.t("btn_choose_item", lang), "sections": [{"title": i18n.t("section_available_now", lang), "rows": fallback_rows}]}})


async def search_and_show(c, text: str):
    lang = _lang(c)
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
            return await _buttons(c, i18n.t("categories_empty", lang),
                                  [("m:store", i18n.t("btn_talk_store", lang)), ("m:menu", i18n.t("btn_home_menu", lang))])
        examples = store.example_names(3)
        ex_str = f" (e.g. {', '.join(examples)})" if examples else ""
        await _buttons(c, i18n.t("search_not_found", lang, query=text.strip()[:60], examples=ex_str),
                       [("m:browse", i18n.t("menu_browse_title", lang)[:20]), ("m:menu", i18n.t("btn_home_menu", lang))])
        return
    where = f" at *{db.get_shop()['name']}*" if c["channel"] == "whatsapp" and tenancy.has_many_shops() else ""
    heading = f"{i18n.t('search_results_heading', lang, query=text.strip()[:60])}{where}"
    await show_products(c, products, heading)


async def ask_quantity(c, product_id: int, data: dict):
    lang = _lang(c)
    p = store.get_product(product_id)
    if not p or not p["active"]:
        await _text(c, i18n.t("item_no_longer_available", lang))
        return await main_menu(c, greet=False)
    if p["available"] <= 0:
        return await search_and_show(c, p["name"])
    data["product_id"] = product_id
    _save(c["id"], "qty", data)
    left = f"\n_{i18n.t('only_n_left', lang, n=p['available'])}_" if p["available"] <= 5 else ""
    at = f"🏪 *{db.get_shop()['name']}*\n" if c["channel"] == "whatsapp" and tenancy.has_many_shops() else ""
    rows = [{"id": f"q:{n}", "title": str(n), "description": store.money(p["price"] * n)}
            for n in QTY_CHOICES if n <= p["available"]]
    await _list(c, f"{at}*{store.product_label(p)}*\n💰 {store.money(p['price'])} each{left}\n\n{i18n.t('ask_quantity_prompt', lang)}",
                i18n.t("btn_choose_quantity", lang), rows, i18n.t("section_quantity", lang))


async def add_to_cart(c, data: dict, qty: int):
    lang = _lang(c)
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
    msg = i18n.t("added_to_cart", lang, qty=qty, item=r['item'], count=cart['item_count'], total=store.money(cart['total'])) + hint
    await _buttons(c, msg,
                   [("cart:more", i18n.t("btn_add_more", lang)),
                    ("m:cart", i18n.t("btn_view_cart", lang)),
                    ("cart:checkout", i18n.t("btn_checkout", lang))])


def parse_selection(text: str) -> list[tuple[int, int]] | None:
    """'1 3 4' → [(1,1),(3,1),(4,1)];  '1x2, 3x5' → [(1,2),(3,5)].  None if the text isn't a selection."""
    if not SELECTION.match(text):
        return None
    return [(int(i), int(q or 1)) for i, q in re.findall(r"(\d{1,2})(?:\s*[x×*]\s*(\d{1,3}))?", text)]


async def add_many(c, data: dict, picks: list[tuple[int, int]]):
    lang = _lang(c)
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
    body = (f"{i18n.t('added_many_heading', lang)}\n" + "\n".join(added)) if added else i18n.t("nothing_added", lang)
    if problems:
        body += "\n\n⚠️ " + "\n⚠️ ".join(problems)
    if cart["items"]:
        body += f"\n\n🧺 {i18n.t('menu_cart_title', lang)}: {cart['item_count']} items · *{store.money(cart['total'])}*"
        if cart.get("free_delivery_hint"):
            body += f"\n_💡 {cart['free_delivery_hint']}_"
    _save(c["id"], "added", {k: data[k] for k in ("results", "page", "heading") if k in data})
    await _buttons(c, body, [("cart:more", i18n.t("btn_add_more", lang)),
                             ("m:cart", i18n.t("btn_view_cart", lang)),
                             ("cart:checkout", i18n.t("btn_checkout", lang))])


async def add_more(c, data: dict):
    lang = _lang(c)
    results = [store.get_product(i) for i in data.get("results", [])]
    results = [p for p in results if p]
    if len(results) > 1:
        heading = ("➕ Pick more from your last search — or type a new product name 🔍" if lang == "en" else
                   "➕ గత శోధన నుండి ఎంచుకోండి — లేదా కొత్త వస్తువు పేరు టైప్ చేయండి 🔍" if lang == "te" else
                   "➕ पिछली खोज से चुनें — या नया सामान खोजें 🔍")
        return await show_products(c, results, heading)
    _save(c["id"], "search")
    examples = store.example_names(2)
    ex_str = f" (e.g. {', '.join(f'_{x}_' for x in examples)})" if examples else ""
    msg = ("🔍 Type the next product you need" if lang == "en" else
           "🔍 తదుపరి కావలసిన వస్తువు పేరు టైప్ చేయండి" if lang == "te" else
           "🔍 अगला सामान जो चाहिए उसका नाम लिखें") + ex_str
    await _buttons(c, msg,
                   [("m:browse", i18n.t("menu_browse_title", lang)[:20]),
                    ("m:cart", i18n.t("btn_view_cart", lang))])


async def show_cart(c):
    lang = _lang(c)
    cart = store.cart(c["id"])
    if not cart["items"]:
        _save(c["id"], "search")
        return await _buttons(c, i18n.t("cart_empty_prompt", lang),
                              [("m:browse", i18n.t("menu_browse_title", lang)[:20]), ("m:menu", i18n.t("btn_home_menu", lang))])
    _save(c["id"], "cart")
    await _buttons(c, f"{i18n.t('cart_heading', lang)}\n\n" + _cart_lines(cart, lang),
                   [("cart:checkout", i18n.t("btn_checkout", lang)),
                    ("cart:more", i18n.t("btn_add_more", lang)),
                    ("cart:edit", i18n.t("btn_edit_cart", lang))])


async def edit_cart(c):
    lang = _lang(c)
    cart = store.cart(c["id"])
    if not cart["items"]:
        return await show_cart(c)
    rows = [{"id": f"e:{i['product_id']}", "title": i["item"][:24],
             "description": f"Qty {i['quantity']} · {store.money(i['line_total'])}"} for i in cart["items"][:9]]
    rows.append({"id": "rm:all", "title": i18n.t("row_clear_cart", lang)[:24]})
    await _list(c, i18n.t("edit_cart_prompt", lang), i18n.t("btn_edit_items", lang), rows, i18n.t("menu_cart_title", lang)[:24])


async def edit_item(c, product_id: int):
    lang = _lang(c)
    item = next((i for i in store.cart(c["id"])["items"] if i["product_id"] == product_id), None)
    if not item:
        return await show_cart(c)
    p = store.get_product(product_id)
    rows = [{"id": f"sq:{product_id}:{n}", "title": str(n), "description": store.money(item["unit_price"] * n)}
            for n in QTY_CHOICES[:9] if n <= max(p["available"], item["quantity"])]
    rows.append({"id": f"sq:{product_id}:0", "title": i18n.t("row_remove_item", lang)[:24]})
    body = (f"*{item['item']}*\nIn cart: {item['quantity']}. Choose the new quantity." if lang == "en" else
            f"*{item['item']}*\nకార్ట్‌లో: {item['quantity']}. కొత్త పరిమాణాన్ని ఎంచుకోండి." if lang == "te" else
            f"*{item['item']}*\nकार्ट में: {item['quantity']}। नई मात्रा चुनें।")
    await _list(c, body, i18n.t("btn_choose_quantity", lang), rows, i18n.t("section_quantity", lang))


async def add_selected(c, data: dict, product_ids: list[int]):
    """Customer ticked several items in the checkbox picker → add 1 of each."""
    lang = _lang(c)
    added, problems = [], []
    for pid in product_ids:
        r = store.set_cart_qty(c["id"], pid, 1, add=True)
        (added.append(f"• 1 × {r['item']}") if r["ok"] else problems.append(r["error"]))
    cart = store.cart(c["id"])
    body = (f"{i18n.t('added_many_heading', lang)}\n" + "\n".join(added)) if added else i18n.t("nothing_added", lang)
    if problems:
        body += "\n\n⚠️ " + "\n⚠️ ".join(problems)
    if cart["items"]:
        body += f"\n\n🧺 {i18n.t('menu_cart_title', lang)}: {cart['item_count']} items · *{store.money(cart['total'])}*"
        if cart.get("free_delivery_hint"):
            body += f"\n_💡 {cart['free_delivery_hint']}_"
        body += f"\n\n{i18n.t('change_qty_hint', lang)}"
    _save(c["id"], "added", {k: data[k] for k in ("results", "page", "heading") if k in data})
    await _buttons(c, body, [("cart:edit", i18n.t("btn_change_qty", lang)),
                             ("cart:more", i18n.t("btn_add_more", lang)),
                             ("cart:checkout", i18n.t("btn_checkout", lang))])


async def checkout(c):
    lang = _lang(c)
    cart = store.cart(c["id"])
    if not cart["items"] or cart.get("warnings"):
        return await show_cart(c)
    addr = _address(c)
    if addr:
        _save(c["id"], "address_choice")
        return await _buttons(c, i18n.t("address_choice_prompt", lang, addr=addr),
                              [("addr:saved", i18n.t("btn_deliver_here", lang)),
                               ("addr:new", i18n.t("btn_new_address", lang))])
    await ask_address(c)


async def ask_address(c):
    lang = _lang(c)
    _save(c["id"], "address")
    await _text(c, i18n.t("ask_address_prompt", lang))


async def ask_payment(c):
    lang = _lang(c)
    _save(c["id"], "payment")
    await _buttons(c, i18n.t("ask_payment_prompt", lang),
                   [("pay:COD", i18n.t("btn_pay_cod", lang)),
                    ("pay:UPI", i18n.t("btn_pay_upi", lang))])


async def review(c, method: str):
    c = db.get_customer(c["id"])
    lang = _lang(c)
    r = store.review_checkout(c, method)
    if not r["ok"]:
        await _text(c, f"⚠️ {r['error'].split(' – ')[0]}")
        return await (ask_address(c) if "address" in r["error"].lower() else show_cart(c))
    s = db.get_settings()
    pay = i18n.t("btn_pay_cod", lang) if method == "COD" else ("📲 యూపీఐ" if lang == "te" else "📲 यूपीआई" if lang == "hi" else "📲 UPI (payment link after confirming)")
    eta_line = (f"🕒 Delivery in {s['delivery_eta']}" if lang == "en" else
                f"🕒 డెలివరీ సమయం: {s['delivery_eta']}" if lang == "te" else
                f"🕒 डिलीवरी समय: {s['delivery_eta']}")
    body = (f"{i18n.t('order_summary_heading', lang)}\n\n{_cart_lines(r, lang)}\n\n"
            f"👤 {r['deliver_to']['name'] or ''}\n📍 {_address(c)}\n💳 {pay}\n{eta_line}\n\n"
            f"{i18n.t('confirm_order_ask', lang)}")
    _save(c["id"], "confirm", {"payment": method})
    await _buttons(c, body, [("ord:confirm", i18n.t("btn_confirm_order", lang)),
                             ("cart:edit", i18n.t("btn_edit_cart", lang)),
                             ("ord:cancel", i18n.t("btn_cancel", lang))])


async def confirm(c):
    lang = _lang(c)
    r = store.place_order(db.get_customer(c["id"]))
    if not r["ok"]:
        await _text(c, f"⚠️ {r['error']}")
        return await show_cart(c)
    o = r["order"]
    pay_str = "Cash on Delivery" if o["payment_method"] == "COD" else "UPI"
    if lang == "te":
        pay_str = "క్యాష్ ఆన్ డెలివరీ" if o["payment_method"] == "COD" else "యూపీఐ"
    elif lang == "hi":
        pay_str = "कैश ऑन डिलीवरी" if o["payment_method"] == "COD" else "यूपीआई"
    body = i18n.t("order_placed_body", lang, code=o['code'], total=store.money(o['total']), pay=pay_str)
    if o["payment_method"] == "UPI":
        pay_url = f"{config.PUBLIC_BASE_URL}/pay/{o['code']}"
        body += f"\n\n{i18n.t('pay_link_text', lang, url=pay_url)}"
    else:
        body += f"\n\n{i18n.t('pay_cod_note', lang)}"
    _save(c["id"], "idle")
    await _buttons(c, body, [(f"o:{o['code']}", i18n.t("btn_track_order", lang)),
                             ("m:menu", i18n.t("btn_home_menu", lang))])


async def my_orders(c):
    lang = _lang(c)
    orders = store.customer_orders(c["id"], 10)
    if not orders:
        return await _buttons(c, i18n.t("no_orders_placed", lang),
                              [("m:browse", i18n.t("menu_browse_title", lang)[:20]), ("m:menu", i18n.t("btn_home_menu", lang))])
    rows = [{"id": f"o:{o['code']}", "title": f"{o['code']} · {store.money(o['total'])}",
             "description": f"{i18n.status_label(o['status'], lang)} · {o['created_at'][:10]}"} for o in orders]
    await _list(c, i18n.t("orders_list_prompt", lang), i18n.t("btn_view_orders", lang), rows, i18n.t("section_recent_orders", lang))


async def order_detail(c, code: str):
    lang = _lang(c)
    o = store.order_by_code(c["id"], code)
    if not o:
        return await my_orders(c)
    items = "\n".join(f"• {i['quantity']} × {i['name']} ({i['variant']}) — {store.money(i['line_total'])}" for i in o["items"])
    status_txt = i18n.status_label(o['status'], lang)
    body = (f"📦 *Order {o['code']}*\nStatus: *{status_txt}*\n\n{items}\n\n"
            f"*{i18n.t('total', lang)}: {store.money(o['total'])}* · {o['payment_method']} ({o['payment_status'].replace('_', ' ')})\n"
            f"🕒 Placed {o['created_at'].replace('T', ' ')[:16]}")
    if o["status"] in ("accepted", "packed", "out_for_delivery") and o["eta"]:
        body += f" · ETA {o['eta']}"
    if o["reject_reason"]:
        body += f"\nNote: {o['reject_reason']}"
    if o["payment_method"] == "UPI" and o["payment_status"] == "pending" and o["status"] not in ("rejected", "cancelled"):
        body += f"\n\n💳 Pay: {config.PUBLIC_BASE_URL}/pay/{o['code']}"
    buttons = []
    if o["status"] in ("pending", "accepted"):
        buttons.append((f"oc:{o['code']}", i18n.t("btn_cancel_order", lang)))
    buttons += [(f"or:{o['code']}", i18n.t("btn_reorder", lang)), ("m:menu", i18n.t("btn_home_menu", lang))]
    await _buttons(c, body, buttons)


async def ask_cancel(c, code: str):
    lang = _lang(c)
    await _buttons(c, i18n.t("ask_cancel_prompt", lang, code=code),
                   [(f"ocy:{code}", i18n.t("btn_yes_cancel", lang)),
                    (f"o:{code}", i18n.t("btn_no_keep", lang))])


async def cancel_order(c, code: str):
    lang = _lang(c)
    o = store.order_by_code(c["id"], code)
    if not o or o["status"] not in ("pending", "accepted"):
        await _text(c, i18n.t("cant_cancel_anymore", lang))
        return await main_menu(c, greet=False)
    store.change_status(o["id"], "cancelled", "customer", "Cancelled by customer on WhatsApp")
    await _buttons(c, i18n.t("order_cancelled_done", lang, code=code), [("m:menu", i18n.t("btn_home_menu", lang))])


async def reorder(c, code: str):
    lang = _lang(c)
    o = store.order_by_code(c["id"], code)
    if not o:
        return await my_orders(c)
    skipped = []
    for i in o["items"]:
        r = store.set_cart_qty(c["id"], i["product_id"], i["quantity"], add=True)
        if not r["ok"]:
            skipped.append(f"{i['name']} ({i['variant']})")
    if skipped:
        not_avail = ("⚠️ Not available right now: " if lang == "en" else
                     "⚠️ ప్రస్తుతం అందుబాటులో లేవు: " if lang == "te" else
                     "⚠️ अभी उपलब्ध नहीं हैं: ")
        await _text(c, not_avail + ", ".join(skipped))
    await show_cart(c)


async def talk_to_store(c):
    lang = _lang(c)
    db.run("UPDATE customers SET needs_attention=1 WHERE id=?", (c["id"],))
    db.add_message(c["id"], "in", "system", "⚠️ Customer asked to talk to the store", kind="alert")
    events.publish("attention", {"customer_id": c["id"], "phone": c["phone"], "message": "wants to talk to the store"})
    notify.add("customer_message", f"💬 {c.get('name') or c.get('wa_name') or c['phone']} wants to talk",
               "Reply from Customer Chats in the portal.", customer_id=c["id"])
    _save(c["id"], "store_chat")
    await _text(c, i18n.t("chat_store_welcome", lang))


# ---------------- dispatcher ----------------

async def _on_tap(c, state, data, rid: str):
    head, _, arg = rid.partition(":")
    lang = _lang(c)
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
    if rid == "m:lang":
        return await show_language_picker(c)
    if head == "lang":
        new_lang = i18n.normalize_language(arg)
        db.run("UPDATE customers SET language=? WHERE id=?", (new_lang, c["id"]))
        c["language"] = new_lang
        await _text(c, i18n.t("lang_changed", new_lang))
        return await main_menu(c, greet=False)
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
        return await _buttons(c, i18n.t("order_not_placed", lang),
                              [("m:cart", i18n.t("btn_view_cart", lang)), ("m:menu", i18n.t("btn_home_menu", lang))])
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
    # Auto-detect language from incoming customer text
    detected = i18n.detect_language(text)
    if detected and detected != c.get("language"):
        db.run("UPDATE customers SET language=? WHERE id=?", (detected, c["id"]))
        c["language"] = detected

    lang = _lang(c)
    t = _norm(text)

    # Explicit language switch keywords
    if t in ("telugu", "తెలుగు", "te"):
        db.run("UPDATE customers SET language='te' WHERE id=?", (c["id"],))
        c["language"] = "te"
        await _text(c, i18n.t("lang_changed", "te"))
        return await main_menu(c, greet=False)
    if t in ("hindi", "हिंदी", "हिन्दी", "hi"):
        db.run("UPDATE customers SET language='hi' WHERE id=?", (c["id"],))
        c["language"] = "hi"
        await _text(c, i18n.t("lang_changed", "hi"))
        return await main_menu(c, greet=False)
    if t in ("english", "en", "angrezi"):
        db.run("UPDATE customers SET language='en' WHERE id=?", (c["id"],))
        c["language"] = "en"
        await _text(c, i18n.t("lang_changed", "en"))
        return await main_menu(c, greet=False)
    if t in i18n.LANG_WORDS_I18N:
        return await show_language_picker(c)

    first_contact = not db.one("SELECT 1 FROM messages WHERE customer_id=? AND direction='out' LIMIT 1", (c["id"],))
    if t in GREETINGS or (first_contact and len(t.split()) <= 2 and not store.search_products(t, limit=1)):
        return await main_menu(c, greet=True)
    if state == "store_chat":
        events.publish("attention", {"customer_id": c["id"], "phone": c["phone"], "message": text[:200]})
        if not data.get("acked"):
            _save(c["id"], "store_chat", {"acked": True})
            await _text(c, i18n.t("chat_store_sent", lang))
        return
    if t in i18n.CART_WORDS_I18N:
        return await show_cart(c)
    if t in i18n.CHECKOUT_WORDS_I18N and state not in ("confirm",):
        return await checkout(c)
    if t in i18n.ORDERS_WORDS_I18N:
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
            return await _text(c, i18n.t("address_too_short", lang))
        db.run("UPDATE customers SET address=?, name=COALESCE(name, wa_name) WHERE id=?", (text.strip()[:300], c["id"]))
        await _text(c, i18n.t("address_saved", lang))
        return await ask_payment(c)
    if state == "payment":
        if any(w in t for w in ("cod", "cash", "నగదు", "క్యాష్", "नकद", "कैश")):
            return await review(c, "COD")
        if any(w in t for w in ("upi", "gpay", "google pay", "phonepe", "paytm", "online", "యూపీఐ", "यूपीआई")):
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
    lang = _lang(c)
    if state in ("address", "address_choice"):
        await _text(c, i18n.t("location_saved_tip", lang))
        return await ask_payment(c)
    if store.cart(c["id"])["items"]:
        return await _buttons(c, i18n.t("location_saved", lang),
                              [("cart:checkout", i18n.t("btn_checkout", lang)),
                               ("m:cart", i18n.t("btn_view_cart", lang))])
    search_prompt = ("Now type a product name to start shopping 🔍" if lang == "en" else
                     "షాపింగ్ ప్రారంభించడానికి వస్తువు పేరు టైప్ చేయండి 🔍" if lang == "te" else
                     "खरीदारी शुरू करने के लिए सामान का नाम लिखें 🔍")
    await _text(c, f"{i18n.t('location_saved', lang)} {search_prompt}")


def _allowed_while_closed(state: str, reply_id: str | None, text: str | None) -> bool:
    """While the store is closed customers can still track/cancel orders and talk to staff — but not shop."""
    if reply_id:
        return reply_id in ("m:orders", "m:store", "m:switch", "m:lang") or reply_id.split(":")[0] in ("o", "oc", "ocy", "lang")
    t = _norm(text)
    return state == "store_chat" or t in i18n.ORDERS_WORDS_I18N


async def _closed_notice(c):
    lang = _lang(c)
    s = db.get_settings()
    await _buttons(c, i18n.t("closed_notice", lang, store_name=s['store_name'], reason=store.closed_reason()),
                   [("m:orders", i18n.t("menu_orders_title", lang)[:20]),
                    ("m:store", i18n.t("menu_store_title", lang)[:20])]
                   + ([("m:switch", i18n.t("menu_switch_title", lang)[:20])]
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
                lang = _lang(c)
                unsupported_msg = ("🙏 Sorry, I can only read text, voice notes and locations here. Type *menu* to start." if lang == "en" else
                                   "🙏 క్షమించండి, నేను టెక్స్ట్, వాయిస్ నోట్స్ మరియు లొకేషన్‌లను మాత్రమే చదవగలను. ప్రారంభించడానికి *menu* అని టైప్ చేయండి." if lang == "te" else
                                   "🙏 क्षमा करें, मैं केवल टेक्स्ट, वॉयस नोट्स और लोकेशन ही समझ सकता हूँ। शुरू करने के लिए *menu* लिखें।")
                await _text(c, unsupported_msg)
        except Exception:
            log.exception("flow error for customer %s", customer_id)
            lang = _lang(c)
            err_msg = ("⚠️ Something went wrong on our side. Type *menu* to start again." if lang == "en" else
                       "⚠️ మా వైపు ఏదో సమస్య వచ్చింది. మళ్లీ ప్రారంభించడానికి *menu* అని టైప్ చేయండి." if lang == "te" else
                       "⚠️ हमारी तरफ से कुछ गड़बड़ हुई। दोबारा शुरू करने के लिए *menu* लिखें।")
            await _text(c, err_msg)
