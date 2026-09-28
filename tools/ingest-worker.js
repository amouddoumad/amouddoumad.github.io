/**
 * mad-arrivals-analytics — privacy-light visit counter for amouddoumad.github.io
 * deployed as a Cloudflare Worker (free plan). Why this design: HANDOFF.md section 10.
 *
 * Endpoints
 *   POST/GET /h      ingest one page view. The page sends ONLY {u,l,tz}
 *                    (random local device id, language, timezone). Country,
 *                    device/OS/browser and referrer are derived HERE, from the
 *                    request itself (cf.country, User-Agent, Referer) — the site
 *                    itself sends no personal data and we never store IPs
 *                    (Cloudflare strips them before this code runs).
 *   GET  /stats      JSON aggregates for the last 30 days (needs ?key= when the
 *                    STATS_KEY secret is set).
 *   GET  /           tiny built-in dashboard (same key rule).
 *
 * Worker bindings / secrets (dashboard or wrangler, see tools/wrangler.toml):
 *   ANALYTICS  Analytics Engine dataset binding, dataset name: madarrivals
 *   SALT       secret string mixed into the daily unique-visitor hash
 *   STATS_KEY  secret string protecting /stats and /  (optional but recommended)
 *
 * Uniqueness: du = sha256(deviceId | day | SALT) truncated — unique visitors can be
 * counted per day, but a visitor cannot be tracked across days without SALT.
 */

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
  "Cache-Control": "no-store",
};

function json(obj, init) {
  return new Response(JSON.stringify(obj), Object.assign({ headers: Object.assign({ "content-type": "application/json" }, CORS) }, init));
}

/* ---------- minimal User-Agent classification (kept tiny on purpose) ---------- */
function deviceOf(ua) {
  if (/ipad|tablet|playbook|silk/i.test(ua)) return "tablet";
  if (/mobi|iphone|ipod|windows phone/i.test(ua)) return "mobile";
  return /bot|crawl|spider/i.test(ua) ? "bot" : "desktop";
}
function osOf(ua) {
  if (/android/i.test(ua)) return "Android";
  if (/iphone|ipad|ipod|ios/i.test(ua)) return "iOS";
  if (/windows nt/i.test(ua)) return "Windows";
  if (/mac os x|macintosh/i.test(ua)) return "macOS";
  if (/cros/i.test(ua)) return "ChromeOS";
  if (/linux|x11/i.test(ua)) return "Linux";
  return "Other";
}
function browserOf(ua) {
  if (/edg(a|ios|e)?\//i.test(ua)) return "Edge";
  if (/crios|chrome|chromium/i.test(ua)) return "Chrome";
  if (/firefox|fxios/i.test(ua)) return "Firefox";
  if (/safari/i.test(ua)) return "Safari";
  if (/okhttp|dart|curl|python|go-http/i.test(ua)) return "script";
  return "Other";
}

async function sha(s) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return Array.from(new Uint8Array(buf).slice(0, 16)).map((x) => x.toString(16).padStart(2, "0")).join("");
}

/* ------------------------------- ingest ------------------------------------ */
async function ingest(request, url, env) {
  let u = "", l = "", tz = "";
  if (request.method === "POST") {
    try {
      const j = JSON.parse((await request.text()) || "{}");
      u = String(j.u || "").slice(0, 64); l = String(j.l || "").slice(0, 24); tz = String(j.tz || "").slice(0, 48);
    } catch (e) { /* tolerate junk payloads */ }
  } else {
    u = (url.searchParams.get("u") || "").slice(0, 64);
    l = (url.searchParams.get("l") || "").slice(0, 24);
    tz = (url.searchParams.get("tz") || "").slice(0, 48);
  }
  const ua = request.headers.get("user-agent") || "";
  if (/bot|crawl|spider|slurp|preview|facebookexternalhit|monitor|uptime|pingdom|headless/i.test(ua))
    return new Response(null, { status: 204, headers: CORS });

  const now = new Date();
  const day = now.toISOString().slice(0, 10);
  const country = (request.cf && request.cf.country) || "??";
  let refHost = "direct";
  try {
    const r = request.headers.get("referer") || request.headers.get("referrer");
    if (r) { const h = new URL(r).hostname; if (h && h !== new URL(request.url).hostname) refHost = h; }
  } catch (e) { /* leave direct */ }
  const du = u ? await sha(u + "|" + day + "|" + (env.SALT || "no-salt")) : "anon";

  try {
    env.ANALYTICS.writeDataPoint({
      blobs: [day, country, deviceOf(ua), osOf(ua), browserOf(ua), refHost, du, tz],
      ints: [now.getUTCHours(), l.length],
      // blobs order:  day  country  device  os  browser  refHost  du  tz
    });
  } catch (e) { /* never break the visitor over analytics */ }
  return new Response(null, { status: 204, headers: CORS });
}

/* -------------------------------- stats ------------------------------------ */
function dayago(n) { const d = new Date(); d.setUTCDate(d.getUTCDate() - n); return d.toISOString().slice(0, 10); }

