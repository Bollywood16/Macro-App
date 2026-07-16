// Supabase Edge Function: mm-search
// Deploy name: mm-search
// NOT committed to the Macro-App repo (public repo) — paste this directly
// into the Supabase dashboard as a NEW function, same pattern as
// smooth-service and mm-journal.
//
// Secrets required (Project Settings -> Edge Functions -> Secrets):
//   APP_PASSPHRASE     - already exists (shared across every Market Memory
//                        function; Supabase Edge Function secrets are
//                        project-wide, so this should already be visible
//                        here — verify on first deploy).
//   ANTHROPIC_API_KEY  - already exists (reused from smooth-service).
//   No new secrets needed.
//
// Market Memory M5: natural-language Q&A over forecasts/decisions with
// SQL-grounded evidence (spec 10, MVP item 8). This function computes
// NOTHING itself — it is a thin tool-use orchestrator. Every number in its
// answer comes from a raw row returned by one of the two tools below, both
// of which are just HTTP calls to mm-journal (same "call mm-journal with
// x-app-key" pattern forecast_engine.py's mm_journal() client and
// trigger_forecast's GitHub-dispatch call already use). This keeps ALL
// Postgres access in exactly one place (mm-journal), per the M1 design —
// mm-search never holds its own database client.
//
// Uses raw fetch against the Messages API (no Anthropic SDK dependency,
// to avoid any Node-vs-Deno-edge-runtime compatibility risk) — same
// "no extra dependency" discipline as mm-journal's own GitHub API call.

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type, x-app-key",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...CORS, "Content-Type": "application/json" },
  });
}

const MM_JOURNAL_URL =
  "https://anzbpxqvibgpxnwgyqoc.supabase.co/functions/v1/mm-journal";
// Sonnet, not Opus: this function only routes tool calls and narrates
// already-retrieved rows (spec 10.2) — it never computes a probability or
// judges evidence quality, so it doesn't need Opus-tier reasoning.
const ANTHROPIC_MODEL = "claude-sonnet-5";
const MAX_TOOL_ROUNDS = 5;

const TOOLS = [
  {
    name: "query_forecasts",
    description:
      "Look up forecast history (probabilities, quantiles, confidence, " +
      "regime, drivers) for a ticker and/or time range. Use for questions " +
      "like 'what did the model say about SMH last month' or 'show recent " +
      "forecasts for XLK'.",
    input_schema: {
      type: "object",
      properties: {
        ticker: { type: "string", description: "Optional ticker filter, e.g. SMH" },
        model_version: { type: "string", description: "Optional exact model_version filter" },
        since: { type: "string", description: "Optional ISO timestamp lower bound on as_of_ts" },
        limit: { type: "integer", description: "Max rows, default 200" },
      },
      additionalProperties: false,
    },
  },
  {
    name: "query_decisions",
    description:
      "Look up the user's logged decisions (buy/sell/hold/watch/stand_down), " +
      "joined to the forecast each decision was made against and any matured " +
      "outcome. Use for questions like 'show times I stood down when the " +
      "model was bullish' or 'what did I do with SMH'.",
    input_schema: {
      type: "object",
      properties: {
        ticker: { type: "string", description: "Optional ticker filter, e.g. SMH" },
        action: {
          type: "string",
          enum: ["buy", "sell", "hold", "watch", "stand_down"],
          description: "Optional action filter",
        },
        since: { type: "string", description: "Optional ISO timestamp lower bound on decision created_at" },
        limit: { type: "integer", description: "Max rows, default 200" },
      },
      additionalProperties: false,
    },
  },
];

const SYSTEM_PROMPT = `You are Market Memory's decision-history assistant. \
You answer questions about the user's own ETF forecasts and decisions — \
never about the general market. You have two tools: query_forecasts and \
query_decisions, both backed by the user's real, stored records. \
\
Rules: \
- Every number, date, or count in your answer must come from a tool result \
  you actually received this turn. Never estimate, round suggestively, or \
  fill gaps from general knowledge. \
- If the tools return nothing relevant, say so plainly rather than guessing. \
- Call a tool whenever the question depends on specific records — do not \
  answer from the conversation alone. \
- Keep the final answer concise: a direct answer, then at most a few \
  supporting specifics (dates, tickers, numbers) drawn from the tool results. \
- This is a personal research tool, not investment advice — do not add \
  disclaimers, just answer factually from the data.`;

