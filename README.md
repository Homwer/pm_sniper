# pm_sniper
Script to snipe a rare product from pulsedmedia

# pm_snipe.py - Installation & Usage on a Linux Server

Quick guide for a standard Debian/Ubuntu system (root or with sudo).

## 1. Install system packages

```bash
apt update
apt install -y python3 python3-venv python3-pip
```

`python3-venv` is needed to create a virtual environment (venv) below.
Without a venv, `pip install` often fails on newer Debian/Ubuntu versions
with `externally-managed-environment` - the venv cleanly avoids that.

## 2. Create a project directory

```bash
mkdir -p /root/order_pm
cd /root/order_pm
```

Put `pm_snipe.py` into this directory (e.g. upload it with `scp`).

## 3. Create the virtual environment

```bash
python3 -m venv venv
```

Creates a `venv/` folder with its own Python + pip, independent of the
system Python.

## 4. Activate the venv

```bash
source venv/bin/activate
```

The prompt then shows a `(venv)` prefix. `python3` and `pip` now point
into the venv instead of the system.

Leave it at any time with:

```bash
deactivate
```

## 5. Install dependencies (with the venv activated)

```bash
pip install requests beautifulsoup4
```

## 6. Create watchlist.json

```bash
python3 pm_snipe.py --init
```

Asks interactively for:

- **Product ID (pid)** - found in the product URL as
  `cart.php?a=add&pid=NNN`
- **Billing cycle**: `monthly`, `quarterly`, `semiannually`,
  `annually`, `biennially`, or `triennially`
- whether to add another product

Writes the result to `watchlist.json`. You can also edit the file by
hand later (add more lines, reorder - order in the file = processing
order).

## 7. Store the session cookie and seedbox username

```bash
python3 pm_snipe.py --setup
```

Asks in turn for:

1. the session cookie
2. the desired seedbox username

**How to get the cookie:**

1. Log in in your browser at
   `https://pulsedmedia.com/clients/clientarea.php`, tick "Remember me",
   solve the captcha yourself.
2. Press `F12` -> **Network** tab -> reload the page with `F5`.
3. Click the `clientarea.php` row (type: *document*).
4. On the right: **Headers** -> **Request Headers** -> the `Cookie:` line.
5. Copy the whole value after it and paste it in when `--setup` asks.

The full cookie consists of several `key=value` pairs separated by
semicolons. A single pair in there - e.g. the auto-generated WHMCS
auto-login token - looks roughly like this:

```
WHMCSmNqPBCNDcH8d=8705a591a95cfe44cb48a18a3e6774e3;
```

The full header usually contains other pairs alongside it too (e.g.
`PHPSESSID=...`). Just paste the whole copied string - a leading
`Cookie:` and surrounding quotes are stripped automatically.

Do **not** use `document.cookie` in the browser - the session cookies
are HttpOnly and won't show up there.

## 8. Test without buying anything

Check availability once (buys nothing):

```bash
python3 pm_snipe.py --probe
```

Full rehearsal: really puts the product in the cart, fills out the
configuration form, loads the checkout page including credit
application, but does **not** submit the final order. The cart is
emptied automatically afterwards.

```bash
python3 pm_snipe.py --dry-run
```

## 9. Live operation

```bash
python3 pm_snipe.py
```

Watches the products from `watchlist.json` at the chosen interval
(`--interval`, default 15s) and orders automatically as soon as one
becomes available. Stops after the first successful purchase - with
`--buy-all` it keeps running until every product in the list has been
bought.

## 10. Keep it running in the background

For cron or systemd you don't need an activated venv - just point
directly at the venv's Python binary:

```bash
/root/order_pm/venv/bin/python3 /root/order_pm/pm_snipe.py
```

For a simple interactive background session, e.g. with `screen`:

```bash
apt install -y screen
screen -S pm_snipe
```

Inside the screen session, activate the venv and start it:

```bash
source venv/bin/activate && python3 pm_snipe.py
```

Detach with `Ctrl+A`, then `D`. Reattach with:

```bash
screen -r pm_snipe
```

## Files this creates

| File              | Contents                                          |
|-------------------|----------------------------------------------------|
| `watchlist.json`  | products being watched (pid + cycle)               |
| `.pm_cookie`       | session cookie (chmod 600)                         |
| `.pm_username`     | seedbox username (chmod 600)                       |
| `pm_snipe.log`     | log file (appended to)                             |
| `debug/`           | HTML dumps on error (directory 700, files 600)     |

All files end up in the current working directory unless a different
path is given via the matching `--` option.
