/* ==========================================================================
   glossary.js — plain-English glosses for every technical term the app
   renders. Shared by every page (Redesign directive Stage C).

   Usage: gloss("RSI") returns the HTML for a tap-to-expand term; drop it
   directly into any template string in place of the raw label, e.g.
     `<div class="l">${gloss("p_positive","Probability positive")}</div>`
   A single delegated click handler (registered once, below) makes every
   .gloss span tappable regardless of how many times its container gets
   re-rendered — no re-wiring needed after innerHTML updates.
   ========================================================================== */

const GLOSSARY = {
  "RSI": "Relative Strength Index — how fast and how far a price has moved recently. Above 70 = stretched up, below 30 = stretched down.",
  "MACD": "Moving Average Convergence/Divergence — compares two moving averages to gauge shifting momentum.",
  "extension": "How far a price has run above (or below) its long-term (200-day) trend line, as a percent.",
  "deflated confidence": "A confidence score adjusted down for how many combinations were searched to find this claim, so a lucky-looking pattern doesn't get overstated.",
  "Brier score": "How well-calibrated a probability forecast was, on a 0-1 scale — lower is better, 0 is perfect.",
  "regime": "The market backdrop at a point in time (e.g. calm/stressed volatility, rising/falling rates) used to condition historical comparisons.",
  "drawdown": "The worst peak-to-trough decline during a period.",
  "analog": "A historical episode whose starting conditions closely resemble today's, used as a comparison case.",
  "uptrend sampling": "Counting each rally once — not every day inside it — so one long run can't masquerade as many independent data points.",
  "conjunction": "A specific combination of regime conditions (e.g. calm VIX + rising rates) used to search for a pattern.",
  "conjunctions searched": "How many regime-condition combinations were tried before landing on this one — the denominator for judging if a claim might just be mined noise.",
  "HMM": "Hidden Markov Model — a statistical way of inferring which unobserved market 'regime' is most likely, from observed data alone.",
  "VIX": "Expected 30-day stock-market volatility. Under 20 = calm, over 30 = stressed.",
  "HY OAS": "High-Yield Option-Adjusted Spread — the extra yield junk-bond investors demand over Treasuries. Widening = credit stress building.",
  "p_positive": "The model's estimated probability the return over this horizon is positive.",
  "p_beat_benchmark": "The model's estimated probability this beats SPY over this horizon.",
  "q20": "20th-percentile outcome — a below-average case, worse than roughly 80% of historical analogs.",
  "q80": "80th-percentile outcome — an above-average case, better than roughly 80% of historical analogs.",
  "confidence": "How much the computed sample size, consistency, and search-depth support a claim — never a vibe.",
  "n_independent": "The number of historically independent episodes (not overlapping days) behind a statistic.",
  "opportunity score": "A single ranking number combining probability, expected downside, and confidence — used only to order this list, not as a standalone signal.",
  "relative strength": "Performance versus a benchmark (usually SPY) — positive means outperforming, negative means underperforming.",
  "200dma": "200-day moving average — a common long-term trend reference line.",
  "50dma": "50-day moving average — a common medium-term trend reference line.",
  "expected max adverse": "The typical worst dip below entry this setup has seen before it worked out, in historical analogs.",
  "leadership": "Which group of sectors (cyclical, defensive, small-cap, etc.) has outperformed SPY by the widest margin recently.",
  "purged cross-validation": "A model-testing method that removes training samples whose outcome window overlaps the test period, so the model can't accidentally peek at the answer.",
  "from 20-day high": "How far the current price sits below its highest close of the last month. 0% = at the highs; a large negative number = a pullback in progress.",
  "log loss": "A stricter scoring rule than Brier score — it punishes confident-but-wrong predictions especially hard. Lower is better.",
  "interval coverage": "How often the actual result landed inside the model's predicted 20th-80th percentile range. Should be close to 60% if the model is well-calibrated.",
  "rates": "The Treasury-yield regime (rising/falling/flat) used as one of the conditioning dimensions for historical comparisons.",
  "credit": "The high-yield credit-spread regime (widening/tightening/flat) — widening usually means rising stress.",
  "positioning": "Futures-market positioning (CFTC leveraged-money net position) — a snapshot-only regime dimension, not conditioned on historically.",
  "options": "CBOE put/call ratio regime — a snapshot-only sentiment dimension, not conditioned on historically.",
};
const GLOSSARY_LOOKUP = {};
for(const k of Object.keys(GLOSSARY)) GLOSSARY_LOOKUP[k.toLowerCase()] = k;

function gloss(term, displayText){
  const label = displayText != null ? displayText : term;
  const key = GLOSSARY_LOOKUP[String(term).toLowerCase()];
  if(!key) return label;
  return `<span class="gloss" data-term="${escGlossHtml(key)}">${escGlossHtml(label)}</span>`;
}
function escGlossHtml(s){
  const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML;
}

document.addEventListener("click", (e)=>{
  const el = e.target.closest(".gloss");
  document.querySelectorAll(".gloss-pop.open").forEach(p=>{
    if(!el || p !== el._pop) p.classList.remove("open");
  });
  if(!el) return;
  e.stopPropagation();
  if(!el._pop){
    const pop = document.createElement("span");
    pop.className = "gloss-pop";
    pop.textContent = GLOSSARY[el.dataset.term] || "";
    el.insertAdjacentElement("afterend", pop);
    el._pop = pop;
  }
  el._pop.classList.toggle("open");
});
