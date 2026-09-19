#!/usr/bin/env python3
"""
pm_snipe.py - stock monitor and auto-order tool for Pulsed Media (WHMCS).

REQUIREMENTS
    Python 3.8+, requests, beautifulsoup4
        pip3 install requests beautifulsoup4

QUICK START
    python3 pm_snipe.py --init      ask which pid(s)/cycle(s) to snipe,
                                    create watchlist.json from that
    python3 pm_snipe.py --setup     store session cookie
    python3 pm_snipe.py --probe     check what is currently in stock
    python3 pm_snipe.py --dry-run   full rehearsal (see below)
    python3 pm_snipe.py             live

SEEDBOX USERNAME
    A random username (3-8 lowercase letters) is generated for each run and
    used for every order placed during it. Pass --username to force a
    specific one instead.

WATCHLIST (watchlist.json) - built by --init, two fields per product:
    [
      { "pid": 355, "cycle": "monthly" },
      { "pid": 359, "cycle": "annually" }
    ]
    pid    product id from cart.php?a=add&pid=NNN
    cycle  monthly | quarterly | semiannually | annually | biennially | triennially
    Products are processed top to bottom. Edit the file directly to add,
    remove, or reorder products later.

--dry-run
    Actually puts the product in the cart, loads the checkout page, applies
    account credit and prints the finished order POST - but does NOT submit
    it. The cart is emptied afterwards. Without filling the cart WHMCS never
    renders a checkout page, so the credit step could not be verified.

LOGIN
    No automatic login - Pulsed Media uses a captcha. Log in once in your
    browser and hand the session cookie to the script (--setup). The script
    does not bypass any captcha, it continues inside your existing session.

PAYMENT
    Orders are paid from account credit only. If the credit cannot be
    applied, nothing is ordered and the cart is left filled for you.

The script only ever connects to pulsedmedia.com.
"""

import sys

if sys.version_info < (3, 8):
    sys.exit(f"Python 3.8 or newer required, found "
             f"{sys.version_info.major}.{sys.version_info.minor}")

_missing = []
try:
    import requests
except ImportError:
    _missing.append("requests")
try:
    from bs4 import BeautifulSoup
except ImportError:
    _missing.append("beautifulsoup4")

if _missing:
    sys.exit(
        "Missing packages: " + ", ".join(_missing) + "\n\n"
        "Install them with:\n"
        "    pip3 install " + " ".join(_missing) + "\n\n"
        "If that fails with 'externally-managed-environment':\n"
        "    pip3 install --break-system-packages " + " ".join(_missing) + "\n\n"
        "Or cleanly inside a virtual environment:\n"
        "    python3 -m venv venv\n"
        "    ./venv/bin/pip install " + " ".join(_missing) + "\n"
        "    ./venv/bin/python pm_snipe.py\n")

import argparse
import json
import os
import random
import re
import string
import time
import traceback
from datetime import datetime
from html import unescape
from pathlib import Path
from urllib.parse import urljoin

BASE = "https://pulsedmedia.com/clients"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
# The author's affiliate id, thanks for using it
AFFILIATE_ID = "3148"
COOKIE_FILE = Path(".pm_cookie")

VALID_CYCLES = ("monthly", "quarterly", "semiannually",
                "annually", "biennially", "triennially")

CAPTCHA_MARKERS = ("g-recaptcha", "h-captcha", "hcaptcha", "recaptcha",
                   "cf-turnstile", "captcha")

CSRF_RE = re.compile(r"csrfToken\s*=\s*['\"]([A-Za-z0-9]+)['\"]")
COOKIE_PREFIX_RE = re.compile(r"^\s*cookie\s*:\s*", re.I)

# Currency may appear as a symbol or a code, before or after the amount.
# HTML entities are decoded before matching.
TOTAL_RE = re.compile(
    r"Total Due Today.{0,200}?"
    r"([€$]\s*[0-9]+[.,][0-9]{2}|[0-9]+[.,][0-9]{2}\s*(?:[€$]|EUR|USD|AUD))",
    re.S | re.I)

