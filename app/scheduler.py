"""Starts each booking at the right moment and holds the court while the user pays.

The CVV only ever exists in memory: it is passed from the /pay request to the
running job through an asyncio queue, typed into the site's payment frame,
and dropped. It is never written to the database or logs.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time

from playwright.async_api import Browser, TimeoutError as PWTimeout, async_playwright

from . import bot, db, push, security
from .config import HOLD_MINUTES, SIGN_IN_LEAD_SECONDS

log = logging.getLogger("courtapp.scheduler")

ACTIVE = ("running", "awaiting_payment", "paying")
OPEN_POLL_SECONDS = 120


class Job:
    def __init__(self, booking_id: int):
        self.booking_id = booking_id
        self.cvv_queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.task: asyncio.Task | None = None


class Runner:
    def __init__(self) -> None:
        self._pw = None
        self._browser: Browser | None = None
        self._loop_task: asyncio.Task | None = None
        self.jobs: dict[int, Job] = {}
        self._browser_lock = asyncio.Lock()

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        # Anything that was mid-flight when the server stopped can't be resumed.
        for b in db.q(f"SELECT id FROM bookings WHERE status IN {ACTIVE}"):
            db.set_booking(b["id"], status="failed", detail="The server restarted during this booking.")
        self._pw = await async_playwright().start()
        self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()
        for job in self.jobs.values():
            if job.task:
                job.task.cancel()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    async def browser(self) -> Browser:
        async with self._browser_lock:
            if not self._browser or not self._browser.is_connected():
                self._browser = await self._pw.chromium.launch(headless=True)
            return self._browser

    async def new_page(self):
        ctx = await (await self.browser()).new_context(
            viewport={"width": 1500, "height": 1000}, locale="en-US",
            timezone_id="America/Chicago")
        page = await ctx.new_page()
        page.set_default_timeout(20_000)
        return ctx, page

    # ------------------------------------------------------------ scheduling

    async def _loop(self) -> None:
        while True:
            try:
                self._start_due()
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(10)

    def _start_due(self) -> None:
        now = bot.now()
        for b in db.q("SELECT * FROM bookings WHERE status = 'scheduled'"):
            if b["id"] in self.jobs:
                continue
            play_date = dt.date.fromisoformat(b["play_date"])
            if play_date < now.date():
                db.set_booking(b["id"], status="failed", detail="That date has passed.")
                continue
            start_at = bot.opens_at(play_date) - dt.timedelta(seconds=SIGN_IN_LEAD_SECONDS)
            if b["is_test"] or now >= start_at:
                job = Job(b["id"])
                self.jobs[b["id"]] = job
                job.task = asyncio.create_task(self._run(job))

    # ------------------------------------------------------------ user actions

    def submit_cvv(self, booking_id: int, cvv: str) -> bool:
        job = self.jobs.get(booking_id)
        if not job:
            return False
        job.cvv_queue.put_nowait(cvv)
        return True

    def release(self, booking_id: int) -> bool:
        job = self.jobs.get(booking_id)
        if not job:
            return False
        job.cvv_queue.put_nowait(None)
        return True

    async def test_sign_in(self, email: str, password: str) -> tuple[bool, str]:
        ctx, page = await self.new_page()
        try:
            await bot.sign_in(page, email, password)
            return True, "Signed in to the Park District site successfully."
        except bot.BotError as e:
            return False, str(e)
        except Exception as e:
            log.exception("test sign-in failed")
            return False, f"Something went wrong ({e.__class__.__name__}). Try again in a minute."
        finally:
            await ctx.close()

    # ------------------------------------------------------------ the booking job

    async def _run(self, job: Job) -> None:
        bid = job.booking_id
        b = db.one("SELECT * FROM bookings WHERE id = ?", (bid,))
        user = db.one("SELECT * FROM users WHERE id = ?", (b["user_id"],))
        uid = user["id"]
        play_date = dt.date.fromisoformat(b["play_date"])
        starts, courts, hours = json.loads(b["starts"]), json.loads(b["courts"]), b["hours"]
        label = f"{play_date:%a %b} {play_date.day}"
        ctx = page = None
        stage = "starting"

        def status(s: str, detail: str = "", **extra) -> None:
            db.set_booking(bid, status=s, detail=detail, **extra)

        try:
            if not user["cpd_email"] or not user["cpd_password_enc"]:
                raise bot.BotError("Add your Park District login in Settings first.")
            status("running", "Signing in to the Park District site...")
            stage = "signing in"
            ctx, page = await self.new_page()
            await bot.sign_in(page, user["cpd_email"], security.decrypt(user["cpd_password_enc"]))

            stage = "opening the court page"
            await bot.open_quick(page)
            stage = "picking the date"
            await bot.select_date(page, play_date)
            open_time = bot.opens_at(play_date)
            waited = bot.now() < open_time
            if waited:
                status("running", f"Signed in. Waiting for courts to open at {open_time:%-I:%M} AM...")
                await asyncio.sleep((open_time - bot.now()).total_seconds() + 0.2)

            status("running", "Looking for a free court...")
            give_up = time.monotonic() + OPEN_POLL_SECONDS
            tried: set[tuple[int, int]] = set()
            failures: list[str] = []
            # The grid loaded above is current unless we waited for 7:00 AM since then.
            first_look = not waited
            while True:
                stage = "reading the court grid"
                if not first_look:
                    await bot.refresh_grid(page, play_date)
                first_look = False
                cells = await bot.available_cells(page)
                if not any(c.startswith("McFetridge Tennis") for c in cells) \
                        and time.monotonic() < give_up:
                    await asyncio.sleep(0.7)        # not open yet
                    continue
                options = [o for o in bot.candidates(starts, hours, courts, cells) if o not in tried]
                if not options and failures:
                    # Courts were free but reserving them failed: that's a problem, not "no court".
                    log.warning("booking %s reserve failed: %s", bid, failures)
                    raise bot.BotError("A court was free but reserving it failed. " + " | ".join(failures))
                if not options:
                    await bot.shot(page, f"error-{bid}")
                    shown = await bot.shown_date(page)
                    seen = bot.free_summary(starts, cells)
                    log.info("booking %s no court; grid date %s; %s", bid, shown, seen)
                    status("no_court", "None of your preferred courts and times were free. "
                                       f"(Checked the grid for {shown}: {seen}.)")
                    await push.notify(uid, "No court this time",
                                      f"{label}: none of your preferred courts/times were free.")
                    return
                start, court = options[0]
                tried.add((start, court))
                stage = f"reserving Court {court} at {bot.hour_label(start)}"
                ok, why = await bot.try_reserve(page, b["event_name"], start, hours, court,
                                                shot_tag=f"error-{bid}")
                if ok:
                    break
                failures.append(f"Court {court} {bot.hour_label(start)}: {why}")
                stage = "reopening the court page"
                await bot.open_quick(page)
                await bot.select_date(page, play_date)
                first_look = True

            what = f"{label}, Court {court}, {bot.hour_label(start)} for {hours}h"
            stage = "reading the checkout page"
            total = await bot.checkout_total(page)

            if b["is_test"]:
                status("test_ok", f"Test worked: reached checkout for {what} ({total or 'price n/a'}). "
                                  "Nothing was paid; the court was released.",
                       court=court, start_hour=start, total=total)
                await push.notify(uid, "Test booking worked", f"Reached checkout for {what}. Not paid.")
                await bot.empty_cart(page)
                return

            expires = time.time() + HOLD_MINUTES * 60
            status("awaiting_payment", f"Court held: {what}. Enter your card's security code to pay.",
                   court=court, start_hour=start, total=total, hold_expires_at=expires)
            await push.notify(uid, "Court held - pay now",
                              f"{what} ({total}). Open the app and enter your card code "
                              f"within {HOLD_MINUTES} minutes.", "/#pay")

            while True:
                remaining = expires - time.time()
                if remaining <= 0:
                    status("expired", "Payment wasn't completed in time, so the court was released.")
                    await push.notify(uid, "Court released", f"{what} was released: no payment in time.")
                    await bot.empty_cart(page)
                    return
                try:
                    cvv = await asyncio.wait_for(job.cvv_queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    continue
                if cvv is None:
                    status("released", "You released this court.")
                    await bot.empty_cart(page)
                    return
                status("paying", "Paying...")
                ok, msg = await bot.pay(page, cvv)
                del cvv
                if ok:
                    status("booked", f"{what}. {msg}")
                    await push.notify(uid, "Court booked!", f"{what} is booked and paid.")
                    return
                if not page.url.rstrip("/").endswith("/checkout"):
                    status("failed", f"Payment failed: {msg}")
                    await push.notify(uid, "Payment failed", f"{what}: {msg}")
                    return
                status("awaiting_payment", f"Court held: {what}. {msg}")

        except asyncio.CancelledError:
            raise
        except bot.BotError as e:
            if page and not bot.latest_shot(bid):   # keep the screenshot from the failing step
                await bot.shot(page, f"error-{bid}")
            status("failed", str(e))
            await push.notify(uid, "Booking problem", f"{label}: {e}")
        except Exception as e:
            log.exception("booking %s failed while %s", bid, stage)
            if page:
                await bot.shot(page, f"error-{bid}")
            kind = "took too long" if isinstance(e, PWTimeout) else f"hit an error ({e.__class__.__name__})"
            status("failed", f"The Park District site {kind} while {stage}.")
            await push.notify(uid, "Booking problem", f"{label}: something went wrong. Book by hand.")
        finally:
            self.jobs.pop(bid, None)
            if ctx:
                try:
                    await ctx.close()
                except Exception:
                    pass


runner = Runner()
