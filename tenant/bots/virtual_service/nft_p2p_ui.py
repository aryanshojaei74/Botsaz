"""
nft_p2p_ui.py — ظاهرِ «بازارِ کاربران» (CSS + JS) که داخلِ صفحهٔ مینی‌اپ ۲ تزریق می‌شود.

=== PATCH_P2P_MARKET_V1 ===
جدا از nft_market.py نگه داشته شده تا آن فایلِ بزرگ ورم نکند. دو رشته:
P2P_CSS و P2P_JS؛ nft_market.py موقعِ import آن‌ها را به _PAGE_HTML می‌چسباند.
JS از همان متغیرها/توابعِ صفحهٔ اصلی (ST, api, sheet, toast, esc, fa, grad, hap, skel, openBot) استفاده می‌کند.
"""

P2P_CSS = r"""
/* ── بازارِ کاربران ── */
.src{display:flex;gap:8px;margin:0 12px 11px}
.src div{flex:1;text-align:center;padding:11px 8px;border-radius:15px;font-size:12.5px;font-weight:800;
  cursor:pointer;background:var(--surface);border:1px solid var(--line);color:var(--dim);
  backdrop-filter:blur(12px);transition:all .22s}
.src div small{display:block;font-size:9.5px;font-weight:600;opacity:.75;margin-top:2px}
.src div.on{color:#fff;border-color:transparent;
  background:linear-gradient(135deg,rgba(63,162,255,.9),rgba(109,92,246,.9));
  box-shadow:0 6px 22px rgba(63,162,255,.32)}
.seller{font-size:10.5px;color:var(--dim);margin:-3px 0 7px;display:flex;align-items:center;gap:4px}
.seller b{color:var(--txt);font-weight:700}
.ptag{background:linear-gradient(135deg,var(--ton),var(--p1))!important;border-color:transparent!important}
.btn.off{opacity:.45;pointer-events:none}
.escrow{display:flex;gap:10px;align-items:flex-start;background:rgba(40,208,123,.09);
  border:1px solid rgba(40,208,123,.28);border-radius:14px;padding:11px 12px;margin:10px 0;
  font-size:11.5px;line-height:1.95;color:#bff0d6}
.escrow i{font-style:normal;font-size:18px;line-height:1.5}

.me-hero{margin:4px 12px 12px;padding:16px;border-radius:22px;position:relative;overflow:hidden;
  background:linear-gradient(135deg,rgba(40,208,123,.22),rgba(63,162,255,.20) 55%,rgba(109,92,246,.24));
  border:1px solid var(--line-2)}
.me-hero::after{content:"";position:absolute;width:170px;height:170px;border-radius:50%;
  background:rgba(255,255,255,.07);top:-80px;inset-inline-end:-50px}
.me-hero .lb{font-size:11.5px;color:rgba(255,255,255,.7);position:relative}
.me-hero .big{font-size:27px;font-weight:800;margin:5px 0 12px;position:relative;direction:ltr;text-align:right}
.me-hero .big small{font-size:12px;font-weight:600;opacity:.7;margin-inline-start:5px}
.me-hero .acts{display:flex;gap:8px;position:relative}
.me-hero .acts .cta{padding:12px;font-size:12.5px;flex:1}
.cta.ghost{background:rgba(255,255,255,.1);box-shadow:none;border:1px solid var(--line-2)}
.cta.green{background:linear-gradient(135deg,#1fb86a,#16895a);box-shadow:0 8px 24px rgba(31,184,106,.35)}
.cta.red{background:linear-gradient(135deg,#e04a52,#a62d3a);box-shadow:0 8px 24px rgba(224,74,82,.3)}

.deal{background:var(--surface);border:1px solid var(--line);border-radius:18px;padding:13px;
  margin-bottom:10px;backdrop-filter:blur(12px)}
.deal .hd{display:flex;align-items:center;gap:9px;margin-bottom:9px}
.deal .th{width:42px;height:42px;border-radius:12px;display:flex;align-items:center;justify-content:center;
  font-size:19px;flex:none;overflow:hidden}
.deal .ttl{font-size:13px;font-weight:800;min-width:0;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.deal .ttl small{display:block;font-size:10.5px;color:var(--faint);font-weight:600;margin-top:2px}
.deal .acts{display:flex;gap:7px;margin-top:11px}
.deal .acts .cta{padding:11px 6px;font-size:12px;flex:1;box-shadow:none}
.who{background:var(--surface-2);border-radius:12px;padding:9px 11px;margin-top:8px;font-size:11.5px;
  display:flex;align-items:center;gap:8px;color:var(--dim)}
.who b{color:var(--txt)}
.who a{margin-inline-start:auto;color:var(--ton);font-weight:800;text-decoration:none}
.b-ton{background:var(--ton-bg);color:var(--ton)}
.bar{display:flex;gap:6px;margin:0 12px 12px}
.bar div{flex:1;text-align:center;padding:9px 4px;border-radius:12px;font-size:11.5px;font-weight:800;
  cursor:pointer;color:var(--dim);background:var(--surface);border:1px solid var(--line);transition:all .2s}
.bar div.on{color:var(--txt);border-color:var(--p1);background:rgba(109,92,246,.18)}
.sw2{display:flex;align-items:center;justify-content:space-between;background:var(--surface-2);
  border:1px solid var(--line);border-radius:14px;padding:12px 14px;margin-bottom:10px;font-size:13px;font-weight:800}
.tgl{width:50px;height:28px;border-radius:99px;background:rgba(255,255,255,.14);position:relative;cursor:pointer;
  transition:background .25s;flex:none}
.tgl::after{content:"";position:absolute;top:3px;inset-inline-start:3px;width:22px;height:22px;border-radius:50%;
  background:#fff;transition:transform .25s cubic-bezier(.3,1.4,.5,1)}
.tgl.on{background:linear-gradient(135deg,#1fb86a,#16895a)}
.tgl.on::after{transform:translateX(-22px)}
.pv{display:flex;gap:11px;align-items:center;background:var(--surface-2);border:1px solid var(--line);
  border-radius:16px;padding:10px;margin:4px 0 10px}
.pv .im{width:64px;height:64px;border-radius:14px;overflow:hidden;flex:none;display:flex;
  align-items:center;justify-content:center;font-size:28px}
.pv .im img{width:100%;height:100%;object-fit:cover}
.pv .tx{font-size:12px;line-height:1.9;min-width:0}
.pv .tx b{font-size:13px}
.hline{font-size:12.5px;font-weight:800;margin:16px 12px 9px;display:flex;align-items:center;gap:7px}
.hline i{font-style:normal}
.hline .n{margin-inline-start:auto;background:var(--surface-2);border-radius:99px;padding:2px 10px;
  font-size:10.5px;color:var(--dim)}
"""

