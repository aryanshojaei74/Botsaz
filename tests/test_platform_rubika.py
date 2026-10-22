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


def test_format_escapes_html_and_roundtrips_chat_id():
    msgs, _ = rb.parse_updates(RESP)
    txt = rb.format_admin_text(msgs[0])
    assert "&lt;b&gt;" in txt and "<b>" not in txt.split("سلام")[1]
    assert rb.chat_id_from_text(txt.replace("<code>", "").replace("</code>", "")) == "b0AAA"


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


def test_poll_once_forwards_with_profile_and_saves_offset(monkeypatch):
    store, calls, sent, bot = _env(monkeypatch, RESP)
    assert asyncio.run(rb.poll_once(bot)) == 2
    assert store[rb.K_OFFSET] == "77" and all(c == 42 for c, _ in sent)
    assert "علی رضایی" in sent[0][1] and "@ali_r" in sent[0][1] and "ثبت نشده" in sent[0][1]
    # کاربر تأییدِ دریافت می‌گیرد
    assert [c for c in calls if c[0] == "sendMessage" and "رسید" in c[1]["text"]]


def test_start_sends_keypad_and_is_not_forwarded(monkeypatch):
    upd = {"status": "OK", "data": {"updates": [{"type": "StartedBot", "chat_id": "b0S"}]}}
    _, calls, sent, bot = _env(monkeypatch, upd)
    assert asyncio.run(rb.poll_once(bot)) == 0 and not sent
    sm = [c[1] for c in calls if c[0] == "sendMessage"][0]
    types = [b["type"] for r in sm["chat_keypad"]["rows"] for b in r["buttons"]]
    assert types == ["Simple", "AskMyPhoneNumber"] and sm["chat_id"] == "b0S"


def test_contact_button_prompts_user_without_forward(monkeypatch):
    upd = {"status": "OK", "data": {"updates": [{"type": "NewMessage", "chat_id": "b0S", "new_message": {
        "message_id": "1", "text": rb.BTN_CONTACT, "sender_type": "User", "sender_id": "u",
        "aux_data": {"button_id": "contact"}}}]}}
    _, calls, sent, bot = _env(monkeypatch, upd)
    assert asyncio.run(rb.poll_once(bot)) == 0 and not sent
    assert any(c[0] == "sendMessage" and "بنویس" in c[1]["text"] for c in calls)


def test_shared_phone_is_saved_and_shown_on_next_messages(monkeypatch):
    upd = {"status": "OK", "data": {"updates": [
        {"type": "NewMessage", "chat_id": "b0P", "new_message": {
            "message_id": "1", "sender_type": "User", "sender_id": "u9",
            "contact_message": {"phone_number": "989121234567", "first_name": "سارا", "last_name": ""}}},
        {"type": "NewMessage", "chat_id": "b0P", "new_message": {
            "message_id": "2", "text": "سلام", "sender_type": "User", "sender_id": "u9"}},
    ]}}
    store, _, sent, bot = _env(monkeypatch, upd)
    assert asyncio.run(rb.poll_once(bot)) == 2
    assert store["rubika_phone:b0P"] == "989121234567"
    assert "989121234567" in sent[0][1] and "989121234567" in sent[1][1]


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
    txt = rb.format_admin_text(m, {})
    assert "r.jpg" in txt and "این رسید" in txt and "b0F" in txt


def test_file_forwarded_as_photo_with_reply_marker(monkeypatch):
    upd = {"status": "OK", "data": {"updates": [{"type": "NewMessage", "chat_id": "b0F", "new_message": {
        "message_id": "5", "sender_type": "User", "sender_id": "u",
        "file": {"file_id": "F1", "file_name": "r.jpg"}}}]}}
    store, calls, sent, bot = _env(monkeypatch, upd)
    photos = []

    async def send_photo(chat, inp, caption=None, **kw):
        photos.append((chat, caption))

    async def dl(token, fid):
        return b"\xff\xd8data"

    bot.send_photo = send_photo
    monkeypatch.setattr(rb, "download_file", dl)
    assert asyncio.run(rb.poll_once(bot)) == 1
    assert photos and photos[0][0] == 42 and rb.chat_id_from_text(photos[0][1].replace("<code>", "")) == "b0F"
    assert not sent


def test_file_download_failure_falls_back_to_text(monkeypatch):
    upd = {"status": "OK", "data": {"updates": [{"type": "NewMessage", "chat_id": "b0F", "new_message": {
        "message_id": "5", "sender_type": "User", "sender_id": "u",
        "file": {"file_id": "F1", "file_name": "big.zip"}}}]}}
    _, _, sent, bot = _env(monkeypatch, upd)

    async def dl(token, fid):
        return None

    monkeypatch.setattr(rb, "download_file", dl)
    assert asyncio.run(rb.poll_once(bot)) == 1
    assert "دانلود نشد" in sent[0][1] and "b0F" in sent[0][1]
