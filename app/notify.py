"""Staff notifications: the bell + Notifications page in the store portal (new orders, stock alerts, status)."""
from . import db, events

LOW_STOCK = 10   # matches the portal's "Low Stock" badge


def add(type_: str, title: str, message: str, order_id: int | None = None,
        product_id: int | None = None, customer_id: int | None = None) -> dict:
    cur = db.run("INSERT INTO notifications(shop_id, type, title, message, order_id, product_id, customer_id, is_read, "
                 "created_at) VALUES(?,?,?,?,?,?,?,0,?)",
                 (db.shop_id(), type_, title, message, order_id, product_id, customer_id, db.now()))
    row = db.one("SELECT * FROM notifications WHERE id=?", (cur.lastrowid,))
    events.publish("notification", row)
    return row


def stock_changed(product: dict, old_stock: int):
    """Raise low/out-of-stock alerts when a product crosses a threshold."""
    new = product["stock"]
    label = f"{product['name']} ({product['variant']})" if product.get("variant") else product["name"]
    if new <= 0 < old_stock:
        add("out_of_stock", f"❌ Out of stock: {label}",
            "The WhatsApp bot has stopped offering it until you restock.", product_id=product["id"])
    elif new <= LOW_STOCK < old_stock:
        add("low_stock", f"⚠️ Low stock: {label}", f"Only {new} left in inventory.", product_id=product["id"])
