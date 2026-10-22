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


def test_keypad_has_request_contact_for_phone_button():
    import platform_msg_tickets as T
    kp = sp._build_keypad([[T.BTN_NEW], [T.BTN_PHONE]])
    assert kp["keyboard"][0] == [{"text": T.BTN_NEW}]
    assert kp["keyboard"][1] == [{"text": T.BTN_PHONE, "request_contact": True}]


def test_handle_passes_normalized_event_to_ticket_engine(monkeypatch):
    import platform_msg_tickets as T
    got = {}

    async def fake_handle(bot, ch, ev):
        got["ev"], got["ch"] = ev, ch
        return 1

    monkeypatch.setattr(T, "handle_user", fake_handle)
    m = sp.parse_updates(_upd(_msg(7, 1, photo=[{"file_id": "P"}], caption="رسید")))[0][0]
    assert asyncio.run(sp._handle(object(), "tok", m)) == 1
    ev = got["ev"]
    assert ev["name"] == "علی رضایی" and ev["username"] == "ali_r" and ev["chat_id"] == "7"
    assert ev["kind"] == "file" and ev["file_type"] == "photo" and ev["text"] == "رسید"
    assert got["ch"].key == "splus"


def test_poll_dispatches_every_message_and_saves_offset(monkeypatch):
    import platform_msg_tickets as T
    seen = []

    async def fake_handle(bot, ch, ev):
        seen.append(ev["text"])
        return 1

    monkeypatch.setattr(T, "handle_user", fake_handle)
    store, _, _, bot = _env(monkeypatch, _upd(_msg(7, 1, text="a"), _msg(7, 2, text="b")))
    assert asyncio.run(sp.poll_once(bot)) == 2 and seen == ["a", "b"]
    assert store[sp.K_OFFSET] == "102"


def test_parsed_items_carry_message_id():
    m = sp.parse_updates(_upd(_msg(7, 41, text="x")))[0][0]
    assert m["message_id"] == 41
