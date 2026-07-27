/* Decision console.

   Plain JavaScript, no framework and no build step. The page has three views
   and one selection; a framework would be more machinery than state.

   The histograms are drawn as SVG from bin counts the API has already
   computed, which keeps the payload small and means the container needs no
   network access to render a chart. */

const $ = (sel) => document.querySelector(sel);

const state = {
  overview: null,
  decisions: [],
  byId: new Map(),
  groups: [],
  signals: [],
  quality: null,
  selected: null,
  view: "decisions",
};

/* ---------- formatting ---------- */

const gbp = (v, dp = 0) =>
  v == null || !isFinite(v)
    ? "—"
    : "£" + Number(v).toLocaleString("en-GB", { minimumFractionDigits: dp, maximumFractionDigits: dp });

const gbpCompact = (v) => {
  if (v == null || !isFinite(v)) return "—";
  const n = Math.abs(v);
  if (n >= 1_000_000) return (v < 0 ? "-" : "") + "£" + (n / 1_000_000).toFixed(2) + "m";
  if (n >= 10_000) return (v < 0 ? "-" : "") + "£" + Math.round(n / 1000) + "k";
  return gbp(v);
};

const pct = (v, dp = 0) => (v == null || !isFinite(v) ? "—" : (v * 100).toFixed(dp) + "%");
const signedPct = (v) => (v == null ? "—" : (v > 0 ? "+" : "") + (v * 100).toFixed(0) + "%");
const num = (v) => (v == null ? "—" : Number(v).toLocaleString("en-GB"));

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );

const titleCase = (s) => s.charAt(0).toUpperCase() + s.slice(1);

/* ---------- boot ---------- */

async function boot() {
  try {
    const [overview, decisions, signals, quality] = await Promise.all([
      fetch("/api/overview").then((r) => r.json()),
      fetch("/api/decisions").then((r) => r.json()),
      fetch("/api/signals").then((r) => r.json()),
      fetch("/api/quality").then((r) => r.json()),
    ]);

    state.overview = overview;
    state.decisions = decisions.decisions;
    state.groups = decisions.groups;
    state.byId = new Map(state.decisions.map((d) => [d.id, d]));
    state.signals = signals;
    state.quality = quality;

    renderHeadline();
    renderQueue();
    renderSignals();
    renderQuality();
    applyHash();

    const boot = $("#boot");
    boot.classList.add("gone");
    // Taken out of the document rather than left at zero opacity. A faded
    // overlay is still a layer over the whole page, and anything that reads the
    // page during the fade sees it.
    setTimeout(() => boot.remove(), 350);
  } catch (err) {
    $("#boot").innerHTML =
      `<p class="error"><strong>The console failed to start.</strong><br>${esc(err)}</p>`;
  }
}

/* ---------- header ---------- */

function renderHeadline() {
  const o = state.overview;
  $("#window").textContent = `${o.window.start} to ${o.window.end} · ${num(o.orders)} orders`;

  const items = [
    ["Net revenue", gbpCompact(o.net_revenue), ""],
    ["Gross profit", gbpCompact(o.gross_profit), `<small>${pct(o.gross_margin)}</small>`],
    ["Signals", num(o.signals), ""],
    ["Decisions", num(o.decisions), ""],
    ["Engine acts on", gbp(o.authorised_autonomously_gbp), "", "act"],
    ["Awaiting approval", gbp(o.awaiting_approval_gbp), "", "approve"],
  ];

  $("#headline").innerHTML = items
    .map(
      ([k, v, extra, cls]) =>
        `<div><dt>${esc(k)}</dt><dd class="${cls || ""}">${esc(v)}${extra || ""}</dd></div>`
    )
    .join("");
}

/* ---------- queue ---------- */

