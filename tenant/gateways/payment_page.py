"""
payment_page.py — سایت واسط پرداخت ارز دیجیتال (WebApp داخل تلگرام).

جریان کار:
  ۱) ربات یک «چرک‌نویس شارژ» با کد پیگیری می‌سازد و لینک /pay/{tc} را به‌صورت
     WebApp به کاربر می‌دهد.
  ۲) کاربر در این صفحه مبلغ را دستی وارد می‌کند و واحد (تومان/دلار) را انتخاب می‌کند.
  ۳) با نرخ آنلاین (همان get_usd_to_toman_rate) مبلغ دلاری حساب می‌شود.
  ۴) فقط ارزهای کم‌کارمزد (USDT/TON/...) نشان داده می‌شوند — نه بیت‌کوین.
  ۵) با انتخاب ارز، پرداخت مستقیم روی نوپیمنت ساخته و آدرس واریز نشان داده می‌شود.
  ۶) IPN نوپیمنت (همان /nowpay/ipn موجود) کیف پول را شارژ می‌کند.

این ماژول مستقل است؛ جدول draft خودش را در اولین استفاده می‌سازد تا نیازی به
دست‌کاری models.py نباشد.

# PATCH_MARKER: crypto_pay_ux_v1 (کپی مبلغ + ارز پیشنهادی سبز + مهلت ۳۵ دقیقه + چک فعال هر ۱ دقیقه)
"""
from __future__ import annotations

import asyncio
import datetime
import html as _html
import secrets
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import (
    BigInteger, Column, DateTime, Integer, Numeric, String, select,
)

from config import Session, engine, WEBHOOK_BASE
from models import Base, CryptoCharge

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)
# نکته: crypto_pay و get_setting داخل توابع import می‌شوند (lazy) تا از
# import حلقوی با tenant/__init__.py جلوگیری شود.

pay_router = APIRouter()

# دقیقهٔ انقضای یک چرک‌نویس شارژ
_DRAFT_TTL_MIN = 60


# ─────────── مدل چرک‌نویس (خودساخت، بدون دست‌زدن به models.py) ───────────
# === PATCH_KYC_CAP_V1 ===
class KycCapExceeded(Exception):
    """سقفِ شارژِ احرازِ هویت رد شده؛ پیامِ فارسی در args[0]."""


class ChargeDraft(Base):
    __tablename__ = "charge_drafts"

    id = Column(Integer, primary_key=True)
    tracking_code = Column(String(20), unique=True, index=True, nullable=False)
    bot_id = Column(Integer, index=True, nullable=False)
    user_telegram_id = Column(BigInteger, index=True, nullable=False)
    # نوع شارژ: 'tenant' (کیف پول فروشنده) یا 'platform' (کیف پول خودِ بات‌ساز)
    kind = Column(String(20), default="tenant")
    # وقتی کاربر مبلغ را وارد کرد، این‌ها پر می‌شوند:
    amount_toman = Column(Numeric(14, 0), default=0)
    amount_usd = Column(Numeric(12, 2), default=0)
    pay_currency = Column(String(20), default="")
    np_payment_id = Column(String(40), default="")
    order_id = Column(String(80), default="")
    status = Column(String(20), default="new")  # new/priced/waiting/paid/expired
    created_at = Column(DateTime, default=datetime.datetime.now)


_ready = False


async def ensure_table() -> None:
    global _ready
    if _ready:
        return
    async with engine.begin() as conn:
        await conn.run_sync(ChargeDraft.__table__.create, checkfirst=True)
        # اگر جدول از نسخهٔ قبلی بدون ستون kind ساخته شده، اضافه‌اش کن
        try:
            from sqlalchemy import text
            await conn.execute(text(
                "ALTER TABLE charge_drafts ADD COLUMN kind VARCHAR(20) DEFAULT 'tenant'"))
        except Exception:
            logger.debug("ستونِ kind از قبل وجود دارد (این خطا طبیعی است)")  # ستون از قبل هست
    _ready = True


def gen_tracking_code() -> str:
    """کد پیگیری عددی ۱۲ رقمی (مثل نمونهٔ اسکرین)."""
    return "".join(secrets.choice("0123456789") for _ in range(12))


async def create_draft(bot_id: int, user_telegram_id: int,
                       amount_toman: int = 0, amount_usd: float = 0,
                       kind: str = "tenant") -> str:
    """
    یک چرک‌نویس می‌سازد و کد پیگیری برمی‌گرداند. ربات این را صدا می‌زند.
    kind: 'tenant' (کیف پول فروشنده) یا 'platform' (کیف پول خودِ بات‌ساز).
    اگر amount از قبل مشخص باشد (مبلغ در چت وارد شده)، وضعیت 'priced' می‌شود
    و سایت واسط مستقیم به مرحلهٔ انتخاب ارز می‌رود.
    """
    await ensure_table()
    tc = gen_tracking_code()
    status = "priced" if (amount_usd and amount_usd > 0) else "new"
    async with Session() as s:
        s.add(ChargeDraft(tracking_code=tc, bot_id=bot_id,
                          user_telegram_id=user_telegram_id,
                          amount_toman=int(amount_toman or 0),
                          amount_usd=float(amount_usd or 0),
                          kind=kind, status=status))
        await s.commit()
    return tc


async def _get_draft(tc: str) -> ChargeDraft | None:
    await ensure_table()
    async with Session() as s:
        return await s.scalar(
            select(ChargeDraft).where(ChargeDraft.tracking_code == tc))


# ─────────────────────── صفحهٔ HTML ───────────────────────
@pay_router.get("/pay/{tc}", response_class=HTMLResponse)
async def pay_page(tc: str):
    draft = await _get_draft(tc)
    if not draft:
        return HTMLResponse(_page_error("فاکتور پیدا نشد یا منقضی شده."),
                            status_code=404)
    if draft.status == "paid":
        return HTMLResponse(_page_error("این فاکتور قبلاً پرداخت شده. ✅"))
    # اگر مبلغ از قبل در چت مشخص شده، صفحه مستقیم به انتخاب ارز می‌رود
    preset = draft.status in ("priced", "waiting") and float(draft.amount_usd or 0) > 0
    return HTMLResponse(_render_page(
        tc,
        preset=preset,
        amount_toman=int(draft.amount_toman or 0),
        amount_usd=float(draft.amount_usd or 0),
    ))


