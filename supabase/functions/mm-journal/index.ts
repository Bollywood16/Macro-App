// Supabase Edge Function: mm-journal
// Deploy name: mm-journal
// NOT committed to the Macro-App repo (public repo) — paste this directly
// into the Supabase dashboard, same pattern as smooth-service.
//
// Secrets required (Project Settings -> Edge Functions -> Secrets):
//   APP_PASSPHRASE  - already exists (reused from smooth-service)
//   GITHUB_PAT      - M3: fine-grained PAT scoped to ONLY
//                     Bollywood16/Macro-App, "Actions: Read and write"
//                     permission and nothing else. Used solely to dispatch
//                     the Forecast Engine workflow on demand — this
//                     function still computes nothing itself.
//   SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY - auto-injected by Supabase,
//   no action needed.
//
// Market Memory M1 write path (+ M3 read/dispatch ops, + M4 outcome ops,
// + M5 search/scorecard/registry ops). This function does NOT compute
// anything — it only inserts and reads rows in the tables created by
// db/001_market_memory_schema.sql and db/002_model_registry.sql, and (M3)
// kicks off the Python forecast job via the GitHub Actions API so the UI
// can get an on-demand recompute at a user-supplied price. Forecast
// numbers are always supplied by the caller (the quant job in
// scripts/forecast_engine.py). RLS denies every role but service_role,
// and service_role itself has no UPDATE/DELETE grant on these tables, so
// this function cannot overwrite a row even if asked to — corrections go
// through an `amendments` insert.
//
// M5's mm-search function calls query_forecasts/query_decisions on this
// function over HTTP (same passphrase, same as any other caller) rather
// than holding its own Postgres client — this stays the single place
// Market Memory's SQL lives, per the M1 design.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

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

// Only the DB's NOT NULL columns (minus ones with defaults) are required
// here — everything else is validated by the table constraints themselves.
const REQUIRED_FIELDS: Record<string, string[]> = {
  create_quote_snapshot: ["ticker", "price", "source"],
  create_forecast: [
    "ticker", "as_of_ts", "effective_price", "quote_snapshot_id",
    "horizon_days", "model_version",
  ],
  create_decision: ["forecast_id", "action"],
  get_forecast: ["forecast_id"],
  trigger_forecast: ["ticker"],
  get_latest_forecast: ["ticker"],
  list_decisions: [],
  list_pending_outcomes: [],
  create_outcome: ["forecast_id", "evaluated_at"],
  query_forecasts: [],
  query_decisions: [],
  list_unactioned_forecasts: [],
  get_calibration_data: [],
  create_registry_entry: ["model_version", "status"],
  list_registry_entries: [],
  // Market Memory tear-sheet upgrade (BUILD.md / db/003_tearsheet_layer):
  // episodes.py reads the library back here to merge cause/what_ended_it
  // into a fresh statistical-analog scan (list_episodes), and both the
  // Python fingerprinting pass and the handoff write-back path use
  // upsert_episode — insert for a newly-found date, update for annotating
  // an existing one — matching episodes' select/insert/update grant (no
  // delete) in db/003.
  list_episodes: ["asset"],
  upsert_episode: ["asset", "event_date"],
};

const GITHUB_REPO = "Bollywood16/Macro-App";