function renderQueue() {
  $("#queue").innerHTML = state.groups
    .map((g) => {
      const cards = g.ids
        .map((id) => {
          const d = state.byId.get(id);
          const money =
            d.verdict === "auto_execute"
              ? `<span class="money">${gbp(d.authorised_now_gbp)} released</span>`
              : d.exposure_gbp
              ? `<span class="money">${gbp(d.exposure_gbp)}</span>`
              : `<span>no cash outcome</span>`;
          const flag = d.needs_scrutiny ? `<span class="flag">scrutiny</span>` : "";
          return `
            <button class="card" data-id="${esc(d.id)}" data-verdict="${esc(d.verdict)}">
              <span class="card-title">${esc(d.title)}</span>
              <span class="card-meta">${money}${flag}</span>
            </button>`;
        })
        .join("");

      return `
        <div class="group">
          <div class="group-head">
            <h3>${esc(g.label)}</h3><span class="count">${g.ids.length}</span>
          </div>
          <p class="group-blurb">${esc(g.blurb)}</p>
          ${cards}
        </div>`;
    })
    .join("");

  $("#queue")
    .querySelectorAll(".card")
    .forEach((el) => el.addEventListener("click", () => select(el.dataset.id)));
}

function select(id, pushHash = true) {
  state.selected = id;
  document
    .querySelectorAll(".card")
    .forEach((el) => el.classList.toggle("is-active", el.dataset.id === id));
  renderDetail(state.byId.get(id));
  $("#detail").scrollTop = 0;
  if (pushHash) {
    const next = `#decisions/${encodeURIComponent(id)}`;
    if (location.hash !== next) history.replaceState(null, "", next);
  }
}

/* ---------- detail ---------- */

function renderDetail(d) {
  if (!d) return;

  const chips = [
    `<span class="chip">${esc(d.action_type.replace(/_/g, " "))}</span>`,
    `<span class="chip${d.reversibility === "irreversible" ? " warn" : ""}">${esc(d.reversibility)}</span>`,
    d.review_after_days
      ? `<span class="chip">review in ${d.review_after_days}d</span>`
      : "",
  ].join("");

  $("#detail").innerHTML = `
    <div class="detail-head">
      <div class="row">
        <span class="badge" data-v="${esc(d.verdict)}">${esc(d.verdict_label)}</span>
        ${chips}
      </div>
      <h2>${esc(d.title)}</h2>
    </div>

    <div class="callout" data-v="${esc(d.verdict)}">${esc(d.headline)}</div>

    ${moneyRow(d)}

    ${
      d.action
        ? `<section class="block"><h3>What to do</h3><p class="lead">${esc(d.action)}</p></section>`
        : ""
    }

    ${
      d.rationale.length
        ? `<section class="block"><h3>Why</h3>${d.rationale
            .map((p) => `<p>${esc(p)}</p>`)
            .join("")}</section>`
        : ""
    }

    ${confidenceBlock(d)}
    ${simulationBlock(d)}
    ${checksBlock(d)}

    ${
      d.safeguard
        ? `<section class="block"><h3>${
            d.reversibility === "irreversible" ? "Before committing" : "How it gets unwound"
          }</h3><p>${esc(d.safeguard)}</p></section>`
        : ""
    }

    ${
      d.caveats.length
        ? `<section class="block"><h3>What would make this wrong</h3><ul class="plain">${d.caveats
            .map((c) => `<li>${esc(c)}</li>`)
            .join("")}</ul></section>`
        : ""
    }

    ${signalsBlock(d)}
  `;
}

