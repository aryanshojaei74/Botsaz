"""PATCH_GROUP_AI_REVAMP_V1 — هوش مصنوعیِ گروهِ بات‌ساز اصلی: دانش، جست‌وجو و پاک‌کنندهٔ فرمت."""
import re
from types import SimpleNamespace

import pytest

import platform_ai_kb as KB
import platform_group_guard as G

MARKS = re.compile("[ً-ْ]")


# ───────── پاک‌کنندهٔ فرمت ─────────

def test_clean_strips_double_asterisks_and_marks():
    assert KB.clean_ai_text("**سلام** دنیا") == "سلام دنیا"
    assert KB.clean_ai_text("این **مهم** است و **این هم**") == "این مهم است و این هم"
    assert "*" not in KB.clean_ai_text("***خیلی*** مهم و *کج* و ** تک")
    assert KB.clean_ai_text("رباتِ اصلی") == "ربات اصلی"                               # اعراب برداشته می‌شود
    assert MARKS.search(KB.clean_ai_text("مُسَلَّمٌ")) is None


def test_clean_bullets_headings_code_links_html():
    out = KB.clean_ai_text("## مراحل\n* اول\n- دوم\n+ سوم\n1) چهارم")
    assert out == "مراحل\n• اول\n• دوم\n• سوم\n1) چهارم"
    assert KB.clean_ai_text("دستور `xo 10000` را بزن") == "دستور xo 10000 را بزن"
    assert KB.clean_ai_text("```\nبلوک\n```") == "بلوک"
    assert KB.clean_ai_text("[راهنما](https://t.me/x)") == "راهنما: https://t.me/x"
    assert KB.clean_ai_text("<b>مهم</b> و <code>x</code>") == "مهم و x"
    assert KB.clean_ai_text("> نقل قول") == "نقل قول"


def test_clean_keeps_plain_arrows_numbers_and_paths():
    t = "پنل مدیریت ← سرویس‌ها و محصولات ← انبار کانفیگ‌های دستی\n1) اول\n2) دوم"
    assert KB.clean_ai_text(t) == t
    assert KB.clean_ai_text("a_b_c و wager_games") == "a_b_c و wager_games"           # زیرخطِ شناسه‌ها دست‌نخورده
    assert KB.clean_ai_text("خط۱\n\n\n\n\nخط۲   \n") == "خط۱\n\nخط۲"
    assert KB.clean_ai_text("") == "" and KB.clean_ai_text(None) == ""


# ───────── پرامپتِ پایه ─────────

def test_always_prompt_is_plain_and_concise():
    t = KB.ALWAYS
    assert "**" not in t and "`" not in t and not MARKS.search(t)
    assert len(t) < 6500                                                                # با هر سوال فرستاده می‌شود
    for must in ("خودمونی", "ستاره", "حداکثر", "ربات‌های من", "ویتوری", "انبار کانفیگ‌های دستی", "مسیر یا عدد"):
        assert must in KB.strip_marks(t), must
    assert "# " not in t and "\n#" not in t                                           # تیترِ مارک‌داون نیست (مدل تقلید نکند)


# ───────── جست‌وجو ─────────

def _sel(q):
    return G._gg_select_relevant_topics(q)


def _clean(s):
    """متنی که مدل واقعا می‌بیند (بدونِ ** و اعراب)."""
    return KB.strip_marks(s.replace("**", ""))


def test_manual_config_question_gets_stock_and_delivery_topics():
    q = ("باتو ساختم خب فقط نمی‌دونم چه جوری کانفینگ دستی فعال کنم یعنی ویتوری و وایر گارد رو خودم "
         "اگه مشتری ثبت سفارش کرد بفرستم")
    s = _sel(q)
    assert "انبار کانفیگ دستی (فروش V2Ray و وایرگارد)" in s and "تحویل دستی کانفیگ" in s
    assert "ساخت ربات" not in s and "همکار" not in s                                    # بخش‌هایِ بی‌ربط (قبلاً بالا می‌آمدند) نیامدند


