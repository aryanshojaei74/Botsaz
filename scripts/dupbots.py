#!/usr/bin/env python3
"""
scripts/dupbots.py — ابزارِ واحدِ رسیدگی به ربات‌هایِ تکراری.

«رباتِ تکراری» یعنی یک یوزرنیمِ تلگرامی که در جدولِ bots بیش از یک ردیف
دارد. علتش: مالک توکن را در بات‌فادر revoke کرده و به‌جای «تعویض توکن»،
ربات را از اول اضافه کرده؛ چون گاردِ قدیمی فقط توکن را چک می‌کرد، ردیفِ
دوم ساخته شده و داده‌ها بینِ دو ردیف تکه‌تکه شده‌اند.

سه دستور:

    scan     فهرستِ همهٔ یوزرنیم‌هایِ تکراری + وضعیتِ زندهٔ هر توکن
    show     جزئیاتِ یک جفت: چه چیزی منتقل می‌شود، منویِ هر طرف چیست
    merge    ادغام (پیش‌فرض خشک؛ برایِ اجرایِ واقعی --apply لازم است)
    menu     پاک‌سازیِ منویِ دوتایی — برایِ رباتی که *قبلاً* با ابزارِ
             قدیمی ادغام شده و هر دو منو رویش نشسته است

نمونهٔ کاملِ یک ادغام:

    venv/bin/python scripts/dupbots.py scan
    venv/bin/python scripts/dupbots.py show --keep 76 --drop 315
    venv/bin/python scripts/dupbots.py merge --keep 76 --drop 315 --menu-from 315
    venv/bin/python scripts/dupbots.py merge --keep 76 --drop 315 --menu-from 315 --apply

قواعدِ ایمنی که در کد رعایت شده:
  • بدونِ --apply هیچ چیزی نوشته نمی‌شود.
  • همه‌چیز در *یک* تراکنش است: یا کامل انجام می‌شود یا هیچ.
  • ردیفِ drop هرگز DELETE نمی‌شود (به FK می‌خورد)؛ به «سنگِ‌قبر» تبدیل
    می‌شود: بایگانیِ دائمی + توکنِ آزاد + یوزرنیمِ تغییریافته.
  • توکنِ زنده خودکار تشخیص داده می‌شود؛ اول از ردیفِ قبلی آزاد و بعد
    روی ردیفِ مقصد نشانده می‌شود (bots.token یکتاست).
  • منو هرگز ادغام نمی‌شود — یکی از دو منو انتخاب و دیگری کامل حذف
    می‌شود. ادغامِ منو همیشه منوی دوبرابر و به‌هم‌ریخته می‌سازد.
  • حذفِ هر سطر، اول وابسته‌هایش را (بر اساسِ FKهایِ واقعیِ دیتابیس، نه
    فهرستِ دستی) پاک می‌کند.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from config import Session  # noqa: E402

# جدول‌هایی که ستونِ bot_id دارند ولی نباید منتقل شوند
SKIP_TABLES = {"bots"}


# ───────────────────────────── کمکی‌هایِ اسکیما ─────────────────────────────

async def tables_with_bot_id(conn) -> list[str]:
    rows = (await conn.execute(text("""
        SELECT table_name FROM information_schema.columns
         WHERE table_schema = 'public' AND column_name = 'bot_id'
         ORDER BY table_name
    """))).all()
    return [r[0] for r in rows if r[0] not in SKIP_TABLES]


async def has_id_column(conn, table: str) -> bool:
    return bool((await conn.execute(text("""
        SELECT 1 FROM information_schema.columns
         WHERE table_schema='public' AND table_name=:t AND column_name='id'
    """), {"t": table})).first())


async def referencing_tables(conn, table: str) -> list[tuple[str, str]]:
    """(جدولِ فرزند, ستونِ FK) برایِ همهٔ جدول‌هایی که به table.id ارجاع دارند."""
    rows = (await conn.execute(text("""
        SELECT tc.table_name, kcu.column_name
          FROM information_schema.table_constraints tc
          JOIN information_schema.key_column_usage kcu
            ON kcu.constraint_name = tc.constraint_name
          JOIN information_schema.constraint_column_usage ccu
            ON ccu.constraint_name = tc.constraint_name
         WHERE tc.constraint_type = 'FOREIGN KEY'
           AND ccu.table_name = :t AND ccu.column_name = 'id'
    """), {"t": table})).all()
    return [(r[0], r[1]) for r in rows]


async def delete_rows_deep(conn, table: str, ids: set[int]) -> int:
    """حذفِ سطرها به‌همراهِ کلِ زنجیرهٔ وابسته‌ها. تعدادِ کلِ حذف را برمی‌گرداند."""
    if not ids:
        return 0
    total = 0
    for child, col in await referencing_tables(conn, table):
        if child == table:
            continue
        if await has_id_column(conn, child):
            child_ids = {r[0] for r in (await conn.execute(text(
                f"SELECT id FROM {child} WHERE {col} = ANY(:ids)"),
                {"ids": list(ids)})).all()}
            total += await delete_rows_deep(conn, child, child_ids)
        else:
            res = await conn.execute(text(
                f"DELETE FROM {child} WHERE {col} = ANY(:ids)"), {"ids": list(ids)})
            total += res.rowcount or 0
    res = await conn.execute(text(
        f"DELETE FROM {table} WHERE id = ANY(:ids)"), {"ids": list(ids)})
    return total + (res.rowcount or 0)


# ───────────────────────────── خواندنِ وضعیت ─────────────────────────────

async def bot_row(conn, bot_id: int):
    return (await conn.execute(text(
        "SELECT id, username, owner_id, token, plan, status, created_at,"
        " plan_expires_at FROM bots WHERE id = :i"), {"i": bot_id})).first()


async def row_counts(conn, bot_id: int) -> dict[str, int]:
    out = {}
    for t in await tables_with_bot_id(conn):
        n = (await conn.execute(text(
            f"SELECT count(*) FROM {t} WHERE bot_id = :i"), {"i": bot_id})).scalar() or 0
        if n:
            out[t] = n
    return out


async def menu_of(conn, bot_id: int) -> list[dict]:
    rows = (await conn.execute(text(
        "SELECT id, parent_id, row, col, label, action, created_at"
        "  FROM main_buttons WHERE bot_id = :i"
        " ORDER BY parent_id NULLS FIRST, row, col, id"), {"i": bot_id})).all()
    return [{"id": r[0], "parent_id": r[1], "row": r[2], "col": r[3],
             "label": r[4], "action": r[5], "created_at": r[6]} for r in rows]


def print_menu(items: list[dict], indent: str = "   ") -> None:
    if not items:
        print(indent + "(بدونِ دکمه)")
        return
    top = [b for b in items if b["parent_id"] is None]
    kids: dict[int, list[dict]] = {}
    for b in items:
        if b["parent_id"] is not None:
            kids.setdefault(b["parent_id"], []).append(b)
    for b in top:
        print(f"{indent}({b['row']},{b['col']}) «{b['label']}»  [{b['action']}]")
        for c in kids.get(b["id"], []):
            print(f"{indent}    └ «{c['label']}»  [{c['action']}]")


async def token_alive(token: str) -> bool | None:
    """True زنده، False باطل، None اگر شبکه جواب نداد."""
    if not token or token.startswith("released#"):
        return False
    try:
        import aiohttp
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                    f"https://api.telegram.org/bot{token}/getMe", timeout=15) as r:
                data = await r.json()
                return bool(data.get("ok"))
    except Exception:
        return None


def alive_mark(state: bool | None) -> str:
    return {True: "🟢 زنده", False: "🔴 باطل", None: "❔ نامعلوم"}[state]


# ───────────────────────────────── scan ─────────────────────────────────

async def cmd_scan(check_tokens: bool) -> None:
    async with Session() as s:
        conn = await s.connection()
        groups = (await conn.execute(text("""
            SELECT lower(username) AS u, array_agg(id ORDER BY id) AS ids
              FROM bots
             WHERE username IS NOT NULL AND username <> ''
               AND username NOT LIKE '%#merged%'
             GROUP BY lower(username) HAVING count(*) > 1
             ORDER BY 1
        """))).all()
        if not groups:
            print("✅ هیچ یوزرنیمِ تکراری‌ای نیست.")
            return
        print(f"⚠️ {len(groups)} یوزرنیمِ تکراری\n")
        for uname, ids in groups:
            print(f"@{uname}")
            best = None
            for bid in ids:
                r = await bot_row(conn, bid)
                counts = await row_counts(conn, bid)
                users = counts.get("bot_users", 0)
                total = sum(counts.values())
                buttons = counts.get("main_buttons", 0)
                alive = await token_alive(r[3]) if check_tokens else None
                tok = (r[3] or "")[-6:]
                print(f"   id={bid:<5} …{tok}  {alive_mark(alive) if check_tokens else ''}"
                      f"  plan={r[4]:<7} {r[5]:<8} کاربر={users:<5}"
                      f" دکمه={buttons:<3} سطر={total:<5} ساخت={r[6]:%Y-%m-%d}")
                if best is None or total > best[1]:
                    best = (bid, total)
            print(f"   ← بیشترین داده رویِ id={best[0]} است؛ معمولاً همان --keep می‌شود.")
            print(f"     دستورِ بعدی:  show --keep {best[0]} --drop <بقیه>\n")


# ───────────────────────────────── show ─────────────────────────────────

async def cmd_show(keep: int, drop: int) -> None:
    async with Session() as s:
        conn = await s.connection()
        k, d = await bot_row(conn, keep), await bot_row(conn, drop)
        if not k or not d:
            print("❌ id نامعتبر."); return
        for tag, r in (("KEEP", k), ("DROP", d)):
            alive = await token_alive(r[3])
            print(f"[{tag}] id={r[0]} @{r[1]} توکن …{(r[3] or '')[-6:]} "
                  f"{alive_mark(alive)} plan={r[4]} status={r[5]} "
                  f"ساخت={r[6]:%Y-%m-%d %H:%M}")
            print("   منو:")
            print_menu(await menu_of(conn, r[0]), indent="      ")
            counts = await row_counts(conn, r[0])
            print("   داده: " + "، ".join(f"{t}={n}" for t, n in counts.items()))
            print()
        print("تصمیم‌هایی که باید بگیری:")
        print("  --keep      کدام ردیف بماند (معمولاً پرداده‌تر)")
        print("  --menu-from کدام منو حفظ شود (معمولاً رباتی که تا امروز زنده بوده)")


# ───────────────────────────────── merge ─────────────────────────────────

async def tombstone_only(drop: int) -> bool:
    """=== PATCH_DUP_ARCHIVED_AUTOFIX_V2 ===
    ردیفِ بایگانیِ یک مالکِ دیگر را بدونِ جابه‌جاییِ داده «سنگِ‌قبر» می‌کند تا
    از فهرستِ تکراری‌ها بیرون برود (داده‌اش سرِ جایش می‌ماند)."""
    async with Session() as s:
        conn = await s.connection()
        d = await bot_row(conn, drop)
        if not d:
            return False
        await conn.execute(text(
            "UPDATE bots SET username=:u, status='paused',"
            " archived_at=COALESCE(archived_at, now()),"
            " archive_reason='merged_duplicate' WHERE id=:i"),
            {"u": f"{d[1]}#merged{drop}"[:64], "i": drop})
        await s.commit()
    return True


async def cmd_merge(keep: int, drop: int, menu_from: int | None, apply: bool,
                    twin: bool = False) -> bool:
    """twin=True: ردیفِ drop یک دوقلویِ بایگانی‌شده است؛ چکِ زنده‌بودنِ توکن
    و انتقالِ توکن رد می‌شود (توکنِ بایگانی‌ها معنایی ندارد). True = انجام شد."""
    mode = "اجرایِ واقعی" if apply else "خشک (dry-run) — چیزی نوشته نمی‌شود"
    async with Session() as s:
        conn = await s.connection()
        k, d = await bot_row(conn, keep), await bot_row(conn, drop)

        # ── اعتبارسنجی ──
        if not k or not d:
            print("❌ id نامعتبر."); return False
        if keep == drop:
            print("❌ keep و drop یکی هستند."); return False
        if (k[1] or "").lower() != (d[1] or "").lower():
            print(f"❌ یوزرنیم‌ها یکی نیستند: @{k[1]} ≠ @{d[1]}"); return False
        if k[2] != d[2]:
            print(f"❌ مالک‌ها فرق دارند: {k[2]} ≠ {d[2]} — دستی بررسی کن."); return False
        if menu_from is not None and menu_from not in (keep, drop):
            print("❌ --menu-from باید یکی از keep یا drop باشد."); return False

        print(f"ادغامِ @{k[1]}:  {drop} → {keep}   [{mode}]\n")

        # ── ۱) توکنِ زنده ──
        if twin:
            k_alive, d_alive = True, False   # توکنِ بایگانی‌ها نادیده گرفته می‌شود
        else:
            k_alive, d_alive = await token_alive(k[3]), await token_alive(d[3])
        print(f"۱) توکن‌ها: keep …{(k[3] or '')[-6:]} {alive_mark(k_alive)} | "
              f"drop …{(d[3] or '')[-6:]} {alive_mark(d_alive)}")
        if k_alive is None or d_alive is None:
            print("   ❌ وضعیتِ توکن نامعلوم است (شبکه). دوباره اجرا کن."); return False
        if k_alive and d_alive:
            print("   ❌ هر دو توکن زنده‌اند! این یعنی دو رباتِ واقعاً جدا. ادغام نکن.")
            return False
        if not k_alive and not d_alive:
            print("   ⚠️ هیچ‌کدام زنده نیست؛ ادغام انجام می‌شود ولی ربات کار نخواهد کرد "
                  "تا مالک توکنِ تازه بدهد.")
        take_token = (not k_alive) and d_alive
        print("   → " + ("توکنِ زنده از drop به keep منتقل می‌شود."
                         if take_token else "توکنِ keep دست‌نخورده می‌ماند."))

        # ── ۲) منو ──
        if menu_from is None:
            print("\n❌ --menu-from را مشخص کن (منوها ادغام نمی‌شوند، یکی انتخاب می‌شود).")
            print("   برایِ دیدنِ هر دو منو:  show --keep %d --drop %d" % (keep, drop))
            return False
        menu_loser = drop if menu_from == keep else keep
        loser_menu = await menu_of(conn, menu_loser)
        winner_menu = await menu_of(conn, menu_from)
        print(f"\n۲) منو: منویِ {menu_from} حفظ می‌شود "
              f"({len(winner_menu)} دکمه)، منویِ {menu_loser} حذف "
              f"({len(loser_menu)} دکمه).")
        print("   منویِ نهایی:")
        print_menu(winner_menu, indent="      ")
        if apply:
            n = await delete_rows_deep(conn, "main_buttons",
                                       {b["id"] for b in loser_menu})
            print(f"   حذف‌شده: {n} سطر (دکمه‌ها + وابسته‌هایشان)")

        # ── ۳) انتقالِ سطرها ──
        print("\n۳) انتقالِ داده‌ها:")
        moved = stuck = 0
        for tname in await tables_with_bot_id(conn):
            if not await has_id_column(conn, tname):
                if apply:
                    await conn.execute(text(
                        f"UPDATE {tname} SET bot_id=:k WHERE bot_id=:d"),
                        {"k": keep, "d": drop})
                continue
            ids = [r[0] for r in (await conn.execute(text(
                f"SELECT id FROM {tname} WHERE bot_id=:d"), {"d": drop})).all()]
            if not ids:
                continue
            t_ok = t_stuck = 0
            for rid in ids:
                if not apply:
                    t_ok += 1
                    continue
                sp = await conn.begin_nested()
                try:
                    await conn.execute(text(
                        f"UPDATE {tname} SET bot_id=:k WHERE id=:r"),
                        {"k": keep, "r": rid})
                    await sp.commit()
                    t_ok += 1
                except Exception:
                    # سطرِ تکراری (قیدِ یکتایی). حذفش نمی‌کنیم — رویِ
                    # ردیفِ سنگِ‌قبر می‌ماند و به کسی آسیب نمی‌زند.
                    await sp.rollback()
                    t_stuck += 1
            moved += t_ok
            stuck += t_stuck
            flag = "  ⚠️" if t_stuck else ""
            print(f"   {tname:<26} منتقل={t_ok:<5} جامانده={t_stuck}{flag}")

        # ── ۴) توکن ──
        if take_token and apply:
            await conn.execute(text("UPDATE bots SET token=:t WHERE id=:i"),
                               {"t": f"released#{drop}", "i": drop})
            await conn.execute(text(
                "UPDATE bots SET token=:t, status='active', archived_at=NULL,"
                " archive_reason=NULL WHERE id=:i"), {"t": d[3], "i": keep})

        # ── ۵) سنگِ‌قبر ──
        print(f"\n۴) ردیفِ {drop} → بایگانیِ دائمی (merged_duplicate)")
        if apply:
            await conn.execute(text(
                "UPDATE bots SET username=:u, status='paused',"
                " archived_at=COALESCE(archived_at, now()),"
                " archive_reason='merged_duplicate' WHERE id=:i"),
                {"u": f"{d[1]}#merged{drop}"[:64], "i": drop})
            await s.commit()

    print(f"\nجمع: {moved} سطر منتقل، {stuck} سطر جامانده.")
    if not apply:
        print("\nچیزی نوشته نشد. برایِ اجرایِ واقعی همین دستور را با --apply بزن.")
        return False
    try:
        import tenant_status_bus
        for tok in {k[3], d[3]}:
            if tok:
                await tenant_status_bus.publish_tenant_invalidate(tok)
    except Exception as e:
        print(f"هشدار: ابطالِ کشِ tenant ناموفق ({e}).")
    print("\nقدم‌هایِ بعدی:")
    print("   venv/bin/python rewebhook.py")
    print("   systemctl restart botsaz-tenant botsaz-platform")
    print("   بعد رویِ خودِ ربات /start بزن و منو را ببین.")
    return True


# ───────────────────────────────── menu ─────────────────────────────────

async def cmd_menu(bot_id: int, since: str | None, apply: bool) -> None:
    """منویِ ربات را به دکمه‌هایِ ساخته‌شده از یک زمان به بعد محدود می‌کند.

    لازم می‌شود وقتی ادغامی با ابزارِ قدیمی انجام شده و دو منو رویِ یک
    ربات جمع شده‌اند. زمانِ مرز = زمانِ ساختِ رباتی که منویش درست است.
    """
    import datetime as _dt

    async with Session() as s:
        conn = await s.connection()
        items = await menu_of(conn, bot_id)
        if not items:
            print("این ربات دکمه‌ای ندارد."); return
        if not since:
            print(f"منویِ فعلیِ ربات {bot_id} — {len(items)} دکمه، "
                  "به‌ترتیبِ زمانِ ساخت:\n")
            for b in sorted(items, key=lambda x: x["created_at"]):
                where = "اصلی" if b["parent_id"] is None else f"زیرِ {b['parent_id']}"
                print(f"   {b['created_at']:%Y-%m-%d %H:%M}  id={b['id']:<6}"
                      f" [{where}] «{b['label']}»")
            print("\nمرزِ زمانی را با --since بده تا دکمه‌هایِ قدیمی‌تر حذف شوند.")
            return

        ts = _dt.datetime.fromisoformat(since)
        victims = {b["id"] for b in items if b["created_at"] < ts}
        # زیرمنوهایِ دکمه‌هایِ حذف‌شده هم باید بروند تا دکمهٔ یتیم نماند
        changed = True
        while changed:
            changed = False
            for b in items:
                if b["parent_id"] in victims and b["id"] not in victims:
                    victims.add(b["id"]); changed = True
        survivors = [b for b in items if b["id"] not in victims]
        print(f"ربات {bot_id}: حذفِ {len(victims)} دکمهٔ قدیمی‌تر از "
              f"{ts:%Y-%m-%d %H:%M}، {len(survivors)} دکمه می‌ماند.\n")
        print("منویِ نهایی:")
        print_menu(survivors, indent="   ")
        if not apply:
            print("\nچیزی نوشته نشد. برایِ اجرایِ واقعی --apply را اضافه کن.")
            return
        n = await delete_rows_deep(conn, "main_buttons", victims)
        await s.commit()
        print(f"\nحذف شد: {n} سطر (دکمه‌ها + وابسته‌هایشان).")
        print("کاربر یک‌بار /start بزند تا منویِ تازه را بگیرد.")


# ───────────────────────────────── main ─────────────────────────────────

async def main() -> None:
    ap = argparse.ArgumentParser(description="رسیدگی به ربات‌هایِ تکراری")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan", help="فهرستِ یوزرنیم‌هایِ تکراری")
    p.add_argument("--no-token-check", action="store_true",
                   help="بدونِ استعلامِ زنده‌بودنِ توکن‌ها (سریع‌تر)")

    p = sub.add_parser("show", help="جزئیاتِ یک جفت")
    p.add_argument("--keep", type=int, required=True)
    p.add_argument("--drop", type=int, required=True)

    p = sub.add_parser("merge", help="ادغام")
    p.add_argument("--keep", type=int, required=True)
    p.add_argument("--drop", type=int, required=True)
    p.add_argument("--menu-from", dest="menu_from", type=int,
                   help="id رباتی که منویش باید حفظ شود")
    p.add_argument("--apply", action="store_true",
                   help="اجرایِ واقعی (بدونِ این فقط گزارش می‌دهد)")

    p = sub.add_parser("menu", help="پاک‌سازیِ منویِ دوتایی")
    p.add_argument("--bot", type=int, required=True)
    p.add_argument("--since", help='مرزِ زمانی، مثلاً "2026-07-28T11:08"')
    p.add_argument("--apply", action="store_true")

    a = ap.parse_args()
    if a.cmd == "scan":
        await cmd_scan(check_tokens=not a.no_token_check)
    elif a.cmd == "show":
        await cmd_show(a.keep, a.drop)
    elif a.cmd == "menu":
        await cmd_menu(a.bot, a.since, a.apply)
    else:
        await cmd_merge(a.keep, a.drop, a.menu_from, a.apply)


if __name__ == "__main__":
    asyncio.run(main())
