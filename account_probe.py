"""
account_probe.py — فقط خواندنی. وضعیتِ واقعیِ اکانت‌هایِ یک کاربر را «از خودِ پنل» می‌خواند:
سقفِ حجم، مصرف، فعال/غیرفعال، انقضا — و با آنچه ربات ثبت کرده مقایسه می‌کند.
(برایِ گزارش‌هایی مثل «تستِ ۱۰۰ مگ تموم می‌شه ولی قطع نمی‌شه»)

اجرا:
    cd /opt/botsaz && source venv/bin/activate
    python3 account_probe.py 5285200964          # آیدی عددیِ تلگرامِ مشتری
    python3 account_probe.py 5285200964 5        # ۵ اکانتِ آخر (پیش‌فرض ۳)
    python3 account_probe.py 5285200964 5 --owner  # آیدی = «صاحبِ ربات»؛ ۵ تستِ رایگانِ آخرِ مشتری‌هایِ رباتِ او
"""
import asyncio
import sys

from sqlalchemy import select

from config import Session
from models import Bot, PanelAccount, VpnPanel

MB = 1024 * 1024


def _mb(v):
    try:
        return f"{int(v) / MB:.1f}MB"
    except Exception:  # noqa: BLE001
        return str(v)


async def main(tg_id, n, owner=False):
    from tenant.shared.panel_clients.vpn_panel_factory import make_panel_client
    async with Session() as s:
        if owner:
            ids = list((await s.scalars(select(Bot.id).where(Bot.owner_id == tg_id))).all())
            print("رباتِ(هایِ) این صاحب:", [(i) for i in ids] or "هیچ")
            accs = list((await s.scalars(select(PanelAccount).where(
                PanelAccount.bot_id.in_(ids), PanelAccount.plan_title == "تست رایگان")
                .order_by(PanelAccount.id.desc()).limit(n))).all()) if ids else []
        else:
            accs = list((await s.scalars(select(PanelAccount).where(
                PanelAccount.user_telegram_id == tg_id).order_by(PanelAccount.id.desc()).limit(n))).all())
        if not accs:
            print("اکانتی برایِ این کاربر پیدا نشد."); return
        for a in accs:
            b = await s.get(Bot, a.bot_id)
            panel = await s.scalar(select(VpnPanel).where(
                VpnPanel.bot_id == a.bot_id, VpnPanel.panel_key == a.panel_key))
            host = panel.base_url.split("//")[-1].split("/")[0] if panel else "؟"
            print(f"\n— اکانت {a.id} | ربات @{b.username if b else '?'} | {a.plan_title} | کد {a.svc_code}")
            print(f"   ربات ثبت کرده: حجم={a.volume_mb and str(a.volume_mb)+'MB' or str(a.volume_gb)+'GB'} "
                  f"روز={a.days} وضعیت={a.status} انقضا={a.expires_at} ایمیل={a.email}")
            print(f"   پنل: {a.panel_key} ({host})")
            if not panel:
                print("   ⚠️ پنلِ این اکانت دیگر در ربات نیست."); continue
            c = make_panel_client(panel)
            try:
                u = await asyncio.wait_for(c.get_usage(a.email), 40)
                if not u:
                    print("   ⚠️ اکانت روی پنل پیدا نشد (حذف شده؟)")
                else:
                    used = (u.get("up") or 0) + (u.get("down") or 0)
                    total = u.get("total")
                    print(f"   روی پنل: سقف={_mb(total) if total else 'نامحدود/۰'} | مصرف={_mb(used)} | "
                          f"فعال={u.get('enable')} | انقضا(ms)={u.get('expiryTime')}")
                    if total and used > total * 1.2:
                        print(f"   ❗ مصرف از سقف {used / total:.1f} برابر بیشتر شده و هنوز "
                              f"{'فعال است' if u.get('enable') else 'غیرفعال شده'}")
                    if not total:
                        print("   ❗ روی پنل سقفِ حجم ثبت نشده (۰ = نامحدود)")
            except Exception as e:  # noqa: BLE001
                print(f"   خطا: {type(e).__name__}: {str(e)[:200]}")
            finally:
                try:
                    await c.close()
                except Exception:  # noqa: BLE001
                    print("   (بستنِ کلاینت ناموفق؛ بی‌اهمیت)")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--owner"]
    if not args or not args[0].isdigit():
        print("استفاده: python3 account_probe.py TELEGRAM_ID [تعداد] [--owner]")
    else:
        asyncio.run(main(int(args[0]), int(args[1]) if len(args) > 1 else 3, "--owner" in sys.argv))
