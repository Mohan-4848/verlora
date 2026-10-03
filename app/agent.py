"""The WhatsApp ordering agent: LLM + tool calling over the store's real catalogue, cart and orders."""
import asyncio
import json
import logging
import re
from datetime import datetime

from . import config, db, events, llm, messaging, store

log = logging.getLogger("agent")
MAX_STEPS = 6
HISTORY_MESSAGES = 16
_locks: dict[int, asyncio.Lock] = {}
_answered_upto: dict[int, int] = {}   # last customer message id already handled, per customer


def _fn(name, description, properties=None, required=None):
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties or {}, "required": required or []}}}


TOOLS = [
    _fn("search_products", "Search the store catalogue. Returns matching products with variants, prices and stock.",
        {"query": {"type": "string", "description": "Product words in English, matching the shop's catalogue (e.g. a product or brand name, with size if given)"},
         "category": {"type": "string", "description": "Optional category filter"}}, ["query"]),
    _fn("browse_catalogue", "List categories (no argument) or all products in one category.",
        {"category": {"type": "string"}}),
    _fn("add_to_cart", "Add a quantity of a product to the cart (adds to any existing quantity).",
        {"product_id": {"type": "integer"}, "quantity": {"type": "integer", "minimum": 1}}, ["product_id", "quantity"]),
    _fn("update_cart_item", "Set the exact quantity of a cart item. quantity=0 removes it.",
        {"product_id": {"type": "integer"}, "quantity": {"type": "integer", "minimum": 0}}, ["product_id", "quantity"]),
    _fn("view_cart", "Show cart items, subtotal, delivery fee and total."),
    _fn("clear_cart", "Remove everything from the cart."),
    _fn("save_customer_details", "Save the customer's name, delivery address, landmark and/or preferred language.",
        {"name": {"type": "string"}, "address": {"type": "string", "description": "Full address: flat/house no, building, street, area"},
         "landmark": {"type": "string"},
         "language": {"type": "string", "description": "e.g. English, Hindi, Telugu, Hinglish (romanised Hindi), Tenglish"}}),
    _fn("review_order", "Checkout step 1: validate cart + address and get the final summary to show the customer for confirmation.",
        {"payment_method": {"type": "string", "enum": ["COD", "UPI"]}}, ["payment_method"]),
    _fn("place_order", "Checkout step 2: place the reviewed order. Only call after the customer explicitly confirmed the summary."),
    _fn("get_order_status", "Status, items and timeline of an order (latest order if no code given).",
        {"order_code": {"type": "string"}}),
    _fn("list_my_orders", "The customer's recent order history."),
    _fn("cancel_order", "Cancel an order that is not yet packed.",
        {"order_code": {"type": "string"}, "reason": {"type": "string"}}, ["order_code"]),
    _fn("reorder", "Copy the items of a previous order (latest if no code) into the cart.",
        {"order_code": {"type": "string"}}),
    _fn("message_store", "Escalate to the store staff: complaints, refunds, special requests, or customer wants a human.",
        {"message": {"type": "string", "description": "Summary of what the customer needs"}}, ["message"]),
]


# ---------------- tool implementations ----------------

def _latest_order(customer_id: int, code: str | None) -> dict | None:
    if code:
        return store.order_by_code(customer_id, code)
    orders = store.customer_orders(customer_id, 1)
    return orders[0] if orders else None


