#!/usr/bin/env python3
"""
Keep the next N issues of the Marvel reading order downloaded and in order in Komga.

  1. Asks Komga which books you've read / started and finds your place in the CSV.
  2. Downloads the next N issues that aren't on the NAS yet (via batcave.py).
  3. Rescans the Komga library and rebuilds an ordered Read List with every issue on disk.

Komga settings (environment):
  KOMGA_URL=http://localhost:25600 (default)   KOMGA_LIBRARY_ID=...
  KOMGA_API_KEY=...            (or KOMGA_USER=... KOMGA_PASS=...)

Usage:
  python reading_order.py --dry-run          # show the plan, change nothing
  python reading_order.py                    # download the next 15 and update Komga
  python reading_order.py --count 10 --dest /mnt/NasComix/Marvel
"""
import argparse
import csv
import json
import os
import re
import sys
import time
from functools import lru_cache
from pathlib import Path
from xml.sax.saxutils import escape

import requests

import batcave

DEFAULT_CSV = "marvel_reading_order.csv"
DEFAULT_DEST = "/mnt/NasComix/Marvel"
READLIST_NAME = "Marvel Reading Order"


# ---------- the reading order ----------

def load_order(csv_path):
    """Unique issues in reading order. Split stories ([A Story], [B Story]) share one issue file,
    so each issue appears once, at the position of its first part."""
    entries, seen = [], set()
    with open(csv_path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            issue = re.sub(r"\s*\[[^\]]*\]\s*$", "", r["issue"]).strip()
            key = (r["series"], r["volume_year"], issue)
            if key in seen:
                continue
            seen.add(key)
            entries.append({"seq": len(entries) + 1, "order": int(r["order"]),
                            "series": r["series"], "year": r["volume_year"], "issue": issue})
    return entries


def volume_name(e):
    return batcave.safe_name(f"{e['series']} ({e['year']})")


def rel_path(e):
    """<Series (Year)>/<Series (Year)> #NNN.cbz  - one Komga series per volume."""
    vol = volume_name(e)
    num = e["issue"]
    if not num:
        return f"{vol}/{vol}.cbz"
    num = num.zfill(3) if num.isdigit() else num
    return f"{vol}/{batcave.safe_name(f'{vol} #{num}')}.cbz"


def tail(path):
    """Last two path components, lower-cased: lets Komga's path and the NAS mount path differ."""
    return "/".join(str(path).replace("\\", "/").split("/")[-2:]).lower()


def comic_info(e):
    parts = [f"<Series>{escape(e['series'])}</Series>", f"<Volume>{escape(e['year'])}</Volume>",
             "<Publisher>Marvel</Publisher>"]
    parts.insert(1, f"<Number>{escape(e['issue'] or '1')}</Number>")
    return '<?xml version="1.0" encoding="utf-8"?>\n<ComicInfo>' + "".join(parts) + "</ComicInfo>\n"


# ---------- Komga ----------

class Komga:
    def __init__(self, base, library_id, api_key=None, user=None, password=None):
        self.base, self.library_id = base.rstrip("/"), library_id
        self.s = requests.Session()
        if api_key:
            self.s.headers["X-API-Key"] = api_key
        elif user:
            self.s.auth = (user, password or "")

    def _call(self, method, path, **kw):
        r = self.s.request(method, f"{self.base}/api/v1{path}", timeout=60, **kw)
        r.raise_for_status()
        return r

    def books(self):
        r = self._call("GET", "/books", params={"library_id": self.library_id, "unpaged": "true"})
        return r.json()["content"]

    def scan(self):
        self._call("POST", f"/libraries/{self.library_id}/scan")

    def set_readlist(self, name, book_ids):
        lists = self._call("GET", "/readlists", params={"unpaged": "true"}).json()["content"]
        existing = next((l for l in lists if l["name"] == name), None)
        if existing:
            self._call("PATCH", f"/readlists/{existing['id']}", json={"bookIds": book_ids})
        else:
            self._call("POST", "/readlists", json={"name": name, "summary": "", "ordered": True,
                                                   "bookIds": book_ids})


def match_books(books, entries):
    """[(entry, komga_book)] for every Komga book that belongs to the reading order."""
    by_tail = {tail(rel_path(e)): e for e in entries}
    return [(by_tail[tail(b["url"])], b) for b in books if tail(b["url"]) in by_tail]


def current_seq(matched):
    """Position in the order: the furthest issue you've read or started (0 = not started)."""
    return max((e["seq"] for e, b in matched if b.get("readProgress")), default=0)


# ---------- batcave lookup ----------

def norm(s):
    return re.sub(r"[^a-z0-9]+", "", re.sub(r"^the\s+", "", s.lower()))


def is_annual(series):
    return bool(re.search(r"\bannual$", series, re.I))


def chapter_key(title):
    """'... Issue #645' -> '645'   '... _Annual 3' -> 'annual3'"""
    m = re.search(r"annual\s*#?\s*(\d+)\s*$", title, re.I)
    if m:
        return "annual" + (m.group(1).lstrip("0") or "0")
    m = re.search(r"#\s*([\w.\-]+)\s*$", title)
    return norm(m.group(1).lstrip("0") or "0") if m else norm(title.rsplit("Issue", 1)[-1])


def entry_keys(e):
    """Chapter keys an order entry may appear under (annuals are often '_Annual N' chapters)."""
    if not e["issue"]:
        return []
    n = norm(e["issue"].lstrip("0") or "0")
    return ["annual" + n, n] if is_annual(e["series"]) else [n]


def find_series(series, year, overrides):
    """(news_id, page_url) of the batcave page for 'Series (Year)', or None."""
    label = f"{series} ({year})"
    if label in overrides:
        url = overrides[label]
        return re.search(r"/(\d+)-", url).group(1), url
    for nid, title, url in batcave.search(f"{series} {year}"):
        if norm(title) == norm(label):
            return nid, url
    return None


@lru_cache(maxsize=None)
def load_series_page(nid, url):
    page = batcave.fetch_html(url)
    data = batcave.parse_page_data(page)
    return batcave.get_user_hash(page), (data or {}).get("chapters", [])


def find_annual_parent(series, year, issue):
    """Annuals usually live inside the parent series as '_Annual N' chapters. Look through the
    parent's volumes (those that started on or before the annual first) for one that has it."""
    parent = re.sub(r"\s*annual$", "", series, flags=re.I)
    cands = []
    for nid, title, url in batcave.search(parent):
        m = re.search(r"\((\d{4})", title)
        if m and norm(title).startswith(norm(parent)):
            cands.append((int(m.group(1)), nid, url))
    cands.sort(key=lambda c: (c[0] > int(year), abs(c[0] - int(year))))
    want = "annual" + (issue.lstrip("0") or "0")
    for _, nid, url in cands[:5]:
        time.sleep(1)
        _, chapters = load_series_page(nid, url)
        if any(chapter_key(c["title"]) == want for c in chapters):
            return nid, url
    return None


def download_entries(missing, dest, overrides, log):
    """Download the given entries (grouped per series so each series page is fetched once)."""
    groups = {}
    for e in missing:
        groups.setdefault((e["series"], e["year"]), []).append(e)

    for (series, year), items in groups.items():
        found = find_series(series, year, overrides)
        time.sleep(1)
        if not found and is_annual(series):
            found = find_annual_parent(series, year, items[0]["issue"])
            if found:
                print(f"  {series} ({year}): using {found[1]}")
        if not found:
            log(f"no batcave series for {series} ({year})")
            continue
        nid, url = found
        user_hash, chapters = load_series_page(nid, url)
        by_key = {}
        for c in chapters:
            by_key.setdefault(chapter_key(c["title"]), c)

        wanted = {}
        for e in items:
            c = next((by_key[k] for k in entry_keys(e) if k in by_key), None)
            if not c and len(chapters) == 1 and e["issue"] in ("", "1"):
                c = chapters[0]
            if c:
                wanted[c["id"]] = e
            else:
                log(f"no chapter for {series} ({year}) #{e['issue']} on {url}")
        ids = list(wanted)
        for i in range(0, len(ids), 10):
            result = batcave.get_images(nid, user_hash, ids[i:i + 10], url)
            if not result.get("success"):
                sys.exit(f"batcave refused the request: {result.get('error')}")
            print("Quota remaining:", result["data"]["quota"]["remaining"])
            for ch in result["data"]["chapters"]:
                e = wanted[ch["chapter_id"]]
                batcave.build_cbz(ch, dest / rel_path(e), url, comic_info(e))


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default=DEFAULT_CSV)
    ap.add_argument("--dest", default=DEFAULT_DEST, help="library folder on the NAS")
    ap.add_argument("--count", type=int, default=15, help="issues to keep ahead of your place")
    ap.add_argument("--overrides", default="overrides.json",
                    help='JSON mapping "Series (Year)" -> batcave page URL, for titles the search misses')
    ap.add_argument("--readlist", default=READLIST_NAME)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    entries = load_order(args.csv)
    dest = Path(args.dest)
    overrides = {}
    if Path(args.overrides).exists():
        try:
            overrides = json.loads(Path(args.overrides).read_text())
        except json.JSONDecodeError as e:
            sys.exit(f"{args.overrides} is not valid JSON ({e}). Expected one object: "
                     '{"Series (Year)": "https://batcave.biz/...html", ...} with commas between entries.')

    komga = Komga(os.environ.get("KOMGA_URL", "http://localhost:25600"), os.environ["KOMGA_LIBRARY_ID"],
                  os.environ.get("KOMGA_API_KEY"), os.environ.get("KOMGA_USER"), os.environ.get("KOMGA_PASS"))
    try:
        books = komga.books()
    except requests.ConnectionError:
        sys.exit(f"Can't reach Komga at {komga.base} - is it running?")
    matched = match_books(books, entries)
    cur = current_seq(matched)
    here = entries[cur - 1] if cur else None
    print(f"Current position: {here['seq']} / {len(entries)} - {here['series']} ({here['year']}) #{here['issue']}"
          if here else "Not started yet - beginning of the order")

    in_komga = {e["seq"] for e, _ in matched}
    window = entries[cur:cur + args.count]
    missing = [e for e in window if e["seq"] not in in_komga and not (dest / rel_path(e)).exists()]
    print(f"Next {len(window)} issues: {len(window) - len(missing)} already on disk, {len(missing)} to download")
    for e in missing:
        print(f"  #{e['seq']:>5}  {e['series']} ({e['year']}) #{e['issue']}")
    if args.dry_run or not missing:
        if not missing:
            print("Nothing to download.")
        return

    errors = Path("reading_order_errors.log")

    def log(msg):
        print("  !", msg)
        with errors.open("a") as f:
            f.write(msg + "\n")

    download_entries(missing, dest, overrides, log)

    print("Rescanning Komga...")
    komga.scan()
    for _ in range(18):  # wait up to ~90s for the new files to be indexed
        time.sleep(5)
        matched = match_books(komga.books(), entries)
        if {tail(b["url"]) for _, b in matched} >= {tail(rel_path(e)) for e in window if (dest / rel_path(e)).exists()}:
            break
    ordered = [b["id"] for _, b in sorted(matched, key=lambda m: m[0]["seq"])]
    komga.set_readlist(args.readlist, ordered)
    print(f"Read list '{args.readlist}' now has {len(ordered)} issues in order.")


if __name__ == "__main__":
    main()