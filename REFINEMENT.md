# Market Memory — Refinement Pass (post-launch)

The app is live and working. This pass is about **making the numbers honest and decision-useful**, not adding features. Every item below either explains a number the user can't currently interpret, or exposes a weakness the current display hides.

Same constraints as before: **Python/SQL computes, UI only interprets.** Vanilla JS, no new dependencies. Verdict colors reserved for verdicts. Light theme, indigo chrome.

---

## 1. Monte Carlo — explainer box + assumptions drawer

**Problem:** the three tiles are readable but the user can't tell what they mean together, and one tile ("likely further move −7.3%") appears to contradict the forecast strip ("66% higher over 1 day"). They don't contradict — MC describes the **trough along the path**, the forecast describes the **endpoint** — but nothing on screen says so.

**Build:**
- **Relabel the tiles** so the path-vs-endpoint distinction is explicit. e.g. "likely further drawdown before bottoming" rather than "likely further move."
- **Plain-sentence box directly under the tiles**, deterministic template from Python (not LLM-written), reading roughly:
  > "Most simulated paths bottom about 7% below today before recovering; roughly 1 in 8 suggest the low is already in. A worse 1-in-10 case falls ~21% from here. These describe the **lowest point along the way**, not where it ends up — the forecast strip below covers endpoints."
- **Make the box tappable** → opens a "How this was calculated" drawer containing:
  - number of simulated paths
  - the analog episodes the distribution was built from, **with their dates**
  - the break-vs-unwind probability weight used
  - depth/duration parameters and whether any were estimated
  - the sample date range
  - "computed as of <timestamp>"

## 2. Monte Carlo — make the key assumption adjustable

**This is the highest-value item in the pass.** The trough and tail are driven overwhelmingly by one judgment input: the **break probability** (thesis break vs. positioning flush). Currently it's invisible and fixed.

**Build:** expose it in the assumptions drawer as a **slider** (e.g. 5%–50%). Moving it re-runs the simulation client-side (or re-requests) and updates the three tiles live. Default stays the engine's computed value, with the reason shown ("18% — revisions rising, credit calm").

**Why:** it converts the module from an oracle into an instrument. The user stops reading "$540" as fact and starts reading it as "$540 *if I accept 18%*." That's a more honest relationship with the tool and guards against over-trust.

## 3. Forecast strip — expose sample size and date range

**Problem:** SMH currently shows 66/68/65/82/77/81% higher across every horizon with full-green confidence bars and +36.2% median at a year. Uniformly strong everywhere with uniformly high confidence is the signature of **sample bias**, not edge — the analog samples are likely dominated by the 2023–2026 semiconductor bull run.

**Build:**
- Show **n** and the **date range of the analog samples** on each horizon row (compact — e.g. "n=14 · 2023–2026").
- **Penalize sample concentration in the confidence score.** If the samples span less than ~2 distinct market regimes or cluster in a narrow date window, confidence must drop materially. A base rate drawn entirely from one bull run cannot render as high confidence.
- Add a one-line warning above the strip when concentration is detected: "These odds are drawn mostly from <date range> — a single market regime. Treat as regime-specific, not a general rule."

**This is the most important item for decision quality.** The app should surface its own weaknesses rather than presenting them as confidence.

## 4. NEW — short-horizon stretch (1 week / 2 weeks)

**Ask:** the relative-strength module currently reports 20/50/100/200-day windows. Add **5-day and 10-day** stretch so the user can see how extended a ticker is on the week and fortnight, not just multi-month.

**Build:**
- Add `5` and `10` to `WINDOWS` in `relative_strength.py`.
- Surface them as a compact row near the top of the Stretch section — these are the **timing-relevant** windows, whereas 100/200-day are the **positioning-relevant** ones. Label them so the distinction is clear, e.g. a "Near term (1–2 weeks)" group and a "Positioning (3–12 months)" group.
- Note honestly in the plain text that short-window readings are noisier: they mean-revert faster and carry lower confidence by construction (fewer independent samples per unit time). The confidence scoring should already reflect this — verify it does.
- The near-term rows are what tell the user whether a snap-back has **already started** (e.g. SMH stretched +2.4σ on 100d but −1.1σ on 20d = the reversion is underway). Make that contrast visible rather than burying it in a list.

## 5. Tail risk in portfolio terms

Translate the tail percentage into the units the user actually sizes in: "a 1/3 tranche at your intended size loses roughly $X in the tail scenario." Requires a user-set intended position size (add to settings). Small addition, outsized effect on decision-making.

## 6. Monte Carlo → ladder levels

The trough distribution can name the staging levels directly. Emit e.g.:
> "25th-percentile trough ≈ $525 → tranche 2 · 10th-percentile ≈ $490 → tranche 3"

Connects the simulation to the staged-entry discipline instead of leaving the user to derive levels manually.

## 7. Staleness guard

Every computed module shows "computed as of <date/time>". If the underlying run is more than ~1 trading day old, visibly de-emphasize it (greyed, with a "refresh" affordance). A Monte Carlo conditioned on Friday's drawdown read on Wednesday is quietly wrong.

---

## Build order

1. §3 (forecast sample transparency) — highest decision-quality impact, small change
2. §4 (short-horizon stretch) — new user request, contained
3. §1 (MC explainer + drawer)
4. §7 (staleness) — cheap, prevents real errors
5. §2 (assumption slider)
6. §6 (ladder levels)
7. §5 (portfolio-terms tail) — needs a settings input first

Ship each before starting the next. Show the rendered result for §1–4 before wiring §5–7.
