# WhatsApp Order Agent for Local Stores — Team Velora

A WhatsApp assistant that lets customers of a neighbourhood kirana store browse, order, pay and track orders in plain English, Hindi, Telugu, Hinglish or Tenglish. It also gives the store owner a live dashboard to accept/reject orders, manage stock and chat with customers.

## Stack (all free)

| Layer | Choice | Why |
|---|---|---|
| AI | **Gemini 3.5 Flash-Lite** (free tier) → fallback 3.1 Flash-Lite → Flash-Lite-latest → Groq (optional) | ~1–2 s replies, reliable parallel tool calling, understands romanised Hindi/Telugu, also handles voice notes and photos. Each model has its own free quota, so the fallback chain absorbs rate limits. |
| WhatsApp | Meta WhatsApp Cloud API (free test number) | Official API, interactive reply buttons, location pins, voice notes |
| Backend | Python FastAPI | Already in use by the team, async, fast |
| Database | SQLite (WAL mode, Python stdlib) | Zero setup, single file at `data/store.db` |
| Dashboard | Vanilla HTML/JS + Server-Sent Events | No build step; live updates |

## Conversation modes (`FLOW_MODE` in `.env`)

- **`menu` (default):** a hardcoded, tap-driven flow using WhatsApp list menus and reply buttons:
  greeting → type a product or browse categories → pick from the list → quantity → add more / cart / checkout → address (text or 📍 pin) → 💵 COD or 📲 UPI → summary → ✅ confirm → order goes to the dashboard → status updates.
  AI is used **only** when plain database search can't understand the typed text (e.g. "doodh aur bread", Telugu script) and for voice notes. Code: `app/flow.py`.
- **`ai`:** a fully conversational Gemini agent with tool calling (`app/agent.py`).

## How the agent works

```
WhatsApp ──webhook──► FastAPI ──► agent (LLM + 14 tools) ──► SQLite (catalogue · carts · orders · messages)
                         │                                         ▲
                         └──► SSE ──► Store dashboard ──REST───────┘ (accept/reject → WhatsApp notification)
```

The LLM never computes prices or stock. It calls tools, and the backend does all the money and inventory work:
`search_products · browse_catalogue · add_to_cart · update_cart_item · view_cart · clear_cart · save_customer_details · review_order · place_order · get_order_status · list_my_orders · cancel_order · reorder · message_store`.

Safety rails enforced in code (not just in the prompt):
- `place_order` only works after `review_order` showed the summary **and** the customer replied after that.
- It also only works if the cart, address and payment method are unchanged since the review (hash check).
- Stock is reserved atomically when the order is placed and restored on reject/cancel.

## Deliverables coverage

