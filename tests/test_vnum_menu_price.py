from types import SimpleNamespace as NS
from tenant.bots.virtual_service import smm


def _v(i, name, rate, vn=True):
    return NS(id=i, name=name, sell_rate=rate * 1000, service_id=("ozv:x:%d" % i) if vn else str(i),
              category="شمارهٔ مجازیِ تلگرام" if vn else "x", type_key="virtual_number" if vn else "member",
              is_active=True)


def test_button_shows_price_for_numbers_only():
    assert smm._item_btn_text(_v(1, "🇧🇩 بنگلادش", 115050)) == "🇧🇩 بنگلادش — 115,050 تومان"
    assert smm._item_btn_text(_v(2, "ممبر کانال", 0, vn=False)) == "ممبر کانال"
    assert len(smm._item_btn_text(_v(3, "x" * 90, 5))) <= 55


def test_numbers_sorted_cheapest_first_normal_services_keep_id_order():
    nums = [_v(1, "ب", 300), _v(2, "الف", 100), _v(3, "ج", 200)]
    assert [v.id for v in smm._menu_sorted(nums)] == [2, 3, 1]
    mixed = [_v(9, "n", 50), _v(5, "svc2", 0, vn=False), _v(4, "svc1", 0, vn=False)]
    assert [v.id for v in smm._menu_sorted(mixed)] == [4, 5, 9]
