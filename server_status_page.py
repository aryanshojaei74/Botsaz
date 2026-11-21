"""PATCH_SERVER_STATUS_V1 — صفحهٔ مستقلِ «وضعیتِ سرورها». هیچ منبعِ خارجی ندارد (فونت/اسکریپت/تصویر همه درون‌خطی)."""

PAGE = r'''<!doctype html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<title>وضعیت سرورها</title>
<style>
:root{--bg:#0b1220;--card:#121b2e;--card2:#16223a;--line:#22304d;--tx:#e8eefc;--mut:#8a9bbd;--ok:#22c55e;--warn:#f59e0b;--bad:#ef4444;--idle:#334155;--acc:#4f8cff}
@media(prefers-color-scheme:light){:root{--bg:#f3f6fc;--card:#fff;--card2:#f6f8fd;--line:#e2e8f3;--tx:#0f1b33;--mut:#64748b;--idle:#cbd5e1}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--bg);color:var(--tx);font-family:Vazirmatn,"IRANSans",Tahoma,"Segoe UI",system-ui,sans-serif;-webkit-text-size-adjust:100%}
.wrap{max-width:760px;margin:0 auto;padding:16px 16px calc(24px + env(safe-area-inset-bottom))}
h1{font-size:18px;margin:4px 0 14px;display:flex;align-items:center;gap:8px}
.banner{border-radius:18px;padding:18px 18px;display:flex;align-items:center;gap:14px;border:1px solid var(--line);background:var(--card);position:relative;overflow:hidden}
.banner::before{content:"";position:absolute;inset:0;opacity:.14;background:var(--c)}
.banner>*{position:relative}
.big{width:46px;height:46px;border-radius:50%;display:grid;place-items:center;background:var(--c);color:#fff;font-size:22px;flex:none;box-shadow:0 0 0 0 var(--c);animation:pulse 2.2s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--c) 55%,transparent)}70%{box-shadow:0 0 0 14px transparent}100%{box-shadow:0 0 0 0 transparent}}
.banner b{font-size:16px;display:block}.banner small{color:var(--mut);font-size:12px}
.sum{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:12px 0}
.sum div{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:10px 12px;text-align:center}
.sum span{display:block;color:var(--mut);font-size:11px}.sum b{font-size:17px}
.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:14px;margin-top:12px}
.hd{display:flex;align-items:center;gap:10px}
.dot{width:11px;height:11px;border-radius:50%;background:var(--c);flex:none;box-shadow:0 0 0 0 var(--c)}
.dot.up{animation:pulse 2.4s infinite}
.hd .nm{font-weight:700;font-size:14px;flex:1}
.pill{font-size:11px;padding:3px 9px;border-radius:99px;background:color-mix(in srgb,var(--c) 18%,transparent);color:var(--c);font-weight:700;white-space:nowrap}
.ms{font-size:12px;color:var(--mut);min-width:62px;text-align:left}
.chart{margin-top:10px;position:relative}
svg{display:block;width:100%;height:auto}
.axis{display:flex;justify-content:space-between;color:var(--mut);font-size:10.5px;margin-top:2px}
.days{display:grid;grid-template-columns:repeat(30,1fr);gap:2px;margin-top:10px;direction:ltr}
.days i{height:22px;border-radius:3px;background:var(--idle);display:block}
.foot{display:flex;justify-content:space-between;gap:6px;color:var(--mut);font-size:11px;margin-top:6px}.foot b{color:var(--tx);font-weight:600}
.legend{display:flex;gap:14px;justify-content:center;color:var(--mut);font-size:11px;margin-top:16px;flex-wrap:wrap}
.legend i{display:inline-block;width:8px;height:8px;border-radius:50%;margin-left:5px}
.upd{text-align:center;color:var(--mut);font-size:11px;margin-top:10px}
.sk{height:84px;border-radius:18px;margin-top:12px;background:linear-gradient(90deg,var(--card),var(--card2),var(--card));background-size:200% 100%;animation:sh 1.2s infinite}
@keyframes sh{to{background-position:-200% 0}}
.err{margin-top:14px;padding:12px;border:1px dashed var(--bad);border-radius:12px;color:var(--bad);font-size:13px;text-align:center}
</style>
</head>
<body>
<div class="wrap">
  <h1>📡 وضعیت زندهٔ سرورها</h1>
  <div id="root"><div class="sk"></div><div class="sk"></div><div class="sk"></div></div>
  <div class="legend"><span><i style="background:var(--ok)"></i>فعال</span><span><i style="background:var(--warn)"></i>افت سرعت</span><span><i style="background:var(--bad)"></i>اختلال</span><span><i style="background:var(--idle)"></i>بدون داده</span></div>
  <div class="upd" id="upd"></div>
</div>
<script>
(function(){
var COL={up:'#22c55e',degraded:'#f59e0b',down:'#ef4444',unknown:'#64748b'};
var LBL={up:'فعال',degraded:'افت سرعت',down:'اختلال',unknown:'نامشخص'};
var fa=function(n){return Number(n).toLocaleString('fa-IR')};
function fmtMs(ms){return ms==null?'—':'<bdi dir="ltr">'+fa(ms)+' ms</bdi>'}
function spark(series){
  var W=600,H=96,pad=6,n=series.length,vals=series.map(function(s){return s[0]}).filter(function(v){return v!=null});
  if(!vals.length)return '<svg viewBox="0 0 '+W+' '+H+'"><text x="300" y="52" fill="#64748b" font-size="13" text-anchor="middle">داده‌ای برای ۲۴ ساعت اخیر نیست</text></svg>';
  var mx=Math.max.apply(null,vals),mn=Math.min.apply(null,vals);if(mx==mn){mx+=1;mn=Math.max(0,mn-1)}
  var avg=vals.reduce(function(a,b){return a+b},0)/vals.length;
  function X(i){return pad+(W-2*pad)*i/(n-1)}function Y(v){return H-pad-(H-2*pad-10)*(v-mn)/(mx-mn)}
  var seg=[],cur=[];
  series.forEach(function(s,i){if(s[0]==null){if(cur.length)seg.push(cur);cur=[]}else cur.push([X(i),Y(s[0]),s[0],i])});
  if(cur.length)seg.push(cur);
  var line='',area='',dots='',id='g'+Math.random().toString(36).slice(2,8);
  seg.forEach(function(sg){
    var d=sg.map(function(p,k){return(k?'L':'M')+p[0].toFixed(1)+' '+p[1].toFixed(1)}).join(' ');
    line+='<path d="'+d+'" fill="none" stroke="var(--acc)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>';
    if(sg.length>1)area+='<path d="'+d+' L'+sg[sg.length-1][0].toFixed(1)+' '+H+' L'+sg[0][0].toFixed(1)+' '+H+' Z" fill="url(#'+id+')"/>';
    sg.forEach(function(p){var slow=p[2]>Math.max(1000,avg*3);if(slow)dots+='<circle cx="'+p[0].toFixed(1)+'" cy="'+p[1].toFixed(1)+'" r="3.4" fill="#f59e0b"><title>'+fa(p[2])+' ms</title></circle>'});
  });
  var last=seg.length?seg[seg.length-1][seg[seg.length-1].length-1]:null;
  if(last)dots+='<circle cx="'+last[0].toFixed(1)+'" cy="'+last[1].toFixed(1)+'" r="4" fill="var(--acc)" stroke="var(--card)" stroke-width="2"/>';
  var down='';
  series.forEach(function(s,i){if(s[1])down+='<rect x="'+(X(i)-2).toFixed(1)+'" y="'+(H-12)+'" width="4" height="12" rx="2" fill="#ef4444"><title>قطعی</title></rect>'});
  var grid='';[0.25,0.5,0.75].forEach(function(f){var y=(pad+(H-2*pad)*f).toFixed(1);grid+='<line x1="0" x2="'+W+'" y1="'+y+'" y2="'+y+'" stroke="var(--line)" stroke-dasharray="3 5"/>'});
  return '<svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none"><defs><linearGradient id="'+id+'" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#4f8cff" stop-opacity=".35"/><stop offset="1" stop-color="#4f8cff" stop-opacity="0"/></linearGradient></defs>'+grid+area+line+down+dots+'</svg>';
}
function daysBar(days){
  return days.map(function(d){var c='var(--idle)',t='بدون داده';
    if(d.p!=null){c=d.p>=99.5?'var(--ok)':d.p>=95?'var(--warn)':'var(--bad)';t=fa(d.p)+'٪ آپتایم'}
    return '<i style="background:'+c+'" title="'+d.d+' — '+t+'"></i>'}).join('');
}
function card(c){
  var col=COL[c.state]||COL.unknown;
  return '<div class="card" style="--c:'+col+'"><div class="hd"><span class="dot '+c.state+'"></span><span class="nm">'+c.name+'</span>'+
   '<span class="ms">'+fmtMs(c.ms)+'</span><span class="pill">'+LBL[c.state]+'</span></div>'+
   '<div class="chart">'+spark(c.series)+'<div class="axis"><span>اکنون</span><span>۱۲ ساعت پیش</span><span>۲۴ ساعت پیش</span></div></div>'+
   '<div class="days">'+daysBar(c.days)+'</div>'+
   '<div class="foot"><span>۳۰ روز اخیر</span><span>آپتایم ۲۴ ساعت: <b>'+(c.uptime24==null?'—':fa(c.uptime24)+'٪')+'</b></span><span>میانگین: <b>'+fmtMs(c.avg_ms)+'</b></span></div></div>';
}
function render(d){
  var cs=d.components,col=COL[d.overall]||COL.unknown;
  if(d.maintenance){col='#4f8cff'}
  var msg={up:'همهٔ سرویس‌ها فعال و پایدار هستند',degraded:'در بخشی از سرویس‌ها افت سرعت داریم',down:'در بخشی از سرویس‌ها اختلال داریم',unknown:'در حال جمع‌آوری اطلاعات…'}[d.overall];
  var ic={up:'✓',degraded:'!',down:'✕',unknown:'…'}[d.overall];
  if(d.maintenance){msg='در حال به‌روزرسانی هستیم؛ تا چند دقیقهٔ دیگر برمی‌گردیم';ic='⟳'}
  var ups=cs.filter(function(c){return c.uptime24!=null}).map(function(c){return c.uptime24});
  var avgUp=ups.length?ups.reduce(function(a,b){return a+b},0)/ups.length:null;
  var ms=cs.map(function(c){return c.ms}).filter(function(v){return v!=null});
  var avgMs=ms.length?Math.round(ms.reduce(function(a,b){return a+b},0)/ms.length):null;
  var okc=cs.filter(function(c){return c.state=='up'}).length;
  document.getElementById('root').innerHTML=
   '<div class="banner" style="--c:'+col+'"><div class="big">'+ic+'</div><div><b>'+msg+'</b><small>به‌روزرسانی خودکار هر ۱۵ ثانیه</small></div></div>'+
   '<div class="sum"><div><span>سرویس‌های فعال</span><b>'+fa(okc)+' از '+fa(cs.length)+'</b></div><div><span>آپتایم ۲۴ ساعت</span><b>'+(avgUp==null?'—':fa(Math.round(avgUp*100)/100)+'٪')+'</b></div><div><span>میانگین پاسخ</span><b>'+fmtMs(avgMs)+'</b></div></div>'+
   cs.map(card).join('');
  document.getElementById('upd').textContent='آخرین به‌روزرسانی: '+new Date().toLocaleTimeString('fa-IR');
}
var failed=0;
function load(){
  fetch('/status/api',{cache:'no-store'}).then(function(r){if(!r.ok)throw 0;return r.json()}).then(function(d){failed=0;render(d)}).catch(function(){
    failed++;if(failed>=2||!document.querySelector('.card'))document.getElementById('root').innerHTML='<div class="err">ارتباط با سرور برقرار نشد؛ در حال تلاش مجدد…</div>';
  });
}
load();setInterval(load,15000);
document.addEventListener('visibilitychange',function(){if(!document.hidden)load()});
})();
</script>
</body>
</html>
'''
