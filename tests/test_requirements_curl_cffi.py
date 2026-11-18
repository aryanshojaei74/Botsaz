"""NFT منابعِ Portals/Tonnel به curl_cffi نیاز دارند؛ باید در requirements.txt باشد تا نصبِ تازه خطای «نصب نیست» ندهد."""
from pathlib import Path


def test_curl_cffi_is_declared_for_nft_market_sources():
    reqs = Path("requirements.txt").read_text(encoding="utf-8").lower().splitlines()
    assert any(r.strip().startswith("curl_cffi") for r in reqs)
    src = Path("tenant/bots/virtual_service/nft_sources.py").read_text(encoding="utf-8")
    assert "from curl_cffi import requests" in src          # وابستگیِ واقعی هنوز استفاده می‌شود
