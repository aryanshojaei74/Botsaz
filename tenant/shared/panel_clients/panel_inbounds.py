"""
panel_inbounds.py — انتخاب و همگام‌سازی اینباندها/گروه‌ها/سرویس‌های پنل
(=== PATCH_PANEL_INBOUND_PICKER_V1 ===).

تیکت MidnightSoul: «پنل ۴ اینباند دارد؛ فقط ترکیه را خواستم ولی بروزرسانی خودکار
(هر ۲ ساعت) هر بار هر ۴ تا را برمی‌گرداند» و «موقع اتصال اولیه چرا لیست اینباندها
را نمی‌آورد که هرکدام را خواستیم تیک بزنیم؟».

ریشه: VpnPanel.inbound_ids هم «انتخاب مالک» بود هم «کپی لیست پنل»؛ همگام‌سازی
خودکار و دکمهٔ «بروزرسانی اینباندها» همیشه آن را با همهٔ اینباندهای فعال پنل
بازنویسی می‌کردند. حالا ستون VpnPanel.inbound_mode فرق می‌گذارد:
  • "all"    = همهٔ اینباندهای فعال؛ با پنل هماهنگ می‌ماند (رفتار قبلی)
  • "custom" = انتخاب دستی مالک؛ هیچ همگام‌سازی‌ای دست نمی‌زند
  • None     = ردیف قدیمی؛ از روی محتوا حدس زده می‌شود (resolve_mode)

این ماژول فقط منطق خالص است (بدون دیتابیس/تلگرام) تا هم هندلرها و هم حلقهٔ
بروزرسانی خودکار از یک قاعدهٔ واحد استفاده کنند.
"""
from __future__ import annotations

from dataclasses import dataclass

MODE_ALL = "all"
MODE_CUSTOM = "custom"

# پنل‌هایی که لیست اینباند/گروه/سرویس قابل‌انتخاب دارند. هیدیفای و شاهان اصلا
# چنین مفهومی ندارند؛ ایلان «اینباند» را پروتکل می‌داند و همان مرحلهٔ دستی را دارد.
PICKABLE = frozenset({"sanai", "alireza", "heimdall", "degeris", "marzban", "rebecca",
                      "upanel_mizban", "pasargad", "pasarguard", "marzneshin", "nodex"})
# شناسهٔ اینباند در این پنل‌ها رشتهٔ «tag» است؛ در بقیه عدد.
_TAG_IDS = frozenset({"marzban", "rebecca", "upanel_mizban"})
_NO_PROTOCOL = ("", "unknown", "group")


def _id_str(base_key: str, raw) -> str | None:
    if raw is None or str(raw).strip() == "":
        return None
    if base_key in _TAG_IDS:
        return str(raw)
    try:
        return str(int(raw))
    except (TypeError, ValueError):
        return str(raw).strip()


def inbound_label(base_key: str, iid: str, ib: dict) -> str:
    """نام قابل‌نمایش یک اینباند: «#3 ترکیه · vless:443» یا برای پنل‌های تگ‌محور «VLESS TCP · vless»."""
    name = str(ib.get("remark") or ib.get("tag") or ib.get("name") or "").strip()
    proto = str(ib.get("protocol") or "").strip()
    if proto.lower() in _NO_PROTOCOL:
        proto = ""
    port = ib.get("port")
    tech = proto + (f":{port}" if proto and port else "")
    if base_key in _TAG_IDS:
        head = name or iid
    else:
        head = f"#{iid} {name}" if name and name != iid else f"#{iid}"
    return head + (f" · {tech}" if tech else "")


def inbound_items(base_key: str, inbounds) -> list[dict]:
    """خروجی خام list_inbounds() را به [{id, label, enabled}] تبدیل می‌کند (یکتا، به ترتیب پنل)."""
    out, seen = [], set()
    for ib in inbounds or []:
        if not isinstance(ib, dict):
            continue
        iid = _id_str(base_key, ib.get("id"))
        if iid is None or iid in seen:
            continue
        seen.add(iid)
        out.append({"id": iid, "label": inbound_label(base_key, iid, ib),
                    "enabled": bool(ib.get("enable", True))})
    return out


def active_ids(base_key: str, inbounds) -> list[str]:
    """شناسهٔ اینباندهای فعال پنل (رشته‌ای، یکتا، به ترتیب پنل)."""
    return [it["id"] for it in inbound_items(base_key, inbounds) if it["enabled"]]


def parse_ids(raw) -> list[str]:
    """«1, 2،3» (یا یک لیست) → ['1','2','3'] (یکتا، به ترتیب)."""
    if isinstance(raw, (list, tuple, set)):
        parts = [str(x) for x in raw]
    else:
        parts = str(raw or "").replace("،", ",").split(",")
    return list(dict.fromkeys(p.strip() for p in parts if p.strip()))


def join_ids(ids) -> str:
    return ",".join(str(i) for i in ids)


