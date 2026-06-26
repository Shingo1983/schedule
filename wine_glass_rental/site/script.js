// ---- Lineup data ----
const glassSVG = (h) => `
<svg class="glass-svg" viewBox="0 0 80 150" aria-hidden="true">
  <path class="bowl" d="${h}"/>
  <line class="stem" x1="40" y1="110" x2="40" y2="138"/>
  <path class="foot" d="M22 142 Q40 132 58 142"/>
</svg>`;

const bowls = {
  // wide round bowl (Burgundy)
  burgundy: "M18 28 Q18 92 40 110 Q62 92 62 28 Q40 16 18 28 Z",
  // tall tapered bowl (Bordeaux)
  bordeaux: "M26 18 Q22 90 40 110 Q58 90 54 18 Q40 10 26 18 Z",
  // medium U bowl (Chardonnay)
  chardonnay: "M22 30 Q24 90 40 110 Q56 90 58 30 Q40 18 22 30 Z",
  // sleek universal (Zalto / Lobmeyr)
  universal: "M24 16 Q20 92 40 110 Q60 92 56 16 Q40 8 24 16 Z",
};

const lineup = [
  { tag:"BORDEAUX", name:"ボルドー", brand:"リーデル / シュピゲラウ", bowl:bowls.bordeaux,
    desc:"縦長で力強い渋み・果実味をまとめる。カベルネやメルロー、しっかりした赤に。", from:"500" },
  { tag:"BURGUNDY", name:"ブルゴーニュ", brand:"リーデル / ザルト", bowl:bowls.burgundy,
    desc:"大きく開いたボウルが繊細な香りを最大化。ピノ・ノワールや熟成赤に。", from:"500" },
  { tag:"CHARDONNAY", name:"シャルドネ", brand:"リーデル ヴィノム", bowl:bowls.chardonnay,
    desc:"ふくよかな白の厚みと樽香を受け止める万能型。白ワイン全般に。", from:"500" },
  { tag:"ZALTO", name:"ザルト デンクアート", brand:"Zalto / オーストリア", bowl:bowls.universal,
    desc:"驚くほど軽く薄い、現代の名品。傾きの角度まで計算された一脚。", from:"1,100" },
  { tag:"RIEDEL VERITAS", name:"リーデル ヴェリタス", brand:"Riedel / 機械吹き最高峰", bowl:bowls.bordeaux,
    desc:"プロの定番。品種別設計で味わいの輪郭をくっきりと描く。", from:"800" },
  { tag:"LOBMEYR", name:"ロブマイヤー", brand:"Lobmeyr / ウィーン 手吹き", bowl:bowls.universal,
    desc:"紙のように薄い口当たり。ハンドメイドの極み。特別な一杯のために。", from:"2,800" },
];

const cards = document.getElementById('lineupCards');
cards.innerHTML = lineup.map(g => `
  <article class="glass-card">
    <div class="glass-visual">${glassSVG(g.bowl)}</div>
    <div class="body">
      <span class="glass-tag">${g.tag}</span>
      <h3>${g.name}</h3>
      <p class="brand-eg">${g.brand}</p>
      <p class="desc">${g.desc}</p>
      <p class="from">¥<b>${g.from}</b> / 脚〜</p>
    </div>
  </article>`).join('');
// add wine fill animation to first three (varietal) glasses
document.querySelectorAll('.glass-card .bowl').forEach(p=>{});

// ---- Simulator ----
const yen = n => '¥' + n.toLocaleString('ja-JP');
const SHIP = 2500;
const $tier = document.getElementById('simTier');
const $qty = document.getElementById('simQty');
const $times = document.getElementById('simTimes');
const $qtyLabel = document.getElementById('simQtyLabel');
const $rental = document.getElementById('simRental');
const $ship = document.getElementById('simShip');
const $total = document.getElementById('simTotal');

function calcSim(){
  const per = +$tier.value;
  const qty = +$qty.value;
  const times = +$times.value;
  const rental = per * qty * times;
  const ship = SHIP * times;
  $qtyLabel.textContent = qty + ' 脚';
  $rental.textContent = yen(rental);
  $ship.textContent = yen(ship);
  $total.textContent = yen(rental + ship);
}
[$tier,$qty,$times].forEach(el=>el.addEventListener('input', calcSim));
calcSim();

// ---- FAQ ----
const faqs = [
  { q:"本当に洗わずに返していいのですか？", a:"はい。ご利用後はそのまま専用ケースにお戻しいただき、同梱の伝票で返送するだけです。専門スタッフが洗浄・検品・ポリッシュまで行います。" },
  { q:"割ってしまったら弁償ですか？", a:"通常のご利用範囲での破損は料金に含まれており、ご請求はありません。安心してお使いください（故意・重過失・紛失は除きます）。" },
  { q:"どのくらいの期間借りられますか？", a:"標準は3〜4日（週末イベント想定）です。延長や長期・定期利用もご相談ください。飲食店さま向けには月額定額プランもあります。" },
  { q:"配送エリアと送料は？", a:"全国対応予定です。料金は往復 ¥2,500（首都圏目安／サイズ・地域で変動）。近郊は自社便も検討しています。" },
  { q:"何脚から借りられますか？", a:"2脚からご利用いただけます。最低利用金額は¥5,000です。大人数のワイン会や法人イベントは数十脚規模も対応します。" },
  { q:"飲食店ですが在庫を持たずに高級グラスを出せますか？", a:"まさにそのためのサービスです。BYO（持込可）店さま向けの定期プランで、在庫リスクなくザルトやロブマイヤーをご提供いただけます。" },
];
const faqList = document.getElementById('faqList');
faqList.innerHTML = faqs.map(f=>`
  <div class="faq-item">
    <button class="faq-q" type="button">${f.q}</button>
    <div class="faq-a"><p>${f.a}</p></div>
  </div>`).join('');
faqList.querySelectorAll('.faq-q').forEach(btn=>{
  btn.addEventListener('click',()=>btn.parentElement.classList.toggle('open'));
});

// ---- Reserve form (demo) ----
const form = document.getElementById('reserveForm');
const note = document.getElementById('formNote');
form.addEventListener('submit', e=>{
  e.preventDefault();
  const data = Object.fromEntries(new FormData(form).entries());
  if(!data.name || !data.email || !data.date){
    note.hidden=false; note.style.background="#fdeeee"; note.style.borderColor="#e6c5c5"; note.style.color="#a33";
    note.textContent="お名前・メール・ご利用日は必須です。"; return;
  }
  note.hidden=false; note.style.background="#f0f7ef"; note.style.borderColor="#cfe6cd"; note.style.color="#2c6b2e";
  note.textContent=`${data.name} さま、${data.date} の ${data.tier}・${data.qty}脚で予約リクエストを受け付けました（デモ）。確認メールを ${data.email} へお送りする想定です。`;
  note.scrollIntoView({behavior:'smooth',block:'center'});
});