P2P_JS = r"""
/* ═════════════ بازارِ کاربران (PATCH_P2P_MARKET_V1) ═════════════ */
const P2P_MODE={buy:"فروش",rent:"اجاره",both:"فروش و اجاره"};
const P2P_BADGE={escrow:["b-wait"],await_buyer:["b-wait"],completed:["b-ok"],released:["b-ok"],
                 cancelled:["b-no"],refunded:["b-no"],disputed:["b-no"]};

function p2pErr(e,fallback){
  const m=e&&e.data&&e.data.message; toast(m||fallback||"انجام نشد.");
}
async function p2pBoot(){
  try{
    const r=await api("/p2p/bootstrap");
    ST.p2p=r||{enabled:false};
    const me=document.getElementById("nv-me");
    if(me){ me.style.display=ST.p2p.enabled?"block":"none";
      const old=me.querySelector(".dot"); if(old) old.remove();
      if(ST.p2p.enabled&&ST.p2p.todo) me.insertAdjacentHTML("beforeend",'<div class="dot" style="top:-2px;inset-inline-end:22%">'+ST.p2p.todo+'</div>'); }
  }catch(e){ ST.p2p={enabled:false}; }
}

/* ───── سوئیچِ منبع در مارکت ───── */
function srcSwitchHtml(){
  if(!ST.p2p||!ST.p2p.enabled) return "";
  return `<div class="src rv" style="animation-delay:20ms">
    <div class="${ST.src!=="users"?"on":""}" onclick="setSrc('shop')">🏬 فروشگاه<small>آیتم‌هایِ خودِ مارکت</small></div>
    <div class="${ST.src==="users"?"on":""}" onclick="setSrc('users')">🤝 بازارِ کاربران<small>خرید و اجارهٔ امن از کاربران</small></div>
  </div>`;
}
function setSrc(v){ hap(); ST.src=v; renderMarket(); }

function p2pEscrowNote(){
  return `<div class="escrow"><i>🛡</i><div>معاملهٔ امن: پولت نزدِ ربات نگه داشته می‌شود و فقط بعد از اینکه
    <b>تو دریافتِ آیتم را تأیید کنی</b> به فروشنده می‌رسد. اگر فروشنده انتقال ندهد، پولت کامل برمی‌گردد.</div></div>`;
}

/* ───── صفحهٔ «من» ───── */
async function renderMe(){
  const v=document.getElementById("view");
  v.innerHTML='<div class="me-hero"><div class="lb">موجودیِ قابلِ برداشت از فروش</div><div class="big">…</div></div>'+skel(2);
  let r;
  try{ r=await api("/p2p/my"); }catch(e){
    v.innerHTML='<div class="empty"><span class="e">⚠️</span>خطا در دریافتِ اطلاعات.</div>'; return; }
  ST.me=r;
  if(!r.enabled){ v.innerHTML='<div class="empty"><span class="e">🔒</span>بازارِ کاربران فعال نیست.</div>'; return; }
  const tab=ST.meTab||"deals";
  const todo=r.deals.filter(d=>d.actions.length).length;
  let body="";
  if(tab==="listings"){
    body=(r.listings.length?r.listings.map((l,i)=>p2pListingCard(l,i)).join("")
      :'<div class="empty"><span class="e">🎁</span>هنوز آگهی‌ای نگذاشته‌ای.<br/>با دکمهٔ «➕ آگهی جدید» گیفت یا یوزرنیمت را بگذار.</div>');
  } else if(tab==="wd"){
    body=(r.withdrawals.length?r.withdrawals.map((w,i)=>{
      const cls=w.status==="paid"?"b-ok":(w.status==="rejected"?"b-no":"b-wait");
      const t=w.status==="paid"?"واریز شد":(w.status==="rejected"?"رد شد — برگشت":"در انتظارِ واریز");
      return `<div class="ord rv" style="animation-delay:${Math.min(i,8)*45}ms"><div style="display:flex;align-items:center;gap:8px">
        <b style="font-size:13px">${fa(w.amount_fa.replace(/,/g,""))} ت</b>
        <span class="attr" style="margin:0"><b>${esc(w.card)}</b></span>
        <span class="badge ${cls}" style="margin-inline-start:auto">${t}</span></div></div>`; }).join("")
      :'<div class="empty"><span class="e">💸</span>هنوز برداشتی ثبت نکرده‌ای.</div>');
  } else {
    body=(r.deals.length?r.deals.map((d,i)=>p2pDealCard(d,i)).join("")
      :'<div class="empty"><span class="e">🤝</span>هنوز معامله‌ای نداری.</div>');
  }
  v.innerHTML=`
    <div class="me-hero rv">
      <div class="lb">💰 قابلِ برداشت از فروش‌هایِ تو</div>
      <div class="big">${r.available_fa}<small>تومان</small></div>
      <div class="acts">
        <button class="cta green" onclick="p2pWithdrawSheet()" ${r.available>0?"":"disabled"}>💸 برداشت</button>
        <button class="cta" onclick="p2pListingSheet(null)">➕ آگهی جدید</button>
      </div>
    </div>
    <div class="bar rv" style="animation-delay:40ms">
      <div class="${tab==="deals"?"on":""}" onclick="setMeTab('deals')">🤝 معامله‌ها${todo?" ("+todo+")":""}</div>
      <div class="${tab==="listings"?"on":""}" onclick="setMeTab('listings')">🎁 آگهی‌ها</div>
      <div class="${tab==="wd"?"on":""}" onclick="setMeTab('wd')">💸 برداشت‌ها</div>
    </div>
    <div class="pad">${body}</div>`;
}
function setMeTab(t){ hap(); ST.meTab=t; renderMe(); }

function p2pThumb(it){
  const bg=grad(it.color||it.collection||it.title);
  const inner=it.image?'<img referrerpolicy="no-referrer" onerror="this.remove()" src="'+esc(it.image)+'"/>':(it.kind==="username"?"@":"🎁");
  return `<div class="th" style="background:${bg}">${inner}</div>`;
}
function p2pListingCard(l,i){
  const st={available:["b-ok","فعال"],reserved:["b-wait","در حالِ معامله"],rented:["b-ton","اجاره رفته"],
            sold:["b-no","فروخته شد"],hidden:["b-no","پنهان"]}[l.status]||["b-wait",l.status];
  const price=[l.mode!=="rent"?fa(l.sale_price)+" ت فروش":"",l.mode!=="buy"?fa(l.rent_daily)+" ت/روز اجاره":""]
    .filter(Boolean).join(" · ");
  const acts=l.status==="available"
    ?`<div class="acts"><button class="cta ghost" onclick='p2pListingSheet(${esc(JSON.stringify(l)).replace(/&#39;/g,"\\u0027")})'>✏️ ویرایش</button>
       <button class="cta red" onclick="p2pListingRemove(${l.id})">🗑 حذف</button></div>`:"";
  return `<div class="deal rv" style="animation-delay:${Math.min(i,8)*45}ms">
    <div class="hd">${p2pThumb(l)}<div class="ttl">${esc(l.title)}<small>${esc(P2P_MODE[l.mode]||l.mode)} · ${price}</small></div>
      <span class="badge ${st[0]}">${st[1]}</span></div>${acts}</div>`;
}
function p2pDealCard(d,i){
  const cls=(P2P_BADGE[d.status]||["b-wait"])[0];
  const who=d.role==="seller"&&(d.buyer_id)
    ?`<div class="who">👤 خریدار: <b>${esc(d.buyer_name||"—")}</b>${d.buyer_username?" <span dir=ltr>@"+esc(d.buyer_username)+"</span>":""}
       <a target="_blank" href="${d.buyer_username?"https://t.me/"+esc(d.buyer_username):"tg://user?id="+d.buyer_id}">پیام</a></div>`:"";
  let acts="";
  if(d.actions.includes("seller_done"))
    acts=`<div class="acts"><button class="cta green" onclick="p2pSellerDone(${d.id})">📤 انتقال دادم</button>
          <button class="cta red" onclick="p2pConfirm(${d.id},'seller_cancel','🚫 از قبل فروخته‌ام','معامله لغو و پولِ خریدار برمی‌گردد و آگهی‌ات برداشته می‌شود.')">🚫 از قبل فروخته‌ام</button></div>`;
  else if(d.actions.includes("buyer_ok"))
    acts=`<div class="acts"><button class="cta green" onclick="p2pConfirm(${d.id},'buyer_ok','✅ دریافت کردم','با تأیید، مبلغ به فروشنده پرداخت می‌شود و قابلِ بازگشت نیست. آیتم را دریافت کرده‌ای؟')">✅ دریافت کردم</button>
          <button class="cta red" onclick="p2pConfirm(${d.id},'buyer_problem','⚠️ مشکلی هست','پرونده به پشتیبانی می‌رود و پول تا بررسی نزدِ ربات می‌ماند.')">⚠️ مشکل دارم</button></div>`;
  const hint=d.role==="seller"&&d.status==="escrow"
    ?'<div class="note" style="margin-top:9px">آیتم را به خریدارِ بالا منتقل کن و بعد «انتقال دادم» را بزن.</div>':"";
  return `<div class="deal rv" style="animation-delay:${Math.min(i,8)*45}ms">
    <div class="hd"><div class="th" style="background:${grad(d.title)}">${d.role==="seller"?"📤":"🛒"}</div>
      <div class="ttl">#${d.id} · ${esc(d.title)}<small>${d.role==="seller"?"فروش":"خرید"}${d.kind==="rent"?" · اجارهٔ "+d.days+" روزه":""} · ${d.price_fa} ت</small></div>
      <span class="badge ${cls}">${esc(d.status_fa)}</span></div>${who}${hint}${acts}</div>`;
}

function p2pConfirm(id,action,title,text){
  sheet(`<div class="grab"></div><div class="shead"><b>${title}</b><div class="x" onclick="closeSheet()">✕</div></div>
    <div class="note" style="margin:0 0 12px">${text}</div>
    <button class="cta ${action==="buyer_ok"?"green":"red"}" id="pcfm">تأیید</button>`);
  document.getElementById("pcfm").onclick=async()=>{
    const b=document.getElementById("pcfm"); b.disabled=true; b.textContent="در حال انجام…";
    try{ await api("/p2p/deal/"+id+"/"+action,{method:"POST",body:JSON.stringify({})});
      closeSheet(); hap("heavy"); toast("✅ انجام شد."); await boot(); }
    catch(e){ closeSheet(); p2pErr(e); renderMe(); }
  };
}
async function p2pSellerDone(id){
  try{
    const r=await api("/p2p/deal/"+id+"/seller_done",{method:"POST",body:JSON.stringify({})});
    if(r.state==="free"){ return p2pPayFee(id,"free"); }
    const opts=(r.options||[]).map(o=>o.method==="stars"
      ?`<button class="cta" style="margin-bottom:9px" onclick="p2pPayFee(${id},'stars')">⭐ پرداختِ کارمزد با استارز (${o.amount} ⭐)</button>`
      :`<button class="cta green" style="margin-bottom:9px" onclick="p2pPayFee(${id},'wallet')">💰 کسر از کیف‌پول (${fa(o.amount)} تومان)</button>`).join("");
    sheet(`<div class="grab"></div><div class="shead"><b>📤 تأییدِ انتقال</b><div class="x" onclick="closeSheet()">✕</div></div>
      <div class="note" style="margin:0 0 12px">برایِ ثبتِ «انتقال دادم» باید کارمزدِ ربات را بپردازی. بعد از پرداخت، معامله <b>قفل</b> می‌شود
      و دیگر نمی‌توانی لغوش کنی. سپس از خریدار تأیید گرفته می‌شود و مبلغِ فروش به موجودی‌ات می‌رسد.</div>${opts}`);
  }catch(e){ p2pErr(e); renderMe(); }
}
async function p2pPayFee(id,method){
  try{
    const r=await api("/p2p/deal/"+id+"/seller_done",{method:"POST",body:JSON.stringify({method:method})});
    closeSheet(); hap("heavy");
    if(r.state==="invoice_sent"){
      sheet(`<div class="grab"></div><div class="shead"><b>⭐ فاکتور ارسال شد</b><div class="x" onclick="closeSheet()">✕</div></div>
        <div class="note" style="margin:0 0 12px">فاکتورِ ${r.stars} ⭐ داخلِ چتِ ربات برایت فرستاده شد. آن را پرداخت کن؛ بعد از پرداخت معامله خودکار قفل می‌شود.</div>
        <button class="cta" onclick="closeSheet();openBot()">باز کردنِ ربات</button>`);
    } else { toast("🔒 کارمزد پرداخت و معامله قفل شد."); await boot(); }
  }catch(e){
    closeSheet();
    if(e.code===402&&e.data) toast("موجودیِ کیف‌پولت کافی نیست (لازم: "+e.data.need_fa+" ت).");
    else p2pErr(e);
    renderMe();
  }
}

/* ───── آگهی ───── */
function p2pListingSheet(l){
  l=l||{kind:"gift",mode:"buy",rent_min_days:1,rent_max_days:30};
  ST._pl=l; const edit=!!l.id;
  sheet(`<div class="grab"></div><div class="shead"><b>${edit?"✏️ ویرایشِ آگهی":"➕ آگهیِ جدید"}</b><div class="x" onclick="closeSheet()">✕</div></div>
    <label class="l">نوعِ آیتم</label>
    <select class="f" id="pl_kind" onchange="p2pKindChange()" ${edit?"disabled":""}>
      <option value="gift" ${l.kind==="gift"?"selected":""}>🎁 گیفتِ NFT</option>
      <option value="username" ${l.kind==="username"?"selected":""}>@ یوزرنیم</option></select>
    <div id="pl_gift" style="display:${l.kind==="gift"?"block":"none"}">
      <label class="l">لینکِ گیفت (فقط همین را بده؛ بقیه خودکار می‌آید)</label>
      <input class="f" id="pl_link" dir="ltr" placeholder="https://t.me/nft/PlushPepe-1841" value="${esc(l.link||"")}" onchange="p2pPreview()"/>
      <div id="pl_pv"></div>
    </div>
    <div id="pl_user" style="display:${l.kind==="username"?"block":"none"}">
      <label class="l">یوزرنیم</label>
      <input class="f" id="pl_username" dir="ltr" placeholder="@username" value="${esc(l.kind==="username"?l.ref||"":"")}"/>
    </div>
    <label class="l">نوعِ عرضه</label>
    <select class="f" id="pl_mode" onchange="p2pModeChange()">
      <option value="buy" ${l.mode==="buy"?"selected":""}>🛒 فقط فروش</option>
      <option value="rent" ${l.mode==="rent"?"selected":""}>🔑 فقط اجاره</option>
      <option value="both" ${l.mode==="both"?"selected":""}>🛒🔑 هم فروش هم اجاره</option></select>
    <div id="pl_sale_w"><label class="l">قیمتِ فروش (تومان)</label>
      <input class="f" id="pl_sale" type="number" inputmode="numeric" value="${l.sale_price||""}"/></div>
    <div id="pl_rent_w">
      <label class="l">اجارهٔ روزانه (تومان)</label>
      <input class="f" id="pl_rent" type="number" inputmode="numeric" value="${l.rent_daily||""}"/>
      <div style="display:flex;gap:8px">
        <div style="flex:1"><label class="l">حداقل روز</label><input class="f" id="pl_min" type="number" value="${l.rent_min_days||1}"/></div>
        <div style="flex:1"><label class="l">حداکثر روز</label><input class="f" id="pl_max" type="number" value="${l.rent_max_days||30}"/></div></div>
    </div>
    <label class="l">توضیحِ کوتاه (اختیاری)</label>
    <input class="f" id="pl_note" maxlength="300" value="${esc(l.note||"")}"/>
    <div class="note">💡 وقتی کسی بخرد، پولش نزدِ ربات می‌ماند و برایت پیام می‌آید. بعد از انتقالِ آیتم و تأییدِ خریدار، مبلغ به موجودی‌ات اضافه می‌شود.
      ${(ST.p2p.fee_options||[]).length?"<br/>کارمزدِ ربات هنگامِ «انتقال دادم» از تو گرفته می‌شود: "+
        ST.p2p.fee_options.map(o=>o.method==="stars"?o.amount+" ⭐ (استارز)":fa(o.amount)+" تومان (کیف‌پول)").join(" یا "):""}</div>
    <button class="cta" style="margin-top:12px" id="pl_save" onclick="p2pListingSave()">${edit?"ذخیره":"ثبتِ آگهی"}</button>`);
  p2pModeChange(); if(edit&&l.kind==="gift") p2pPreview();
}
function p2pKindChange(){
  const k=document.getElementById("pl_kind").value;
  document.getElementById("pl_gift").style.display=k==="gift"?"block":"none";
  document.getElementById("pl_user").style.display=k==="username"?"block":"none";
}
function p2pModeChange(){
  const m=document.getElementById("pl_mode").value;
  document.getElementById("pl_sale_w").style.display=m==="rent"?"none":"block";
  document.getElementById("pl_rent_w").style.display=m==="buy"?"none":"block";
}
async function p2pPreview(){
  const link=(document.getElementById("pl_link").value||"").trim(); const box=document.getElementById("pl_pv");
  if(!link){ box.innerHTML=""; return; }
  box.innerHTML='<div class="note">در حالِ گرفتنِ مشخصات…</div>';
  try{
    const r=await api("/p2p/preview",{method:"POST",body:JSON.stringify({link:link})}); const m=r.meta;
    ST._pm=m;
    box.innerHTML=`<div class="pv"><div class="im" style="background:${grad(m.color||m.collection||m.title)}">${m.image?'<img referrerpolicy="no-referrer" onerror="this.remove()" src="'+esc(m.image)+'"/>':"🎁"}</div>
      <div class="tx"><b>${esc(m.title)}</b><br/>${m.collection?"کالکشن: "+esc(m.collection)+"<br/>":""}
      ${m.model?"مدل: "+esc(m.model)+" · ":""}${m.color?"رنگ: "+esc(m.color)+" · ":""}${m.symbol?"سمبل: "+esc(m.symbol):""}
      ${m.fetched?"":'<br/><span style="color:var(--gold)">مشخصاتِ کامل گرفته نشد؛ آگهی با نامِ لینک ثبت می‌شود.</span>'}</div></div>`;
  }catch(e){ ST._pm=null; box.innerHTML='<div class="note" style="color:var(--no)">'+esc((e.data&&e.data.message)||"لینک معتبر نیست.")+'</div>'; }
}
async function p2pListingSave(){
  const g=id=>document.getElementById(id), kind=g("pl_kind").value;
  const body={id:(ST._pl&&ST._pl.id)||0,kind:kind,mode:g("pl_mode").value,
    link:g("pl_link").value,username:g("pl_username").value,
    sale_price:g("pl_sale").value,rent_daily:g("pl_rent").value,
    rent_min_days:g("pl_min").value,rent_max_days:g("pl_max").value,note:g("pl_note").value};
  const b=g("pl_save"); b.disabled=true; b.textContent="در حال ثبت…";
  try{ await api("/p2p/listing",{method:"POST",body:JSON.stringify(body)});
    closeSheet(); hap("heavy"); toast("✅ آگهی ثبت شد."); ST.meTab="listings"; renderMe(); }
  catch(e){ b.disabled=false; b.textContent="ثبتِ آگهی"; p2pErr(e,"ثبت نشد."); }
}
async function p2pListingRemove(id){
  try{ await api("/p2p/listing/"+id+"/remove",{method:"POST"}); hap("medium"); renderMe(); }
  catch(e){ p2pErr(e); }
}

/* ───── برداشت ───── */
function p2pWithdrawSheet(){
  const r=ST.me||{};
  sheet(`<div class="grab"></div><div class="shead"><b>💸 برداشتِ مبلغِ فروش</b><div class="x" onclick="closeSheet()">✕</div></div>
    <div class="row"><span>قابلِ برداشت</span><b>${r.available_fa} ت</b></div>
    ${r.min_withdraw?`<div class="row"><span>حداقلِ برداشت</span><b>${fa(r.min_withdraw)} ت</b></div>`:""}
    <label class="l">مبلغ (تومان)</label>
    <input class="f" id="wd_amt" type="number" inputmode="numeric" value="${r.available||""}"/>
    <label class="l">شمارهٔ کارت (۱۶ رقم)</label>
    <input class="f" id="wd_card" dir="ltr" inputmode="numeric" maxlength="19" placeholder="6037 9981 0000 0000"/>
    <label class="l">نامِ صاحبِ کارت</label>
    <input class="f" id="wd_holder"/>
    <div class="note">با ثبتِ درخواست، مبلغ از موجودی‌ات کم می‌شود و مشخصاتِ کارتت برایِ ادمین می‌رود. بعد از واریز، به تو پیام می‌دهیم.
      اگر ادمین رد کند، مبلغ به موجودی‌ات برمی‌گردد.</div>
    <button class="cta green" style="margin-top:12px" id="wd_go" onclick="p2pWithdrawGo()">ثبتِ درخواستِ برداشت</button>`);
}
async function p2pWithdrawGo(){
  const g=id=>document.getElementById(id);
  const b=g("wd_go"); b.disabled=true; b.textContent="در حال ثبت…";
  try{ await api("/p2p/withdraw",{method:"POST",body:JSON.stringify({amount:g("wd_amt").value,card:g("wd_card").value,holder:g("wd_holder").value})});
    closeSheet(); hap("heavy"); toast("✅ درخواست ثبت شد. بعد از واریز به تو پیام می‌دهیم."); ST.meTab="wd"; await boot(); }
  catch(e){ b.disabled=false; b.textContent="ثبتِ درخواستِ برداشت"; p2pErr(e,"ثبت نشد."); }
}

/* ───── مدیریت: بازارِ کاربران ───── */
async function loadAdminP2P(){
  const b=document.getElementById("ab");
  try{
    const r=await api("/admin/p2p"); const c=r.cfg; ST._ap=r;
    const disputes=r.deals.filter(d=>d.status==="disputed");
    const pendW=r.withdrawals.filter(w=>w.status==="pending");
    const sec=(ic,t,n)=>`<div class="hline" style="margin:16px 2px 9px"><i>${ic}</i>${t}<span class="n">${n}</span></div>`;
    b.innerHTML=`
      <div class="sw2 rv"><span>🤝 بازارِ کاربران</span><div class="tgl ${c.enabled?"on":""}" onclick="p2pToggle(${c.enabled?0:1})"></div></div>
      <button class="cta ghost" style="margin-bottom:6px" onclick="p2pSettingsSheet()">⚙️ تنظیماتِ کارمزد و مهلت‌ها</button>
      <div class="note" style="margin-top:0">کارمزد: ${c.fee_mode==="stars"?"فقط استارز":c.fee_mode==="wallet"?"فقط کیف‌پول":"استارز یا کیف‌پول (فروشنده انتخاب می‌کند)"}
        — ${c.fee_stars} ⭐ / ${fa(c.fee_wallet)} ت · لغوِ خودکار بعد از ${c.seller_hours} ساعت</div>
      ${sec("⚠️","اختلاف‌ها",disputes.length)}
      ${disputes.length?disputes.map(p2pAdminDeal).join(""):'<div class="note">اختلافی نیست 🎉</div>'}
      ${sec("💸","درخواست‌هایِ برداشت در انتظار",pendW.length)}
      ${pendW.length?pendW.map(w=>`<div class="ord"><div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
          <b style="font-size:13px">#${w.id} · ${esc(w.user)}</b><span class="badge b-wait" style="margin-inline-start:auto">${w.amount_fa} ت</span></div>
        <div class="attr"><span>کارت</span><b>${esc(w.card)}</b></div><div class="attr"><span>به نامِ</span><b>${esc(w.holder)}</b></div>
        <div class="btns"><div class="mini" style="flex:1;text-align:center" onclick="p2pAdmW(${w.id},'pay')">✅ واریز شد</div>
        <div class="mini" style="flex:1;text-align:center" onclick="p2pAdmW(${w.id},'reject')">❌ رد و برگشت</div></div></div>`).join(""):'<div class="note">درخواستی نیست.</div>'}
      ${sec("🤝","آخرین معامله‌ها",r.deals.length)}
      ${r.deals.filter(d=>d.status!=="disputed").slice(0,25).map(p2pAdminDeal).join("")||'<div class="note">هنوز معامله‌ای نیست.</div>'}
      ${sec("🎁","آگهی‌هایِ کاربران",r.listings.length)}
      ${r.listings.map(l=>`<div class="adm"><div class="t"><div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(l.title)}</div>
        <span style="color:var(--faint);font-weight:400;font-size:10.5px">${esc(l.seller)} · ${esc(P2P_MODE[l.mode]||l.mode)} · ${esc(l.status)}</span></div>
        ${(l.status==="available"||l.status==="hidden")?`<div class="mini" onclick="p2pAdmRemove(${l.id})">🗑</div>`:""}</div>`).join("")||'<div class="note">آگهی‌ای نیست.</div>'}`;
  }catch(e){ b.innerHTML='<div class="empty"><span class="e">⚠️</span>خطا.</div>'; }
}
function p2pAdminDeal(d){
  const cls=(P2P_BADGE[d.status]||["b-wait"])[0];
  const acts=d.status==="disputed"
    ?`<div class="btns"><div class="mini" style="flex:1;text-align:center" onclick="p2pAdmDeal(${d.id},'release')">💸 آزاد به فروشنده</div>
      <div class="mini" style="flex:1;text-align:center" onclick="p2pAdmDeal(${d.id},'refund')">↩️ برگشت به خریدار</div></div>`:"";
  return `<div class="ord"><div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
    <b style="font-size:12.5px">#${d.id} · ${esc(d.title)}</b><span class="badge ${cls}" style="margin-inline-start:auto">${esc(d.status_fa)}</span></div>
    <div class="attr"><span>خریدار</span><b>${esc(d.buyer)}</b></div><div class="attr"><span>فروشنده</span><b>${esc(d.seller)}</b></div>
    <div class="attr"><span>${d.kind==="buy"?"فروش":"اجاره ("+d.days+" روز)"}</span><b>${d.price_fa} ت</b></div>
    <div class="attr"><span>کارمزد</span><b>${esc(d.fee)}</b></div>${d.note?`<div class="attr"><span>یادداشت</span><b>${esc(d.note)}</b></div>`:""}${acts}</div>`;
}
async function p2pToggle(v){
  try{ await api("/admin/p2p/settings",{method:"POST",body:JSON.stringify({enabled:!!v})});
    hap("medium"); await p2pBoot(); loadAdminP2P(); }catch(e){ toast("انجام نشد."); }
}
function p2pSettingsSheet(){
  const c=ST._ap.cfg;
  sheet(`<div class="grab"></div><div class="shead"><b>⚙️ تنظیماتِ بازارِ کاربران</b><div class="x" onclick="closeSheet()">✕</div></div>
    <label class="l">روشِ دریافتِ کارمزد از فروشنده</label>
    <select class="f" id="ps_mode">
      <option value="both" ${c.fee_mode==="both"?"selected":""}>استارز یا کیف‌پول (فروشنده انتخاب می‌کند)</option>
      <option value="stars" ${c.fee_mode==="stars"?"selected":""}>فقط استارز ⭐</option>
      <option value="wallet" ${c.fee_mode==="wallet"?"selected":""}>فقط کیف‌پولِ ربات</option></select>
    <label class="l">کارمزد با استارز (تعدادِ ⭐ برایِ هر معامله؛ ۰ = غیرفعال)</label>
    <input class="f" id="ps_stars" type="number" value="${c.fee_stars}"/>
    <label class="l">کارمزد از کیف‌پول (تومان برایِ هر معامله؛ ۰ = غیرفعال)</label>
    <input class="f" id="ps_wallet" type="number" value="${c.fee_wallet}"/>
    <div style="display:flex;gap:8px">
      <div style="flex:1"><label class="l">لغوِ خودکار اگر فروشنده اقدام نکرد (ساعت)</label><input class="f" id="ps_sh" type="number" value="${c.seller_hours}"/></div>
      <div style="flex:1"><label class="l">ارجاع به ادمین اگر خریدار پاسخ نداد (ساعت)</label><input class="f" id="ps_bh" type="number" value="${c.buyer_hours}"/></div></div>
    <label class="l">حداقلِ مبلغِ برداشت (تومان)</label>
    <input class="f" id="ps_minw" type="number" value="${c.min_withdraw}"/>
    <div class="note">اگر هر دو کارمزد صفر باشد، معامله بدونِ کارمزد انجام می‌شود.</div>
    <button class="cta" style="margin-top:12px" onclick="p2pSettingsSave()">ذخیره</button>`);
}
async function p2pSettingsSave(){
  const g=id=>document.getElementById(id).value;
  try{ await api("/admin/p2p/settings",{method:"POST",body:JSON.stringify({fee_mode:g("ps_mode"),fee_stars:g("ps_stars"),
      fee_wallet:g("ps_wallet"),seller_hours:g("ps_sh"),buyer_hours:g("ps_bh"),min_withdraw:g("ps_minw")})});
    closeSheet(); hap("medium"); toast("✅ ذخیره شد."); await p2pBoot(); loadAdminP2P(); }
  catch(e){ toast("ذخیره نشد."); }
}
async function p2pAdmDeal(id,a){
  try{ await api("/admin/p2p/deal/"+id+"/"+a,{method:"POST"}); hap("medium"); loadAdminP2P(); }
  catch(e){ p2pErr(e); }
}
async function p2pAdmW(id,a){
  try{ await api("/admin/p2p/withdrawal/"+id+"/"+a,{method:"POST"}); hap("medium"); loadAdminP2P(); }
  catch(e){ p2pErr(e); }
}
async function p2pAdmRemove(id){
  try{ await api("/admin/p2p/listing/"+id+"/remove",{method:"POST"}); hap("medium"); loadAdminP2P(); }
  catch(e){ p2pErr(e); }
}

/* ───── تمِ ظاهری (PATCH_NFT_THEME_V1) ───── */
const TH_PRESETS={
  accent:[["#6d5cf6","بنفش (پیش‌فرض)"],["#ef4444","قرمز"],["#ff7a1a","نارنجی"],["#f5b62c","طلایی"],
          ["#22c55e","سبز"],["#14b8a6","فیروزه‌ای"],["#3b82f6","آبی"],["#ec4899","صورتی"]],
  bg:[["#07070a","مشکیِ پیش‌فرض"],["#000000","مشکیِ خالص"],["#0f172a","سرمه‌ای"],["#1a1210","قهوه‌ای تیره"],
      ["#12081f","بنفشِ تیره"],["#f2f3f5","روشن"],["#e6e8eb","طوسیِ روشن"]],
  card:[["#15151c","پیش‌فرض"],["#000000","مشکیِ خالص"],["#111b2e","سرمه‌ای"],["#1d1512","قهوه‌ای تیره"],
        ["#1c1c22","خاکستریِ تیره"],["#ffffff","سفید"]]
};
const TH_LABEL={accent:["🎨 رنگِ دکمه‌ها","دکمه‌ها، تب‌ها و هایلایت‌ها"],
                bg:["🌗 تمِ اصلی","پس‌زمینهٔ کلِ مینی‌اپ"],
                card:["🪟 رنگِ ثانویه","کارت‌ها و باکس‌ها"]};
function applyTheme(css){ let el=document.getElementById("thm");
  if(!el){ el=document.createElement("style"); el.id="thm"; document.head.appendChild(el); }
  el.textContent=css||""; }
async function loadAdminTheme(){
  const b=document.getElementById("ab");
  let th=(ST.theme)||{accent:"",bg:"",card:""};
  const sec=k=>{
    const cur=(th[k]||"").toLowerCase();
    const sw=TH_PRESETS[k].map(([h,n])=>`<div title="${n}" onclick="thPick('${k}','${h}')"
      style="width:36px;height:36px;border-radius:50%;background:${h};flex:none;cursor:pointer;
      border:2px solid ${cur===h?"var(--txt)":"var(--line-2)"};box-shadow:${cur===h?"0 0 0 3px rgba(255,255,255,.18)":"none"}"></div>`).join("");
    return `<div class="ord rv"><div style="display:flex;align-items:center;margin-bottom:4px">
      <b style="font-size:13px">${TH_LABEL[k][0]}</b>
      <span class="mini" style="margin-inline-start:auto" onclick="thPick('${k}','')">♻️ پیش‌فرض</span></div>
      <div style="font-size:11px;color:var(--faint);margin-bottom:10px">${TH_LABEL[k][1]} — الان: <b dir="ltr">${cur||"پیش‌فرض"}</b></div>
      <div style="display:flex;gap:9px;flex-wrap:wrap;margin-bottom:10px">${sw}</div>
      <div style="display:flex;gap:8px;align-items:center">
        <input type="color" id="thc_${k}" value="${cur||TH_PRESETS[k][0][0]}"
          style="width:46px;height:42px;border:none;border-radius:11px;background:none;padding:0">
        <input class="f" id="thh_${k}" dir="ltr" placeholder="#RRGGBB" value="${cur}" style="margin:0;flex:1">
        <button class="mini" onclick="thCustom('${k}')">ثبت</button></div></div>`; };
  b.innerHTML=`<div class="note rv" style="margin:0 0 10px">سه رنگِ مستقل: <b>رنگِ دکمه‌ها</b> (تأکیدها)، <b>تمِ اصلی</b> (پس‌زمینه) و <b>رنگِ ثانویه</b> (کارت‌ها). تغییر همان لحظه اعمال می‌شود و برایِ همهٔ کاربران ذخیره می‌شود.</div>`
    +sec("accent")+sec("bg")+sec("card")
    +`<button class="cta ghost" style="margin-top:6px" onclick="thReset()">♻️ برگشت به ظاهرِ پیش‌فرضِ همه</button>`;
}
async function thSave(patch){
  try{ const r=await api("/admin/theme",{method:"POST",body:JSON.stringify(patch)});
    ST.theme=r.theme; applyTheme(r.css); hap("medium"); loadAdminTheme(); }
  catch(e){ toast("ذخیره نشد."); }
}
function thPick(k,h){ const o={}; o[k]=h; thSave(o); }
function thCustom(k){ const v=(document.getElementById("thh_"+k).value||document.getElementById("thc_"+k).value||"").trim();
  if(!/^#?[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$/.test(v)){ toast("کدِ رنگ نامعتبر است؛ مثل #ef4444"); return; }
  thPick(k,v.startsWith("#")?v:"#"+v); }
function thReset(){ thSave({accent:"",bg:"",card:""}); }
"""
