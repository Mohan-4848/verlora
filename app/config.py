import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


DB_PATH = env("DB_PATH", str(ROOT / "data" / "store.db"))
STATIC_DIR = ROOT / "static"

# Public URL of this server (cloudflared/ngrok tunnel) – used for payment links
PUBLIC_BASE_URL = env("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")

# Meta WhatsApp Cloud API
VERIFY_TOKEN = env("VERIFY_TOKEN", "hackathon_token_123")
PHONE_NUMBER_ID = env("PHONE_NUMBER_ID")
ACCESS_TOKEN = env("ACCESS_TOKEN")
APP_SECRET = env("APP_SECRET")  # optional: verifies X-Hub-Signature-256 on webhooks
GRAPH_VERSION = env("GRAPH_VERSION", "v21.0")

# WhatsApp Flow used for multi-select (checkbox) product picking — created by `python -m app.setup_flow`
WABA_ID = env("WABA_ID")
WHATSAPP_FLOW_ID = env("WHATSAPP_FLOW_ID")
WHATSAPP_FLOW_DRAFT = env("WHATSAPP_FLOW_DRAFT", "true").lower() != "false"   # draft flows need no publishing


def current_flow_id() -> str:
    """The Flow id, re-read from .env until it's set (so setup_flow works without a server restart)."""
    global WHATSAPP_FLOW_ID
    if not WHATSAPP_FLOW_ID:
        WHATSAPP_FLOW_ID = (dotenv_values(ROOT / ".env").get("WHATSAPP_FLOW_ID") or "").strip()
    return WHATSAPP_FLOW_ID


def reload_whatsapp_credentials() -> bool:
    """Re-read .env so a freshly pasted ACCESS_TOKEN works without restarting. Returns True if it changed."""
    global ACCESS_TOKEN, PHONE_NUMBER_ID
    load_dotenv(ROOT / ".env", override=True)
    old = ACCESS_TOKEN
    ACCESS_TOKEN, PHONE_NUMBER_ID = env("ACCESS_TOKEN"), env("PHONE_NUMBER_ID")
    return ACCESS_TOKEN != old


# First message a customer gets on WhatsApp
GREETING = (
    "Heyyy! 👋😊\n"
    "Perfect timing — I was just wondering what we could get you today. 😄\n"
    "Are you looking for something to eat, something to drink, groceries, or maybe something you need "
    "delivered urgently? 🛵"
)

# "menu" = button/list driven flow (AI only to understand free-text product requests)
# "ai"   = fully conversational LLM agent
FLOW_MODE = env("FLOW_MODE", "menu").lower()


# LLM providers (OpenAI-compatible chat completions). Tried in order; on 429/5xx we fall through.
GEMINI_API_KEY = env("GEMINI_API_KEY")
GEMINI_MODELS = [m.strip() for m in env(
    "GEMINI_MODELS", "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-flash-lite-latest"
).split(",") if m.strip()]
GEMINI_REASONING = env("GEMINI_REASONING", "low")
GROQ_API_KEY = env("GROQ_API_KEY")
GROQ_MODELS = [m.strip() for m in env("GROQ_MODELS", "llama-3.3-70b-versatile").split(",") if m.strip()]

# Store defaults (editable later from the dashboard → Settings)
DEFAULT_SETTINGS = {
    "store_name": env("STORE_NAME", "Sri Krishna Quick Mart"),
    "store_address": env("STORE_ADDRESS", "Maisammaguda, Dulapally, Hyderabad"),
    "store_hours": "7:00 AM – 10:00 PM, all days",
    "delivery_eta": "30-45 mins",
    "delivery_fee": "25",
    "free_delivery_above": "200",
    "min_order": "0",
    "upi_id": env("STORE_UPI_ID", "srikrishnamart@upi"),
    "store_phone": env("STORE_PHONE", ""),
    # store profile shown in the portal (Upload Details / Settings)
    "business_type": "Kirana / General Store",
    "custom_business_type": "",
    "store_city": "Hyderabad",
    "store_pincode": "500100",
    "store_description": "Your neighbourhood store for milk, groceries, bakery and snacks — order on WhatsApp.",
    "opening_time": "07:00",
    "closing_time": "22:00",
    "logo_bg": "#059669",
    "logo_image": "",           # small data-URL uploaded from the portal
    "coordinates": "",
    "welcome_message": "",      # optional custom greeting line for the WhatsApp main menu
    # open / closed switch — the WhatsApp bot stops taking orders while closed
    "is_open": "1",
    "close_reason": "",
}

# Extra sites allowed to call the API from a browser (the portal published on GitHub Pages, Vite dev server).
CORS_ORIGINS = [o.strip().rstrip("/") for o in env(
    "CORS_ORIGINS", "https://mohan-4848.github.io,http://localhost:5173,http://127.0.0.1:5173").split(",") if o.strip()]

# Built store portal (served at "/"). The classic dashboard stays at /classic.
FRONTEND_DIR = Path(env("FRONTEND_DIR", str(ROOT / "frontend")))
