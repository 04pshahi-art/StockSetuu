# SHOP MANAGEMENT SYSTEM — shop management

Stock, purchases, GST invoices, serial-number warranty tracking and service jobs for one
shop, running on one Windows Server, reached over Tailscale from the counter PC and from a
phone. Not exposed to the internet.

Python + FastAPI, server-rendered Jinja2 templates, SQLite **encrypted at rest with
SQLCipher**. One login, created from the command line — there is no sign-up page.

> **What this app does not do:** it does not file GST returns. It produces the sales and
> purchase registers, rate-wise and HSN summaries that make filing GSTR-1 and claiming ITC
> a copy-out job instead of a reconstruction job. Filing stays with your accountant.

---

## Quick start (run it locally, any OS)

The rest of this README is a full production deployment guide (Windows Server, run as a
service, Tailscale, scheduled backups). If you just want to run it on your own machine to
try it out or develop on it, this is all you need:

```bash
git clone <this-repo-url>
cd <repo-folder>
python -m venv .venv

# activate the virtualenv
source .venv/bin/activate        # macOS / Linux
.venv\Scripts\activate           # Windows

pip install -r requirements.txt
cp .env.example .env             # Windows: copy .env.example .env
```

Open `.env` and set `DB_KEY` to any random passphrase (this encrypts the database —
losing it means losing access to the data, so save it somewhere safe even for local dev).

```bash
python manage.py init-db
python manage.py create-user owner --name "Your Name"
python manage.py set-password owner
python manage.py seed-shop        # fills in placeholder shop details — edit for real later at /settings
python run.py
```

Then open **http://localhost:8000** and sign in with the username/password you just set.

If `sqlcipher3-wheels` fails to install for your platform, set `ALLOW_UNENCRYPTED_DB=1` in
`.env` for local development only — never do this on a machine holding real data.

---



