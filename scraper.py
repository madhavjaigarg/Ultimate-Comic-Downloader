#!/usr/bin/env python3
"""
Scrape the Complete Marvel Reading Order (main 616) from
https://marvelreading.com/reading-order into a CSV.

The page is server-rendered (Livewire) and paginated via ?page=N (30 rows per
page, ~1230 pages), so plain GET requests + HTML parsing are enough.

Usage:
    pip install requests beautifulsoup4 lxml
    python scrape_marvelreading.py                 # full run -> marvel_reading_order.csv
    python scrape_marvelreading.py --max-pages 3   # quick test

If you get HTTP 403 (Cloudflare), copy cf_clearance + your User-Agent from a
browser that can open the site:
    python scrape_marvelreading.py --cf-clearance 'XXXX' --user-agent 'Mozilla/5.0 ...'
and if that is still blocked, also add --impersonate chrome (pip install curl_cffi).

Browser mode: --browser (pip install playwright; playwright install chromium).
A Chrome window opens; if Cloudflare shows a check, click it once. The profile is
kept in ./pw-profile so later runs usually pass straight through.

Speed / output options:
    --delay/--jitter  each request is followed by a wait of delay + random(0..jitter) seconds
                    (defaults 0.5 + 0..0.7). Parsing uses lxml if installed (pip install lxml).
    --workers 4     fetch 4 pages at once (default 1). Each worker waits --delay seconds
                    after every request, so the overall rate is roughly workers/delay req/s.
                    (--browser always uses 1 worker.)
    --verbose       print every row to the terminal as it is written

Reliability:
    --page-retries N   re-fetch a page up to N more times if it comes back empty or
                       short (default 3). Pages that still fail are skipped, not fatal.
    --repair-passes N  after the main pass, re-fetch any pages with missing rows, up to
                       N times (default 2).
    --error-log [FILE] append warnings, retries and the integrity report to FILE
                       (default name scraper_errors.log when no FILE is given).
    At the end the CSV is de-duplicated, sorted by order, and checked for gaps.

The script is resumable: re-run it and it fetches only the pages that are still
incomplete. If your cf_clearance cookie expires (HTTP 403) it saves what it has
and exits; paste a fresh cookie and run it again.
"""
import argparse
import csv
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests
from bs4 import BeautifulSoup, SoupStrainer

try:
    import lxml  # noqa: F401  (pip install lxml) - faster than html.parser
    PARSER = "lxml"
except ImportError:
    PARSER = "html.parser"

BASE_URL = "https://marvelreading.com/reading-order"
PER_PAGE = 30
FIELDS = ["order", "date", "title", "series", "volume_year", "issue", "url"]
TITLE_RE = re.compile(r"^(?P<series>.*?)\s*\((?P<year>\d{4})\)\s*(?:#(?P<issue>.*))?$")

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
SESSION_CFG = {}
_tls = threading.local()
browser_page = None

ERROR_LOG = None
_log_lock = threading.Lock()


# ---------------------------------------------------------------- logging ----
def _write_log(msg):
    if ERROR_LOG:
        with _log_lock, open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")


def warn(msg):
    """Problem worth knowing about: stderr + error log."""
    print(msg, file=sys.stderr)
    _write_log(msg)


def info(msg):
    """Normal output that should also land in the error log (integrity report etc.)."""
    print(msg)
    _write_log(msg)


