"""پلِ روبیکا → پی‌ویِ مدیر: پارسِ آپدیت، قالبِ پیام، استخراجِ شناسهٔ چت، و یک دورِ polling."""
import asyncio

import platform_rubika as rb

RESP = {"status": "OK", "data": {"updates": [
    {"type": "NewMessage", "chat_id": "b0AAA", "new_message": {
        "message_id": "1", "text": "سلام <b>", "sender_type": "User", "sender_id": "u1"}},
    {"type": "NewMessage", "chat_id": "b0AAA", "new_message": {
        "message_id": "2", "text": "echo", "sender_type": "Bot", "sender_id": "bot"}},
    {"type": "StartedBot", "chat_id": "b0BBB"},
    {"type": "NewMessage", "chat_id": "b0CCC", "new_message": {
        "message_id": "3", "sender_type": "User", "sender_id": "u2", "file": {"file_id": "x"}}},
], "next_offset_id": "77"}}


def test_parse_filters_bot_and_events():
    msgs, nxt = rb.parse_updates(RESP)
    assert nxt == "77"
    assert [(m["chat_id"], m["kind"]) for m in msgs] == [("b0AAA", "text"), ("b0BBB", "start"), ("b0CCC", "file")]
    assert msgs[0]["text"] == "سلام <b>"


def test_parse_legacy_inline_shape():
    msgs, _ = rb.parse_updates({"updates": [{"inline_message": {
        "sender_id": "u", "text": "hi", "message_id": "9", "chat_id": "b0Z"}}]})
    assert msgs and msgs[0]["chat_id"] == "b0Z" and msgs[0]["text"] == "hi"



def _env(monkeypatch, resp_updates, chat=None):
    store = {rb.K_TOKEN: "tok1234567890", rb.K_ENABLED: "1"}
    calls, sent = [], []

    async def _get(k, d=""):
        return store.get(k, d)

    async def _set(k, v):
        store[k] = v

    async def api(token, method, body=None, timeout=25):
        calls.append((method, body))
        if method == "getUpdates":
            return resp_updates
        if method == "getChat":
            return {"status": "OK", "data": {"chat": chat or {
                "first_name": "علی", "last_name": "رضایی", "username": "ali_r", "user_id": "u1"}}}
        return {"status": "OK"}

    class Bot:
        async def send_message(self, chat_id, text, **kw):
            sent.append((chat_id, text))

    rb._prof_cache.clear()
    monkeypatch.setattr(rb, "_get", _get)
    monkeypatch.setattr(rb, "_set", _set)
    monkeypatch.setattr(rb, "api_call", api)
    monkeypatch.setattr(rb, "PLATFORM_ADMIN_ID", 42)
    return store, calls, sent, Bot()






def test_keypad_rejected_falls_back_to_plain(monkeypatch):
    seen = []

    async def api(token, method, body=None, timeout=25):
        seen.append(body)
        return {"status": "ERR", "error": "bad keypad"} if "chat_keypad" in (body or {}) else {"status": "OK"}

    monkeypatch.setattr(rb, "api_call", api)
    asyncio.run(rb._send_user("t", "c", "hi", keypad=True))
    assert len(seen) == 2 and "chat_keypad" not in seen[1]


def test_poll_once_idle_without_token(monkeypatch):
    async def _get(k, d=""):
        return d

    monkeypatch.setattr(rb, "_get", _get)
    monkeypatch.setattr(rb, "PLATFORM_ADMIN_ID", 42)
    assert asyncio.run(rb.poll_once(object())) == 0


def test_poll_once_error_returns_minus_one(monkeypatch):
    store = {rb.K_TOKEN: "tok1234567890"}
    warned = []

    async def _get(k, d=""):
        return store.get(k, d)

    async def api(*a, **k):
        return {"status": "ERR", "error": "HTTP 401"}

    class Bot:
        async def send_message(self, chat, text, **kw):
            warned.append(text)

    monkeypatch.setattr(rb, "_get", _get)
    monkeypatch.setattr(rb, "api_call", api)
    monkeypatch.setattr(rb, "PLATFORM_ADMIN_ID", 42)
    monkeypatch.setattr(rb, "_last_warn", float("-inf"))
    assert asyncio.run(rb.poll_once(Bot())) == -1 and warned


