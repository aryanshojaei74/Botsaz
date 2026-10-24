"""
restore_one_bot.py
-------------------
بازگردانیِ داده‌های ربات‌های مستأجر از یک بک‌آپِ قدیمیِ botsaz.db (SQLite)
به دیتابیسِ فعلی (Postgres یا SQLite؛ همان DATABASE_URL موجود در .env).

USAGE (یک ربات خاص):
    python3 restore_one_bot.py --backup /path/to/old_botsaz.db --username ROBOT_USERNAME
    یا
    python3 restore_one_bot.py --backup /path/to/old_botsaz.db --bot-id 42

USAGE (همهٔ ربات‌ها):
    python3 restore_one_bot.py --backup /path/to/old_botsaz.db --all

با --dry-run فقط گزارش می‌دهد چند ردیف پیدا شده، بدون هیچ تغییری در مقصد
(توصیه می‌شود اول با --dry-run اجرا کنید، مخصوصاً برای --all).

⚠️ روش کار: برای هر ردیفِ موجود در بک‌آپ یک UPSERT انجام می‌شود (اگر همان id در
مقصد موجود باشد به‌روزرسانی، وگرنه درج می‌شود). هیچ ردیفی حذف نمی‌شود — بنابراین
هیچ خطای Foreign Key‌ای رخ نمی‌دهد و هر دادهٔ جدیدی که بعد از بک‌آپ اضافه شده
دست‌نخورده می‌ماند. در حالت --all، خطای یک ربات باعثِ توقفِ کل عملیات نمی‌شود؛
در گزارشِ پایانی لیست می‌شود تا جداگانه بررسی شود. جدول خودِ «bots» دست‌نخورده
می‌ماند مگر با --include-bot-row صریحاً بخواهید. قبل از اجرا از دیتابیسِ مقصد
بک‌آپ بگیرید.
"""
import argparse
import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from config import Session as DestSession, engine as dest_engine
from models import Bot as BotRow, MainButton, ButtonSlide, TenantTicket, TenantTicketReply, TENANT_OWNED_MODELS

from logging_setup import get_logger as _get_logger, PrintLogger as _PrintLogger
logger = _get_logger(__name__)
print = _PrintLogger(__name__)  # هدایتِ print()های موجود در این فایل به logging

# ButtonSlide/TenantTicketReply ندارند bot_id مستقیم؛ از طریق جدول والد مرتبط می‌شوند.
_DIRECT_MODELS = [M for M in TENANT_OWNED_MODELS if "bot_id" in M.__table__.columns]

_DIALECT = dest_engine.dialect.name  # 'postgresql' یا 'sqlite'
if _DIALECT == "postgresql":
    from sqlalchemy.dialects.postgresql import insert as _dialect_insert
else:
    from sqlalchemy.dialects.sqlite import insert as _dialect_insert


async def _upsert_rows(dst_session, M, rows) -> tuple[int, int]:
    """هر ردیف را با UPSERT (بر اساس primary key) در مقصد می‌نویسد. حذفی انجام
    نمی‌شود، پس هیچ برخوردی با Foreign Key پیش نمی‌آید.
    هر ردیف در یک SAVEPOINT جدا اجرا می‌شود؛ اگر یک ردیف خطا داد (مثلاً داده‌ی
    خراب یا برخورد با یک unique constraint دیگر غیر از id)، فقط همان ردیف رد
    می‌شود و بقیه‌ی ردیف‌ها ادامه پیدا می‌کنند. خروجی: (تعداد موفق, تعداد ردشده)."""
    if not rows:
        return 0, 0
    pk_cols = [c.name for c in M.__table__.primary_key.columns]
    ok = 0
    skipped = 0
    for row in rows:
        data = {c.name: getattr(row, c.name) for c in M.__table__.columns}
        update_cols = {k: v for k, v in data.items() if k not in pk_cols}
        stmt = _dialect_insert(M).values(**data)
        if update_cols:
            stmt = stmt.on_conflict_do_update(index_elements=pk_cols, set_=update_cols)
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=pk_cols)
        try:
            async with dst_session.begin_nested():
                await dst_session.execute(stmt)
            ok += 1
        except Exception as e:
            skipped += 1
            pk_val = data.get(pk_cols[0]) if pk_cols else "?"
            print(f"      ⚠️  رد شد یک ردیف از {M.__name__} (id={pk_val}): "
                  f"{type(e).__name__} — {str(e).splitlines()[0][:120]}")
    await dst_session.commit()
    return ok, skipped


