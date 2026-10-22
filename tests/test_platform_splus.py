"""پلِ سروش پلاس → پی‌ویِ مدیر (API شبیهِ تلگرام)."""
import asyncio

import platform_splus as sp


def _msg(uid, mid, **kw):
    base = {"message_id": mid, "from": {"id": uid, "first_name": "علی", "last_name": "رضایی", "username": "ali_r"},
            "chat": {"id": uid, "type": "private"}}
    base.update(kw)
    return base


def _upd(*msgs, start=100):
    return {"ok": True, "result": [{"update_id": start + i, "message": m} for i, m in enumerate(msgs)]}


def test_parse_kinds_and_last_update_id():
    resp = _upd(
        _msg(7, 1, text="سلام <b>"),
        _msg(7, 2, photo=[{"file_id": "s", "file_size": 10}, {"file_id": "BIG", "file_size": 99}], caption="رسید"),
        _msg(7, 3, document={"file_id": "D", "file_name": "a.pdf"}),
        _msg(7, 4, contact={"phone_number": "98912", "user_id": 7}),
        _msg(7, 5, contact={"phone_number": "98000", "user_id": 999}),     # مخاطبِ دیگران، نه شمارهٔ خودش
        {"message_id": 6, "from": {"id": 7, "is_bot": True}, "chat": {"id": 7, "type": "private"}, "text": "x"},
        {"message_id": 7, "from": {"id": 7}, "chat": {"id": -5, "type": "group"}, "text": "y"},
    )
    msgs, last = sp.parse_updates(resp)
    assert last == 106
    assert [m["kind"] for m in msgs] == ["text", "file", "file", "contact", "other"]
    assert msgs[1]["file_id"] == "BIG" and msgs[1]["file_type"] == "photo" and msgs[1]["text"] == "رسید"
    assert msgs[2]["file_type"] == "document" and msgs[3]["phone"] == "98912"


def test_format_escapes_and_marker_roundtrip():
    m = sp.parse_updates(_upd(_msg(7, 1, text="<script>")))[0][0]
    txt = sp.format_admin_text(m)
    assert "&lt;script&gt;" in txt and "@ali_r" in txt and "ثبت نشده" in txt
    assert sp.chat_id_from_text(txt.replace("<code>", "").replace("</code>", "")) == "7"


def _env(monkeypatch, resp):
    store = {sp.K_TOKEN: "tok1234567890", sp.K_ENABLED: "1"}
    calls, sent = [], []

    async def _get(k, d=""):
        return store.get(k, d)

    async def _set(k, v):
        store[k] = v

    async def api(token, method, body=None, timeout=25, form=None):
        calls.append((method, body))
        if method == "getUpdates":
            return resp
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": 900}}
        return {"ok": True, "result": True}

    class Bot:
        async def send_message(self, chat, text, **kw):
            sent.append((chat, text))

    monkeypatch.setattr(sp, "_get", _get)
    monkeypatch.setattr(sp, "_set", _set)
    monkeypatch.setattr(sp, "api_call", api)
    monkeypatch.setattr(sp, "PLATFORM_ADMIN_ID", 42)
    return store, calls, sent, Bot()


def test_start_sends_keypad_not_forwarded(monkeypatch):
    store, calls, sent, bot = _env(monkeypatch, _upd(_msg(7, 1, text="/start")))
    assert asyncio.run(sp.poll_once(bot)) == 0 and not sent
    kp = [c[1]["reply_markup"] for c in calls if c[0] == "sendMessage"][0]
    assert kp["keyboard"][1][0]["request_contact"] is True
    assert store[sp.K_OFFSET] == "101"


def test_text_forwarded_ack_scheduled_for_delete(monkeypatch):
    store, calls, sent, bot = _env(monkeypatch, _upd(_msg(7, 1, text="سلام")))
    real = sp._delete_later

    async def fast(token, chat_id, message_id, secs=0):
        await real(token, chat_id, message_id, 0)

    monkeypatch.setattr(sp, "_delete_later", fast)

    async def run():
        n = await sp.poll_once(bot)
        await asyncio.sleep(0.05)
        return n

    assert asyncio.run(run()) == 1 and sent[0][0] == 42 and "علی رضایی" in sent[0][1]
    assert ("deleteMessage", {"chat_id": "7", "message_id": 900}) in calls
    assert sp.ACK_DELETE_SECS == 3.0


def test_phone_saved_and_shown_on_later_messages(monkeypatch):
    store, _, sent, bot = _env(monkeypatch, _upd(
        _msg(7, 1, contact={"phone_number": "98912", "user_id": 7}), _msg(7, 2, text="hi")))
    assert asyncio.run(sp.poll_once(bot)) == 2
    assert store["splus_phone:7"] == "98912"
    assert "98912" in sent[0][1] and "98912" in sent[1][1]


def test_file_forwarded_as_photo(monkeypatch):
    _, _, sent, bot = _env(monkeypatch, _upd(_msg(7, 1, photo=[{"file_id": "P"}])))
    photos = []

    async def send_photo(chat, inp, caption=None, **kw):
        photos.append((chat, caption))

    async def dl(token, fid):
        return b"\xff\xd8img"

    bot.send_photo = send_photo
    monkeypatch.setattr(sp, "download_file", dl)
    assert asyncio.run(sp.poll_once(bot)) == 1 and photos and photos[0][0] == 42 and not sent


def test_file_download_failure_falls_back(monkeypatch):
    _, _, sent, bot = _env(monkeypatch, _upd(_msg(7, 1, document={"file_id": "D", "file_name": "big.zip"})))

    async def dl(token, fid):
        return None

    monkeypatch.setattr(sp, "download_file", dl)
    assert asyncio.run(sp.poll_once(bot)) == 1 and "دانلود نشد" in sent[0][1]


def test_error_warns_once_and_returns_minus_one(monkeypatch):
    store, _, sent, bot = _env(monkeypatch, {"ok": False, "error": "401 Unauthorized"})
    monkeypatch.setattr(sp, "_last_warn", float("-inf"))
    assert asyncio.run(sp.poll_once(bot)) == -1 and asyncio.run(sp.poll_once(bot)) == -1
    assert len(sent) == 1


def test_idle_without_token(monkeypatch):
    async def _get(k, d=""):
        return d

    monkeypatch.setattr(sp, "_get", _get)
    monkeypatch.setattr(sp, "PLATFORM_ADMIN_ID", 42)
    assert asyncio.run(sp.poll_once(object())) == 0


def test_timeouts_are_quiet_until_repeated(monkeypatch):
    store, calls, sent, bot = _env(monkeypatch, {"ok": False, "error": "تایم‌اوت"})
    monkeypatch.setattr(sp, "_last_warn", float("-inf"))
    monkeypatch.setattr(sp, "_timeouts", 0)
    rs = [asyncio.run(sp.poll_once(bot)) for _ in range(sp.TIMEOUT_WARN_AFTER)]
    assert rs[:-1] == [0] * (sp.TIMEOUT_WARN_AFTER - 1) and rs[-1] == -1 and len(sent) == 1
    up = [c[1] for c in calls if c[0] == "getUpdates"][0]
    assert up["timeout"] == sp.POLL_LONG_SECS


def test_ok_poll_resets_timeout_counter(monkeypatch):
    _, _, sent, bot = _env(monkeypatch, _upd())
    monkeypatch.setattr(sp, "_timeouts", 4)
    assert asyncio.run(sp.poll_once(bot)) == 0 and sp._timeouts == 0 and not sent
