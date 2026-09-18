/**
 * GridWise Frontend — Nothing / CMF Energy Dispatch Controller
 */

const $ = (id) => document.getElementById(id);

// ────────────────────────────────────────────────────────────────────────────
// API base URL.
//
// Default = same-origin ("/optimize-energy") so this works when the page is
// served from the FastAPI host. Override by appending `?api=http://host:port`
// to the URL, or by setting localStorage.gridwise_api in DevTools.
// ────────────────────────────────────────────────────────────────────────────
const API_BASE = (() => {
  try {
    const q = new URLSearchParams(window.location.search).get("api");
    if (q) return q.replace(/\/$/, "");
    const ls = window.localStorage?.getItem("gridwise_api");
    if (ls) return ls.replace(/\/$/, "");
  } catch (_) { /* ignore */ }
  return ""; // empty = same-origin
})();

// Pre-defined Scenario Presets
const PRESETS = {
  cleaning: {
    id: "case-solar-cleaning",
    notes: [
      "Facilities will wash the rooftop solar panels from noon until 2 PM. During cleaning, usable solar should be treated as roughly 25% of the forecast.",
      "Do not charge the battery between 6 PM and 9 PM.",
      "Keep a minimum battery reserve of 6 kWh at all times."
    ],
    battery: { capacity_kwh: 20, initial_kwh: 10, min_reserve_kwh: 4, charge_limit_kw: 5, discharge_limit_kw: 5 },
    hours: defaultHours()
  },
  paraphrased: {
    id: "case-paraphrased",
    notes: [
      "Panels will be serviced by cleaners from 12pm to 2pm; treat usable generation as roughly a quarter of the forecast.",
      "Avoid charging between 18:00 and 21:00.",
      "Hold at least 6 kWh in the battery at all times."
    ],
    battery: { capacity_kwh: 20, initial_kwh: 10, min_reserve_kwh: 4, charge_limit_kw: 5, discharge_limit_kw: 5 },
    hours: defaultHours()
  },
  basic: {
    id: "case-basic",
    notes: [],
    battery: { capacity_kwh: 20, initial_kwh: 10, min_reserve_kwh: 4, charge_limit_kw: 5, discharge_limit_kw: 5 },
    hours: defaultHours()
  },
  peak: {
    id: "case-peak-shaving",
    notes: [
      "Heavy peak grid load expected. Limit grid draw to at most 4 kWh between 17:00 and 21:00.",
      "Avoid charging battery from 17:00 to 22:00."
    ],
    battery: { capacity_kwh: 25, initial_kwh: 15, min_reserve_kwh: 5, charge_limit_kw: 6, discharge_limit_kw: 6 },
    hours: defaultHours()
  },
  curtail: {
    id: "case-grid-curtailment",
    notes: [
      "Local grid maintenance: cap grid import at 2 kWh between 8 AM and 11 AM.",
      "Solar output reduced to 50% from 9 AM to 1 PM due to dust storm."
    ],
    battery: { capacity_kwh: 20, initial_kwh: 12, min_reserve_kwh: 4, charge_limit_kw: 5, discharge_limit_kw: 5 },
    hours: defaultHours()
  }
};

let currentHours = defaultHours();
let lastOptimizationResult = null;

function defaultHours() {
  const demand = [3, 3, 3, 3, 3, 4, 6, 8, 7, 5, 4, 4, 4, 4, 4, 5, 7, 9, 10, 9, 7, 5, 4, 3];
  const solar  = [0, 0, 0, 0, 0, 0, 1, 3, 5, 7, 9, 10, 10, 9, 7, 4, 2, 1, 0, 0, 0, 0, 0, 0];
  const tariff = [6, 6, 6, 6, 6, 7, 8, 9, 9, 7, 6, 5, 5, 5, 6, 8, 10, 12, 13, 12, 10, 8, 7, 6];
  return Array.from({ length: 24 }, (_, h) => ({
    hour: h,
    demand_kwh: demand[h],
    solar_kwh: solar[h],
    tariff_bdt_per_kwh: tariff[h]
  }));
}

