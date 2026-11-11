"""Drives the Chicago Park District (ActiveNet) site in a real browser.

Every selector below was checked against the live McFetridge quick-reservation
pages in Nov 2026. If the site is redesigned, this is the file to update.
"""

from __future__ import annotations

import datetime as dt
import re
import time

from playwright.async_api import Page, TimeoutError as PWTimeout

from .config import SHOT_DIR, TZ

BASE = "https://anc.apm.activecommunities.com/chicagoparkdistrict"
QUICK_URL = f"{BASE}/reservation/landing/quick?groupId=2"
SIGNIN_URL = f"{BASE}/signin"
CART_URL = f"{BASE}/newcart"

OPEN_HOUR = 7       # residents can book at 7:00 AM ...
DAYS_AHEAD = 6      # ... 6 days before the play date
TENNIS_COURTS = [1, 2, 3, 4, 5, 6]
FIRST_HOUR, LAST_HOUR = 6, 22   # grid runs 6 AM - 11 PM


class BotError(Exception):
    """A problem worth showing to the user as-is."""


def now() -> dt.datetime:
    return dt.datetime.now(TZ)


def opens_at(play_date: dt.date) -> dt.datetime:
    d = play_date - dt.timedelta(days=DAYS_AHEAD)
    return dt.datetime(d.year, d.month, d.day, OPEN_HOUR, tzinfo=TZ)


def hour_label(h: int) -> str:
    return f"{h % 12 or 12}:00 {'AM' if h < 12 else 'PM'}"


def slot_prefix(court: int, hour: int) -> str:
    # The site labels each grid cell like
    # "McFetridge Tennis Ct03 6:00 PM - 7:00 PM Available"
    return f"McFetridge Tennis Ct{court:02d} {hour_label(hour)} - {hour_label(hour + 1)}"


def latest_shot(booking_id: int):
    """Newest failure screenshot saved for a booking, or None."""
    files = sorted(SHOT_DIR.glob(f"*-error-{booking_id}.png")) if SHOT_DIR.exists() else []
    return files[-1] if files else None


async def shot(page: Page, tag: str) -> None:
    try:
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - 3 * 86400
        for old in SHOT_DIR.glob("*.png"):
            if old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
        await page.screenshot(path=str(SHOT_DIR / f"{now():%Y%m%d-%H%M%S}-{tag}.png"),
                              full_page=True)
    except Exception:
        pass


async def visible_error(page: Page) -> str:
    try:
        texts = await page.locator('[role="alert"], .error, [class*="error-message"], '
                                   '[class*="alert-danger"]').all_inner_texts()
        msg = " ".join(t.strip() for t in texts if t.strip())
        return msg[:200]
    except Exception:
        return ""


# ------------------------------------------------------------------ sign in

async def sign_in(page: Page, email: str, password: str) -> None:
    await page.goto(SIGNIN_URL, wait_until="domcontentloaded")
    await page.get_by_placeholder("Enter your Email address").fill(email)
    await page.locator("input[type=password]").fill(password)
    await page.get_by_role("button", name=re.compile(r"^\s*sign in\s*$", re.I)).first.click()
    try:
        await page.wait_for_url(lambda u: "/signin" not in u, timeout=30_000)
    except PWTimeout:
        await shot(page, "signin")
        err = await visible_error(page)
        raise BotError("Park District sign-in failed"
                       + (f": {err}" if err else ". Check the email and password in Settings, "
                          "or the site may have asked for a CAPTCHA check."))
    status = await page.evaluate(
        "async b => (await (await fetch(b + '/rest/common/logincheck?locale=en-US')).json())"
        ".headers.response_message", BASE)
    if status != "Successful":
        raise BotError(f"Park District sign-in check failed: {status}")


# ------------------------------------------------------------------ court grid

DATE_INPUT = 'input[role="combobox"][aria-label^="Date picker"]'
GRID_CELL = '[role="gridcell"][aria-label]'


