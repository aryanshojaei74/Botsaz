"""
perf_summary.py — خلاصهٔ سلامتِ سرعت از روی لاگ (فقط خواندنی). هر وقت «ربات کند شد» یک‌بار اجرا کن:

    cd /opt/botsaz && source venv/bin/activate
    python3 perf_summary.py                 # ۶۰ دقیقهٔ اخیر
    python3 perf_summary.py --since "-3 hours"
    journalctl -u 'botsaz-tenant*' --since "-30 min" --no-pager | python3 perf_summary.py -     # از stdin

می‌گوید: چند آپدیتِ کند داشتیم و در هر ۱۰ دقیقه چندتا، کدام دکمه‌ها، علتِ اصلیِ هر کدام (تلگرام/دیتابیس/پنل/انتظارِ
عمدی/نامشخص)، کدام پنل‌ها کندند، گیرِ لوپ و retryِ تلگرام.
"""
import argparse
import collections
import re
import subprocess
import sys

SLOW = re.compile(r"slow update \(token …(\w+), kind=(\w+), ([0-9.]+)s\) → (.*?)(?: \| (.*))?$")
CAT = re.compile(r"(tg|db|http|wait) (\d+)×([0-9.]+)s")
HOST = re.compile(r"پنلِ کند: (\w+) (\S+) \| ([0-9.]+)s \| (\S+)")
TIME = re.compile(r"(?:^|\s)(\d{2}):(\d{2}):\d{2}\b")
THR = re.compile(r"انتظارِ ([\w-]+): ([0-9.]+)s \| (\S+)")
BLK = re.compile(r"event loop blocked ([0-9.]+)s")


# بازی‌هایِ تاسِ گروه عمداً ~۴ثانیه منتظرِ پایانِ انیمیشنِ تلگرام می‌مانند؛ کندیِ واقعی نیستند
DICE_WORDS = {"فوتبال", "اسلات", "بسکتبال", "بولینگ", "دارت", "تاس", "🎲", "🎯", "🏀", "⚽", "🎳", "🎰"}


def _handler(what: str) -> str:
    w = what.strip()
    return re.sub(r":\d+", ":N", w)[:34]


