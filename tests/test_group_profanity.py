"""PATCH_GROUP_PROFANITY_V1 + PATCH_AI_NO_SECRETS_V1."""
import types
import pytest

import platform_group_guard as GG
import platform_profanity as P
import platform_ai_kb as K


@pytest.mark.parametrize("t", ["کیر تو این بات", "مادر جنده", "ننتو گاییدم", "کیییییر", "fuck you", "pedar sag", "حرومزاده", "خارکسه", "گوه نخور"])
def test_profanity_detected(t):
    assert P.find_profanity(t)


@pytest.mark.parametrize("t", ["هیچ کس نیومد", "کسب و کار من", "کیرگیزستان", "بات ساز چطور ساخته شده؟", "کونتاکت", "نت من قطعه",
                               "کیف پول", "کسی هست؟", "shitake", "dickens", "اوبی", "رمز رو فراموش کردم"])
def test_normal_words_not_flagged(t):
    assert P.find_profanity(t) is None


def test_ai_output_never_leaks_secrets():
    out = K.clean_ai_text("سرور 188.132.174.150 توکن 1234567890:AAAbbbCCCdddEEEfffGGGhhhIIIjjjKKK12 کلید sk-abcdefghijklmnopqrstuv "
                          "GROUP_GUARD_AI_API_KEY=abc123456 password: hunter22222")
    for bad in ("188.132", "AAAbbb", "sk-abc", "abc123456", "hunter2"):
        assert bad not in out
    assert "[محرمانگی" in K.ALWAYS and "کلید API" in K.ALWAYS


class _Msg:
    def __init__(self, text, uid=5):
        self.text, self.caption = text, None
        self.from_user = types.SimpleNamespace(id=uid, full_name="Ali", username="ali", is_bot=False)
        self.chat = types.SimpleNamespace(id=-100)
        self.message_id = 7
        self.sender_chat = None
        self.out = []

    async def answer(self, t, **k):
        self.out.append(t)


class _Bot:
    def __init__(self):
        self.deleted = []

    async def delete_message(self, chat, mid):
        self.deleted.append((chat, mid))


@pytest.mark.asyncio
async def test_swearing_deletes_and_warns_admin_exempt(monkeypatch):
    warned = []

    async def pset(k, d=""):
        return {"group_guard_profanity": "on"}.get(k, d)

    async def warn(bot, chat, u, reason, warned_by, evidence=""):
        warned.append((u.id, reason))
        return 1, 3, False

    state = {"admin": False}

    async def auth(msg, bot):
        return state["admin"]
    monkeypatch.setattr(GG, "get_pset", pset)
    monkeypatch.setattr(GG, "_apply_warn", warn)
    monkeypatch.setattr(GG, "_is_authorized", auth)
    m, b = _Msg("کیر تو این بات"), _Bot()
    assert await GG._check_profanity(m, b) is True
    assert b.deleted and warned == [(5, "فحش/توهین")] and "اخطار" in m.out[0] and "1/3" in m.out[0]
    # پیامِ عادی
    m2, b2 = _Msg("سلام بات ساز چطوری؟"), _Bot()
    assert await GG._check_profanity(m2, b2) is False and not b2.deleted
    # ادمین مستثناست
    state["admin"] = True
    assert await GG._check_profanity(_Msg("کیر تو این بات"), _Bot()) is False
    # خاموش
    state["admin"] = False

    async def off(k, d=""):
        return "off" if k == "group_guard_profanity" else d
    monkeypatch.setattr(GG, "get_pset", off)
    assert await GG._check_profanity(_Msg("کیر تو این بات"), _Bot()) is False
