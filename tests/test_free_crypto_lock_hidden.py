import inspect
import tenant


def test_lock_label_only_for_owner_or_admin():
    src = inspect.getsource(tenant.show_wallet)
    i = src.index("🔒 شارژ با ارز دیجیتال (پلن رشد+)")
    window = src[max(0, i - 400):i]
    assert "is_owner_or_admin" in window            # مشتری نباید دکمهٔ ارتقا را ببیند (باگ #212)
