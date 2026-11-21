"""PATCH_PREMIUM_EMOJI_REGEX_V1 — جایگزینیِ ایموجیِ پرمیوم با regexِ واحد (به‌جایِ O(طول×کلید)) که رویدادلوپ را نبندد؛ خروجی باید یکسان بماند."""
import random
import time

import premium_emoji_dynamic as P


def _ref_plain(text, mapping):
    """پیاده‌سازیِ مرجعِ قدیمی (کاراکتر به کاراکتر)."""
    keys = sorted(mapping.keys(), key=len, reverse=True)
    out, ents, pos, i, hit = [], [], 0, 0, False
    while i < len(text):
        m = next((k for k in keys if k and text.startswith(k, i)), None)
        if m:
            pid, disp = mapping[m]
            ln = P._utf16_len(disp)
            ents.append((pos, ln, pid))
            out.append(disp)
            pos += ln
            i += len(m)
            hit = True
        else:
            out.append(text[i])
            pos += P._utf16_len(text[i])
            i += 1
    return ("".join(out), ents) if hit else (text, [])


def test_plain_apply_map_matches_reference_on_random_inputs():
    rnd = random.Random(7)
    alpha = ["😀", "❤️", "🔥", "⭐", "a", "b", "سلام", " ", "\n", "👍", "👍🏽", "x"]
    pool = ["😀", "❤️", "🔥", "⭐", "👍", "👍🏽", "❤", "ab", "سلام"]
    for _ in range(600):
        mapping = {k: (f"id{i}", "★") for i, k in enumerate(rnd.sample(pool, rnd.randint(0, 6)))}
        text = "".join(rnd.choice(alpha) for _ in range(rnd.randint(0, 40)))
        got, ents = P.plain_apply_map(text, mapping)
        assert (got, [(e.offset, e.length, e.custom_emoji_id) for e in ents]) == _ref_plain(text, mapping)


def test_longest_key_wins_and_html_formatting_is_kept():
    mapping = {"👍": ("A", "★"), "👍🏽": ("B", "☆")}
    txt, ents = P.hx_dynamic("<b>سلام</b> 👍🏽 و 👍", mapping)
    assert [e.custom_emoji_id for e in ents if e.type == "custom_emoji"] == ["B", "A"]
    assert any(e.type == "bold" for e in ents)
    assert P.hx_dynamic("بدونِ ایموجی", mapping) == ("بدونِ ایموجی", None)
    assert P.hx_dynamic("x", {}) == ("x", None)


def test_big_mapping_and_long_text_do_not_block():
    mapping = {chr(0x1F300 + i): (f"id{i}", "★") for i in range(400)}
    text = "hello world سلام <b>x</b> 😀 " * 1500
    t = time.perf_counter()
    P.hx_dynamic(text, mapping)
    P.plain_apply_map(text, mapping)
    assert time.perf_counter() - t < 1.0                 # قبلاً چند ثانیه (رویدادلوپ گیر می‌کرد)