def test_ack_is_deleted_after_delay(monkeypatch):
    calls = []

    async def api(token, method, body=None, timeout=25):
        calls.append((method, body))
        return {"status": "OK", "data": {"message_id": "m55"}} if method == "sendMessage" else {"status": "OK"}

    async def fast_delete(token, chat_id, message_id, secs=0):
        await real(token, chat_id, message_id, 0)

    real = rb._delete_later
    monkeypatch.setattr(rb, "api_call", api)
    monkeypatch.setattr(rb, "_delete_later", fast_delete)

    async def run():
        await rb._ack("t", "c1", "ok")
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert ("deleteMessage", {"chat_id": "c1", "message_id": "m55"}) in calls
    assert rb.ACK_DELETE_SECS == 3.0


def test_parse_file_keeps_caption_and_info():
    msgs, _ = rb.parse_updates({"data": {"updates": [{"type": "NewMessage", "chat_id": "b0F", "new_message": {
        "message_id": "5", "text": "این رسید", "sender_type": "User", "sender_id": "u",
        "file": {"file_id": "F1", "file_name": "r.jpg", "size": "2048"}}}]}})
    m = msgs[0]
    assert m["kind"] == "file" and m["file_id"] == "F1" and m["file_name"] == "r.jpg"
    assert m["file_size"] == 2048 and m["text"] == "این رسید"





def test_keypad_shape_and_phone_button_type():
    import platform_msg_tickets as T
    kp = rb._build_keypad([[T.BTN_NEW], [T.BTN_MY, T.BTN_ABOUT], [T.BTN_PHONE]])
    btns = [b for r in kp["rows"] for b in r["buttons"]]
    assert [b["type"] for b in btns] == ["Simple", "Simple", "Simple", "AskMyPhoneNumber"]
    assert btns[0]["button_text"] == T.BTN_NEW and len({b["id"] for b in btns}) == 4


def test_handle_passes_normalized_event_to_ticket_engine(monkeypatch):
    import platform_msg_tickets as T
    got = {}

    async def fake_handle(bot, ch, ev):
        got["ev"], got["ch"] = ev, ch
        return 1

    async def prof(token, chat_id):
        return {"first_name": "علی", "last_name": "رضایی", "username": "ali", "user_id": "U9"}

    monkeypatch.setattr(T, "handle_user", fake_handle)
    monkeypatch.setattr(rb, "get_profile", prof)
    m = rb.parse_updates({"data": {"updates": [{"type": "NewMessage", "chat_id": "b0F", "new_message": {
        "message_id": "5", "text": "رسید", "sender_type": "User", "sender_id": "u",
        "file": {"file_id": "F1", "file_name": "r.jpg"}}}]}})[0][0]
    assert asyncio.run(rb._handle(object(), "tok", m)) == 1
    ev = got["ev"]
    assert ev["name"] == "علی رضایی" and ev["username"] == "ali" and ev["user_id"] == "U9"
    assert ev["kind"] == "file" and ev["file_type"] == "photo" and ev["file_id"] == "F1"
    assert got["ch"].key == "rubika"


def test_send_falls_back_to_plain_when_keypad_rejected(monkeypatch):
    seen = []

    async def api(token, method, body=None, timeout=25):
        seen.append(body)
        return {"status": "ERR", "error": "bad"} if "chat_keypad" in (body or {}) else {"status": "OK"}

    monkeypatch.setattr(rb, "api_call", api)
    asyncio.run(rb.RubikaChannel("t").send("c", "hi", [["a"]]))
    assert len(seen) == 2 and "chat_keypad" not in seen[1]