@pytest.mark.parametrize("q,needle", [
    ("چطور بازی شرط بندی ایکس او رو برای رباتم فعال کنم؟ قیمتش چقدره", "بازی‌های گروهی ربات"),
    ("بازی xo توی گروه کار نمیکنه", "راه‌اندازی و استفاده از بازی‌های گروهی"),
    ("اگه یکی توی گروه توهین کرد چطور گزارش بدم", "گزارش یه عضو تو همین گروه"),
    ("تست رایگان حجمش رو ۱ گیگ گذاشتم ولی ۲ گیگ میده", "تست رایگان VPN — دو جا"),
    ("میخوام کارت به کارت از فاکتور خرید برداشته بشه", "خاموش کردن کارت‌به‌کارت فقط توی فاکتور"),
    ("محصول دستی میو پوینت رو میخوام آیدی عددی بگیره نه لینک", "ورودی سفارش در سرویس‌های دستی"),
    ("دکمه خرید کانفیگ ندارم مشتری نمی‌تونه بخره", "دکمه فروش کانفیگ تو منوی ربات"),
])
def test_new_topics_are_found(q, needle):
    assert needle in _sel(q)


def test_small_talk_selects_nothing_and_generic_words_do_not_match():
    assert _sel("سلام وقت بخیر یه سوال داشتم") == ""
    assert _sel("ای بات نمی ثبت ساخت") == ""                                            # فقط کلمه‌هایِ عمومی ⇒ هیچ بخشی


def test_old_knowledge_is_still_retrievable():
    for q, needle in [("زرین پال مرچنت ایدی کجا بزنم", "درگاه زرین‌پال"),
                      ("میتونم توی متن پیام ثبت سفارش از بولد استفاده کنم", "متن‌های اطلاع‌رسانی سفارش"),
                      ("وقتی tgtools سفارش استارز رو لغو میکنه پولش برنمیگرده", "لغو TGTools و"),
                      ("زرین پال ازم آی پی سرور خواسته چی بزنم", "آی‌پی سرور در زرین‌پال"),
                      ("میشه زرین پال رو با دامنه و سرور خودم راه بندازم", "آی‌پی سرور در زرین‌پال"),
                      ("چطور فقط یه اینباند ترکیه رو انتخاب کنم که بروزرسانی خودکار همه رو برنگردونه", "انتخاب اینباندهای پنل"),
                      ("پنل هایمدال وایرگارد وصل کنم", "HEIMDALL"),
                      ("نرخ خودکار پرمیوم چطور کار میکنه", "نرخ خودکار پرمیوم")]:
        assert needle in _clean(_sel(q)), q


def test_every_topic_is_reachable_by_its_most_specific_keyword():
    """هر بخش باید با یکی از کلیدواژه‌هایِ خاصِ خودش پیدا شود (به‌جز بخش‌هایِ تکه‌تکهٔ قدیمیِ بدونِ کلیدواژهٔ خاص)."""
    topics = G._GG_AI_TOPICS
    df = KB._df_table(topics)
    missed, tested = [], 0
    for idx, (kws, text) in enumerate(topics):
        cands = [KB._PUNCT.sub(" ", KB.norm(k)).strip() for k in kws]
        cands = [k for k in cands if len(k) >= 5 and k not in KB._STOP and df.get(k, 99) <= 2]
        if not cands:
            continue
        tested += 1
        kw = min(cands, key=lambda k: (df[k], -len(k)))
        scores = dict((i, sc) for sc, i in KB.topic_scores(kw, topics))
        top3 = [i for _, i in KB.topic_scores(kw, topics)[:3]]
        if idx not in top3 and scores.get(idx, 0) < KB.STRONG:
            missed.append((idx, kw))
    assert tested > 60 and len(missed) <= max(3, int(0.05 * tested)), missed


