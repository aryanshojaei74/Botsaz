import pytest
from types import SimpleNamespace as NS
from tenant.bots.virtual_service import smm, smm_api as A


@pytest.mark.parametrize("name,cat,expect", [
    ("👍ری اکشن [🟢استوری]", "🖼 استوری تلگرام", "reaction"),
    ("🇮🇷فیک ایرانی 2+ ماه [🟢بدون ریزش]", "✈️ تلگرام", "member"),
    ("📉 فیک ریزشی [اقتصادی]", "✈️ تلگرام", "member"),
    ("🤖اپلودر اخلاقی [🟢واقعی]", "✈️ تلگرام", "member"),
    ("🔥ادلیست [🟢معمولی]", "✈️ تلگرام", "member"),
    ("فالوور فیک اینستاگرام", "اینستا", "follower"),
    ("لایک فیک", "x", "like"),
    ("ویو تلگرام", "x", "view"),
])
def test_detect(name, cat, expect):
    assert A.detect_service_type(name, cat) == expect


@pytest.mark.asyncio
async def test_reclassify_only_other(monkeypatch):
    class S:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, *a, **k): return None
        async def commit(self): return None
    monkeypatch.setattr(smm, "Session", lambda: S())
    a = NS(id=1, name="👍ری اکشن", category="x", type_key="other", service_id="5")
    b = NS(id=2, name="👍ری اکشن", category="x", type_key="view", service_id="6")
    c = NS(id=3, name="🇺🇸 آمریکا", category="شمارهٔ مجازیِ تلگرام", type_key="other", service_id="ozv:o:US")
    d = NS(id=4, name="چیز عجیب", category="x", type_key="other", service_id="7")
    assert await smm.reclassify_other_types([a, b, c, d]) == 1
    assert a.type_key == "reaction" and b.type_key == "view" and c.type_key == "other" and d.type_key == "other"
