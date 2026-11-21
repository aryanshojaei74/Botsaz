"""
تست‌هایِ خودِ audit.py (PATCH_AUDIT_PRECISION_V1).

هدف: بررسی‌ها هم «هشدارِ الکی» ندهند (تا کسی به audit بی‌اعتماد نشود) و هم موردِ واقعی را
از دست ندهند. با فایل‌هایِ نمونهٔ موقت کار می‌کنیم، نه کدِ پروژه.
"""
import importlib.util
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("audit_under_test", ROOT / "audit.py")
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)


def _run(check, tmp_path, name, code):
    p = tmp_path / name
    p.write_text(textwrap.dedent(code), encoding="utf-8")
    return check([p])


# ───────────────────── except بی‌صدا ─────────────────────

@pytest.mark.parametrize("code", [
    # منطقِ برنامه/دیتابیس/پول — باید گزارش شود
    """
    async def f(s):
        try:
            await s.commit()
        except Exception:
            pass
    """,
    """
    async def f(s):
        try:
            await s.commit()
        except:
            pass
    """,
    """
    async def f(s, x):
        try:
            x.a = 1
            await s.commit()
        except Exception:  # noqa: BLE001
            pass
    """,   # noqa تنها دلیل نیست
    """
    def f():
        try:
            do()
        except BaseException:
            pass
    """,
    """
    async def f(msg):
        try:
            await msg.delete()
            await do_important_thing()
        except Exception:
            pass
    """,   # بدنهٔ try فقط یک عملیاتِ UI نیست
])
def test_silent_flags_real_swallows(tmp_path, code):
    assert len(_run(audit.check_silent, tmp_path, "mod.py", code)) == 1


@pytest.mark.parametrize("code", [
    # پاک‌کردنِ پیامِ کاربر
    """
    async def f(msg):
        try:
            await msg.delete()
        except Exception:
            pass
    """,
    # پاسخ به callbackِ احتمالاً منقضی
    """
    async def f(cb):
        try:
            await cb.answer("در حال دریافت...")
        except Exception:
            pass
    """,
    # بستنِ session در finally
    """
    async def f(tbot):
        try:
            work()
        finally:
            try:
                await tbot.session.close()
            except Exception:  # noqa: BLE001
                pass
    """,
    # نویسنده دلیل را نوشته
    """
    async def f(bot, chat):
        try:
            m = await bot.get_chat_member(chat, 1)
        except Exception:
            # اگه get_chat_member شکست خورد، ادامه بده — عضوه.
            pass
    """,
    # exceptِ مشخص — اصطلاحِ درستِ پایتون
    """
    import os
    def f(p):
        try:
            os.remove(p)
        except OSError:
            pass
    """,
    # خطا لاگ می‌شود
    """
    def f():
        try:
            do()
        except Exception:
            logger.exception("x")
    """,
])
def test_silent_ignores_intentional_patterns(tmp_path, code):
    assert _run(audit.check_silent, tmp_path, "mod.py", code) == []


def test_silent_detects_one_liner_form_the_old_regex_missed(tmp_path):
    """`except Exception:  # noqa` + pass روی خطِ بعد را regexِ قبلی نمی‌دید."""
    code = """
    async def f(s):
        try:
            await s.execute(q)
        except Exception:                # noqa: BLE001
            pass
    """
    assert len(_run(audit.check_silent, tmp_path, "mod.py", code)) == 1


def test_silent_reports_line_of_the_except(tmp_path):
    code = "def f():\n    try:\n        do()\n    except Exception:\n        pass\n"
    hit = _run(audit.check_silent, tmp_path, "mod.py", code)[0]
    assert ":4 " in hit


def test_silent_skips_root_diagnostic_scripts(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    code = "def f():\n    try:\n        do()\n    except Exception:\n        pass\n"
    for name in ("check_something.py", "diag_thing.py", "test_x.py"):
        p = tmp_path / name
        p.write_text(code, encoding="utf-8")
        assert audit.check_silent([p]) == [], name
    # ولی ماژولِ عادی در همان ریشه گزارش می‌شود
    p = tmp_path / "service.py"
    p.write_text(code, encoding="utf-8")
    assert len(audit.check_silent([p])) == 1


# ───────────────────── مسیرِ فروش بدونِ گزارش ─────────────────────

def test_money_flags_real_sale_without_alert(tmp_path):
    code = """
    async def buy(s, uid, amount):
        s.add(WalletTxn(user_id=uid, amount=-amount, kind="purchase"))
        await s.commit()
    """
    assert len(_run(audit.check_money, tmp_path, "shop.py", code)) == 1


def test_money_flags_sold_kind_too(tmp_path):
    code = """
    def rec(s):
        s.add(Sale(kind="sold"))
    """
    assert len(_run(audit.check_money, tmp_path, "sales.py", code)) == 1


def test_money_ok_when_alert_is_called(tmp_path):
    code = """
    async def buy(s, uid, amount):
        s.add(WalletTxn(user_id=uid, amount=-amount, kind="purchase"))
        await s.commit()
        await _purchase_alert(bot, row, buyer=uid, amount=amount)
    """
    assert _run(audit.check_money, tmp_path, "shop.py", code) == []


def test_money_ignores_docstring_and_comment_mentions(tmp_path):
    """باگِ قبلی: ذکرِ WalletTxn(kind="purchase") در docstring به‌عنوانِ ثبتِ فروش گزارش می‌شد."""
    code = '''
    async def has_previous_purchase(s, uid):
        """آیا قبلاً خریدی داشته؟ منبع: WalletTxn(kind="purchase")."""
        # kind="sold" هم مثل همین
        return await s.scalar(select(WalletTxn).where(WalletTxn.kind == "purchase"))
    '''
    assert _run(audit.check_money, tmp_path, "reader.py", code) == []


def test_money_ignores_wallet_topups(tmp_path):
    code = """
    def charge(s):
        s.add(WalletTxn(kind="charge", amount=5))
    """
    assert _run(audit.check_money, tmp_path, "wallet.py", code) == []


def test_money_ignores_partner_bulk_stock_orders(tmp_path):
    code = """
    def stock(s):
        s.add(PartnerStockOrder(kind="purchase"))
    """
    assert _run(audit.check_money, tmp_path, "partner.py", code) == []


def test_money_skips_models_and_entrypoints(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    code = 'def f(s):\n    s.add(WalletTxn(kind="purchase"))\n'
    for name in ("models.py", "main_tenant.py", "platform_bot.py"):
        p = tmp_path / name
        p.write_text(code, encoding="utf-8")
        assert audit.check_money([p]) == [], name


# ───────────────────── روی خودِ پروژه ─────────────────────

def test_project_has_no_audit_warnings_for_these_two_checks(monkeypatch):
    """رگرسیون: کسی `except Exception: pass` یا فروشِ بدونِ _purchase_alert اضافه نکند."""
    monkeypatch.setattr(audit, "ROOT", ROOT)   # مستقل از پوشهٔ اجرایِ pytest
    files = audit.py_files(None)
    assert audit.check_money(files) == []
    assert audit.check_silent(files) == []
