# StockSetuu

**Self-hosted inventory, GST billing, warranty tracking, and service management for independent retailers in India.**

StockSetuu brings a shop's product catalogue, stock movements, purchases, sales, GST invoices, serial-number warranties, and repair jobs into one application. It runs on a computer or server managed by the shop and stores records in a local SQLCipher-encrypted SQLite database. Reports and Tally import/export tools help the owner and accountant keep books in order.

StockSetuu organizes business records; it does **not** file GST returns or replace accounting advice.

## What it includes

- Product catalogue with SKU/barcode lookup, stock counts, cost and sale prices, HSN, and GST rates.
- Purchase bills, suppliers, stock movements, and product cost history.
- GST sales invoices with intra-state CGST/SGST and inter-state IGST calculations.
- Serial-number tracking, warranty lookup, and service-job records.
- Low-stock, sales, purchase, GST, dealer, and audit reports.
- CSV/XLSX catalogue import and CSV/Tally export workflows.
- A responsive browser interface for a counter computer or phone.
- Encrypted local database storage; user accounts are created from the command line.

## Requirements

- 64-bit Python 3.10–3.14.
- Internet access during installation to download Python packages.
- A modern browser.

The pinned `sqlcipher3` dependency supplies SQLCipher and has binary wheels for supported Windows, macOS, and Linux Python versions. The application refuses to start if SQLCipher or the database key is missing, rather than silently creating an unencrypted database.

## Install and run

Choose the instructions for your operating system. Run commands from the project folder (the folder containing `manage.py` and `run.py`).

### Windows

Open Command Prompt in the project folder:

```bat
py -m venv .venv
.venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
copy .env.example .env
```

PowerShell users can activate the environment with `.\.venv\Scripts\Activate.ps1` instead. If activation is blocked, run the same commands with `.venv\Scripts\python.exe` in place of `python`.

### macOS

Open Terminal in the project folder:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
```

### Linux

Open a terminal in the project folder. On Debian or Ubuntu, install Python's venv support first if needed (`sudo apt install python3-venv`):

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
```

If installation reports that it cannot find a compatible SQLCipher wheel, use 64-bit CPython 3.10–3.14 on a supported platform. Do not enable unencrypted storage for real shop data.

## First-time setup

1. Generate a unique database key from the activated virtual environment:

   ```sh
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

2. Open `.env` in a text editor. Replace the `DB_KEY` example value with the generated key. Keep the key in a password manager or another secure location. Losing it means losing access to the database. Never commit `.env` to Git or store it beside backups.
3. Create the database and owner account:

   ```sh
   python manage.py init-db
   python manage.py create-user owner --name "Your Name"
   python manage.py seed-shop
   ```

   `create-user` prompts for a password twice. Use at least 10 characters. `seed-shop` adds editable placeholder business details; enter the correct legal name, GSTIN, and address in **Settings** before issuing real invoices.
4. Start the web app:

   ```sh
   python run.py
   ```

5. Open <http://localhost:8000> and sign in with username `owner` and the password you just created. Keep the terminal window open while the app is running. Press `Ctrl+C` to stop it.

The sample `.env` binds the server to `127.0.0.1`, so it is reachable only on the same computer. To use another device on a trusted private network, set `HOST=0.0.0.0` in `.env` and restrict access with your firewall or private VPN. Do not expose port 8000 directly to the public internet.

## Updating an existing installation

If you installed an earlier copy that used `sqlcipher3-wheels`, activate that same virtual environment and replace the old driver:

```sh
python -m pip uninstall sqlcipher3-wheels
python -m pip install -r requirements.txt
```

Keep the existing `.env` and `data/shop.db`. Do **not** run `init-db` to reset an existing shop. The app applies pending schema migrations when it starts.

## Common commands

Run these from the project folder with the virtual environment activated:

| Command | Purpose |
| --- | --- |
| `python manage.py status` | Show database encryption, schema version, and record counts. |
| `python manage.py users` | List user accounts and lockout state. |
| `python manage.py set-password owner` | Reset an account password and sign out active sessions. |
| `python manage.py backup` | Write an encrypted backup under `data/backups/`. |
| `python manage.py unlock owner` | Clear an account lockout. |

Back up the encryption key separately from database backups. To restore, stop the app, place the selected encrypted backup at `data/shop.db`, then start the app with the same `DB_KEY`.

## Tally and GST records

The catalogue importer accepts CSV and XLSX files and provides a preview before writing changes. Quantity updates are recorded in the stock ledger. The app can export product lists, GST sales and purchase registers, and Tally-ready masters and vouchers. Test Tally voucher imports in a separate company before using them for live books. GST return filing remains the responsibility of the shop's accountant.

## Data and security

- Database: `data/shop.db` (encrypted with SQLCipher).
- Backups: `data/backups/` (encrypted with the same database key).
- Local settings and database key: `.env` (ignored by Git).
- Session signing key: `data/secret_key` (generated locally and ignored by Git).

Do not publish the database, `.env`, generated session key, backups, or virtual environment. Keep at least one encrypted backup off the computer that runs the app, and keep its key somewhere separate.

## Development

The application uses FastAPI, Jinja templates, SQLite/SQLCipher, and plain JavaScript/CSS. To run with auto-reload while developing:

```sh
python run.py --reload
```

For an always-on shop server, run the app under an operating-system service manager and allow access only through a trusted private network. The app is designed for one shop database and one server process.

## Repository description

> Self-hosted inventory, GST invoicing, warranty tracking, and service management for independent Indian retailers.
