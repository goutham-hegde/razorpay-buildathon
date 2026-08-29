/* reclaim console.
 *
 * The Live view replays a run that already happened, at an accelerated clock. Every line
 * it renders is a row that exists in the ledger - nothing here is invented for the screen,
 * and the same rows produce the numbers on the Results view.
 */

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const state = {
  batch: null,
  run: null,
  events: [],
  cursor: 0,
  playing: false,
  timer: null,
  caseId: null,
  totals: { worked: new Set(), charges: 0, contacts: 0, recovered: 0, double: 0, rows: 0 },
};

/* Deep links. Every shot in `docs/recording-runsheet.md` is a URL rather than a sequence of
   clicks, because a fumbled click is a retaken take - and "find case_B00106 in a 600-case
   stream, on camera" is exactly the shot that gets fumbled. Recognised query parameters:

     ?batch=B&run=agent&case=case_B00106&view=live

   `run` accepts either an arm name or a full run id. The URL is kept in sync as you click,
   so a shot you find by hand is a link you can paste into the runsheet afterwards. */
const link = new URLSearchParams(location.search);
const wanted = {
  batch: link.get("batch"),
  arm: link.get("run") || link.get("arm"),
  case: link.get("case"),
  view: link.get("view"),
  zoom: link.get("zoom"),
};

/* Display size. `present` scales the root font size, and because every length in style.css
   is in `rem`, that scales the whole interface rather than just the words - which is the
   difference between "bigger text in the same small boxes" and something legible in a
   screen recording. Sticky, because reaching for it again after a reload mid-take is
   exactly the fumble the deep links exist to prevent. */
function setZoom(mode, persist = true) {
  const m = mode === "present" ? "present" : "normal";
  document.documentElement.dataset.zoom = m;
  $$(".zoom button").forEach((b) => b.classList.toggle("is-active", b.dataset.zoom === m));
  if (persist) { try { localStorage.setItem("reclaim.zoom", m); } catch (e) { /* private mode */ } }
  syncLink();
}

function storedZoom() {
  try { return localStorage.getItem("reclaim.zoom"); } catch (e) { return null; }
}

/* ---------------- helpers ---------------- */

const rupees = (paise, opts = {}) => {
  if (paise === null || paise === undefined) return "--";
  const v = paise / 100;
  const sign = opts.signed && v > 0 ? "+" : "";
  return sign + "₹" + v.toLocaleString("en-IN", { maximumFractionDigits: 0 });
};

const pct = (x) => (x * 100).toFixed(1) + "%";
const clock = (iso) => (iso || "").slice(11, 16);
const day = (iso) => (iso || "").slice(5, 10);

function signClass(v) {
  return v > 0 ? "pos" : v < 0 ? "neg" : "nil";
}

async function api(path) {
  const res = await fetch(path);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* keep statusText */ }
    throw new Error(detail);
  }
  return res.json();
}

function toast(msg) {
  const el = $("#toast");
  el.innerHTML = msg;
  el.hidden = false;
}
function clearToast() { $("#toast").hidden = true; }

/* ---------------- boot ---------------- */

function showView(name) {
  $$(".tab").forEach((t) => t.classList.toggle("is-active", t.dataset.view === name));
  $$(".view").forEach((v) => v.classList.toggle("is-active", v.id === "view-" + name));
  syncLink();
}

function currentView() {
  const t = $(".tab.is-active");
  return t ? t.dataset.view : "live";
}

/* Rewrite the address bar without navigating, so the current shot is always copy-pasteable.
   `replaceState` rather than `pushState` on purpose: this is a bookmark, not history, and
   filling the back button with every case you clicked would make the console annoying to
   use during a demo. */
