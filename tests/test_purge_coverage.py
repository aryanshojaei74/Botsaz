"""
test_purge_coverage.py — جلوگیری از تکرارِ پرتکرارترین باگِ production.

هر بار که جدولِ تازه‌ای با FK به bots.id ساخته شده و کسی یادش رفته آن را به
TENANT_OWNED_MODELS اضافه کند، «حذفِ ربات» و «تعویضِ توکن» با خطای
foreign key constraint شکسته‌اند (gift_orders تنها یکی از آن‌ها بود و ۸۴ بار
در error.log افتاد). این تست همان چک را خودکار می‌کند.
"""
from __future__ import annotations

import models


# جدول‌هایی که عمداً در لیست‌ها نیستند، همراه با دلیلش.
INTENTIONALLY_EXCLUDED = {
    # PATCH_PURGE_PARTNER_LINKS_V1 در config.py این دو را جدا و با حفظِ
    # سوابقِ مالی (NULL کردنِ ارجاع به‌جایِ حذف) مدیریت می‌کند.
    "partner_bots",
    "partner_sales",
    "partner_stock_orders",
    "partner_escalations",
    "partner_card_orders",
    # سابقهٔ مالیِ پلتفرم؛ حذف نمی‌شود، فقط target_bot_id آن NULL می‌شود.
    "plan_orders",
    # خودِ ربات.
    "bots",
}


def _covered_tables() -> set[str]:
    names: set[str] = set()
    for M in models.TENANT_OWNED_MODELS:
        names.add(M.__tablename__)
    for Child, *_ in models.TENANT_CHILD_VIA_MODELS:
        names.add(Child.__tablename__)
    for M, *_ in models.TENANT_DUAL_REF_MODELS:
        names.add(M.__tablename__)
    return names


def _tables_referencing_bots() -> dict[str, list[str]]:
    """{نامِ جدول: [ستون‌هایی که به bots.id یا bot_users.id اشاره دارند]}"""
    out: dict[str, list[str]] = {}
    for table in models.Base.metadata.sorted_tables:
        cols = [
            c.name
            for c in table.columns
            if any(fk.column.table.name in ("bots", "bot_users")
                   for fk in c.foreign_keys)
        ]
        if cols:
            out[table.name] = cols
    return out


def test_every_bot_owned_table_is_purgeable():
    covered = _covered_tables() | INTENTIONALLY_EXCLUDED
    missing = {
        tab: cols
        for tab, cols in _tables_referencing_bots().items()
        if tab not in covered
    }
    assert not missing, (
        "این جدول‌ها به bots.id/bot_users.id وصل‌اند ولی در هیچ‌کدام از "
        "TENANT_OWNED_MODELS / TENANT_CHILD_VIA_MODELS / TENANT_DUAL_REF_MODELS "
        "نیستند؛ حذفِ ربات رویشان با خطای foreign key می‌شکند. "
        "مدل را به TENANT_OWNED_MODELS اضافه کن (یا اگر عمدی است، به "
        "INTENTIONALLY_EXCLUDED در همین فایل با ذکرِ دلیل):\n"
        + "\n".join(f"  {t}: {c}" for t, c in sorted(missing.items()))
    )


def test_owned_models_all_have_bot_id():
    """حلقهٔ purge_bot روی M.bot_id فیلتر می‌کند؛ مدلِ بدونِ این ستون
    همان‌جا AttributeError می‌دهد و کلِ حذف را می‌شکند."""
    bad = [
        M.__name__
        for M in models.TENANT_OWNED_MODELS
        if "bot_id" not in M.__table__.columns
    ]
    assert not bad, (
        "این مدل‌ها ستونِ bot_id ندارند و نباید در TENANT_OWNED_MODELS باشند "
        f"(برایشان از TENANT_CHILD_VIA_MODELS استفاده کن): {bad}"
    )


def test_no_duplicate_entries_in_owned_models():
    seen, dupes = set(), []
    for M in models.TENANT_OWNED_MODELS:
        if M in seen:
            dupes.append(M.__name__)
        seen.add(M)
    assert not dupes, f"مدلِ تکراری در TENANT_OWNED_MODELS: {dupes}"
