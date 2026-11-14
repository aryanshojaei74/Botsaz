import io
import subprocess
import sys

SAMPLE = """\
13:55:20 BotSaz uvicorn[1]: 2026-10-05 13:55:20 [WARNING] main_tenant: slow update (token …TqkH4U, kind=callback_query, 14.1s) → vget:single:16856 | tg 17×6.0s db 23×8.1s http 3×0.8s
13:52:45 BotSaz uvicorn[1]: 2026-10-05 13:52:45 [WARNING] main_tenant: slow update (token …QPoPwo, kind=callback_query, 16.1s) → mbtn:7093 | tg 4×0.6s db 37×0.1s http 9×2.6s wait 1×11.0s(max 11.0s login-throttle sub.sefrtasad.com)
13:30:12 BotSaz python3[2]: 2026-10-05 13:30:12 [WARNING] loopwatch: event loop blocked 0.8s [worker pid=1] stack:
13:30:12 BotSaz python3[2]:   File "/opt/botsaz/x/degeris_panel.py", line 37, in _deg_list_inbounds
13:30:12 BotSaz python3[2]:     res = await self._request("GET", path)
13:30:13 BotSaz python3[2]: [retry] SendMessage بعد از 1 تلاشِ ناموفق
"""


def test_summary_classifies_and_reads_stack():
    out = subprocess.run([sys.executable, "perf_summary.py", "-"], input=SAMPLE,
                         capture_output=True, text=True).stdout
    assert "آپدیتِ کند (>۲ث): 2" in out and "گیرِ لوپ: 1" in out and "retryِ تلگرام: 1" in out
    assert "vget:single:N" in out and "mbtn:N" in out
    assert "انتظارِ عمدیِ کد" in out and "دیتابیس" in out
    assert "degeris_panel.py" in out          # بالاترین خطِ stackِ لوپِ گیرکرده


def test_dice_games_are_excluded_and_handler_breakdown_is_printed():
    sample = SAMPLE + (
        "13:56:00 BotSaz uvicorn[1]: 2026-10-05 13:56:00 [WARNING] main_tenant: slow update (token …ABCDEF, kind=message, 4.3s) → فوتبال | tg 2×0.5s db 5×0.0s\n")
    out = subprocess.run([sys.executable, "perf_summary.py", "-"], input=sample, capture_output=True, text=True).stdout
    assert "آپدیتِ کند (>۲ث): 2" in out                  # بازیِ تاس شمرده نشد
    assert "1 بازیِ تاسِ گروه" in out
    assert "میانگینِ هر دکمه به تفکیکِ علت" in out and "db  0.1 (37 کوئری)" in out