function syncLink() {
  if (!state.batch) return;
  const q = new URLSearchParams({ batch: state.batch });
  if (state.run) q.set("run", state.run.replace(/^[AB]-/, ""));
  if (state.caseId) q.set("case", state.caseId);
  const view = currentView();
  if (view !== "live") q.set("view", view);
  const zoom = document.documentElement.dataset.zoom;
  if (zoom === "present") q.set("zoom", zoom);
  history.replaceState(null, "", location.pathname + "?" + q.toString());
}

async function boot() {
  let data;
  try {
    data = await api("/api/batches");
  } catch (e) {
    return toast("Could not reach the API: " + e.message);
  }
  if (!data.batches.length) {
    return toast("No batches on disk. Run <code>python -m reclaim.synth.generator --batch B</code>");
  }

  const sel = $("#batch");
  sel.innerHTML = data.batches
    .map((b) => `<option value="${b.name}">${b.name} — ${b.cases} cases, ${b.role}</option>`)
    .join("");

  const asked = wanted.batch && data.batches.find((b) => b.name === wanted.batch.toUpperCase());
  const withLedger = asked || data.batches.find((b) => b.has_ledger) || data.batches[0];
  sel.value = withLedger.name;
  state.batch = withLedger.name;

  sel.addEventListener("change", () => { state.batch = sel.value; state.caseId = null; loadBatch(); });
  $("#run").addEventListener("change", () => {
    state.run = $("#run").value;
    state.caseId = null;
    syncLink();
    loadRun();
  });

  $$(".tab").forEach((t) => t.addEventListener("click", () => showView(t.dataset.view)));
  $$(".zoom button").forEach((b) => b.addEventListener("click", () => setZoom(b.dataset.zoom)));
  setZoom(wanted.zoom || storedZoom() || "normal", !wanted.zoom);

  $("#play").addEventListener("click", togglePlay);
  $("#restart").addEventListener("click", restart);

  await loadBatch();
}

async function loadBatch() {
  clearToast();
  let results;
  try {
    results = await api(`/api/results?batch=${state.batch}`);
  } catch (e) {
    renderPending();
    return toast(
      `No ledger for batch ${state.batch}. Run ` +
      `<code>python -m reclaim.eval.replay --batch ${state.batch} --arms all</code>`
    );
  }

  // Order the picker by capability, and default to the most capable arm built so far.
  // Landing on `control` would open the console on an arm that by definition does
  // nothing, which reads as a broken page rather than as the point it is making.
  const rank = { agent: 0, rules: 1, naive: 2, control: 3 };
  const arms = [...results.arms].sort((a, b) => (rank[a.arm] ?? 9) - (rank[b.arm] ?? 9));

  const sel = $("#run");
  sel.innerHTML = arms
    .map((a) => `<option value="${a.run_id}">${a.arm}</option>`)
    .join("");
  const askedArm =
    wanted.arm && arms.find((a) => a.arm === wanted.arm || a.run_id === wanted.arm);
  if (askedArm) sel.value = askedArm.run_id;
  state.run = sel.value;

  renderResults(results);
  api(`/api/invariants?batch=${state.batch}`).then(renderInvariants).catch(() => {});
  api(`/api/detection?batch=${state.batch}`).then(renderDetection).catch(() => {});
  await loadRun();

  // Consumed once. A deep link sets the opening shot; it must not keep dragging the console
  // back to that case every time the operator changes arm mid-demo.
  if (wanted.view) showView(wanted.view);
  if (wanted.case) await showCase(wanted.case);
  wanted.case = wanted.view = wanted.arm = wanted.batch = null;
  syncLink();
}

async function loadRun() {
  stop();
  const data = await api(`/api/timeline?batch=${state.batch}&run=${state.run}&limit=3000`);
  state.events = data.events;
  state.totals.rows = data.ledger_rows;
  $("#stream-hint").textContent = `replaying ${data.run_id}`;
  restart();
}

/* ---------------- live replay ---------------- */

