import os
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Chicago")

DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "courtapp.db"
SHOT_DIR = DATA_DIR / "screenshots"       # failure screenshots, pruned after 3 days
VAPID_KEY_PATH = DATA_DIR / "vapid_private.pem"

# Required: key that encrypts everyone's Park District passwords (Fernet key).
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")
# Required: code friends must enter to create an account.
INVITE_CODE = os.getenv("INVITE_CODE", "")
# Contact put in push-notification requests (required by the push services).
VAPID_CONTACT = os.getenv("VAPID_CONTACT", "mailto:courtapp@example.invalid")

MAX_ACTIVE_BOOKINGS_PER_USER = 10
HOLD_MINUTES = 9          # site drops the cart at 10 min; leave a margin
SIGN_IN_LEAD_SECONDS = 240