function hoursToCSV(hours) {
  return hours.map(h => `${h.hour},${h.demand_kwh},${h.solar_kwh},${h.tariff_bdt_per_kwh}`).join("\n");
}

function parseCSV(text) {
  const lines = text.trim().split(/\r?\n/).filter(Boolean);
  if (lines.length !== 24) {
    throw new Error(`Expected exactly 24 rows of hourly data, received ${lines.length}`);
  }
  return lines.map((line, i) => {
    const parts = line.split(",").map(s => parseFloat(s.trim()));
    if (parts.length < 4 || parts.some(isNaN)) {
      throw new Error(`Row ${i + 1} has invalid format: expected 'hour,demand,solar,tariff'`);
    }
    return {
      hour: i,
      demand_kwh: Math.max(0, parts[1]),
      solar_kwh: Math.max(0, parts[2]),
      tariff_bdt_per_kwh: Math.max(0, parts[3])
    };
  });
}

function renderMiniBarPreview(hours) {
  const container = $("hours-preview-strip");
  if (!container) return;
  const maxDemand = Math.max(1, ...hours.map(h => h.demand_kwh));
  
  container.innerHTML = hours.map(h => {
    const heightPercent = Math.max(10, (h.demand_kwh / maxDemand) * 100);
    const hasSolar = h.solar_kwh > 0;
    const cls = hasSolar ? "mini-bar has-solar" : "mini-bar has-demand";
    return `<div class="${cls}" style="height:${heightPercent}%" title="Hour ${h.hour}:00 | Demand: ${h.demand_kwh}kWh, Solar: ${h.solar_kwh}kWh, Tariff: ${h.tariff_bdt_per_kwh}BDT"></div>`;
  }).join("");
}

function loadPreset(key) {
  const preset = PRESETS[key];
  if (!preset) return;

  $("scenario-id-tag").textContent = `ID: ${preset.id}`;
  $("operator-notes").value = preset.notes.join("\n");

  $("bat-capacity").value = preset.battery.capacity_kwh;
  $("bat-initial").value = preset.battery.initial_kwh;
  $("bat-min-reserve").value = preset.battery.min_reserve_kwh;
  $("bat-charge-limit").value = preset.battery.charge_limit_kw;
  $("bat-discharge-limit").value = preset.battery.discharge_limit_kw;

  currentHours = preset.hours;
  $("hours-csv").value = hoursToCSV(currentHours);
  renderMiniBarPreview(currentHours);

  document.querySelectorAll(".preset-pill").forEach(p => p.classList.remove("active"));
  const btn = $(`btn-preset-${key}`);
  if (btn) btn.classList.add("active");

  toast(`Preset '${key}' loaded`);
}

function buildPayload() {
  const scenarioId = $("scenario-id-tag").textContent.replace("ID: ", "").trim() || "scenario-001";
  let notes = $("operator-notes").value
    .split(/\r?\n/)
    .map(s => s.trim())
    .filter(Boolean);

  // Backend requires 1-3 operator notes. When the operator deliberately
  // submits a baseline ("no instructions today") scenario, fall back to a
  // single sentinel note so the directive path still resolves to no_op.
  if (notes.length === 0) {
    notes = ["Run baseline economic dispatch. No special operator directives today."];
  }

  const capacity = parseFloat($("bat-capacity").value);
  const initial = parseFloat($("bat-initial").value);
  const minReserve = parseFloat($("bat-min-reserve").value);
  const chargeLimit = parseFloat($("bat-charge-limit").value);
  const dischargeLimit = parseFloat($("bat-discharge-limit").value);

  if ([capacity, initial, minReserve, chargeLimit, dischargeLimit].some(isNaN)) {
    throw new Error("All battery specification fields must be valid numbers");
  }
  if (initial > capacity) {
    throw new Error("Initial battery energy cannot exceed maximum capacity");
  }
  if (minReserve > capacity) {
    throw new Error("Minimum reserve cannot exceed maximum capacity");
  }

  const hours = parseCSV($("hours-csv").value || hoursToCSV(currentHours));

  return {
    scenario_id: scenarioId,
    operator_notes: notes,
    scenario: {
      scenario_id: scenarioId,
      battery: {
        capacity_kwh: capacity,
        initial_kwh: initial,
        min_reserve_kwh: minReserve,
        charge_limit_kw: chargeLimit,
        discharge_limit_kw: dischargeLimit
      },
      hours: hours
    }
  };
}

