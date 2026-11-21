from tenant.bots.virtual_service import smm_api as A


def test_aparat_is_a_platform():
    keys = [k for k, _l, _kw in A.PLATFORMS]
    assert "aparat" in keys and keys.count("aparat") == 1
    assert A.platform_label("aparat") == "📺 آپارات"


def test_detection_by_name():
    assert A.detect_platform("ویو آپارات [ارزان]") == "aparat"
    assert A.detect_platform("Aparat Views") == "aparat"
    assert A.detect_platform("ویو تلگرام") == "telegram"
