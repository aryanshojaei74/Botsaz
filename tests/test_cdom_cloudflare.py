"""PATCH_CDOM_CLOUDFLARE_V1 — cdom_sync: دامنه‌هایِ پشتِ پراکسیِ کلودفلر پذیرفته می‌شوند (تشخیصِ رنجِ CF + بدونِ ریدایرکتِ ۸۰)."""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = (ROOT / "deploy" / "cdom_sync.sh").read_text()


def _func(name: str) -> str:
    m = re.search(rf"^{name}\(\) \{{.*?^\}}\n", SCRIPT, re.S | re.M)
    assert m, name
    return m.group(0)


def _bash(snippet: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, timeout=60,
                          env={"PATH": "/usr/bin:/bin:/usr/local/bin", "APP": str(ROOT), "PY": sys.executable})


def test_script_is_valid_bash():
    assert subprocess.run(["bash", "-n", str(ROOT / "deploy" / "cdom_sync.sh")]).returncode == 0


def test_is_cf_detects_cloudflare_ranges_only():
    f = _func("is_cf")
    assert _bash(f + '\nis_cf 104.21.55.80 172.67.170.194 2606:4700:3032::6815:3750').returncode == 0
    assert _bash(f + '\nis_cf 188.132.174.150').returncode != 0
    assert _bash(f + '\nis_cf 104.21.55.80 188.132.174.150').returncode != 0      # مخلوط ⇒ پشتِ کلودفلر حساب نمی‌شود
    assert _bash(f + '\nis_cf').returncode != 0                                    # بدونِ رکورد


def test_render_conf_http_redirect_only_for_non_cloudflare():
    f = _func("render_conf")
    base = 'WEBROOT=/w; UPSTREAM=127.0.0.1:8001; HTTPS_LISTEN="listen 127.0.0.1:8443 ssl;"\n' + f + "\n"
    plain = _bash(base + 'render_conf d.ir 1 0').stdout
    cf = _bash(base + 'render_conf d.ir 1 1').stdout
    first = lambda t: t.split("server {")[1]                                        # بلوکِ پورتِ ۸۰
    assert "return 301 https://" in first(plain)
    assert "return 301" not in first(cf) and "proxy_pass http://127.0.0.1:8001" in first(cf)   # Flexible در حلقه نمی‌افتد
    assert "ssl_certificate" in plain and "ssl_certificate" in cf                    # بلوکِ https هر دو را دارد
    assert "return 301" not in first(_bash(base + 'render_conf d.ir 0 0').stdout)    # بدونِ گواهی هم همین


def test_loop_wires_cloudflare_and_backoff():
    assert 'is_cf $ips' in SCRIPT and 'CF_STATE' in SCRIPT and "-mmin -360" in SCRIPT
    assert 'render_conf "$d" 1 "$cf"' in SCRIPT and 'render_conf "$d" 0 "$cf"' in SCRIPT
    assert "DNS به این سرور نیست" in SCRIPT            # دامنهٔ غیرِ کلودفلری که به جایِ دیگر می‌رود هنوز رد می‌شود
