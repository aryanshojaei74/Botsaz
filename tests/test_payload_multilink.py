"""کانفیگِ دستیِ «توضیح + چند لینک»: هر لینک جدا قابلِ کپی (MidnightSoul، SSH اندروید/آیفون)."""
import re
from types import SimpleNamespace

import pytest

import tenant

SSH = "ssh://mc9501:7288@n1.prvn.ir:3290#mc9501"
NPV = "npvt-ssh://" + "ewogICAgInNzaENvbm" * 40


def test_each_link_gets_its_own_code_tag_and_text_stays_plain():
    p = f"🔗 لینک مخصوص برنامه Netmod و Karing\nبرای کپی روی لینک بزنید 👇\n{SSH}\n\n🔗 لینک مخصوص ایفون (NPV)\n{NPV}"
    html, mixed = tenant._fmt_payload_html(p)
    assert mixed
    assert re.findall(r"<code>(.*?)</code>", html, re.S) == [SSH, NPV]
    assert "<code>🔗" not in html and "لینک مخصوص ایفون" in html


def test_plain_cases_keep_old_behaviour():
    assert tenant._fmt_payload_html(SSH) == (f"<code>{SSH}</code>", False)
    two = f"vless://a@b:1#x\nvless://c@d:2#y"
    assert tenant._fmt_payload_html(two) == (f"<code>{two}</code>", False)
    assert tenant._fmt_payload_html("just text") == ("<code>just text</code>", False)


def test_html_is_escaped_and_trailing_punctuation_not_in_link():
    html, mixed = tenant._fmt_payload_html("a<b> بزن: https://x.io/p?a=1&b=2.")
    assert mixed and "a&lt;b&gt;" in html
    assert "<code>https://x.io/p?a=1&amp;b=2</code>." in html


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat, text, **kw):
        self.sent.append(("msg", text))

    async def send_photo(self, *a, **kw):
        self.sent.append(("photo", kw.get("caption")))


@pytest.mark.asyncio
async def test_delivery_sends_text_without_qr_photo_for_mixed(monkeypatch):
    async def no_row(bot): return None
    async def boom(*a, **k): raise AssertionError("QR نباید برایِ متنِ ترکیبی ساخته شود")
    monkeypatch.setattr(tenant, "get_bot_row", no_row)
    monkeypatch.setattr(tenant, "_qr_png_cfg", boom)
    cfg = SimpleNamespace(kind="sublink", payload=f"آندروید 👇\n{SSH}\nآیفون 👇\n{NPV}", volume_gb=0, days=30,
                          code=7, custom_label=None, bot_id=1, id=9, category=None)
    bot = FakeBot()
    await tenant.send_config_to_user(bot, 1, cfg)
    kind, text = bot.sent[0]
    assert kind == "msg" and len(text) < 4096
    assert re.findall(r"<code>(.*?)</code>", text, re.S) == [SSH, NPV]
