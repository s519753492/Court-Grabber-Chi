"""Web Push notifications (works on Android, and on iPhone once the app is on the home screen)."""

import asyncio
import base64
import json
import logging

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid01
from pywebpush import WebPushException, webpush

from . import config, db

log = logging.getLogger("courtapp.push")
_public_key = ""


def init() -> None:
    global _public_key
    path = config.VAPID_KEY_PATH
    if not path.exists():
        v = Vapid01()
        v.generate_keys()
        path.write_bytes(v.private_pem())
    v = Vapid01.from_file(str(path))
    raw = v.public_key.public_bytes(serialization.Encoding.X962,
                                    serialization.PublicFormat.UncompressedPoint)
    _public_key = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def public_key() -> str:
    return _public_key


def _send_sync(user_id: int, title: str, body: str, url: str) -> None:
    payload = json.dumps({"title": title, "body": body, "url": url})
    for sub in db.q("SELECT * FROM push_subs WHERE user_id = ?", (user_id,)):
        info = {"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}}
        try:
            webpush(info, payload, vapid_private_key=str(config.VAPID_KEY_PATH),
                    vapid_claims={"sub": config.VAPID_CONTACT}, ttl=600)
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):   # phone unsubscribed / app removed
                db.x("DELETE FROM push_subs WHERE id = ?", (sub["id"],))
            else:
                log.warning("push failed: %s", status)
        except Exception as e:
            log.warning("push failed: %s", e.__class__.__name__)


async def notify(user_id: int, title: str, body: str, url: str = "/") -> None:
    await asyncio.to_thread(_send_sync, user_id, title, body, url)
