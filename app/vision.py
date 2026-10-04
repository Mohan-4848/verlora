"""Vision AI: Handwritten notes, grocery 'parcha' lists, and product photo ordering.

Uses Gemini Multimodal to transcribe handwritten Indian grocery lists
(English, Telugu, Hindi, Tenglish, Hinglish) and map them directly into
the store's catalogue and cart.
"""
import base64
import json
import logging
import re

import httpx

from . import config, db, i18n, messaging, store

log = logging.getLogger("vision")

GEMINI_NATIVE = "https://generativelanguage.googleapis.com/v1beta"
_client = httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=5.0))


def _build_catalogue_prompt(shop_id: int) -> str:
    """Format available products with ID, name, variant and price for Gemini Vision."""
    rows = db.all_(
        "SELECT id, name, variant, price, category FROM products "
        "WHERE shop_id=? AND active=1 AND (stock - COALESCE(reserved, 0)) > 0 ORDER BY category, name",
        (shop_id,)
    )
    if not rows:
        return "No products currently in stock."
    lines = []
    for r in rows:
        v = f" ({r['variant']})" if r.get("variant") else ""
        lines.append(f"[ID: {r['id']}] {r['name']}{v} - ₹{r['price']} [{r['category']}]")
    return "\n".join(lines)


