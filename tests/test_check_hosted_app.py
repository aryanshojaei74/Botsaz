"""scripts/check_hosted_app.py — تشخیصِ «توکن درست است ولی Unauthorized»."""
import importlib.util
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

spec = importlib.util.spec_from_file_location(
    "check_hosted_app", os.path.join(os.path.dirname(__file__), "..", "scripts", "check_hosted_app.py"))
CH = importlib.util.module_from_spec(spec)
spec.loader.exec_module(CH)

GOOD = "123456789:AAEabcdefghijklmnopqrstuvwxyz012345"
OLD = "987654321:AAFzyxwvutsrqponmlkjihgfedcba543210"


def test_diagnose_token_flags_invisible_problems():
    assert CH.diagnose_token(GOOD) == []
    assert any("فاصله" in x for x in CH.diagnose_token(GOOD + "\n"))
    assert any("نقل‌قول" in x for x in CH.diagnose_token('"' + GOOD + '"'))
    assert any("bot" in x for x in CH.diagnose_token("bot" + GOOD))
    assert any("قالب" in x for x in CH.diagnose_token("123:abc"))


def test_mask_never_prints_full_token():
    m = CH.mask(GOOD)
    assert GOOD not in m and m.startswith("1234") and m.endswith(GOOD[-4:])


class _H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.endswith("/env"):
            body = {"name": "pyhdbkm", "env": [{"key": "BOT_TOKEN", "value": GOOD}], "needed": ["BOT_TOKEN"]}
        elif "/files" in self.path:
            body = {"files": [{"path": "main.py", "size": 100}, {"path": ".env", "size": 50}]}
        elif "/filetext" in self.path:
            body = {"text": f'TOKEN = "{OLD}"\n' if "main.py" in self.path else f"BOT_TOKEN={GOOD}\n"}
        elif "/logs" in self.path:
            body = {"logs": "start\naiogram.exceptions.TelegramUnauthorizedError: Unauthorized\n"}
        else:
            body = {}
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(raw)


def test_end_to_end_finds_hardcoded_old_token(monkeypatch, capsys):
    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("VIP_ADMIN_SECRET", "s")
    monkeypatch.setenv("VIP_API_BASE", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setattr(CH, "get_me", lambda t: "✅ معتبر: @x" if t.strip("'\" ") == GOOD else "❌ تلگرام رد کرد (HTTP 401)")
    monkeypatch.setattr(sys, "argv", ["check_hosted_app.py", "5"])
    try:
        rc = CH.main()
    finally:
        srv.shutdown()
    out = capsys.readouterr().out
    assert rc == 1
    assert "main.py" in out and "با توکنِ ثبت‌شده فرق دارد" in out      # توکنِ قدیمیِ سخت‌کدشده
    assert OLD not in out and GOOD not in out                             # توکنِ کامل چاپ نمی‌شود
    assert "Unauthorized" in out


def test_req_has_handles_versions_comments_and_name_normalisation():
    req = "# deps\npython_telegram_bot==20.7\nyt-dlp>=2024.1  # video\n-r other.txt\nrequests\n"
    assert CH.req_has(req, "python-telegram-bot") and CH.req_has(req, "yt-dlp") and CH.req_has(req, "Requests")
    assert not CH.req_has(req, "Pillow") and not CH.req_has("", "x")
    assert CH.PIP_NAMES["telegram"] == "python-telegram-bot"


def test_missing_module_is_reported_with_requirements_verdict(monkeypatch, capsys):
    class H2(_H):
        def do_GET(self):
            if "/logs" in self.path:
                body = {"logs": "ERROR - خطا: No module named 'telegram'\n"}
            elif "/filetext" in self.path and "requirements.txt" in self.path:
                body = {"text": "yt-dlp\n"}
            elif "/filetext" in self.path:
                body = {"text": "from telegram.ext import Application\n"}
            elif "/files" in self.path:
                body = {"files": [{"path": "main.py", "size": 10}, {"path": "requirements.txt", "size": 7}]}
            elif self.path.endswith("/env"):
                body = {"name": "x", "env": [{"key": "BOT_TOKEN", "value": GOOD}, {"key": "chat", "value": "1"}]}
            else:
                body = {}
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(raw)
    srv = HTTPServer(("127.0.0.1", 0), H2)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("VIP_ADMIN_SECRET", "s")
    monkeypatch.setenv("VIP_API_BASE", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setattr(CH, "get_me", lambda t: "✅ معتبر: @x")
    monkeypatch.setattr(sys, "argv", ["check_hosted_app.py", "5"])
    try:
        rc = CH.main()
    finally:
        srv.shutdown()
    out = capsys.readouterr().out
    assert rc == 1
    assert "python-telegram-bot» در requirements.txt نیست" in out
    assert "Application" in out and "متغیرِ با نامِ کوچک" in out


def test_fix_token_replaces_rejected_registered_token_with_the_single_valid_file_token(monkeypatch, capsys):
    saved = {}

    class H3(_H):
        def do_GET(self):
            if self.path.endswith("/env"):
                body = {"name": "x", "env": [{"key": "BOT_TOKEN", "value": saved.get("tok", OLD)},
                                             {"key": "ADMIN_ID", "value": "7"}]}
            elif "/files" in self.path:
                body = {"files": [{"path": "main.py", "size": 10}]}
            elif "/filetext" in self.path:
                body = {"text": f'BOT_TOKEN = "{GOOD}"\n'}
            else:
                body = {"logs": ""}
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            from urllib.parse import parse_qs
            payload = json.loads(parse_qs(self.rfile.read(n).decode())["payload"][0])
            saved["tok"] = payload["BOT_TOKEN"]
            saved["other"] = payload["ADMIN_ID"]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")
    srv = HTTPServer(("127.0.0.1", 0), H3)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("VIP_ADMIN_SECRET", "s")
    monkeypatch.setenv("VIP_API_BASE", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setattr(CH, "get_me", lambda t: "✅ معتبر: @x" if t.strip("'\" ") == GOOD else "❌ تلگرام رد کرد (HTTP 401)")
    monkeypatch.setattr(sys, "argv", ["check_hosted_app.py", "5", "--fix-token"])
    try:
        CH.main()
    finally:
        srv.shutdown()
    out = capsys.readouterr().out
    assert saved["tok"] == GOOD and saved["other"] == "7"       # فقط توکن عوض شد، بقیهٔ متغیرها دست‌نخورده
    assert "تأیید شد" in out and GOOD not in out and OLD not in out


def test_fix_token_does_nothing_when_registered_token_is_valid(monkeypatch, capsys):
    posted = []

    class H4(_H):
        def do_POST(self):
            posted.append(1)
            self.send_response(200)
            self.end_headers()
    srv = HTTPServer(("127.0.0.1", 0), H4)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("VIP_ADMIN_SECRET", "s")
    monkeypatch.setenv("VIP_API_BASE", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setattr(CH, "get_me", lambda t: "✅ معتبر: @x")
    monkeypatch.setattr(sys, "argv", ["check_hosted_app.py", "5", "--fix-token"])
    try:
        CH.main()
    finally:
        srv.shutdown()
    assert posted == [] and "نیازی به تغییر نیست" in capsys.readouterr().out