async def open_quick(page: Page) -> None:
    """Open the quick-reservation page and wait until its date picker is usable."""
    await page.goto(QUICK_URL, wait_until="domcontentloaded", timeout=45_000)
    await page.locator(DATE_INPUT).first.wait_for(state="visible", timeout=45_000)


def _date_text(d: dt.date) -> str:
    return f"{d:%a, %b} {d.day}, {d.year}"      # how the input shows it: "Tue, Nov 10, 2026"


async def select_date(page: Page, d: dt.date) -> None:
    box = page.locator(DATE_INPUT).first
    if (await box.input_value()).strip() == _date_text(d) and await page.locator(GRID_CELL).count():
        return      # already showing that day; clicking it again would load nothing
    await box.click()
    # Wait for the calendar pop-up before deciding whether to change month;
    # checking too early made the bot skip ahead to the next month.
    await page.locator(".an-calendar-day").first.wait_for(state="visible", timeout=15_000)
    mid_month = page.locator(f'.an-calendar-day[aria-label="{d:%b} 15, {d.year}"]')
    for _ in range(3):
        if await mid_month.count():
            break
        await page.locator('[aria-label="Switch calendar to next month right arrow"]').click()
        await page.wait_for_timeout(300)
    day = page.locator(f'.an-calendar-day[aria-label="{d:%b} {d.day}, {d.year}"]').first
    if not await day.count():
        await shot(page, "no-day")
        raise BotError(f"Couldn't find {d:%a %b} {d.day} in the Park District date picker.")
    try:
        async with page.expect_response(lambda r: "quickreservation/availability" in r.url,
                                        timeout=20_000):
            await day.click()
    except PWTimeout:
        pass        # checked below
    # Never read a grid unless it is for the date we asked for.
    try:
        await page.wait_for_function(
            "([sel, want]) => { const e = document.querySelector(sel); return e && e.value.trim() === want; }",
            arg=[DATE_INPUT, _date_text(d)], timeout=10_000)
        await page.locator(GRID_CELL).first.wait_for(state="attached", timeout=20_000)
    except PWTimeout:
        await shot(page, "no-grid")
        raise BotError(f"The court grid for {d:%a %b} {d.day} didn't load on the Park District site.")
    await page.wait_for_timeout(600)


async def refresh_grid(page: Page, d: dt.date) -> None:
    other = d - dt.timedelta(days=1)
    if other < now().date():
        other = d + dt.timedelta(days=1)
    await select_date(page, other)
    await select_date(page, d)


async def available_cells(page: Page) -> set[str]:
    labels = await page.eval_on_selector_all(
        GRID_CELL, "els => els.map(e => e.getAttribute('aria-label'))")
    return {lab.rsplit(" ", 1)[0] for lab in labels
            if lab and lab.rsplit(" ", 1)[-1] == "Available"}


async def shown_date(page: Page) -> str:
    try:
        return (await page.locator(DATE_INPUT).first.input_value()).strip() or "?"
    except Exception:
        return "?"


def free_summary(starts: list[int], cells: set[str]) -> str:
    """e.g. '6:00 AM free on courts 3, 4' - for explaining a 'no court' result."""
    parts = []
    for h in starts:
        free = [c for c in TENNIS_COURTS if slot_prefix(c, h) in cells]
        parts.append(f"{hour_label(h)} free on courts {', '.join(map(str, free))}" if free
                     else f"{hour_label(h)} free on no courts")
    tennis = sum(1 for c in cells if c.startswith("McFetridge Tennis"))
    return "; ".join(parts) + f"; {tennis} free tennis slots that day"


def candidates(starts: list[int], hours: int, courts: list[int], cells: set[str]):
    for start in starts:
        for court in courts:
            if all(slot_prefix(court, start + i) in cells for i in range(hours)):
                yield start, court


async def clear_selection(page: Page) -> None:
    try:
        for name in ("Cancel", "Close", "OK"):
            btn = page.get_by_role("button", name=name, exact=True)
            if await btn.count() and await btn.first.is_visible():
                await btn.first.click()
        clear = page.get_by_role("button", name="Clear all bookings")
        if await clear.count() and await clear.first.is_visible():
            await clear.first.click()
    except Exception:
        pass


