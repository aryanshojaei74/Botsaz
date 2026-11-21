"""کاربرانِ بدونِ اتصال در پنل‌یار (PATCH_PANEL_YAR_IDLE_V1) — بدونِ شبکه."""
import asyncio
import time
import types

import pytest

from tenant.shared.panel_clients import inactive_users as IU
from tenant.shared.panel_clients import panelyar as py

NOW = int(time.time() * 1000)
DAY = 86_400_000


def _c(name, last=None, created=None, exp=0, **kw):
    d = {"email": name, "expiryTime": exp, **kw}
    if last is not None:
        d["last_online_ms"] = last
    if created is not None:
        d["created_ms"] = created
    return d


def test_parse_ms_variants():
    assert IU.parse_ms(None) == 0 and IU.parse_ms("") == 0 and IU.parse_ms("abc") == 0
    assert IU.parse_ms(1_700_000_000) == 1_700_000_000_000          # ثانیه
    assert IU.parse_ms(1_700_000_000_000) == 1_700_000_000_000      # میلی‌ثانیه
    assert IU.parse_ms("2026-10-20T10:00:00Z") == int(
        __import__("datetime").datetime(2026, 10, 20, 10, tzinfo=__import__("datetime").timezone.utc).timestamp() * 1000)


def test_classify_picks_idle_active_users_only():
    clients = [
        _c("recent", last=NOW - DAY),                                   # دیروز وصل شد ⇒ نه
        _c("old3d", last=NOW - 3 * DAY, sub_id="tok3"),                 # ۳ روز ⇒ بله
        _c("old10d", last=NOW - 10 * DAY, uuid="11111111-1111-1111-1111-111111111111"),
        _c("never_old", last=0, created=NOW - 20 * DAY),                # هرگز، قدیمی ⇒ بله
        _c("never_new", last=0, created=NOW - DAY),                     # تازه ساخته ⇒ نه
        _c("never_unknown", last=0),                                    # ساخت نامعلوم ⇒ بله (هرگز)
        _c("expired", last=NOW - 9 * DAY, exp=NOW - DAY),               # منقضی ⇒ نه
        _c("off", last=NOW - 9 * DAY, enable=False),                    # غیرفعال ⇒ نه
        _c("lim", last=NOW - 9 * DAY, status="limited"),                # حجم تمام ⇒ نه
        {"email": "", "last_online_ms": 0},                             # بی‌نام ⇒ نادیده
    ]
    res = IU.classify(clients, NOW, 2)
    names = [r["name"] for r in res["rows"]]
    assert set(names) == {"old3d", "old10d", "never_old", "never_unknown"}
    assert names[:2] == ["never_old", "never_unknown"] or names[:2] == ["never_unknown", "never_old"]   # هرگزها اول
    assert names[2:] == ["old10d", "old3d"]                                                          # بعد قدیمی‌ترین
    by = {r["name"]: r for r in res["rows"]}
    assert by["old3d"]["code"] == "tok3" and by["old10d"]["code"].startswith("1111") and by["never_old"]["code"] == "—"
    assert res["active"] == 6 and res["total"] == 10
    assert {r["name"] for r in IU.classify(clients, NOW, 7)["rows"]} == {"old10d", "never_old", "never_unknown"}


@pytest.mark.asyncio
async def test_probe_uses_get_usage_with_cap_and_flags_unsupported():
    class Fake:
        calls = 0

        async def get_usage(self, email):
            Fake.calls += 1
            if email == "boom":
                raise RuntimeError("x")
            return {"last_online_ms": NOW - 5 * DAY} if email.startswith("u") else {}
    clients = [{"email": "u1"}, {"email": "u2"}, {"email": "boom"}, {"email": "z"}, {"email": "off", "enable": False}]
    n, skipped = await IU.probe_last_online(Fake(), clients, cap=3)
    assert n == 3 and skipped == 1 and Fake.calls == 3                  # غیرفعال بررسی نمی‌شود، سقف رعایت می‌شود
    assert IU.supported(clients) and clients[0]["last_online_ms"] == NOW - 5 * DAY and clients[2]["last_online_ms"] == 0
    assert not IU.supported([{"email": "a", "last_online_ms": 0}])