function missingFields(op: string, payload: Record<string, unknown>) {
  return (REQUIRED_FIELDS[op] ?? []).filter(
    (f) => payload[f] === undefined || payload[f] === null || payload[f] === ""
  );
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  if (req.method !== "POST") return json({ error: "POST only" }, 405);

  const pass = Deno.env.get("APP_PASSPHRASE");
  if (!pass || req.headers.get("x-app-key") !== pass) {
    return json({ error: "bad passphrase" }, 401);
  }

  let body: any = {};
  try {
    body = await req.json();
  } catch {
    return json({ error: "invalid JSON body" }, 400);
  }
  const op = body.op as string;
  const payload = (body.payload ?? {}) as Record<string, unknown>;

  if (!op || !(op in REQUIRED_FIELDS)) {
    return json(
      { error: "unknown op", known_ops: Object.keys(REQUIRED_FIELDS) },
      400,
    );
  }
  const missing = missingFields(op, payload);
  if (missing.length) {
    return json({ error: "missing fields", missing }, 400);
  }

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  try {
    switch (op) {
      case "create_quote_snapshot": {
        const { data, error } = await supabase
          .from("quote_snapshots").insert(payload).select().single();
        if (error) throw error;
        return json({ quote_snapshot: data });
      }

      case "create_forecast": {
        const { data, error } = await supabase
          .from("forecasts").insert(payload).select().single();
        if (error) throw error;
        return json({ forecast: data });
      }

      case "create_decision": {
        const { data, error } = await supabase
          .from("decisions").insert(payload).select().single();
        if (error) throw error;
        return json({ decision: data });
      }

      case "get_forecast": {
        const { data: forecast, error: fErr } = await supabase
          .from("forecasts").select("*")
          .eq("id", payload.forecast_id).single();
        if (fErr) throw fErr;

        const { data: quote_snapshot, error: qErr } = await supabase
          .from("quote_snapshots").select("*")
          .eq("id", forecast.quote_snapshot_id).single();
        if (qErr) throw qErr;

        const { data: decisions, error: dErr } = await supabase
          .from("decisions").select("*")
          .eq("forecast_id", forecast.id)
          .order("created_at", { ascending: true });
        if (dErr) throw dErr;

        const { data: outcome, error: oErr } = await supabase
          .from("outcomes").select("*")
          .eq("forecast_id", forecast.id)
          .order("evaluated_at", { ascending: false })
          .limit(1).maybeSingle();
        if (oErr) throw oErr;

        return json({ forecast, quote_snapshot, decisions, outcome });
      }

      case "trigger_forecast": {
        const pat = Deno.env.get("GITHUB_PAT");
        if (!pat) return json({ error: "GITHUB_PAT not configured" }, 500);

        const inputs: Record<string, string> = { ticker: String(payload.ticker) };
        if (payload.price !== undefined && payload.price !== null && payload.price !== "") {
          inputs.price = String(payload.price);
        }
        if (payload.source) {
          inputs.source = String(payload.source);
        }

        const gh = await fetch(
          `https://api.github.com/repos/${GITHUB_REPO}/actions/workflows/forecast-engine.yml/dispatches`,
          {
            method: "POST",
            headers: {
              "Authorization": `Bearer ${pat}`,
              "Accept": "application/vnd.github+json",
              "X-GitHub-Api-Version": "2022-11-28",
              "User-Agent": "mm-journal",
              "Content-Type": "application/json",
            },
            body: JSON.stringify({ ref: "main", inputs }),
          },
        );
        if (gh.status !== 204) {
          const detail = await gh.text();
          return json({ error: "github_dispatch_failed", detail }, 502);
        }
        return json({ dispatched: true });
      }

      case "get_latest_forecast": {
        const { data: latest, error: lErr } = await supabase
          .from("forecasts").select("as_of_ts")
          .eq("ticker", payload.ticker)
          .order("as_of_ts", { ascending: false })
          .limit(1).maybeSingle();
        if (lErr) throw lErr;
        if (!latest) return json({ forecasts: [] });

        const { data: forecasts, error: fErr } = await supabase
          .from("forecasts").select("*")
          .eq("ticker", payload.ticker)
          .eq("as_of_ts", latest.as_of_ts)
          .order("horizon_days");
        if (fErr) throw fErr;
        return json({ forecasts });
      }

      case "list_decisions": {
        const limit = Number(payload.limit) > 0 ? Number(payload.limit) : 100;
        const { data, error } = await supabase
          .from("decisions")
          .select(`*, forecasts (
            ticker, horizon_days, as_of_ts, effective_price, p_positive,
            p_beat_benchmark, confidence_label, confidence_score,
            evidence_json, features_json, outcomes (*)
          )`)
          .order("created_at", { ascending: false })
          .limit(limit);
        if (error) throw error;
        return json({ decisions: data });
      }

      case "list_pending_outcomes": {
        const limit = Number(payload.limit) > 0 ? Number(payload.limit) : 500;
        const { data, error } = await supabase
          .from("forecasts")
          .select("*, outcomes!left(id)")
          .is("outcomes.id", null)
          .order("as_of_ts", { ascending: true })
          .limit(limit);
        if (error) throw error;
        return json({ forecasts: (data ?? []).map(({ outcomes, ...f }) => f) });
      }

      case "create_outcome": {
        const { data, error } = await supabase
          .from("outcomes").insert(payload).select().single();
        if (error) throw error;
        return json({ outcome: data });
      }

      case "query_forecasts": {
        const limit = Number(payload.limit) > 0 ? Number(payload.limit) : 200;
        // Home watchlist (index.html) passes view:"watchlist" and only
        // needs a thin per-row slice; mm-search's Q&A tool calls this same
        // op with no view and still needs the full row (p_positive,
        // evidence_json.recommendation_label), so "*" stays the default.
        const WATCHLIST_SELECT = `ticker, as_of_ts, effective_price, horizon_days,
          confidence_score, confidence_label,
          verdict:evidence_json->tearsheet_extras->dip_context->verdict,
          ret_1d:features_json->query_features->ret_1d`;
        let q = supabase.from("forecasts")
          .select(payload.view === "watchlist" ? WATCHLIST_SELECT : "*")
          .order("as_of_ts", { ascending: false }).limit(limit);
        if (payload.ticker) q = q.eq("ticker", payload.ticker);
        if (payload.model_version) q = q.eq("model_version", payload.model_version);
        if (payload.since) q = q.gte("as_of_ts", payload.since);
        const { data, error } = await q;
        if (error) throw error;
        return json({ forecasts: data });
      }

      case "query_decisions": {
        const limit = Number(payload.limit) > 0 ? Number(payload.limit) : 200;
        // forecast_id is NOT NULL on decisions, so !inner never drops a row —
        // it's only here so .eq("forecasts.ticker", ...) is filterable.
        let q = supabase.from("decisions")
          .select(`*, forecasts!inner (
            ticker, horizon_days, as_of_ts, effective_price, p_positive,
            p_beat_benchmark, confidence_label, confidence_score,
            evidence_json, features_json, outcomes (*)
          )`)
          .order("created_at", { ascending: false })
          .limit(limit);
        if (payload.action) q = q.eq("action", payload.action);
        if (payload.ticker) q = q.eq("forecasts.ticker", payload.ticker);
        if (payload.since) q = q.gte("created_at", payload.since);
        const { data, error } = await q;
        if (error) throw error;
        return json({ decisions: data });
      }

      case "list_unactioned_forecasts": {
        const limit = Number(payload.limit) > 0 ? Number(payload.limit) : 200;
        const { data, error } = await supabase
          .from("forecasts")
          .select("*, decisions!left(id)")
          .is("decisions.id", null)
          .order("as_of_ts", { ascending: false })
          .limit(limit);
        if (error) throw error;
        return json({ forecasts: (data ?? []).map(({ decisions, ...f }) => f) });
      }

      case "get_calibration_data": {
        // features_json/as_of_ts are pulled through for the Scorecards Misses
        // view (Stage D3), which groups misses by regime/leadership/source —
        // all of which live inside features_json, not their own columns.
        const limit = Number(payload.limit) > 0 ? Number(payload.limit) : 2000;
        const { data, error } = await supabase
          .from("forecasts")
          .select(`ticker, horizon_days, model_version, confidence_label, p_positive,
            as_of_ts, features_json,
            outcomes!inner (event_occurred, brier, log_loss, interval_covered)`)
          .order("as_of_ts", { ascending: false })
          .limit(limit);
        if (error) throw error;
        const rows = (data ?? []).flatMap((f: any) =>
          (f.outcomes ?? []).map((o: any) => ({
            ticker: f.ticker, horizon_days: f.horizon_days,
            model_version: f.model_version, confidence_label: f.confidence_label,
            p_positive: f.p_positive, as_of_ts: f.as_of_ts, features_json: f.features_json,
            event_occurred: o.event_occurred,
            brier: o.brier, log_loss: o.log_loss, interval_covered: o.interval_covered,
          })),
        );
        return json({ rows });
      }

      case "list_episodes": {
        const { data, error } = await supabase
          .from("episodes").select("*")
          .eq("asset", payload.asset)
          .order("event_date", { ascending: false });
        if (error) throw error;
        return json({ episodes: data });
      }

      case "upsert_episode": {
        const { data, error } = await supabase
          .from("episodes")
          .upsert(payload, { onConflict: "asset,event_date" })
          .select().single();
        if (error) throw error;
        return json({ episode: data });
      }

      case "create_registry_entry": {
        const { data, error } = await supabase
          .from("model_registry").insert(payload).select().single();
        if (error) throw error;
        return json({ entry: data });
      }

      case "list_registry_entries": {
        const limit = Number(payload.limit) > 0 ? Number(payload.limit) : 100;
        const { data, error } = await supabase
          .from("model_registry").select("*")
          .order("created_at", { ascending: false })
          .limit(limit);
        if (error) throw error;
        return json({ entries: data });
      }

      default:
        return json({ error: "unhandled op" }, 500);
    }
  } catch (e) {
    return json({ error: "db_error", detail: String(e?.message ?? e) }, 500);
  }
});
