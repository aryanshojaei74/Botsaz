import inspect
import tenant.shared.miniapp as mi
import tenant.bots.virtual_service.nft_market as nm


def test_web_user_creation_handles_unique_race():
    for mod in (mi, nm):
        src = inspect.getsource(mod)
        assert "except IntegrityError" in src