function moneyRow(d) {
  const stats = [];

  if (d.exposure_gbp) {
    stats.push({
      k: "Committed if taken in full",
      v: gbp(d.exposure_gbp),
      n:
        d.reversibility === "irreversible"
          ? "Cash out. Cannot be recalled."
          : "Redirected, not spent. Recoverable by moving it back.",
    });
  }
  if (d.authorised_now_gbp != null) {
    stats.push({
      k: "Released now, unsupervised",
      v: gbp(d.authorised_now_gbp),
      n:
        d.authorised_now_gbp < d.exposure_gbp
          ? "First step. The rest needs this one's measured result."
          : "The whole action fits inside one step.",
      cls: "act",
    });
  }
  if (d.worst_case_cost_gbp != null && d.exposure_gbp) {
    stats.push({
      k: "At risk if the assumptions are wrong",
      v: gbp(d.worst_case_cost_gbp),
      n: `${pct(d.worst_case_cost_gbp / d.exposure_gbp)} of the amount committed, under the stress test.`,
      cls: d.needs_scrutiny ? "warn" : "",
    });
  }

  if (!stats.length) return "";
  return `<div class="money-row">${stats
    .map(
      (s) =>
        `<div class="stat ${s.cls || ""}"><div class="k">${esc(s.k)}</div>` +
        `<div class="v">${esc(s.v)}</div><div class="n">${esc(s.n)}</div></div>`
    )
    .join("")}</div>`;
}

function confidenceBlock(d) {
  const c = d.confidence;
  const bar = (label, value, cls = "") => `
    <div class="lbl">${esc(label)}</div>
    <div class="track ${cls}"><i style="width:${Math.max(0, Math.min(1, value)) * 100}%"></i></div>
    <div class="num">${value.toFixed(2)}</div>`;

  return `
    <section class="block">
      <h3>How sure the engine is</h3>
      <div class="conf-grid">
        ${bar("Strength", c.strength)}
        ${bar("Persistence", c.persistence)}
        ${bar("Corroboration", c.corroboration)}
        ${bar("Data quality", c.data_quality, "dq")}
        ${bar("Overall", c.overall, "total")}
      </div>
      <p class="conf-note">${esc(c.explanation)}</p>
      <p style="margin-top:12px;font-size:12.5px;color:var(--ink-3)">
        The first three are averaged; data quality multiplies that average rather than
        joining it. Averaging all four would let a strong signal built on broken data
        still score well, because three strong terms outvote one weak one &mdash; but all
        three are computed from the same suspect data, so they are one doubt counted
        three times, not independent evidence.
      </p>
    </section>`;
}

function checksBlock(d) {
  if (!d.checks.length) return "";
  const rows = d.checks
    .map((c) => {
      // A failed routing check is not a mark against the recommendation, and
      // colouring it like one would put "this is a purchase order" and "this
      // costs more than we tolerate" in the same red, which is the single most
      // useful distinction on the page.
      const [tag, label] = c.passed
        ? ["pass", "pass"]
        : c.routing
        ? ["note", "routes"]
        : c.blocking
        ? ["fail", "fail"]
        : ["note", "sizing"];
      return `
        <div class="check">
          <span class="tag ${tag}">${label}</span>
          <div><div class="name">${esc(titleCase(c.name))}</div>
          <div class="why">${esc(c.detail)}</div></div>
        </div>`;
    })
    .join("");
  return `<section class="block wide"><h3>What the gate checked</h3>${rows}</section>`;
}

function simulationBlock(d) {
  const s = d.simulation;
  if (!s) return "";
  if (s.not_simulated) {
    return `<section class="block"><h3>Simulated outcome</h3>
      <div class="notsim">${esc(s.not_simulated)}</div></section>`;
  }

  const dist = s.distribution;
  const rows = [
    ["Bad case (5th pct)", "p5"],
    ["Central (median)", "p50"],
    ["Good case (95th pct)", "p95"],
    ["Chance of a profit", "prob_positive"],
  ]
    .map(([label, key]) => {
      const fmt = (v) => (key === "prob_positive" ? pct(v) : gbp(v));
      return `<div>${esc(label)}</div>
        <div class="r nom">${fmt(dist.nominal[key])}</div>
        <div class="r str">${dist.stressed ? fmt(dist.stressed[key]) : "—"}</div>
        <div></div><div></div>`;
    })
    .join("");

  return `
    <section class="block wide">
      <h3>What happens if we do it</h3>
      <div class="chart-card">
        <div class="chart-legend">
          <span><i style="background:#6b7f95"></i>As modelled</span>
          <span><i style="background:var(--approve)"></i>With its assumptions set against it</span>
          <span style="color:var(--ink-3)">${esc(s.metric)} over ${s.horizon_days} days, ${num(
    s.n_draws
  )} draws</span>
        </div>
        ${histogram(dist)}
        <div class="dist-table">
          <div class="h"></div><div class="h r">As modelled</div><div class="h r">Stressed</div>
          <div></div><div></div>
          ${rows}
        </div>
        ${assumptions(s.assumptions)}
      </div>
    </section>`;
}