function restart() {
  stop();
  state.cursor = 0;
  state.caseId = null;
  syncLink();
  state.totals = { worked: new Set(), charges: 0, contacts: 0, recovered: 0, double: 0, rows: state.totals.rows };
  $("#stream").innerHTML = "";
  $("#detail").innerHTML = `<p class="empty">Press <b>Play</b>, or pick a case from the stream.</p>`;
  paint();

  if (!state.events.length) {
    $("#stream").innerHTML =
      `<li class="idle"><p><b>No actions recorded.</b> This arm did nothing at all — which ` +
      `for the control arm is the entire point, and is what every other arm's recovery ` +
      `number is measured against. Switch arms to watch one work.</p></li>`;
  } else {
    $("#stream").innerHTML =
      `<li class="idle"><p>Press <b>Play</b> to replay all ` +
      `${state.events.length.toLocaleString()} recorded decisions in order, or open a case ` +
      `directly from a link. Nothing here is generated live — it is read back off the ` +
      `append-only ledger.</p></li>`;
  }
}

function togglePlay() {
  state.playing ? stop() : play();
}

function play() {
  if (state.cursor >= state.events.length) restart();
  state.playing = true;
  $("#play").textContent = "Pause";
  tick();
}

function stop() {
  state.playing = false;
  clearTimeout(state.timer);
  $("#play").textContent = "Play";
}

function tick() {
  if (!state.playing) return;
  if (state.cursor >= state.events.length) {
    stop();
    $("#play").textContent = "Play";
    return;
  }
  step();
  const speed = Number($("#speed").value);
  state.timer = setTimeout(tick, Math.max(8, 900 / speed));
}

function step() {
  const ev = state.events[state.cursor++];
  const li = renderEvent(ev);
  const stream = $("#stream");
  const idle = stream.querySelector(".idle");
  if (idle) idle.remove();
  stream.prepend(li);
  while (stream.children.length > 120) stream.lastElementChild.remove();
  // Newest-first feeds must stay pinned to the top. Prepending into a container that has
  // been scrolled (which clicking an entry does) keeps scrollTop where it was, so the
  // viewport drifts past the tail and the panel goes blank while events keep arriving.
  stream.scrollTop = 0;

  state.totals.worked.add(ev.case_id);
  if (ev.kind === "charge") {
    state.totals.charges++;
    if (ev.outcome === "captured" && !ev.double_charge) state.totals.recovered++;
    if (ev.double_charge) state.totals.double++;
  }
  if (ev.kind === "contact") state.totals.contacts++;
  paint();
}

function paint() {
  $("#s-worked").textContent = state.totals.worked.size.toLocaleString();
  $("#s-charges").textContent = state.totals.charges.toLocaleString();
  $("#s-contacts").textContent = state.totals.contacts.toLocaleString();
  $("#s-recovered").textContent = state.totals.recovered.toLocaleString();
  $("#s-double").textContent = state.totals.double.toLocaleString();
  $("#s-rows").textContent = state.totals.rows.toLocaleString();
  $("#stream-progress").textContent = `${state.cursor} / ${state.events.length} events`;
}

