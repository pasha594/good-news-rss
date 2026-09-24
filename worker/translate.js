// Cloudflare Worker: translate one news headline with Gemini (free tier) for
// the Good News dashboard's per-row "AI" button. The Gemini key lives only
// here, as a Worker secret; the page never sees it.
//
// POST /  body {"text": "<headline>", "target": "en" | "he" | "ar"}
//   200 {"translation": "...", "model": "...", "cached": bool}
//   4xx/5xx {"error": "busy" | "blocked" | "bad_request" | "origin" | "failed"}

const LANGS = { en: "English", he: "Hebrew", ar: "Arabic" };
const MAX_CHARS = 400;
const CALL_TIMEOUT_MS = 12000;
const DEADLINE_MS = 20000;               // total time a click may wait
const CONFIG_STATUSES = new Set([400, 401, 403, 404]);
const CACHE_TTL_S = 60 * 60 * 24 * 60;   // keep saved translations 60 days

export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin") || "";
    const allowed = (env.ALLOWED_ORIGINS || "").split(",").map(s => s.trim()).includes(origin);
    const cors = allowed ? { "Access-Control-Allow-Origin": origin, "Vary": "Origin" } : { "Vary": "Origin" };

    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: allowed ? 204 : 403,
        headers: { ...cors, "Access-Control-Allow-Methods": "POST",
                   "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Max-Age": "86400" },
      });
    }
    if (request.method !== "POST") return reply({ error: "bad_request" }, 405, cors);
    if (!allowed) return reply({ error: "origin" }, 403, cors);

    let body;
    try { body = JSON.parse(await request.text()); } catch { return reply({ error: "bad_request" }, 400, cors); }
    const text = typeof body.text === "string" ? body.text.trim() : "";
    const target = body.target;
    if (!text || text.length > MAX_CHARS || !LANGS[target]) return reply({ error: "bad_request" }, 400, cors);

    const cacheKey = target + ":" + await sha256(text);
    if (env.TRANSLATIONS) {
      try {
        const hit = await env.TRANSLATIONS.get(cacheKey, "json");
        if (hit) return reply({ ...hit, cached: true }, 200, cors);
      } catch { /* cache is best-effort */ }
    }

    if (!env.GEMINI_API_KEY) {
      console.log("GEMINI_API_KEY secret is not set");
      return reply({ error: "config" }, 500, cors);
    }

    const prompt = `Translate this news headline into ${LANGS[target]}. ` +
      "Reply with only the translated headline, nothing else.\n\n" + text;
    const models = (env.MODELS || "gemini-3.8-flash").split(",").map(s => s.trim()).filter(Boolean);
    let blocked = false;
    const started = Date.now();
    // The free tier sheds load in short spikes; one more pass after a brief
    // pause rescues many clicks without making the viewer wait too long.
    for (let pass = 0; pass < 2; pass++) {
      if (pass) await new Promise(r => setTimeout(r, 2000));
      let tried = 0, configErrors = 0;
      for (const model of models) {
        const left = DEADLINE_MS - (Date.now() - started);
        if (left < 1500) break;
        tried++;
        const r = await callGemini(env.GEMINI_API_KEY, model, prompt, Math.min(CALL_TIMEOUT_MS, left));
        if (r.status) console.log(`gemini ${model} -> HTTP ${r.status}`);
        if (CONFIG_STATUSES.has(r.status)) configErrors++;
        if (r.translation) {
          const out = { translation: r.translation, model };
          if (env.TRANSLATIONS) {
            try { await env.TRANSLATIONS.put(cacheKey, JSON.stringify(out), { expirationTtl: CACHE_TTL_S }); }
            catch { /* free-plan write limit reached: serve uncached */ }
          }
          return reply({ ...out, cached: false }, 200, cors);
        }
        if (r.blocked) blocked = true;   // another model may still translate it
      }
      if (blocked) break;
      // Every model rejected the request itself (bad/revoked key, retired
      // model names): retrying won't help, and "busy" would hide the cause.
      if (tried && configErrors === tried) return reply({ error: "config" }, 502, cors);
    }
    return reply({ error: blocked ? "blocked" : "busy" }, blocked ? 422 : 503, cors);
  },
};

// One Gemini call. Returns {translation} on success, {blocked: true} when the
// safety system withheld output, or {status} / {} for anything worth trying
// the next model for (overloaded, over quota, model unavailable, timeout).
async function callGemini(key, model, prompt, timeoutMs) {
  let res;
  try {
    res = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-goog-api-key": key },
      body: JSON.stringify({
        contents: [{ parts: [{ text: prompt }] }],
        generationConfig: { temperature: 0 },
        safetySettings: ["HARASSMENT", "HATE_SPEECH", "SEXUALLY_EXPLICIT", "DANGEROUS_CONTENT"]
          .map(c => ({ category: "HARM_CATEGORY_" + c, threshold: "BLOCK_NONE" })),
      }),
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch {
    return {};
  }
  if (!res.ok) return { status: res.status };
  let data;
  try { data = await res.json(); } catch { return {}; }
  if (data.promptFeedback && data.promptFeedback.blockReason) return { blocked: true };
  const cand = (data.candidates || [])[0];
  if (!cand) return {};
  const parts = (cand.content && cand.content.parts) || [];
  const raw = parts.filter(p => !p.thought).map(p => p.text || "").join("").trim();
  if (!raw) return ["SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"].includes(cand.finishReason)
    ? { blocked: true } : {};
  const translation = clean(raw);
  return translation ? { translation } : {};
}

// Models sometimes wrap the answer in quotes, bold or a label, lead with a
// "Here is the translation:" line, or offer extra lines. Keep the one real
// line and strip wrappers only when they are a single enclosing pair.
function clean(s) {
  const lines = s.split("\n").map(l => l.trim()).filter(Boolean);
  let line = lines[0] || "";
  if (lines.length > 1 && line.endsWith(":")) line = lines[1];
  line = line.replace(/^(translation|translated headline|headline)\s*:\s*/i, "");
  return stripOuter(line).trim();
}

function stripOuter(line) {
  const pairs = [['"', '"'], ["“", "”"], ["«", "»"], ["'", "'"], ["**", "**"]];
  for (const [a, b] of pairs) {
    if (line.length > a.length + b.length && line.startsWith(a) && line.endsWith(b)) {
      const inner = line.slice(a.length, line.length - b.length);
      // `"Hope" returns after "miracle"` starts and ends with quotes that are
      // not one pair. Hebrew acronyms (או"ם) put a quote between two letters;
      // those don't count.
      const probe = a === '"' ? inner.replace(/(?<=[א-ת])"(?=[א-ת])/g, "") : inner;
      if (!probe.includes(a) && !probe.includes(b)) return inner;
    }
  }
  return line;
}

async function sha256(s) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, "0")).join("");
}

function reply(obj, status, cors) {
  return new Response(JSON.stringify(obj), {
    status, headers: { ...cors, "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
  });
}