async function q(env, sql, from) {
  const res = await env.ANALYTICS.query(sql, [["__ANALYTICS__", "table"], [from]]);
  return (res && res.data) || [];
}

async function stats(env, from) {
  const sel = (cols, group, order, limit) =>
    q(env, `SELECT ${cols} FROM ?? WHERE day >= ? GROUP BY ${group} ORDER BY ${order} LIMIT ${limit}`, from);
  const [byDay, byCountry, byRef, byDevice, byOs, byBrowser] = await Promise.all([
    sel("day, count(*) pv, count(DISTINCT du) uv", "day", "day", 40),
    sel("country, count(*) pv, count(DISTINCT du) uv", "country", "pv", 20),
    sel("refhost, count(*) pv, count(DISTINCT du) uv", "refhost", "pv", 15),
    sel("device, count(*) pv", "device", "pv", 6),
    sel("os, count(*) pv", "os", "pv", 8),
    sel("browser, count(*) pv", "browser", "pv", 8),
  ]);
  return json({ from, generatedAt: new Date().toISOString(), byDay, byCountry, byRef, byDevice, byOs, byBrowser });
}

/* ------------------------------- dashboard --------------------------------- */
function dash() {
  return new Response(`<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MAD Llegadas · visitas</title>
<style>
body{font:15px/1.45 system-ui,sans-serif;background:#0a0f1e;color:#eaf0ff;margin:0;padding:18px;max-width:760px;margin-inline:auto}
h1{font-size:19px}h2{font-size:13px;color:#8ba0c9;text-transform:uppercase;letter-spacing:.5px;margin:22px 0 8px}
.kpis{display:flex;gap:10px;flex-wrap:wrap}.kpi{background:#121c33;border:1px solid #22304e;border-radius:12px;padding:12px 16px;min-width:120px}
.kpi b{font-size:24px;display:block}.kpi span{color:#8ba0c9;font-size:12px}
.row{display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px dashed #22304e}
.row .nm{flex:0 0 150px;font-weight:600}.row .tr{flex:1;height:16px;background:#16223f;border-radius:6px;overflow:hidden}
.row .fi{height:100%;background:#5aa9e6;border-radius:6px}.row .ct{width:70px;text-align:right;font-weight:700;font-variant-numeric:tabular-nums}
.bad{color:#ff6b57;font-weight:700}
</style></head><body>
<h1>Madrid Llegadas — visitas (30 d)</h1><div id="a">cargando…</div>
<script>
const a=document.getElementById('a');
fetch('stats'+location.search,{cache:'no-store'}).then(r=>{if(!r.ok)throw new Error('HTTP '+r.status);return r.json()}).then(d=>{
 const pv=d.byDay.reduce((s,x)=>s+ +x.pv,0), uv=d.byDay.reduce((s,x)=>s+ +x.uv,0);
 const y=new Date(Date.now()-864e5).toISOString().slice(0,10);
 const yd=d.byDay.find(x=>x.day===y)||{pv:0,uv:0};
 const h=(t,rows,cols)=>{const mx=Math.max(1,...rows.map(r=>+r[cols[1]]));
  return '<h2>'+t+'</h2>'+(rows.length?rows.map(r=>'<div class="row"><div class="nm">'+r[cols[0]]+'</div><div class="tr"><div class="fi" style="width:'+(100*r[cols[1]]/mx)+'%"></div></div><div class="ct">'+r[cols[1]]+(cols[2]?' / '+r[cols[2]]:'')+'</div></div>').join(''):'<div class="bad">sin datos todavía</div>')};
 a.innerHTML='<div class="kpis"><div class="kpi"><b>'+pv+'</b><span>visitas 30 d</span></div>'+
 '<div class="kpi"><b>'+uv+'</b><span>visitantes (aprox.)</span></div>'+
 '<div class="kpi"><b>'+yd.pv+'</b><span>ayer</span></div></div>'+
 h('Días', d.byDay, ['day','pv','uv'])+h('Países', d.byCountry, ['country','pv','uv'])+
 h('Procedencia', d.byRef, ['refhost','pv','uv'])+h('Dispositivos', d.byDevice, ['device','pv'])+
 h('Sistemas', d.byOs, ['os','pv'])+h('Navegadores', d.byBrowser, ['browser','pv']);
}).catch(e=>{a.innerHTML='<div class="bad">'+e.message+' — abre con ?key=… si el secreto STATS_KEY está activo</div>'});
</script></body></html>`, { headers: Object.assign({ "content-type": "text/html; charset=utf-8" }, CORS) });
}

/* --------------------------------- router ---------------------------------- */
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
    if (env.STATS_KEY && (url.pathname === "/stats" || url.pathname === "/") && url.searchParams.get("key") !== env.STATS_KEY)
      return json({ error: "unauthorized" }, { status: 403 });
    if (url.pathname === "/h") return ingest(request, url, env);
    if (url.pathname === "/stats") return stats(env, dayago(29));
    if (url.pathname === "/" || url.pathname === "/dash") return dash();
    return new Response("nope", { status: 404, headers: CORS });
  },
};
