/* reclaim console.

   The page is a statement of account, and this file's job is to fill it in from the ledger.
   Nothing here computes a figure: every number comes from `/api/results`, which reads the
   append-only ledger through `eval.metrics`. If a number on screen is wrong, it is wrong in
   the ledger, which is the property the whole project is built on.

   Deep links. Every shot in `docs/recording-runsheet.md` is a URL rather than a sequence of
   clicks, because a fumbled click is a retaken take:

     ?batch=B&run=agent&case=case_B00106&view=book&zoom=present

   `run` takes an arm name or a full run id. `view` takes the current names (statement, book,
   assurance) and the old ones (results, live) so links written before the redesign still
   land. The address bar is kept in sync as you click, so a shot found by hand is a link. */

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const state = {
  batch: null,
  run: null,
  caseId: null,
  events: [],
  cursor: 0,
  playing: false,
  timer: null,
  totals: { worked: new Set(), charges: 0, contacts: 0, recovered: 0, double: 0, rows: 0 },
};

const VIEWS = ["statement", "book", "assurance"];
const VIEW_ALIAS = { results: "statement", live: "book", invariants: "assurance" };

const link = new URLSearchParams(location.search);
const wanted = {
  batch: link.get("batch"),
  arm:   link.get("run") || link.get("arm"),
  case:  link.get("case"),
  view:  link.get("view"),
  zoom:  link.get("zoom"),
};

/* ---------------- formatting ----------------
   Indian grouping throughout - these are rupees, and 5,74,947 is how a rupee figure is
   written. A loss is shown in parentheses rather than with a minus sign, which is the
   convention on every statement of account and is far harder to misread at a glance than a
   hyphen that can be mistaken for a dash or lost at the start of a column. */

const inr = (n) => Math.round(n).toLocaleString("en-IN");

/** Bare figure for a statement column: 10,06,151 or (10,547). */
function fig(paise, opts = {}) {
  if (paise === null || paise === undefined) return "—";
  const v = paise / 100;
  if (v === 0 && opts.dashZero !== false) return "—";
  return v < 0 ? `(${inr(-v)})` : inr(v);
}

/** Figure with the currency mark, for prose and for anywhere outside a rupee column. */
function rs(paise) {
  if (paise === null || paise === undefined) return "—";
  const v = paise / 100;
  return v < 0 ? `(₹${inr(-v)})` : `₹${inr(v)}`;
}

