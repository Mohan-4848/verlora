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
    "products": {"description": "TEXT", "deleted": "INTEGER DEFAULT 0", "shop_id": "INTEGER"},
    "orders": {"shop_id": "INTEGER"},
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
    def fmt(hhmm: str) -> str:
        h, m = (int(x) for x in hhmm.split(":")[:2])
        return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"
    try:
        return f"{fmt(opening)} – {fmt(closing)}"
    except (ValueError, AttributeError):
        return ""


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
    c.executescript(INDEXES)