@pytest.mark.asyncio
async def test_handler_lists_users_and_sends_file(monkeypatch):
    sent = {}

    class FakeClient:
        async def list_clients(self):
            return [_c("ali", last=NOW - 4 * DAY, sub_id="TOK"), _c("reza", last=NOW - DAY), _c("sara", last=0, created=NOW - 30 * DAY)]
        async def close(self): pass

    async def guard(cb, pid):
        return types.SimpleNamespace(id=1), types.SimpleNamespace(id=pid, panel_key="marzban")
    monkeypatch.setattr(py, "_pyar_guard", guard)
    monkeypatch.setattr(py, "make_panel_client", lambda p: FakeClient())

    class Msg:
        async def edit_text(self, text, **k): sent["text"] = text; sent["kb"] = k.get("reply_markup")
        async def answer_document(self, doc, **k): sent["doc"] = doc.data.decode(); sent["cap"] = k.get("caption")

    class CB:
        data = "pyar:idle:5:2"
        message = Msg()
        async def answer(self, *a, **k): pass
    await py.pyar_idle(CB())
    assert "ali" in sent["text"] and "sara" in sent["text"] and "reza" not in sent["text"] and "TOK" in sent["text"]
    assert "ali | TOK" in sent["doc"] and "sara" in sent["doc"] and "reza" not in sent["doc"]
    cbs = [b.callback_data for row in sent["kb"].inline_keyboard for b in row]
    assert "pyar:idle:5:7" in cbs and "pyar:idle:5:30" in cbs and "pyar:panel:5" in cbs


@pytest.mark.asyncio
async def test_handler_unsupported_panel_says_so(monkeypatch):
    class FakeClient:
        async def list_clients(self): return [{"email": "a"}, {"email": "b"}]
        async def get_usage(self, email): return {}
        async def close(self): pass

    async def guard(cb, pid):
        return types.SimpleNamespace(id=1), types.SimpleNamespace(id=pid, panel_key="shahan")
    monkeypatch.setattr(py, "_pyar_guard", guard)
    monkeypatch.setattr(py, "make_panel_client", lambda p: FakeClient())
    out = {}

    class Msg:
        async def edit_text(self, text, **k): out["t"] = text
        async def answer_document(self, *a, **k): out["doc"] = True

    class CB:
        data = "pyar:idle:5:2"
        message = Msg()
        async def answer(self, *a, **k): pass
    await py.pyar_idle(CB())
    assert "نمی‌دهد" in out["t"] and "doc" not in out


@pytest.mark.asyncio
async def test_marzban_list_exposes_online_created_and_sub_token():
    from tenant.shared.panel_clients.marzban_panel import MarzbanPanelClient
    c = MarzbanPanelClient.__new__(MarzbanPanelClient)
    c.timeout = 8

    async def req(method, path, **k):
        return {"total": 2, "users": [
            {"username": "ali", "used_traffic": 5, "data_limit": 10, "expire": None, "status": "active",
             "online_at": "2026-10-20T10:00:00", "created_at": "2026-10-01T10:00:00",
             "subscription_url": "https://p.example/sub/TOKEN123"},
            {"username": "sara", "used_traffic": 0, "data_limit": 0, "expire": None, "status": "disabled",
             "online_at": None, "created_at": None, "subscription_url": ""}]}
    c._request = req
    out = await c._list_clients_fetch()
    assert out[0]["email"] == "ali" and out[0]["last_online_ms"] > 0 and out[0]["created_ms"] > 0
    assert out[0]["sub_id"] == "TOKEN123" and out[0]["traffic"]["down"] == 5          # کلیدهای قدیمی دست‌نخورده
    assert out[1]["last_online_ms"] == 0 and out[1]["sub_id"] is None and out[1]["status"] == "disabled"
    assert IU.has_last_online_field(out)