def _page_error(msg: str) -> str:
    return f"""<!DOCTYPE html><html lang="fa" dir="rtl"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>پرداخت</title><style>
body{{font-family:-apple-system,Tahoma,sans-serif;background:#0f1115;color:#e9edf3;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}}
.box{{padding:24px}}</style></head><body><div class="box"><h2>⚠️</h2><p>{_html.escape(msg)}</p></div>
<script src="/miniapp/_assets/tgwebapp.js"></script><script>window.Telegram||document.write('<scr'+'ipt src="https://telegram.org/js/telegram-web-app.js"></scr'+'ipt>')</script>
<script>try{{Telegram.WebApp.ready()}}catch(e){{}}</script></body></html>"""


def _render_page(tc: str, preset: bool = False,
                 amount_toman: int = 0, amount_usd: float = 0) -> str:
    """صفحهٔ اصلی؛ منطق در جاوااسکریپت با فراخوانی APIهای زیر.

    اگر preset=True، یعنی مبلغ از قبل در چت مشخص شده و صفحه مستقیم به
    مرحلهٔ انتخاب ارز می‌رود.
    """
    import json as _json
    preset_js = _json.dumps({
        "preset": bool(preset),
        "amount_toman": int(amount_toman or 0),
        "amount_usd": float(amount_usd or 0),
    })
    return ("""<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>پرداخت با ارز دیجیتال</title>
<script src="/miniapp/_assets/tgwebapp.js"></script><script>window.Telegram||document.write('<scr'+'ipt src="https://telegram.org/js/telegram-web-app.js"></scr'+'ipt>')</script>
<script>window.__PRESET__ = """ + preset_js + """;</script>
<style>
  :root{
    --bg: var(--tg-theme-bg-color, #0f1115);
    --card: var(--tg-theme-secondary-bg-color, #1a1d24);
    --text: var(--tg-theme-text-color, #e9edf3);
    --hint: var(--tg-theme-hint-color, #8b93a2);
    --accent: var(--tg-theme-button-color, #3d8bff);
    --accent-text: var(--tg-theme-button-text-color, #fff);
    --line: rgba(255,255,255,.08);
    --ok:#2ec16b; --warn:#e0483d; --gold:#e0a83d;
  }
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Vazirmatn","Segoe UI",Tahoma,sans-serif;
    background:var(--bg);color:var(--text);padding:16px;line-height:1.7}
  .head{text-align:center;margin:6px 0 18px}
  .head .coin{font-size:32px}
  .head h1{font-size:17px;margin:6px 0 2px;font-weight:700}
  .head .tc{font-size:12px;color:var(--hint);direction:ltr;font-family:ui-monospace,monospace;
    display:inline-flex;align-items:center;gap:6px;cursor:pointer;user-select:none}
  .head .tc .copy-tc{background:var(--line);color:var(--text);border:none;border-radius:6px;
    padding:2px 7px;font-family:inherit;font-size:11px;cursor:pointer}
  .card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px;margin-bottom:14px}
  .label{font-size:13px;color:var(--hint);margin:0 0 10px}
  .seg{display:flex;gap:8px;margin-bottom:14px}
  .seg button{flex:1;padding:12px;border:1.5px solid var(--line);background:transparent;
    color:var(--text);border-radius:12px;font-family:inherit;font-size:15px;font-weight:700;cursor:pointer;transition:.15s}
  .seg button.sel{border-color:var(--accent);background:rgba(61,139,255,.12)}
  .amount-in{width:100%;padding:15px;border:1.5px solid var(--line);background:var(--bg);
    color:var(--text);border-radius:12px;font-family:inherit;font-size:18px;font-weight:700;
    text-align:center;direction:ltr;margin-bottom:8px}
  .amount-in:focus{outline:none;border-color:var(--accent)}
  .conv{text-align:center;font-size:13px;color:var(--hint);min-height:20px;margin-bottom:6px}
  .conv b{color:var(--gold)}
  .btn{width:100%;border:none;border-radius:14px;padding:16px;font-size:16px;font-weight:700;
    font-family:inherit;cursor:pointer;background:var(--accent);color:var(--accent-text);transition:.15s}
  .btn:disabled{opacity:.45}
  .btn:active:not(:disabled){transform:scale(.99)}
  .cur{display:flex;align-items:center;justify-content:space-between;width:100%;background:transparent;
    border:1.5px solid var(--line);color:var(--text);border-radius:12px;padding:14px;margin-bottom:10px;
    font-size:15px;font-family:inherit;cursor:pointer;transition:.15s}
  .cur.sel{border-color:var(--accent);background:rgba(61,139,255,.08)}
  .cur.dis{opacity:.4;pointer-events:none}
  .cur.reco{border-color:var(--ok);background:rgba(46,193,107,.10)}
  .cur.reco.sel{border-color:var(--ok);background:rgba(46,193,107,.16)}
  .cur .left{font-weight:700}
  .cur .badge{font-size:11px;color:var(--hint);direction:ltr}
  .cur .warnmin{font-size:11px;color:var(--warn)}
  .cur .reco-badge{font-size:10px;color:var(--ok);border:1px solid var(--ok);border-radius:6px;
    padding:1px 6px;margin-inline-start:6px}
  .note{font-size:12px;color:var(--hint);text-align:center;margin-top:12px}
  .pay-amt-row{display:flex;align-items:center;justify-content:center;gap:8px;margin:6px 0}
  .pay-amt{font-size:22px;font-weight:800;direction:ltr;text-align:center}
  .copy-amt{background:var(--line);color:var(--text);border:none;border-radius:8px;
    padding:6px 10px;font-family:inherit;font-size:12px;cursor:pointer}
  .warn-exact{font-size:12.5px;color:var(--warn);text-align:center;margin:4px 0 10px;line-height:1.6}
  .addr{direction:ltr;word-break:break-all;background:var(--bg);border:1px dashed var(--line);
    border-radius:12px;padding:12px;font-family:ui-monospace,monospace;font-size:13px;margin:10px 0}
  .copy{display:block;margin:0 auto;background:var(--line);color:var(--text);border:none;border-radius:10px;
    padding:10px 16px;font-family:inherit;font-size:13px;cursor:pointer}
  .spin{display:inline-block;width:18px;height:18px;border:2px solid var(--line);
    border-top-color:var(--accent);border-radius:50%;animation:s .7s linear infinite;vertical-align:middle}
  @keyframes s{to{transform:rotate(360deg)}}
  .status{margin-top:14px;padding:12px;border-radius:12px;text-align:center;font-weight:700;font-size:14px}
  .status.wait{background:rgba(224,168,61,.12);color:var(--gold)}
  .status.done{background:rgba(46,193,107,.12);color:var(--ok)}
  .status.cancel{background:rgba(224,72,61,.12);color:var(--warn)}
  .countdown{text-align:center;font-size:13px;color:var(--hint);margin-top:8px;direction:ltr}
  .countdown b{color:var(--text)}
  .hidden{display:none}
</style>
</head>
<body>
  <div class="head">
    <div class="coin">🪙</div>
    <h1>شارژ با ارز دیجیتال</h1>
    <div class="tc" id="tcLabel" onclick="copyTC()">
      <span id="tcText"></span>
      <button type="button" class="copy-tc" onclick="event.stopPropagation();copyTC()">📋 کپی</button>
    </div>
  </div>

  <!-- مرحله ۱: مبلغ -->
  <div id="step1" class="card">
    <div class="label">۱. واحد و مبلغ را وارد کن</div>
    <div class="seg">
      <button id="segToman" class="sel" onclick="setUnit('toman')">💵 تومان</button>
      <button id="segUsd" onclick="setUnit('usd')">💲 دلار</button>
    </div>
    <input id="amountIn" class="amount-in" inputmode="decimal" placeholder="مبلغ را وارد کن" oninput="onAmount()">
    <div class="conv" id="conv"></div>
    <button id="toStep2" class="btn" disabled onclick="goStep2()">ادامه</button>
    <div class="note" id="rateNote"></div>
  </div>

  <!-- مرحله ۲: انتخاب ارز -->
  <div id="step2" class="card hidden">
    <div class="label">۲. ارز پرداخت را انتخاب کن</div>
    <div id="curList"><div style="text-align:center;padding:16px"><span class="spin"></span></div></div>
    <button id="createBtn" class="btn" disabled onclick="createPayment()">ساخت آدرس پرداخت</button>
    <div class="note">💡 برای مبالغ کم، USDT-TRC20 یا TON کمترین کارمزد را دارند.</div>
  </div>

  <!-- مرحله ۳: آدرس واریز -->
  <div id="step3" class="card hidden">
    <div class="label">۳. این مبلغ را به آدرس زیر واریز کن</div>
    <div class="pay-amt-row">
      <div class="pay-amt" id="payAmt"></div>
      <button class="copy-amt" onclick="copyAmt()">📋 کپی مبلغ</button>
    </div>
    <div class="warn-exact">⚠️ دقیقاً همین مقدار را ارسال کن؛ مبلغ کمتر پذیرفته نمی‌شود و پرداخت تأیید نخواهد شد.</div>
    <div class="addr" id="payAddr"></div>
    <button class="copy" onclick="copyAddr()">📋 کپی آدرس</button>
    <div class="status wait" id="statusBox">⏳ در انتظار دریافت پرداخت…</div>
    <div class="countdown" id="countdownBox"></div>
    <button class="btn" id="checkNowBtn" onclick="checkNow()">🔄 بررسی پرداخت</button>
    <div class="note">بعد از تأیید شبکه، کیف پولت خودکار شارژ می‌شود. اگر واریز کردی و هنوز تأیید نشده، روی «بررسی پرداخت» بزن.</div>
  </div>

<script>
const tg = window.Telegram.WebApp;
try{ tg.ready(); tg.expand(); }catch(e){}

const TC = location.pathname.split('/').pop();
document.getElementById('tcText').textContent = 'کد پیگیری: ' + TC;

function copyTC(){
  navigator.clipboard.writeText(TC).catch(()=>{
    const ta=document.createElement('textarea'); ta.value=TC;
    document.body.appendChild(ta); ta.select();
    try{ document.execCommand('copy'); }catch(e){}
    document.body.removeChild(ta);
  });
  try{ tg.HapticFeedback.notificationOccurred('success'); }catch(e){}
}

let unit = 'toman';
let rate = 0;            // نرخ تومان به ازای ۱ دلار
let amountToman = 0;
let amountUsd = 0;
let selectedCur = null;

// نرخ آنلاین را از سرور بگیر
async function loadRate(){
  try{
    const r = await fetch(`/pay/api/rate/${TC}`);
    const d = await r.json();
    if(d.rate){ rate = d.rate;
      document.getElementById('rateNote').innerHTML =
        `💱 نرخ لحظه‌ای: <b>${d.rate.toLocaleString('en-US')}</b> تومان = ۱ دلار`;
    } else {
      document.getElementById('rateNote').textContent = 'دریافت نرخ ناموفق بود.';
    }
  }catch(e){}
}

function setUnit(u){
  unit = u;
  document.getElementById('segToman').classList.toggle('sel', u==='toman');
  document.getElementById('segUsd').classList.toggle('sel', u==='usd');
  document.getElementById('amountIn').placeholder = u==='toman' ? 'مبلغ به تومان' : 'مبلغ به دلار';
  onAmount();
}

function onAmount(){
  let raw = document.getElementById('amountIn').value
    .replace(/[۰-۹]/g, d => '۰۱۲۳۴۵۶۷۸۹'.indexOf(d))
    .replace(/,/g,'').trim();
  const num = parseFloat(raw);
  const conv = document.getElementById('conv');
  const btn = document.getElementById('toStep2');
  if(!num || num <= 0 || !rate){ conv.textContent=''; btn.disabled=true; return; }

  if(unit === 'toman'){
    amountToman = Math.round(num);
    amountUsd = +(amountToman / rate).toFixed(2);
    conv.innerHTML = `≈ <b>$${amountUsd}</b>`;
  } else {
    amountUsd = +num.toFixed(2);
    amountToman = Math.round(amountUsd * rate);
    conv.innerHTML = `≈ <b>${amountToman.toLocaleString('en-US')}</b> تومان`;
  }
  // حداقل ۰.۵ دلار
  btn.disabled = amountUsd < 0.5;
  if(amountUsd > 0 && amountUsd < 0.5){
    conv.innerHTML += ' — حداقل ۰.۵ دلار';
  }
}

async function goStep2(){
  // مبلغ را روی سرور ثبت کن
  const btn = document.getElementById('toStep2');
  btn.disabled = true; btn.innerHTML = '<span class="spin"></span>';
  try{
    const r = await fetch(`/pay/api/set-amount/${TC}`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({amount_toman: amountToman, amount_usd: amountUsd})
    });
    if(!r.ok) throw new Error();
    document.getElementById('step1').classList.add('hidden');
    document.getElementById('step2').classList.remove('hidden');
    loadCurrencies();
  }catch(e){
    btn.disabled=false; btn.textContent='ادامه';
    tg.showAlert && tg.showAlert('خطا. دوباره تلاش کن.');
  }
}

async function loadCurrencies(){
  const list = document.getElementById('curList');
  try{
    const r = await fetch(`/pay/api/currencies/${TC}`);
    const arr = await r.json();
    list.innerHTML = '';
    // اولین ارزِ کافی در ترتیب اولویتِ کم‌کارمزدها (که سرور فرستاده) = کمترین نرخ جابه‌جایی
    const recoCode = (arr.find(c => c.enough) || {}).code;
    arr.forEach(c => {
      const el = document.createElement('button');
      const isReco = c.enough && c.code === recoCode;
      el.className = 'cur' + (c.enough ? '' : ' dis') + (isReco ? ' reco' : '');
      const badge = c.enough
        ? `<span class="badge">حداقل: $${c.min_usd}</span>`
        : `<span class="warnmin">زیر حداقل ($${c.min_usd})</span>`;
      const recoBadge = isReco ? '<span class="reco-badge">✅ پیشنهادی · کمترین کارمزد</span>' : '';
      el.innerHTML = `<span class="left">${c.label}${recoBadge}</span>${badge}`;
      if(c.enough){
        el.onclick = () => {
          document.querySelectorAll('.cur').forEach(x=>x.classList.remove('sel'));
          el.classList.add('sel');
          selectedCur = c.code;
          document.getElementById('createBtn').disabled = false;
        };
      }
      list.appendChild(el);
    });
    if(!arr.some(c=>c.enough)){
      list.insertAdjacentHTML('beforeend',
        '<div class="note" style="color:var(--warn)">هیچ ارزی برای این مبلغ در دسترس نیست. مبلغ بیشتری وارد کن.</div>');
    }
  }catch(e){
    list.innerHTML = '<div class="note" style="color:var(--warn)">خطا در دریافت ارزها.</div>';
  }
}

async function createPayment(){
  if(!selectedCur) return;
  const btn = document.getElementById('createBtn');
  btn.disabled = true; btn.innerHTML = '<span class="spin"></span>';
  try{
    const r = await fetch(`/pay/api/create/${TC}`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({currency: selectedCur})
    });
    const d = await r.json();
    if(!r.ok || !d.pay_address) throw new Error(d.error || 'خطا');
    document.getElementById('step2').classList.add('hidden');
    document.getElementById('step3').classList.remove('hidden');
    payAmountText = `${d.pay_amount} ${d.pay_currency.toUpperCase()}`;
    payAmountRaw = String(d.pay_amount);
    document.getElementById('payAmt').textContent = payAmountText;
    document.getElementById('payAddr').textContent = d.pay_address;
    pollStatus();
    startCountdown(35 * 60); // ۳۵ دقیقه مهلت واریز
  }catch(e){
    btn.disabled=false; btn.textContent='ساخت آدرس پرداخت';
    tg.showAlert && tg.showAlert(e.message || 'خطا در ساخت پرداخت');
  }
}

let payAmountText = '';
let payAmountRaw = '';

function copyAddr(){
  const a = document.getElementById('payAddr').textContent;
  navigator.clipboard.writeText(a).then(()=> tg.showAlert && tg.showAlert('آدرس کپی شد ✅'));
}

function copyAmt(){
  navigator.clipboard.writeText(payAmountRaw).then(()=> tg.showAlert && tg.showAlert('مبلغ کپی شد ✅ — دقیقاً همین مقدار را ارسال کن.'));
}

let statusTimer = null;
let countdownTimer = null;

function pollStatus(){
  const box = document.getElementById('statusBox');
  statusTimer = setInterval(async ()=>{
    try{
      const r = await fetch(`/pay/api/status/${TC}`);
      const d = await r.json();
      if(d.status === 'paid'){
        clearInterval(statusTimer);
        clearInterval(countdownTimer);
        box.className = 'status done';
        box.textContent = '✅ پرداخت تأیید شد! کیف پولت شارژ شد.';
        document.getElementById('countdownBox').textContent = '';
        try{ tg.HapticFeedback.notificationOccurred('success'); }catch(e){}
        setTimeout(()=>{ try{ tg.close(); }catch(e){} }, 3000);
      } else if(d.status === 'expired'){
        clearInterval(statusTimer);
        clearInterval(countdownTimer);
        showCancelled();
      }
    }catch(e){}
  }, 20000); // هر ۲۰ ثانیه چک کن (حتی اگر IPN نرسیده باشد، وضعیت مستقیماً از درگاه استعلام می‌شود)
}

async function checkNow(){
  const btn = document.getElementById('checkNowBtn');
  const box = document.getElementById('statusBox');
  const original = btn.textContent;
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> در حال بررسی...';
  try{
    const r = await fetch(`/pay/api/status/${TC}`);
    const d = await r.json();
    if(d.status === 'paid'){
      clearInterval(statusTimer);
      clearInterval(countdownTimer);
      box.className = 'status done';
      box.textContent = '✅ پرداخت تأیید شد! کیف پولت شارژ شد.';
      document.getElementById('countdownBox').textContent = '';
      btn.style.display = 'none';
      try{ tg.HapticFeedback.notificationOccurred('success'); }catch(e){}
      setTimeout(()=>{ try{ tg.close(); }catch(e){} }, 3000);
    } else if(d.status === 'expired'){
      clearInterval(statusTimer);
      clearInterval(countdownTimer);
      showCancelled();
    } else {
      btn.disabled = false;
      btn.textContent = original;
      try{ tg.HapticFeedback.notificationOccurred('warning'); }catch(e){}
      box.className = 'status wait';
      box.textContent = '⏳ هنوز پرداخت روی شبکه تأیید نشده — چند دقیقهٔ دیگر دوباره بررسی کن.';
      // پیام بررسی بعد از ۲۰ ثانیه پاک شود و به حالت «در انتظار» برگردد
      if(window._checkMsgTimer) clearTimeout(window._checkMsgTimer);
      window._checkMsgTimer = setTimeout(()=>{
        // فقط اگر هنوز پرداخت تأیید/لغو نشده، پیام را پاک کن
        if(box.className.indexOf('done') === -1 && box.className.indexOf('cancel') === -1){
          box.className = 'status wait';
          box.textContent = '⏳ در انتظار دریافت پرداخت…';
        }
      }, 20000);
    }
  }catch(e){
    btn.disabled = false;
    btn.textContent = original;
    tg.showAlert && tg.showAlert('خطا در بررسی وضعیت. دوباره تلاش کن.');
  }
}

function showCancelled(){
  const box = document.getElementById('statusBox');
  box.className = 'status cancel';
  box.textContent = '❌ لغو شد. مهلت ۳۵ دقیقه‌ای برای واریز به پایان رسید.';
  document.getElementById('countdownBox').textContent = '';
  try{ tg.HapticFeedback.notificationOccurred('error'); }catch(e){}
}

function startCountdown(totalSeconds){
  let remaining = totalSeconds;
  const box = document.getElementById('countdownBox');
  const tick = () => {
    if(remaining <= 0){
      clearInterval(countdownTimer);
      clearInterval(statusTimer);
      // اگر تا این لحظه تأیید نشده، درخواست را لغو کن
      fetch(`/pay/api/cancel/${TC}`, {method:'POST'}).catch(()=>{});
      showCancelled();
      return;
    }
    const m = Math.floor(remaining / 60);
    const s = remaining % 60;
    box.innerHTML = `⏱ مهلت باقی‌مانده: <b>${m}:${s.toString().padStart(2,'0')}</b>`;
    remaining--;
  };
  tick();
  countdownTimer = setInterval(tick, 1000);
}

// اگر مبلغ از قبل مشخص شده (از چت ربات)، مستقیم به انتخاب ارز برو
const PRESET = window.__PRESET__ || {preset:false};

async function init(){
  await loadRate();
  if(PRESET.preset && PRESET.amount_usd > 0){
    amountToman = PRESET.amount_toman;
    amountUsd = PRESET.amount_usd;
    // مرحلهٔ مبلغ را رد کن، مستقیم برو انتخاب ارز
    document.getElementById('step1').classList.add('hidden');
    document.getElementById('step2').classList.remove('hidden');
    loadCurrencies();
  }
}

init();
</script>
</body>
</html>""")


