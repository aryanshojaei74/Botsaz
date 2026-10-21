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
    assert [m["chat_id"] for m in msgs] == ["b0AAA", "b0CCC"]
    assert msgs[0]["text"] == "سلام <b>" and msgs[1]["kind"] == "file"


def test_parse_legacy_inline_shape():
    msgs, _ = rb.parse_updates({"updates": [{"inline_message": {
        "sender_id": "u", "text": "hi", "message_id": "9", "chat_id": "b0Z"}}]})
    assert msgs and msgs[0]["chat_id"] == "b0Z" and msgs[0]["text"] == "hi"


def test_format_escapes_html_and_roundtrips_chat_id():
    msgs, _ = rb.parse_updates(RESP)
    txt = rb.format_admin_text(msgs[0])
    assert "&lt;b&gt;" in txt and "<b>" not in txt.split("سلام")[1]
    assert rb.chat_id_from_text(txt.replace("<code>", "").replace("</code>", "")) == "b0AAA"


def test_poll_once_forwards_and_saves_offset(monkeypatch):
    store = {rb.K_TOKEN: "tok1234567890", rb.K_ENABLED: "1"}
    sent = []

    async def _get(k, d=""):
        return store.get(k, d)

    async def _set(k, v):
        store[k] = v

    async def api(token, method, body=None, timeout=25):
        assert method == "getUpdates" and "offset_id" not in body
        return RESP

    class Bot:
        async def send_message(self, chat, text, **kw):
            sent.append((chat, text))

    monkeypatch.setattr(rb, "_get", _get)
    monkeypatch.setattr(rb, "_set", _set)
    monkeypatch.setattr(rb, "api_call", api)
    monkeypatch.setattr(rb, "PLATFORM_ADMIN_ID", 42)
    assert asyncio.run(rb.poll_once(Bot())) == 2
    assert store[rb.K_OFFSET] == "77" and all(c == 42 for c, _ in sent)


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
