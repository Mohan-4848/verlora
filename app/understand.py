"""AI understanding of free-typed WhatsApp messages (used only when plain database search can't).

Given what is actually in stock, the model tells us
  • intent: "order" (wants products, even vaguely: "something cold to drink"), "greeting" (hi / thanks / chit-chat), "other"
  • items:  the closest *real product names* from the catalogue, one or more per requested thing
so the bot can show the right products, greet back, or say it doesn't know — instead of searching for "Heyyy!".
"""
import json
import logging
import re

from . import llm

log = logging.getLogger("understand")
NOTHING = {"intent": "other", "items": []}


async def understand(text: str, vocabulary: list[str], who: str, generic: bool = False) -> dict:
    """who: e.g. 'Ravi Kirana Store (a Kirana / General Store)' or 'local shops on this WhatsApp number'.
    generic=True asks for plain item words ("milk") instead of exact product names — used when comparing shops,
    so one shop's brand name doesn't win over another shop's equivalent product."""
    if not vocabulary:
        return NOTHING
    try:
        msg, _ = await llm.chat([
            {"role": "system", "content":
                f"You read WhatsApp messages that customers send to {who} in India. Messages may be in any language "
                "or script (English, Hindi, Telugu, Hinglish, Tenglish…) and may have typos.\n"
                "Products available right now:\n" + "; ".join(vocabulary[:300]) + "\n\n"
                'Reply with ONLY a JSON object like {"intent": "order", "items": ["Amul Taaza Milk", "Brown Bread"]}.\n'
                '- intent: "order" if they want or ask about products (even vaguely, e.g. "something cold to drink", '
                '"fever medicine"); "greeting" for hi/hello/thanks/small talk; "other" for anything else.\n'
                + ("- items: for each thing they want, a short generic English item word or two that appears in the "
                   'list (e.g. "milk", "bread", "cold drink", "biryani") — not brand names. For a vague request give the '
                   "1-3 best-fitting kinds of item. Use [] if nothing in the list fits."
                   if generic else
                   "- items: for each thing they want, copy the closest product name(s) from the list above exactly. "
                   "For a vague request pick the 1-3 best-fitting products. Use [] if nothing in the list fits.")},
            {"role": "user", "content": text[:500]},
        ], temperature=0)
        raw = msg.get("content") or ""
        m = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(m.group(0)) if m else {}
        intent = data.get("intent") if data.get("intent") in ("order", "greeting", "other") else "other"
        items = [str(x).strip() for x in data.get("items") or [] if str(x).strip()][:8]
        return {"intent": intent, "items": items}
    except (llm.LLMError, ValueError, AttributeError) as e:
        log.warning("AI understanding failed: %s", e)
        return NOTHING