function renderEvent(ev) {
  const li = document.createElement("li");
  let kind = "k-" + ev.kind;
  let head = "";
  let why = "";

  if (ev.kind === "charge") {
    if (ev.double_charge) {
      kind = "k-double";
      head = `DOUBLE CHARGE · ${ev.case_id}`;
      why = "the charge succeeded on a payment that had already been debited — this is a liability, not revenue";
    } else if (ev.outcome === "captured") {
      kind = "k-capture";
      head = `captured · ${ev.case_id}`;
      why = `attempt ${ev.attempt_no} on ${ev.rail}`;
    } else if (ev.outcome === "unresolved") {
      kind = "k-double";
      head = `unresolved · ${ev.case_id}`;
      why = "claimed but never settled — the outcome is unknown";
    } else {
      kind = "k-fail";
      head = `declined · ${ev.case_id}`;
      why = `attempt ${ev.attempt_no} on ${ev.rail}`;
    }
  } else if (ev.kind === "contact") {
    head = `${ev.channel} · ${ev.case_id}`;
    why = ev.engaged ? "customer engaged" : "no response";
  } else {
    kind = "k-retry";
    head = `${ev.action} · ${ev.case_id}`;
    why = ev.reason || "";
    if (ev.diagnosis) {
      why = `diagnosed ${ev.diagnosis}` +
        (ev.confidence != null ? ` (${ev.confidence.toFixed(2)})` : "") + " — " + why;
    }
  }

  li.className = "ev " + kind;
  li.innerHTML =
    `<time>${day(ev.at)} ${clock(ev.at)}</time>` +
    `<span class="dot"></span>` +
    `<div class="body"><div class="head">${escapeHtml(head)}</div>` +
    `<div class="why">${escapeHtml(why)}</div></div>` +
    `<span class="amt">${ev.amount_paise != null ? rupees(ev.amount_paise) : ""}</span>`;

  li.addEventListener("click", () => {
    // Pause on inspect. Reading a case while the feed keeps moving underneath it is
    // exactly the frustration this panel exists to remove.
    stop();
    $$(".ev").forEach((e) => e.classList.remove("is-sel"));
    li.classList.add("is-sel");
    showCase(ev.case_id);
  });
  return li;
}

/* ---------------- case detail ---------------- */

async function showCase(caseId) {
  state.caseId = caseId;
  syncLink();
  const el = $("#detail");
  el.innerHTML = `<p class="empty">loading ${caseId}…</p>`;
  let d;
  try {
    d = await api(`/api/case/${caseId}?batch=${state.batch}&run=${state.run}`);
  } catch (e) {
    el.innerHTML = `<p class="empty">${escapeHtml(e.message)}</p>`;
    return;
  }

  const c = d.case;
  const err = d.observed_error;
  const det = d.detection;

  el.innerHTML = `
    <h3>${c.id}</h3>
    <p class="sub">${c.rail} · ${c.issuer} · ${c.psp} · ${c.kind}</p>

    <dl class="kv">
      <dt>at risk</dt><dd>${rupees(c.amount_paise)}</dd>
      <dt>customer</dt><dd>${c.customer_id}</dd>
      <dt>opened</dt><dd>${c.opened_at.replace("T", " ").slice(0, 16)}</dd>
      ${c.mandate_id ? `<dt>mandate</dt><dd>${c.mandate_id}</dd>` : ""}
      <dt>channels</dt><dd>${d.customer ? d.customer.channels.join(", ") : "--"}</dd>
    </dl>

    ${err ? `
    <div class="block">
      <h4>What the agent was given</h4>
      <div class="quote">
        “${escapeHtml(err.description)}”
        <div class="meta">
          ${err.code} · ${err.source} · ${err.step} · ${err.reason}<br>
          bank reference: ${err.bank_reference || "— none returned"}
        </div>
      </div>
    </div>` : ""}

    <div class="block">
      <h4>Detection</h4>
      <div>
        <span class="tag ${det.disposition === "eligible" ? "good" : "warn"}">${det.disposition}</span>
        ${det.flags.map((f) => `<span class="tag">${escapeHtml(f)}</span>`).join(" ")}
      </div>
      <div class="why" style="margin-top:0.5rem;color:var(--ink-dim)">
        ${escapeHtml(det.reason)}
      </div>
    </div>

    <div class="block">
      <h4>Audit trail — ${d.trail.length} entries</h4>
      <ol class="trail">
        ${d.trail.map(trailRow).join("")}
      </ol>
    </div>
  `;
}