1. [What you need before starting](#1-what-you-need-before-starting)
2. [Install](#2-install)
3. [First run — the encryption key and the admin password](#3-first-run--the-encryption-key-and-the-admin-password)
4. [Run it as a Windows Service with NSSM](#4-run-it-as-a-windows-service-with-nssm)
5. [Reach it from anywhere with Tailscale](#5-reach-it-from-anywhere-with-tailscale)
6. [Automatic backups with schtasks](#6-automatic-backups-with-schtasks)
7. [Bringing products in from Tally, and exporting sales/purchases into it](#7-bringing-products-in-from-tally)
8. [Day-to-day command reference](#8-day-to-day-command-reference)
9. [Troubleshooting](#9-troubleshooting)
10. [What has and has not been tested](#10-what-has-and-has-not-been-tested)

---

## 1. What you need before starting

| | |
|---|---|
| **Python** | 3.10 – 3.14, 64-bit. `python --version` must work in a Command Prompt. |
| **Server** | The shop's Windows Server. It stays on; the app runs as a service. |
| **A password manager** | Or a notebook in the safe. You are about to generate a key that cannot be recovered. |
| **Tailscale account** | Free tier is enough. Used to reach the app from the counter and from a phone. |
| **USB barcode scanner** | Optional. Any HID keyboard-wedge scanner works — no driver, no configuration. |

Nothing else. No SQL Server, no IIS, no web server in front, no SSL certificate.

---

## 2. Install

Open a Command Prompt **as the account the shop will run under**, and put the project
somewhere permanent — `C:\shopapp` in these instructions. Not the Desktop, not Downloads.

```bat
cd C:\shopapp
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

`requirements.txt` pins `sqlcipher3-wheels`, which is what makes the database encrypted.
Confirm it actually installed — this is the one check worth doing before anything else:

```bat
.venv\Scripts\python -c "import sqlcipher3; print(sqlcipher3.dbapi2.connect(':memory:').execute('PRAGMA cipher_version').fetchone())"
```

You should see a version like `('4.12.0 community',)`. If you get `ModuleNotFoundError`,
the encryption is not installed and the app will refuse to start rather than write your
books to disk in the clear. Fix the install. **Do not** reach for `ALLOW_UNENCRYPTED_DB`.

> If pip tries to *compile* SQLCipher and fails asking for MSVC or OpenSSL, you have
> `sqlcipher3-binary` from somewhere, not `sqlcipher3-wheels`. They are different packages;
> only the latter ships Windows wheels. `pip uninstall sqlcipher3-binary` and re-run the
> install from `requirements.txt`.

---

## 3. First run — the encryption key and the admin password

### 3.1 Create the configuration file

```bat
copy .env.example .env
notepad .env
```

### 3.2 Generate the encryption key

This is the SQLCipher passphrase. It is the only thing between a copied `.db` file and the
shop's entire books. Generate it — do not invent one by hand:

```bat
.venv\Scripts\python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Paste the output into `.env`:

```
DB_KEY=<the long random string you just generated>
```

> ### Read this twice
>
> **There is no recovery.** Lose this key and every invoice, purchase, warranty record and
> service job is gone permanently. No support call retrieves it, because the app genuinely
> cannot read the file without it.
>
> - Write it down **now**, before you create the database. Password manager, or paper in
>   the safe.
> - Keep it **somewhere separate from the backups.** The key stored with the backups is the
>   same as no encryption at all. The backups with no key is the same as no backups at all.
> - Changing it later does **not** re-encrypt an existing database. The app will simply
>   refuse to open the file. Decide once, at the start.

Also make sure `ALLOW_UNENCRYPTED_DB` is **not** in the file, or is commented out. That
switch exists for a developer's laptop where SQLCipher cannot be installed. On this server
it would mean the app starts up perfectly happily writing your books in plain text, and
nothing on screen would tell you. Leave it out.

### 3.3 Create the database

```bat
.venv\Scripts\python manage.py init-db
```

It prints the database path and must say:

```
Storage:  Encrypted at rest via SQLCipher (sqlcipher3).
```

If it says anything else, stop and fix the install before entering real data.

### 3.4 Create the login

There is no sign-up page. This command is the only way an account comes into existence.

```bat
.venv\Scripts\python manage.py create-user owner --name "Your Name"
```

It prompts for the password twice, without echoing. Minimum 10 characters. Use a real one —
this login is the only thing protecting the app from anyone else on your Tailscale network.

### 3.5 Fill in the shop's details

```bat
.venv\Scripts\python manage.py seed-shop
```

This sets the legal name, trade name and GSTIN. It deliberately does **not** set the
address, phone, email or bank details, because those print on every invoice and must match
your GST certificate exactly. Enter them yourself at **Settings** once the app is running.

Everything on that screen is editable — nothing about the shop is hardcoded in the app.

### 3.6 Check it starts

```bat
.venv\Scripts\python run.py
```

Open <http://localhost:8000>, sign in, and confirm the banner in the console says the
storage is encrypted. Press `Ctrl+C` to stop, then move on to running it as a service.

---

## 4. Run it as a Windows Service with NSSM

The app must start when the server boots and restart if it ever crashes, without anyone
logging in. NSSM (the Non-Sucking Service Manager) does this for a plain Python process.

### 4.1 Install NSSM

Download from <https://nssm.cc/download>, take the **win64** build, and copy `nssm.exe`
into `C:\shopapp\tools\`.

### 4.2 Create the service

In a Command Prompt **as Administrator**:

```bat
C:\shopapp\tools\nssm.exe install ShopManagement "C:\shopapp\.venv\Scripts\python.exe" "C:\shopapp\run.py"
C:\shopapp\tools\nssm.exe set ShopManagement AppDirectory C:\shopapp
C:\shopapp\tools\nssm.exe set ShopManagement DisplayName "Shop Management System"
C:\shopapp\tools\nssm.exe set ShopManagement Description "Stock, GST invoicing and service jobs for Shop Management System"
C:\shopapp\tools\nssm.exe set ShopManagement Start SERVICE_AUTO_START
```

`AppDirectory` is not optional. The app reads `.env` and resolves `data\` relative to the
project folder, so a service started from `C:\Windows\System32` would look for its
configuration in the wrong place.

### 4.3 Send the logs somewhere you can read them

```bat
mkdir C:\shopapp\logs
C:\shopapp\tools\nssm.exe set ShopManagement AppStdout C:\shopapp\logs\service.log
C:\shopapp\tools\nssm.exe set ShopManagement AppStderr C:\shopapp\logs\service.log
C:\shopapp\tools\nssm.exe set ShopManagement AppRotateFiles 1
C:\shopapp\tools\nssm.exe set ShopManagement AppRotateBytes 10485760
```

That rotates at 10 MB so the log cannot quietly fill the disk.

### 4.4 Restart behaviour

```bat
C:\shopapp\tools\nssm.exe set ShopManagement AppExit Default Restart
C:\shopapp\tools\nssm.exe set ShopManagement AppRestartDelay 5000
C:\shopapp\tools\nssm.exe set ShopManagement AppThrottle 10000
```

### 4.5 Start it

```bat
C:\shopapp\tools\nssm.exe start ShopManagement
sc query ShopManagement
type C:\shopapp\logs\service.log
```

The log should end with the startup banner and `Uvicorn running on http://0.0.0.0:8000`.

### 4.6 Everyday service commands

```bat
C:\shopapp\tools\nssm.exe restart ShopManagement
C:\shopapp\tools\nssm.exe stop ShopManagement
C:\shopapp\tools\nssm.exe edit ShopManagement
```

Use `restart` after changing `.env` — the configuration is read once at startup.

> **Which account runs the service?** The default `LocalSystem` works. If you change it to
> a specific user, that user needs read/write on `C:\shopapp\data`, or the app cannot open
> its own database.

---

## 5. Reach it from anywhere with Tailscale

The app listens on `0.0.0.0:8000`. That is safe **only** because the machine is not
port-forwarded and the Windows Firewall is not opened to the internet. Tailscale is the
security boundary — it gives every device a private encrypted address that only your own
devices can reach.

**Do not** port-forward 8000 on the router. **Do not** set `COOKIE_SECURE=1` unless you
have put real HTTPS in front; it makes the login cookie undeliverable over plain HTTP and
locks you out of your own app.

### 5.1 On the server

1. Install from <https://tailscale.com/download/windows>.
2. `tailscale up` — it opens a browser to sign in. Use the same account on every device.
3. Note the address it was given:

   ```bat
   tailscale ip -4
   ```

   Something like `100.x.y.z`.

4. Stop the machine from ever losing that address, and let it run headless:

   ```bat
   tailscale set --unattended
   ```

   Without this, Tailscale can disconnect when no one is signed in — and the app becomes
   unreachable even though the service is running fine.

### 5.2 Let the app through the firewall

Tailscale traffic still hits the Windows Firewall. Allow port 8000 on the Tailscale
interface only, as Administrator:

```bat
netsh advfirewall firewall add rule name="Shop Management (Tailscale)" dir=in action=allow protocol=TCP localport=8000 remoteip=100.64.0.0/10
```

`100.64.0.0/10` is the Tailscale address range. This rule lets your own devices in and
nothing else — not the shop's Wi-Fi, not the street.

### 5.3 On the counter PC and the phone

Install Tailscale, sign in with the same account, then open:

```
http://100.x.y.z:8000
```

Add it to the home screen on the phone — it behaves like an app. The layout is built for a
phone; stock lookups and warranty checks work one-handed.

### 5.4 A nicer address (optional)

Enable MagicDNS in the Tailscale admin console and use the machine name instead:

```
http://shop-server:8000
```

---

## 6. Automatic backups with schtasks

`manage.py backup` writes a timestamped copy into `data\backups\`. **The copy is itself
encrypted with the same key** — it is not a plaintext export, so a stolen backup is as
useless as a stolen database. It keeps the most recent 30 and prunes older ones, so the
folder cannot grow without limit.

Test it by hand once:

```bat
cd C:\shopapp
.venv\Scripts\python manage.py backup
dir data\backups
```

Two flags worth knowing: `--into "E:\shop-backups"` writes somewhere other than the default
folder, and `--keep 60` changes the retention count (`--keep 0` keeps everything forever).

### 6.1 Schedule it

Create `C:\shopapp\backup.bat`:

```bat
@echo off
cd /d C:\shopapp
.venv\Scripts\python.exe manage.py backup >> logs\backup.log 2>&1
```

Register it, as Administrator. Twice a day — the shop opens around 10 and closes around 9,
so these land just after opening stock-taking and just after closing:

```bat
schtasks /create /tn "Shop Management Backup" /tr "C:\shopapp\backup.bat" /sc daily /st 13:30 /ru SYSTEM /rl HIGHEST /f
schtasks /create /tn "Shop Management Backup Night" /tr "C:\shopapp\backup.bat" /sc daily /st 21:45 /ru SYSTEM /rl HIGHEST /f
```

Verify, and force one run now to prove the schedule works rather than assuming it:

```bat
schtasks /query /tn "Shop Management Backup" /v /fo LIST
schtasks /run /tn "Shop Management Backup"
type C:\shopapp\logs\backup.log
```

### 6.2 Get a copy off the machine

A backup on the same disk as the database protects you from a mistake, not from a dead
disk, a fire or a theft. The simplest second line of defence is one more scheduled task
writing straight to an external or cloud-synced drive:

```bat
schtasks /create /tn "Shop Management Backup Offsite" /tr "C:\shopapp\.venv\Scripts\python.exe C:\shopapp\manage.py backup --into E:\shop-backups --keep 90" /sc weekly /d SUN /st 22:15 /ru SYSTEM /rl HIGHEST /f
```

**Keep the key out of that copy.** Do not put `.env` in the same place as the backups.

### 6.3 Restoring

1. Stop the service: `nssm stop ShopManagement`
2. Copy the chosen backup over `data\shop.db`
3. Start the service: `nssm start ShopManagement`
4. Check: `.venv\Scripts\python manage.py status`

The restore only works with the **same `DB_KEY`** that created it. This is the moment the
written-down key earns its keep.

---

## 7. Bringing products in from Tally

The import is for the **product/stock-item list** — SKU, name, HSN, GST rate, prices,
quantity on hand. It is a one-time-per-list job to avoid retyping the catalogue. Invoices
and purchases are entered in this app going forward; they are not imported from Tally.

### 7.1 Export from Tally

In Tally ERP 9 / Tally Prime:

1. **Gateway of Tally → Display → Inventory Books → Stock Items** (Tally Prime:
   **Display More Reports → Inventory Books → Stock Items**).
2. Press **Alt + E** → **Export**.
3. Set:
   - **Format:** `Excel (Spreadsheet)` or `CSV`
   - **Include:** stock item name, alias/part number, HSN, GST rate, quantity, rate
4. Note where it saved the file.

Tally's column names vary by version and by how the company was configured, so **this app
does not assume a schema.** It reads the real columns out of your file and asks you to
confirm the mapping. Whatever Tally called things, you will see it on screen.

If Tally produced an old `.xls`, open it in Excel and re-save as `.xlsx` or CSV. `.xlsx`
and CSV are what the app reads.

### 7.2 Import into the app

Go to **Import** in the app (`/import`). Four steps, and nothing is written until the last
one:

1. **Upload** the `.csv` or `.xlsx`. The app finds the header row and shows you the
   columns it found.
2. **Map the columns.** It pre-selects sensible guesses — a column called `Particulars`,
   `Stock Item` or `Item Name` is offered as the product name; `Alias`, `Part No` or
   `Barcode` as the SKU. Correct anything it guessed wrong. Only **SKU** and **Product
   name** are required; leave the rest unmapped if the file has no such column.
3. **Preview.** This is the important screen. It shows exactly what will be created, what
   will be updated, and what will be skipped and why — row by row, before any change.
   Check the row count and a few prices against the Tally report.
4. **Commit.** Now it writes.

**Quantity handling** — choose deliberately, because this is the setting that can silently
wreck your stock figures:

| Option | What it does |
|---|---|
| **Ignore** | Never touch stock. Safest for a catalogue refresh on a live shop. |
| **New items only** *(default)* | Set opening stock on products it creates; leave existing counts alone. |
| **Set from file** | Overwrite the on-hand count for every mapped row. This is a stock-take, not an import. |

Every quantity change is written to the stock ledger with a reason, so an import can always
be traced afterwards under the product's movement history.

### 7.3 Getting data back out

**Products → Export CSV** downloads the catalogue. The **GST sales register** and
**GST purchase register** under Reports export the columns your accountant needs for
GSTR-1 and ITC, with rate-wise and HSN summaries. Take a backup before an import so there
is always a way back.

### 7.4 Exporting sales & purchases *into* Tally

This is separate from 7.1–7.3 above (that's the one-time catalogue import). This is for
handing a financial year's sales and purchase vouchers to your CA as a file they can drop
straight into their own Tally (ERP 9 or Prime).

Go to **Reports → Tally export**, pick the date range, and download two files:

1. **Masters** — stock items and the ledgers the vouchers reference (Sales Accounts,
   Purchase Accounts, one tax ledger per GST rate in use, one ledger per registered
   customer/dealer, plus a shared walk-in ledger for anonymous retail sales).
2. **Vouchers** — the actual Sales/Purchase entries for the range.

**Import the masters file first**, then the vouchers file — in Tally: **Gateway of Tally →
Import Data**, pick the file, and confirm the company. A vouchers import will fail on any
ledger that doesn't exist yet, which is why the order matters.

**Before handing this to your CA, test it yourself first** — import both files into a
spare/test Tally company (not the real one) and check: the Dr/Cr direction on each ledger
looks right, and stock quantities move the correct way (down on a sale, up on a purchase).
The export logic has been checked for internal consistency (every voucher's debits and
credits balance to zero) but has not been round-tripped through a real Tally install by
the people who built it — treat the first import as a test, not a given.

---

## 8. Day-to-day command reference

Run from `C:\shopapp` with the venv active, or with the full path
`.venv\Scripts\python manage.py …`.

| Command | What it does |
|---|---|
| `manage.py status` | Database path and size, schema version, **whether encryption is on**, hashing backend, row counts. The first thing to run when something looks wrong. |
| `manage.py backup` | Timestamped encrypted copy into `data\backups\`, keeping the last 30. `--into <folder>` and `--keep <n>` override both. |
| `manage.py users` | List accounts, with lockout state and last sign-in. |
| `manage.py create-user <username> --name "Display Name"` | Add a login. The only way an account is created. |
| `manage.py set-password <username>` | Change a password, and sign out every device. |
| `manage.py unlock <username>` | Clear a lockout after too many wrong passwords, without waiting it out. |
| `manage.py seed-shop` | Fill in legal name, trade name and GSTIN. |
| `manage.py init-db` | Create or migrate the database. Safe to re-run; it only applies what is missing. |

Every one of these reads `.env` for `DB_KEY` exactly as the server does, so the CLI and the
app always talk to the same database with the same key.

---

## 9. Troubleshooting

**"Refusing to start" on startup.**
`DB_KEY` is empty or SQLCipher is not installed. The app stops rather than writing your
books to disk unencrypted. Run `manage.py status` to see which of the two it is. The fix is
to install `sqlcipher3-wheels` or set `DB_KEY` — not to set `ALLOW_UNENCRYPTED_DB`.

**"Could not open the database with the supplied DB_KEY."**
The key does not match the file. Check `.env` for a stray space or a truncated paste. A
database created with a different key cannot be opened with this one, ever.

**"It looks like an encrypted database and DB_KEY is not set."**
The file is encrypted but the app has no key. Usually a `.env` that did not get copied to a
new machine, or a restored backup on a server whose `.env` was rebuilt from
`.env.example`. Put the original key back.

**Service is running but nothing loads.**
Check in this order: `type C:\shopapp\logs\service.log`; then `tailscale status` on both
devices; then the firewall rule from §5.2. If `http://localhost:8000` works on the server
but not from the phone, it is Tailscale or the firewall, not the app.

**Locked out after wrong passwords.**
`manage.py unlock owner`. Tune `LOGIN_MAX_ATTEMPTS` and `LOGIN_LOCKOUT_MINUTES` in `.env`.

**Signed out constantly.**
`SESSION_IDLE_MINUTES` in `.env`. 60 is reasonable for a counter; lower it if the screen
faces customers.

**Barcode scanner types the code but nothing happens.**
The scanner must send Enter after the code — that is what submits the field. Almost all HID
scanners do by default; if yours does not, its manual will have a configuration barcode for
"add carriage return / Enter suffix".

**Invoice prints with the wrong address.**
Settings, not the code. Nothing about the shop is hardcoded.

---

## 10. What has and has not been tested

Stated plainly so you know where to be careful.

**Verified by running it, on Linux with real SQLCipher 4.12.0:**

- Encryption at rest is genuinely on — the file header is random bytes, the standard SQLite
  driver cannot open the file, and searching the raw bytes for product names, GSTINs and
  invoice numbers finds nothing.
- Wrong key, missing key and correct key all behave correctly and explain themselves.
- Backups are themselves encrypted, and reopen with the same key.
- Full business round-trip written and read back **in a separate process**: purchases and
  stock, gap-free invoice numbering, CGST/SGST for a Maharashtra buyer, IGST for a Karnataka
  buyer, serials with computed warranty expiry, and both GST registers reconciling with what
  was entered.
- All 34 screens load with no errors against an encrypted database.
- 75 automated tests pass, with encryption on.

**Not yet exercised on real hardware:**

- **The USB barcode scanner.** The auto-focused input and its Enter handling are
  implemented and work with keyboard input, but no physical scanner has been attached.
- **Print → Save as PDF.** The A4 print stylesheet has not been eyeballed on a real
  printout.
- **The Windows-specific operations in §4, §5 and §6** — NSSM, Tailscale and `schtasks`.
  These are written from the tools' documented behaviour, not from a run on your server.
  Expect to adjust a path. The app itself is platform-independent; it is the service
  plumbing that has not been rehearsed.
