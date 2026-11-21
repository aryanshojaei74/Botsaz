"""دکمه‌هایِ «بررسیِ هوشمند / اصلاح با هوش مصنوعی» در پنلِ ادمینِ رباتِ اصلی (PATCH_HOSTED_AI_FIX_V1)."""
import time
from types import SimpleNamespace

import pytest

import hosted_ai_fix as H
import platform_bot as PB


class Msg:
    def __init__(self):
        self.sent, self.edits = [], []

    async def answer(self, text, **kw):
        self.sent.append((text, kw.get("reply_markup")))
        return self

    async def edit_text(self, text, **kw):
        self.edits.append((text, kw.get("reply_markup")))


def _cb(data, uid=1):
    m = Msg()
    answers = []

    async def answer(t="", show_alert=False):
        answers.append((t, show_alert))
    return SimpleNamespace(data=data, from_user=SimpleNamespace(id=uid), answer=answer, message=m), m, answers


def _datas(kb):
    return [b.callback_data for r in kb.inline_keyboard for b in r]


@pytest.fixture
def fake(monkeypatch):
    async def perm(uid, p):
        return uid == 1
    monkeypatch.setattr(PB, "has_perm", perm)
    PB._FIX_PLANS.clear()
    PB._FIX_BUSY.clear()
    issue = H.Issue("syntax", "handlers/admin.py", 824, "SyntaxError: f-string: unterminated string", ai_fixable=True)
    rep = H.Report(app_id=7, name="hhfhh", issues=[issue], py_version="3.11.9", py_source="image")
    calls = {}

    async def diagnose(client, app_id, compiler=None):
        return rep, {"handlers/admin.py": "old\n"}

    async def plan_fix(client, app_id, **kw):
        return H.FixPlan(7, patches=[H.FilePatch("handlers/admin.py", "old\n", "new\n", "ثابت شد")], notes=["🤖 اصلاح شد"])

    async def apply_fix(client, app_id, plan, name="", **kw):
        calls["apply"] = (app_id, name, [p.path for p in plan.patches])
        return True, ["✅ handlers/admin.py نوشته شد", "✅ بعد از ری‌استارت خطایِ تازه‌ای در لاگ نیست."]
    monkeypatch.setattr(H, "diagnose", diagnose)
    monkeypatch.setattr(H, "plan_fix", plan_fix)
    monkeypatch.setattr(H, "apply_fix", apply_fix)
    return rep, calls


@pytest.mark.asyncio
async def test_non_admin_is_refused(fake):
    cb, m, answers = _cb("vipfix:chk:7", uid=99)
    await PB.vip_fix_action(cb, SimpleNamespace())
    assert answers[-1][1] is True and not m.sent


@pytest.mark.asyncio
async def test_check_shows_report_with_fix_button(fake):
    cb, m, _a = _cb("vipfix:chk:7")
    await PB.vip_fix_action(cb, SimpleNamespace())
    text, kb = m.sent[-1]
    assert "handlers/admin.py:824" in text and "f-string" in text and "3.11.9" in text
    assert "vipfix:plan:7" in _datas(kb)


@pytest.mark.asyncio
async def test_plan_then_apply_flow_and_expiry(fake):
    rep, calls = fake
    cb, m, _a = _cb("vipfix:plan:7")
    await PB.vip_fix_action(cb, SimpleNamespace())
    text, kb = m.edits[-1]
    assert "handlers/admin.py" in text and "-old" in text and "+new" in text
    assert _datas(kb)[:2] == ["vipfix:apply:7", "vipfix:drop:7"] and (1, 7) in PB._FIX_PLANS
    cb2, m2, answers2 = _cb("vipfix:apply:7")
    await PB.vip_fix_action(cb2, SimpleNamespace())
    assert calls["apply"] == (7, "hhfhh", ["handlers/admin.py"]) and "اصلاح اعمال شد" in m2.sent[-1][0]
    assert (1, 7) not in PB._FIX_PLANS
    cb3, m3, answers3 = _cb("vipfix:apply:7")                       # بار دوم: طرح مصرف شده
    await PB.vip_fix_action(cb3, SimpleNamespace())
    assert answers3[-1][1] is True and "apply" in calls
    PB._FIX_PLANS[(1, 7)] = (H.FixPlan(7, patches=[H.FilePatch("a", "", "x")]), "n", time.time() - PB._FIX_PLAN_TTL - 5)
    cb4, m4, answers4 = _cb("vipfix:apply:7")
    await PB.vip_fix_action(cb4, SimpleNamespace())
    assert "منقضی" in answers4[-1][0]


@pytest.mark.asyncio
async def test_clean_project_shows_ok_and_no_plan(fake, monkeypatch):
    async def diagnose(client, app_id, compiler=None):
        return H.Report(app_id=7, name="hhfhh"), {}
    monkeypatch.setattr(H, "diagnose", diagnose)
    cb, m, _a = _cb("vipfix:plan:7")
    await PB.vip_fix_action(cb, SimpleNamespace())
    assert "مشکلی پیدا نشد" in m.edits[-1][0] and not PB._FIX_PLANS


@pytest.mark.asyncio
async def test_busy_guard_and_drop(fake):
    PB._FIX_BUSY.add(7)
    cb, m, answers = _cb("vipfix:chk:7")
    await PB.vip_fix_action(cb, SimpleNamespace())
    assert answers[-1][1] is True and not m.sent
    PB._FIX_BUSY.clear()
    PB._FIX_PLANS[(1, 7)] = (H.FixPlan(7), "n", time.time())
    cb2, _m2, _a2 = _cb("vipfix:drop:7")

    async def screen(app_id, uid):
        return "", None
    PB._vip_app_screen, orig = screen, PB._vip_app_screen
    try:
        await PB.vip_fix_action(cb2, SimpleNamespace())
    finally:
        PB._vip_app_screen = orig
    assert (1, 7) not in PB._FIX_PLANS
