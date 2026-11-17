from tenant.shared import premium_emoji_admin as PE


def test_plan_cache_is_short_enough_after_upgrade():
    assert PE._TOKEN_CACHE_TTL <= 60.0
