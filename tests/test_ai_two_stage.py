"""هوش مصنوعیِ گروه: نقشهٔ دقیقِ دکمه‌هایِ پنل + مرحلهٔ تحلیلِ سوال (منظورِ کاربر ← بخش‌هایِ لازم)."""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

import platform_ai_kb as K
import platform_group_guard as GG

ROOT = Path(__file__).resolve().parents[1]


def test_menu_lines_use_real_button_labels_and_plans():
    t = K.menu_lines("کد تخفیف چطور بسازم؟")
    assert "پنل مدیریت ← 💰 مالی و کیف پول ← 🏷 کدهای تخفیف [از پلن رشد]" in t
    t2 = K.menu_lines("تست رایگان vpn کجاست")
    assert "🎁 تنظیماتِ تست رایگان" in t2 and "فروش VPN" not in t2.split("تنظیماتِ تست رایگان")[0]
    assert K.menu_lines("سلام") == ""                         # بدونِ کلمهٔ معنادار ⇒ خالی


def test_parse_analysis_is_robust():
    raw = 'متن اضافه ```json\n{"intent":"ساخت کد تخفیف","bot_type":"vpn_sales","topics":[2,"3",99,2,-1],"menu_terms":["تخفیف"],"ambiguous":false,"clarify":""}\n```'
    d = K.parse_analysis(raw, 10)
    assert d["topics"] == [2, 3] and d["intent"] == "ساخت کد تخفیف" and d["ambiguous"] is False
    assert K.parse_analysis("هیچ", 5) is None and K.parse_analysis("{نامعتبر}", 5) is None
    assert K.parse_analysis('<think>x</think>{"topics":[0]}', 3)["topics"] == [0]


def test_compose_puts_llm_picked_topic_even_without_keyword_hit():
    topics = [(("الف",), "بخش اول: خیلی بی‌ربط"), (("ب",), "بخش دوم: جوابِ واقعی دربارهٔ پشتیبان")]
    block = K.compose_knowledge("یه چیزی بی‌ربط به کلیدواژه‌ها", topics,
                                {"topics": [1], "intent": "می‌خواهد پشتیبان اضافه کند", "bot_type": "unknown", "menu_terms": []})
    assert "جوابِ واقعی دربارهٔ پشتیبان" in block and "می‌خواهد پشتیبان اضافه کند" in block


class _Router:
    def __init__(self, reply): self.reply, self.calls = reply, []
    def configured(self): return True
    async def chat(self, system, user, **kw):
        self.calls.append((system, user))
        return self.reply(system, user), {"total_tokens": 1}, "r"


def _patch_router(monkeypatch, router):
    import ai_router
    monkeypatch.setattr(ai_router, "get_router", lambda: router)

    async def pset(key, default=""):
        return default
    monkeypatch.setattr(GG, "get_pset", pset)
    GG._AN_CACHE.clear()


def test_ambiguous_question_gets_clarifying_question_without_second_call(monkeypatch):
    r = _Router(lambda s, u: json.dumps({"intent": "؟", "ambiguous": True, "clarify": "منظورت پنل رباتِ خودته یا ربات همکاری؟", "topics": []}))
    _patch_router(monkeypatch, r)
    out = asyncio.run(GG._ask_ai("چطوری تخفیف بزارم"))
    assert "ربات همکاری" in out and len(r.calls) == 1


def test_final_prompt_contains_analysis_and_exact_menu(monkeypatch):
    def reply(system, user):
        if system.startswith("تو تحلیل‌گرِ"):
            return json.dumps({"intent": "ساخت کد تخفیف برای مشتریان", "bot_type": "vpn_sales", "topics": [0],
                               "menu_terms": ["تخفیف"], "ambiguous": False, "clarify": ""})
        return "جواب نهایی"
    r = _Router(reply)
    _patch_router(monkeypatch, r)
    out = asyncio.run(GG._ask_ai("چجوری کد تخفیف بسازم"))
    assert out == "جواب نهایی" and len(r.calls) == 2
    final_system = r.calls[1][0]
    assert "ساخت کد تخفیف برای مشتریان" in final_system and "🏷 کدهای تخفیف" in final_system


def test_analysis_failure_falls_back_to_single_call(monkeypatch):
    def reply(system, user):
        if system.startswith("تو تحلیل‌گرِ"):
            raise RuntimeError("boom")
        return "جواب بدون تحلیل"
    r = _Router(reply)
    _patch_router(monkeypatch, r)
    assert asyncio.run(GG._ask_ai("چجوری کد تخفیف بسازم")) == "جواب بدون تحلیل"


def test_menu_map_is_up_to_date_with_the_real_panel():
    """اگر منوهایِ پنل عوض شده‌اند: python3 scripts/gen_ai_menu_map.py را بزن و فایلِ تولیدشده را commit کن."""
    res = subprocess.run([sys.executable, str(ROOT / "scripts" / "gen_ai_menu_map.py"), "--check"], cwd=ROOT,
                         capture_output=True, text=True, timeout=300)
    assert res.returncode == 0, "platform_ai_menu_map.py کهنه است؛ scripts/gen_ai_menu_map.py را اجرا کن"
