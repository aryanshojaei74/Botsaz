from tenant.bots.vpn.vpn_user import wg_files_from_sub_response

CONF = b"[Interface]\nPrivateKey = abc\nAddress = 10.0.0.2/32\nMTU = 1280\n\n[Peer]\nPublicKey = xyz\nEndpoint = h:51820\n"


def test_single_file_is_byte_identical_and_keeps_name():
    out = wg_files_from_sub_response(CONF, "Turkey.conf")
    assert out == [("Turkey.conf", CONF)]


def test_name_without_extension_and_missing_name():
    assert wg_files_from_sub_response(CONF, "Turkey")[0][0] == "Turkey.conf"
    assert wg_files_from_sub_response(CONF, None)[0][0] == "wireguard.conf"


def test_multi_blob_split_with_names():
    blob = b"# Name: DE\n" + CONF + b"\n# Name: TR\n" + CONF
    out = wg_files_from_sub_response(blob, None)
    assert [n for n, _ in out] == ["DE.conf", "TR.conf"]
    assert all(b"[Interface]" in d for _, d in out)


def test_base64_and_non_wg():
    import base64
    assert wg_files_from_sub_response(base64.b64encode(CONF), "a.conf")[0][1] == CONF
    assert wg_files_from_sub_response(b"vless://abc", "a.conf") == []


import pytest

from tenant.bots.vpn import vpn_user as _vu


class _Bot:
    def __init__(self):
        self.docs = []

    async def send_document(self, chat_id, doc, caption=None, parse_mode=None):
        self.docs.append(doc.filename)


class _Client:
    def __init__(self, n):
        self.n = n

    async def get_wireguard_endpoints(self, email):
        return [{"id": i, "file": f"loc{i}.conf"} for i in range(self.n)]

    async def fetch_wireguard_config_text(self, email, hid, path=None):
        return "[Interface]\nPrivateKey = x\n"

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_single_sub_file_but_many_native_locations_sends_all(monkeypatch):
    """#857: ساب فقط یک لوکیشن می‌دهد ولی پنل ۴ لوکیشن دارد → هر ۴ فایل."""
    async def fetch(link):
        return [("one.conf", b"[Interface]\n")]
    monkeypatch.setattr(_vu, "_fetch_wg_files_from_sub", fetch)
    monkeypatch.setattr(_vu, "_client_from_panel", lambda p: _Client(4))
    b = _Bot()
    assert await _vu._send_sub_wg_files(b, 1, "https://s", object(), "e@x") == 4
    assert len(b.docs) == 4


@pytest.mark.asyncio
async def test_single_sub_file_single_native_keeps_sub_file(monkeypatch):
    async def fetch(link):
        return [("one.conf", b"[Interface]\n")]
    monkeypatch.setattr(_vu, "_fetch_wg_files_from_sub", fetch)
    monkeypatch.setattr(_vu, "_client_from_panel", lambda p: _Client(1))
    b = _Bot()
    assert await _vu._send_sub_wg_files(b, 1, "https://s", object(), "e@x") == 1
    assert b.docs == ["one.conf"]


@pytest.mark.asyncio
async def test_multi_sub_files_sent_directly(monkeypatch):
    async def fetch(link):
        return [("a.conf", b"x"), ("b.conf", b"y")]
    monkeypatch.setattr(_vu, "_fetch_wg_files_from_sub", fetch)
    b = _Bot()
    assert await _vu._send_sub_wg_files(b, 1, "https://s") == 2


# === PATCH_WG_SUB_DIRECT_SINGLE_V1 === دکمهٔ «کانفیگ‌هایِ تکی» هم اول فایل را عیناً از ساب می‌فرستد
@pytest.mark.asyncio
async def test_single_configs_button_sends_sub_file_first_and_skips_rebuilt_wireguard(monkeypatch):
    import pytest_asyncio  # noqa: F401
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    import models
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(_vu, "Session", mk)
    async with mk() as s:
        s.add(models.Bot(id=5, owner_id=1, token="5:T", username="x", field="vpn_sales", plan="pro", status="active"))
        s.add(models.PanelAccount(id=7, bot_id=5, user_telegram_id=777, email="u1", panel_key="marzban",
                                  sub_link="https://p.example/sub/TOK"))
        await s.commit()
    calls = []

    async def sub_files(bot, chat_id, sub_link, panel=None, email=""):
        calls.append(("sub", sub_link, email))
        return 2                                               # ساب دو فایل داد و فرستاده شد

    async def native(bot, chat_id, client, email):
        calls.append(("native",))
        return 0

    class Client:
        async def get_client_links(self, email):
            return ["[Interface]\nPrivateKey = rebuilt\n", "vless://abc#x"]

        async def close(self):
            return None

    async def panel_for(s, bot_id, a):
        return SimpleNS(panel_key="marzban")
    from types import SimpleNamespace as SimpleNS
    monkeypatch.setattr(_vu, "_send_sub_wg_files", sub_files)
    monkeypatch.setattr(_vu, "_send_native_wg_files", native)
    monkeypatch.setattr(_vu, "_client_from_panel", lambda p: Client())
    monkeypatch.setattr(_vu, "_panel_for_account", panel_for)
    sent_msgs, cards = [], []

    async def send_cards(bot, bid, uid, c):
        cards.extend(c)
    monkeypatch.setattr(_vu, "_send_config_cards", send_cards)

    class CB:
        data = "vget:single:7"
        from_user = SimpleNS(id=777)

        class message:
            @staticmethod
            async def answer(*a, **k): sent_msgs.append(a)

        async def answer(self, *a, **k): pass
    await _vu.vget_single(CB(), SimpleNS(token="5:T"))
    assert calls == [("sub", "https://p.example/sub/TOK", "u1")]                    # ساب اول؛ مسیرِ بومی صدا زده نشد
    assert [c[1] for c in cards] == ["vless://abc#x"]                                # وایرگاردِ بازسازی‌شده دوباره فرستاده نشد
    await engine.dispose()
