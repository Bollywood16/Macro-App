// Supabase Edge Function: pm-synthesis
// Deploy name: pm-synthesis
// Secrets required (Project Settings -> Edge Functions -> Secrets):
//   ANTHROPIC_API_KEY  - your Anthropic key
//   APP_PASSPHRASE     - any phrase you choose; the page will ask you for it
//
// The function: verifies the passphrase, pulls the evidence digests from
// your public GitHub Pages site, compacts them to fit a prompt, and asks
// Claude for a PM synthesis under strict rules (cite only computed numbers,
// explain confidence rather than invent it, respect sampling honesty).

const PAGES_BASE = "https://bollywood16.github.io/Macro-App";
const MODEL = "claude-sonnet-5";
// Sonnet 5 runs adaptive thinking by default (thinking tokens count against
// max_tokens). 4500 was tight enough that thinking could consume the whole
// budget before any answer text was emitted, so every call fell through to
// the "unstructured" fallback. Give it real headroom.
const MAX_TOKENS = 8000;

// Claude 4.6+ models reject assistant-turn prefills (400), and relying on
// the model to emit a bare JSON text block is fragile (thinking can eat the
// token budget before any text block appears, or the model can wrap prose
// around the JSON). Forcing a strict tool call sidesteps both: tool_use.input
// arrives pre-parsed and, with strict:true, is guaranteed to validate against
// this schema — no JSON.parse/salvage step needed at all.
const PM_SYNTHESIS_SCHEMA = {
  type: "object",
  properties: {
    headline: { type: "string" },
    perspectives: {
      type: "array",
      items: {
        type: "object",
        properties: {
          stance: { type: "string" },
          thesis: { type: "string" },
          evidence: { type: "array", items: { type: "string" } },
          confidence_basis: { type: "string" },
          what_breaks_it: { type: "string" },
        },
        required: ["stance", "thesis", "evidence", "confidence_basis", "what_breaks_it"],
        additionalProperties: false,
      },
    },
    tripwires: { type: "array", items: { type: "string" } },
    analog_map: {
      type: "object",
      properties: {
        rhymes_with: { type: "string" },
        where_it_breaks: { type: "string" },
      },
      required: ["rhymes_with", "where_it_breaks"],
      additionalProperties: false,
    },
    mining_caution: { type: "string" },
    sizing_note: { type: "string" },
  },
  required: [
    "headline", "perspectives", "tripwires", "analog_map",
    "mining_caution", "sizing_note",
  ],
  additionalProperties: false,
};

const PM_SYNTHESIS_TOOL = {
  name: "emit_pm_synthesis",
  description:
    "Emit the structured PM synthesis. Call this exactly once, as your " +
    "entire response — do not also answer in plain text.",
  input_schema: PM_SYNTHESIS_SCHEMA,
  strict: true,
};

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type, x-app-key",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

