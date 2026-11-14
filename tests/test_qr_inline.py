"""PATCH_QR_INLINE_V1 — QR روی سرورِ خودمان؛ لینکِ اشتراک به api.qrserver.com داده نمی‌شود."""
from pathlib import Path

from tenant.shared import qr_inline, web_view

ROOT = Path(__file__).resolve().parent.parent / "tenant" / "shared"


def test_no_source_sends_links_to_external_qr_service():
    for f in ("miniapp.py", "web_view.py", "miniapp_admin.py"):
        assert "api.qrserver.com" not in (ROOT / f).read_text(encoding="utf-8"), f


def test_data_uri_is_png_and_empty_or_oversize_is_none():
    u = qr_inline.data_uri("https://sub.example.com/sub/abc123")
    assert u and u.startswith("data:image/png;base64,") and len(u) > 200
    assert qr_inline.data_uri("") is None and qr_inline.data_uri("   ") is None
    assert qr_inline.data_uri("x" * 4000) is None                 # بیش از ظرفیتِ استانداردِ QR ⇒ هیچ QR (نه درزِ لینک)


def test_servers_html_embeds_local_qr_per_link():
    html = web_view._servers_html(["vless://aaa@h:443?x=1#a", "vmess://bbb"])
    assert html.count('data-qr-src="data:image/png;base64,') == 2
    assert "qrserver" not in html


def test_servers_html_too_long_link_gets_empty_qr_src():
    html = web_view._servers_html(["vless://" + "a" * 4000])
    assert 'data-qr-src=""' in html
