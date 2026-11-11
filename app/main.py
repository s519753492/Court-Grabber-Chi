from __future__ import annotations

import datetime as dt
import json
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import bot, config, db, push, security
from .scheduler import runner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

COOKIE = "sid"


@asynccontextmanager
async def lifespan(app: FastAPI):
    security.init()
    db.init(config.DB_PATH)
    push.init()
    await runner.start()
    yield
    await runner.stop()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


# ------------------------------------------------------------------ auth helpers

def current_user(request: Request) -> dict:
    user = security.session_user(request.cookies.get(COOKIE))
    if not user:
        raise HTTPException(401, "Please sign in.")
    return user


def start_session(request: Request, response: Response, user_id: int) -> None:
    response.set_cookie(COOKIE, security.new_session(user_id), max_age=security.SESSION_DAYS * 86400,
                        httponly=True, samesite="lax", secure=request.url.scheme == "https")


def me_payload(user: dict) -> dict:
    return {"name": user["name"], "email": user["email"],
            "cpd_email": user["cpd_email"] or "",
            "has_park_login": bool(user["cpd_email"] and user["cpd_password_enc"]),
            "vapid_key": push.public_key()}


# ------------------------------------------------------------------ accounts

class RegisterIn(BaseModel):
    invite: str
    name: str = Field(min_length=1, max_length=60)
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=8, max_length=200)


class LoginIn(BaseModel):
    email: str
    password: str


@app.post("/api/register")
def register(body: RegisterIn, request: Request, response: Response):
    if not secrets.compare_digest(body.invite.strip(), config.INVITE_CODE):
        raise HTTPException(403, "That invite code isn't right.")
    email = body.email.strip().lower()
    if db.one("SELECT id FROM users WHERE email = ?", (email,)):
        raise HTTPException(409, "An account with that email already exists. Sign in instead.")
    uid = db.x("INSERT INTO users (name, email, pw_hash, created_at) VALUES (?, ?, ?, ?)",
               (body.name.strip(), email, security.hash_password(body.password), time.time()))
    start_session(request, response, uid)
    return me_payload(db.one("SELECT * FROM users WHERE id = ?", (uid,)))


@app.post("/api/login")
def login(body: LoginIn, request: Request, response: Response):
    key = body.email.strip().lower()
    if security.too_many_attempts(key):
        raise HTTPException(429, "Too many tries. Wait 15 minutes and try again.")
    user = db.one("SELECT * FROM users WHERE email = ?", (key,))
    if not user or not security.check_password(body.password, user["pw_hash"]):
        security.record_failure(key)
        raise HTTPException(401, "Wrong email or password.")
    start_session(request, response, user["id"])
    return me_payload(user)


@app.post("/api/logout")
def logout(request: Request, response: Response):
    security.end_session(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(current_user)):
    return me_payload(user)


class ParkLoginIn(BaseModel):
    cpd_email: str = Field(min_length=3, max_length=200)
    cpd_password: str = Field(min_length=1, max_length=200)


@app.put("/api/me/park-login")
def save_park_login(body: ParkLoginIn, user: dict = Depends(current_user)):
    db.x("UPDATE users SET cpd_email = ?, cpd_password_enc = ? WHERE id = ?",
         (body.cpd_email.strip(), security.encrypt(body.cpd_password), user["id"]))
    return {"ok": True}


_testing: set[int] = set()


@app.post("/api/me/park-login/test")
async def test_park_login(user: dict = Depends(current_user)):
    if not user["cpd_email"] or not user["cpd_password_enc"]:
        raise HTTPException(400, "Save your Park District login first.")
    if user["id"] in _testing:
        raise HTTPException(429, "Already testing; give it a few seconds.")
    _testing.add(user["id"])
    try:
        ok, msg = await runner.test_sign_in(user["cpd_email"],
                                            security.decrypt(user["cpd_password_enc"]))
    finally:
        _testing.discard(user["id"])
    return {"ok": ok, "message": msg}


@app.delete("/api/me")
def delete_account(request: Request, response: Response, user: dict = Depends(current_user)):
    if db.one(f"SELECT id FROM bookings WHERE user_id = ? AND status IN "
              f"('running','awaiting_payment','paying')", (user["id"],)):
        raise HTTPException(409, "A booking is in progress. Try again after it finishes.")
    db.x("DELETE FROM users WHERE id = ?", (user["id"],))
    response.delete_cookie(COOKIE)
    return {"ok": True}


# ------------------------------------------------------------------ bookings

class BookingIn(BaseModel):
    play_date: dt.date
    start: int = Field(ge=bot.FIRST_HOUR, le=bot.LAST_HOUR)
    fallbacks: list[int] = Field(default_factory=list, max_length=6)
    hours: int = Field(ge=1, le=4)
    courts: list[int] = Field(default_factory=list, max_length=6)
    any_court: bool = True
    event_name: str = Field(default="Tennis", min_length=1, max_length=40)
    is_test: bool = False


def booking_out(b: dict) -> dict:
    out = {k: b[k] for k in ("id", "play_date", "hours", "event_name", "status", "detail",
                             "court", "start_hour", "total", "hold_expires_at")}
    out.update(starts=json.loads(b["starts"]), courts=json.loads(b["courts"]),
               is_test=bool(b["is_test"]),
               opens_at=bot.opens_at(dt.date.fromisoformat(b["play_date"])).isoformat(),
               has_shot=b["status"] in ("failed", "no_court") and bot.latest_shot(b["id"]) is not None)
    return out