function trailRow(r) {
  let what = "";
  let why = "";
  if (r.kind === "decision") {
    what = r.action;
    why = r.reason || "";
  } else if (r.kind === "charge") {
    what = r.double_charge ? "DOUBLE CHARGE" : (r.outcome || "unresolved");
    why = `attempt ${r.attempt_no} · ${r.rail} · ${r.psp} · ${rupees(r.amount_paise)}` +
          (r.note ? ` — ${r.note}` : "");
  } else if (r.kind === "contact") {
    what = r.channel;
    why = `${r.template} · ${r.engaged ? "engaged" : "ignored"}`;
  } else if (r.kind === "closed") {
    what = "closed";
    why = `${r.status}` +
      (r.recovered_paise ? ` · ${rupees(r.recovered_paise)} via ${r.recovered_by}` : "");
  }
  // Two rows in this list are the ones a reader is meant to stop on: a duplicate debit, and
  // the policy refusing to cause one. Both get a class rather than an inline colour.
  const cls = [
    r.double_charge ? "is-double" : "",
    r.kind === "decision" && (r.action === "hold" || r.action === "escalate") ? "is-hold" : "",
  ].filter(Boolean).join(" ");
  return `<li class="${cls}"><time>${(r.at || "").replace("T", " ").slice(0, 16)}</time>` +
         `<div><div class="what">${escapeHtml(what)}</div>` +
         `<div class="why">${escapeHtml(why)}</div></div></li>`;
}

/* ---------------- results ---------------- */

/* What each arm actually is. The table used to print the word `agent` and leave the reader
   to work out that it is the same engine as `rules` with a different diagnoser - which is
   the single most important fact on the page. */
const ARM_BLURB = {
  control: "no intervention at all",
  naive:   "retry immediately, 3x, fixed interval",
  rules:   "policy engine, keyword diagnosis, no model",
  agent:   "policy engine, model diagnosis",
};

const ARM_ORDER = { control: 0, naive: 1, rules: 2, agent: 3 };

/* A two-sided bar, zero in the middle, on a SQUARE-ROOT scale - and the column caption says
   so, because an undisclosed non-linear axis is a lie told with a picture.
 
   Linear was tried first and it does not work here. Naive's net lift is around -56 lakh and
   the two arms the reader is actually comparing are +3.3 and +5.7 lakh, so on a linear axis
   naive fills the row and the comparison that matters renders as two identical specks. The
   numbers next to the bar are exact and unscaled; the bar's job is only to make the sign and
   the order of magnitude legible from across a room. */
function liftBar(paise, scale) {
  if (!scale) return "";
  const frac = Math.min(Math.sqrt(Math.abs(paise)) / Math.sqrt(scale), 1);
  const w = paise === 0 ? 0 : Math.max(frac * 50, 1.5);
  const side = paise >= 0 ? `left:50%;width:${w}%` : `right:50%;width:${w}%`;
  return `<div class="track"><span class="zero" style="left:50%"></span>` +
         `<span class="fill ${paise >= 0 ? "pos" : "neg"}" style="${side}"></span></div>`;
}

