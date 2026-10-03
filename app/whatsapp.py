"""Meta WhatsApp Cloud API: inbound parsing, outbound text/buttons, media download."""
import hashlib
import hmac
import json
import logging
import uuid

import httpx

from . import config

log = logging.getLogger("whatsapp")
_client = httpx.AsyncClient(timeout=15.0)


def _api(path: str) -> str:
    return f"https://graph.facebook.com/{config.GRAPH_VERSION}/{path}"


def _creds() -> tuple[str, str]:
    """(phone_number_id, access_token): the current shop's dedicated number if it has one, else the shared number."""
    from . import db
    sid = db.current_shop_id()
    shop = db.get_shop(sid) if sid is not None else None
    if shop and shop.get("wa_phone_number_id") and shop.get("wa_access_token"):
        return shop["wa_phone_number_id"], shop["wa_access_token"]
    return config.PHONE_NUMBER_ID, config.ACCESS_TOKEN


def _headers() -> dict:
    return {"Authorization": f"Bearer {_creds()[1]}"}


def verify_signature(raw_body: bytes, header: str | None) -> bool:
    if not config.APP_SECRET:
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(config.APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[7:])


def parse_inbound(payload: dict) -> list[dict]:
    """Flattens a webhook payload into simple message dicts (ignores delivery/read receipts)."""
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            business_number_id = value.get("metadata", {}).get("phone_number_id")
            names = {c.get("wa_id"): c.get("profile", {}).get("name") for c in value.get("contacts", [])}
            for m in value.get("messages", []):
                msg = {"wa_id": m.get("id"), "from": m.get("from"), "name": names.get(m.get("from")),
                       "to_number_id": business_number_id,
                       "type": m.get("type"), "text": None}
                t = m.get("type")
                if t == "text":
                    msg["text"] = m["text"].get("body", "")
                elif t == "interactive" and m.get("interactive", {}).get("type") == "nfm_reply":
                    # a submitted WhatsApp Flow (checkbox product picker)
                    try:
                        resp = json.loads(m["interactive"]["nfm_reply"].get("response_json") or "{}")
                    except ValueError:
                        resp = {}
                    ids = [str(x) for x in resp.get("items") or []]
                    msg["text"] = f"🛒 Selected {len(ids)} item(s)"
                    msg["reply_id"] = "multi:" + ",".join(ids)
                elif t == "interactive":
                    i = m.get("interactive", {})
                    reply = i.get("button_reply") or i.get("list_reply") or {}
                    msg["text"] = reply.get("title", "")
                    msg["reply_id"] = reply.get("id")
                elif t == "button":
                    msg["text"] = m.get("button", {}).get("text", "")
                elif t == "location":
                    msg["location"] = m.get("location", {})
                elif t in ("audio", "image", "document", "video", "sticker"):
                    media = m.get(t, {})
                    msg["media_id"] = media.get("id")
                    msg["mime"] = media.get("mime_type", "")
                    msg["text"] = media.get("caption")
                out.append(msg)
    return out


async def _post(payload: dict, retry: bool = True) -> bool:
    number_id, token = _creds()
    if not (token and number_id):
        log.warning("WhatsApp ACCESS_TOKEN / PHONE_NUMBER_ID missing – not sending")
        return False
    try:
        r = await _client.post(_api(f"{number_id}/messages"), headers=_headers(),
                               json={"messaging_product": "whatsapp", **payload})
    except httpx.HTTPError as e:
        log.error("WhatsApp send error: %r", e)
        return False
    if r.status_code == 401 and retry and config.reload_whatsapp_credentials():
        log.info("Picked up a new ACCESS_TOKEN from .env – retrying")
        return await _post(payload, retry=False)
    if r.status_code == 401:
        log.error("WhatsApp ACCESS_TOKEN expired/invalid – paste a new one into .env (no restart needed)")
        return False
    if r.status_code != 200:
        log.error("WhatsApp send failed %s: %s", r.status_code, r.text[:400])
        return False
    return True


async def send_text(to: str, body: str) -> bool:
    ok = True
    for i in range(0, len(body), 4000):   # WhatsApp text limit is 4096 chars
        ok &= await _post({"recipient_type": "individual", "to": to, "type": "text",
                           "text": {"preview_url": True, "body": body[i:i + 4000]}})
    return ok


async def send_buttons(to: str, body: str, buttons: list[tuple[str, str]]) -> bool:
    if len(body) > 1024:   # interactive body limit: send the long text first, then short buttons
        await send_text(to, body)
        body = "👇 Choose an option"
    return await _post({
        "recipient_type": "individual", "to": to, "type": "interactive",
        "interactive": {"type": "button", "body": {"text": body}, "action": {"buttons": [
            {"type": "reply", "reply": {"id": bid, "title": title[:20]}} for bid, title in buttons[:3]
        ]}},
    })


async def send_list(to: str, body: str, button: str, sections: list[dict]) -> bool:
    """sections: [{"title": str, "rows": [{"id", "title", "description"}]}] — max 10 rows in total."""
    if len(body) > 4096:
        await send_text(to, body)
        body = "👇 Choose an option"
    return await _post({
        "recipient_type": "individual", "to": to, "type": "interactive",
        "interactive": {"type": "list", "body": {"text": body}, "action": {
            "button": button[:20],
            "sections": [{"title": sec["title"][:24], "rows": [
                {"id": r["id"], "title": r["title"][:24], **({"description": r["description"][:72]} if r.get("description") else {})}
                for r in sec["rows"]
            ]} for sec in sections],
        }},
    })


async def send_flow(to: str, body: str, cta: str, screen: str, data: dict) -> bool:
    """Opens our WhatsApp Flow (a form inside WhatsApp) on `screen`, pre-filled with `data`."""
    params = {"flow_message_version": "3", "flow_token": f"pick-{uuid.uuid4().hex[:12]}",
              "flow_id": config.current_flow_id(), "flow_cta": cta[:30], "flow_action": "navigate",
              "flow_action_payload": {"screen": screen, "data": data}}
    if config.WHATSAPP_FLOW_DRAFT:
        params["mode"] = "draft"
    return await _post({"recipient_type": "individual", "to": to, "type": "interactive",
                        "interactive": {"type": "flow", "body": {"text": body[:1024]},
                                        "action": {"name": "flow", "parameters": params}}})


async def mark_read_typing(wa_message_id: str):
    """Blue ticks + 'typing…' bubble while the agent thinks."""
    await _post({"status": "read", "message_id": wa_message_id, "typing_indicator": {"type": "text"}})


async def download_media(media_id: str) -> tuple[bytes, str]:
    r = await _client.get(_api(media_id), headers=_headers())
    r.raise_for_status()
    meta = r.json()
    f = await _client.get(meta["url"], headers=_headers())
    f.raise_for_status()
    return f.content, meta.get("mime_type", "application/octet-stream")