@app.get("/api/bookings")
def list_bookings(user: dict = Depends(current_user)):
    since = (bot.now().date() - dt.timedelta(days=14)).isoformat()
    rows = db.q("SELECT * FROM bookings WHERE user_id = ? AND status != 'cancelled' "
                "AND play_date >= ? ORDER BY play_date, id", (user["id"], since))
    return [booking_out(b) for b in rows]


@app.post("/api/bookings")
def add_booking(body: BookingIn, user: dict = Depends(current_user)):
    today = bot.now().date()
    if body.play_date < today:
        raise HTTPException(400, "Pick today or a later date.")
    if body.is_test and body.play_date > today + dt.timedelta(days=bot.DAYS_AHEAD):
        raise HTTPException(400, "A test has to use a date that's already open (within 6 days).")
    starts = [body.start] + [h for h in body.fallbacks if h != body.start]
    for h in starts:
        if not (bot.FIRST_HOUR <= h <= bot.LAST_HOUR) or h + body.hours > bot.LAST_HOUR + 1:
            raise HTTPException(400, f"{bot.hour_label(h)} for {body.hours}h runs past closing (11 PM).")
    courts = [c for c in dict.fromkeys(body.courts) if c in bot.TENNIS_COURTS]
    if body.any_court:
        courts += [c for c in bot.TENNIS_COURTS if c not in courts]
    if not courts:
        raise HTTPException(400, "Pick at least one court.")
    active = db.one("SELECT COUNT(*) AS n FROM bookings WHERE user_id = ? AND status IN "
                    "('scheduled','running','awaiting_payment','paying')", (user["id"],))["n"]
    if active >= config.MAX_ACTIVE_BOOKINGS_PER_USER:
        raise HTTPException(400, "You already have 10 bookings waiting. Remove one first.")
    if not body.is_test and db.one(
            "SELECT id FROM bookings WHERE user_id = ? AND play_date = ? AND is_test = 0 AND status IN "
            "('scheduled','running','awaiting_payment','paying')", (user["id"], body.play_date.isoformat())):
        raise HTTPException(409, "You already have a request for that date.")
    t = time.time()
    bid = db.x("INSERT INTO bookings (user_id, play_date, starts, hours, courts, event_name, is_test, "
               "status, detail, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (user["id"], body.play_date.isoformat(), json.dumps(starts), body.hours,
                json.dumps(courts), body.event_name.strip(), int(body.is_test), "scheduled",
                "", t, t))
    return booking_out(db.one("SELECT * FROM bookings WHERE id = ?", (bid,)))


def own_booking(booking_id: int, user: dict) -> dict:
    b = db.one("SELECT * FROM bookings WHERE id = ? AND user_id = ?", (booking_id, user["id"]))
    if not b:
        raise HTTPException(404, "Booking not found.")
    return b


@app.delete("/api/bookings/{booking_id}")
def cancel_booking(booking_id: int, user: dict = Depends(current_user)):
    b = own_booking(booking_id, user)
    if b["status"] == "awaiting_payment":
        runner.release(booking_id)
    elif booking_id in runner.jobs or b["status"] in ("running", "paying"):
        raise HTTPException(409, "It's booking right now; wait a moment.")
    else:  # waiting or finished: stop / hide it
        db.set_booking(booking_id, status="cancelled")
    return {"ok": True}


@app.get("/api/bookings/{booking_id}/screenshot")
def booking_screenshot(booking_id: int, user: dict = Depends(current_user)):
    own_booking(booking_id, user)
    path = bot.latest_shot(booking_id)
    if not path:
        raise HTTPException(404, "No screenshot for this booking.")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})


class PayIn(BaseModel):
    cvv: str


@app.post("/api/bookings/{booking_id}/pay")
def pay_booking(booking_id: int, body: PayIn, user: dict = Depends(current_user)):
    b = own_booking(booking_id, user)
    if not re.fullmatch(r"\d{3,4}", body.cvv or ""):
        raise HTTPException(400, "The security code is the 3 or 4 digits on your card.")
    if b["status"] != "awaiting_payment" or not runner.submit_cvv(booking_id, body.cvv):
        raise HTTPException(409, "This court isn't waiting for payment anymore.")
    return {"ok": True}


# ------------------------------------------------------------------ push

class PushSubIn(BaseModel):
    endpoint: str = Field(max_length=1000)
    keys: dict[str, str]


@app.post("/api/push/subscribe")
def push_subscribe(body: PushSubIn, user: dict = Depends(current_user)):
    if not body.endpoint.startswith("https://") or "p256dh" not in body.keys or "auth" not in body.keys:
        raise HTTPException(400, "Bad subscription.")
    db.x("INSERT INTO push_subs (user_id, endpoint, p256dh, auth) VALUES (?,?,?,?) "
         "ON CONFLICT(endpoint) DO UPDATE SET user_id = excluded.user_id, "
         "p256dh = excluded.p256dh, auth = excluded.auth",
         (user["id"], body.endpoint, body.keys["p256dh"], body.keys["auth"]))
    return {"ok": True}


@app.post("/api/push/test")
async def push_test(user: dict = Depends(current_user)):
    await push.notify(user["id"], "Notifications work", "You'll get an alert when a court is held.")
    return {"ok": True}


# ------------------------------------------------------------------ app shell

app.mount("/", StaticFiles(directory=Path(__file__).with_name("static"), html=True), name="static")