function renderResults(data) {
  $("#results-hint").textContent =
    `batch ${data.batch} · ${data.cases} cases · ${rupees(data.at_risk_paise)} at risk`;

  const arms = [...data.arms].sort(
    (a, b) => (ARM_ORDER[a.arm] ?? 9) - (ARM_ORDER[b.arm] ?? 9)
  );
  const by = Object.fromEntries(arms.map((a) => [a.arm, a]));
  const control = by.control;
  const hero = by.agent || by.rules;
  const scale = Math.max(...arms.map((a) => Math.abs(a.lift_net_paise)), 1);

  renderVerdict(data, by, hero, control);

  $("#arms tbody").innerHTML = arms.map((a) => {
    const isHero = hero && a.arm === hero.arm;
    const cls = isHero ? "is-hero" : (a.arm === "control" || a.arm === "naive" ? "is-baseline" : "");
    const lift = a.arm === "control"
      ? `<span class="nil">— it is the baseline</span>`
      : `<div class="liftbar">${liftBar(a.lift_net_paise, scale)}` +
        `<span class="val ${signClass(a.lift_net_paise)}">${rupees(a.lift_net_paise, { signed: true })}</span></div>`;
    return `
      <tr class="${cls}">
        <td class="arm-name"><b>${a.arm}</b><span>${ARM_BLURB[a.arm] || ""}</span></td>
        <td class="n ${isHero ? "big" : ""}">${pct(a.recovery_rate)}</td>
        <td>${lift}</td>
        <td class="n ${a.mandates_halted ? "neg" : "nil"}">${
          a.mandates_halted ? `${a.mandates_halted} <span class="hint">(${pct(a.mandate_halt_rate)})</span>` : "0"
        }</td>
        <td class="n">${a.double_charges ? `<span class="tag bad">${a.double_charges}</span>` : `<span class="nil">0</span>`}</td>
      </tr>`;
  }).join("");

  $("#money tbody").innerHTML = arms.map((a) => `
      <tr class="${hero && a.arm === hero.arm ? "is-hero" : ""}">
        <td class="arm-name"><b>${a.arm}</b></td>
        <td class="n">${a.recovered}</td>
        <td class="n"><span class="tag organic">${a.recovered_organic}</span></td>
        <td class="n">${rupees(a.gross_paise)}</td>
        <td class="n">${rupees(a.cost_paise)}</td>
        <td class="n ${a.residual_loss_paise ? "neg" : "nil"}">${rupees(a.residual_loss_paise)}</td>
        <td class="n ${signClass(a.net_paise)}">${rupees(a.net_paise)}</td>
      </tr>`).join("");

  for (const arm of data.pending_arms || []) {
    $("#arms tbody").insertAdjacentHTML(
      "beforeend",
      `<tr class="pending"><td>${escapeHtml(arm)}</td><td colspan="4">not built yet</td></tr>`
    );
  }
}

/* One sentence and four numbers, above the table.

   The tables are correct and they are not a first impression. A viewer given eleven columns
   spends the first ten seconds working out what they are looking at, and in a five-minute
   video those are the ten seconds that decide whether the rest lands. */
function renderVerdict(data, by, hero, control) {
  if (!hero || !control) {
    $("#verdict-line").textContent = "Run the control arm to put these numbers in context.";
    $("#headline").innerHTML = "";
    return;
  }

  const naive = by.naive;
  const lift = hero.lift_net_paise;
  $("#verdict-line").innerHTML =
    `Over <b>${data.cases} failed payments and mandates</b> in batch ${data.batch}, the ` +
    `<b>${hero.arm}</b> arm recovered <b class="num">${pct(hero.recovery_rate)}</b> of them. ` +
    `Doing nothing at all recovers <b class="num">${pct(control.recovery_rate)}</b>, so what it ` +
    `is actually worth is the difference: <b class="num ${signClass(lift)}">` +
    `${rupees(lift, { signed: true })}</b> net, after retry fees, messages, incentives and ` +
    `any money it had to give back.`;

  const cards = [
    {
      cls: lift >= 0 ? "good" : "bad",
      value: rupees(lift, { signed: true }),
      label: "net lift over doing nothing",
      sub: `control recovers ${rupees(control.gross_paise)} unaided; that is subtracted, not counted`,
    },
    {
      cls: "flat",
      value: pct(hero.recovery_rate),
      label: "recovery rate",
      sub: `${hero.recovered} of ${data.cases} cases · ${hero.recovered_organic} would have come back anyway`,
    },
    {
      cls: hero.mandates_halted ? "bad" : "good",
      value: String(hero.mandates_halted),
      label: "mandates halted",
      sub: naive
        ? `over-retrying destroys subscriptions — naive halted ${naive.mandates_halted}`
        : "subscriptions destroyed by over-retrying",
    },
    {
      cls: hero.double_charges ? "bad" : "good",
      value: String(hero.double_charges),
      label: "double charges",
      sub: naive
        ? `retrying a possible debit takes the money twice — naive did it ${naive.double_charges} times`
        : "money taken twice from a customer",
    },
  ];

  $("#headline").innerHTML = cards.map((c) => `
    <div class="hstat ${c.cls}">
      <b>${c.value}</b>
      <span class="lbl">${c.label}</span>
      <span class="sub">${c.sub}</span>
    </div>`).join("");
}

