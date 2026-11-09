"use strict";
const state = { days: 30, source: "all", person: "" };
const LABEL = { emby: "Emby", jellyfin: "Jellyfin" };
const TYPES = { movie: "Films", episode: "Épisodes", audio: "Musique", musicvideo: "Clips", audiobook: "Livres audio", tvchannel: "TV en direct", trailer: "Bandes-annonces", video: "Vidéos" };
const DAYS = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"];
let chart = null;
let lastData = null;

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const nf = new Intl.NumberFormat("fr-FR");

function dur(seconds) {
  const s = Math.max(0, Math.round(seconds || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  if (h >= 100) return `${nf.format(h)} h`;
  if (h > 0) return `${h} h ${String(m).padStart(2, "0")}`;
  if (m > 0) return `${m} min`;
  return `${s} s`;
}
function ago(ts) {
  if (!ts) return "jamais";
  const min = Math.round((Date.now() - new Date(ts.replace(" ", "T")).getTime()) / 60000);
  if (min < 1) return "à l'instant";
  if (min < 60) return `il y a ${min} min`;
  if (min < 1440) return `il y a ${Math.round(min / 60)} h`;
  return `il y a ${Math.round(min / 1440)} j`;
}
function fmtDate(ts) {
  const d = new Date(ts.replace(" ", "T"));
  return isNaN(d) ? esc(ts) : d.toLocaleString("fr-FR", { day: "2-digit", month: "2-digit", year: "2-digit", hour: "2-digit", minute: "2-digit" });
}
async function api(path, options) {
  const r = await fetch(path, options);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

/* ---------- rendu ---------- */
function splitTitle(row) {
  return `Emby : ${dur(row.emby)} · Jellyfin : ${dur(row.jellyfin)} · ${nf.format(row.plays)} lecture${row.plays > 1 ? "s" : ""}`;
}
function barList(id, rows) {
  const el = $(id);
  if (!rows.length) { el.innerHTML = '<div class="none">Aucune donnée sur cette période.</div>'; return; }
  const max = Math.max(...rows.map((r) => r.seconds), 1);
  el.innerHTML = rows.map((r) => {
    const seg = (k) => r[k] > 0 ? `<i class="seg-${k}" style="width:${(r[k] / max) * 100}%"></i>` : "";
    return `<div class="bar-row" title="${esc(splitTitle(r))}">
      <span class="name">${esc(r.name)}</span><span class="val">${dur(r.seconds)}</span>
      <span class="track">${seg("emby")}${seg("jellyfin")}</span></div>`;
  }).join("");
}
function renderKpis(t) {
  const total = t.seconds || 0;
  const pct = (v) => total ? Math.round((v / total) * 100) : 0;
  $("kpis").innerHTML = `
    <div class="kpi"><div class="label">Temps de visionnage</div><div class="value">${dur(total)}</div>
      <div class="hint">soit ${(total / 86400).toLocaleString("fr-FR", { maximumFractionDigits: 1 })} jours cumulés</div></div>
    <div class="kpi"><div class="label">Lectures</div><div class="value">${nf.format(t.plays)}</div>
      <div class="hint">${t.plays ? dur(total / t.plays) + " en moyenne" : "—"}</div></div>
    <div class="kpi"><div class="label">Utilisateurs actifs</div><div class="value">${nf.format(t.people)}</div>
      <div class="hint">comptes regroupés par personne</div></div>
    <div class="kpi"><div class="label">Titres différents</div><div class="value">${nf.format(t.titles)}</div>
      <div class="hint">films et séries</div></div>
    <div class="kpi"><div class="label">Répartition par serveur</div>
      <div class="share">${t.emby > 0 ? `<i class="seg-emby" style="flex:${t.emby}"></i>` : ""}${t.jellyfin > 0 ? `<i class="seg-jellyfin" style="flex:${t.jellyfin}"></i>` : ""}</div>
      <div class="share-legend">
        <span><i class="dot emby"></i>Emby ${pct(t.emby)} % · ${dur(t.emby)}</span>
        <span><i class="dot jellyfin"></i>Jellyfin ${pct(t.jellyfin)} % · ${dur(t.jellyfin)}</span></div>
      <div class="hint">${nf.format(t.emby_plays)} et ${nf.format(t.jellyfin_plays)} lectures</div></div>`;
}
function bucketLabel(bucket, unit) {
  if (unit === "month") {
    const [y, m] = bucket.split("-");
    return new Date(+y, +m - 1, 1).toLocaleDateString("fr-FR", { month: "short", year: "2-digit" });
  }
  return new Date(bucket + "T00:00:00").toLocaleDateString("fr-FR", { day: "numeric", month: "short" });
}
function renderTimeline(tl) {
  const keys = state.source === "all" ? ["emby", "jellyfin"] : [state.source];
  const datasets = keys.map((k) => ({
    label: LABEL[k], data: tl.points.map((p) => p[k] / 3600), backgroundColor: css(`--${k}`),
    borderRadius: 4, borderSkipped: "bottom", borderColor: css("--surface"), borderWidth: { top: 0, right: 1, bottom: 0, left: 1 },
    maxBarThickness: 28, stack: "s",
  }));
  const cfg = {
    type: "bar",
    data: { labels: tl.points.map((p) => bucketLabel(p.bucket, tl.unit)), datasets },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (c) => ` ${c.dataset.label} : ${dur(c.raw * 3600)}`,
            footer: (items) => items.length > 1 ? `Total : ${dur(items.reduce((a, i) => a + i.raw, 0) * 3600)}` : "",
          },
        },
      },
      scales: {
        x: { stacked: true, grid: { display: false }, border: { color: css("--grid") }, ticks: { color: css("--muted"), maxRotation: 0, autoSkipPadding: 12 } },
        y: { stacked: true, beginAtZero: true, grid: { color: css("--grid") }, border: { display: false },
             ticks: { color: css("--muted"), callback: (v) => `${nf.format(v)} h`, maxTicksLimit: 6 } },
      },
    },
  };
  if (chart) chart.destroy();
  chart = new Chart($("timeline"), cfg);
}
function renderHeatmap(grid) {
  const max = Math.max(...grid.flat(), 0);
  let html = "<span></span>" + Array.from({ length: 24 }, (_, h) => `<span class="hour">${h}</span>`).join("");
  grid.forEach((row, d) => {
    html += `<span class="day">${DAYS[d]}</span>`;
    row.forEach((v, h) => {
      const style = v > 0 ? ` style="background:color-mix(in oklab, var(--seq-hi) ${Math.round((v / max) * 100)}%, var(--seq-lo))"` : "";
      html += `<span class="cell"${style} title="${DAYS[d]} ${h} h : ${v > 0 ? dur(v) : "rien"}"></span>`;
    });
  });
  $("heatmap").innerHTML = html;
}
function renderRecent(rows) {
  $("recent").querySelector("tbody").innerHTML = rows.map((r) => `<tr>
    <td>${fmtDate(r.date_created)}</td>
    <td><i class="dot ${esc(r.server)}"></i>${esc(LABEL[r.server] || r.server)}</td>
    <td>${esc(r.person)}</td><td class="title">${esc(r.item_name)}</td>
    <td>${esc(r.device)} <span class="muted">· ${esc(r.client)}</span></td>
    <td>${esc(r.method)}</td><td class="num">${dur(r.duration)}</td></tr>`).join("")
    || '<tr><td colspan="7" class="none">Aucune lecture sur cette période.</td></tr>';
}
function renderPeopleFilter(names) {
  const sel = $("f-person");
  const options = ['<option value="">Tous</option>'].concat(names.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`));
  sel.innerHTML = options.join("");
  sel.value = names.some((n) => n.toLowerCase() === state.person.toLowerCase()) ? names.find((n) => n.toLowerCase() === state.person.toLowerCase()) : "";
  state.person = sel.value;
}
function render(d) {
  lastData = d;
  renderPeopleFilter(d.people_filter);
  renderKpis(d.totals);
  renderTimeline(d.timeline);
  barList("l-people", d.people);
  barList("l-movies", d.movies);
  barList("l-series", d.series);
  barList("l-clients", d.clients);
  barList("l-devices", d.devices);
  barList("l-methods", d.methods);
  barList("l-types", d.types.map((t) => ({ ...t, name: TYPES[t.name.toLowerCase()] || t.name })));
  renderHeatmap(d.heatmap);
  renderRecent(d.recent);
  $("updated").textContent = `Affichage du ${fmtDate(d.generated)}`;
}
function renderStatus(s) {
  $("servers").innerHTML = s.servers.map((x) => {
    const err = !!x.last_error;
    const text = err ? `erreur : ${x.last_error}` : (x.last_ok ? `synchro ${ago(x.last_ok)}` : "en attente de synchro");
    return `<span class="pill ${err ? "err" : ""}" title="${esc(x.url)}"><i class="dot ${x.key}"></i><b>${esc(x.label)}</b>
      <span class="state">${err ? "✕" : "✓"}</span> ${esc(text)} · ${nf.format(x.rows)} lectures</span>`;
  }).join("");
  const empty = $("empty");
  if (!s.servers.length) {
    empty.hidden = false;
    empty.innerHTML = "Aucun serveur configuré. Renseigne <code>EMBY_URL</code> / <code>EMBY_API_KEY</code> et/ou <code>JELLYFIN_URL</code> / <code>JELLYFIN_API_KEY</code> dans le fichier compose, puis redémarre le conteneur.";
  } else { empty.hidden = true; }
}

/* ---------- chargement ---------- */
async function load() {
  const q = new URLSearchParams({ days: state.days, source: state.source, person: state.person });
  const [d, s] = await Promise.all([api(`/api/dashboard?${q}`), api("/api/status")]);
  renderStatus(s);
  render(d);
}
function segmented(id, key, cast) {
  $(id).addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    $(id).querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
    state[key] = cast(b.dataset.v);
    load();
  });
}
segmented("f-days", "days", Number);
segmented("f-source", "source", String);
$("f-person").addEventListener("change", (e) => { state.person = e.target.value; load(); });

$("btn-sync").addEventListener("click", async (e) => {
  const b = e.currentTarget;
  b.disabled = true; b.textContent = "Synchronisation…";
  try { await api("/api/sync", { method: "POST" }); await load(); }
  catch (err) { alert(`Échec de la synchronisation : ${err.message}`); }
  finally { b.disabled = false; b.textContent = "Synchroniser"; }
});

$("btn-users").addEventListener("click", async () => {
  const users = await api("/api/users");
  $("users").querySelector("tbody").innerHTML = users.map((u) => `<tr>
    <td><i class="dot ${esc(u.server)}"></i>${esc(LABEL[u.server] || u.server)}</td>
    <td>${esc(u.name)}</td><td class="num">${nf.format(u.plays)}</td>
    <td><input type="text" value="${esc(u.alias)}" placeholder="${esc(u.name)}" data-server="${esc(u.server)}" data-id="${esc(u.user_id)}" aria-label="Nom commun de ${esc(u.name)}"></td></tr>`).join("")
    || '<tr><td colspan="4" class="none">Aucun compte récupéré pour le moment.</td></tr>';
  $("dlg-users").showModal();
});
$("users").addEventListener("change", async (e) => {
  const i = e.target;
  if (!i.matches("input")) return;
  await api("/api/users/alias", {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ server: i.dataset.server, user_id: i.dataset.id, alias: i.value }),
  });
});
$("dlg-users").addEventListener("close", load);

matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => lastData && renderTimeline(lastData.timeline));
load().catch((err) => { $("empty").hidden = false; $("empty").textContent = `Impossible de charger les données : ${err.message}`; });
setInterval(() => load().catch(() => {}), 5 * 60 * 1000);
