"""Outbound messages to customers (WhatsApp or the dashboard simulator) + order notifications."""
import json
import logging

from . import config, db, events, i18n, llm, store, whatsapp

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


def _status_message(o: dict, status: str, note: str | None, lang: str = "en") -> str | None:
    try:
        s = db.get_settings()
        store_name = s.get("store_name", "the store")
    except Exception:
        store_name = "the store"
    total = store.money(o["total"])
    msg = i18n.status_notification_message(o, status, note, lang=lang,
                                           store_name=store_name,
                                           total_str=total)
    if not msg:
        return None
    if status == "accepted" and o.get("payment_method") == "UPI" and o.get("payment_status") != "paid":
        code = o["code"]
        norm_l = i18n.normalize_language(lang)
        if norm_l == i18n.LANG_TE:
            msg += f"\n💳 {total} ఇక్కడ చెల్లించండి: {config.PUBLIC_BASE_URL}/pay/{code}"
        elif norm_l == i18n.LANG_HI:
            msg += f"\n💳 {total} यहाँ भुगतान करें: {config.PUBLIC_BASE_URL}/pay/{code}"
        else:
            msg += f"\n💳 Pay {total} here: {config.PUBLIC_BASE_URL}/pay/{code}"
    return msg


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
    customer = db.get_customer(order["customer_id"])
    lang = customer.get("language") or "en"
    text = _status_message(order, status, note, lang=lang)
    if not text:
        return
    norm_lang = i18n.normalize_language(lang)
    if norm_lang not in (i18n.LANG_EN, i18n.LANG_TE, i18n.LANG_HI):
        text = await _localise(text, customer.get("language"))
    await send_to_customer(customer, text, sender="system")
