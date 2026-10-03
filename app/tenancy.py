"""Routes incoming WhatsApp messages to the right shop.

A shop with its own WhatsApp number gets every message sent to that number. On the shared number, a customer
picks a shop by sending "join <shop-code>" (the shop's wa.me link pre-fills it) or by tapping it in the shop list;
the choice is remembered per phone number until they switch.
"""
import logging
import re
import time
from urllib.parse import quote

import httpx

from . import config, db, whatsapp

log = logging.getLogger("tenancy")
JOIN_RE = re.compile(r"^\s*(?:join|shop|store|start)\s+([a-z0-9][a-z0-9-]{1,40})\s*$", re.I)
_number_cache: dict = {"value": None, "at": 0.0}


def active_shops() -> list[dict]:
    return db.all_("""SELECT s.*, (SELECT value FROM shop_settings WHERE shop_id=s.id AND key='business_type') AS business_type,
                             (SELECT value FROM shop_settings WHERE shop_id=s.id AND key='store_city') AS city
                      FROM shops s WHERE s.active=1 ORDER BY s.name""")


def remember(phone: str, shop_id: int):
    db.run("INSERT INTO wa_sessions(phone, shop_id, updated_at) VALUES(?,?,?) "
           "ON CONFLICT(phone) DO UPDATE SET shop_id=excluded.shop_id, updated_at=excluded.updated_at",
           (phone, shop_id, db.now()))


def forget(phone: str):
    db.run("DELETE FROM wa_sessions WHERE phone=?", (phone,))


def resolve(m: dict) -> tuple[int | None, bool]:
    """→ (shop_id or None, just_joined). None means: ask the customer to pick a shop."""
    phone = m["from"]
    if m.get("to_number_id"):
        dedicated = db.one("SELECT id FROM shops WHERE wa_phone_number_id=? AND active=1", (m["to_number_id"],))
        if dedicated:
            return dedicated["id"], False
    match = JOIN_RE.match(m.get("text") or "")
    if match:
        shop = db.one("SELECT id FROM shops WHERE slug=? AND active=1", (match.group(1).lower(),))
        if shop:
            remember(phone, shop["id"])
            return shop["id"], True
    if (m.get("reply_id") or "").startswith("shop:"):
        shop = db.one("SELECT id FROM shops WHERE id=? AND active=1", (int(m["reply_id"].split(":")[1]),))
        if shop:
            remember(phone, shop["id"])
            return shop["id"], True
    session = db.one("SELECT w.shop_id FROM wa_sessions w JOIN shops s ON s.id=w.shop_id AND s.active=1 WHERE w.phone=?",
                     (phone,))
    if session:
        return session["shop_id"], False
    shops = active_shops()
    if len(shops) == 1:
        remember(phone, shops[0]["id"])
        return shops[0]["id"], True
    return None, False


def has_many_shops() -> bool:
    return db.one("SELECT COUNT(*) AS n FROM shops WHERE active=1")["n"] > 1


async def send_shop_picker(phone: str, intro: str | None = None):
    """Lists shops on the shared number (sent without a shop context → shared credentials)."""
    token = db.use_shop(None)
    try:
        shops = active_shops()
        if not shops:
            await whatsapp.send_text(phone, "🙏 No shops are taking orders on this number yet.")
            return
        rows = [{"id": f"shop:{s['id']}", "title": s["name"][:24],
                 "description": " · ".join(x for x in (s["business_type"], s["city"]) if x)[:72]} for s in shops[:10]]
        body = (intro or "👋 Welcome! Several local shops take orders on this WhatsApp number.") + \
            "\n\n🏪 *Which shop would you like to order from?*"
        if len(shops) > 10:
            body += "\n\n(Not listed? Send *join <shop-code>* — the code is on the shop's poster.)"
        await whatsapp.send_list(phone, body, "Choose shop", [{"title": "Shops", "rows": rows}])
    finally:
        db._current_shop.reset(token)


async def shared_number_display() -> str:
    """The shared WhatsApp number in +CC format, for join links (cached for an hour)."""
    if _number_cache["value"] and time.time() - _number_cache["at"] < 3600:
        return _number_cache["value"]
    number = config.env("SHARED_WA_NUMBER")
    if not number and config.ACCESS_TOKEN and config.PHONE_NUMBER_ID:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(f"https://graph.facebook.com/{config.GRAPH_VERSION}/{config.PHONE_NUMBER_ID}",
                                     params={"fields": "display_phone_number"},
                                     headers={"Authorization": f"Bearer {config.ACCESS_TOKEN}"})
                if r.status_code == 200:
                    number = r.json().get("display_phone_number", "")
        except httpx.HTTPError as e:
            log.warning("couldn't look up the WhatsApp number: %r", e)
    if number:
        _number_cache.update(value=number, at=time.time())
    return number or ""


async def join_info(shop: dict) -> dict:
    dedicated = bool(shop.get("wa_phone_number_id") and shop.get("wa_access_token"))
    number = "" if dedicated else await shared_number_display()
    digits = re.sub(r"\D", "", number)
    text = "hi" if dedicated else f"join {shop['slug']}"
    return {"code": shop["slug"], "join_text": text, "whatsapp_number": number, "dedicated_number": dedicated,
            "wa_link": f"https://wa.me/{digits}?text={quote(text)}" if digits else ""}
