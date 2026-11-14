"""
panel_perm_check.py — فقط خواندنی (هیچ کاربری ساخته/تغییر داده نمی‌شود).
نشان می‌دهد ادمینی که ربات با آن به پنلِ پاسارگاد/پاسارگارد وصل است چه دسترسی‌ای دارد
(برای خطای «403 Permission denied: users.create»).

اجرا (یوزرنیم رباتِ مشتری را بدونِ @ بگذار):
    cd /opt/botsaz && source venv/bin/activate
    python3 panel_perm_check.py USERNAME_RABAT
"""
import asyncio
import json
import sys

from sqlalchemy import select

from config import Session
from models import Bot, VpnPanel

_SECRET = ("password", "token", "secret", "hash", "api_key")


def _clean(o):
    if isinstance(o, dict):
        return {k: ("***" if any(w in k.lower() for w in _SECRET) else _clean(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean(x) for x in o[:30]]
    return o


def _summary(d):
    """خلاصهٔ یک‌خطیِ دسترسی‌هایِ کاربران (برایِ اینکه خروجی بریده نشود)."""
    if not isinstance(d, dict):
        return "؟ (پاسخِ نامعتبر)"
    role = d.get("role")
    if isinstance(role, dict):
        u = ((role.get("permissions") or {}).get("users")) or {}

        def f(k):
            v = u.get(k)
            return "✅" if v else ("❌" if v is False else "—")
        return (f"نقش={role.get('name')} | مالک={role.get('is_owner')} | "
                f"ساخت={f('create')} خواندن={f('read')} ویرایش={f('update')} حذف={f('delete')} "
                f"ریست‌حجم={f('reset_usage')}")
    if "is_sudo" in d:
        return f"پنلِ نسخهٔ قدیمی | is_sudo={d.get('is_sudo')} | is_disabled={d.get('is_disabled')}"
    return "؟ (قالبِ ناشناخته)"


async def main(uname):
    from tenant.shared.panel_clients.vpn_panel_factory import make_panel_client
    async with Session() as s:
        b = await s.scalar(select(Bot).where(Bot.username.ilike(uname.lstrip("@"))))
        if not b:
            print("رباتی با این یوزرنیم پیدا نشد."); return
        panels = list((await s.scalars(select(VpnPanel).where(VpnPanel.bot_id == b.id))).all())
        from models import MainButton
        for mb in (await s.scalars(select(MainButton).where(
                MainButton.bot_id == b.id, MainButton.action == "free_trial"))).all():
            try:
                cfg = json.loads(mb.config) if mb.config else {}
            except Exception:  # noqa: BLE001
                cfg = {}
            print(f"دکمهٔ تست «{mb.label}» (id {mb.id}): حالت={cfg.get('trial_mode')} "
                  f"پنل={cfg.get('trial_panel_keys') or cfg.get('trial_panel_key') or 'پیش‌فرض (اولین پنلِ فعال)'}")
    for p in panels:
        if "pasar" not in (p.panel_key or ""):
            continue
        host = p.base_url.split("//")[-1].split("/")[0]
        print(f"\n— {host}  (پنل {p.id}، نوع {p.panel_key}، فعال={p.enabled}، ادمینِ متصل: {p.username})")
        c = make_panel_client(p)
        try:
            for path in ("/api/admin", "/api/admins"):
                try:
                    d = await c._request("GET", path)
                    print(f"  GET {path} → OK")
                    if path == "/api/admin":
                        print("  ★ خلاصه:", _summary(d))
                except Exception as e:  # noqa: BLE001
                    print(f"  GET {path} → {type(e).__name__}: {str(e)[:200]}")
        finally:
            try:
                await c.close()
            except Exception:  # noqa: BLE001
                print("  (بستنِ کلاینت ناموفق؛ بی‌اهمیت)")
    if not panels:
        print("این ربات پنلی ندارد.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("استفاده: python3 panel_perm_check.py USERNAME_RABAT")
    else:
        asyncio.run(main(sys.argv[1]))