def test_budget_and_topic_cap_are_respected():
    q = "کانفیگ دستی ربات همکار پنل VPN درگاه زرین پال کیف پول تست رایگان شرط بندی گزارش"
    s = KB.select_relevant_topics(q, G._GG_AI_TOPICS, char_budget=3000)
    assert s.count("\n\n") + 1 <= KB.MAX_TOPICS
    first = s.split("\n\n")[0]
    assert len(s) <= 3000 or len(first) > 3000                                          # فقط اولین بخش اجازهٔ تجاوز دارد


# ───────── اتصال به سامانه ─────────

@pytest.mark.asyncio
async def test_system_prompt_has_no_markdown_or_marks_but_keeps_facts(monkeypatch):
    async def gp(key, default=""):
        return {"group_guard_ai_custom_knowledge": "نکتهٔ **سفارشیِ** مدیر", "group_guard_ai_prompt_mode": "smart"}.get(key, default)
    monkeypatch.setattr(G, "get_pset", gp)
    prompt = await G._build_ai_system_prompt("کانفیگ دستی وایرگارد رو چطور بفروشم")
    assert "**" not in prompt and not MARKS.search(prompt)
    assert "انبار کانفیگ‌های دستی" in prompt and "سفارشی مدیر" in prompt
    full = await G._build_ai_system_prompt("")                                          # بدونِ سوال: همهٔ دانش
    assert "**" not in full and not MARKS.search(full) and "بازی‌های گروهی ربات" in full


@pytest.mark.asyncio
async def test_ask_ai_output_is_cleaned_in_router_path(monkeypatch):
    class R:
        routes = [1]

        def configured(self):
            return True

        async def chat(self, system, question, max_tokens=600):
            assert "**" not in system
            return "**مسیر:** پنل مدیریت ← `انبار`\n* اول\n<think>x</think>", {}, "fake"
    import ai_router
    monkeypatch.setattr(ai_router, "get_router", lambda: R())

    async def gp(key, default=""):
        return default
    monkeypatch.setattr(G, "get_pset", gp)
    out = await G._ask_ai("کانفیگ دستی؟")
    assert out == "مسیر: پنل مدیریت ← انبار\n• اول"


@pytest.mark.asyncio
async def test_answer_sent_to_group_is_clean_even_if_cached_dirty(monkeypatch):
    sent = []

    async def edit_text(t):
        sent.append(t)
    wait = SimpleNamespace(edit_text=edit_text)

    async def reply(t, **kw):
        sent.append(t)
        return wait
    msg = SimpleNamespace(text="بات ساز کانفیگ دستی؟", reply=reply, from_user=SimpleNamespace(id=1), reply_to_message=None,
                          chat=SimpleNamespace(id=-1))

    async def gp(key, default=""):
        return {"group_guard_ai_ratelimit_enabled": "off"}.get(key, default)
    monkeypatch.setattr(G, "get_pset", gp)
    dirty = G._ai_cache_key("بات ساز کانفیگ دستی؟")
    G._AI_CACHE[dirty] = (__import__("time").monotonic(), "**جواب** با `کد`")
    await G._answer_ai_question(msg, "بات ساز کانفیگ دستی؟")
    assert sent[-1] == "🤖 جواب با کد"


def test_new_topics_2026_10e_are_findable_and_clean():
    from platform_ai_kb import TOPICS_2026_10E, select_relevant_topics
    assert TOPICS_2026_10E and all("**" not in t for _, t in TOPICS_2026_10E)
    out = select_relevant_topics("چطور نتایج تستی قرعه کشی رو پاک کنم؟", TOPICS_2026_10E)
    assert "پاک‌کردن کل تاریخچه" in out
    out = select_relevant_topics("رنگ دکمه کارت به کارت اعمال نمیشه", TOPICS_2026_10E)
    assert "۶۴ حرف" in out


def test_group_guard_registers_new_topics():
    import platform_group_guard as g
    assert any("پاک‌کردن کل تاریخچه" in t for _, t in g._GG_AI_TOPICS)
