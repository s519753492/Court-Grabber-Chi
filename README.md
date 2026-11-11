# Court Grabber

A phone app that books **McFetridge indoor tennis courts** (Chicago Park District) the moment they open, **7:00 AM Chicago time, 6 days ahead**, for you and a few friends.

## How it works

1. **Ask for a court.** In the app you choose a day, a start time, backup times, how long you want to play and your favorite courts.
2. **The server books it.** At 7:00 AM, six days before that day, the server signs in to the Park District site as you and grabs the first free court that matches.
3. **You get an alert.** It says something like "Court held – pay now".
4. **You pay in the app.** Open it and type the **3–4 digit security code (CVV)** of the card saved on your Park District account. The server enters it into the site's payment form and pays. The CVV is **used once and never stored**.
5. **Miss the window and it's released.** If nobody pays within 9 minutes, the court goes back for someone else.

It's an installable web app. Friends open a link and tap **Add to Home Screen**, so no App Store is involved.

### What is stored and where

| What | How it's kept |
|---|---|
| App password | Hashed (bcrypt). Can't be read back. |
| Park District password | Encrypted (Fernet/AES) with a key only the server has. The bot needs it to sign in for you. |
| Card number | **Never seen.** It stays saved on your Park District account. |
| Card security code (CVV) | **Never stored.** It exists only in memory for the few seconds it takes to pay. |
| Your court requests | Plain database rows on the server. |

The person running the server (you) is responsible for keeping it safe. Only invite people you trust, and keep the invite code private.

---

## Part 1: put the server online (the owner does this once)

This guide uses **Fly.io**, which has a Chicago data center. Expect about **$6/month**: a 1 GB machine that stays on, plus a small disk. Fly asks for a credit card.

### Easiest: deploy from GitHub (nothing to install)
GitHub runs the Fly tool for you, using `.github/workflows/deploy.yml`.

1. **Get a Fly token.** On fly.io, open **Dashboard → Tokens** and create an **organization** token. Copy it.
2. **Create a private repository** on GitHub. Leave it empty: no README.
3. **Add the token to the repository.** Go to **Settings → Secrets and variables → Actions → New repository secret**:
   - Name: `FLY_API_TOKEN`
   - Value: the token you copied.
4. **Upload the code.** In the repository, click **uploading an existing file** (or **Add file → Upload files**). Drag in **everything inside** this `courtapp` folder, including the `.github` folder, then click **Commit changes**.
5. **Watch it deploy.** Open the **Actions** tab. The "Deploy to Fly" run takes about 5–10 minutes.

The first run creates the app, its 1 GB disk in Chicago and the secrets. It generates a random `ENCRYPTION_KEY`, which is stored only in Fly. The invite code is set in `deploy.yml`; you can override it with a repository secret named `INVITE_CODE`.

To update the app later, upload changed files to the repository (or edit them on GitHub). Every commit to `main` deploys again.

### Alternative: deploy from your own computer

#### 1. Install the Fly tool
In PowerShell:
```powershell
iwr https://fly.io/install.ps1 -useb | iex
```
Some antivirus programs block `flyctl.exe`. If that happens, use the GitHub method above.

Then close and reopen PowerShell and sign up (or sign in):
```powershell
fly auth signup
```

#### 2. Create the app
Open PowerShell in this `courtapp` folder.
1. Make sure `app` in `fly.toml` is a name nobody else uses on Fly. It's currently `court-grabber-chi`.
2. Create the app and its disk:
```powershell
fly apps create court-grabber-yourname
```
```powershell
fly volumes create courtdata --region ord --size 1
```

#### 3. Create your secrets
Make an encryption key. It protects everyone's Park District passwords.
```powershell
$b = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); [Convert]::ToBase64String($b).Replace('+','-').Replace('/','_')
```
Copy the line it prints, then set the secrets. Use your own invite code, and an email where push services can reach you if something's wrong:
```powershell
fly secrets set ENCRYPTION_KEY="PASTE-KEY-HERE" INVITE_CODE="pick-a-secret-phrase" VAPID_CONTACT="mailto:you@example.com"
```
Also save the encryption key in your password manager. If it's lost, everyone has to re-enter their Park District password.

#### 4. Deploy
```powershell
fly deploy
```
The first deploy takes a few minutes. Your app's address is then `https://court-grabber-yourname.fly.dev`.

To update the app later, run `fly deploy` again. To see what the server is doing, run `fly logs`.

---

## Part 2: set up on your phone (everyone does this)

1. **Open the link** you were sent:
   - iPhone: in **Safari**.
   - Android: in **Chrome**.
2. **Add it to your home screen:**
   - iPhone: **Share → Add to Home Screen**.
   - Android: **⋮ → Install app** (or **Add to Home screen**).

   Then open it from the home-screen icon.
3. **Create an account** with your name, an email, a password for this app, and the **invite code**.
4. **Add your Park District login** in **Settings**, then tap **Test sign-in**.
5. **Make sure a card is saved** on your Park District account. If it isn't, book once on the Park District website and choose to save the card.
6. **Tap "Turn on notifications"** and allow them, then tap **Send a test alert**.

   On iPhone this only works from the home-screen icon, not from inside Safari.
7. **Optional: tap "Run test" under "Try it safely".** It books a real open slot up to checkout and **stops without paying**, which proves everything works.

That's it. On the **Courts** tab, tap **+ Add** whenever you want to play.

---

## Good to know

- **Be ready at 7:00 AM.** You have about 9 minutes after the alert to type your code. If you miss it, the court is released.
- **Dates that are already open** (within 6 days) are tried immediately instead of waiting for 7:00 AM.
- **Friends compete for the same courts.** If two of you want the same court and time, whoever the server tries first gets it. Pick different favorite courts.
- **CAPTCHA:** the Park District site uses Google reCAPTCHA. If it blocks the server, the booking shows **Problem** and you get an alert. The app does not try to get around CAPTCHAs; book by hand that day.
- **Site changes:** if the Park District changes its booking pages, bookings may fail until `app/bot.py` is updated. Failure screenshots are kept for 3 days in `/data/screenshots` on the server.
- **Rules and prices:** the 6-day / 7:00 AM rule is the residents' rule as shown by the site. Courts were $30/hour plus a $1.37 fee in Nov 2026.
- **Terms of use:** check the Park District's terms of use. Each person is responsible for bookings on their own account.

## Run it on your own computer (for development)

You need Python 3.11+.
```bash
pip install -r requirements.txt
```
```bash
python -m playwright install chromium
```
Set `ENCRYPTION_KEY` and `INVITE_CODE` as environment variables, then:
```bash
uvicorn app.main:app --reload --port 8080
```
Open http://localhost:8080. Push notifications need HTTPS, so they only work on the deployed site, or on localhost in desktop Chrome.