function toast(msg, type = "ok") {
  const el = $("toast");
  if (!el) return;
  el.textContent = msg;
  el.className = `cmf-toast show ${type === "err" ? "err" : ""}`;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.remove("show"), 3500);
}

function escapeHTML(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}



function renderTable(plan, scenarioHours) {
  const tbody = $("schedule-tbody");
  if (!plan || plan.length === 0) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="9">No schedule available.</td></tr>`;
    return;
  }

  tbody.innerHTML = plan.map((r, i) => {
    const h = String(r.hour).padStart(2, "0") + ":00";
    const solar = r.solar_used_kwh !== undefined ? r.solar_used_kwh : (r.solar_kwh || 0);
    const soc = r.battery_energy_after_kwh !== undefined ? r.battery_energy_after_kwh : (r.battery_state_kwh || 0);
    const isChg = r.battery_action === "charge" || (r.battery_charge_kwh && r.battery_charge_kwh > 0);
    const isDis = r.battery_action === "discharge" || (r.battery_discharge_kwh && r.battery_discharge_kwh > 0);
    const chgVal = isChg ? (r.battery_kwh || r.battery_charge_kwh || 0) : 0;
    const disVal = isDis ? (r.battery_kwh || r.battery_discharge_kwh || 0) : 0;
    const demand = (r.grid_kwh + solar + disVal - chgVal).toFixed(2);
    const tariff = r.tariff_bdt_per_kwh !== undefined ? r.tariff_bdt_per_kwh : (scenarioHours && scenarioHours[i] ? scenarioHours[i].tariff_bdt_per_kwh : 0);
    const cost = (r.grid_kwh * tariff).toFixed(2);

    return `
      <tr>
        <td><strong>${h}</strong></td>
        <td>${demand}</td>
        <td class="col-solar">${solar.toFixed(2)}</td>
        <td class="col-grid">${r.grid_kwh.toFixed(2)}</td>
        <td class="col-charge">${chgVal > 0 ? "+" + chgVal.toFixed(2) : "—"}</td>
        <td class="col-discharge">${disVal > 0 ? "-" + disVal.toFixed(2) : "—"}</td>
        <td class="col-soc"><strong>${soc.toFixed(2)}</strong></td>
        <td class="col-tariff">${tariff.toFixed(2)}</td>
        <td><strong>${cost}</strong></td>
      </tr>
    `;
  }).join("");
}

function renderKPIs(resp, scenarioHours) {
  $("kpi-grid").innerHTML = `${resp.total_grid_kwh.toFixed(2)} <span class="kpi-unit">kWh</span>`;
  $("kpi-cost").innerHTML = `${resp.total_cost_bdt.toFixed(2)} <span class="kpi-unit">BDT</span>`;
  $("kpi-peak-grid").textContent = `Peak Draw: ${resp.peak_grid_kwh.toFixed(2)} kWh`;

  // Calculate unoptimized baseline cost
  let baseCost = 0;
  for (let i = 0; i < 24; i++) {
    const d = scenarioHours[i].demand_kwh;
    const s = scenarioHours[i].solar_kwh;
    const t = scenarioHours[i].tariff_bdt_per_kwh;
    const net = Math.max(0, d - s);
    baseCost += net * t;
  }
  const savings = Math.max(0, baseCost - resp.total_cost_bdt);
  const savingsPercent = baseCost > 0 ? ((savings / baseCost) * 100).toFixed(1) : "0.0";
  $("kpi-savings").textContent = `Savings: ${savings.toFixed(2)} BDT (${savingsPercent}%)`;

  const totalSolar = resp.hourly_plan.reduce((sum, r) => sum + (r.solar_used_kwh !== undefined ? r.solar_used_kwh : (r.solar_kwh || 0)), 0);
  $("kpi-solar").innerHTML = `${totalSolar.toFixed(2)} <span class="kpi-unit">kWh</span>`;

  const totalThroughput = resp.hourly_plan.reduce((sum, r) => {
    const bKwh = r.battery_kwh !== undefined ? r.battery_kwh : ((r.battery_charge_kwh || 0) + (r.battery_discharge_kwh || 0));
    return sum + bKwh;
  }, 0);
  $("kpi-battery").innerHTML = `${totalThroughput.toFixed(2)} <span class="kpi-unit">kWh</span>`;

  const lastRow = resp.hourly_plan[23];
  const endSOC = lastRow.battery_energy_after_kwh !== undefined ? lastRow.battery_energy_after_kwh : (lastRow.battery_state_kwh || 0);
  $("kpi-neutrality").textContent = `End SOC: ${endSOC.toFixed(2)} kWh · Neutral`;
}

function renderCharts(plan, battery) {
  renderEnergyMixChart(plan);
  renderSOCChart(plan, battery);
  renderArbitrageChart(plan);
}

function renderEnergyMixChart(plan) {
  const root = $("chart-energy-mix");
  if (!root) return;
  root.innerHTML = "";

  const maxVal = Math.max(1, ...plan.map(r => {
    const s = r.solar_used_kwh !== undefined ? r.solar_used_kwh : (r.solar_kwh || 0);
    const dis = (r.battery_action === "discharge" ? r.battery_kwh : 0) || (r.battery_discharge_kwh || 0);
    return s + r.grid_kwh + dis;
  }));

  for (const r of plan) {
    const s = r.solar_used_kwh !== undefined ? r.solar_used_kwh : (r.solar_kwh || 0);
    const dis = (r.battery_action === "discharge" ? r.battery_kwh : 0) || (r.battery_discharge_kwh || 0);
    const total = s + r.grid_kwh + dis;
    const col = document.createElement("div");
    col.className = "chart-col";
    col.title = `Hour ${r.hour}:00\nSolar: ${s.toFixed(2)} kWh\nBattery Dis: ${dis.toFixed(2)} kWh\nGrid: ${r.grid_kwh.toFixed(2)} kWh`;

    const colHeight = (total / maxVal) * 100;
    col.style.height = `${Math.max(4, colHeight)}%`;

    const sH = total > 0 ? (s / total) * 100 : 0;
    const bH = total > 0 ? (dis / total) * 100 : 0;
    const gH = total > 0 ? (r.grid_kwh / total) * 100 : 0;

    col.innerHTML = `
      <div class="col-seg solar" style="height:${sH}%"></div>
      <div class="col-seg battery" style="height:${bH}%"></div>
      <div class="col-seg grid" style="height:${gH}%"></div>
    `;
    root.appendChild(col);
  }
}

function renderSOCChart(plan, battery) {
  const root = $("chart-soc-canvas");
  if (!root) return;

  const w = root.clientWidth || 550;
  const h = 180;
  const pad = 24;

  const cap = battery.capacity_kwh;
  const minRes = battery.minimum_energy_kwh || battery.min_reserve_kwh || 0;

  const xs = plan.map((_, i) => pad + (i / (plan.length - 1)) * (w - 2 * pad));
  const ys = plan.map(r => {
    const soc = r.battery_energy_after_kwh !== undefined ? r.battery_energy_after_kwh : (r.battery_state_kwh || 0);
    return h - pad - ((soc / cap) * (h - 2 * pad));
  });

  const pathD = xs.map((x, i) => `${i === 0 ? "M" : "L"} ${x.toFixed(1)} ${ys[i].toFixed(1)}`).join(" ");
  const minFloorY = h - pad - ((minRes / cap) * (h - 2 * pad));

  root.innerHTML = `
    <svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" style="width:100%;height:100%;">
      <!-- Min Reserve Line -->
      <line x1="${pad}" y1="${minFloorY.toFixed(1)}" x2="${w - pad}" y2="${minFloorY.toFixed(1)}" stroke="#d71920" stroke-width="1.5" stroke-dasharray="4 4" opacity="0.75" />
      <text x="${w - pad}" y="${(minFloorY - 4).toFixed(1)}" fill="#d71920" font-size="9" text-anchor="end" font-family="JetBrains Mono">MIN FLOOR (${minRes} kWh)</text>
      
      <!-- SOC Path -->
      <path d="${pathD}" fill="none" stroke="#d6ff3a" stroke-width="2.5" />
      ${xs.map((x, i) => `<circle cx="${x.toFixed(1)}" cy="${ys[i].toFixed(1)}" r="3" fill="#ffffff" />`).join("")}
    </svg>
  `;
}


function renderArbitrageChart(plan) {
  const root = $("chart-arbitrage-canvas");
  if (!root) return;
  root.innerHTML = "";

  const maxTariff = Math.max(1, ...plan.map(r => r.tariff_bdt_per_kwh));
  const maxGrid = Math.max(1, ...plan.map(r => r.grid_kwh));

  for (const r of plan) {
    const col = document.createElement("div");
    col.className = "chart-col";
    col.title = `Hour ${r.hour}:00\nTariff: ${r.tariff_bdt_per_kwh} BDT\nGrid Import: ${r.grid_kwh.toFixed(2)} kWh`;

    const tariffHeight = (r.tariff_bdt_per_kwh / maxTariff) * 100;
    const gridHeight = (r.grid_kwh / maxGrid) * 100;

    col.style.height = `${Math.max(6, tariffHeight)}%`;
    col.style.background = "rgba(214, 255, 58, 0.15)";
    col.style.borderTop = "2px solid var(--nothing-lime)";

    col.innerHTML = `
      <div style="height:${gridHeight}%;background:var(--cmf-orange);width:100%;border-radius:2px 2px 0 0;"></div>
    `;
    root.appendChild(col);
  }
}

async function runOptimization() {
  let payload;
  try {
    payload = buildPayload();
  } catch (err) {
    toast(err.message, "err");
    return;
  }
  // If the user has edited any directive JSON in the dashboard, send the
  // edited interpretation as `optimized_directives` so the backend
  // re-optimizes with the manual override instead of re-running the LLM.
  if (editedDirectives && editedDirectives.length) {
    payload.optimized_directives = buildEditedDirectivesPayload();
  }

  const btn = $("btn-dispatch");
  btn.disabled = true;
  btn.style.opacity = "0.7";
  toast("Processing directives with LLM & optimizing...");

  try {
    const res = await fetch(`${API_BASE}/optimize-energy`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });

    if (!res.ok) {
      const errText = await res.text();
      throw new Error(`API ${res.status}: ${errText || res.statusText}`);
    }

    const data = await res.json();
    lastOptimizationResult = data;

    renderDirectives(data.directive_interpretation);
    renderTable(data.hourly_plan, payload.scenario.hours);
    renderKPIs(data, payload.scenario.hours);
    renderCharts(data.hourly_plan, payload.scenario.battery);

    toast(`Optimal dispatch calculated · ${data.total_cost_bdt.toFixed(2)} BDT`);
  } catch (err) {
    console.error(err);
    const msg = (err && err.message) || "Optimization request failed";
    // Distinguish the most common cause: the backend isn't running on the
    // expected host. The browser's "Failed to fetch" message is what we
    // surface when the network request never even left the page.
    if (/Failed to fetch|NetworkError|TypeError/i.test(msg)) {
      toast(
        `Backend unreachable at ${API_BASE || window.location.origin}. ` +
        `Start uvicorn on port 8000 (see README).`,
        "err"
      );
    } else {
      toast(msg, "err");
    }
  } finally {
    btn.disabled = false;
    btn.style.opacity = "1";
  }
}

// ---------------------------------------------------------------------------
// Live API tester (Section 04)
// ---------------------------------------------------------------------------

async function runLiveTest(method, path, payload) {
  const panel = $("api-test-panel");
  const metaEl = $("api-test-meta");
  const outputEl = $("api-test-output");
  if (!panel || !outputEl || !metaEl) {
    toast("Live-test panel unavailable in DOM", "err");
    return;
  }

  panel.hidden = false;
  metaEl.textContent = `${method} ${path} · …`;
  outputEl.textContent = "// sending request…";

  const started = performance.now();
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (payload !== null && payload !== undefined) {
    opts.body = JSON.stringify(payload);
  }
  try {
    const res = await fetch(`${API_BASE}${path}`, opts);
    const elapsed = (performance.now() - started).toFixed(0);
    const text = await res.text();
    let pretty;
    try {
      pretty = JSON.stringify(JSON.parse(text), null, 2);
    } catch {
      pretty = text;
    }
    metaEl.textContent = `${method} ${path} · ${res.status} ${res.statusText} · ${elapsed} ms`;
    outputEl.textContent = pretty;
    if (!res.ok) toast(`${method} ${path} → ${res.status}`, "err");
  } catch (err) {
    metaEl.textContent = `${method} ${path} · network error`;
    outputEl.textContent = String(err && err.message ? err.message : err);
    toast(`Live test failed: ${err.message || err}`, "err");
  }
}

// ---------------------------------------------------------------------------
// Editable directive cards
// ---------------------------------------------------------------------------

let editedDirectives = [];

function renderDirectives(items) {
  editedDirectives = (items || []).map((it, idx) => ({
    note_index: it.note_index !== undefined ? it.note_index : idx,
    applies: it.applies !== undefined ? it.applies : (it.directive_type !== "no_op"),
    directive_type: it.directive_type || (it.directive && it.directive.type) || "no_op",
    structured_adjustment: it.structured_adjustment || null,
    explanation: it.explanation || "",
  }));
  const container = $("directives-list");
  if (!editedDirectives.length) {
    container.innerHTML = `
      <div class="empty-state">
        <div class="empty-dot"></div>
        <p>No operator notes provided. System executed pure economic grid dispatch against physical battery bounds.</p>
      </div>`;
    return;
  }

  container.innerHTML = editedDirectives.map((d, idx) => {
    const dtype = d.directive_type || "unknown";
    const applies = d.applies;
    return `
      <div class="directive-card" data-dir-idx="${idx}">
        <div class="directive-card-top">
          <span class="dir-badge ${dtype}">${dtype}</span>
          <span class="dir-confidence">${applies ? "APPLIES: TRUE" : "NO-OP"}</span>
          <button class="btn-micro dir-edit-btn" data-dir-edit="${idx}">EDIT JSON</button>
          <button class="btn-micro dir-save-btn hidden" data-dir-save="${idx}">SAVE</button>
          <button class="btn-micro dir-cancel-btn hidden" data-dir-cancel="${idx}">CANCEL</button>
        </div>
        <div class="dir-note-quote">"${escapeHTML(d.explanation || "Note #" + d.note_index)}"</div>
        <pre class="dir-json" data-dir-pre="${idx}"><code>${escapeHTML(JSON.stringify(d.structured_adjustment, null, 2))}</code></pre>
        <textarea class="dir-json-edit hidden" data-dir-text="${idx}" rows="5" spellcheck="false"></textarea>
        <div class="dir-edit-error hidden" data-dir-err="${idx}"></div>
      </div>
    `;
  }).join("");

  // Wire up the new edit / save / cancel handlers (delegation-free for clarity).
  container.querySelectorAll(".dir-edit-btn").forEach(btn => {
    btn.addEventListener("click", () => enterDirectiveEditMode(parseInt(btn.dataset.dirEdit, 10)));
  });
  container.querySelectorAll(".dir-cancel-btn").forEach(btn => {
    btn.addEventListener("click", () => cancelDirectiveEdit(parseInt(btn.dataset.dirCancel, 10)));
  });
  container.querySelectorAll(".dir-save-btn").forEach(btn => {
    btn.addEventListener("click", () => saveDirectiveEdit(parseInt(btn.dataset.dirSave, 10)));
  });
}

function enterDirectiveEditMode(idx) {
  const card = document.querySelector(`.directive-card[data-dir-idx="${idx}"]`);
  if (!card) return;
  const ta = card.querySelector(`[data-dir-text="${idx}"]`);
  const pre = card.querySelector(`[data-dir-pre="${idx}"]`);
  if (!ta || !pre) return;
  ta.value = pre.querySelector("code").textContent;
  ta.classList.remove("hidden");
  pre.classList.add("hidden");
  card.querySelector(`.dir-edit-btn[data-dir-edit="${idx}"]`).classList.add("hidden");
  card.querySelector(`.dir-save-btn[data-dir-save="${idx}"]`).classList.remove("hidden");
  card.querySelector(`.dir-cancel-btn[data-dir-cancel="${idx}"]`).classList.remove("hidden");
  const err = card.querySelector(`[data-dir-err="${idx}"]`);
  if (err) {
    err.classList.add("hidden");
    err.textContent = "";
  }
}

function cancelDirectiveEdit(idx) {
  const card = document.querySelector(`.directive-card[data-dir-idx="${idx}"]`);
  if (!card) return;
  card.querySelector(`[data-dir-text="${idx}"]`).classList.add("hidden");
  card.querySelector(`[data-dir-pre="${idx}"]`).classList.remove("hidden");
  card.querySelector(`.dir-edit-btn[data-dir-edit="${idx}"]`).classList.remove("hidden");
  card.querySelector(`.dir-save-btn[data-dir-save="${idx}"]`).classList.add("hidden");
  card.querySelector(`.dir-cancel-btn[data-dir-cancel="${idx}"]`).classList.add("hidden");
}

function saveDirectiveEdit(idx) {
  const card = document.querySelector(`.directive-card[data-dir-idx="${idx}"]`);
  if (!card) return;
  const ta = card.querySelector(`[data-dir-text="${idx}"]`);
  const pre = card.querySelector(`[data-dir-pre="${idx}"]`);
  const errEl = card.querySelector(`[data-dir-err="${idx}"]`);
  const raw = ta.value.trim();
  let parsed = null;
  if (raw.length === 0 || raw === "null") {
    parsed = null;
  } else {
    try {
      parsed = JSON.parse(raw);
    } catch (e) {
      errEl.textContent = `Invalid JSON: ${e.message}`;
      errEl.classList.remove("hidden");
      return;
    }
  }
  // Persist into the in-memory edited set.
  editedDirectives[idx].structured_adjustment = parsed;
  // Re-render this card's view.
  const display = parsed === null ? "null" : JSON.stringify(parsed, null, 2);
  pre.querySelector("code").textContent = display;
  cancelDirectiveEdit(idx);
  toast(`Directive #${idx + 1} updated — re-run dispatch to apply`);
}

