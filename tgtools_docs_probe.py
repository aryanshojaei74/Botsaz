"""
tgtools_docs_probe.py — فقط خواندنی (GET صفحه‌هایِ عمومیِ مستندات؛ هیچ کلیدی فرستاده نمی‌شود، هیچ خریدی نمی‌شود).
صفحهٔ «Send a gift» (و در صورتِ نیاز هر صفحهٔ دیگر) را پیدا می‌کند و بخشِ «Response fields + نمونهٔ پاسخ» را چاپ می‌کند.

اجرا:
    cd /opt/botsaz && source venv/bin/activate
    python3 tgtools_docs_probe.py                      # صفحهٔ «Send a gift»
    python3 tgtools_docs_probe.py "NFT order status"   # هر عنوانِ دیگرِ منویِ مستندات
    python3 tgtools_docs_probe.py --list               # فهرستِ همهٔ لینک‌هایِ منو
"""
import argparse
import html as _html
import re
import sys
import urllib.request
from urllib.parse import urljoin

BASE = "https://tg-tools.shop"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "replace")


def to_text(h: str) -> str:
    h = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", h)
    h = re.sub(r"(?s)<[^>]+>", "\n", h)
    t = _html.unescape(h)
    return re.sub(r"\n\s*\n+", "\n", re.sub(r"[ \t]+", " ", t)).strip()


def links(page_html: str, base: str):
    out = []
    for m in re.finditer(r'(?is)<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page_html):
        href, label = m.group(1), to_text(m.group(2)).replace("\n", " ").strip()
        if label and ("api-docs" in href or href.startswith("#") or "docs" in href):
            out.append((label, urljoin(base, href)))
    return out


def response_block(text: str) -> str:
    i = text.find("Response fields")
    if i < 0:
        return ""
    j = text.find("Need integration help", i)
    return text[max(0, i - 600): (j if j > 0 else i + 3500)]


def bundle_search(root: str, html0: str, needles: list[str], window: int = 2600, max_files: int = 60) -> bool:
    """صفحه‌هایِ SPA دادهٔ هر endpoint را داخلِ فایل‌هایِ JavaScript دارند؛ همه را (فقط GET) می‌خوانیم و
    اطرافِ اولین رخدادِ هر عبارتِ needles را چاپ می‌کنیم (فقط متنِ مستندات؛ کلیدی در کار نیست)."""
    from urllib.parse import urlparse
    seen, queue, found = set(), [], False
    for m in re.finditer(r'(?is)<(?:script|link)\b[^>]*(?:src|href)="([^"]+\.m?js[^"]*)"', html0):
        queue.append(urljoin(root, m.group(1)))
    host = urlparse(root).netloc
    while queue and len(seen) < max_files:
        u = queue.pop(0)
        if u in seen or urlparse(u).netloc != host:
            continue
        seen.add(u)
        try:
            js = fetch(u)
        except Exception:  # noqa: BLE001
            continue
        for n in needles:
            i = js.find(n)
            if i >= 0:
                found = True
                print(f"\n— {u}  («{n}» @ {i})")
                print(js[max(0, i - 300): i + window])
                return True
        # فایل‌هایِ chunkِ دیگر که از همین فایل صدا زده می‌شوند
        for m in re.finditer(r'["\'](/?[\w./-]*[\w-]+\.m?js)["\']', js):
            queue.append(urljoin(u, m.group(1)))
    print(f"\n(فایل‌هایِ JavaScript بررسی‌شده: {len(seen)}؛ عبارتی از {needles} پیدا نشد)")
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("title", nargs="?", default="Send a gift")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    root = BASE + "/api-docs"
    html0 = fetch(root)
    ls = links(html0, root)
    if not ls and not a.list:
        print("منوی صفحه با JavaScript ساخته می‌شود؛ دادهٔ مستندات را از فایل‌هایِ JS می‌خوانم…")
        needles = {"send a gift": ["/api/Purchase/gift\"", "/api/purchase/gift\"", "Send a gift"],
                   "telegram gifts": ["/api/Purchase/gifts", "/api/purchase/gifts", "Telegram gifts"],
                   "nft order status": ["/api/marketplace/orders", "NFT order status"],
                   "buy nft": ["/api/marketplace/buy/auto", "Buy NFT"]}.get(a.title.strip().lower(), [a.title])
        if bundle_search(root, html0, needles):
            return
    if a.list or not ls:
        print(f"لینک‌هایِ پیداشده در {root}: {len(ls)}")
        for lab, href in ls:
            print(f"  {lab[:40]:<40} {href}")
        if not ls:
            print("هیچ لینکی پیدا نشد (صفحه احتمالاً با JavaScript ساخته می‌شود). متنِ صفحه:\n" + to_text(html0)[:1500])
        if a.list:
            return
    want = a.title.strip().lower()
    cand = [(lab, href) for lab, href in ls if want in lab.lower()]
    tried = []
    for lab, href in cand + [("guess", f"{root}/purchase/gift"), ("guess", f"{root}/purchase/send-a-gift")]:
        if href in tried:
            continue
        tried.append(href)
        try:
            t = to_text(fetch(href.split("#")[0]))
        except Exception as e:  # noqa: BLE001
            print(f"✗ {href}  ({type(e).__name__})")
            continue
        blk = response_block(t)
        print(f"\n— {lab}: {href}")
        if blk and ("gift" in t.lower()):
            print(blk)
            return
        print("  (بخشِ پاسخِ این صفحه پیدا نشد)" + (f"\n  عنوانِ صفحه: {t[:120]!r}" if t else ""))
    print("\nپیدا نشد. خروجیِ `--list` را بفرست یا صفحهٔ «Send a gift» را از مرورگر ذخیره کن و بفرست.")


if __name__ == "__main__":
    main()
