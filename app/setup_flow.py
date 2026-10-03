"""One-time setup: create the "Choose items" WhatsApp Flow (checkbox product picker) and save its id to .env.

    .venv/bin/python -m app.setup_flow            # uses WABA_ID from .env or data/waba_id.txt
    .venv/bin/python -m app.setup_flow <WABA_ID>

Re-running it uploads the latest Flow JSON to the existing Flow.
"""
import json
import re
import sys

import httpx

from . import config

FLOW_JSON = {
    "version": "7.1",
    "screens": [{
        "id": "PICK_ITEMS",
        "title": "Choose items",
        "terminal": True,
        "success": True,
        "data": {
            "heading": {"type": "string", "__example__": "Results for milk"},
            "products": {
                "type": "array",
                "items": {"type": "object", "properties": {
                    "id": {"type": "string"}, "title": {"type": "string"}, "description": {"type": "string"}}},
                "__example__": [{"id": "1", "title": "Amul Taaza Toned Milk (1 L)", "description": "₹54 · Amul"}],
            },
        },
        "layout": {"type": "SingleColumnLayout", "children": [
            {"type": "TextSubheading", "text": "${data.heading}"},
            {"type": "CheckboxGroup", "name": "items", "label": "Tick everything you want",
             "required": True, "min-selected-items": 1, "data-source": "${data.products}"},
            {"type": "Footer", "label": "Add to cart",
             "on-click-action": {"name": "complete", "payload": {"items": "${form.items}"}}},
        ]},
    }],
}


def _set_env(key: str, value: str):
    path = config.ROOT / ".env"
    text = path.read_text()
    if re.search(rf"^{key}=.*$", text, flags=re.M):
        text = re.sub(rf"^{key}=.*$", f"{key}={value}", text, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n{key}={value}\n"
    path.write_text(text)


def main():
    waba = (sys.argv[1] if len(sys.argv) > 1 else config.WABA_ID) or ""
    waba_file = config.ROOT / "data" / "waba_id.txt"
    if not waba and waba_file.exists():
        waba = waba_file.read_text().strip()
    if not waba:
        sys.exit("WABA_ID unknown: send any WhatsApp message to the bot first (it records it), "
                 "or pass it: python -m app.setup_flow <WABA_ID>  (Meta → WhatsApp → API Setup)")
    config.reload_whatsapp_credentials()
    api = f"https://graph.facebook.com/{config.GRAPH_VERSION}"
    auth = {"Authorization": f"Bearer {config.ACCESS_TOKEN}"}

    flow_id = config.current_flow_id()
    if not flow_id:
        r = httpx.post(f"{api}/{waba}/flows", headers=auth, timeout=30,
                       data={"name": "Choose items (multi-select)", "categories": json.dumps(["SHOPPING"])})
        print("create flow:", r.status_code, r.text)
        r.raise_for_status()
        flow_id = r.json()["id"]
        _set_env("WABA_ID", waba)
        _set_env("WHATSAPP_FLOW_ID", flow_id)

    r = httpx.post(f"{api}/{flow_id}/assets", headers=auth, timeout=30,
                   data={"name": "flow.json", "asset_type": "FLOW_JSON"},
                   files={"file": ("flow.json", json.dumps(FLOW_JSON, ensure_ascii=False), "application/json")})
    print("upload flow json:", r.status_code, r.text)
    r.raise_for_status()
    errors = r.json().get("validation_errors") or []
    if errors:
        sys.exit(f"Flow JSON has validation errors: {errors}")
    print(f"\n✅ Flow ready: WHATSAPP_FLOW_ID={flow_id} (saved to .env, sent in draft mode)")


if __name__ == "__main__":
    main()