async def try_reserve(page: Page, event_name: str, start: int, hours: int, court: int) -> bool:
    """Select the slot(s) and go to checkout. True when the court is in the cart."""
    await page.get_by_role("textbox", name="Event name").fill(event_name)
    for i in range(hours):
        await page.locator(
            f'[role="gridcell"][aria-label^="{slot_prefix(court, start + i)} "]').first.click()
    # Accessible name is e.g. "1 Resource(s), 1 Booking(s) selected Confirm bookings"
    await page.get_by_role("button", name="Confirm bookings").click()

    agree = page.get_by_role("checkbox", name="I have read and agree", exact=False)
    # Accessible name is e.g. "Total $30.00 Reserve"
    reserve = page.get_by_role("button", name=re.compile(r"Reserve\s*$"))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if await agree.count() and await agree.first.is_visible():
            if not await agree.first.is_checked():
                await agree.first.check()
            await page.get_by_role("button", name="Save", exact=True).click()
            await page.wait_for_timeout(500)
        if await reserve.count() and await reserve.first.is_visible():
            break
        await page.wait_for_timeout(200)
    else:
        await shot(page, f"no-reserve-ct{court}")
        await clear_selection(page)
        return False

    await reserve.first.click()
    try:
        await page.wait_for_url("**/checkout**", timeout=30_000)
    except PWTimeout:
        await shot(page, f"no-checkout-ct{court}")
        await clear_selection(page)
        return False
    return True


async def checkout_total(page: Page) -> str:
    try:
        await page.get_by_text("Due Now").first.wait_for(timeout=15_000)
        m = re.search(r"Due Now\s*\$\s*([\d,]+\.\d{2})", await page.inner_text("body"))
        return f"${m.group(1)}" if m else ""
    except Exception:
        return ""


# ------------------------------------------------------------------ pay / release

async def pay(page: Page, cvv: str) -> tuple[bool, str]:
    """Type the CVV into the site's secure payment frame and press Pay."""
    frame = page.frame_locator('iframe[title="primaryPCIPaymentIframe"]')
    field = frame.locator('input[autocomplete="cc-csc"], input[aria-label*="CVV" i], '
                          'input[name*="cvv" i], input[id*="cvv" i], '
                          'input[placeholder*="CVV" i]').first
    try:
        await field.wait_for(timeout=20_000)
    except PWTimeout:
        await shot(page, "no-cvv-field")
        return False, "Couldn't find the security-code box on the payment page."
    await field.fill("")
    await field.fill(cvv)
    await page.get_by_role("button", name="Pay", exact=True).click()
    try:
        await page.wait_for_function("() => !location.pathname.endsWith('/checkout')",
                                     timeout=60_000)
    except PWTimeout:
        await shot(page, "pay-stuck")
        err = await visible_error(page)
        return False, (err or "The site didn't accept the payment. "
                       "Check the security code and try again.")
    await page.wait_for_timeout(2000)
    text = (await page.inner_text("body")).lower()
    if any(w in text for w in ("confirmation", "receipt", "thank you", "successful")):
        return True, "Booked and paid."
    await shot(page, "after-pay")
    return True, "Payment went through. Check your email for the receipt."


async def empty_cart(page: Page) -> None:
    """Remove everything this session put in the cart (frees the court right away)."""
    try:
        await page.goto(CART_URL, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(4000)
        for _ in range(5):
            trash = page.locator('a[aria-label^="delete transaction"]')
            if not await trash.count():
                return
            await trash.first.click()
            # Accessible name: "Are you sure you want to remove ... cart? Yes"
            await page.get_by_role("button", name=re.compile(r"Yes\s*$")).click()
            await page.wait_for_timeout(8000)
    except Exception:
        pass  # the site drops held items after 10 minutes anyway