async function fetchJson(path: string) {
  try {
    const r = await fetch(`${PAGES_BASE}/${path}`, { cache: "no-store" });
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

// Strip the heavy parts (paths, long episode lists) so the prompt stays lean.
function compactResearch(d: any, focus: string | null) {
  if (!d) return null;
  const out: any = { generated: d.meta?.generated_utc, assets: {} };
  for (const [t, a] of Object.entries<any>(d.assets ?? {})) {
    const isFocus = !focus || t === focus;
    const studies: any = {};
    for (const [k, s] of Object.entries<any>(a.studies ?? {})) {
      if (!k.endsWith("|uptrend") && isFocus === false) continue;
      studies[k] = {
        summary: s.summary,
        conjunctions_searched: s.conjunctions_searched,
        claims: (s.claims ?? []).slice(0, isFocus ? 8 : 3),
        recent_episodes: (s.episodes ?? []).slice(-(isFocus ? 8 : 3))
          .map((e: any) => ({
            date: e.date, regimes: e.regimes,
            fwd_126d_pct: e.fwd_126d_pct, max_dd_pct: e.max_dd_pct,
            complete: e.complete,
            news: (e.news ?? [])?.slice(0, 3),
          })),
      };
    }
    out.assets[t] = { label: a.label, current: a.current, studies };
  }
  return out;
}

function compactRotation(d: any) {
  if (!d) return null;
  return {
    generated: d.meta?.generated_utc,
    proxies: d.meta?.proxies,
    current: d.current,
    rs_table: d.rs_table,
    claims: (d.claims ?? []).slice(0, 10),
    conjunctions_searched: d.conjunctions_searched,
    recent_regime_onsets: (d.episodes ?? []).slice(-10),
  };
}

const SYSTEM = `You are the portfolio-manager synthesis layer of a personal
research app. You are rigorous, skeptical, and allergic to false precision.

HARD RULES — violating any of these makes your output worse than useless:
1. Every number you cite must appear verbatim in the EVIDENCE JSON. You do
   not compute, extrapolate, or estimate statistics. If a number you want
   is absent, say "not in evidence."
2. Confidence is COMPUTED upstream (sample size x consistency x conjunction
   depth x decade coverage). You explain what a score means; you never
   assign your own. A claim's confidence_label of "low / likely mined"
   means you must treat it as probable noise and say so.
3. Prefer "uptrend"-sampled studies (independent episodes). If you cite an
   "all"-sampled figure, flag its autocorrelation in the same sentence.
4. Regime tags lag at inflections — historical "rising revisions" included
   dates months before major tops. Never present a regime tag as a
   guarantee; identify the tripwire that would flip it.
5. conjunctions_searched tells you how many combinations were mined. Weigh
   "never happened" claims against that denominator explicitly.
6. News items are dated artifacts from the trigger day. You may use them.
   You may NOT supply what you remember happening around historical dates —
   that is hindsight contamination, the failure mode this app exists to kill.
7. No position sizes, no "you should buy." You may state the sizing
   arithmetic pattern: worst same-cohort interim drawdown x position size
   must not exceed the user's stated portfolio tolerance.
8. Genuine disagreement between perspectives is required, not decoration.
   If the evidence is thin, the honest headline is "the evidence is thin."

Call the emit_pm_synthesis tool exactly once to deliver your answer — do not
respond in plain text. Its fields:
 headline: one sentence, the single most decision-relevant read
 perspectives: 2-4 entries with genuinely different stances, each -
   stance: short label
   thesis: 2-3 sentences
   evidence: claim or stat citations, verbatim numbers
   confidence_basis: which computed scores/ns support this and why that's
     strong or weak
   what_breaks_it: the specific observable that falsifies this stance
 tripwires: specific, observable conditions to watch, each one line
 analog_map: rhymes_with (which historical episodes and why), where_it_breaks
   (how today differs from those analogs)
 mining_caution: one sentence on searched-count vs claims cited
 sizing_note: the arithmetic pattern applied to the worst relevant drawdown
   in evidence, no recommendation`;

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  if (req.method !== "POST") {
    return new Response("POST only", { status: 405, headers: CORS });
  }

  const pass = Deno.env.get("APP_PASSPHRASE");
  if (!pass || req.headers.get("x-app-key") !== pass) {
    return new Response(JSON.stringify({ error: "bad passphrase" }),
      { status: 401, headers: { ...CORS, "Content-Type": "application/json" } });
  }

  const key = Deno.env.get("ANTHROPIC_API_KEY");
  if (!key) {
    return new Response(JSON.stringify({ error: "ANTHROPIC_API_KEY not set" }),
      { status: 500, headers: { ...CORS, "Content-Type": "application/json" } });
  }

  let body: any = {};
  try { body = await req.json(); } catch { /* defaults */ }
  const focus: string | null = body.focus ?? null;
  const question: string = (body.question ?? "").slice(0, 500);

  const [research, rotation] = await Promise.all([
    fetchJson("data/research_digest.json"),
    fetchJson("data/rotation_digest.json"),
  ]);
  if (!research && !rotation) {
    return new Response(JSON.stringify({
      error: "No digests found on Pages. Run the engine workflows first.",
    }), { status: 502, headers: { ...CORS, "Content-Type": "application/json" } });
  }

  const evidence = {
    research: compactResearch(research, focus),
    rotation: compactRotation(rotation),
  };

  const userMsg =
    `EVIDENCE JSON:\n${JSON.stringify(evidence)}\n\n` +
    `FOCUS ASSET: ${focus ?? "portfolio-wide"}\n` +
    `USER QUESTION: ${question || "General read on the current setup."}`;

  const resp = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-api-key": key,
      "anthropic-version": "2023-06-01",
    },
    body: JSON.stringify({
      model: MODEL,
      max_tokens: MAX_TOKENS,
      system: SYSTEM,
      thinking: { type: "adaptive" },
      output_config: { effort: "medium" },
      tools: [PM_SYNTHESIS_TOOL],
      tool_choice: { type: "tool", name: "emit_pm_synthesis" },
      messages: [
        { role: "user", content: userMsg },
      ],
    }),
  });

  if (!resp.ok) {
    const detail = await resp.text();
    console.error("anthropic_error", resp.status, detail);
    return new Response(JSON.stringify({ error: "anthropic_error", detail }),
      { status: 502, headers: { ...CORS, "Content-Type": "application/json" } });
  }

  const data = await resp.json();

  if (data.stop_reason === "refusal") {
    console.error("anthropic_refusal", data.stop_details);
    return new Response(JSON.stringify({
      error: "anthropic_refusal",
      detail: data.stop_details?.explanation || "Claude declined this request.",
    }), { status: 502, headers: { ...CORS, "Content-Type": "application/json" } });
  }

  const toolUse = (data.content ?? [])
    .find((b: any) => b.type === "tool_use" && b.name === "emit_pm_synthesis");

  let parsed: any;
  if (toolUse && toolUse.input && typeof toolUse.input === "object") {
    // strict:true guarantees this already validates against the schema.
    parsed = toolUse.input;
  } else {
    // The model didn't call the tool (rare with tool_choice forcing it, but
    // can happen on max_tokens truncation). Salvage whatever text exists and
    // surface it honestly rather than silently rendering a blank card.
    console.error("anthropic_no_tool_use", data.stop_reason,
      JSON.stringify(data.content ?? []).slice(0, 500));
    const text = (data.content ?? [])
      .filter((b: any) => b.type === "text")
      .map((b: any) => b.text).join("\n")
      .replace(/```json|```/g, "").trim();
    try { parsed = JSON.parse(text); }
    catch {
      const cut = text.lastIndexOf("}");
      try { parsed = JSON.parse(text.slice(0, cut + 1)); }
      catch {
        const reason = data.stop_reason === "max_tokens"
          ? "The model ran out of budget before finishing (stop_reason: max_tokens). Try a narrower question."
          : text || `The model returned no usable content (stop_reason: ${data.stop_reason ?? "unknown"}).`;
        parsed = {
          headline: "Synthesis (unstructured)",
          perspectives: [{ stance: "PM read", thesis: reason,
            evidence: [], confidence_basis: "Formatting fallback — the structured tool call did not come back; this is raw/diagnostic text, not a computed synthesis.",
            what_breaks_it: "" }],
          tripwires: [], analog_map: { rhymes_with: "", where_it_breaks: "" },
          mining_caution: "", sizing_note: "",
        };
      }
    }
  }

  return new Response(JSON.stringify(parsed),
    { headers: { ...CORS, "Content-Type": "application/json" } });
});
