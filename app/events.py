"""Tiny in-process pub/sub that feeds each shop portal's Server-Sent Events stream (per shop)."""
import asyncio
import json

from . import db

_subscribers: dict[asyncio.Queue, int] = {}   # queue → shop id


def subscribe(shop_id: int) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _subscribers[q] = shop_id
    return q


def unsubscribe(q: asyncio.Queue):
    _subscribers.pop(q, None)


def publish(event: str, data: dict | None = None, shop_id: int | None = None):
    """Sends to the portals of the current shop (or `shop_id`) only."""
    target = shop_id if shop_id is not None else db.current_shop_id()
    if target is None:
        return
    payload = json.dumps({"event": event, "data": data or {}}, default=str)
    for q, sid in list(_subscribers.items()):
        if sid != target:
            continue
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass
