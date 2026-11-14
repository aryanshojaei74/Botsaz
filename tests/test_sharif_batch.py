from types import SimpleNamespace as NS
from tenant.bots.virtual_service import smm
from tenant.gateways import apay as AP


def test_unit_label():
    assert smm.unit_label(NS(unit_mode="auto", max_qty=1, unit_name=None)) == "هر عدد"
    assert smm.unit_label(NS(unit_mode="auto", max_qty=1, unit_name="اکانت")) == "هر اکانت"
    assert smm.unit_label(NS(unit_mode="per_1000", max_qty=1, unit_name="کد")) == "هر ۱۰۰۰ کد"
    assert smm.unit_label(NS(unit_mode="auto", max_qty=100, unit_name=None)) == "هر ۱۰۰۰ عدد"


def _btns(**kw):
    t, kb = AP.invoice_view(title="x", pay_rial=300890, card="6219861965232381", holder="h", order_id="o",
                            pay_url="https://x.y", check_cb="c", cancel_cb="d", **kw)
    return t, [b.text for r in kb.inline_keyboard for b in r]


def test_apay_hide_pay_btn():
    t, b = _btns()
    assert "💳 پرداخت" in b and "«💳 پرداخت» را بزن" in t
    t, b = _btns(show_pay_btn=False)
    assert "💳 پرداخت" not in b and "«💳 پرداخت»" not in t
    # بدونِ کارت دکمه تنها راه است، پنهان نمی‌شود
    t2, kb = AP.invoice_view(title="x", pay_rial=1, card="", holder="", order_id="o", pay_url="https://x.y",
                             check_cb="c", cancel_cb="d", show_pay_btn=False)
    assert "💳 پرداخت" in [b.text for r in kb.inline_keyboard for b in r]


import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
import models


class _Cb:
    def __init__(self, data):
        self.data = data
        self.message = type("M", (), {"edit_text": staticmethod(lambda *a, **k: _noop()),
                                      "answer": staticmethod(lambda *a, **k: _noop())})()
        self.from_user = type("U", (), {"id": 1})()
    async def answer(self, *a, **k): pass


async def _noop(): return None


def test_bulk_select_price_cat_delete(monkeypatch, tmp_path):
    async def go():
        eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        async with eng.begin() as c:
            await c.run_sync(models.Base.metadata.create_all)
        S = async_sessionmaker(eng, expire_on_commit=False)
        monkeypatch.setattr(smm, "Session", S)
        class R: id = 1
        async def own(cb, bot): return R()
        monkeypatch.setattr(smm, "_owner", own)
        store = {}
        async def gs(b, k, d=""): return store.get(k, d)
        async def ss(b, k, v): store[k] = v
        monkeypatch.setattr(smm, "_get_setting", gs)
        monkeypatch.setattr(smm, "_set_setting", ss)
        async with S() as s:
            for i in (1, 2, 3):
                s.add(models.SmmService(id=i, bot_id=1, service_id=f"m{i}", name=f"s{i}", is_manual=True,
                                        sell_rate=1000 * i, min_qty=1, max_qty=100, category_pk=None))
            s.add(models.SmmService(id=9, bot_id=2, service_id="x", name="other", is_manual=True, sell_rate=5))
            s.add(models.SmmCategory(id=7, bot_id=1, platform="telegram", name="c", is_active=True))
            await s.commit()
        await smm.smm_msel_mode(_Cb("smm:msel:mode:1"), None)
        assert store["smm_mysvc_selmode"] == "1"
        await smm.smm_msel_toggle(_Cb("smm:msel:t:1:1"), None)
        await smm.smm_msel_toggle(_Cb("smm:msel:t:2:1"), None)
        await smm.smm_msel_toggle(_Cb("smm:msel:t:9:1"), None)      # سرویسِ رباتِ دیگر: نباید اثر بگذارد
        assert await smm._msel_valid(1) == [1, 2]
        t, kb = await smm._smm_myservices_list_content(1, 1)
        cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
        assert "smm:msel:price" in cbs and "smm:msel:del" in cbs
        await smm.smm_msel_cat_set(_Cb("smm:msel:catset:7"), None)
        async with S() as s:
            assert (await s.get(models.SmmService, 1)).category_pk == 7
            assert (await s.get(models.SmmService, 3)).category_pk is None
            assert (await s.get(models.SmmService, 9)).category_pk is None
        await smm.smm_msel_active(_Cb("smm:msel:act:off"), None)
        async with S() as s:
            assert not (await s.get(models.SmmService, 2)).is_active
            assert (await s.get(models.SmmService, 9)).is_active
        class Msg:
            def __init__(self, t): self.text = t; self.from_user = type("U", (), {"id": 1})()
            async def answer(self, *a, **k): pass
        class St:
            def __init__(self): self.d = {"msel_mode": "up"}
            async def get_data(self): return self.d
            async def clear(self): pass
        await smm.smm_msel_price_apply(Msg("10"), None, St())
        async with S() as s:
            assert int((await s.get(models.SmmService, 1)).sell_rate) == 1100
            assert int((await s.get(models.SmmService, 2)).sell_rate) == 2200
            assert int((await s.get(models.SmmService, 3)).sell_rate) == 3000
        await smm.smm_msel_del(_Cb("smm:msel:delok"), None)
        async with S() as s:
            assert await s.get(models.SmmService, 1) is None and await s.get(models.SmmService, 3) is not None
            assert await s.get(models.SmmService, 9) is not None
    asyncio.run(go())


def test_vs_pick_kb_styled(monkeypatch):
    import tenant
    from aiogram.enums import ButtonStyle
    async def run(allowed, saved):
        async def ok(b): return allowed
        async def ibs(b, k): return saved.get(k)
        monkeypatch.setattr(tenant, "_ibtn_colored_allowed", ok)
        monkeypatch.setattr(tenant, "ibtn_style", ibs)
        return await smm._pick_kb_styled(1, [0.1, 0.5, 1], "ton:q", "TON", "ton:qc", price_of=lambda q: round(q * 299400), icon="💎")
    kb = asyncio.run(run(True, {}))
    flat = [b for r in kb.inline_keyboard for b in r]
    assert [b.callback_data for b in flat] == ["ton:q:0.1", "ton:q:0.5", "ton:q:1", "ton:qc", "fsm:cancel"]
    assert "29,940" in flat[0].text and flat[0].style == ButtonStyle.PRIMARY and flat[-1].style == ButtonStyle.DANGER
    assert len(kb.inline_keyboard[0]) == 2          # دو ستون
    kb2 = asyncio.run(run(True, {"ibtn:vs_amount": ButtonStyle.SUCCESS}))
    assert kb2.inline_keyboard[0][0].style == ButtonStyle.SUCCESS      # رنگِ انتخابیِ مالک
    kb3 = asyncio.run(run(False, {}))
    assert all(b.style is None for r in kb3.inline_keyboard for b in r)    # پلنِ بدونِ رنگ ⇒ بی‌رنگ مثلِ قبل


def test_vs_color_catalog():
    import tenant
    assert "ibtn:vs_amount" in tenant.IBTN_CATALOG
    assert tenant._IBTN_GROUP_FIELDS["💎 خریدِ تون / استارز (خدماتِ مجازی)"] == {"virtual_service"}
