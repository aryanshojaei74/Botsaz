"""
ai_router.py — PATCH_AI_ROUTER_V1

روترِ چندکلیدیِ چندارائه‌دهنده برایِ مدل‌هایِ «رایگان» (همه OpenAI-compatible).
هر درخواست از یک مسیر (ارائه‌دهنده+کلید) شروع می‌شود؛ اگر جواب نداد (۴۲۹، ۵xx، تایم‌اوت، کلیدِ باطل، …)
همان لحظه مسیرِ بعدی امتحان می‌شود و مسیرِ خراب مدتی «خنک» می‌شود تا درخواست‌هایِ بعدی وقتشان را هدر ندهد.
مسیرها به‌صورتِ round-robin بینِ همهٔ مسیرهایِ سالم می‌چرخند تا سهمیهٔ رایگانِ هر کلید یکنواخت مصرف شود.

پیکربندی (فایلِ .env سرور) — متغیرِ GROUP_GUARD_AI_ROUTES، جداشده با کاما:

    GROUP_GUARD_AI_ROUTES=groq=gsk_aaa,groq=gsk_bbb,openrouter=sk-or-ccc,gemini=AIza...,cerebras=csk-...,mistral=xxxx

هر مورد:  نامِ‌ارائه‌دهنده[:مدل]=کلید
  • نام‌هایِ آماده: groq, cerebras, gemini, openrouter, mistral, sambanova, nvidia, github
  • مدلِ دلخواه: groq:llama-3.1-8b-instant=gsk_...
  • ارائه‌دهندهٔ دلخواه (هر APIِ OpenAI-compatible):  https://host/v1|نامِ-مدل=کلید
یک ارائه‌دهنده می‌تواند چند کلید داشته باشد (چند خط/مورد با همان نام).
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

PRESETS: dict[str, tuple[str, str]] = {
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "cerebras": ("https://api.cerebras.ai/v1", "llama-3.3-70b"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.0-flash"),
    "openrouter": ("https://openrouter.ai/api/v1", "meta-llama/llama-3.3-70b-instruct:free"),
    "mistral": ("https://api.mistral.ai/v1", "mistral-small-latest"),
    "sambanova": ("https://api.sambanova.ai/v1", "Meta-Llama-3.3-70B-Instruct"),
    "nvidia": ("https://integrate.api.nvidia.com/v1", "meta/llama-3.3-70b-instruct"),
    "github": ("https://models.inference.ai.azure.com", "gpt-4o-mini"),
}

COOL_429 = 60           # ثانیه (اگر Retry-After نبود)
COOL_AUTH = 3600        # کلیدِ باطل/بدونِ دسترسی
COOL_BAD = 600          # مدل/درخواستِ نامعتبر برایِ این مسیر
COOL_5XX = 30
ATTEMPT_TIMEOUT = 18.0


@dataclass
class Route:
    name: str
    base_url: str
    model: str
    key: str
    cool_until: float = 0.0
    fails: int = 0
    ok: int = 0

    @property
    def label(self) -> str:
        return f"{self.name}:{self.model}#{self.key[-4:]}"


def parse_routes(raw: str) -> list[Route]:
    out: list[Route] = []
    for item in (raw or "").replace("\n", ",").replace(";", ",").split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        spec, key = item.split("=", 1)
        spec, key = spec.strip(), key.strip()
        if not key:
            continue
        if "|" in spec:                                   # https://host/v1|model
            base, model = spec.split("|", 1)
            out.append(Route("custom", base.strip().rstrip("/"), model.strip(), key))
            continue
        name, _, model = spec.partition(":")
        name = name.strip().lower()
        if name not in PRESETS:
            logger.warning("ai_router: نامِ ارائه‌دهندهٔ ناشناس نادیده گرفته شد: %r", name)
            continue
        base, default_model = PRESETS[name]
        out.append(Route(name, base, model.strip() or default_model, key))
    return out


@dataclass
class Router:
    routes: list[Route] = field(default_factory=list)
    _rr: int = 0
    transport: object | None = None          # برایِ تست (httpx.MockTransport)

    def configured(self) -> bool:
        return bool(self.routes)

    def _order(self) -> list[Route]:
        """مسیرهایِ سالم به‌ترتیبِ round-robin، بعدش مسیرهایِ خنک (آخرین شانس)."""
        n = len(self.routes)
        if not n:
            return []
        start = self._rr % n
        self._rr += 1
        rot = self.routes[start:] + self.routes[:start]
        now = time.monotonic()
        ready = [r for r in rot if r.cool_until <= now]
        cooling = sorted((r for r in rot if r.cool_until > now), key=lambda r: r.cool_until)
        return ready + cooling

    async def chat(self, system: str, user: str, *, max_tokens: int = 600,
                   temperature: float = 0.0) -> tuple[str, dict, str]:
        """(متن، usage، برچسبِ مسیر). اگر همهٔ مسیرها شکست خوردند RuntimeError."""
        import httpx
        errors: list[str] = []
        payload_base = {"temperature": temperature, "max_tokens": max_tokens,
                        "messages": [{"role": "system", "content": system},
                                     {"role": "user", "content": user}]}
        kw = {"transport": self.transport} if self.transport is not None else {}
        async with httpx.AsyncClient(timeout=httpx.Timeout(ATTEMPT_TIMEOUT, connect=5.0),
                                     headers={"Accept-Encoding": "identity"}, **kw) as client:
            for r in self._order():
                try:
                    resp = await client.post(
                        r.base_url + "/chat/completions", json={**payload_base, "model": r.model},
                        headers={"Authorization": f"Bearer {r.key}", "Content-Type": "application/json"})
                except (httpx.TimeoutException, httpx.HTTPError) as e:
                    r.fails += 1
                    r.cool_until = time.monotonic() + COOL_5XX
                    errors.append(f"{r.label}: {type(e).__name__}")
                    continue
                st = resp.status_code
                if st == 200:
                    try:
                        data = resp.json()
                        text = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
                    except Exception:                       # noqa: BLE001
                        data, text = {}, ""
                    if text:
                        r.ok += 1
                        return text, (data.get("usage") or {}), r.label
                    r.cool_until = time.monotonic() + COOL_5XX
                    errors.append(f"{r.label}: پاسخِ خالی")
                    continue
                r.fails += 1
                if st == 429:
                    try:
                        wait = min(600, max(5, int(float(resp.headers.get("retry-after", COOL_429)))))
                    except (TypeError, ValueError):
                        wait = COOL_429
                    r.cool_until = time.monotonic() + wait
                elif st in (401, 403):
                    r.cool_until = time.monotonic() + COOL_AUTH
                elif st in (400, 404, 413, 422):
                    r.cool_until = time.monotonic() + COOL_BAD
                else:
                    r.cool_until = time.monotonic() + COOL_5XX
                errors.append(f"{r.label}: HTTP {st}")
        raise RuntimeError("همهٔ مسیرهایِ هوش مصنوعی جواب ندادند: " + " | ".join(errors[-6:]))

    def status(self) -> str:
        now = time.monotonic()
        return "\n".join(
            f"{'🟢' if r.cool_until <= now else '🟠'} {r.label} — موفق {r.ok}، ناموفق {r.fails}"
            + (f"، خنک {int(r.cool_until - now)}ثانیه" if r.cool_until > now else "")
            for r in self.routes) or "هیچ مسیری تنظیم نشده."


_ROUTER: Router | None = None


def get_router() -> Router:
    """روترِ سراسری (از GROUP_GUARD_AI_ROUTES در محیط/.env)."""
    global _ROUTER
    if _ROUTER is None:
        raw = os.getenv("GROUP_GUARD_AI_ROUTES", "")
        try:
            from config import GROUP_GUARD_AI_ROUTES as _cfg
            raw = _cfg or raw
        except Exception:                                   # noqa: BLE001
            logger.debug("ai_router: خواندنِ GROUP_GUARD_AI_ROUTES از config ممکن نشد؛ از محیط")
        _ROUTER = Router(parse_routes(raw))
        logger.info("ai_router: %d مسیر بارگذاری شد", len(_ROUTER.routes))
    return _ROUTER
