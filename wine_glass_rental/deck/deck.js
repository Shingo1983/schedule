const deck = document.getElementById('deck');
const slides = [...document.querySelectorAll('.slide')];
const bar = document.getElementById('bar');
const counter = document.getElementById('counter');
const hint = document.getElementById('hint');
const total = slides.length;
let idx = 0;

const pad = n => String(n).padStart(2, '0');

function render(){
  bar.style.width = ((idx) / (total - 1) * 100) + '%';
  counter.textContent = `${pad(idx + 1)} / ${pad(total)}`;
}
function go(i){
  idx = Math.max(0, Math.min(total - 1, i));
  slides[idx].scrollIntoView({behavior:'smooth', block:'start'});
  render();
}

// keyboard
document.addEventListener('keydown', e => {
  if(['ArrowRight','ArrowDown',' ','PageDown'].includes(e.key)){ e.preventDefault(); go(idx + 1); }
  else if(['ArrowLeft','ArrowUp','PageUp'].includes(e.key)){ e.preventDefault(); go(idx - 1); }
  else if(e.key === 'Home'){ e.preventDefault(); go(0); }
  else if(e.key === 'End'){ e.preventDefault(); go(total - 1); }
  else if(e.key.toLowerCase() === 'f'){ if(!document.fullscreenElement) document.documentElement.requestFullscreen(); else document.exitFullscreen(); }
  else if(e.key.toLowerCase() === 'p'){ e.preventDefault(); window.print(); }
});

// buttons
document.getElementById('next').addEventListener('click', () => go(idx + 1));
document.getElementById('prev').addEventListener('click', () => go(idx - 1));

// keep counter synced when user scrolls manually
const io = new IntersectionObserver(entries => {
  entries.forEach(en => {
    if(en.isIntersecting){
      const i = slides.indexOf(en.target);
      if(i >= 0){ idx = i; render(); }
    }
  });
}, {root: deck, threshold: 0.6});
slides.forEach(s => io.observe(s));

render();
setTimeout(() => hint.classList.add('fade'), 6000);