# ─────────────────────── APIها ───────────────────────
@pay_router.get("/pay/api/rate/{tc}")
async def api_rate(tc: str):
    draft = await _get_draft(tc)
    if not draft:
        return JSONResponse({"error": "not found"}, status_code=404)
    from tenant.gateways import crypto_pay
    kind = getattr(draft, "kind", "tenant") or "tenant"
    rate = 0
    if kind == "platform":
        from platform_bot import get_pset
        try:
            rate = float(await get_pset("platform_nowpay_toman_per_usd", "0") or "0")
        except ValueError:
            rate = 0
        if rate <= 0:
            brsapi_key = await get_pset("brsapi_key", "")
            rate = await crypto_pay.get_rate_smart(brsapi_key)
    else:
        from tenant import get_setting
        try:
            rate = float(await get_setting(draft.bot_id, "toman_per_usd", "0"))
        except ValueError:
            rate = 0
        if rate <= 0:
            brsapi_key = await get_setting(draft.bot_id, "brsapi_key", "")
            rate = await crypto_pay.get_rate_smart(brsapi_key)
    return JSONResponse({"rate": int(rate) if rate else 0})


@pay_router.post("/pay/api/set-amount/{tc}")
async def api_set_amount(tc: str, request: Request):
    draft = await _get_draft(tc)
    if not draft:
        return JSONResponse({"error": "not found"}, status_code=404)
    body = await request.json()
    amount_toman = int(body.get("amount_toman", 0) or 0)
    amount_usd = float(body.get("amount_usd", 0) or 0)
    if amount_usd < 0.5:
        return JSONResponse({"error": "مبلغ کم است"}, status_code=400)

    # === PATCH_KYC_CAP_V2 ===
    # اینجا اولین جایی است که مبلغ واقعاً مشخص می‌شود (کاربر در صفحهٔ وب
    # وارد می‌کند)، پس بررسیِ سقفِ احرازِ هویت باید همین‌جا باشد نه موقعِ
    # ساختِ draft که مبلغ هنوز صفر است. شارژِ خودِ پلتفرم مستثناست.
    if (getattr(draft, "kind", "tenant") or "tenant") != "platform" and draft.bot_id:
        try:
            from models import BotUser as _BU
            from tenant.shared.kyc import check_charge as _cc
            async with Session() as _s:
                _u = await _s.scalar(select(_BU).where(
                    _BU.bot_id == draft.bot_id,
                    _BU.telegram_id == draft.user_telegram_id))
            _ok, _why = await _cc(draft.bot_id, _u, amount_toman)
            if not _ok:
                return JSONResponse({"error": _why}, status_code=400)
        except Exception:
            logger.exception("kyc: بررسیِ سقف ناموفق بود (crypto)")

    async with Session() as s:
        d = await s.scalar(select(ChargeDraft).where(ChargeDraft.tracking_code == tc))
        d.amount_toman = amount_toman
        d.amount_usd = amount_usd
        d.status = "priced"
        await s.commit()
    return JSONResponse({"ok": True})


