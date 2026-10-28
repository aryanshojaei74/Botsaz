import asyncio
from types import SimpleNamespace
import tenant
from tenant.shared import text_vars


def test_btn_message_vars_rendered_without_mutating(monkeypatch):
    async def fake(text, **kw):
        return text.replace("{{balance}}", "5,000")
    monkeypatch.setattr(text_vars, "render_text", fake)
    btn = SimpleNamespace(message="موجودی: {{balance}}", label="x", id=3)
    out = asyncio.run(tenant._btn_with_vars(btn, 1, None))
    assert out.message == "موجودی: 5,000"
    assert out.label == "x" and out.id == 3
    assert btn.message == "موجودی: {{balance}}"


def test_btn_without_vars_returned_as_is():
    btn = SimpleNamespace(message="سلام", label="x")
    assert asyncio.run(tenant._btn_with_vars(btn, 1, None)) is btn