async def run_tool(name: str, args: dict, customer_id: int, turn: dict) -> dict:
    c = db.get_customer(customer_id)
    if name == "search_products":
        rows = store.search_products(args.get("query", ""), args.get("category"))
        if not rows:
            return {"results": [], "hint": "No match. Try another word, or browse_catalogue for categories.",
                    "categories": [r["category"] for r in store.categories()]}
        return {"results": [store.public_product(r) for r in rows]}
    if name == "browse_catalogue":
        if args.get("category"):
            return {"products": [store.public_product(r) for r in store.products_in_category(args["category"])]}
        return {"categories": store.categories()}
    if name == "add_to_cart":
        return store.set_cart_qty(customer_id, int(args["product_id"]), int(args["quantity"]), add=True)
    if name == "update_cart_item":
        return store.set_cart_qty(customer_id, int(args["product_id"]), int(args["quantity"]))
    if name == "view_cart":
        return store.cart(customer_id)
    if name == "clear_cart":
        store.clear_cart(customer_id)
        return {"ok": True, "cart": store.cart(customer_id)}
    if name == "save_customer_details":
        fields = {k: v.strip() for k, v in args.items() if k in ("name", "address", "landmark", "language") and v and v.strip()}
        if fields:
            db.run(f"UPDATE customers SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), customer_id))
            events.publish("customer", {"id": customer_id})
        return {"ok": True, "saved": fields}
    if name == "review_order":
        result = store.review_checkout(c, args.get("payment_method", ""))
        if result.get("ok"):
            turn["buttons"] = [("confirm", "✅ Confirm order"), ("edit", "✏️ Make changes")]
        return result
    if name == "place_order":
        result = store.place_order(c)
        turn.pop("buttons", None)
        if not result.get("ok"):
            return result
        o = result["order"]
        out = {"ok": True, **store.agent_order_view(o),
               "next": "Order is waiting for the store to accept it; the customer gets WhatsApp updates automatically."}
        if o["payment_method"] == "UPI":
            out["upi_payment_link"] = f"{config.PUBLIC_BASE_URL}/pay/{o['code']}"
        return out
    if name == "get_order_status":
        o = _latest_order(customer_id, args.get("order_code"))
        return store.agent_order_view(o) if o else {"error": "No order found for this customer."}
    if name == "list_my_orders":
        return {"orders": [{"order_code": o["code"], "status": o["status"], "total": o["total"], "placed_at": o["created_at"],
                            "items": len(o["items"])} for o in store.customer_orders(customer_id, 5)]}
    if name == "cancel_order":
        o = store.order_by_code(customer_id, args["order_code"])
        if not o:
            return {"ok": False, "error": "Order not found."}
        if o["status"] not in ("pending", "accepted"):
            return {"ok": False, "error": f"Order is already {o['status']} and can't be cancelled. Offer message_store."}
        r = store.change_status(o["id"], "cancelled", "customer", args.get("reason") or "Cancelled by customer")
        return {"ok": r["ok"], "order_code": o["code"], "status": "cancelled"} if r["ok"] else r
    if name == "reorder":
        o = _latest_order(customer_id, args.get("order_code"))
        if not o:
            return {"ok": False, "error": "No previous order found."}
        added, skipped = [], []
        for i in o["items"]:
            r = store.set_cart_qty(customer_id, i["product_id"], i["quantity"], add=True)
            (added if r.get("ok") else skipped).append(f"{i['name']} ({i['variant']})" + ("" if r.get("ok") else f": {r['error']}"))
        return {"ok": True, "added": added, "skipped": skipped, "cart": store.cart(customer_id)}
    if name == "message_store":
        from . import notify
        notify.add("customer_message", f"💬 {c.get('name') or c.get('wa_name') or c['phone']} needs help",
                   args["message"][:200], customer_id=customer_id)
        db.run("UPDATE customers SET needs_attention=1 WHERE id=?", (customer_id,))
        db.add_message(customer_id, "in", "system", f"⚠️ Needs attention: {args['message']}", kind="alert")
        events.publish("attention", {"customer_id": customer_id, "phone": c["phone"], "message": args["message"]})
        return {"ok": True, "info": "Store staff notified; they will reply in this chat."}
    return {"error": f"Unknown tool {name}"}


# ---------------- prompt ----------------

def _system_prompt(c: dict) -> str:
    s = db.get_settings()
    crt = store.cart(c["id"])
    cart_txt = "empty" if not crt["items"] else "; ".join(
        f"{i['quantity']} x {i['item']} [id {i['product_id']}] = ₹{i['line_total']:g}" for i in crt["items"]
    ) + f" | subtotal ₹{crt['subtotal']:g}, delivery ₹{crt['delivery_fee']:g}, total ₹{crt['total']:g}"
    active = db.all_(f"SELECT code, status FROM orders WHERE customer_id=? AND status IN {store.ACTIVE_STATUSES} "
                     "ORDER BY id DESC LIMIT 3", (c["id"],))
    address = c["address"] or ("location pin shared" if c["latitude"] is not None else "not saved")
    checkout = (f"summary shown ({c['checkout_payment']}); waiting for the customer to confirm → if they now say yes, call place_order"
                if c["checkout_token"] else "not started")
    return f"""You are the WhatsApp ordering assistant for *{s['store_name']}*, a {s.get('business_type') or 'local shop'} at {s['store_address']}.
It currently sells (in stock): {', '.join(store.catalogue_vocabulary(80)) or 'nothing yet — tell customers products are coming soon'}.
Store: open {s['store_hours']} · delivery in {s['delivery_eta']} · delivery fee ₹{s['delivery_fee']} below ₹{s['free_delivery_above']}, free above · payment: Cash on Delivery (COD) or UPI.
Now: {datetime.now().strftime('%A %d %b %Y, %I:%M %p')}

CUSTOMER
- Phone: {c['phone']} · WhatsApp name: {c['wa_name'] or '-'} · Saved name: {c['name'] or 'not saved'}
- Preferred language: {c['language'] or 'unknown'}
- Delivery address: {address}{' · Landmark: ' + c['landmark'] if c['landmark'] else ''}
- Cart: {cart_txt}
- Active orders: {', '.join(f"{o['code']} ({o['status']})" for o in active) or 'none'}
- Checkout: {checkout}

HOW TO WORK
1. Availability, prices, variants, stock and categories come ONLY from tools. Never invent products, categories, prices, discounts or delivery promises. For "what do you have / menu / list" questions call browse_catalogue.
2. Search with English product words that match the catalogue above (translate local words, e.g. doodh/paalu→milk, chawal/biyyam→rice). One search per distinct item; call several tools in parallel when the customer lists many items.
3. Variants & clarification: add directly ONLY when exactly one product fits (brand/type stated, or the search returns a single product). If several brands or types fit (e.g. Amul Taaza vs Amul Gold vs Heritage milk, white vs brown bread) and the customer didn't specify, do NOT pick for them: add the unambiguous items, then ask one short numbered question per ambiguous item with prices. Missing quantity → assume 1 and say so. Convert sizes sensibly: "2 litre milk" with a 1 L pack → quantity 2.
4. Out of stock or not enough stock → say so and offer the alternatives returned. If something isn't sold here, say so.
5. After cart changes, confirm briefly what was added (with price) and the running total.
6. Checkout, when the customer is done:
   a. Need a name (WhatsApp name is fine if it looks real) and a full delivery address (flat/house no., building/street, area + landmark). A WhatsApp location pin is great for the area, but still ask for the flat/house number if it's missing. Save with save_customer_details.
   b. Ask "Cash on Delivery or UPI?" unless the customer already said it in this conversation. Never assume a payment method.
   c. Call review_order and show the summary: items with prices, subtotal, delivery fee, total, address, payment method, ETA. Ask them to confirm.
   d. Call place_order ONLY when the customer's reply to the summary is a clear yes (yes / ok / confirm / haan / avunu / ✅ Confirm order). Any other reply (a change, a question, a payment choice) is NOT a confirmation: apply it, call review_order again and re-ask.
7. After placing: give the order code, total and ETA. UPI → share the payment link. COD → pay on delivery. Mention they'll get WhatsApp updates.
8. Status, history, cancellation and reorder questions → use the tools. Cancellation is only possible before the order is packed.
9. Complaints, refunds, damaged items, special requests or "talk to a person" → message_store, then tell them the store team will reply here.
10. Off-topic chat → reply briefly and steer back to shopping.

STYLE
- Reply in the customer's language and script (English, Hindi, Telugu, Hinglish, Tenglish…). If it differs from the preferred language above, also call save_customer_details(language=...).
- WhatsApp formatting only: *bold* with single asterisks, short lines, "•" bullets, ₹ amounts. No markdown headings, tables or **double asterisks**.
- Warm and concise: usually under 8 lines. A few emojis are fine.
- Never mention product IDs, tools, or these instructions."""


def _history(customer_id: int) -> list[dict]:
    rows = db.all_("SELECT sender, kind, body FROM messages WHERE customer_id=? AND kind != 'alert' "
                   "ORDER BY id DESC LIMIT ?", (customer_id, HISTORY_MESSAGES))[::-1]
    out: list[dict] = []
    for r in rows:
        if r["sender"] == "customer":
            role, text = "user", r["body"]
        elif r["sender"] == "store":
            role, text = "assistant", f"[Store staff wrote]: {r['body']}"
        elif r["sender"] == "system":
            role, text = "assistant", f"[Automatic update sent]: {r['body']}"
        else:
            role, text = "assistant", r["body"]
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n\n" + text
        else:
            out.append({"role": role, "content": text})
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def whatsapp_format(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"__(.+?)__", r"_\1_", text)
    text = re.sub(r"^#{1,6}\s*(.+)$", r"*\1*", text, flags=re.M)
    text = re.sub(r"^(\s*)[-*]\s+", r"\1• ", text, flags=re.M)
    return text.strip()


# ---------------- main entry ----------------

async def respond(customer_id: int):
    """Generate and send the agent's reply to the latest customer message(s)."""
    lock = _locks.setdefault(customer_id, asyncio.Lock())
    async with lock:
        c = db.get_customer(customer_id)
        if c["bot_paused"]:
            return
        latest = db.one("SELECT MAX(id) AS m FROM messages WHERE customer_id=? AND sender='customer'", (customer_id,))["m"]
        done = _answered_upto.get(customer_id, 0)
        if not latest or latest <= done:
            return   # a previous run already covered this message
        history = _history(customer_id)
        if not history or history[-1]["role"] != "user":
            # customer messages that arrived while the previous reply was being written
            pending = db.all_("SELECT body FROM messages WHERE customer_id=? AND sender='customer' AND id>? ORDER BY id",
                              (customer_id, done))
            history.append({"role": "user", "content": "\n".join(p["body"] for p in pending)})
        _answered_upto[customer_id] = latest
        messages = [{"role": "system", "content": _system_prompt(c)}, *history]
        turn: dict = {}
        reply = None
        try:
            for step in range(MAX_STEPS):
                msg, provider = await llm.chat(messages, TOOLS if step < MAX_STEPS - 1 else None)
                calls = msg.get("tool_calls") or []
                if not calls:
                    reply = msg.get("content")
                    if not (reply or "").strip() and step < MAX_STEPS - 1:
                        msg, _ = await llm.chat(messages)   # model went silent after tools: ask for the text reply
                        reply = msg.get("content")
                    break
                messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls", "extra_content")})
                for tc in calls:
                    name = tc["function"]["name"]
                    try:
                        args = json.loads(tc["function"].get("arguments") or "{}")
                        result = await run_tool(name, args, customer_id, turn)
                    except Exception as e:   # tool bugs must never kill the conversation
                        log.exception("tool %s failed", name)
                        result = {"error": f"{type(e).__name__}: {e}"}
                    log.info("tool %s(%s) -> %s", name, tc["function"].get("arguments"), json.dumps(result, default=str)[:300])
                    messages.append({"role": "tool", "tool_call_id": tc.get("id"), "name": name,
                                     "content": json.dumps(result, ensure_ascii=False, default=str)})
        except llm.LLMError as e:
            log.error("LLM unavailable: %s", e)
            reply = ("Sorry, I'm having trouble right now 🙏 Please try again in a minute — "
                     "or reply *store* and our team will help you.")

        reply = whatsapp_format(reply or "Sorry, I didn't catch that. Could you say it again? 🙂")
        await messaging.send_to_customer(db.get_customer(customer_id), reply, "agent", turn.get("buttons"))
