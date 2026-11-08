"""PATCH_FRAGAPI_V1 — تحویلِ خودکارِ استارز/پرمیوم با Fragment API (fragment-api.com)."""
import asyncio
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
from tenant.bots.virtual_service import fragapi as FA
from tenant.bots.virtual_service import smm as S

BOT = 7


def test_helpers():
    assert FA.looks_like_jwt("eyJhbGciOiJSUzI1NiJ9.eyJzc3QiOiJ4eHh4eHh4In0.abcdefghijkl")
    assert not FA.looks_like_jwt("hello world") and not FA.looks_like_jwt("a.b")
    assert FA.extract_username("👤 یوزرنیم: @ali_x") == "ali_x" and FA.extract_username("x") is None
    msg, codes = FA._errors_of({"errors": [{"error": "Recipient username was not found on Fragment", "code": "20"}]})
    assert codes == ["20"] and "not found" in msg
    assert FA.FragApiError("x", 400, ["20"]).clean and not FA.FragApiError("x", 400, ["10"]).clean
    assert FA.FragApiError("x", 429).clean and not FA.FragApiError("x", 400, []).clean
    assert FA.norm_status({"status": "COMPLETED"}) == "completed" and FA.norm_status({"status": "FAILED"}) == "failed"
    for st in ("CREATED", "PENDING", "BLOCKCHAIN_SENT"):
        assert FA.norm_status({"status": st, "success": True}) == "pending"       # success به‌تنهایی کافی نیست
    import base64, json
    pl = base64.urlsafe_b64encode(json.dumps({"exp": 1825609730, "sst": "SECRET"}).encode()).decode().rstrip("=")
    assert FA.token_expiry(f"aaaaaaaa.{pl}.bbbbbbbb") == 1825609730
    assert FA.mask("A" * 30).startswith("AAAAAAAA…")


class _Resp:
    def __init__(self, status, data):
        self.status, self._d = status, data

    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def json(self, content_type=None): return self._d


def _fake_session(status, data, exc=None):
    class Sess:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        def request(self, method, url, headers=None, json=None):
            if exc:
                raise exc
            Sess.last = (method, url, headers, json)
            return _Resp(status, data)
    return Sess


@pytest.mark.asyncio
async def test_call_maps_errors_and_sends_jwt_header(monkeypatch):
    import aiohttp
    sess = _fake_session(200, {"id": "u1", "status": "COMPLETED"})
    monkeypatch.setattr(FA.aiohttp, "ClientSession", sess)
    r = await FA.buy_stars("TOK", "ali", 100)
    assert r["status"] == "COMPLETED"
    m, url, h, body = sess.last
    assert m == "POST" and url.endswith("/v1/order/stars/") and h["Authorization"] == "JWT TOK"
    assert body == {"username": "ali", "quantity": 100, "show_sender": False, "currency": "ton"}
    monkeypatch.setattr(FA.aiohttp, "ClientSession", _fake_session(400, {"errors": [{"error": "nf", "code": "20"}]}))
    with pytest.raises(FA.FragApiError) as e:
        await FA.buy_stars("TOK", "ali", 100)
    assert e.value.clean and e.value.codes == ["20"]
    monkeypatch.setattr(FA.aiohttp, "ClientSession", _fake_session(500, None))
    with pytest.raises(FA.FragApiAmbiguous):                       # خطایِ ۵۰۰ در خرید = نامشخص
        await FA.gift_premium("TOK", "ali", 3)
    monkeypatch.setattr(FA.aiohttp, "ClientSession", _fake_session(0, None, exc=asyncio.TimeoutError()))
    with pytest.raises(FA.FragApiAmbiguous):
        await FA.buy_stars("TOK", "ali", 100)
    with pytest.raises(FA.FragApiError):                           # خواندن: خطایِ قطعی، نه Ambiguous
        await FA.get_wallet("TOK")
    monkeypatch.setattr(FA.aiohttp, "ClientSession", _fake_session(401, {"detail": "x"}))
    with pytest.raises(FA.FragApiError) as e2:
        await FA.get_wallet("TOK")
    assert e2.value.clean and "منقضی" in e2.value.message


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(config, "Session", mk)
    monkeypatch.setattr(S, "Session", mk)
    async with mk() as s:
        s.add(models.Bot(id=BOT, owner_id=500, token="7:T", username="vs", field="virtual_service", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=BOT, telegram_id=1001, full_name="Ali", balance=0))
        await s.commit()
    notes, sent, calls = [], [], []

    async def notify(bot, row, text, kb=None, **k):
        notes.append(text)

    async def trust(*a, **k): return None

    async def support_kb(trk): return None

    async def stars_kb(oid): return None

    async def tok(bot_id): return "TOK"
    monkeypatch.setattr(S, "_notify_manual_order", notify)
    monkeypatch.setattr(S, "_notify_gift_order_trust_success", trust)
    monkeypatch.setattr(S, "_gift_support_kb", support_kb)
    monkeypatch.setattr(S, "_stars_admin_kb", stars_kb)
    monkeypatch.setattr(S, "_prm_admin_kb", stars_kb)
    monkeypatch.setattr(FA, "token_of", tok)
    store = {}

    async def sets(bid, k, v): store[k] = v
    monkeypatch.setattr(S, "_set_setting", sets)
    monkeypatch.setattr(FA, "_POLL_DELAYS", [0, 0])
    FA._running.clear()
    FA._locks.clear()

    class B:
        async def send_message(self, chat, text, **k): sent.append((chat, text))
    yield NS(mk=mk, notes=notes, sent=sent, calls=calls, store=store, bot=B())
    await engine.dispose()