async function mmJournal(op: string, payload: Record<string, unknown>, passphrase: string) {
  const r = await fetch(MM_JOURNAL_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json", "x-app-key": passphrase },
    body: JSON.stringify({ op, payload }),
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body?.error || `mm-journal ${op} failed (${r.status})`);
  return body;
}

function summarize(op: string, result: any) {
  const rows = result?.forecasts ?? result?.decisions ?? [];
  if (op === "query_forecasts") {
    return rows.slice(0, 10).map((f: any) => ({
      ticker: f.ticker, as_of_ts: f.as_of_ts, horizon_days: f.horizon_days,
      p_positive: f.p_positive, confidence_label: f.confidence_label,
      recommendation_label: f.evidence_json?.recommendation_label,
    }));
  }
  if (op === "query_decisions") {
    return rows.slice(0, 10).map((d: any) => ({
      ticker: d.forecasts?.ticker, action: d.action, created_at: d.created_at,
      thesis: d.thesis, recommendation_label: d.forecasts?.evidence_json?.recommendation_label,
      outcome_abs_return: d.forecasts?.outcomes?.[0]?.abs_return ?? null,
    }));
  }
  return rows;
}

async function runTool(name: string, input: Record<string, unknown>, passphrase: string) {
  if (name === "query_forecasts") return mmJournal("query_forecasts", input, passphrase);
  if (name === "query_decisions") return mmJournal("query_decisions", input, passphrase);
  throw new Error(`unknown tool ${name}`);
}

async function callClaude(apiKey: string, messages: unknown[]) {
  const r = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-api-key": apiKey,
      "anthropic-version": "2023-06-01",
    },
    body: JSON.stringify({
      model: ANTHROPIC_MODEL,
      max_tokens: 2048,
      system: SYSTEM_PROMPT,
      tools: TOOLS,
      messages,
    }),
  });
  const body = await r.json();
  if (!r.ok) throw new Error(body?.error?.message || `Anthropic API error (${r.status})`);
  return body;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  if (req.method !== "POST") return json({ error: "POST only" }, 405);

  const pass = Deno.env.get("APP_PASSPHRASE");
  if (!pass || req.headers.get("x-app-key") !== pass) {
    return json({ error: "bad passphrase" }, 401);
  }

  const apiKey = Deno.env.get("ANTHROPIC_API_KEY");
  if (!apiKey) return json({ error: "ANTHROPIC_API_KEY not configured" }, 500);

  let body: any = {};
  try {
    body = await req.json();
  } catch {
    return json({ error: "invalid JSON body" }, 400);
  }
  const question = String(body.question ?? "").trim();
  if (!question) return json({ error: "missing question" }, 400);

  const messages: any[] = [{ role: "user", content: question }];
  const evidence: { tool: string; args: unknown; result_summary: unknown }[] = [];

  try {
    for (let round = 0; round < MAX_TOOL_ROUNDS; round++) {
      const resp = await callClaude(apiKey, messages);
      messages.push({ role: "assistant", content: resp.content });

      if (resp.stop_reason !== "tool_use") {
        const answer = (resp.content ?? [])
          .filter((b: any) => b.type === "text")
          .map((b: any) => b.text)
          .join("\n")
          .trim();
        return json({ answer, evidence });
      }

      const toolResults = [];
      for (const block of resp.content) {
        if (block.type !== "tool_use") continue;
        try {
          const result = await runTool(block.name, block.input, pass);
          const result_summary = summarize(block.name, result);
          evidence.push({ tool: block.name, args: block.input, result_summary });
          toolResults.push({
            type: "tool_result", tool_use_id: block.id,
            content: JSON.stringify(result),
          });
        } catch (e) {
          toolResults.push({
            type: "tool_result", tool_use_id: block.id,
            content: `error: ${String((e as Error)?.message ?? e)}`, is_error: true,
          });
        }
      }
      messages.push({ role: "user", content: toolResults });
    }
    return json({
      answer: "I wasn't able to finish gathering evidence for this within the "
        + "allotted tool-call rounds — try narrowing the question (a specific "
        + "ticker or time range).",
      evidence,
    });
  } catch (e) {
    return json({ error: "search_failed", detail: String((e as Error)?.message ?? e) }, 500);
  }
});