async def _find_bot(old_session_factory, username: str | None, bot_id: int | None) -> tuple[int, str] | None:
    async with old_session_factory() as s:
        if bot_id is not None:
            row = await s.get(BotRow, bot_id)
        else:
            row = await s.scalar(select(BotRow).where(BotRow.username == username))
        if not row:
            return None
        return row.id, row.username


async def _list_all_bots(old_session_factory) -> list[tuple[int, str]]:
    async with old_session_factory() as s:
        rows = (await s.scalars(select(BotRow))).all()
    return [(r.id, r.username) for r in rows]


async def restore_one(OldSession, target_bot_id: int, uname: str,
                       dry_run: bool, include_bot_row: bool) -> int:
    """یک ربات را بازگردانی می‌کند؛ (تعداد کل ردیف‌های پیدا/بازگردانی‌شده، تعداد ردشده) را برمی‌گرداند."""
    grand_total = 0
    grand_skipped = 0

    for M in _DIRECT_MODELS:
        name = M.__name__
        try:
            async with OldSession() as s:
                rows = (await s.scalars(select(M).where(M.bot_id == target_bot_id))).all()
        except Exception as e:
            print(f"   ⚠️  رد شد {name}: خطا در خواندن از بک‌آپ ({e})")
            continue
        if not rows:
            continue
        grand_total += len(rows)
        if dry_run:
            print(f"   📦 {name}: {len(rows)} ردیف")
            continue
        async with DestSession() as dst:
            ok, skipped = await _upsert_rows(dst, M, rows)
        grand_skipped += skipped
        tail = f" (⚠️ {skipped} ردیف رد شد)" if skipped else ""
        print(f"   📦 {name}: {ok}/{len(rows)} ردیف — بازگردانی شد (upsert).{tail}")

    # ── ButtonSlide: از طریقِ button_id → MainButton.bot_id ──
    async with OldSession() as s:
        old_btn_ids = (await s.scalars(
            select(MainButton.id).where(MainButton.bot_id == target_bot_id))).all()
    if old_btn_ids:
        async with OldSession() as s:
            slides = (await s.scalars(
                select(ButtonSlide).where(ButtonSlide.button_id.in_(old_btn_ids)))).all()
        if slides:
            grand_total += len(slides)
            if not dry_run:
                async with DestSession() as dst:
                    ok, skipped = await _upsert_rows(dst, ButtonSlide, slides)
                grand_skipped += skipped
                tail = f" (⚠️ {skipped} ردیف رد شد)" if skipped else ""
                print(f"   📦 ButtonSlide: {ok}/{len(slides)} ردیف — بازگردانی شد (upsert).{tail}")
            else:
                print(f"   📦 ButtonSlide: {len(slides)} ردیف")

    # ── TenantTicketReply: از طریقِ ticket_id → TenantTicket.bot_id ──
    async with OldSession() as s:
        old_ticket_ids = (await s.scalars(
            select(TenantTicket.id).where(TenantTicket.bot_id == target_bot_id))).all()
    if old_ticket_ids:
        async with OldSession() as s:
            replies = (await s.scalars(
                select(TenantTicketReply).where(TenantTicketReply.ticket_id.in_(old_ticket_ids)))).all()
        if replies:
            grand_total += len(replies)
            if not dry_run:
                async with DestSession() as dst:
                    ok, skipped = await _upsert_rows(dst, TenantTicketReply, replies)
                grand_skipped += skipped
                tail = f" (⚠️ {skipped} ردیف رد شد)" if skipped else ""
                print(f"   📦 TenantTicketReply: {ok}/{len(replies)} ردیف — بازگردانی شد (upsert).{tail}")
            else:
                print(f"   📦 TenantTicketReply: {len(replies)} ردیف")

    if include_bot_row and not dry_run:
        async with OldSession() as s:
            old_bot = await s.get(BotRow, target_bot_id)
        if old_bot:
            async with DestSession() as dst:
                await _upsert_rows(dst, BotRow, [old_bot])
            print("   📦 ردیفِ اصلیِ ربات (bots) هم بازنویسی شد.")

    return grand_total, grand_skipped