async def analyze_grocery_image(data: bytes, mime: str, shop_id: int, caption: str | None = None) -> dict:
    """Sends image to Gemini Vision to transcribe handwritten notes and match catalogue items."""
    if not config.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is required for vision understanding")

    mime = mime.split(";")[0].strip()
    catalog_str = _build_catalogue_prompt(shop_id)
    shop = db.get_shop(shop_id) if hasattr(db, "get_shop") and callable(getattr(db, "get_shop")) else None
    shop_name = shop["name"] if shop and isinstance(shop, dict) else "Local Kirana Store"

    prompt = f"""You are the AI Vision Assistant for {shop_name}, a neighbourhood grocery and Kirana store in India.
The customer uploaded a photo via WhatsApp.

POSSIBILITIES:
1. Handwritten shopping list ("parcha"): A photo of a paper note, diary, or notebook with handwritten groceries in pen/pencil.
   The handwriting could be in English, Telugu script (తెలుగు), Devanagari Hindi (हिन्दी), or Romanized Tenglish/Hinglish
   (e.g., "paalu 2 pkt", "doodh 2 packet", "tamata 1kg", "atta 5kg", "bread 1", "chinna sugar").
2. Printed or typed grocery list / checklist / digital note.
3. A photo of grocery products, packaging, or items the customer wants to buy.
4. An unrelated photo (selfie, landscape, meme, etc.).

STORE CATALOGUE CURRENTLY IN STOCK:
{catalog_str}

TASK:
1. Carefully transcribe all items, quantities, and units written on the note or visible in the photo.
2. Detect the customer's language: 'te' (Telugu), 'hi' (Hindi), or 'en' (English).
3. If it is a grocery list or shows grocery products:
   - For each item, extract:
     • requested item name
     • quantity (integer, default 1)
     • unit if specified (e.g. packet, kg, grams, litre, bottle)
   - Match each item to the EXACT Product ID from the store catalogue above.
   - If an item is clearly requested on the list but DOES NOT exist in the catalogue, put it in 'unmatched'.
4. If the photo has no grocery items or shopping list at all, set "is_grocery_order": false.

Output ONLY a valid JSON object matching this schema (do NOT include markdown code blocks or extra text):
{{
  "is_grocery_order": true,
  "lang": "en",
  "transcription": "1. 2 packets milk\\n2. 1 bread\\n3. 1kg onions",
  "matched": [
    {{"product_id": 1, "name": "Amul Taaza Milk 500ml", "quantity": 2, "raw_text": "2 packets milk"}},
    {{"product_id": 3, "name": "Britannia 100% Whole Wheat Bread", "quantity": 1, "raw_text": "1 bread"}}
  ],
  "unmatched": ["Vim bar 250g"]
}}
"""
    if caption:
        prompt += f"\nCustomer note/caption with this photo: {caption}"

    body = {"contents": [{"parts": [
        {"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}},
        {"text": prompt}
    ]}]}

    models = config.GEMINI_MODELS or ["gemini-3.8-flash", "gemini-flash-lite-latest"]
    errors = []
    for m in models:
        try:
            r = await _client.post(
                f"{GEMINI_NATIVE}/models/{m}:generateContent",
                json=body,
                headers={"x-goog-api-key": config.GEMINI_API_KEY}
            )
            if r.status_code == 200:
                parts = r.json()["candidates"][0]["content"].get("parts", [])
                raw_text = "".join(p.get("text", "") for p in parts).strip()
                # Extract JSON from potential code block or raw string
                match = re.search(r"\{.*\}", raw_text, re.DOTALL)
                if match:
                    parsed = json.loads(match.group(0))
                    return parsed
                log.warning("Vision response was not valid JSON: %s", raw_text[:200])
                return {"is_grocery_order": False, "transcription": raw_text, "matched": [], "unmatched": []}
            errors.append(f"{m}: HTTP {r.status_code} {r.text[:200]}")
        except Exception as e:
            errors.append(f"{m}: {e!r}")

    log.error("All vision models failed: %s", "; ".join(errors))
    return {"is_grocery_order": False, "transcription": "Could not process image", "matched": [], "unmatched": []}


async def handle_handwritten_image(customer_id: int, data: bytes, mime: str, caption: str | None = None) -> dict:
    """Processes handwritten note image, updates cart, and sends localized WhatsApp response with buttons."""
    c = db.get_customer(customer_id)
    shop_id = c["shop_id"] if c.get("shop_id") else db.shop_id()

    # Analyze image with Gemini Vision
    res = await analyze_grocery_image(data, mime, shop_id, caption)
    log.info("Vision analysis result for customer %s: %s", customer_id, res)

    detected_lang = res.get("lang") or i18n.normalize_language(c.get("language"))
    if detected_lang in ("te", "hi", "en"):
        # Save customer's detected language preference
        db.run("UPDATE customers SET language=? WHERE id=?", (detected_lang, customer_id))
        c["language"] = detected_lang

    lang = i18n.normalize_language(c.get("language"))
    is_order = res.get("is_grocery_order", False)
    matched = res.get("matched", [])
    unmatched = res.get("unmatched", [])
    transcription = res.get("transcription", "")

    # If items were matched to catalogue, add them to the customer's cart
    added_lines = []
    problems = []
    if is_order and matched:
        for item in matched:
            pid = item.get("product_id")
            qty = max(1, int(item.get("quantity", 1)))
            if pid:
                r = store.set_cart_qty(customer_id, pid, qty, add=True)
                if r["ok"]:
                    p = r.get("product") or store.get_product(pid)
                    price_str = store.money((p["price"] if p else 0) * qty)
                    added_lines.append(f"• {qty} × {r['item']} — {price_str}")
                else:
                    problems.append(f"{item.get('name', 'Item')}: {r.get('error')}")

    cart = store.cart(customer_id)

    # Format localized response message
    if added_lines:
        if lang == "te":
            heading = "📝 *హస్తలిఖిత జాబితా గుర్తించబడింది!*\n_(Handwritten list recognized)_"
            added_title = "✅ *మీ కార్ట్‌కి జోడించబడినవి:*"
            unmatched_title = "⚠️ *దుకాణంలో ప్రస్తుతం లభించనివి:*"
            cart_status = f"🧺 కార్ట్: {cart['item_count']} వస్తువులు · *{store.money(cart['total'])}*"
            btn_view = "🛒 కార్ట్ చూడండి"
            btn_order = "💳 ఆర్డర్ చేయండి"
            btn_more = "➕ మరిన్ని వస్తువులు"
        elif lang == "hi":
            heading = "📝 *हस्तलिखित पर्ची पढ़ी गई!*\n_(Handwritten list recognized)_"
            added_title = "✅ *आपके कार्ट में जोड़े गए:*"
            unmatched_title = "⚠️ *स्टोर में फिलहाल उपलब्ध नहीं:*"
            cart_status = f"🧺 कुल कार्ट: {cart['item_count']} सामान · *{store.money(cart['total'])}*"
            btn_view = "🛒 कार्ट देखें"
            btn_order = "💳 ऑर्डर करें"
            btn_more = "➕ और सामान जोड़ें"
        else:
            heading = "📝 *Handwritten Shopping List Recognized!*"
            added_title = "✅ *Added to your cart:*"
            unmatched_title = "⚠️ *Not available in store right now:*"
            cart_status = f"🧺 Cart: {cart['item_count']} items · *{store.money(cart['total'])}*"
            btn_view = "🛒 View Cart"
            btn_order = "💳 Checkout"
            btn_more = "➕ Add More"

        body = f"{heading}\n\n{added_title}\n" + "\n".join(added_lines)

        if unmatched:
            body += f"\n\n{unmatched_title}\n" + "\n".join(f"• {u}" for u in unmatched)

        if problems:
            body += "\n\n⚠️ " + "\n⚠️ ".join(problems)

        body += f"\n\n{cart_status}"
        if cart.get("free_delivery_hint"):
            body += f"\n_💡 {cart['free_delivery_hint']}_"

        # Update customer state to 'added' so Checkout / Cart buttons work naturally
        flow_data = {"heading": "Handwritten List", "results": [m.get("product_id") for m in matched if m.get("product_id")]}
        db.run("UPDATE customers SET flow_state='added', flow_data=? WHERE id=?",
               (json.dumps(flow_data), customer_id))

        buttons = [
            ("cart:checkout", btn_order[:20]),
            ("m:cart", btn_view[:20]),
            ("cart:more", btn_more[:20])
        ]
        await messaging.send_to_customer(c, body, "agent", buttons=buttons)

    elif is_order and unmatched:
        # Items were read from the note, but none matched the store catalogue
        if lang == "te":
            msg = (f"📝 *మీ జాబితా:* \n{transcription}\n\n"
                   f"⚠️ క్షమించండి, ఈ వస్తువులు ప్రస్తుతం మా దుకాణంలో అందుబాటులో లేవు. "
                   f"లభించే వస్తువులను చూడటానికి క్రింది బటన్ నొక్కండి.")
            btn_browse = "🛍️ వస్తువుల జాబితా"
            btn_menu = "🏠 హోమ్ మెనూ"
        elif lang == "hi":
            msg = (f"📝 *आपकी पर्ची:* \n{transcription}\n\n"
                   f"⚠️ क्षमा करें, ये सामान अभी हमारे स्टोर में उपलब्ध नहीं हैं। "
                   f"उपलब्ध सामान देखने के लिए नीचे बटन दबाएं।")
            btn_browse = "🛍️ सामान देखें"
            btn_menu = "🏠 मुख्य मेनू"
        else:
            msg = (f"📝 *Your list was read:* \n{transcription}\n\n"
                   f"⚠️ Sorry, these items are currently not in stock at our store. "
                   f"Tap Browse below to see what is available!")
            btn_browse = "🛍️ Browse Store"
            btn_menu = "🏠 Main Menu"

        buttons = [("m:browse", btn_browse[:20]), ("m:menu", btn_menu[:20])]
        await messaging.send_to_customer(c, msg, "agent", buttons=buttons)

    else:
        # Image wasn't a shopping list
        if lang == "te":
            msg = (f"📷 మీ ఫోటో అందింది: _{transcription[:150]}_\n\n"
                   f"💡 *చిట్కా:* మీరు మీ కిరాణా సామాను చేతితో రాసిన కాగితం (Parcha) ఫోటో పంపితే, "
                   f"నేను వెంటనే మీ కార్ట్‌కి జోడిస్తాను! లేదా మీకు కావలసిన వస్తువుల పేర్లు టైప్ చేయండి.")
            btn_browse = "🛍️ కేటలాగ్ చూడండి"
            btn_menu = "🏠 హోమ్ మెనూ"
        elif lang == "hi":
            msg = (f"📷 आपकी फोटो मिली: _{transcription[:150]}_\n\n"
                   f"💡 *सलाह:* आप अपनी किराने के सामान की हाथ से लिखी पर्ची की फोटो भेजें, "
                   f"मैं सीधे आपके कार्ट में सामान जोड़ दूँगा! या सामान का नाम लिखकर भेजें।")
            btn_browse = "🛍️ सामान देखें"
            btn_menu = "🏠 मुख्य मेनू"
        else:
            msg = (f"📷 Photo received: _{transcription[:150]}_\n\n"
                   f"💡 *Tip:* Send a photo of your handwritten grocery list (\"parcha\"), "
                   f"and I'll automatically add the items to your cart! You can also type what you need.")
            btn_browse = "🛍️ Browse Store"
            btn_menu = "🏠 Main Menu"

        buttons = [("m:browse", btn_browse[:20]), ("m:menu", btn_menu[:20])]
        await messaging.send_to_customer(c, msg, "agent", buttons=buttons)

    return {
        "ok": True,
        "is_grocery_order": is_order,
        "transcription": transcription,
        "added_count": len(added_lines),
        "cart_total": cart["total"]
    }