| Deliverable | Where |
|---|---|
| WhatsApp conversational ordering | `app/main.py` webhook → `app/agent.py` |
| Customer registration & identification | auto-created by phone number; name/address/language saved (`customers` table) |
| Catalogue integration, search, NL queries | `store.search_products` (fuzzy, typo-tolerant, Hindi/Telugu keywords) |
| Availability, quantity & variants | stock-aware cart; agent asks when brand/size is ambiguous |
| Cart management | add / change qty / remove / clear / reorder |
| Order creation, confirmation | two-step `review_order` → ✅ button → `place_order` |
| Address & delivery details | text address, landmark, or WhatsApp location pin (map link on dashboard) |
| AI intent understanding, clarification | Gemini tool calling + system prompt rules |
| Price & total calculation | server-side subtotal, delivery fee, free-delivery threshold |
| Status tracking, order history | `get_order_status`, `list_my_orders`, timeline in `order_events` |
| Notifications & updates | every status change from the dashboard → WhatsApp message (translated to the customer's language) |
| Store-side dashboard, accept/reject | `/` → Orders |
| Inventory integration | `/` → Inventory (live stock, low-stock badges, add products) |
| Customer ↔ store communication | `/` → Chats: staff reply, take over from AI, "needs attention" escalations |
| Payment / payment status | COD or UPI; `/pay/<code>` UPI QR + deep link; mark paid / refund due |
| Multilingual | replies in the customer's language & script; voice notes transcribed |

## Multiple shops (register · log in · separate data)

- **Register:** http://localhost:8000/login.html → *Register your shop* (shop name, business type, owner, email, password). Every shop starts empty. The owner adds real products in *Items / Inventory*, and the bot offers only what is listed and in stock (out-of-stock or hidden items disappear from WhatsApp immediately).
- **Log in / log out:** email + password; passwords are scrypt-hashed, and sessions are HttpOnly cookies (30 days). Change name, phone and password in *Store Settings → Your account*.
- **Stock:** placing an order *holds* the units (customers can't buy what's on hold). Your stock count only goes down when the order is **sent out** (Out for delivery) or **delivered**. Rejecting or cancelling releases the hold. Inventory shows "N on hold · M sellable".
- **Isolation:** every product, customer, order, chat, notification and setting belongs to one shop. Each request is scoped to the logged-in shop (`auth.ShopSessionMiddleware` + `db.shop_id()`), and live updates (SSE) only go to that shop's portal.
- **WhatsApp routing** (`app/tenancy.py`):
  - **Shared number (default), product-first** (`app/marketplace.py`): the customer just types what they want. Every open shop is searched; if one shop has it they go straight to its picker, and if several do they choose from a list (matches, lowest price, city). While their cart has items or they're checking out, they stay in that shop. "hi" shows a welcome with **Browse shops** / **Continue at <last shop>**. Each shop still has a direct link `wa.me/<number>?text=join <code>` (QR + poster in Settings).
  - **Own number (optional):** in Settings, a shop can connect its own WhatsApp Business phone-number ID + token. Messages to that number go straight to that shop.
- **Upgrading an old single-store database** turns it into shop #1, and its owner login is written to `data/first_shop_login.txt`.

## Store portal (frontend/)

The **VyaparAI** store-owner portal is at **http://localhost:8000/** (the backend serves `frontend/` directly — no build step). Everything in it is live:

| Page | What it does |
|---|---|
| Dashboard | today's orders & revenue, low-stock alerts, activity feed, store open/close switch |
| Orders | FIFO queue: Accept → Start Preparing → Mark Ready → Out for Delivery → Delivered, Reject with reason, Mark paid. Each step messages the customer on WhatsApp |
| Customer Chats | every WhatsApp conversation; reply as staff, pause the bot for one customer, resolve "needs reply" requests |
| Upload Details / Settings | store profile, logo, hours, delivery fee, free-delivery limit, minimum order, UPI ID, welcome message — the bot uses them immediately |
| Items / Inventory | stock +/−, add/edit/delete, hide from WhatsApp; low/out-of-stock alerts |
| Analysis | revenue, average order, items sold, 7-day comparison, sales by category, top items |
| Notifications | new orders, payments, stock alerts, cancellations, customers asking for help, store status |
| WhatsApp Bot Preview | chat with your real bot as a customer (tick-box picker, buttons, location) — quick prompts use your in-stock products |

Closing the store from the portal pauses WhatsApp ordering (customers can still track orders and message staff).
Dev mode with hot reload: `cd frontend && npm install && npm run dev` → http://localhost:5173 (talks to the backend on :8000).
The old minimal dashboard is still at `/classic`.

## Run it

```bash
cp .env.example .env      # fill GEMINI_API_KEY + WhatsApp creds
./run.sh                  # http://localhost:8000  (dashboard)
ngrok http 8000           # or cloudflared
```

1. In Meta App → WhatsApp → Configuration, set the callback URL to `https://<tunnel>/webhook` with your `VERIFY_TOKEN`, then subscribe to **messages**.
2. Set `PUBLIC_BASE_URL=https://<tunnel>` in `.env` so payment links work.
3. No phone handy, or venue Wi-Fi is bad? Use **Dashboard → Simulator**. It runs the same agent, database and notifications.

Notes:
- The Meta test `ACCESS_TOKEN` expires every 24 h. Use a System User token for the event day.
- WhatsApp only allows free-form messages within 24 h of the customer's last message, which is fine for order updates.
- Delete `data/store.db` to start completely fresh (no shops, no products); register again at `/login.html`.

## Files

```
app/config.py     env + defaults          app/store.py      catalogue, cart, pricing, orders
app/db.py         SQLite schema/helpers   app/agent.py      prompt, tools, tool-calling loop
app/auth.py       logins & sessions       app/llm.py        Gemini/Groq client + fallback, voice/image
app/tenancy.py    shop routing on WhatsApp
app/whatsapp.py   Cloud API in/out        app/messaging.py  outbound + status notifications
app/main.py       FastAPI routes          static/           dashboard (orders, chats, inventory, simulator, settings)
```