/* Two histograms on one shared axis, drawn as SVG.

   Shared axis on purpose: the comparison is about where the stressed
   distribution sits relative to the nominal one, and two independently scaled
   charts would show two similar humps and hide exactly that. */
function histogram(dist) {
  if (!dist) return "";

  const W = 760;
  const H = 200;
  const PAD_B = 26;
  const bins = dist.bins;
  const bw = W / bins;

  const all = dist.stressed
    ? dist.nominal.counts.concat(dist.stressed.counts)
    : dist.nominal.counts;
  const peak = Math.max(1, ...all);
  const plot = H - PAD_B;

  const bars = (counts, fill, opacity) =>
    counts
      .map((c, i) => {
        if (!c) return "";
        const h = (c / peak) * (plot - 6);
        return `<rect x="${(i * bw).toFixed(2)}" y="${(plot - h).toFixed(2)}" width="${(
          bw - 0.6
        ).toFixed(2)}" height="${h.toFixed(2)}" fill="${fill}" opacity="${opacity}" />`;
      })
      .join("");

  const zeroX = dist.zero_at * W;
  const showZero = dist.zero_at >= 0 && dist.zero_at <= 1;

  // One unit for the whole axis. Mixing "£7,703" and "£11k" across five ticks
  // makes the reader convert between scales to compare two labels on the same
  // line, which is work the chart should have done for them.
  const useThousands = Math.max(Math.abs(dist.lo), Math.abs(dist.hi)) >= 10_000;
  const tickLabel = (v) =>
    useThousands ? (v / 1000).toFixed(1).replace(/\.0$/, "") + "k" : Math.round(v).toLocaleString("en-GB");

  const ticks = [0, 0.25, 0.5, 0.75, 1]
    .map((f) => {
      const value = dist.lo + f * (dist.hi - dist.lo);
      const x = Math.min(W - 2, Math.max(2, f * W));
      const anchor = f === 0 ? "start" : f === 1 ? "end" : "middle";
      return `<text x="${x.toFixed(1)}" y="${H - 8}" text-anchor="${anchor}"
        fill="#6f7f90" font-size="11" font-family="ui-monospace, monospace">£${esc(
          tickLabel(value)
        )}</text>`;
    })
    .join("");

  const median = (d, colour) => {
    const f = (d.p50 - dist.lo) / (dist.hi - dist.lo);
    if (f < 0 || f > 1) return "";
    return `<line x1="${(f * W).toFixed(1)}" y1="4" x2="${(f * W).toFixed(1)}" y2="${plot}"
      stroke="${colour}" stroke-width="1.5" />`;
  };

  return `
    <svg class="hist" viewBox="0 0 ${W} ${H}" role="img"
         aria-label="Distribution of simulated outcomes, as modelled and under stress">
      <line x1="0" y1="${plot}" x2="${W}" y2="${plot}" stroke="#212c39" stroke-width="1" />
      ${bars(dist.nominal.counts, "#6b7f95", 0.85)}
      ${dist.stressed ? bars(dist.stressed.counts, "#d99a2b", 0.6) : ""}
      ${median(dist.nominal, "#c7d5e3")}
      ${dist.stressed ? median(dist.stressed, "#f0b64a") : ""}
      ${zeroMarker(zeroX, showZero, plot, W)}
      ${ticks}
    </svg>`;
}