# ------------------------------------------------------------- networking ----
def build_session(user_agent, cf_clearance, impersonate):
    """Create the HTTP session.

    Cloudflare's cf_clearance cookie is tied to the User-Agent (and IP) of the
    browser that earned it, so --user-agent must be the EXACT UA string of the
    browser you copied the cookie from.
    """
    if impersonate:
        from curl_cffi import requests as cffi_requests  # pip install curl_cffi
        s = cffi_requests.Session(impersonate=impersonate)
    else:
        s = requests.Session()
    s.headers.update({
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    if cf_clearance:
        s.cookies.set("cf_clearance", cf_clearance, domain="marvelreading.com")
    return s


def get_session():
    """One HTTP session per thread (sessions aren't guaranteed thread-safe)."""
    if not hasattr(_tls, "session"):
        _tls.session = build_session(**SESSION_CFG)
    return _tls.session


CHALLENGE_TIMEOUT = 180  # seconds to wait for you to clear a Cloudflare check


def fetch_browser(page):
    """Fetch through a real Chrome window (passes Cloudflare like a normal visitor).

    Navigates ONCE, then just watches the tab. Re-navigating while a Cloudflare check
    is in progress restarts the check, which is what caused the endless
    reload-and-captcha loop in the first version.
    """
    try:
        browser_page.goto(f"{BASE_URL}?page={page}", wait_until="domcontentloaded")
    except Exception as e:  # e.g. navigation interrupted by Cloudflare's own redirect
        print(f"  (page {page}: navigation note: {str(e).splitlines()[0]})")
    start, last_msg = time.time(), 0
    while time.time() - start < CHALLENGE_TIMEOUT:
        try:
            if browser_page.query_selector("tr.ro-tr"):
                return browser_page.content()
            title = browser_page.title()
        except Exception:  # page is mid-navigation/redirect: wait, don't re-navigate
            time.sleep(1)
            continue
        waited = int(time.time() - start)
        if waited - last_msg >= 10:
            last_msg = waited
            print(f"  waiting for the reading-order table (tab title: {title!r}, {waited}s). "
                  f"If a Cloudflare check is showing, complete it in the browser window.")
        time.sleep(1)
    raise RuntimeError(f"Page {page}: reading-order table never appeared within "
                       f"{CHALLENGE_TIMEOUT}s (challenge unsolved or looping?)")


def fetch(page, retries=5):
    if browser_page is not None:
        return fetch_browser(page)
    for attempt in range(1, retries + 1):
        try:
            r = get_session().get(BASE_URL, params={"page": page}, timeout=30)
            if r.status_code == 200:
                return r.text
            if r.status_code == 403:
                warn(f"  page {page}: 403 server={r.headers.get('server')} "
                     f"cf-mitigated={r.headers.get('cf-mitigated', '-')} body[:150]={r.text[:150]!r}")
                raise PermissionError(
                    "HTTP 403 (Cloudflare block). Easiest fix: run with --browser. Otherwise open the "
                    "site in your browser, copy the cf_clearance cookie (DevTools > Application > "
                    "Cookies) and your exact User-Agent, then run with --cf-clearance '<value>' "
                    "--user-agent '<UA>'. If it still 403s, add --impersonate chrome "
                    "(pip install curl_cffi).")
            if r.status_code in (429, 500, 502, 503, 504):
                wait = 5 * attempt
                warn(f"  page {page}: HTTP {r.status_code}, retrying in {wait}s "
                     f"(attempt {attempt}/{retries})")
                time.sleep(wait)
                continue
            r.raise_for_status()
        except PermissionError:
            raise
        except Exception as e:
            wait = 5 * attempt
            warn(f"  page {page}: {e}, retrying in {wait}s (attempt {attempt}/{retries})")
            time.sleep(wait)
    raise RuntimeError(f"Failed to fetch page {page} after {retries} attempts")


# ---------------------------------------------------------------- parsing ----
def parse_rows(html):
    # Only build tree nodes for the table rows; skips the huge <option> dropdowns etc.
    soup = BeautifulSoup(html, PARSER, parse_only=SoupStrainer("tr"))
    rows = []
    for tr in soup.find_all("tr", class_="ro-tr"):
        order_td = tr.find("td", class_="ro-td--order")
        date_td = tr.find("td", class_="ro-td--date")
        link = tr.find("a", class_="ro-title-link")
        if not (order_td and link):
            continue

        title = link.get_text(strip=True)
        m = TITLE_RE.match(title)
        rows.append({
            "order": int(re.sub(r"\D", "", order_td.get_text())),  # "1,021" -> 1021
            "date": date_td.get_text(strip=True) if date_td else "",
            "title": title,
            "series": m.group("series") if m else "",
            "volume_year": m.group("year") if m else "",
            "issue": (m.group("issue") or "") if m else "",
            "url": link.get("href", ""),
        })
    return rows


def last_page(html):
    soup = BeautifulSoup(html, PARSER, parse_only=SoupStrainer(attrs={"data-page-link": True}))
    pages = [int(a["data-page-link"]) for a in soup.find_all(attrs={"data-page-link": True})]
    return max(pages) if pages else 1


# ------------------------------------------------------------ concurrency ----
def iter_pages(pages, workers, fn):
    """Yield (page, fn(page)) in page order, running up to `workers` at once."""
    pages = list(pages)
    if workers <= 1:  # sequential, stays in the main thread (required for --browser)
        for p in pages:
            yield p, fn(p)
        return
    pending, nxt = {}, 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        try:
            for p in pages:
                while nxt < len(pages) and len(pending) < workers * 2:  # sliding window
                    pending[pages[nxt]] = ex.submit(fn, pages[nxt])
                    nxt += 1
                yield p, pending.pop(p).result()
        finally:
            for fut in pending.values():
                fut.cancel()


# --------------------------------------------------------------- CSV / QA ----
def load_existing(path):
    """Return the set of saved order numbers; migrate old CSVs to the current columns."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        old_fields = reader.fieldnames
        rows = list(reader)
    if old_fields != FIELDS:  # e.g. an older file that still has story_arc
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print(f"Migrated {path} to columns: {', '.join(FIELDS)}")
    return {int(r["order"]) for r in rows}


def finalize_csv(path):
    """De-duplicate by order, sort by order, rewrite atomically.

    Returns (rows_by_order, number_of_duplicates_removed).
    """
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return {}, 0
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    by_order, dups = {}, 0
    for r in rows:
        o = int(r["order"])
        if o in by_order:
            dups += 1
        else:
            by_order[o] = r
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for o in sorted(by_order):
            w.writerow(by_order[o])
    os.replace(tmp, path)
    return by_order, dups


def incomplete_pages(seen, total):
    """Pages whose rows aren't all saved yet (assumes 30 consecutive orders per page)."""
    out = []
    for p in range(1, total + 1):
        lo = (p - 1) * PER_PAGE + 1
        if p < total:
            if any(o not in seen for o in range(lo, lo + PER_PAGE)):
                out.append(p)
        elif lo not in seen:  # last page has 1-30 rows; need at least its first row
            out.append(p)
    return out


def ranges(nums):
    """[1,2,3,7,9,10] -> '1-3, 7, 9-10'"""
    nums, out, i = sorted(nums), [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ", ".join(out)


def integrity_report(by_order, dups, total, partial_run):
    """Print/log the final integrity check. Returns True if everything is clean."""
    info("---- Integrity check ----")
    if not by_order:
        info("No rows saved.")
        return False
    top = max(by_order)
    missing = [o for o in range(1, top + 1) if o not in by_order]
    blanks = [o for o, r in by_order.items() if not (r["title"] and r["date"] and r["url"])]
    last_first = (total - 1) * PER_PAGE + 1
    ok = True

    info(f"Rows: {len(by_order)} (orders 1..{top}); duplicates removed: {dups}")
    if missing:
        ok = False
        info(f"MISSING {len(missing)} order number(s): {ranges(missing)}")
    if blanks:
        ok = False
        info(f"{len(blanks)} row(s) with an empty date/title/url: {ranges(blanks)}")
    if top < last_first:
        if partial_run:
            info(f"Partial run: stopped before the last page (orders up to {top}).")
        else:
            ok = False
            info(f"Last page ({total}) not reached: highest order is {top}, expected >= {last_first}.")
    if ok and not partial_run:
        info("OK: contiguous, no duplicates, no blank fields.")
    return ok


# ------------------------------------------------------------------- main ----
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="marvel_reading_order.csv")
    ap.add_argument("--delay", type=float, default=0.5,
                    help="minimum seconds each worker waits after a request")
    ap.add_argument("--jitter", type=float, default=0.7,
                    help="extra random wait of 0..JITTER seconds added to --delay")
    ap.add_argument("--workers", type=int, default=1, help="number of pages to fetch concurrently")
    ap.add_argument("--verbose", "-v", action="store_true", help="print each row as it is written")
    ap.add_argument("--page-retries", type=int, default=3,
                    help="extra attempts for a page that comes back empty/short (default 3)")
    ap.add_argument("--repair-passes", type=int, default=2,
                    help="times to re-fetch pages with missing rows after the main pass (default 2)")
    ap.add_argument("--error-log", nargs="?", const="scraper_errors.log", default=None,
                    metavar="FILE", help="write warnings + integrity report to FILE")
    ap.add_argument("--max-pages", type=int, default=None, help="stop after N pages (testing)")
    ap.add_argument("--cf-clearance", default=os.environ.get("CF_CLEARANCE"),
                    help="cf_clearance cookie value copied from your browser (or env CF_CLEARANCE)")
    ap.add_argument("--user-agent", default=DEFAULT_UA,
                    help="exact User-Agent of the browser the cookie came from")
    ap.add_argument("--impersonate", nargs="?", const="chrome", default=None,
                    help="use curl_cffi with a browser TLS fingerprint (e.g. chrome)")
    ap.add_argument("--browser", action="store_true",
                    help="drive a real Chrome window via Playwright (most reliable vs Cloudflare)")
    args = ap.parse_args()

    global ERROR_LOG, browser_page
    ERROR_LOG = args.error_log
    if ERROR_LOG:
        _write_log(f"=== run started: {' '.join(sys.argv[1:]).replace(args.cf_clearance or chr(0), '***')} ===")

    workers = max(1, args.workers)
    print(f"HTML parser: {PARSER}" + ("" if PARSER == "lxml" else "  (tip: pip install lxml for faster parsing)"))
    if args.browser:
        if workers > 1:
            print("--browser drives a single window, so using 1 worker")
            workers = 1
        # pip install playwright && playwright install chromium
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        # Hide the most obvious automation flags that make Cloudflare re-challenge forever.
        launch_kw = dict(headless=False, no_viewport=True,
                         ignore_default_args=["--enable-automation"],
                         args=["--disable-blink-features=AutomationControlled"])
        try:
            ctx = pw.chromium.launch_persistent_context("pw-profile", channel="chrome", **launch_kw)
        except Exception:
            ctx = pw.chromium.launch_persistent_context("pw-profile", **launch_kw)
        browser_page = ctx.new_page()
    else:
        SESSION_CFG.update(user_agent=args.user_agent, cf_clearance=args.cf_clearance,
                           impersonate=args.impersonate)

    # Resume support: remember which order numbers we already have.
    seen = set()
    if os.path.exists(args.out) and os.path.getsize(args.out) > 0:
        seen = load_existing(args.out)
        print(f"Resuming: {len(seen)} rows already saved")

    state = {"total": 1}
    dups_total = 0
    try:
        # Page 1 also tells us how many pages there are.
        first_html = fetch(1)
        total = state["total"] = last_page(first_html)

        def polite_sleep():
            time.sleep(args.delay + random.uniform(0, args.jitter))

        def work(page):
            """Fetch + parse one page, retrying if it comes back empty or short."""
            expected = PER_PAGE if page < total else 1  # last page may have fewer rows
            rows = []
            for attempt in range(args.page_retries + 1):
                try:
                    if page == 1 and attempt == 0:
                        html = first_html  # already downloaded
                    else:
                        html = fetch(page)
                        polite_sleep()
                    rows = parse_rows(html)
                except PermissionError:
                    raise
                except Exception as e:
                    warn(f"  page {page}: attempt {attempt + 1} failed: {e}")
                    rows = []
                else:
                    if len(rows) >= expected:
                        return rows
                    warn(f"  page {page}: got {len(rows)} row(s), expected "
                         f"{PER_PAGE if page < total else '1-30'} (attempt {attempt + 1}"
                         f"/{args.page_retries + 1})")
                if attempt < args.page_retries:
                    time.sleep(2 * (attempt + 1) + random.uniform(0, 1))
            return rows  # whatever we got; the repair pass / report will flag it

        def run_pass(pages, label):
            """Fetch `pages`, append new rows to the CSV. Returns False if aborted."""
            need_header = not os.path.exists(args.out) or os.path.getsize(args.out) == 0
            consecutive_empty, failed = 0, []
            with open(args.out, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=FIELDS)
                if need_header:
                    writer.writeheader()
                for n, (page, page_rows) in enumerate(iter_pages(pages, workers, work), 1):
                    for r in page_rows:
                        if r["order"] in seen:
                            continue
                        writer.writerow(r)
                        seen.add(r["order"])
                        if args.verbose:
                            print(f"{r['order']:>6} | {r['date']:>7} | {r['title']}")
                    f.flush()

                    if len(page_rows) < (PER_PAGE if page < total else 1):
                        failed.append(page)
                        warn(f"  page {page}: incomplete after retries ({len(page_rows)} rows)")
                    consecutive_empty = consecutive_empty + 1 if not page_rows else 0
                    if consecutive_empty >= 5:
                        warn(f"{label}: 5 empty pages in a row - looks like a block or rate limit. "
                             f"Stopping; wait a bit (or raise --delay) and re-run to continue.")
                        return False
                    if n % 10 == 0 or n == len(pages):
                        print(f"  [{label}] page {page}/{total} ({n}/{len(pages)} this pass, "
                              f"{len(seen)} rows total)")
            if failed:
                info(f"{label}: {len(failed)} page(s) incomplete: {ranges(failed)}")
            return True

        pages = incomplete_pages(seen, total)
        partial_run = args.max_pages is not None
        if partial_run:
            pages = pages[:args.max_pages]
        print(f"{total} pages total; {len(pages)} to fetch with {workers} worker(s)")

        ok = run_pass(pages, "main") if pages else True

        # Repair passes: re-fetch pages that still have missing rows.
        for k in range(1, args.repair_passes + 1):
            if not ok or partial_run:
                break
            by_order, d = finalize_csv(args.out)
            dups_total += d
            seen = set(by_order)
            pages = incomplete_pages(seen, total)
            if not pages:
                break
            info(f"Repair pass {k}/{args.repair_passes}: re-fetching {len(pages)} page(s): {ranges(pages)}")
            ok = run_pass(pages, f"repair {k}")

    except (PermissionError, KeyboardInterrupt) as e:
        by_order, dups = finalize_csv(args.out)
        reason = "Interrupted" if isinstance(e, KeyboardInterrupt) else str(e)
        warn(f"\nStopped: {reason}\nProgress is saved ({len(by_order)} rows in {args.out}). "
             f"Fix the problem and re-run to continue.")
        sys.exit(2)

    by_order, dups = finalize_csv(args.out)
    clean = integrity_report(by_order, dups + dups_total, state["total"], partial_run)
    print(f"Done. {len(by_order)} rows in {args.out}" + ("" if clean else "  (see issues above)"))
    if not clean and not partial_run:
        sys.exit(1)


if __name__ == "__main__":
    main()