async def restore(backup_path: str, username: str | None, bot_id: int | None,
                   dry_run: bool, include_bot_row: bool) -> None:
    old_engine = create_async_engine(f"sqlite+aiosqlite:///{backup_path}")
    OldSession = async_sessionmaker(old_engine, expire_on_commit=False)

    found = await _find_bot(OldSession, username, bot_id)
    if not found:
        print("❌ رباتی با این مشخصات در فایل بک‌آپ پیدا نشد.")
        await old_engine.dispose()
        return
    target_bot_id, uname = found
    print(f"✅ ربات پیدا شد: @{uname}  (bot_id={target_bot_id})\n")

    grand_total, grand_skipped = await restore_one(OldSession, target_bot_id, uname, dry_run, include_bot_row)

    await old_engine.dispose()
    if dry_run:
        print(f"\n🔎 حالت dry-run — مجموعاً {grand_total} ردیف در بک‌آپ پیدا شد؛ "
              f"چیزی در مقصد تغییر نکرد.")
    else:
        skip_txt = f" ({grand_skipped} ردیف به‌دلیل داده‌ی خراب/تکراری رد شد)" if grand_skipped else ""
        print(f"\n✅ تمام شد — مجموعاً {grand_total} ردیف بازگردانی شد (upsert، بدون حذف).{skip_txt}")


async def restore_all(backup_path: str, dry_run: bool, include_bot_row: bool) -> None:
    old_engine = create_async_engine(f"sqlite+aiosqlite:///{backup_path}")
    OldSession = async_sessionmaker(old_engine, expire_on_commit=False)

    bots = await _list_all_bots(OldSession)
    if not bots:
        print("❌ هیچ رباتی در فایل بک‌آپ پیدا نشد.")
        await old_engine.dispose()
        return
    print(f"🔎 {len(bots)} ربات در بک‌آپ پیدا شد.\n")

    ok_bots: list[str] = []
    failed_bots: list[tuple[str, str]] = []
    bots_with_skips: list[tuple[str, int]] = []
    grand_total = 0
    grand_skipped = 0

    for i, (bid, uname) in enumerate(bots, start=1):
        print(f"[{i}/{len(bots)}] @{uname} (bot_id={bid})")
        try:
            n, skipped = await restore_one(OldSession, bid, uname, dry_run, include_bot_row)
            grand_total += n
            grand_skipped += skipped
            if skipped:
                bots_with_skips.append((uname, skipped))
            ok_bots.append(uname)
        except Exception as e:
            failed_bots.append((uname, str(e)[:200]))
            print(f"   ❌ این ربات با خطا متوقف شد — رد شد و رفتیم سراغ بعدی: {e}")
        print()

    await old_engine.dispose()

    print("=" * 50)
    if dry_run:
        print(f"🔎 dry-run تمام شد — مجموعاً {grand_total} ردیف در {len(ok_bots)} ربات پیدا شد.")
    else:
        skip_txt = f" ({grand_skipped} ردیف در کل به‌دلیل داده‌ی خراب/تکراری رد شد)" if grand_skipped else ""
        print(f"✅ تمام شد — {len(ok_bots)} ربات با موفقیت بازگردانی شد "
              f"(مجموعاً {grand_total} ردیف).{skip_txt}")
    if bots_with_skips:
        print(f"\n⚠️ {len(bots_with_skips)} ربات حداقل یک ردیفِ رد‌شده داشتند (جزئیات بالاتر در لاگ):")
        for uname, cnt in bots_with_skips:
            print(f"  • @{uname}: {cnt} ردیف رد شد")
    if failed_bots:
        print(f"\n❌ {len(failed_bots)} ربات کاملاً با خطا مواجه شد و رد شد:")
        for uname, err in failed_bots:
            print(f"  • @{uname}: {err}")
        print("\nبرای هرکدام از این‌ها می‌توانید جداگانه با --username اجرا کنید "
              "تا خطای دقیق‌تر و کامل‌تر را ببینید.")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backup", required=True, help="مسیر فایل بک‌آپِ قدیمیِ botsaz.db")
    ap.add_argument("--username", help="یوزرنیمِ ربات (بدون @)")
    ap.add_argument("--bot-id", type=int, help="آیدیِ عددیِ ربات در جدولِ bots")
    ap.add_argument("--all", action="store_true", help="بازگردانیِ همهٔ ربات‌های موجود در بک‌آپ")
    ap.add_argument("--dry-run", action="store_true", help="فقط گزارش بده، چیزی تغییر نده")
    ap.add_argument("--include-bot-row", action="store_true",
                    help="ردیفِ اصلیِ جدولِ bots را هم از بک‌آپ بازنویسی کن")
    args = ap.parse_args()
    if args.all:
        asyncio.run(restore_all(args.backup, args.dry_run, args.include_bot_row))
        return
    if not args.username and args.bot_id is None:
        print("باید --username یا --bot-id یا --all را بدهید.")
        sys.exit(1)
    asyncio.run(restore(args.backup, args.username, args.bot_id, args.dry_run, args.include_bot_row))


if __name__ == "__main__":
    main()
