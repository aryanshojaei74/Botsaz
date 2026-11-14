#!/usr/bin/env python3
"""
scripts/repair_misdirected_topups.py — انتقالِ شارژهایی که به حسابِ ساختگیِ خودِ ربات رفته‌اند.
(PATCH_PAYER_IDENTITY_V1 — جزئیات در tenant/gateways/topup_repair.py)

اجرا از ریشهٔ پروژه (همان venv و .env سرویس):

  ۱) فهرستِ شارژهایِ اشتباه (همهٔ ربات‌ها، یا یک ربات):
       venv/bin/python scripts/repair_misdirected_topups.py list [--bot ID]

  ۲) پیش‌نمایشِ انتقالِ یک تراکنش (هیچ‌چیز تغییر نمی‌کند):
       venv/bin/python scripts/repair_misdirected_topups.py move TXN_ID --to TELEGRAM_ID

  ۳) انجامِ واقعیِ انتقال (+ اطلاع‌رسانی به مشتری، اختیاری):
       venv/bin/python scripts/repair_misdirected_topups.py move TXN_ID --to TELEGRAM_ID --apply [--notify]

آیدیِ مشتری را از خودِ مشتری/پنلِ ادمین (جستجوی کاربر) یا از «کارتِ» فهرست و «شارژِ قبلیِ» او بگیرید.
"""
import argparse
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)


async def _list(bot_id):
    from config import Session
    from tenant.gateways.topup_repair import list_misdirected, find_phantoms
    async with Session() as s:
        phantoms = await find_phantoms(s, bot_id)
        if not phantoms:
            print("✅ هیچ حسابِ ساختگیِ ربات پیدا نشد؛ شارژِ اشتباهی وجود ندارد.")
            return
        for b, u in phantoms:
            print(f"\n🤖 ربات #{b.id} @{b.username} — حسابِ ساختگی: user_id={u.id} tg={u.telegram_id} "
                  f"نام=«{u.full_name}» موجودی={int(u.balance or 0):,}")
        items = await list_misdirected(s, bot_id)
    if not items:
        print("\n(تراکنشِ شارژی در این حساب‌ها نیست.)")
        return
    print("\nشارژهای جمع‌شده در حسابِ ساختگی:")
    for m in items:
        flag = "✔ منتقل‌شده" if m.already_moved else "⏳ منتظرِ انتقال"
        print(f"\n  txn #{m.txn_id} | ربات #{m.bot_id} | {int(m.amount):,} تومان | {m.kind} | {m.created_at} | {flag}")
        print(f"     توضیح: {m.note}")
        if m.candidates and not m.already_moved:
            for c in m.candidates:
                print(f"     ↳ صاحبِ احتمالی: tg={c['telegram_id']} «{c['name']}» @{c['username']} ({c['why']}؛ {c['n']} مورد)")
        elif not m.already_moved:
            print("     ↳ صاحبِ احتمالی پیدا نشد؛ از مشتری بپرسید یا با ساعت/مبلغ در پنل درگاه تطبیق دهید.")


async def _move(txn_id, to_tg, apply_, notify):
    from config import Session
    from tenant.gateways.topup_repair import move_credit, RepairError
    try:
        plan = await move_credit(Session, txn_id, to_tg, apply=apply_)
    except RepairError as e:
        print(f"❌ {e}")
        raise SystemExit(2)
    print(f"{'✅ انجام شد' if plan.applied else '🔎 پیش‌نمایش (چیزی تغییر نکرد)'}:")
    print(f"   {plan.amount:,} تومان  از حسابِ ساختگی ({int(plan.phantom_balance_before):,} → {int(plan.phantom_balance_after):,})")
    print(f"   به کاربر {plan.to_telegram_id} «{plan.to_name}» ({int(plan.target_balance_before):,} → {int(plan.target_balance_after):,})")
    if not plan.applied:
        print("\nبرایِ انجامِ واقعی همین دستور را با --apply تکرار کنید.")
        return
    if notify:
        from aiogram import Bot
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode
        from models import Bot as BotRow
        async with Session() as s:
            b = await s.get(BotRow, plan.bot_id)
        tb = Bot(token=b.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        try:
            await tb.send_message(
                plan.to_telegram_id,
                f"✅ شارژ <b>{plan.amount:,} تومانی</b> که پرداخت کرده بودی، به‌دلیلِ یک خطای فنی در حسابِ دیگری "
                f"ثبت شده بود و همین حالا به کیف پولت منتقل شد.\n💰 موجودیِ جدید: <b>{int(plan.target_balance_after):,} تومان</b>\n"
                "بابتِ این اتفاق پوزش می‌خواهیم 🙏")
            print("   📨 پیامِ اطلاع‌رسانی به مشتری رفت.")
        except Exception as e:  # noqa: BLE001
            print(f"   ⚠️ پیامِ اطلاع‌رسانی نرفت ({type(e).__name__}) — انتقال انجام شده است.")
        finally:
            await tb.session.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("list")
    a.add_argument("--bot", type=int, default=None)
    b = sub.add_parser("move")
    b.add_argument("txn_id", type=int)
    b.add_argument("--to", type=int, required=True, help="آیدیِ عددیِ تلگرامِ مشتری")
    b.add_argument("--apply", action="store_true", help="بدونِ این، فقط پیش‌نمایش")
    b.add_argument("--notify", action="store_true", help="بعد از انتقال به مشتری پیام بده")
    args = ap.parse_args()
    if args.cmd == "list":
        asyncio.run(_list(args.bot))
    else:
        asyncio.run(_move(args.txn_id, args.to, args.apply, args.notify))


if __name__ == "__main__":
    main()