async def _get_api_key_for_draft(draft) -> str:
    """کلید API نوپیمنت را بر اساس نوع شارژ (platform/tenant) برمی‌گرداند."""
    kind = getattr(draft, "kind", "tenant") or "tenant"
    if kind == "platform":
        from platform_bot import get_pset
        return await get_pset("platform_nowpay_api_key", "")
    from tenant import get_setting
    return await get_setting(draft.bot_id, "nowpay_api_key", "")


@pay_router.get("/pay/api/currencies/{tc}")
async def api_currencies(tc: str):
    draft = await _get_draft(tc)
    if not draft or not draft.amount_usd:
        return JSONResponse([], status_code=200)
    import asyncio
    from tenant.gateways import crypto_pay
    api_key = await _get_api_key_for_draft(draft)
    amount_usd = float(draft.amount_usd)
    codes = crypto_pay.PREFERRED_CURRENCIES

    # همهٔ حداقل‌ها را موازی بگیر (نه پشت سر هم) تا صفحه سریع لود شود.
    # سقف کلی ۱۰ ثانیه تا صفحه هنگ نکند.
    async def _one(code):
        try:
            return code, await crypto_pay.get_min_amount(api_key, code, "usd")
        except Exception:
            return code, 0
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*[_one(c) for c in codes]), timeout=10.0)
    except asyncio.TimeoutError:
        results = [(c, 0) for c in codes]

    mins = dict(results)
    out = []
    for code in codes:
        raw_min = mins.get(code, 0)
        # حاشیهٔ امن ۲۰٪: نوپیمنت موقع ساخت پرداخت به‌خاطر نوسان قیمت،
        # حداقل کمی بالاتر از min-amount می‌خواهد. با این حاشیه کاربر
        # همیشه مبلغی می‌زند که واقعاً کار می‌کند.
        min_usd = round(raw_min * 1.2, 2) if raw_min else 0
        label = crypto_pay.CURRENCY_LABELS.get(code, code.upper())
        out.append({
            "code": code,
            "label": label,
            "min_usd": min_usd,
            "enough": bool(min_usd) and amount_usd >= min_usd,
        })
    out.sort(key=lambda x: (not x["enough"]))
    return JSONResponse(out)