function buildEditedDirectivesPayload() {
  return editedDirectives.map(d => ({
    note_index: d.note_index,
    applies: !!d.applies,
    directive_type: d.directive_type,
    structured_adjustment: d.structured_adjustment,
    explanation: d.explanation,
  }));
}

async function checkHealth() {
  const pip = $("sys-pip");
  const label = $("sys-label");
  const badge = $("model-badge");

  try {
    const res = await fetch(`${API_BASE}/health/llm`);
    if (!res.ok) throw new Error("Health check failed");
    const data = await res.json();

    if (data.llm_enabled) {
      pip.className = "status-pip";
      label.textContent = "SYS_ONLINE";
      badge.textContent = `LLM: ${data.provider.toUpperCase()} (${data.model || "active"})`;
    } else {
      pip.className = "status-pip warn";
      label.textContent = "SYS_RULES_ONLY";
      badge.textContent = "LLM: RULES FALLBACK";
    }
  } catch (e) {
    pip.className = "status-pip err";
    label.textContent = "SYS_OFFLINE";
    const origin = API_BASE || window.location.origin;
    badge.textContent = `API: UNREACHABLE @ ${origin}`;
  }
}

function exportCSV() {
  if (!lastOptimizationResult) {
    toast("Run optimization first to export schedule", "err");
    return;
  }
  const headers = "hour,demand_kwh,solar_kwh,grid_kwh,battery_charge_kwh,battery_discharge_kwh,battery_state_kwh,tariff_bdt_per_kwh,cost_bdt\n";
  const rows = lastOptimizationResult.hourly_plan.map(r => {
    const demand = (r.grid_kwh + r.solar_kwh + r.battery_discharge_kwh - r.battery_charge_kwh).toFixed(2);
    const cost = (r.grid_kwh * r.tariff_bdt_per_kwh).toFixed(2);
    return `${r.hour},${demand},${r.solar_kwh},${r.grid_kwh},${r.battery_charge_kwh},${r.battery_discharge_kwh},${r.battery_state_kwh},${r.tariff_bdt_per_kwh},${cost}`;
  }).join("\n");

  const blob = new Blob([headers + rows], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `gridwise_schedule_${lastOptimizationResult.scenario_id}.csv`;
  a.click();
  URL.revokeObjectURL(url);
  toast("CSV schedule exported");
}

document.addEventListener("DOMContentLoaded", () => {
  // Preset buttons
  $("btn-preset-cleaning")?.addEventListener("click", () => loadPreset("cleaning"));
  $("btn-preset-paraphrased")?.addEventListener("click", () => loadPreset("paraphrased"));
  $("btn-preset-basic")?.addEventListener("click", () => loadPreset("basic"));
  $("btn-preset-peak")?.addEventListener("click", () => loadPreset("peak"));
  $("btn-preset-curtail")?.addEventListener("click", () => loadPreset("curtail"));

  // Quick insert chips
  document.querySelectorAll(".chip-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const text = btn.dataset.insert;
      const ta = $("operator-notes");
      ta.value = ta.value ? ta.value.trim() + "\n" + text : text;
      toast("Note added");
    });
  });

  // Toggle raw CSV editor
  $("btn-toggle-hours-raw")?.addEventListener("click", () => {
    const wrap = $("hours-raw-container");
    wrap.classList.toggle("hidden");
  });

  $("hours-csv")?.addEventListener("input", (e) => {
    try {
      currentHours = parseCSV(e.target.value);
      renderMiniBarPreview(currentHours);
    } catch (_) {}
  });

  // Dispatch Action
  $("btn-dispatch")?.addEventListener("click", runOptimization);
  $("btn-send-notes")?.addEventListener("click", runOptimization);
  $("btn-export-csv")?.addEventListener("click", exportCSV);

  // API Tester buttons
  $("btn-test-health")?.addEventListener("click", async () => {
    await runLiveTest("GET", "/health", null);
  });

  $("btn-test-optimize")?.addEventListener("click", async () => {
    // Live-test uses the *current* UI payload so judges can hit the
    // endpoint with the exact scenario they've just composed.
    let payload;
    try {
      payload = buildPayload();
    } catch (err) {
      toast(err.message, "err");
      return;
    }
    await runLiveTest("POST", "/optimize-energy", payload);
  });

  $("api-test-close")?.addEventListener("click", () => {
    const panel = $("api-test-panel");
    if (panel) panel.hidden = true;
  });

  $("btn-copy-curl")?.addEventListener("click", () => {
    const txt = $("curl-preview").textContent;
    navigator.clipboard.writeText(txt);
    toast("cURL command copied to clipboard");
  });

  // Initial setup
  loadPreset("cleaning");
  checkHealth();
});