def mode_for_choice(chosen, active) -> str:
    """وقتی مالک دستی تیک می‌زند: اگر همهٔ اینباندهای فعال را تیک زده «all»، وگرنه «custom»."""
    act = {str(a) for a in active or []}
    return MODE_ALL if act and {str(c) for c in chosen} == act else MODE_CUSTOM


def resolve_mode(mode, stored, active) -> str:
    """حالت مؤثر. ردیف قدیمی (mode=None): لیست خالی یا شامل همهٔ فعال‌ها = همه؛ وگرنه انتخاب دستی.

    این حدس عمدا محتاط است: اگر اشتباه کند، بدترین نتیجه این است که اینباند تازهٔ پنل
    خودکار اضافه نشود، نه اینکه انتخاب مالک خراب شود."""
    cur, act = parse_ids(stored), {str(a) for a in active or []}
    if mode == MODE_CUSTOM:
        return MODE_CUSTOM if cur else MODE_ALL
    if mode == MODE_ALL:
        return MODE_ALL
    return MODE_ALL if (not cur or act <= set(cur)) else MODE_CUSTOM


@dataclass(frozen=True)
class SyncPlan:
    ids: str                            # مقدار نهایی VpnPanel.inbound_ids
    mode: str | None                    # مقدار نهایی VpnPanel.inbound_mode
    changed: bool                       # ids یا mode نسبت به قبل فرق دارد؟
    missing: tuple = ()                 # (custom) انتخاب‌هایی که در پنل نیستند یا غیرفعال‌اند
    fresh: tuple = ()                   # (custom) اینباندهای فعال پنل که انتخاب نشده‌اند


def plan_inbound_sync(mode, stored, active) -> SyncPlan:
    """قاعدهٔ واحد بروزرسانی خودکار و دکمهٔ دستی «بروزرسانی اینباندها»."""
    cur = parse_ids(stored)
    cur_s = join_ids(cur)
    act = list(dict.fromkeys(str(a) for a in active or []))
    if not act:                          # پنل چیزی برنگرداند: هیچ‌چیز را عوض نکن
        return SyncPlan(cur_s, mode, False)
    eff = resolve_mode(mode, cur_s, act)
    if eff == MODE_ALL:
        ids = join_ids(act)
        return SyncPlan(ids, MODE_ALL, ids != cur_s or mode != MODE_ALL)
    act_set, cur_set = set(act), set(cur)
    return SyncPlan(cur_s, MODE_CUSTOM, mode != MODE_CUSTOM,
                    tuple(x for x in cur if x not in act_set),
                    tuple(x for x in act if x not in cur_set))


def describe(mode, stored) -> str:
    """یک خط برای منوی پنل."""
    ids = join_ids(parse_ids(stored))
    if not ids:
        return "همهٔ فعال‌ها"
    if mode == MODE_CUSTOM:
        return f"{ids} (انتخاب دستی؛ ثابت)"
    if mode == MODE_ALL:
        return f"{ids} (همه؛ با پنل هماهنگ)"
    return ids


def with_ghosts(items: list[dict], stored) -> list[dict]:
    """انتخاب‌های قبلی که دیگر در پنل نیستند/غیرفعال‌اند را با هشدار به آخر لیست اضافه می‌کند
    تا مالک ببیند و بتواند تیکشان را بردارد."""
    have = {it["id"] for it in items}
    ghosts = [{"id": g, "label": f"⚠️ {g} (در پنل نیست یا غیرفعال است)", "enabled": False}
              for g in parse_ids(stored) if g not in have]
    return items + ghosts


def picker_state(base_key: str, inbounds, mode=None, stored=None, existing: bool = False):
    """(items, تیک‌های اولیه به‌صورت اندیس، تعداد غیرفعال‌های پنهان‌شده) برای صفحهٔ انتخاب.

    فقط اینباندهای فعال نشان داده می‌شوند (روی اینباند غیرفعال کانفیگ ساختن بی‌فایده است).
    پنل تازه: هیچ‌چیز تیک‌خورده نیست (مگر اینکه فقط یک گزینه باشد). پنل موجود: انتخاب فعلی تیک‌خورده است؛ در حالت
    «همه» همهٔ فعال‌ها؛ در حالت «انتخابی» همان‌ها (به‌علاوهٔ موارد گم‌شده با هشدار)."""
    every = inbound_items(base_key, inbounds)
    items = [it for it in every if it["enabled"]]
    hidden = len(every) - len(items)
    if not existing:
        return items, ([0] if len(items) == 1 else []), hidden      # تنها گزینه از قبل تیک‌خورده است
    cur = parse_ids(stored)
    if not cur or resolve_mode(mode, cur, [it["id"] for it in items]) == MODE_ALL:
        return items, list(range(len(items))), hidden
    items = with_ghosts(items, cur)
    cur_set = set(cur)
    return items, [i for i, it in enumerate(items) if it["id"] in cur_set], hidden