@pay_router.post("/pay/api/create/{tc}")
async def api_create(tc: str, request: Request):
    draft = await _get_draft(tc)
    if not draft or not draft.amount_usd:
        return JSONResponse({"error": "فاکتور نامعتبر"}, status_code=404)
    body = await request.json()
    currency = (body.get("currency") or "").lower()

    from tenant.gateways import crypto_pay
    if currency not in crypto_pay.PREFERRED_CURRENCIES:
        return JSONResponse({"error": "ارز نامعتبر"}, status_code=400)

    kind = getattr(draft, "kind", "tenant") or "tenant"

    # کلید API و آدرس IPN بر اساس نوع شارژ فرق می‌کند
    if kind == "platform":
        from platform_bot import get_pset
        api_key = await get_pset("platform_nowpay_api_key", "")
        # order_id باید با plt_ شروع شود تا IPN پلتفرم آن را بشناسد
        order_id = f"plt_{draft.user_telegram_id}_{tc}"
        ipn_url = f"{WEBHOOK_BASE}/nowpay/platform/ipn" if WEBHOOK_BASE else ""
    else:
        from tenant import get_setting
        api_key = await get_setting(draft.bot_id, "nowpay_api_key", "")
        order_id = f"bs-{draft.bot_id}-{draft.user_telegram_id}-{tc}"
        ipn_url = f"{WEBHOOK_BASE}/nowpay/ipn" if WEBHOOK_BASE else ""

    if not api_key:
        return JSONResponse({"error": "درگاه تنظیم نشده"}, status_code=400)

    payment = await crypto_pay.create_payment(
        api_key=api_key,
        price_amount_usd=float(draft.amount_usd),
        pay_currency=currency,
        order_id=order_id,
        ipn_callback_url=ipn_url,
        order_description=f"شارژ کیف پول - {int(draft.amount_toman):,} تومان",
    )
    if not payment or payment.get("_error") or not payment.get("pay_address"):
        msg = (payment or {}).get("message", "") if payment else ""
        low = msg.lower()
        if "too small" in low or "less than min" in low or "minimal" in low:
            # حداقل واقعی این ارز از مبلغ کاربر بیشتر است
            user_msg = ("مبلغ برای این ارز کافی نیست. لطفاً مبلغ را بیشتر کن "
                        "یا ارز دیگری (مثل TON یا LTC) انتخاب کن.")
        elif "amountto is too small" in low:
            user_msg = ("مبلغ برای این ارز خیلی کم است. مبلغ را بیشتر کن.")
        else:
            user_msg = ("این ارز الان در دسترس نیست. ارز دیگری انتخاب کن "
                        "یا مبلغ را بیشتر کن.")
        return JSONResponse({"error": user_msg}, status_code=400)

    # ثبت رکورد در انتظار — بسته به نوع، در جدول مربوطه
    async with Session() as s:
        if kind == "platform":
            from models import PlatformTxn
            s.add(PlatformTxn(
                user_telegram_id=draft.user_telegram_id,
                amount=0,  # تا تأیید IPN صفر می‌ماند
                kind="nowpay_pending",
                note=f"order_id={order_id}|toman={int(draft.amount_toman)}|"
                     f"usd={float(draft.amount_usd)}|invoice_id={payment.get('payment_id','')}",
            ))
        else:
            s.add(CryptoCharge(
                bot_id=draft.bot_id, user_telegram_id=draft.user_telegram_id,
                order_id=order_id, amount_toman=int(draft.amount_toman),
                usd_amount=float(draft.amount_usd), status="waiting"))
        d = await s.scalar(select(ChargeDraft).where(ChargeDraft.tracking_code == tc))
        d.pay_currency = currency
        d.np_payment_id = str(payment.get("payment_id", ""))
        d.order_id = order_id
        d.status = "waiting"
        await s.commit()

    return JSONResponse({
        "pay_address": payment.get("pay_address"),
        "pay_amount": payment.get("pay_amount"),
        "pay_currency": payment.get("pay_currency", currency),
    })


