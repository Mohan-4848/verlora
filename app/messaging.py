"""Outbound messages to customers (WhatsApp or the dashboard simulator) + order notifications."""
import json
import logging

from . import config, db, events, llm, store, whatsapp

log = logging.getLogger("messaging")


async def send_to_customer(customer: dict, text: str, sender: str = "agent",
                           buttons: list[tuple[str, str]] | None = None, menu: dict | None = None,
                           checklist: dict | None = None) -> dict:
    """buttons: up to 3 (id, title) reply buttons.
    menu: WhatsApp list {"button": label, "sections": [{"title", "rows": [{"id", "title", "description"}]}]}.
    checklist: multi-select {"button", "heading", "options": [{"id", "title", "description"}], "fallback": menu}
               → WhatsApp Flow with checkboxes (falls back to the single-pick `fallback` list if unavailable)."""
    meta = None
    if checklist:
        meta = {"type": "checklist", "button": checklist["button"], "options": checklist["options"]}
    elif buttons:
        meta = {"type": "buttons", "options": [{"id": i, "title": t} for i, t in buttons[:3]]}
    elif menu:
        meta = {"type": "list", "button": menu["button"],
                "options": [r for sec in menu["sections"] for r in sec["rows"]][:10]}
    msg = db.add_message(customer["id"], "out", sender, text, kind=meta["type"] if meta else "text",
                         meta=json.dumps(meta, ensure_ascii=False) if meta else None)
    events.publish("message", {**msg, "phone": customer["phone"], "channel": customer["channel"]})
    if customer["channel"] == "whatsapp":
        if checklist:
            sent = False
            if config.current_flow_id():
                sent = await whatsapp.send_flow(customer["phone"], text, checklist["button"], "PICK_ITEMS", {
                    "heading": checklist["heading"],
                    "products": [{"id": o["id"], "title": o["title"][:30], "description": o.get("description", "")}
                                 for o in checklist["options"][:20]]})
            if not sent:   # Flow not set up yet / rejected → single-pick list still works
                fb = checklist["fallback"]
                await whatsapp.send_list(customer["phone"], text, fb["button"], fb["sections"])
        elif buttons:
            await whatsapp.send_buttons(customer["phone"], text, buttons)
        elif menu:
            await whatsapp.send_list(customer["phone"], text, menu["button"], menu["sections"])
        else:
            await whatsapp.send_text(customer["phone"], text)
    return msg


def _status_message(o: dict, status: str, note: str | None) -> str | None:
    s = db.get_settings()
    code, total = o["code"], store.money(o["total"])
    reason = f"\nReason: {note}" if note else ""
    if status == "accepted":
        msg = f"✅ Your order *{code}* has been accepted! We're packing it now.\n🕒 Expected delivery: {o['eta']}"
        if o["payment_method"] == "UPI" and o["payment_status"] != "paid":
            msg += f"\n💳 Pay {total} here: {config.PUBLIC_BASE_URL}/pay/{code}"
        return msg
    if status == "rejected":
        return (f"❌ Sorry, we couldn't accept order *{code}*.{reason}\n"
                + ("Your payment will be refunded. " if o["payment_status"] in ("paid", "refund_due") else "")
                + "Reply here if you'd like to order something else.")
    if status == "preparing":
        return f"👨‍🍳 We're preparing your order *{code}* now."
    if status == "packed":
        return f"📦 Order *{code}* is packed and will leave the store shortly."
    if status == "out_for_delivery":
        cash = f"\nPlease keep {total} ready (cash or UPI)." if o["payment_status"] != "paid" else ""
        return f"🛵 Order *{code}* is out for delivery!{cash}"
    if status == "delivered":
        return (f"🎉 Order *{code}* delivered. Thank you for shopping with {s['store_name']}!\n"
                "Reply *reorder* anytime to get the same items again.")
    if status == "cancelled":
        return f"🛑 Order *{code}* has been cancelled.{reason}"
    if status == "payment_paid":
        return f"💰 Payment of {total} received for order *{code}*. Thank you!"
    if status == "payment_refunded":
        return f"↩️ Refund of {total} for order *{code}* has been processed."
    return None


async def _localise(text: str, language: str | None) -> str:
    if not language or language.lower().startswith(("en", "english")):
        return text
    try:
        msg, _ = await llm.chat([
            {"role": "system", "content": "Translate the WhatsApp message into the requested language/style. Keep emojis, "
                                          "*bold* markers, order codes, amounts, and URLs exactly. Output only the message."},
            {"role": "user", "content": f"Language: {language}\n\n{text}"},
        ], temperature=0)
        return (msg.get("content") or "").strip() or text
    except llm.LLMError as e:
        log.warning("notification translation failed: %s", e)
        return text


async def notify_order_update(order: dict, status: str, note: str | None = None):
    text = _status_message(order, status, note)
    if not text:
        return
    customer = db.get_customer(order["customer_id"])
    text = await _localise(text, customer.get("language"))
    await send_to_customer(customer, text, sender="system")