def main(lines):
    slow = []
    kinds = collections.defaultdict(list)
    dice = []
    hcat = collections.defaultdict(lambda: {"n": 0, "el": 0.0, "tg": 0.0, "db": 0.0, "http": 0.0, "wait": 0.0, "dbq": 0, "tgn": 0, "httpn": 0})
    buckets = collections.Counter()
    hosts = collections.defaultdict(lambda: [0, 0.0])
    waits = collections.defaultdict(lambda: [0, 0.0])
    blocked, retries, stack_top = [], 0, collections.Counter()
    in_block = False
    for ln in lines:
        m = SLOW.search(ln)
        if m:
            tok, kind, el, what, rest = m.group(1), m.group(2), float(m.group(3)), m.group(4), m.group(5) or ""
            cats = {k: float(t) for k, _n, t in CAT.findall(rest)}
            if what.strip() in DICE_WORDS:
                dice.append(el)
                continue
            slow.append((el, _handler(what), cats, tok))
            kinds[kind].append(el)
            _h = hcat[_handler(what)]
            _h["n"] += 1
            _h["el"] += el
            for _k, _n, _t in CAT.findall(rest):
                _h[_k] += float(_t)
                if _k == "db":
                    _h["dbq"] += int(_n)
                elif _k == "tg":
                    _h["tgn"] += int(_n)
                elif _k == "http":
                    _h["httpn"] += int(_n)
            t = TIME.search(ln)
            if t:
                buckets[f"{t.group(1)}:{t.group(2)[0]}0"] += 1
        m = HOST.search(ln)
        if m:
            h = hosts[m.group(4).replace("https://", "").replace("http://", "")]
            h[0] += 1
            h[1] = max(h[1], float(m.group(3)))
        m = THR.search(ln)
        if m:
            w = waits[f"{m.group(1)} {m.group(3)}"]
            w[0] += 1
            w[1] = max(w[1], float(m.group(2)))
        m = BLK.search(ln)
        if m:
            blocked.append(float(m.group(1)))
            in_block = True
            continue
        if in_block:
            body = re.sub(r"^.*?\[\d+\]: ", "", ln).strip()      # پیشوندِ journald را بردار
            if body.startswith("File "):
                stack_top[body[:110]] += 1
            elif not body.startswith(("return", "res", "await", "async", "b =", "self", "with", "data")) and body:
                in_block = False
        if "[retry]" in ln and "فعال" not in ln:
            retries += 1

    print(f"آپدیتِ کند (>۲ث): {len(slow)}   | گیرِ لوپ: {len(blocked)}   | retryِ تلگرام: {retries}")
    if dice:
        print(f"(+ {len(dice)} بازیِ تاسِ گروه با ~{sum(dice) / len(dice):.1f}ث که طبیعی است و در آمار نیامده)")
    if buckets:
        print("\nدر هر ۱۰ دقیقه:  " + "  ".join(f"{k}→{v}" for k, v in sorted(buckets.items())[-12:]))
    if kinds:
        print("\nبه تفکیکِ نوعِ آپدیت: " + "   ".join(
            f"{k}: {len(v)}× (avg {sum(v) / len(v):.1f}s، max {max(v):.1f}s)" for k, v in sorted(kinds.items())))
        msg = [x for x in slow if x[1].startswith("/start")]
        if msg:
            v = [x[0] for x in msg]
            print(f"   /start: {len(v)}× کند (avg {sum(v) / len(v):.1f}s، max {max(v):.1f}s)")
    if slow:
        by = collections.defaultdict(list)
        for el, h, _c, _t in slow:
            by[h].append(el)
        print("\nکندترین دکمه‌ها (تعداد، میانگین، بیشینه):")
        for h, v in sorted(by.items(), key=lambda kv: -sum(kv[1]))[:10]:
            print(f"   {h:<34} {len(v):>3}×  avg {sum(v) / len(v):5.1f}s  max {max(v):5.1f}s")
        print("\nمیانگینِ هر دکمه به تفکیکِ علت (ثانیه؛ «؟» = زمانی که هیچ شمارنده‌ای توضیح نداد):")
        for h, v in sorted(by.items(), key=lambda kv: -sum(kv[1]))[:10]:
            c = hcat[h]
            n = max(1, c["n"])
            un = max(0.0, c["el"] - c["tg"] - c["db"] - c["http"] - c["wait"]) / n
            print(f"   {h:<34} avg {c['el'] / n:5.1f}s = tg {c['tg'] / n:4.1f} ({c['tgn'] / n:.0f} تماس) | db {c['db'] / n:4.1f} ({c['dbq'] / n:.0f} کوئری) "
                  f"| http {c['http'] / n:4.1f} ({c['httpn'] / n:.0f}) | wait {c['wait'] / n:4.1f} | ؟ {un:4.1f}")
        cause = collections.Counter()
        for el, _h, c, _t in slow:
            if not c:
                cause["نامشخص (بدونِ شمارنده؛ نسخهٔ قدیمی)"] += 1
                continue
            top = max(c, key=c.get)
            if sum(c.values()) < 0.5 * el:
                cause["نامشخص (کمتر از نیمِ زمان توضیح داده شد)"] += 1
            else:
                cause = cause
                cause[{"tg": "تلگرام", "db": "دیتابیس", "http": "پنل/سرویسِ بیرونی", "wait": "انتظارِ عمدیِ کد"}[top]] += 1
        print("\nعلتِ اصلیِ آپدیت‌هایِ کند:")
        for k, v in cause.most_common():
            print(f"   {v:>3}  {k}")
    if hosts:
        print("\nپنل‌هایِ کند (تعدادِ هشدار، بیشینه):")
        for h, (n, mx) in sorted(hosts.items(), key=lambda kv: -kv[1][1])[:10]:
            print(f"   {h:<40} {n:>3}×  max {mx:5.1f}s")
    if waits:
        print("\nانتظارِ عمدی (محدودکنندهٔ لاگین/قفل):")
        for k, (n, mx) in sorted(waits.items(), key=lambda kv: -kv[1][1])[:8]:
            print(f"   {k:<44} {n:>3}×  max {mx:5.1f}s")
    if blocked:
        print(f"\nگیرِ لوپ: {len(blocked)} بار، بیشینه {max(blocked):.1f}s؛ بالاترین خطوطِ stack:")
        for k, n in stack_top.most_common(5):
            print(f"   {n:>3}× {k}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("src", nargs="?", default="")
    ap.add_argument("--since", default="-60 min")
    a = ap.parse_args()
    if a.src == "-":
        main(sys.stdin)
    else:
        out = subprocess.run(
            "journalctl -u 'botsaz-tenant*' --since '%s' --no-pager" % a.since,
            shell=True, capture_output=True, text=True).stdout
        main(out.splitlines())
