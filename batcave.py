#!/usr/bin/env python3
"""Usage:
  python batcave.py search <query>
  python batcave.py <news_id> <user_hash> <page_url> <chapter_id> [chapter_id ...]

Downloads each chapter's pages and saves them as <Series Title>/<Chapter Name>.cbz
"""
import ast
import json
import re
import subprocess
import sys
import tempfile
import zipfile
from html import unescape
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

BASE = "https://batcave.biz"
URL = f"{BASE}/engine/ajax/controller.php?mod=api&action=bulkdl/getImages"
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:154.0) Gecko/20100101 Firefox/154.0"
BATCH = 10  # chapters per getImages request


def load_cookie() -> str:
    """Read cookie.txt (next to this script): a {'name': 'value'} dict or a raw Cookie header."""
    raw = (Path(__file__).resolve().parent / "cookie.txt").read_text().strip()
    if "{" in raw and "}" in raw:
        try:
            cookies = ast.literal_eval(raw[raw.index("{"): raw.rindex("}") + 1])
            return "; ".join(f"{k}={v}" for k, v in cookies.items())
        except (ValueError, SyntaxError):
            pass
    return re.sub(r"^Cookie:\s*", "", raw, flags=re.I)


def curl(url, headers, extra=()):
    cmd = ["curl", "-s", "--fail", "--compressed", *extra, url]
    for k, v in headers.items():
        cmd += ["-H", f"{k}: {v}"]
    return subprocess.run(cmd, capture_output=True, timeout=120, check=True).stdout


# ---------- site requests ----------

def fetch_html(url, referer=BASE + "/"):
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Cookie": load_cookie(),
    }
    return curl(url, headers).decode("utf-8", "replace")


def get_images(news_id, user_hash, chapter_ids, referer):
    headers = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
        "Origin": BASE,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Cookie": load_cookie(),
    }
    body = urlencode([("news_id", news_id), ("user_hash", user_hash)]
                     + [("chapter_ids[]", c) for c in chapter_ids])
    return json.loads(curl(URL, headers, ["-X", "POST", "--data-raw", body]))


def download_page(url, dest, referer):
    headers = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
        "Origin": BASE,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
    }
    dest.write_bytes(curl(url, headers, ["--retry", "3"]))


# ---------- search / page parsing ----------

def search(query):
    """Returns [(news_id, title, page_url), ...]"""
    html = fetch_html(f"{BASE}/search/{quote(query)}")
    pattern = r'<h2 class="readed__title"><a href="(https://batcave\.biz/(\d+)-[^"]+\.html)">(.*?)</a></h2>'
    return [(nid, unescape(title), url) for url, nid, title in re.findall(pattern, html)]


def get_user_hash(page_html):
    m = re.search(r"dle_login_hash\s*=\s*'([0-9a-f]+)'", page_html)
    return m.group(1) if m else None


def parse_page_data(page_html):
    """The comic page's window.__DATA__ object (chapters, bulkdl quota info, ...), or None."""
    m = re.search(r"window\.__DATA__\s*=\s*(\{.*?\});\s*</script>", page_html, re.S)
    try:
        return json.loads(m.group(1)) if m else None
    except ValueError:
        return None


def parse_selection(text, valid):
    """'1-5, 8' / 'all' -> sorted list of numbers present in `valid`."""
    text = text.strip().lower()
    if text in ("", "all"):
        return sorted(valid)
    chosen = set()
    for part in text.split(","):
        lo, _, hi = part.strip().partition("-")
        chosen |= {n for n in range(int(lo), int(hi or lo) + 1) if n in valid}
    return sorted(chosen)


# ---------- output ----------

def make_cbz(files, cbz_path, comic_info=None):
    with zipfile.ZipFile(cbz_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, f.name)
        if comic_info:
            z.writestr("ComicInfo.xml", comic_info)


def build_cbz(chapter, cbz, referer, comic_info=None):
    """Download a chapter's pages into `cbz`, written via a .part file so half-finished files never show up."""
    pages = chapter["pages"]
    width = max(3, len(str(len(pages))))
    cbz.parent.mkdir(parents=True, exist_ok=True)
    part = cbz.with_name(cbz.name + ".part")
    with tempfile.TemporaryDirectory() as tmp:
        files = []
        for n, url in enumerate(pages, 1):
            ext = Path(urlparse(url).path).suffix or ".jpg"
            dest = Path(tmp) / f"{n:0{width}d}{ext}"
            download_page(url, dest, referer)
            files.append(dest)
            print(f"\r{cbz.stem}: {n}/{len(pages)}", end="", flush=True)
        make_cbz(files, part, comic_info)
    part.replace(cbz)
    print(f"\nSaved {cbz}")


def series_title(page_url):
    """'https://batcave.biz/9267-green-lantern-by-geoff-johns.html' -> 'Green Lantern By Geoff Johns'"""
    slug = urlparse(page_url).path.rsplit("/", 1)[-1].removesuffix(".html")
    return re.sub(r"^\d+-", "", slug).replace("-", " ").title()


def safe_name(name):
    return re.sub(r'[\\/:*?"<>|]', "", name).strip()


def download_chapters(news_id, user_hash, chapter_ids, page_url, batch=BATCH):
    series_dir = Path(safe_name(series_title(page_url)))
    series_dir.mkdir(exist_ok=True)

    for i in range(0, len(chapter_ids), batch):
        result = get_images(news_id, user_hash, chapter_ids[i:i + batch], page_url)
        if not result.get("success"):
            sys.exit(f"Request failed: {result.get('error')}")
        print("Quota remaining:", result["data"]["quota"]["remaining"])

        for ch in result["data"]["chapters"]:
            build_cbz(ch, series_dir / f"{safe_name(ch['name'])}.cbz", page_url)


# ---------- interactive search flow ----------

def search_flow(query):
    results = search(query)
    if not results:
        sys.exit("No results.")
    for n, (_, title, _) in enumerate(results, 1):
        print(f"{n:>3}. {title}")
    try:
        news_id, title, page_url = results[int(input("Pick a comic: ")) - 1]
    except (ValueError, IndexError):
        sys.exit("Invalid choice.")

    page = fetch_html(page_url)
    user_hash = get_user_hash(page)
    data = parse_page_data(page)
    if not user_hash or not data or not data.get("chapters"):
        Path("comic_page_debug.html").write_text(page)
        sys.exit("Couldn't read the chapter list; saved the page to comic_page_debug.html")

    chapters = sorted(data["chapters"], key=lambda c: c["posi"])
    quota = data.get("bulkdl", {})
    print(f"\n{title} - {len(chapters)} chapters")
    if quota:
        print(f"Quota remaining: {quota['remaining']}/{quota['quota']}")
    for c in chapters:
        print(f"{c['posi']:>4}  {c['title']}")

    selected = parse_selection(input("Chapters (e.g. 1-5, 8 or all): "), {c["posi"] for c in chapters})
    ids = [c["id"] for c in chapters if c["posi"] in selected]
    if not ids:
        sys.exit("Nothing selected.")
    download_chapters(news_id, user_hash, ids, page_url, quota.get("batch", BATCH))


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) >= 2 and args[0] == "search":
        search_flow(" ".join(args[1:]))
    elif len(args) >= 4:
        news_id, user_hash, page_url, *chapters = args
        download_chapters(news_id, user_hash, chapters, page_url)
    else:
        sys.exit(__doc__)