const pct = (x) => (x * 100).toFixed(1) + "%";
const clock = (iso) => (iso || "").slice(11, 16);
const day = (iso) => (iso || "").slice(5, 10);
const dirClass = (v) => (v > 0 ? "up" : v < 0 ? "down" : "nil");

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${res.status} on ${path}`);
  return res.json();
}

function toast(msg) {
  const t = $("#toast");
  t.innerHTML = msg;
  t.hidden = false;
}
function clearToast() { $("#toast").hidden = true; }

/* ---------------- view, size, address bar ---------------- */

function showView(name) {
  const v = VIEW_ALIAS[name] || (VIEWS.includes(name) ? name : "statement");
  $$(".tab").forEach((t) => t.classList.toggle("is-on", t.dataset.view === v));
  $$(".view").forEach((s) => s.classList.toggle("is-on", s.id === "view-" + v));
  syncLink();
}

function currentView() {
  const t = $(".tab.is-on");
  return t ? t.dataset.view : "statement";
}

/* `Large` scales the root font size, and because every length in style.css is in rem that
   scales the whole page - type, rules, padding and hit targets - rather than only the words.
   Sticky, because reaching for it again after a reload mid-take is the fumble the deep links
   exist to prevent. */
function setZoom(mode, persist = true) {
  const m = mode === "present" ? "present" : "normal";
  document.documentElement.dataset.zoom = m;
  $$(".sizer button").forEach((b) => b.classList.toggle("is-on", b.dataset.zoom === m));
  if (persist) { try { localStorage.setItem("reclaim.zoom", m); } catch (e) { /* private mode */ } }
  syncLink();
}

function storedZoom() {
  try { return localStorage.getItem("reclaim.zoom"); } catch (e) { return null; }
}

/* replaceState, not pushState: this is a bookmark, not history. Filling the back button
   with every case clicked during a demo makes the console worse to drive, not better. */
function syncLink() {
  if (!state.batch) return;
  const q = new URLSearchParams({ batch: state.batch });
  if (state.run) q.set("run", state.run.replace(/^[AB]-/, ""));
  if (state.caseId) q.set("case", state.caseId);
  const v = currentView();
  if (v !== "statement") q.set("view", v);
  if (document.documentElement.dataset.zoom === "present") q.set("zoom", "present");
  history.replaceState(null, "", location.pathname + "?" + q.toString());
}

/* ---------------- boot ---------------- */

async function boot() {
  let data;
  try {
    data = await api("/api/batches");
  } catch (e) {
    return toast("Could not reach the API: " + escapeHtml(e.message));
  }
  if (!data.batches.length) {
    return toast("No batches on disk. Run <code>python -m reclaim.synth.generator --batch B</code>");
  }

  const sel = $("#batch");
  sel.innerHTML = data.batches
    .map((b) => `<option value="${b.name}">${b.name}</option>`).join("");

  const asked = wanted.batch && data.batches.find((b) => b.name === wanted.batch.toUpperCase());
  const chosen = asked || data.batches.find((b) => b.has_ledger) || data.batches[0];
  sel.value = chosen.name;
  state.batch = chosen.name;
  state.meta = Object.fromEntries(data.batches.map((b) => [b.name, b]));

  sel.addEventListener("change", () => {
    state.batch = sel.value; state.caseId = null; loadBatch();
  });
  $("#run").addEventListener("change", () => {
    state.run = $("#run").value; state.caseId = null; syncLink(); loadRun();
  });
  $$(".tab").forEach((t) => t.addEventListener("click", () => showView(t.dataset.view)));
  $$(".sizer button").forEach((b) => b.addEventListener("click", () => setZoom(b.dataset.zoom)));
  setZoom(wanted.zoom || storedZoom() || "normal", !wanted.zoom);

  $("#play").addEventListener("click", togglePlay);
  $("#restart").addEventListener("click", restart);

  await loadBatch();
}

async function loadBatch() {
  clearToast();
  const meta = state.meta[state.batch];
  $("#m-batch").textContent = state.batch;
  $("#m-basis").textContent = meta && meta.role ? meta.role : "—";
  $("#m-cases").textContent = meta ? inr(meta.cases) : "—";
  $("#m-risk").textContent = meta ? rs(meta.at_risk_paise) : "—";

  let results;
  try {
    results = await api(`/api/results?batch=${state.batch}`);
  } catch (e) {
    renderNoLedger();
    return toast(
      `No ledger for batch ${escapeHtml(state.batch)}. Run ` +
      `<code>python -m reclaim.eval.replay --batch ${escapeHtml(state.batch)} --arms all</code>`);
  }

  // Most capable arm first, and default to it. Landing on `control` opens the console on an
  // arm that by definition does nothing, which reads as a broken page rather than the point.
  const rank = { agent: 0, rules: 1, naive: 2, control: 3 };
  const arms = [...results.arms].sort((a, b) => (rank[a.arm] ?? 9) - (rank[b.arm] ?? 9));

  const runSel = $("#run");
  runSel.innerHTML = arms.map((a) => `<option value="${a.run_id}">${a.arm}</option>`).join("");
  const askedArm = wanted.arm && arms.find((a) => a.arm === wanted.arm || a.run_id === wanted.arm);
  if (askedArm) runSel.value = askedArm.run_id;
  state.run = runSel.value;

  renderStatement(results);
  api(`/api/invariants?batch=${state.batch}`).then(renderAssurance).catch(() => {});
  api(`/api/detection?batch=${state.batch}`).then(renderDetection).catch(() => {});
  await loadRun();

  // Consumed once. A deep link sets the opening shot; it must not keep dragging the console
  // back to that case every time the operator changes arm mid-demo.
  // A link that names a case means to show that case. Without this it would set the case
  // and leave the reader on the statement, which is the tab that cannot display it.
  if (wanted.view) showView(wanted.view);
  else if (wanted.case) showView("book");
  if (wanted.case) await showCase(wanted.case);
  wanted.case = wanted.view = wanted.arm = wanted.batch = null;
  syncLink();
}

async function loadRun() {
  stop();
  const data = await api(`/api/timeline?batch=${state.batch}&run=${state.run}&limit=3000`);
  state.events = data.events;
  state.totals.rows = data.ledger_rows;
  const hint = $("#stream-hint");
  if (hint) hint.textContent = `replaying ${data.run_id}`;
  restart();
}

/* ---------------- the statement ---------------- */

const ARM_BLURB = {
  control: "no intervention at all",
  naive:   "retry immediately, three times, fixed interval",
  rules:   "policy engine, keyword diagnosis, no model",
  agent:   "policy engine, model diagnosis",
};
const ARM_ORDER = { control: 0, naive: 1, rules: 2, agent: 3 };

/* Net lift, drawn, on a SQUARE-ROOT scale - and the column header says so, because an
   undisclosed non-linear axis is a lie told with a picture.

   Linear was tried and does not work here: naive's net lift is about -56 lakh and the two
   arms a reader is actually comparing are +3.3 and +5.7 lakh, so on a linear axis naive
   fills the row and the comparison that matters renders as two identical specks. The figure
   beside the bar is always exact and unscaled; the bar only has to carry the sign and the
   order of magnitude from across a room. */
function liftBar(paise, scale) {
  if (!scale) return "";
  const frac = Math.min(Math.sqrt(Math.abs(paise)) / Math.sqrt(scale), 1);
  const w = paise === 0 ? 0 : Math.max(frac * 50, 1.5);
  const side = paise >= 0 ? `left:50%;width:${w}%` : `right:50%;width:${w}%`;
  return `<span class="track"><span class="zero"></span>` +
         `<span class="fill ${paise >= 0 ? "up" : "down"}" style="${side}"></span></span>`;
}

function renderNoLedger() {
  $("#claim-line").textContent =
    "No run recorded for this batch yet. Replay it and reload.";
  $("#derivation").innerHTML = "";
  $("#figures").innerHTML = "";
  $("#ledger-notes").innerHTML = "";
  $("#arms tbody").innerHTML = "";
  $("#working tbody").innerHTML = "";
}

function renderStatement(data) {
  const arms = [...data.arms].sort((a, b) => (ARM_ORDER[a.arm] ?? 9) - (ARM_ORDER[b.arm] ?? 9));
  const by = Object.fromEntries(arms.map((a) => [a.arm, a]));
  const control = by.control;
  const subject = by.agent || by.rules || arms[arms.length - 1];

  renderClaim(data, subject, control);
  renderFigures(by, subject, control);
  renderDerivation(subject, control);
  renderNotes(by, subject, control);

  const scale = Math.max(...arms.map((a) => Math.abs(a.lift_net_paise)), 1);

  $("#arms tbody").innerHTML = arms.map((a) => {
    const isSubject = subject && a.arm === subject.arm;
    const lift = a.arm === "control"
      ? `<span class="nil">the baseline</span>`
      : `<span class="liftcell">${liftBar(a.lift_net_paise, scale)}` +
        `<span class="${dirClass(a.lift_net_paise)}">${fig(a.lift_net_paise)}</span></span>`;
    return `<tr class="${isSubject ? "is-subject" : ""}">
      <td class="arm">${escapeHtml(a.arm)}<small>${escapeHtml(ARM_BLURB[a.arm] || "")}</small></td>
      <td class="fig">${pct(a.recovery_rate)}</td>
      <td class="fig">${fig(a.net_paise)}</td>
      <td class="fig">${lift}</td>
      <td class="fig ${a.mandates_halted ? "down" : "nil"}">${
        a.mandates_halted ? `${inr(a.mandates_halted)} · ${pct(a.mandate_halt_rate)}` : "none"}</td>
      <td class="fig ${a.double_charges ? "down" : "nil"}">${
        a.double_charges ? inr(a.double_charges) : "none"}</td>
    </tr>`;
  }).join("");

  $("#working tbody").innerHTML = arms.map((a) => `
    <tr class="${subject && a.arm === subject.arm ? "is-subject" : ""}">
      <td class="arm">${escapeHtml(a.arm)}</td>
      <td class="fig">${inr(a.recovered)}</td>
      <td class="fig">${inr(a.recovered_organic)}</td>
      <td class="fig ${a.arm === "control" ? "nil" : dirClass(a.lift_cases)}">${
        a.arm === "control" ? "—" : (a.lift_cases > 0 ? "+" : "") + inr(a.lift_cases)}</td>
      <td class="fig">${inr(a.charge_attempts)}</td>
      <td class="fig">${inr(a.contacts)}</td>
      <td class="fig">${fig(a.gross_paise)}</td>
      <td class="fig">${fig(-a.cost_paise)}</td>
      <td class="fig ${a.residual_loss_paise ? "down" : ""}">${fig(-a.residual_loss_paise)}</td>
      <td class="fig ${dirClass(a.net_paise)}">${fig(a.net_paise)}</td>
      <td class="fig">${a.cost_per_rupee_lifted != null
        ? a.cost_per_rupee_lifted.toFixed(3)
        : `<span class="nil">—</span>`}</td>
    </tr>`).join("");

  // An arm that has not been replayed yet is named rather than silently absent, so a reader
  // can tell "not built" apart from "built and scored zero".
  for (const arm of data.pending_arms || []) {
    $("#arms tbody").insertAdjacentHTML("beforeend",
      `<tr class="pending"><td class="arm">${escapeHtml(arm)}</td>` +
      `<td colspan="5">not replayed yet</td></tr>`);
    $("#working tbody").insertAdjacentHTML("beforeend",
      `<tr class="pending"><td class="arm">${escapeHtml(arm)}</td>` +
      `<td colspan="10">not replayed yet</td></tr>`);
  }
}

/* The four figures a reader wants before any table: what it was worth, how much it
   recovered, and the two things it did not break. Each is captioned with what the strawman
   did to the same batch, because a zero means nothing without something to be zero against. */
function renderFigures(by, subject, control) {
  if (!subject || !control) { $("#figures").innerHTML = ""; return; }
  const naive = by.naive;
  const lift = subject.lift_net_paise;

  const cards = [
    { dir: dirClass(lift), val: rs(lift), lbl: "net lift over doing nothing",
      sub: `control collects ${rs(control.net_paise)} unaided — subtracted, not counted` },
    { dir: "", val: pct(subject.recovery_rate), lbl: "of cases recovered",
      sub: `${inr(subject.recovered)} of ${inr(subject.cases)}, against ${pct(control.recovery_rate)} doing nothing` },
    { dir: subject.mandates_halted ? "down" : "up",
      val: subject.mandates_halted ? inr(subject.mandates_halted) : "None",
      lbl: "mandates destroyed",
      sub: naive ? `retrying blindly destroyed ${inr(naive.mandates_halted)}` : "subscriptions halted by over-retrying" },
    { dir: subject.double_charges ? "down" : "up",
      val: subject.double_charges ? inr(subject.double_charges) : "None",
      lbl: "payments charged twice",
      sub: naive ? `retrying blindly did it ${inr(naive.double_charges)} times` : "money taken twice from a customer" },
  ];

  $("#figures").innerHTML = cards.map((c) => `
    <div class="f-item ${c.dir}">
      <div class="f-val">${c.val}</div>
      <div class="f-lbl">${c.lbl}</div>
      <div class="f-sub">${c.sub}</div>
    </div>`).join("");
}

/* One sentence, in the register of the statement: what is being claimed, and against what.
   It leads because an eleven-column table is a correct artifact and a bad first three
   seconds - a reader who does not yet know what they are looking at cannot check it. */
function renderClaim(data, subject, control) {
  if (!subject || !control) {
    $("#claim-line").textContent = "Replay the control arm to put these figures in context.";
    return;
  }
  const lift = subject.lift_net_paise;
  $("#claim-line").innerHTML =
    `Over <b>${inr(data.cases)} failed payments and mandates</b>, the ` +
    `<b>${escapeHtml(subject.arm)}</b> arm recovered <span class="n">${pct(subject.recovery_rate)}</span>. ` +
    `Doing nothing at all recovers <span class="n">${pct(control.recovery_rate)}</span>, so what the ` +
    `agent is worth is the difference — <span class="n ${dirClass(lift)}">${rs(lift)}</span>, ` +
    `after every fee, message, incentive and refund it caused.`;
}

/* The signature. The headline figure is worked, not asserted: a reader who doubts it can
   run down the column and name the line they disagree with. The rules are the accounting
   convention - one above a subtotal, two above a final total - and they carry meaning:
   this line is derived from the ones above it. */
function renderDerivation(subject, control) {
  if (!subject || !control) { $("#derivation").innerHTML = ""; return; }

  const row = (cls, op, label, value, opts = {}) => `
    <tr class="${cls}">
      <td class="op">${op}</td>
      <td class="lbl">${label}</td>
      <td class="fig">${fig(value, opts)}</td>
    </tr>`;

  const html = [
    row("", "", "Recovered by the agent", subject.gross_paise, { dashZero: false }),
    row("", "less", "Cost of recovering it — retry fees, messages, incentives, refunds", -subject.cost_paise),
    row("", "less", "Subscription revenue forfeited by halting a mandate", -subject.residual_loss_paise),
    row("sub", "", "Net retained", subject.net_paise, { dashZero: false }),
    `<tr class="spacer"><td colspan="3"></td></tr>`,
    row("", "less", "Recovered without any help — what the control arm collected", -control.net_paise),
    row(`total ${dirClass(subject.lift_net_paise)}`, "", "Net lift over doing nothing",
        subject.lift_net_paise, { dashZero: false }),
  ].join("");

  const tb = $("#derivation").querySelector("tbody") || $("#derivation");
  tb.innerHTML = html;
}

/* The aside carries what the figures strip cannot: the numbers that need a sentence to mean
   anything. It used to repeat "charged twice" and "mandates destroyed" straight from the
   strip above it, which is wasted column - and meanwhile the single most important figure in
   an AI-track submission was nowhere on the page at all. */
function renderNotes(by, subject, control) {
  if (!subject || !control) { $("#ledger-notes").innerHTML = ""; return; }

  const items = [];

  // What the model is worth, in rupees. `rules` and `agent` are the same policy engine and
  // differ only in the diagnoser, so this subtraction is the whole answer to "what is the
  // language model doing here" - and it is measured rather than assumed.
  if (by.rules && by.agent) {
    const worth = by.agent.lift_net_paise - by.rules.lift_net_paise;
    items.push({
      dir: dirClass(worth),
      figure: rs(worth),
      label: "is what the model is worth",
      sub: "<b>agent</b> and <b>rules</b> are the same policy engine over the same batch and " +
           "differ only in what read the failure text — a model, or keyword matching. The gap " +
           "between their lift is attributable to diagnosis quality and to nothing else.",
    });
  }

  if (subject.cost_per_rupee_lifted != null) {
    items.push({
      dir: "",
      figure: subject.cost_per_rupee_lifted.toFixed(3),
      label: "paise spent per rupee won",
      sub: "Per rupee of <i>incremental</i> recovery, not per rupee recovered. Dividing by " +
           "gross would flatter every arm with the money that was coming back anyway.",
    });
  }

  items.push({
    dir: "",
    figure: pct(control.recovery_rate),
    label: "came back on their own",
    sub: "Failed payments recover unaided all the time. Every arm's headline rate contains " +
         "all of them, which is why the figure opposite is lift and not gross recovery.",
  });

  $("#ledger-notes").innerHTML = items.map((i) => `
    <div class="n-item ${i.dir}">
      <div class="n-fig">${i.figure}</div>
      <div class="n-lbl">${i.label}</div>
      <div class="n-sub">${i.sub}</div>
    </div>`).join("");
}

/* ---------------- assurance ---------------- */

function renderAssurance(data) {
  $("#assurance").innerHTML = data.reports.map((r) => {
    const ok = r.held === r.total;
    const broken = r.results.filter((g) => !g.held);
    return `
    <section class="a-arm">
      <div class="a-head">
        <h3>${escapeHtml(r.arm)}</h3>
        <span class="a-score ${ok ? "ok" : "no"}">${r.held} of ${r.total} held</span>
      </div>
      <p class="a-note">${r.must_hold
        ? "Asserted. A failure here is a defect."
        : "Baseline — measured, not asserted. Its failures are the finding."}</p>
      <ul class="a-list">
        ${r.results.map((g) => `
          <li class="${g.held ? "" : "broke"}">
            <span class="id">${g.id}</span>
            <span>${escapeHtml(g.title)}<span class="det">${escapeHtml(g.note || "")}</span></span>
            <span class="verdict">${g.held ? "held" : "FAILED"}</span>
          </li>`).join("")}
      </ul>
      ${broken.map((g) => `
        <div class="a-breach">
          <b>${g.id} failed on ${inr(g.violation_count)} ${g.violation_count === 1 ? "case" : "cases"}</b>
          ${g.violations.slice(0, 4).map((v) =>
            `<code>${escapeHtml(v.subject)}</code> ${escapeHtml(v.detail)}`).join("<br>")}
          ${g.violation_count > 4 ? `<br>and ${inr(g.violation_count - 4)} more` : ""}
        </div>`).join("")}
    </section>`;
  }).join("");
}

function renderDetection(data) {
  const entries = Object.entries(data.by_disposition).sort((a, b) => b[1] - a[1]);
  const max = Math.max(...entries.map((e) => e[1]), 1);
  $("#detection").innerHTML = entries.map(([k, v]) => `
    <div class="d-row">
      <span class="lbl">${escapeHtml(k)}</span>
      <span class="track"><span class="fill" style="width:${(v / max) * 100}%"></span></span>
      <span class="val">${inr(v)}</span>
    </div>`).join("");
}

/* ---------------- replay ---------------- */

function restart() {
  stop();
  state.cursor = 0;
  state.caseId = null;
  syncLink();
  state.totals = { worked: new Set(), charges: 0, contacts: 0, recovered: 0, double: 0, rows: state.totals.rows };
  $("#detail").innerHTML =
    `<p class="vacant">Select <b>Play</b>, or choose an entry from the decision list.</p>`;
  paint();

  $("#stream").innerHTML = !state.events.length
    ? `<li class="idle"><p><b>No actions recorded.</b> This arm did nothing at all — which for
       the control arm is the entire point, and is what every other arm's recovery figure is
       measured against. Change arm to watch one work.</p></li>`
    : `<li class="idle"><p>Select <b>Play</b> to replay all ${inr(state.events.length)} recorded
       decisions in order, or open a case directly from a link. Nothing here is generated
       live — it is read back off the append-only ledger.</p></li>`;
}

function togglePlay() { state.playing ? stop() : play(); }

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
  if (state.cursor >= state.events.length) { stop(); return; }
  step();
  state.timer = setTimeout(tick, Math.max(8, 900 / Number($("#speed").value)));
}

function step() {
  const ev = state.events[state.cursor++];
  const stream = $("#stream");
  const idle = stream.querySelector(".idle");
  if (idle) idle.remove();

  stream.prepend(renderEvent(ev));
  while (stream.children.length > 120) stream.lastElementChild.remove();
  // Newest-first feeds must stay pinned. Prepending into a container that has been scrolled
  // (which selecting an entry does) keeps scrollTop where it was, so the viewport drifts
  // past the tail and the panel goes blank while entries keep arriving.
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
  $("#s-worked").textContent = inr(state.totals.worked.size);
  $("#s-charges").textContent = inr(state.totals.charges);
  $("#s-contacts").textContent = inr(state.totals.contacts);
  $("#s-recovered").textContent = inr(state.totals.recovered);
  $("#s-double").textContent = inr(state.totals.double);
  $("#s-rows").textContent = inr(state.totals.rows);
  $("#stream-progress").textContent =
    `${inr(state.cursor)} of ${inr(state.events.length)} entries`;
}

function renderEvent(ev) {
  const li = document.createElement("li");
  let kind = "k-" + ev.kind;
  let head = "";
  let why = "";

  if (ev.kind === "charge") {
    if (ev.double_charge) {
      kind = "k-double";
      head = `charged twice · ${ev.case_id}`;
      why = "the charge succeeded on a payment that had already been debited — this is a liability, not revenue";
    } else if (ev.outcome === "captured") {
      kind = "k-capture";
      head = `recovered · ${ev.case_id}`;
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
        (ev.confidence != null ? ` at ${ev.confidence.toFixed(2)}` : "") + " — " + why;
    }
  }

  li.className = "ev " + kind;
  li.innerHTML =
    `<time>${day(ev.at)} ${clock(ev.at)}</time>` +
    `<span class="dot"></span>` +
    `<div class="body"><div class="head">${escapeHtml(head)}</div>` +
    `<div class="why">${escapeHtml(why)}</div></div>` +
    `<span class="amt">${ev.amount_paise != null ? rs(ev.amount_paise) : ""}</span>`;

  li.addEventListener("click", () => {
    // Pause on inspect. Reading a case while the feed moves underneath it is exactly the
    // frustration this panel exists to remove.
    stop();
    $$(".ev").forEach((e) => e.classList.remove("is-sel"));
    li.classList.add("is-sel");
    showCase(ev.case_id);
  });
  return li;
}

/* ---------------- one case ---------------- */

async function showCase(caseId) {
  state.caseId = caseId;
  syncLink();
  const el = $("#detail");
  el.innerHTML = `<p class="vacant">Opening ${escapeHtml(caseId)}…</p>`;

  let d;
  try {
    d = await api(`/api/case/${encodeURIComponent(caseId)}?batch=${state.batch}&run=${state.run}`);
  } catch (e) {
    el.innerHTML = `<p class="vacant">${escapeHtml(e.message)}</p>`;
    return;
  }

  const c = d.case, err = d.observed_error, det = d.detection;

  el.innerHTML = `
    <h3>${escapeHtml(c.id)}</h3>
    <p class="sub">${escapeHtml(c.rail)} · ${escapeHtml(c.issuer)} · ${escapeHtml(c.psp)} · ${escapeHtml(c.kind)}</p>

    <dl class="kv">
      <dt>At risk</dt><dd>${rs(c.amount_paise)}</dd>
      <dt>Customer</dt><dd>${escapeHtml(c.customer_id)}</dd>
      <dt>Opened</dt><dd>${escapeHtml(c.opened_at.replace("T", " ").slice(0, 16))}</dd>
      ${c.mandate_id ? `<dt>Mandate</dt><dd>${escapeHtml(c.mandate_id)}</dd>` : ""}
      <dt>Channels</dt><dd>${d.customer ? escapeHtml(d.customer.channels.join(", ")) : "—"}</dd>
    </dl>

    ${err ? `
      <h4>What the agent was given</h4>
      <div class="quote">
        “${escapeHtml(err.description)}”
        <div class="meta">
          ${escapeHtml(err.code)} · ${escapeHtml(err.source)} · ${escapeHtml(err.step)} · ${escapeHtml(err.reason)}<br>
          Bank reference: ${err.bank_reference ? escapeHtml(err.bank_reference) : "none returned"}
        </div>
      </div>` : ""}

    <h4>Detection</h4>
    <div>
      <span class="mark ${det.disposition === "eligible" ? "ok" : "no"}">${escapeHtml(det.disposition)}</span>
      ${det.flags.map((f) => `<span class="mark">${escapeHtml(f)}</span>`).join("")}
    </div>
    <p class="n-sub" style="margin:0.5rem 0 0">${escapeHtml(det.reason)}</p>

    <h4>Every action taken — ${inr(d.trail.length)} ${d.trail.length === 1 ? "entry" : "entries"}</h4>
    <ol class="trail">${d.trail.map(trailRow).join("")}</ol>`;
}

function trailRow(r) {
  let what = "", why = "";
  if (r.kind === "decision") {
    what = r.action;
    why = r.reason || "";
  } else if (r.kind === "charge") {
    what = r.double_charge ? "charged twice" : (r.outcome || "unresolved");
    why = `attempt ${r.attempt_no} · ${r.rail} · ${r.psp} · ${rs(r.amount_paise)}` +
          (r.note ? ` — ${r.note}` : "");
  } else if (r.kind === "contact") {
    what = r.channel;
    why = `${r.template} · ${r.engaged ? "engaged" : "ignored"}`;
  } else if (r.kind === "closed") {
    what = "closed";
    why = r.status + (r.recovered_paise ? ` · ${rs(r.recovered_paise)} via ${r.recovered_by}` : "");
  }
  // Two rows in this list are the ones a reader is meant to stop on: a duplicate debit, and
  // the policy refusing to cause one.
  const cls = [
    r.double_charge ? "is-double" : "",
    r.kind === "decision" && (r.action === "hold" || r.action === "escalate") ? "is-hold" : "",
  ].filter(Boolean).join(" ");

  return `<li class="${cls}"><time>${escapeHtml((r.at || "").replace("T", " ").slice(0, 16))}</time>` +
         `<div><div class="what">${escapeHtml(what)}</div>` +
         `<div class="why">${escapeHtml(why)}</div></div></li>`;
}

boot();