/* The break-even line, with its label kept legible.

   The label sits directly over the densest part of the distribution whenever a
   recommendation is marginal, which is exactly when someone is reading it
   closely. It gets a backing panel, and it flips to the other side of the line
   when the line is close to the right edge. */
function zeroMarker(zeroX, show, plot, W) {
  if (!show) return "";
  const flip = zeroX > W * 0.8;
  const boxW = 66;
  const boxX = flip ? zeroX - boxW - 4 : zeroX + 4;
  return `
    <line x1="${zeroX.toFixed(1)}" y1="0" x2="${zeroX.toFixed(1)}" y2="${plot}"
      stroke="#f2564b" stroke-width="1.5" stroke-dasharray="4 3" />
    <rect x="${boxX.toFixed(1)}" y="1" width="${boxW}" height="16" rx="3"
      fill="#0a0e13" opacity="0.86" />
    <text x="${(boxX + boxW / 2).toFixed(1)}" y="12.5" text-anchor="middle" fill="#f2564b"
      font-size="10.5" font-family="ui-monospace, monospace">break even</text>`;
}

function assumptions(a) {
  const entries = Object.entries(a || {});
  if (!entries.length) return "";
  const rows = entries
    .map(([k, v]) => {
      const value = typeof v === "number" ? (Number.isInteger(v) ? v : v.toFixed(3)) : v;
      return `<dt>${esc(k.replace(/_/g, " "))}</dt><dd>${esc(value)}</dd>`;
    })
    .join("");
  return `<details class="assumptions">
    <summary>Everything this rests on (${entries.length})</summary>
    <dl class="kv">${rows}</dl></details>`;
}

function signalsBlock(d) {
  if (!d.signals.length) return "";
  const rows = d.signals
    .map(
      (s) => `
      <li><strong style="color:var(--ink)">${esc(s.entity)} · ${esc(s.metric)}</strong>
      <span class="cls" data-c="${esc(s.classification)}" style="margin-left:8px">${esc(
        s.classification.replace(/_/g, " ")
      )}</span>
      <span style="font-family:var(--mono);font-size:11.5px;color:var(--ink-3);margin-left:8px">
        ${esc(signedPct(s.magnitude))} · held ${s.persistence_days}d · from ${esc(s.detected_at)}
      </span>
      <br>${esc(s.summary)}</li>`
    )
    .join("");
  return `<section class="block wide"><h3>Evidence it rests on</h3>
    <ul class="plain">${rows}</ul></section>`;
}

/* ---------- signals view ---------- */

function renderSignals() {
  const head = `<div class="tbl-row head">
      <div>Signal</div><div>Classification</div><div>Metric</div>
      <div class="r">Change</div><div class="r">Held</div><div class="r">Data quality</div>
    </div>`;

  const rows = state.signals
    .map(
      (s, i) => `
      <div class="tbl-row body" data-i="${i}">
        <div class="name">${esc(s.entity)}</div>
        <div><span class="cls" data-c="${esc(s.classification)}">${esc(
        s.classification.replace(/_/g, " ")
      )}</span></div>
        <div class="mono" style="color:var(--ink-3)">${esc(s.metric)}</div>
        <div class="r mono ${s.magnitude >= 0 ? "up" : "down"}">${esc(signedPct(s.magnitude))}</div>
        <div class="r mono">${s.persistence_days}d</div>
        <div class="r mono" style="color:${
          s.data_quality < 0.5 ? "var(--fail)" : "var(--ink-3)"
        }">${s.data_quality.toFixed(2)}</div>
      </div>
      <div class="tbl-detail" data-d="${i}" hidden>
        <div style="font-size:11px;color:var(--ink-3);margin-bottom:6px">
          ${esc(s.classification_blurb)} · detected ${esc(s.detected_at)} · ${esc(s.id)}
        </div>
        ${esc(s.summary)}
      </div>`
    )
    .join("");

  $("#signals").innerHTML = `<div class="tbl">${head}${rows}</div>`;

  $("#signals")
    .querySelectorAll(".tbl-row.body")
    .forEach((row) =>
      row.addEventListener("click", () => {
        const panel = $(`#signals .tbl-detail[data-d="${row.dataset.i}"]`);
        panel.hidden = !panel.hidden;
      })
    );
}

