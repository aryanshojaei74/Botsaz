#!/usr/bin/env python3
"""deploy/miniapp_ui_patch.py — کارتِ «🌐 مینی‌اپ» در صفحهٔ هر اپِ مینی‌اپِ /vip (PATCH_MINIAPP_UI_V1).

    python3 /opt/botsaz/deploy/miniapp_ui_patch.py && systemctl restart botsazvip

idempotent؛ پشتیبان: ui.py.bak_miniapp. کدِ جاوااسکریپتِ افزوده بدونِ بک‌اسلش و سه‌تایی‌کوتیشن است تا داخلِ رشتهٔ پایتونیِ ui.py امن باشد.
"""
import ast
import shutil
import sys
from pathlib import Path

UI = Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/botsazvip/hosting/ui.py")
MARK = "PATCH_MINIAPP_UI_V1"

JS = """/* === PATCH_MINIAPP_UI_V1 === کارتِ مینی‌اپ (ساب‌دامین + آپلودِ zipِ استاتیک) */
var MINI_PRICE = 0;
function miniLoad(id){
  var el = document.getElementById('mini-card');
  if (!el) return;
  api('/apps/'+id+'/miniapp').then(function(d){
    if (CUR && CUR.id===id) miniRender(el, id, d);
  }).catch(function(){ el.innerHTML = ''; });
}
function miniRender(el, id, d){
  MINI_PRICE = d.price || 0;
  var h = '<div class="card" style="margin-top:12px"><div class="row between"><div class="grow">'
    + '<div class="name">🌐 مینی‌اپ</div><div class="meta">'
    + (d.active ? 'فعال تا '+faDate(d.until) : 'برای وصل‌کردنِ مینی‌اپ به رباتت، اشتراکِ ماهانه بگیر')
    + '</div></div></div>';
  if (d.url) h += '<div class="kv"><span>آدرس</span><span class="mono">'+H(d.url)+'</span></div>';
  h += '<div class="grid2" style="margin-top:10px">'
    + '<button class="b-key" onclick="miniBuy('+id+')">'+(d.active ? 'تمدید ۳۰ روزه' : 'فعال‌سازی')+' · '+fa(d.price)+' تومان</button>';
  if (d.active) h += '<button onclick="miniPick('+id+')">'+(d.static ? 'جایگزینیِ zip مینی‌اپ' : 'آپلودِ zip مینی‌اپ')+'</button>';
  else h += '<button disabled>آپلودِ zip مینی‌اپ</button>';
  h += '</div>';
  if (d.active){
    h += '<div class="grid2" style="margin-top:8px"><button onclick="miniMenu('+id+')">🔘 ثبت در Menu Button رباتت</button>'
      + (d.static ? '<button class="b-off" onclick="miniDelStatic('+id+')">حذفِ فایل‌هایِ مینی‌اپ</button>' : '<button disabled>—</button>')
      + '</div>';
  }
  h += '<div class="meta" style="margin-top:9px">'
    + 'دو راه برایِ مینی‌اپ: ۱) zipِ فایل‌هایِ HTML/JS (باید index.html داشته باشد) را آپلود کن؛ بدونِ سرور از همین آدرس باز می‌شود. '
    + '۲) اگر zip نگذاری، مینی‌اپ از وب‌سرورِ داخلِ پروژهٔ خودت (کنارِ ربات) سرو می‌شود؛ پورت را خودمان تشخیص می‌دهیم. '
    + 'متغیرِ MINIAPP_URL هم به .env اضافه می‌شود.</div></div>';
  el.innerHTML = h;
}
function miniBuy(id){
  if (!confirm('مبلغ '+fa(MINI_PRICE)+' تومان از کیف‌پولت کم می‌شود. ادامه می‌دهی؟')) return;
  msg('ok','در حال فعال‌سازی…');
  api('/apps/'+id+'/miniapp/buy', {method:'POST'}).then(function(d){
    buzz('success');
    msg('ok','مینی‌اپ فعال شد: '+d.url);
    miniLoad(id);
  }).catch(function(e){ buzz('error'); msg('err', e.message); });
}
function miniPick(id){
  var i = document.getElementById('fpm');
  if (!i){
    i = document.createElement('input');
    i.type = 'file'; i.id = 'fpm'; i.style.display = 'none';
    document.body.appendChild(i);
  }
  i.onchange = function(){
    var f = i.files && i.files[0];
    i.value = '';
    if (!f){ msg('err','فایلی انتخاب نشد. دوباره امتحان کن.'); return; }
    if (!/[.]zip$/i.test(f.name)){ msg('err','فقط فایل zip پذیرفته می‌شود.'); return; }
    msg('ok','در حال آپلود '+f.name+' ...');
    var fd = new FormData(); fd.append('file', f);
    api('/apps/'+id+'/miniapp/upload', {method:'POST', body:fd}).then(function(d){
      buzz('success');
      msg('ok', d.message+' — آدرس: '+d.url);
      miniLoad(id);
    }).catch(function(e){ buzz('error'); msg('err', e.message); });
  };
  i.click();
}
function miniMenu(id){
  api('/apps/'+id+'/miniapp/menu-button', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({text:'🌐 باز کردن'})}).then(function(d){
    if (d.ok){ buzz('success'); msg('ok', d.message); } else { buzz('error'); msg('err', d.message); }
  }).catch(function(e){ buzz('error'); msg('err', e.message); });
}
function miniDelStatic(id){
  if (!confirm('فایل‌هایِ مینی‌اپ حذف شود؟ (اشتراک می‌ماند)')) return;
  api('/apps/'+id+'/miniapp/static', {method:'DELETE'}).then(function(){
    msg('ok','فایل‌هایِ مینی‌اپ حذف شد.');
    miniLoad(id);
  }).catch(function(e){ msg('err', e.message); });
}

"""

A_FUNC = "function pick(){"
A_CARD = """  h += '<div class="card" style="margin-top:12px">'
     + '<div class="kv"><span>وضعیت اشتراک</span><span>'"""
CARD_HOOK = """  h += '<div id="mini-card"></div>';   /* PATCH_MINIAPP_UI_V1 */
  setTimeout(function(){ miniLoad(a.id); }, 0);

"""


def patch(s: str) -> str:
    if MARK in s:
        return s
    if s.count(A_FUNC) != 1:
        raise SystemExit("ui.py: «function pick(){» یک‌بار پیدا نشد؛ چیزی تغییر نکرد")
    if s.count(A_CARD) != 1:
        raise SystemExit("ui.py: کارتِ «وضعیت اشتراک» یک‌بار پیدا نشد؛ چیزی تغییر نکرد")
    if "\\" in JS or '"""' in JS:
        raise SystemExit("خطای داخلی: JS نباید بک‌اسلش داشته باشد")
    s = s.replace(A_FUNC, JS + A_FUNC, 1)
    s = s.replace(A_CARD, CARD_HOOK + A_CARD, 1)
    return s


def main() -> None:
    s = UI.read_text()
    new = patch(s)
    ast.parse(new)
    if new != s:
        b = UI.with_name(UI.name + ".bak_miniapp")
        if not b.exists():
            shutil.copy2(UI, b)
        UI.write_text(new)
        print("ui.py: پچ شد")
    else:
        print("ui.py: قبلاً پچ شده بود")
    print("OK — حالا: systemctl restart botsazvip")


if __name__ == "__main__":
    main()