@pay_router.post("/pay/api/cancel/{tc}")
async def api_cancel(tc: str):
    """لغو یک چرک‌نویس در انتظار (مثلاً بعد از اتمام مهلت ۳۵ دقیقه‌ای واریز)."""
    draft = await _get_draft(tc)
    if not draft:
        return JSONResponse({"error": "not found"}, status_code=404)
    if draft.status in ("paid",):
        return JSONResponse({"status": draft.status})
    async with Session() as s:
        d = await s.scalar(select(ChargeDraft).where(ChargeDraft.tracking_code == tc))
        if d and d.status != "paid":
            d.status = "expired"
            await s.commit()
        kind = getattr(draft, "kind", "tenant") or "tenant"
        if draft.order_id and kind != "platform":
            cc = await s.scalar(
                select(CryptoCharge).where(CryptoCharge.order_id == draft.order_id))
            if cc and cc.status == "waiting":
                cc.status = "expired"
                await s.commit()
    return JSONResponse({"status": "expired"})


async def _active_check_tenant(draft) -> bool:
    """در صورت نرسیدن IPN، مستقیماً وضعیت پرداخت را از نوپیمنت استعلام و کیف پول را شارژ می‌کند.
    خروجی True یعنی پرداخت تأیید و شارژ شد."""
    from tenant import get_setting
    from tenant.gateways import crypto_pay
    from models import BotUser, WalletTxn
    if not draft.np_payment_id:
        return False
    api_key = await get_setting(draft.bot_id, "nowpay_api_key", "")
    if not api_key:
        return False
    data = await crypto_pay.get_payment_status(api_key, draft.np_payment_id)
    if not data:
        return False
    pay_status = (data.get("payment_status") or "").lower()
    if pay_status not in ("finished", "confirmed"):
        return False
    notify = None  # (bot_id, telegram_id, credit) — فقط وقتی همین الان شارژ شد
    async with Session() as s:
        # === PATCH_WALLET_ROW_LOCK_V1 ===
        ch = await s.scalar(select(CryptoCharge).where(
            CryptoCharge.order_id == draft.order_id).with_for_update())
        if not ch or ch.status == "paid":
            return bool(ch and ch.status == "paid")
        credit = float(ch.amount_toman)
        if credit <= 0 and ch.usd_amount:
            try:
                manual_rate = float(await get_setting(draft.bot_id, "toman_per_usd", "0"))
            except Exception:
                manual_rate = 0
            if manual_rate > 0:
                credit = round(float(ch.usd_amount) * manual_rate)
                ch.amount_toman = credit
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == ch.bot_id, BotUser.telegram_id == ch.user_telegram_id).with_for_update())
        if u and credit > 0:
            u.balance = float(u.balance) + credit
            s.add(WalletTxn(bot_id=ch.bot_id, user_id=u.id,
                            amount=credit, kind="crypto_charge",
                            note="شارژ با ارز دیجیتال (تأیید فعال)"))
            notify = (ch.bot_id, ch.user_telegram_id, credit, float(u.balance))
            _kyc_uid = u.id
        else:
            _kyc_uid = None
        ch.status = "paid"
        await s.commit()
    # === PATCH_KYC_GATE_ALL_PATHS_V1 ===
    if _kyc_uid:
        from tenant.shared.kyc import after_charge as _ac
        await _ac(ch.bot_id, _kyc_uid)

    # اطلاع به کاربر در تلگرام (چون این مسیر IPN نیست و خودش پیام نمی‌دهد)
    if notify:
        bot_id, tg_id, amt, new_balance = notify
        try:
            await _notify_tenant_user(bot_id, tg_id, amt, new_balance)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        # === PATCH_TX_LOG_CHANNEL_V1 ===
        try:
            await _tx_log_tenant(bot_id, tg_id, amt, new_balance, "crypto")
        except Exception:
            logger.exception("tx_log (crypto)")
    return True


