"""PATCH_CDOM_PROBE_V1 — تستِ واقعیِ دامنه (Weirdnet: «SSL فعال» ثابت بود ولی مستندات باز نمی‌شد)."""
import pytest

import tenant.gateways.custom_domain as CD


async def _probe(ips, status=None, err=None, server=("1.2.3.4",)):
    async def resolver(d): return ips

    async def fetcher(url):
        if err == "timeout":
            import asyncio
            raise asyncio.TimeoutError()
        if err == "cert":
            raise RuntimeError("SSLCertVerificationError certificate verify failed")
        if err == "connect":
            raise OSError("refused")
        return status

    async def tgt(): return "host", list(server)
    orig = CD.get_dns_target
    CD.get_dns_target = tgt
    try:
        return await CD.probe_domain("weirdico.ir", "/api/docs/vpn", resolver=resolver, fetcher=fetcher)
    finally:
        CD.get_dns_target = orig


@pytest.mark.asyncio
async def test_all_good():
    r = await _probe(["1.2.3.4"], status=200)
    t = CD.probe_report_text("weirdico.ir", r)
    assert r["status"] == 200 and "✅ <b>DNS" in t and "✅ <b>SSL و وب‌سرویس" in t


@pytest.mark.asyncio
async def test_cloudflare_proxy_is_detected():
    r = await _probe(["104.21.5.5", "172.67.1.1"], err="cert")
    assert r["cloudflare"] is True
    t = CD.probe_report_text("weirdico.ir", r)
    assert "کلودفلر" in t and "DNS only" in t


@pytest.mark.asyncio
async def test_wrong_ip_cert_timeout_and_404_each_get_a_specific_cause():
    t = CD.probe_report_text("d.ir", await _probe(["9.9.9.9"], err="timeout"))
    assert "9.9.9.9" in t and "1.2.3.4" in t and "تایم‌اوت" in t
    t = CD.probe_report_text("d.ir", await _probe(["1.2.3.4"], err="cert"))
    assert "گواهیِ معتبر" in t and "✅ <b>DNS" in t                       # DNS درست ولی گواهی نیست
    t = CD.probe_report_text("d.ir", await _probe(["1.2.3.4"], status=404))
    assert "404" in t and "nginx" in t
    t = CD.probe_report_text("d.ir", await _probe(["1.2.3.4"], status=502))
    assert "502" in t
    t = CD.probe_report_text("d.ir", await _probe([], err="connect"))
    assert "هیچ رکورد" in t and "پورتِ ۴۴۳" in t


@pytest.mark.asyncio
async def test_resolver_failure_never_raises():
    async def bad(d): raise RuntimeError("dns down")
    r = await CD.probe_domain("x.ir", resolver=bad, fetcher=lambda u: _ok())
    assert r["ips"] == [] and r["status"] == 200


async def _ok():
    return 200