async def mk_order(env, gid="starsell_5", qty=500, dest="👤 یوزرنیم: @ali_x", status="awaiting_admin"):
    async with env.mk() as s:
        o = models.GiftOrder(bot_id=BOT, buyer_user_id=1, gift_id=gid, gift_emoji="⭐", star_count=qty, price_toman=1000,
                             dest_telegram_id=1001, dest_label=dest, status=status, pay_method="wallet", tracking_code="T9")
        s.add(o)
        await s.commit()
        return o.id


async def status(env, oid):
    async with env.mk() as s:
        return (await s.get(models.GiftOrder, oid)).status


ROW = NS(id=BOT, owner_id=500)


@pytest.mark.asyncio
async def test_stars_completed_sync(env, monkeypatch):
    async def buy(tok, u, q):
        env.calls.append((u, q))
        return {"id": "o1", "status": "COMPLETED", "ref_id": "REF1"}
    monkeypatch.setattr(FA, "buy_stars", buy)
    oid = await mk_order(env)
    await FA.fulfil(env.bot, ROW, oid, "stars")
    assert env.calls == [("ali_x", 500)] and await status(env, oid) == "sent"
    assert env.store["fragapi_tx_%d" % oid] == "o1"
    assert any("500" in t for _, t in env.sent) and any("REF1" in n for n in env.notes)


@pytest.mark.asyncio
async def test_clean_rejection_reverts_to_manual_without_warning(env, monkeypatch):
    async def buy(tok, u, q):
        raise FA.FragApiError("not found", 400, ["20"])
    monkeypatch.setattr(FA, "buy_stars", buy)
    oid = await mk_order(env)
    await FA.fulfil(env.bot, ROW, oid, "stars")
    assert await status(env, oid) == "awaiting_admin"
    assert env.notes and "رد کرد" in env.notes[-1] and env.notes[-1].startswith("🤖")


@pytest.mark.asyncio
async def test_ambiguous_reverts_with_warning_and_never_retries(env, monkeypatch):
    async def buy(tok, u, q):
        env.calls.append(1)
        raise FA.FragApiAmbiguous("timeout")
    monkeypatch.setattr(FA, "buy_stars", buy)
    oid = await mk_order(env)
    await FA.fulfil(env.bot, ROW, oid, "stars")
    assert len(env.calls) == 1 and await status(env, oid) == "awaiting_admin"
    assert env.notes[-1].startswith("⚠️") and "fragment-api.com/dashboard" in env.notes[-1]


@pytest.mark.asyncio
async def test_pending_then_completed_by_polling(env, monkeypatch):
    async def buy(tok, u, q):
        return {"id": "o2", "status": "PENDING"}
    seq = iter([{"status": "BLOCKCHAIN_SENT"}, {"status": "COMPLETED", "ref_id": "R"}])

    async def get(tok, oid):
        return next(seq)
    monkeypatch.setattr(FA, "buy_stars", buy)
    monkeypatch.setattr(FA, "get_order", get)
    oid = await mk_order(env)
    await FA.fulfil(env.bot, ROW, oid, "stars")
    assert await status(env, oid) == "sent"


@pytest.mark.asyncio
async def test_failed_order_reverts_and_double_claim_is_blocked(env, monkeypatch):
    async def buy(tok, u, q):
        env.calls.append(1)
        return {"id": "o3", "status": "FAILED", "error": {"errors": [{"error": "no funds", "code": "21"}]}}
    monkeypatch.setattr(FA, "buy_stars", buy)
    oid = await mk_order(env)
    await FA.fulfil(env.bot, ROW, oid, "stars")
    assert await status(env, oid) == "awaiting_admin" and "no funds" in env.notes[-1]
    # سفارشِ در حالِ پردازش/تحویل‌شده دوباره برداشته نمی‌شود
    for stt in ("auto_processing", "sent"):
        o2 = await mk_order(env, status=stt)
        await FA.fulfil(env.bot, ROW, o2, "stars")
    assert len(env.calls) == 1