function renderPending() {
  $("#arms tbody").innerHTML =
    `<tr class="pending"><td colspan="5">no runs recorded for this batch</td></tr>`;
  $("#money tbody").innerHTML =
    `<tr class="pending"><td colspan="7">no runs recorded for this batch</td></tr>`;
  $("#verdict-line").textContent =
    "No ledger for this batch yet. Run the replay command below and reload.";
  $("#headline").innerHTML = "";
}

/* Six chips per arm rather than six rows.

   The list version was correct and read as boilerplate - six lines of small grey text the
   eye slides off. These are the falsifiable claims the whole project rests on, and R1 broke
   on the held-out batch once, so a failed one has to be the loudest thing on the panel
   rather than a tag at the end of a row.

   The marks are inline SVG, not `✓` and `✕`. Those code points are absent from most
   monospace faces, so the browser falls back to the emoji font and renders them as blue
   boxed glyphs that read as UI chrome rather than as a pass or a fail. A drawn mark cannot
   be substituted by a font stack. */
const MARK_OK = `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3.2 3.4L13 4.8"/></svg>`;
const MARK_NO = `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8"/></svg>`;

function renderInvariants(data) {
  $("#invariants").innerHTML = data.reports.map((r) => {
    const ok = r.held === r.total;
    // Violations render full width under the grid rather than inside their chip. Inside, a
    // twenty-six-case list stretches its grid row and drags five passing chips out of shape,
    // which makes the panel look broken in exactly the place it is working correctly.
    const broken = r.results.filter((g) => !g.held);
    return `
    <div class="inv-run">
      <header>
        <h3>${escapeHtml(r.arm)}</h3>
        <span class="inv-score ${ok ? "ok" : "fail"}">${r.held}/${r.total} held</span>
        ${r.must_hold
          ? `<span class="tag good">asserted</span>`
          : `<span class="tag">baseline — measured, not asserted</span>`}
      </header>
      <div class="inv-grid">
        ${r.results.map((g) => `
          <div class="inv-chip ${g.held ? "" : "broken"}">
            <span class="mark">${g.held ? MARK_OK : MARK_NO}</span>
            <div>
              <span class="id">${g.id}</span>
              <div class="title">${escapeHtml(g.title)}
                <span class="note">${escapeHtml(g.note || "")}</span>
              </div>
            </div>
          </div>`).join("")}
      </div>
      ${broken.map((g) => `
        <div class="inv-violations">
          <b>${g.id} violated — ${g.violation_count} ${g.violation_count === 1 ? "case" : "cases"}</b>
          ${g.violations.slice(0, 4).map((v) => escapeHtml(v.subject + ": " + v.detail)).join("<br>")}
          ${g.violation_count > 4 ? `<br>… ${g.violation_count - 4} more` : ""}
        </div>`).join("")}
    </div>`;
  }).join("");
}

function renderDetection(data) {
  const entries = Object.entries(data.by_disposition).sort((a, b) => b[1] - a[1]);
  const max = Math.max(...entries.map((e) => e[1]), 1);
  $("#detection").innerHTML = entries.map(([k, v]) => `
    <div class="det-row">
      <span class="lbl">${escapeHtml(k)}</span>
      <div class="bar-track"><div class="bar-fill" style="width:${(v / max) * 100}%"></div></div>
      <span class="val">${v}</span>
    </div>`).join("");
}

/* ---------------- ---------------- */

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

boot();