/* ---------- quality view ---------- */

function renderQuality() {
  const q = state.quality;

  const scores = q.scores
    .map(
      (s) => `
      <div class="fam">${esc(s.family)}</div>
      <div class="track ${s.score < 0.5 ? "dq" : ""}"><i style="width:${s.score * 100}%;${
        s.score === 0 ? "background:var(--fail)" : ""
      }"></i></div>
      <div class="num ${s.score === 0 ? "zero" : ""}">${s.score.toFixed(2)}</div>`
    )
    .join("");

  const row = (c) => `
    <div class="chk-row">
      <span class="dot ${c.passed ? "ok" : "no"}"></span>
      <div class="nm">${esc(c.name)}</div>
      <div class="ds">${esc(c.description)}</div>
      <div class="mt">${esc(c.category)}<br>${esc(c.severity)}</div>
    </div>`;

  const failed = q.checks.filter((c) => !c.passed);
  const passed = q.checks.filter((c) => c.passed);
  const o = state.overview;

  $("#quality").innerHTML = `
    <div class="score-grid">${scores}</div>

    <div class="q-sect">
      <h3>Known issues — ${failed.length} of ${o.checks_total} checks, all documented</h3>
      <div class="tbl">${failed.map(row).join("")}</div>
    </div>

    <div class="q-sect">
      <h3>Passing — ${passed.length} checks</h3>
      <div class="tbl">${passed.map(row).join("")}</div>
    </div>`;
}

/* ---------- routing ---------- */

/* The URL carries the view and the selected decision, so a specific decision
   can be linked to rather than described as "the third one down". */

function showView(view) {
  state.view = view;
  document
    .querySelectorAll(".tab")
    .forEach((t) => t.classList.toggle("is-active", t.dataset.view === view));
  document
    .querySelectorAll(".view")
    .forEach((v) => v.classList.toggle("is-active", v.id === `view-${view}`));
}

/* Recommendation ids are not URL-safe. "restock_cbd_oil_20%" is a real one, and
   a bare "%" in a fragment makes decodeURIComponent throw rather than return
   something imperfect, so an un-encoded id would take the whole page down on
   the next reload. Ids are encoded on the way out and decoded defensively on
   the way in, because a hash can also be typed by hand. */

const decodeSegment = (s) => {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
};

function applyHash() {
  const [view, ...rest] = location.hash.replace(/^#/, "").split("/");
  const id = decodeSegment(rest.join("/"));
  showView(["decisions", "signals", "quality"].includes(view) ? view : "decisions");
  const target = (state.byId.has(id) && id) || state.groups[0]?.ids[0];
  if (target) select(target, false);
}

document.querySelectorAll(".tab").forEach((tab) =>
  tab.addEventListener("click", () => {
    showView(tab.dataset.view);
    location.hash =
      tab.dataset.view === "decisions" && state.selected
        ? `decisions/${encodeURIComponent(state.selected)}`
        : tab.dataset.view;
  })
);

window.addEventListener("hashchange", applyHash);

/* Arrow keys move through the queue, which is the whole point of a queue. */
document.addEventListener("keydown", (e) => {
  if (state.view !== "decisions" || !state.selected) return;
  if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
  if (/^(INPUT|TEXTAREA|SUMMARY)$/.test(document.activeElement?.tagName || "")) return;

  const order = state.groups.flatMap((g) => g.ids);
  const at = order.indexOf(state.selected);
  const next = order[at + (e.key === "ArrowDown" ? 1 : -1)];
  if (next) {
    e.preventDefault();
    select(next);
    document.querySelector(`.card[data-id="${CSS.escape(next)}"]`)?.scrollIntoView({
      block: "nearest",
    });
  }
});

boot();