KEEPALIVE_SECONDS = 240

_LOGFILE = None
_DUMPDIR = None


# --------------------------------------------------------------------------
# Logging and HTML dumps
# --------------------------------------------------------------------------

def setup_logging(log_path, dump_dir):
    global _LOGFILE, _DUMPDIR
    if _LOGFILE:
        try:
            _LOGFILE.close()
        except OSError:
            pass
    _LOGFILE, _DUMPDIR = None, None
    if log_path:
        _LOGFILE = open(log_path, "a", buffering=1, encoding="utf-8")
        _LOGFILE.write(f"\n{'=' * 60}\nStart {datetime.now():%Y-%m-%d %H:%M:%S} "
                       f"argv={' '.join(sys.argv[1:])}\n")
    if dump_dir:
        _DUMPDIR = Path(dump_dir)
        _DUMPDIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(_DUMPDIR, 0o700)
        except OSError:
            pass


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    if _LOGFILE:
        _LOGFILE.write(line + "\n")


def alert(msg):
    """Important message. Log and stdout only - nothing leaves the machine."""
    log(f"*** {msg}")


def dump(tag, text):
    """
    Save an HTML response for troubleshooting.
    Note: checkout pages contain your billing details.
    Directory is 0700, files are 0600.
    """
    if not _DUMPDIR or not text:
        return None
    now = datetime.now()
    stamp = now.strftime("%Y%m%d-%H%M%S-") + f"{now.microsecond // 1000:03d}"
    path = _DUMPDIR / f"{stamp}_{tag}.html"
    try:
        with open(path, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(text)
        os.chmod(path, 0o600)
    except OSError as e:
        log(f"   dump failed: {e}")
        return None
    log(f"   HTML saved: {path}")
    return path


# --------------------------------------------------------------------------
# Session, cookie, username
# --------------------------------------------------------------------------

COOKIE_HELP = """
How to get the session cookie:
  1. Log in at https://pulsedmedia.com/clients/clientarea.php, tick
     "Remember me" and solve the captcha yourself.
  2. Press F12 -> "Network" tab -> reload the page with F5.
  3. Click the "clientarea.php" row (type: document).
  4. On the right: "Headers" -> "Request Headers" -> the "Cookie:" line.
  5. Copy the whole value after it and paste it here.

Do not use document.cookie - the session cookies are HttpOnly and will be
missing there. A leading "Cookie:" and surrounding quotes are fine, they get
stripped automatically.
"""


def new_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    return s


def session_from_cookie(raw):
    s = new_session()
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k:
            s.cookies.set(k, v, domain="pulsedmedia.com", path="/")
    return s


def clean_cookie(raw):
    raw = (raw or "").strip().replace("\n", " ").replace("\r", " ")
    raw = COOKIE_PREFIX_RE.sub("", raw)
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        raw = raw[1:-1]
    return raw.strip()


def logged_in(s):
    r = s.get(f"{BASE}/clientarea.php", timeout=20)
    r.raise_for_status()
    low = r.text.lower()
    return "logout.php" in low or "action=details" in low


def store(path, value, quiet=False):
    path.write_text(value.rstrip("\n") + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if not quiet:
        log(f"Saved: {path}")


def jar_to_string(s):
    return "; ".join(f"{c.name}={c.value}" for c in s.cookies)


def persist_jar(s, previous):
    """WHMCS rotates the remember-me token. Without writing it back, the next
    start would begin with a dead cookie."""
    current = jar_to_string(s)
    if current and current != previous:
        store(COOKIE_FILE, current, quiet=True)
        log("   cookie refreshed and saved")
    return current


def get_session(ask_again=False):
    raw, source = None, None
    if not ask_again:
        if os.getenv("PM_COOKIE"):
            raw, source = os.getenv("PM_COOKIE"), "PM_COOKIE"
        elif COOKIE_FILE.exists():
            raw, source = COOKIE_FILE.read_text(), str(COOKIE_FILE)

    for attempt in range(4):
        if raw:
            raw = clean_cookie(raw)
            if "=" not in raw:
                log("That does not look like a cookie (no '=').")
            else:
                s = session_from_cookie(raw)
                try:
                    if logged_in(s):
                        log(f"Session valid (source: {source or 'input'})")
                        store(COOKIE_FILE, jar_to_string(s), quiet=True)
                        return s
                    log("Cookie rejected - expired or different IP.")
                except requests.RequestException as e:
                    log(f"Network error while checking: {e}")
                    if not sys.stdin.isatty():
                        sys.exit(1)

        if not sys.stdin.isatty():
            sys.exit("No valid cookie and no terminal to ask on.\n"
                     "Run once interactively:  python3 pm_snipe.py --setup")
        if attempt == 3:
            break
        print(COOKIE_HELP)
        try:
            raw, source = input("Cookie> "), None
        except (EOFError, KeyboardInterrupt):
            sys.exit("\nAborted.")

    sys.exit("Invalid several times - aborting.")


def random_username():
    """3-8 lowercase letters, picked fresh for each run."""
    length = random.randint(3, 8)
    return "".join(random.choice(string.ascii_lowercase) for _ in range(length))


def set_affiliate(s, aff):
    """Set the affiliate cookie."""
    if not aff:
        return False
    try:
        s.get(f"{BASE}/aff.php?aff={aff}", timeout=15, allow_redirects=True)
    except requests.RequestException as e:
        log(f"Could not set affiliate cookie: {e}")
        return False
    return any(c.name == "WHMCSAffiliateID" for c in s.cookies)


# --------------------------------------------------------------------------
# Forms
# --------------------------------------------------------------------------

def has_captcha(html):
    low = html.lower()
    return any(mark in low for mark in CAPTCHA_MARKERS)


def extract_csrf(html):
    """standard_cart renders no <input name="token">; the token sits in a JS
    variable inside <head>."""
    m = CSRF_RE.search(html)
    return m.group(1) if m else None


def parse_form(html, form_ids=(), must_contain=(), default_action="cart.php"):
    soup = BeautifulSoup(html, "html.parser")
    form = None

    for fid in form_ids:
        form = soup.find("form", id=fid)
        if form:
            break

    # Fall back to matching characteristic field names. More reliable than
    # the action attribute: WHMCS pages carry several forms with the same
    # action (promo code, empty cart, remove item).
    if not form and must_contain:
        for cand in soup.find_all("form"):
            names = " ".join(e.get("name", "") for e in
                             cand.find_all(["input", "select", "textarea"]))
            if any(frag in names for frag in must_contain):
                form = cand
                break

    if not form:
        raise RuntimeError(f"form not found (ids={list(form_ids)}, "
                           f"fields={list(must_contain)}).")

    data = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        typ = (inp.get("type") or "text").lower()
        if not name or typ in ("submit", "button", "image", "file"):
            continue
        if typ in ("checkbox", "radio"):
            if inp.has_attr("checked"):
                data[name] = inp.get("value", "on")
        else:
            data[name] = inp.get("value", "")

    for sel in form.find_all("select"):
        name = sel.get("name")
        if name:
            opt = sel.find("option", selected=True) or sel.find("option")
            if opt:
                data[name] = opt.get("value", "")

    for ta in form.find_all("textarea"):
        if ta.get("name"):
            data[ta["name"]] = ta.get_text()

    raw_action = (form.get("action") or "").strip() or default_action
    # urljoin instead of concatenation: action="/clients/cart.php" would
    # otherwise turn into .../clients/clients/cart.php
    action = urljoin(f"{BASE}/", raw_action)
    log(f"   form '{form.get('id') or '?'}' -> POST {action}")
    return form, action, data


def set_username(form, data, username):
    """Locate the username field by its label text. The customfield number
    differs per product (577 and 591 seen so far), so it is not hardcoded."""
    for inp in form.find_all(["input", "select"]):
        name = inp.get("name")
        typ = (inp.get("type") or "text").lower()
        if not name or typ in ("submit", "button", "hidden", "checkbox", "radio"):
            continue
        text = ""
        if inp.get("id"):
            lbl = form.find("label", attrs={"for": inp["id"]})
            if lbl:
                text = lbl.get_text(" ", strip=True)
        if not text:
            parent = inp.find_parent(["div", "td", "tr", "p", "li"])
            if parent:
                text = parent.get_text(" ", strip=True)
        if "username" in text.lower():
            data[name] = username
            log(f"   {name} = {username!r}")
            return name
    return None


def check_cycle(form, data, wanted, html):
    """
    The billingcycle URL parameter only preselects. If the product does not
    offer that term, WHMCS silently falls back to its default - better to skip
    the order than to be billed for the wrong interval.
    """
    if not wanted:
        return True

    sel = form.find("select", attrs={"name": "billingcycle"})
    radios = form.find_all("input", attrs={"name": "billingcycle",
                                           "type": "radio"})
    if sel:
        offered = [o.get("value") for o in sel.find_all("option") if o.get("value")]
    elif radios:
        offered = [r.get("value") for r in radios if r.get("value")]
    else:
        current = data.get("billingcycle")
        if current and current != wanted:
            dump("cycle_mismatch", html)
            alert(f"Billing cycle would be '{current}' but '{wanted}' was "
                  f"requested - not ordering.")
            return False
        if not current:
            log(f"   billingcycle: no field in the form, relying on URL "
                f"parameter '{wanted}'")
        return True

    if wanted not in offered:
        dump("cycle_unavailable", html)
        alert(f"Product does not offer '{wanted}'. Offered: {offered}. "
              f"Nothing ordered - adjust watchlist.json.")
        return False

    if data.get("billingcycle") != wanted:
        log(f"   billingcycle corrected: {data.get('billingcycle')!r} -> "
            f"{wanted!r}")
        data["billingcycle"] = wanted
    else:
        log(f"   billingcycle confirmed: {wanted}")
    return True


def apply_credit(form, data):
    """
    Apply account credit. Element ids taken from
    standard_cart/js/scripts.min.js. WHMCS serves the page with
    skipCreditOnCheckout preselected. The field name is read from the form,
    not guessed.
    """
    use = form.find(id="useCreditOnCheckout")
    skip = form.find(id="skipCreditOnCheckout")
    if not use or not use.get("name"):
        log("   no credit field in the form")
        return False
    name = use["name"]
    data[name] = use.get("value", "1")
    log(f"   credit enabled: {name}={data[name]}")
    if skip and skip.get("name") and skip["name"] != name:
        data.pop(skip["name"], None)
    return True


# --------------------------------------------------------------------------
# Purchase flow
# --------------------------------------------------------------------------

def add_to_cart(s, pid, cycle):
    url = f"{BASE}/cart.php?a=add&pid={pid}"
    if cycle:
        url += f"&billingcycle={cycle}"
    r = s.get(url, timeout=20, allow_redirects=True)
    r.raise_for_status()
    if re.search(r"out of stock", r.text, re.I):
        return None
    return r


def empty_cart(s):
    """
    On the cart page "empty" is a POST form carrying a token:
        <form method="post" action="/clients/cart.php">
          <input type="hidden" name="token" value="...">
          <input type="hidden" name="a" value="empty">
    So fetch the token and post it instead of guessing a GET.
    """
    try:
        r = s.get(f"{BASE}/cart.php?a=view", timeout=15)
        r.raise_for_status()
        tok = extract_csrf(r.text)
        if not tok:
            m = re.search(r'name="token"\s+value="([^"]+)"', r.text)
            tok = m.group(1) if m else None
        if not tok:
            log("   empty cart: no token found, skipped")
            return False
        s.post(f"{BASE}/cart.php", data={"a": "empty", "token": tok},
               headers={"Referer": f"{BASE}/cart.php?a=view"}, timeout=15)
        return True
    except requests.RequestException as e:
        log(f"   emptying the cart failed: {e}")
        return False


def _redacted(d):
    return {k: (str(v)[:8] + "..." if k == "token" else v) for k, v in d.items()}


def configure(s, html, cycle, username, dry_run):
    if "confproduct" not in html and "customfield" not in html:
        log("   no configuration step needed")
        return True

    if has_captcha(html):
        dump("config_captcha", html)
        alert("CAPTCHA on the configuration page - aborting, do it manually.")
        return False

    form, _, data = parse_form(html, form_ids=("frmConfigureProduct", "frmProduct"),
                               must_contain=("billingcycle", "customfield["))

    if not data.get("token"):
        tok = extract_csrf(html)
        if not tok:
            dump("config_no_token", html)
            alert("No CSRF token on the configuration page - aborting.")
            return False
        data["token"] = tok
        log("   CSRF token taken from the page JS")

    if not check_cycle(form, data, cycle, html):
        return False

    if not set_username(form, data, username):
        dump("config_no_username_field", html)
        alert("No username field found - aborting, nothing ordered.")
        return False

    # Exactly what the browser does. From standard_cart/js/scripts.min.js:
    #   n = whmcsBaseUrl+"/cart.php"
    #   i = "a=confproduct&"+jQuery("#frmConfigureProduct").serialize()
    #   displayRecommendations(n, "addproductajax=1&"+i, false)
    # Without addproductajax=1 WHMCS answers 200 but stores nothing.
    target = f"{BASE}/cart.php"
    data["a"] = "confproduct"
    data["addproductajax"] = "1"

    if data.get("i") not in (None, "", "0"):
        log(f"   warning: cart index i={data['i']} - something is already in "
            f"the cart and would be ordered along at checkout")

    log(f"   config POST {target} -> {_redacted(data)}")
    if dry_run:
        # Done in dry runs too: WHMCS only renders the checkout page once the
        # product sits in the cart, otherwise the credit step cannot be
        # verified. The cart is emptied again afterwards.
        log("   (dry run: fills the cart, will be emptied afterwards)")

    r = s.post(target, data=data,
               headers={"Referer": f"{BASE}/cart.php?a=confproduct",
                        "X-Requested-With": "XMLHttpRequest"},
               timeout=20, allow_redirects=True)
    try:
        r.raise_for_status()
    except requests.HTTPError as e:
        dump("config_post_error", r.text)
        alert(f"Config POST failed: {e}")
        return False

    dump("config_post_response", r.text)

    if "frmConfigureProduct" in r.text:
        found = re.findall(
            r'containerProductValidationErrorsList[^>]*>(.*?)</ul>', r.text, re.S)
        detail = re.sub(r"<[^>]+>", " ", found[0]).strip() if found else ""
        alert(f"Configuration rejected, the form came back. {detail}".strip())
        return False
    return True


def place_order(s, dry_run):
    r = s.get(f"{BASE}/cart.php?a=checkout&e=false", timeout=20)
    r.raise_for_status()

    if re.search(r"Shopping Cart is Empty", r.text, re.I):
        dump("cart_empty", r.text)
        alert("Cart is empty - the config POST stored nothing. Not ordering.")
        return None

    if has_captcha(r.text):
        dump("checkout_captcha", r.text)
        alert("CAPTCHA at checkout - NOT ordering. The cart is filled, "
              "please finish manually.")
        return None

    try:
        form, action, data = parse_form(
            r.text, form_ids=("frmCheckout",),
            must_contain=("paymentmethod", "accepttos"),
            default_action="cart.php?a=checkout")
    except RuntimeError:
        dump("checkout_no_form", r.text)
        raise

    if not data.get("token"):
        tok = extract_csrf(r.text)
        if tok:
            data["token"] = tok
            log("   CSRF token taken from the page JS")
    if not data.get("token"):
        dump("checkout_no_token", r.text)
        raise RuntimeError("No CSRF token in the checkout form.")

    data["accepttos"] = "on"
    data.setdefault("custtype", "existing")

    total = TOTAL_RE.search(unescape(r.text))
    if total:
        log(f"   Total Due Today per page: {' '.join(total.group(1).split())}")
    else:
        log("   Total Due Today not found")

    if not apply_credit(form, data):
        dump("no_credit", r.text)
        alert("Credit cannot be applied - NOT ordering. The cart is filled, "
              "please finish manually.")
        return None

    if dry_run:
        log("DRY RUN. The order is NOT submitted. It would be:")
        log(f"   POST {action}")
        log(f"   {_redacted(data)}")
        return "dry-run"

    r = s.post(action, data=data,
               headers={"Referer": f"{BASE}/cart.php?a=checkout"},
               timeout=30, allow_redirects=True)
    r.raise_for_status()
    if "a=complete" in r.url or re.search(r"order (confirmation|number)",
                                          r.text, re.I):
        dump("order_complete", r.text)
        return r.url
    dump("checkout_unclear", r.text)
    raise RuntimeError(f"Checkout unclear. Landed on: {r.url}")


def buy(s, entry, username, dry_run, prefetched=None):
    """Returns 'ok' | 'retry' | 'abort'."""
    pid, cycle = entry["pid"], entry["cycle"]
    log(f"-> {'Testing' if dry_run else 'Buying'} pid {pid} ({cycle})")

    r = prefetched if prefetched is not None else add_to_cart(s, pid, cycle)
    if r is None:
        log("   out of stock")
        return "retry"

    if not configure(s, r.text, cycle, username, dry_run):
        return "abort"

    result = place_order(s, dry_run)

    if dry_run:
        empty_cart(s)
        log("   dry run finished, cart emptied")
        return "ok" if result else "abort"

    if result:
        alert(f"ORDERED: pid {pid} -> {result}")
        return "ok"
    return "abort"


# --------------------------------------------------------------------------
# Watchlist
# --------------------------------------------------------------------------

def ask_pid():
    while True:
        try:
            raw = input("Product ID (pid, from cart.php?a=add&pid=NNN)> ").strip()
        except (EOFError, KeyboardInterrupt):
            sys.exit("\nAborted.")
        try:
            return int(raw)
        except ValueError:
            print("Must be a number.")


def ask_cycle():
    options = ", ".join(VALID_CYCLES)
    while True:
        try:
            raw = input(f"Billing cycle ({options})> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            sys.exit("\nAborted.")
        if raw in VALID_CYCLES:
            return raw
        print(f"Allowed: {options}")


def build_watchlist_interactively():
    entries = []
    print("Which product(s) do you want to snipe?\n")
    while True:
        pid = ask_pid()
        cycle = ask_cycle()
        entries.append({"pid": pid, "cycle": cycle})
        try:
            more = input("Add another product? [y/N]> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            sys.exit("\nAborted.")
        if more != "y":
            break
        print()
    return entries


def load_watchlist(path):
    if not path.exists():
        sys.exit(f"{path} is missing. Create it with:  "
                 f"python3 {Path(sys.argv[0]).name} --init")
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        sys.exit(f"{path} is not valid JSON: {e}")
    if not isinstance(raw, list) or not raw:
        sys.exit(f"{path} must be a non-empty list.")

    clean = []
    for i, e in enumerate(raw, 1):
        if not isinstance(e, dict) or "pid" not in e or "cycle" not in e:
            sys.exit(f"{path}, entry {i}: needs 'pid' and 'cycle'.\n"
                     f'Example: {{ "pid": 355, "cycle": "monthly" }}')
        try:
            pid = int(e["pid"])
        except (TypeError, ValueError):
            sys.exit(f"{path}, entry {i}: 'pid' must be a number.")
        cycle = str(e["cycle"]).strip()
        if cycle not in VALID_CYCLES:
            sys.exit(f"{path}, entry {i}: 'cycle' is '{cycle}'.\n"
                     f"Allowed: {', '.join(VALID_CYCLES)}")
        clean.append({"pid": pid, "cycle": cycle})
    return clean


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description="Stock monitor and auto-order tool for Pulsed Media.")
    p.add_argument("--watchlist", default="watchlist.json")
    p.add_argument("--init", action="store_true", help="create watchlist.json")
    p.add_argument("--setup", action="store_true",
                   help="ask for the session cookie again")
    p.add_argument("--username",
                   help="seedbox username (default: random, 3-8 lowercase "
                        "letters)")
    p.add_argument("--probe", action="store_true",
                   help="check availability once, buy nothing")
    p.add_argument("--dry-run", action="store_true",
                   help="full rehearsal without submitting the order")
    p.add_argument("--buy-all", action="store_true",
                   help="keep running until every product was bought")
    p.add_argument("--interval", type=float, default=15.0,
                   help="seconds between checks (minimum 10)")
    p.add_argument("--affiliate", default=AFFILIATE_ID,
                   help=f"affiliate id (default: {AFFILIATE_ID})")
    p.add_argument("--no-affiliate", action="store_true",
                   help="do not attach an affiliate id")
    p.add_argument("--log-file", default="pm_snipe.log", help="'' disables it")
    p.add_argument("--debug-dir", default="debug", help="'' disables it")
    args = p.parse_args()

    setup_logging(args.log_file or None, args.debug_dir or None)
    path = Path(args.watchlist)

    if args.init:
        if path.exists():
            sys.exit(f"{path} already exists.")
        if not sys.stdin.isatty():
            sys.exit("No terminal to ask on. --init needs interactive input.")
        entries = build_watchlist_interactively()
        path.write_text(json.dumps(entries, indent=2) + "\n")
        print(f"\n{path} created:\n")
        print(path.read_text())
        print("Order in the file = processing order. Next: --setup")
        return

    if args.setup:
        get_session(ask_again=True)
        print("\nDone. Next step: python3 pm_snipe.py --probe")
        return

    watchlist = load_watchlist(path)

    if args.probe:
        s = new_session()
        for e in watchlist:
            available = add_to_cart(s, e["pid"], e["cycle"]) is not None
            log(f"pid {e['pid']:<6} {e['cycle']:<14} "
                f"{'IN STOCK' if available else 'out of stock'}")
        empty_cart(s)
        return

    s = get_session()
    username = args.username or random_username()
    log(f"Seedbox username: {username}")
    set_affiliate(s, "" if args.no_affiliate else args.affiliate)

    # Clear leftovers from aborted runs: WHMCS orders the ENTIRE cart at
    # checkout, not just the product we just added.
    if empty_cart(s):
        log("Cart emptied on startup")

    interval = max(10.0, args.interval)
    log(f"Watching {len(watchlist)} product(s), every {interval:.0f}s")

    pending = list(watchlist)
    jar = jar_to_string(s)
    last_ping = time.time()

    while pending:
        try:
            if time.time() - last_ping > KEEPALIVE_SECONDS:
                if not logged_in(s):
                    alert("Session expired.")
                    if sys.stdin.isatty():
                        s = get_session(ask_again=True)
                    else:
                        alert("No terminal - exiting. Refresh the cookie "
                              "(--setup) and start again.")
                        return
                jar = persist_jar(s, jar)
                last_ping = time.time()

            status = []
            for e in list(pending):
                r = add_to_cart(s, e["pid"], e["cycle"])
                status.append(f"{e['pid']}={'YES' if r else '-'}")
                if r is None:
                    continue
                log(f"pid {e['pid']}: in stock")
                result = buy(s, e, username, args.dry_run, prefetched=r)
                if result == "ok":
                    pending.remove(e)
                    if not args.buy_all:
                        log("Done.")
                        return
                elif result == "abort":
                    alert(f"pid {e['pid']} removed from the watch list - "
                          f"please check manually.")
                    pending.remove(e)
                else:
                    empty_cart(s)

            if pending and status:
                log(" | ".join(status))

        except requests.RequestException as e:
            log(f"Network error: {e}")
        except RuntimeError as e:
            alert(f"Aborted: {e}")
            return
        except KeyboardInterrupt:
            log("Interrupted.")
            return
        except Exception:
            log("Unexpected error:\n" + traceback.format_exc())
            raise

        time.sleep(interval + random.uniform(0, 3))

    log("All products processed.")


if __name__ == "__main__":
    main()