# === PATCH_TX_LOG_CHANNEL_V1 ===
async def _tx_log_tenant(bot_id: int, tg_id: int, amt: float,
                         new_balance: float, gateway: str, card: str = "") -> None:
    """لاگِ تراکنش در کانالِ رباتِ مستأجر (با توکن خودِ همان ربات)."""
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from models import Bot as BotRow, BotUser
    from sqlalchemy import select
    async with Session() as s:
        brow = await s.get(BotRow, bot_id)
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == tg_id))
    if not brow:
        return
    tbot = Bot(token=brow.token,
               default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        from tenant.shared.tx_channel import tx_log
        await tx_log(tbot, bot_id, buyer=u, amount=amt, gateway=gateway,
                     old_balance=(float(new_balance) - float(amt)),
                     new_balance=new_balance, card=card)
    finally:
        try:
            await tbot.session.close()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

async def _notify_tenant_user(bot_id: int, tg_id: int, amt: float,
                              new_balance: float | None = None) -> None:
    """ارسال پیام «کیف پولت شارژ شد» به کاربرِ ربات مستأجر با توکن همان ربات."""
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from models import Bot as BotRow
    async with Session() as s:
        brow = await s.get(BotRow, bot_id)
    if not brow:
        return
    tbot = Bot(token=brow.token,
               default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        from tenant.gateways.wallet_receipt import build_wallet_charge_message
        text, kb = build_wallet_charge_message(
            amt, new_balance if new_balance is not None else amt, "crypto")
        await tbot.send_message(tg_id, text, reply_markup=kb)
    finally:
        await tbot.session.close()


async def _active_check_platform(draft) -> bool:
    """معادل _active_check_tenant اما برای شارژ کیف پول خودِ بات‌ساز (platform).
    قبلاً این fallback برای kind == 'platform' اصلاً وجود نداشت، به همین دلیل
    اگر IPN به هر دلیلی نمی‌رسید (قطعی شبکه، ری‌استارت سرور و ...)، پرداخت‌های
    'Finished' در نوپیمنت برای همیشه بدون شارژ می‌ماندند.
    خروجی True یعنی پرداخت تأیید و کیف پول شارژ شد (یا قبلاً شارژ شده بود)."""
    from platform_bot import get_pset
    from models import PlatformTxn, PlatformUser
    from tenant.gateways import crypto_pay
    if not draft.np_payment_id or not draft.order_id:
        return False
    api_key = await get_pset("platform_nowpay_api_key", "")
    if not api_key:
        return False
    data = await crypto_pay.get_payment_status(api_key, draft.np_payment_id)
    if not data:
        return False
    pay_status = (data.get("payment_status") or "").lower()
    if pay_status not in ("finished", "confirmed"):
        return False
    async with Session() as s:
        done = await s.scalar(select(PlatformTxn).where(
            PlatformTxn.kind == "nowpay_done",
            PlatformTxn.note.contains(f"order_id={draft.order_id}")))
        if done:
            return True
        pending = await s.scalar(select(PlatformTxn).where(
            PlatformTxn.user_telegram_id == draft.user_telegram_id,
            PlatformTxn.kind == "nowpay_pending",
            PlatformTxn.note.contains(f"order_id={draft.order_id}")).with_for_update())
        if not pending:
            return False
        note = pending.note or ""
        credit = 0
        for part in note.split("|"):
            if part.startswith("toman="):
                try:
                    credit = int(float(part.split("=")[1]))
                except Exception:
                    logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        fee_pct = float(await get_pset("platform_nowpay_fee_percent", "0") or "0")
        if fee_pct > 0:
            credit = int(credit * (1 - fee_pct / 100))
        if credit <= 0:
            return False
        pu = await s.scalar(select(PlatformUser).where(
            PlatformUser.telegram_id == draft.user_telegram_id).with_for_update())
        if pu:
            pu.balance = float(pu.balance or 0) + credit
        s.add(PlatformTxn(
            user_telegram_id=draft.user_telegram_id,
            amount=credit,
            kind="nowpay_charge",
            note=f"شارژ با ارز دیجیتال (تأیید فعال) — order: {draft.order_id}",
        ))
        pending.kind = "nowpay_done"
        await s.commit()
    try:
        import platform_bot as _pb
        from tenant.gateways.wallet_receipt import tehran_date_time
        jdate, jtime = tehran_date_time()
        bal_line = f"👛 موجودی فعلی: <b>{pu.balance:,.0f} تومان</b>\n" if pu else ""
        await _pb.platform_bot.send_message(
            draft.user_telegram_id,
            "✅ <b>کیف پولت با موفقیت شارژ شد!</b>\n"
            "┈┅━━⊷⊰💳⊱⊶━━┅┈\n"
            f"💰 مبلغ واریزی: <b>{credit:,} تومان</b>\n"
            f"{bal_line}"
            "🧾 روش پرداخت: 🪙 ارز دیجیتال (NOWPayments)\n"
            f"📅 تاریخ: {jdate}   🕒 ساعت: {jtime}\n"
            "┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
            "🙏 ممنون از اعتمادت! برای دیدنِ موجودی، «💰 کیف پول» را بزن.")
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    return True


@pay_router.get("/pay/api/status/{tc}")
async def api_status(tc: str):
    draft = await _get_draft(tc)
    if not draft:
        return JSONResponse({"status": "unknown"}, status_code=404)
    status = draft.status
    if draft.status == "paid":
        return JSONResponse({"status": "paid"})
    if draft.status == "expired":
        return JSONResponse({"status": "expired"})

    kind = getattr(draft, "kind", "tenant") or "tenant"
    if draft.order_id:
        async with Session() as s:
            paid = False
            if kind == "platform":
                # IPN پلتفرم، PlatformTxn را از nowpay_pending به nowpay_done می‌برد
                from models import PlatformTxn
                done = await s.scalar(select(PlatformTxn).where(
                    PlatformTxn.kind == "nowpay_done",
                    PlatformTxn.note.contains(f"order_id={draft.order_id}")))
                paid = done is not None
            else:
                cc = await s.scalar(
                    select(CryptoCharge).where(CryptoCharge.order_id == draft.order_id))
                paid = bool(cc and cc.status == "paid")
            if paid:
                status = "paid"
                d = await s.scalar(
                    select(ChargeDraft).where(ChargeDraft.tracking_code == tc))
                d.status = "paid"
                await s.commit()
        # اگر هنوز پرداخت‌شده نیست، به‌جای منتظر ماندن صرف برای IPN، یک‌بار هم
        # مستقیماً از درگاه استعلام کن (fallback برای وقتی کاربر واریز کرده
        # ولی IPN به هر دلیلی نرسیده است).
        if status != "paid":
            try:
                paid_now = (await _active_check_platform(draft) if kind == "platform"
                            else await _active_check_tenant(draft))
                if paid_now:
                    status = "paid"
                    async with Session() as s:
                        d = await s.scalar(
                            select(ChargeDraft).where(ChargeDraft.tracking_code == tc))
                        d.status = "paid"
                        await s.commit()
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    return JSONResponse({"status": status})


# === PATCH_CRYPTO_POLL_LOOP_SPLIT_V1 ===
# قبلاً یک crypto_poll_loop واحد (در main.py) هر دو نوعِ چرک‌نویسِ شارژ
# (kind='tenant' و kind='platform') را با هم اسکن می‌کرد — چون همه‌چیز در
# یک پروسه بود. حالا که بات‌ساز (پلتفرم) و بات‌های مستأجر ممکن است در دو
# پروسه/سرویسِ جدا اجرا شوند (main_platform.py / main_tenant.py)، این تابع
# با یک آرگومانِ kind پارامتری شده تا هر پروسه فقط چرک‌نویس‌های خودش را
# اسکن کند — نه هردو را دوبار (که فقط اضافه‌کاریِ بی‌مورد بود؛ خودِ UPDATE
# با شرطِ status != 'paid' idempotent است، پس رقابتِ فرضیِ دو پروسه هرگز
# باعثِ شارژِ دوبل نمی‌شد).
async def crypto_poll_loop(kind: str = "tenant"):
    """تأیید خودکار پرداخت‌های ارز دیجیتال (NOWPayments) مستقل از IPN.

    هر ۳۰ ثانیه چرک‌نویس‌های شارژِ در انتظار (waiting/priced) از نوعِ
    kind ('tenant' یا 'platform') که شناسهٔ پرداخت نوپیمنت دارند را مستقیم
    از درگاه استعلام می‌کند؛ اگر finished/confirmed شده باشند، وضعیت را
    paid می‌کند. idempotent است.
    """
    import datetime as _dt
    await asyncio.sleep(30)  # مهلت راه‌اندازی اولیه
    while True:
        try:
            await ensure_table()
            cutoff = _dt.datetime.now() - _dt.timedelta(days=2)
            async with Session() as s:
                drafts = (await s.scalars(select(ChargeDraft).where(
                    ChargeDraft.status.in_(("waiting", "priced")),
                    ChargeDraft.np_payment_id != "",
                    ChargeDraft.np_payment_id.isnot(None),
                    ChargeDraft.kind == kind,
                    ChargeDraft.created_at >= cutoff,
                ))).all()
            for d in drafts:
                try:
                    ok = (await _active_check_platform(d) if kind == "platform"
                          else await _active_check_tenant(d))
                    if ok:
                        async with Session() as s:
                            row = await s.scalar(select(ChargeDraft).where(
                                ChargeDraft.tracking_code == d.tracking_code))
                            if row and row.status != "paid":
                                row.status = "paid"
                                await s.commit()
                except Exception:
                    logger.exception(f"crypto_poll_loop({kind}) item error")
                await asyncio.sleep(0.3)  # فشار نیاوردن به API نوپیمنت
        except Exception:
            logger.exception(f"crypto_poll_loop({kind}) error")
        await asyncio.sleep(30)
