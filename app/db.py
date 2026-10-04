import logging
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path

from . import config

log = logging.getLogger("db")
_local = threading.local()

# The shop the current request / WhatsApp conversation belongs to. Every shop-owned query is filtered by it,
# so one shop can never read or change another shop's data.
_current_shop: ContextVar[int | None] = ContextVar("current_shop", default=None)


def shop_id() -> int:
    sid = _current_shop.get()
    if sid is None:
        raise RuntimeError("No shop selected for this operation")
    return sid


def current_shop_id() -> int | None:
    return _current_shop.get()


def use_shop(sid: int | None):
    """Selects the shop for the rest of this task (asyncio tasks copy the context, so it stays isolated)."""
    return _current_shop.set(sid)


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (            -- legacy single-store settings (migrated into shop_settings)
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS shops (
    id                 INTEGER PRIMARY KEY,
    slug               TEXT UNIQUE NOT NULL,      -- customers send "join <slug>" on WhatsApp
    name               TEXT NOT NULL,
    code_prefix        TEXT,                      -- order codes, e.g. SK1001
    wa_phone_number_id TEXT,                      -- optional dedicated WhatsApp number for this shop
    wa_access_token    TEXT,
    active             INTEGER DEFAULT 1,
    created_at         TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    shop_id       INTEGER NOT NULL REFERENCES shops(id),
    name          TEXT,
    email         TEXT UNIQUE NOT NULL,
    phone         TEXT,
    password_hash TEXT NOT NULL,
    role          TEXT DEFAULT 'owner',
    created_at    TEXT,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT,
    expires_at TEXT
);

CREATE TABLE IF NOT EXISTS shop_settings (
    shop_id INTEGER NOT NULL REFERENCES shops(id),
    key     TEXT NOT NULL,
    value   TEXT,
    PRIMARY KEY (shop_id, key)
);

CREATE TABLE IF NOT EXISTS wa_sessions (           -- which shop a WhatsApp number is talking to (shared number)
    phone      TEXT PRIMARY KEY,
    shop_id    INTEGER NOT NULL REFERENCES shops(id),
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS customers (
    id               INTEGER PRIMARY KEY,
    shop_id          INTEGER REFERENCES shops(id),
    phone            TEXT NOT NULL,
    wa_name          TEXT,
    name             TEXT,
    language         TEXT DEFAULT 'en',
    address          TEXT,
    landmark         TEXT,
    latitude         REAL,
    longitude        REAL,
    channel          TEXT DEFAULT 'whatsapp',      -- whatsapp | sim
    bot_paused       INTEGER DEFAULT 0,            -- store staff took over the chat
    needs_attention  INTEGER DEFAULT 0,            -- customer asked for a human
    checkout_token   TEXT,                         -- set by review_order, required by place_order
    checkout_payment TEXT,
    checkout_msg_id  INTEGER,                      -- last customer message id when the summary was shown
    created_at       TEXT,
    last_seen_at     TEXT,
    UNIQUE (shop_id, phone)
);

CREATE TABLE IF NOT EXISTS products (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    brand      TEXT,
    category   TEXT NOT NULL,
    variant    TEXT,                   -- e.g. "500 ml", "1 kg", "Pack of 6"
    price      REAL NOT NULL,
    mrp        REAL,
    stock      INTEGER NOT NULL DEFAULT 0,
    keywords   TEXT DEFAULT '',        -- synonyms incl. Hindi/Telugu words
    active     INTEGER DEFAULT 1,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS cart_items (
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    product_id  INTEGER NOT NULL REFERENCES products(id),
    quantity    INTEGER NOT NULL,
    added_at    TEXT,
    PRIMARY KEY (customer_id, product_id)
);

CREATE TABLE IF NOT EXISTS orders (
    id             INTEGER PRIMARY KEY,
    code           TEXT UNIQUE,
    customer_id    INTEGER NOT NULL REFERENCES customers(id),
    status         TEXT NOT NULL,      -- pending|accepted|packed|out_for_delivery|delivered|rejected|cancelled
    subtotal       REAL NOT NULL,
    delivery_fee   REAL NOT NULL,
    total          REAL NOT NULL,
    payment_method TEXT NOT NULL,      -- COD | UPI
    payment_status TEXT NOT NULL,      -- pending | paid | refunded
    customer_name  TEXT,
    address        TEXT,
    landmark       TEXT,
    latitude       REAL,
    longitude      REAL,
    notes          TEXT,
    reject_reason  TEXT,
    eta            TEXT,
    created_at     TEXT,
    updated_at     TEXT
);

CREATE TABLE IF NOT EXISTS order_items (
    id         INTEGER PRIMARY KEY,
    order_id   INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER REFERENCES products(id),
    name       TEXT,
    variant    TEXT,
    price      REAL,
    quantity   INTEGER,
    line_total REAL
);

CREATE TABLE IF NOT EXISTS order_events (
    id         INTEGER PRIMARY KEY,
    order_id   INTEGER NOT NULL REFERENCES orders(id),
    status     TEXT,
    note       TEXT,
    actor      TEXT,                   -- customer | store | system
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    direction   TEXT NOT NULL,         -- in | out
    sender      TEXT NOT NULL,         -- customer | agent | store | system
    kind        TEXT DEFAULT 'text',   -- text | voice | image | location | buttons
    body        TEXT,
    wa_id       TEXT UNIQUE,           -- WhatsApp message id (dedupes webhook retries)
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY,
    type        TEXT NOT NULL,         -- new_order | low_stock | out_of_stock | payment_received | order_dispatched |
                                       -- order_delivered | order_cancelled | order_rejected | customer_message | store_status
    title       TEXT,
    message     TEXT,
    order_id    INTEGER,
    product_id  INTEGER,
    customer_id INTEGER,
    is_read     INTEGER DEFAULT 0,
    created_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_customer ON messages(customer_id, id);
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id, id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_products_shop ON products(shop_id);
CREATE INDEX IF NOT EXISTS idx_orders_shop ON orders(shop_id, id);
CREATE INDEX IF NOT EXISTS idx_customers_shop ON customers(shop_id);
CREATE INDEX IF NOT EXISTS idx_notifications_shop ON notifications(shop_id, id);
CREATE INDEX IF NOT EXISTS idx_users_shop ON users(shop_id);
"""


# Columns added after the first release (applied on startup to existing databases)
MIGRATIONS = {
    "customers": {"checkout_msg_id": "INTEGER", "flow_state": "TEXT", "flow_data": "TEXT"},
    "messages": {"meta": "TEXT"},   # JSON: buttons / list options shown with an outgoing message
    "products": {"description": "TEXT", "deleted": "INTEGER DEFAULT 0", "shop_id": "INTEGER",
                 "reserved": "INTEGER DEFAULT 0"},          # held by open orders; available = stock - reserved
    "orders": {"shop_id": "INTEGER",
               "stock_deducted": "INTEGER DEFAULT 1"},      # 1 = stock already taken off (old orders); new orders start at 0
    "notifications": {"shop_id": "INTEGER"},
}


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(config.DB_PATH, isolation_level=None, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=5000")
        _local.conn = c
    return c


@contextmanager
def tx():
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise


def one(sql: str, params=()) -> dict | None:
    row = conn().execute(sql, params).fetchone()
    return dict(row) if row else None


def all_(sql: str, params=()) -> list[dict]:
    return [dict(r) for r in conn().execute(sql, params).fetchall()]


def run(sql: str, params=()) -> sqlite3.Cursor:
    return conn().execute(sql, params)


# ---------- shops ----------

def get_shop(sid: int | None = None) -> dict | None:
    return one("SELECT * FROM shops WHERE id=?", (sid if sid is not None else shop_id(),))


def slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (name or "shop").lower()).strip("-")[:24] or "shop"
    slug, n = base, 2
    while one("SELECT 1 FROM shops WHERE slug=?", (slug,)):
        slug, n = f"{base}-{n}", n + 1
    return slug


def code_prefix_for(name: str) -> str:
    words = re.findall(r"[A-Za-z]+", name or "")
    return ("".join(w[0] for w in words[:2]) if len(words) >= 2 else (words[0][:2] if words else "OR")).upper()


def create_shop(name: str, settings: dict | None = None) -> dict:
    with tx() as c:
        cur = c.execute("INSERT INTO shops(slug, name, code_prefix, created_at) VALUES(?,?,?,?)",
                        (slugify(name), name.strip(), code_prefix_for(name), now()))
        sid = cur.lastrowid
        values = {**(settings or {}), "store_name": name.strip()}
        for k, v in values.items():
            if k in config.DEFAULT_SETTINGS:
                c.execute("INSERT INTO shop_settings(shop_id, key, value) VALUES(?,?,?)", (sid, k, str(v)))
    return get_shop(sid)


# ---------- settings (per shop) ----------

def get_settings() -> dict:
    sid = shop_id()
    s = dict(config.DEFAULT_SETTINGS)
    shop = get_shop(sid)
    if shop:
        s["store_name"] = shop["name"]
    s.update({r["key"]: r["value"] for r in all_("SELECT key, value FROM shop_settings WHERE shop_id=?", (sid,))})
    return s


def _hours_label(opening: str, closing: str) -> str:
    if (opening == "00:00" and closing in ("23:59", "24:00", "00:00")) or (opening == closing):
        return "Open 24x7, all days"
    def fmt(hhmm: str) -> str:
        h, m = (int(x) for x in hhmm.split(":")[:2])
        return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"
    try:
        return f"{fmt(opening)} – {fmt(closing)}"
    except (ValueError, AttributeError):
        return "Open 24x7, all days"


def set_settings(values: dict):
    sid = shop_id()
    values = dict(values)
    if "opening_time" in values or "closing_time" in values:
        cur = get_settings()
        label = _hours_label(values.get("opening_time", cur["opening_time"]), values.get("closing_time", cur["closing_time"]))
        if label:
            values["store_hours"] = label
    with tx() as c:
        for k, v in values.items():
            if k in config.DEFAULT_SETTINGS:
                c.execute("INSERT INTO shop_settings(shop_id, key, value) VALUES(?, ?, ?) "
                          "ON CONFLICT(shop_id, key) DO UPDATE SET value=excluded.value", (sid, k, str(v)))
        if values.get("store_name", "").strip():
            c.execute("UPDATE shops SET name=? WHERE id=?", (values["store_name"].strip(), sid))


# ---------- customers (per shop) ----------

def get_or_create_customer(phone: str, wa_name: str | None = None, channel: str = "whatsapp") -> dict:
    sid = shop_id()
    c = one("SELECT * FROM customers WHERE shop_id=? AND phone=?", (sid, phone))
    if c is None:
        run("INSERT INTO customers(shop_id, phone, wa_name, channel, created_at, last_seen_at) VALUES(?,?,?,?,?,?)",
            (sid, phone, wa_name, channel, now(), now()))
    else:
        run("UPDATE customers SET last_seen_at=?, wa_name=COALESCE(?, wa_name) WHERE id=?",
            (now(), wa_name, c["id"]))
    return one("SELECT * FROM customers WHERE shop_id=? AND phone=?", (sid, phone))


def get_customer(customer_id: int) -> dict | None:
    return one("SELECT * FROM customers WHERE id=? AND shop_id=?", (customer_id, shop_id()))


def add_message(customer_id: int, direction: str, sender: str, body: str,
                kind: str = "text", wa_id: str | None = None, meta: str | None = None) -> dict | None:
    """Returns the stored message, or None if this WhatsApp message id was already seen."""
    try:
        cur = run("INSERT INTO messages(customer_id, direction, sender, kind, body, wa_id, meta, created_at) "
                  "VALUES(?,?,?,?,?,?,?,?)", (customer_id, direction, sender, kind, body, wa_id, meta, now()))
    except sqlite3.IntegrityError:
        return None
    return one("SELECT * FROM messages WHERE id=?", (cur.lastrowid,))


# ---------- startup & migration from the single-store version ----------

def _rebuild_customers_with_shop():
    """customers.phone used to be globally UNIQUE; now it's unique per shop. SQLite needs a table rebuild."""
    c = conn()
    c.execute("PRAGMA foreign_keys=OFF")
    try:
        cols = [r["name"] for r in all_("PRAGMA table_info(customers)")]
        c.execute("BEGIN")
        create_sql = SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS customers"):]
        create_sql = create_sql[:create_sql.index(");") + 2].replace("IF NOT EXISTS customers", "customers_new")
        c.execute(create_sql)
        new_cols = {r["name"] for r in all_("PRAGMA table_info(customers_new)")}
        for col, kind in MIGRATIONS["customers"].items():
            if col not in new_cols:
                c.execute(f"ALTER TABLE customers_new ADD COLUMN {col} {kind}")
                new_cols.add(col)
        shared = [col for col in cols if col != "shop_id" and col in new_cols]
        c.execute(f"INSERT INTO customers_new(shop_id, {', '.join(shared)}) SELECT 1, {', '.join(shared)} FROM customers")
        c.execute("DROP TABLE customers")
        c.execute("ALTER TABLE customers_new RENAME TO customers")
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    finally:
        c.execute("PRAGMA foreign_keys=ON")


def _migrate_legacy_store():
    """The pre-multi-shop database becomes shop #1, with a generated owner login."""
    legacy = {r["key"]: r["value"] for r in all_("SELECT key, value FROM settings")}
    has_data = one("SELECT COUNT(*) AS n FROM products")["n"] or one("SELECT COUNT(*) AS n FROM orders")["n"]
    if not has_data and not legacy:
        return
    name = legacy.get("store_name") or config.DEFAULT_SETTINGS["store_name"]
    with tx() as c:
        c.execute("INSERT INTO shops(id, slug, name, code_prefix, created_at) VALUES(1,?,?,?,?)",
                  (slugify(name), name, "SK", now()))
        for k, v in legacy.items():
            c.execute("INSERT OR REPLACE INTO shop_settings(shop_id, key, value) VALUES(1,?,?)", (k, v))
        for table in ("products", "orders", "notifications"):
            c.execute(f"UPDATE {table} SET shop_id=1 WHERE shop_id IS NULL")
    from .auth import hash_password
    password = secrets.token_urlsafe(8)
    email = f"owner@{get_shop(1)['slug']}.shop"
    run("INSERT INTO users(shop_id, name, email, password_hash, role, created_at) VALUES(1,?,?,?, 'owner', ?)",
        ("Store Owner", email, hash_password(password), now()))
    path = Path(config.DB_PATH).parent / "first_shop_login.txt"
    path.write_text(f"Login for your existing shop \"{name}\"\n  email:    {email}\n  password: {password}\n"
                    "Change it after logging in (Store Settings → Account).\n")
    log.warning("Existing store migrated to shop #1 — login saved in %s", path)


def _ensure_example_shop():
    """Ensure example shop exists in the database and is configured to Open 24x7."""
    if one("SELECT COUNT(*) AS n FROM shops")["n"] > 0:
        with tx() as c:
            for shop in all_("SELECT id FROM shops"):
                sid = shop["id"]
                for k, v in [
                    ("is_open", "1"),
                    ("close_reason", ""),
                    ("store_hours", "Open 24x7, all days"),
                    ("opening_time", "00:00"),
                    ("closing_time", "23:59"),
                ]:
                    c.execute("INSERT OR REPLACE INTO shop_settings(shop_id, key, value) VALUES(?,?,?)", (sid, k, v))
        return

    name = config.DEFAULT_SETTINGS["store_name"]
    shop = create_shop(name, config.DEFAULT_SETTINGS)
    sid = shop["id"]

    example_products = [
        # Dairy
        (sid, "Amul Taaza Toned Milk", "Amul", "Dairy", "500ml", 27.0, 27.0, 50, "milk doodh paalu fresh dairy", "Fresh pasteurised toned milk 500ml"),
        (sid, "Amul Taaza Toned Milk", "Amul", "Dairy", "1 L", 54.0, 54.0, 50, "milk doodh paalu fresh dairy 1l 1litre", "Fresh pasteurised toned milk 1 litre"),
        (sid, "Amul Gold Full Cream Milk", "Amul", "Dairy", "500ml", 33.0, 33.0, 40, "milk doodh paalu gold cream dairy", "High fat rich full cream milk 500ml"),
        (sid, "Amul Gold Full Cream Milk", "Amul", "Dairy", "1 L", 66.0, 66.0, 40, "milk doodh paalu gold cream dairy 1l", "High fat rich full cream milk 1 litre"),
        (sid, "Fresh Malai Paneer", "Amul", "Dairy", "200g", 90.0, 95.0, 30, "paneer cottage cheese dairy cooking", "Soft and fresh rich paneer cubes 200g"),
        (sid, "Amul Masti Dahi Curd", "Amul", "Dairy", "400g", 40.0, 40.0, 35, "curd dahi perugu yogurt dairy", "Thick and fresh probiotic curd 400g"),
        (sid, "Amul Butter", "Amul", "Dairy", "100g", 58.0, 60.0, 30, "butter makhan venna salted dairy", "Utterly butterly delicious salted butter 100g"),
        # Bakery
        (sid, "Britannia Milk Bread", "Britannia", "Bakery", "400g", 45.0, 45.0, 30, "bread loaf bakery breakfast toast", "Soft enriched daily white milk bread 400g"),
        (sid, "Britannia Brown Bread", "Britannia", "Bakery", "400g", 55.0, 55.0, 25, "bread brown wheat bakery healthy", "Nutritious whole wheat brown bread 400g"),
        (sid, "Fresh Pav Buns", "Local Bakery", "Bakery", "6 pcs", 30.0, 30.0, 25, "pav bun buns pavbhaji bakery", "Fresh and soft pav buns pack of 6"),
        (sid, "Farm Fresh White Eggs", "Farm Fresh", "Bakery", "Tray of 6", 45.0, 50.0, 40, "eggs egg anda guddu breakfast", "Nutritious farm fresh protein rich eggs pack of 6"),
        (sid, "Farm Fresh White Eggs", "Farm Fresh", "Bakery", "Tray of 30", 210.0, 240.0, 15, "eggs egg anda guddu tray wholesale", "Farm fresh eggs complete tray of 30"),
        # Staples
        (sid, "Aashirvaad Shudh Chakki Atta", "Aashirvaad", "Staples", "5kg", 245.0, 260.0, 20, "atta wheat flour godhuma pindi roti", "100% whole wheat chakki ground atta 5kg"),
        (sid, "Aashirvaad Shudh Chakki Atta", "Aashirvaad", "Staples", "10kg", 475.0, 500.0, 15, "atta wheat flour godhuma pindi roti 10kg", "100% whole wheat chakki ground atta 10kg"),
        (sid, "India Gate Basmati Rice", "India Gate", "Staples", "1kg", 130.0, 145.0, 30, "rice chawal biyyam basmati biryani", "Aromatic long grain basmati rice 1kg"),
        (sid, "India Gate Basmati Rice", "India Gate", "Staples", "5kg", 590.0, 650.0, 15, "rice chawal biyyam basmati 5kg", "Aromatic long grain basmati rice 5kg"),
        (sid, "Tata Sampann Toor Dal", "Tata Sampann", "Staples", "1kg", 160.0, 175.0, 25, "dal daal toor kandi pappu pulses lentils", "High protein unpolished toor dal 1kg"),
        (sid, "Tata Salt Vacuum Evaporated", "Tata", "Staples", "1kg", 28.0, 28.0, 50, "salt namak uppu iodised groceries", "Desh ka namak pure iodised salt 1kg"),
        (sid, "Fortune Refined Sunflower Oil", "Fortune", "Staples", "1 L", 145.0, 160.0, 30, "oil tel nune sunflower cooking", "Healthy cooking refined sunflower oil 1 litre"),
        # Fresh Veggies
        (sid, "Fresh Potatoes", "Farm", "Veggies", "1kg", 35.0, 40.0, 40, "potato aloo bangaladumpa vegetables veggies", "Fresh farm potatoes 1kg"),
        (sid, "Fresh Red Onions", "Farm", "Veggies", "1kg", 40.0, 45.0, 40, "onion pyaz ullipayalu vegetables veggies", "Fresh crunchy red onions 1kg"),
        (sid, "Fresh Tomatoes", "Farm", "Veggies", "1kg", 30.0, 35.0, 40, "tomato tamatar tamata vegetables veggies", "Juicy ripe cooking tomatoes 1kg"),
        (sid, "Fresh Green Chillies", "Farm", "Veggies", "250g", 20.0, 25.0, 30, "chilli mirchi mirapakayalu spicy veggies", "Fresh spicy green chillies 250g"),
        # Snacks & Beverages
        (sid, "Maggi 2-Minute Masala Noodles", "Nestle", "Snacks", "4-Pack", 56.0, 60.0, 50, "maggi noodles masala snacks instant", "Favorite instant 2-minute masala noodles 4 pack"),
        (sid, "Parle-G Gluco Biscuits", "Parle", "Snacks", "250g", 25.0, 25.0, 60, "parleg biscuits chai biscuit snacks", "Classic glucose tea biscuits 250g"),
        (sid, "Britannia Good Day Butter Cookies", "Britannia", "Snacks", "200g", 35.0, 35.0, 45, "goodday biscuits cookies butter snacks", "Crispy butter rich cookies 200g"),
        (sid, "Thums Up Charged Cola", "Thums Up", "Beverages", "750ml", 45.0, 45.0, 30, "thumsup cold drink soda soft drink cola", "Refreshing strong cola drink 750ml pet bottle"),
        (sid, "Coca Cola", "Coca Cola", "Beverages", "750ml", 45.0, 45.0, 30, "coke cocacola cold drink soda beverage", "Chilled original taste coca cola 750ml"),
        (sid, "Tata Tea Gold Leaf Tea", "Tata Tea", "Beverages", "500g", 310.0, 330.0, 20, "tea chai patti tea powder beverages", "Rich aroma premium black tea 500g"),
    ]
    with tx() as c:
        for p in example_products:
            c.execute(
                "INSERT INTO products(shop_id, name, brand, category, variant, price, mrp, stock, keywords, description, active, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,1,?)",
                (*p, now())
            )

    from .auth import hash_password
    pwd = "password123"
    email = f"owner@{shop['slug']}.shop"
    run("INSERT INTO users(shop_id, name, email, password_hash, role, created_at) VALUES(1,?,?,?, 'owner', ?)",
        ("Store Owner", email, hash_password(pwd), now()))
    path = Path(config.DB_PATH).parent / "first_shop_login.txt"
    path.write_text(f"Login for your existing shop \"{name}\"\n  email:    {email}\n  password: {pwd}\n"
                    "Change it after logging in (Store Settings → Account).\n")
    log.info("Example 24x7 shop '%s' created with initial catalogue", name)


def init():
    c = conn()
    old_customers = {r["name"] for r in all_("PRAGMA table_info(customers)")}
    c.executescript(SCHEMA)
    for table, columns in MIGRATIONS.items():
        existing = {r["name"] for r in all_(f"PRAGMA table_info({table})")}
        for col, kind in columns.items():
            if col not in existing:
                run(f"ALTER TABLE {table} ADD COLUMN {col} {kind}")
    if old_customers and "shop_id" not in old_customers:
        _rebuild_customers_with_shop()
    if one("SELECT COUNT(*) AS n FROM shops")["n"] == 0:
        _migrate_legacy_store()
    _ensure_example_shop()
    c.executescript(INDEXES)