@pytest.mark.asyncio
async def test_limits_and_username_checks_do_not_call_api(env, monkeypatch):
    async def buy(*a):
        env.calls.append(1)
        return {"status": "COMPLETED"}
    monkeypatch.setattr(FA, "buy_stars", buy)
    monkeypatch.setattr(FA, "gift_premium", buy)
    for kw in ({"qty": 10}, {"qty": 9000}, {"dest": "بدونِ یوزرنیم"}):
        oid = await mk_order(env, **kw)
        await FA.fulfil(env.bot, ROW, oid, "stars")
        assert await status(env, oid) == "awaiting_admin"
    bad = await mk_order(env, gid="premium_5", qty=0)
    await FA.fulfil(env.bot, ROW, bad, "premium")
    assert await status(env, bad) == "awaiting_admin" and env.calls == []


@pytest.mark.asyncio
async def test_premium_completed(env, monkeypatch):
    async def gift(tok, u, m):
        env.calls.append((u, m))
        return {"id": "p1", "status": "COMPLETED"}
    monkeypatch.setattr(FA, "gift_premium", gift)
    oid = await mk_order(env, gid="premium_6", qty=0)
    await FA.fulfil(env.bot, ROW, oid, "premium")
    assert env.calls == [("ali_x", 6)] and await status(env, oid) == "sent"
    assert any("۶" in t or "6" in t for _, t in env.sent)


def test_wired_into_smm():
    import inspect
    src = inspect.getsource(S)
    assert "fapi:menu" in src and "fapi:go:s:" in src and "fapi:go:p:" in src and "_fapi.register(smm_router)" in src
    assert "fragapi_key_in" in src


def test_extract_username_prefers_username_line_over_at_in_name():
    lbl = "Ali @shop_off\n🆔 آیدی: 5\n👤 یوزرنیم: @real_user"
    assert FA.extract_username(lbl) == "real_user"
    assert FA.extract_username("@abc_def (گیرندهٔ بدونِ استارت)") == "abc_def"
    assert FA.extract_username("👤 یوزرنیم: ندارد") is None


def test_uuid_is_not_jwt():
    assert not FA.looks_like_jwt("00000000-1111-2222-3333-444444444444")


@pytest.mark.asyncio
async def test_long_token_in_chunks_is_accumulated(monkeypatch):
    import tenant, wallet_crypto
    handlers = {}

    class R:
        def message(self, *a, **k):
            return lambda f: handlers.setdefault(f.__name__, f)

        def callback_query(self, *a, **k):
            return lambda f: f
    FA.register(R())

    class St:
        def __init__(self):
            self.d = {}

        async def get_data(self):
            return dict(self.d)

        async def update_data(self, **k):
            self.d.update(k)

        async def clear(self):
            self.d = {}

    class M:
        def __init__(self, t):
            self.text = t
            self.from_user = NS(id=5)
            self.out = []

        async def delete(self):
            pass

        async def answer(self, t, **k):
            self.out.append(t)

    async def row(bot):
        return NS(id=1, owner_id=5)
    saved = {}

    async def setset(bid, k, v):
        saved[k] = v

    async def wal(tok):
        if tok != FULL:
            raise FA.FragApiError("bad", 401)
        return {}

    async def show(*a, **k):
        return None
    FULL = "eyJ" + "a" * 5000 + "." + "b" * 3000 + "." + "c" * 600
    monkeypatch.setattr(tenant, "get_bot_row", row)
    monkeypatch.setattr(S, "_set_setting", setset)
    monkeypatch.setattr(FA, "get_wallet", wal)
    monkeypatch.setattr(FA, "_show", show)
    monkeypatch.setattr(wallet_crypto, "encrypt_secret", lambda t: "ENC:" + t)
    st = St()
    parts = [FULL[:4000], FULL[4000:8000], FULL[8000:]]
    m1 = M(parts[0]); await handlers["fapi_key_save"](m1, None, st)
    assert "بخشی از توکن" in m1.out[0] and not saved
    m2 = M(parts[1]); await handlers["fapi_key_save"](m2, None, st)
    assert not saved
    m3 = M(parts[2]); await handlers["fapi_key_save"](m3, None, st)
    assert saved == {"fragapi_jwt": "ENC:" + FULL} and "ذخیره شد" in m3.out[0]


def test_jwt_diag_flags_truncated_and_hides_token():
    import base64, json
    def b(x): return base64.urlsafe_b64encode(x).rstrip(b"=").decode()
    good = b(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()) + "." + b(json.dumps({"exp": 4102444800}).encode()) + "." + b(b"x" * 256)
    d = FA.jwt_diag(good)
    assert "RS256" in d and "⚠️" not in d and good not in d
    bad = good[:-40]
    assert "ناقص" in FA.jwt_diag(bad) or "⚠️" in FA.jwt_diag(bad)
    assert "۳ بخش" in FA.jwt_diag("abc.def")
