"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  apiBase: "/v1",
  authRequired: false,
  token: sessionStorage.getItem("fasttalkDiagnosticsToken") || "",
  snapshot: null,
  range: "24h",
  historyTimer: null,
  refreshTimer: null,
  sparks: { grid: [], load: [], solar: [] },
  historical: {
    financials: [],
    forecastComparison: [],
    forecastBaseComparison: [],
    plans: [],
    tariff: [],
  },
};

const ranges = {
  "6h": { milliseconds: 6 * 3600e3, bucket: 60 },
  "24h": { milliseconds: 24 * 3600e3, bucket: 300 },
  "7d": { milliseconds: 7 * 86400e3, bucket: 1800 },
  "30d": { milliseconds: 30 * 86400e3, bucket: 7200 },
};

const powerSeries = [
  ["site.load_power", "Consumption", "#b86242"],
  ["external_pv.active_power", "External PV", "#d9a629"],
  ["asw.active_power", "Solplanet AC", "#3e8b68"],
  ["grid.active_power", "Grid", "#587c95"],
];

function measurement(name) {
  return state.snapshot?.measurements?.[name] || null;
}

function number(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function formatPower(value, signed = false) {
  const watts = number(value);
  if (watts === null) return "—";
  const prefix = signed && watts > 0 ? "+" : "";
  if (Math.abs(watts) >= 1000) return `${prefix}${(watts / 1000).toFixed(2)} kW`;
  return `${prefix}${Math.round(watts)} W`;
}

function formatPercent(value, ratio = false) {
  let numeric = number(value);
  if (numeric === null) return "—";
  if (ratio) numeric *= 100;
  return `${numeric.toFixed(numeric < 10 ? 1 : 0)}%`;
}

function formatMoney(value) {
  const numeric = number(value);
  if (numeric === null) return "—";
  const sign = numeric < 0 ? "−" : "";
  return `${sign}$${Math.abs(numeric).toFixed(2)}`;
}

function formatNetCost(value) {
  const numeric = number(value);
  if (numeric === null) return "—";
  return numeric < 0
    ? `$${Math.abs(numeric).toFixed(2)} profit`
    : `$${numeric.toFixed(2)} cost`;
}

function formatPrice(value) {
  const numeric = number(value);
  return numeric === null ? "—" : `${(numeric * 100).toFixed(1)} c/kWh`;
}

function formatAge(seconds) {
  const numeric = number(seconds);
  if (numeric === null) return "unknown";
  if (numeric < 60) return `${Math.round(numeric)}s`;
  if (numeric < 3600) return `${Math.round(numeric / 60)}m`;
  if (numeric < 86400) return `${(numeric / 3600).toFixed(1)}h`;
  return `${(numeric / 86400).toFixed(1)}d`;
}

function formatTime(timestamp, includeDate = false) {
  if (!timestamp) return "—";
  const value = new Date(timestamp);
  if (Number.isNaN(value.getTime())) return "—";
  return new Intl.DateTimeFormat(undefined, {
    ...(includeDate ? { month: "short", day: "numeric" } : {}),
    hour: "numeric",
    minute: "2-digit",
  }).format(value);
}

function title(value) {
  return String(value || "unknown")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function setText(id, value) {
  const node = $(id);
  if (node) node.textContent = value;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function setChip(id, text, status) {
  const node = $(id);
  if (!node) return;
  node.textContent = text;
  node.className = "quality-chip";
  if (status && status !== "ok") node.classList.add(`quality-chip--${status}`);
}

async function api(path) {
  const headers = { Accept: "application/json" };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const response = await fetch(`${state.apiBase}${path}`, {
    headers,
    cache: "no-store",
  });
  if (response.status === 401) {
    showAuthentication("That token was not accepted.");
    throw new Error("Authentication required");
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.error || `Request failed (${response.status})`);
  }
  return response.json();
}

function showAuthentication(message = "") {
  setText("authError", message);
  if (!$("authDialog").open) $("authDialog").showModal();
  window.setTimeout(() => $("authToken").focus(), 0);
}

function toast(message) {
  const node = $("toast");
  node.textContent = message;
  node.classList.add("is-visible");
  window.clearTimeout(toast.timeout);
  toast.timeout = window.setTimeout(() => node.classList.remove("is-visible"), 4500);
}

function updateClock() {
  setText(
    "localTime",
    new Intl.DateTimeFormat(undefined, {
      weekday: "short",
      day: "numeric",
      month: "short",
      hour: "numeric",
      minute: "2-digit",
      second: "2-digit",
      timeZoneName: "short",
    }).format(new Date()),
  );
}

function renderCurrent() {
  const grid = number(measurement("grid.active_power")?.value);
  const load = number(measurement("site.load_power")?.value);
  const external = number(measurement("external_pv.active_power")?.value);
  const aswPv = number(measurement("asw.pv.active_power")?.value);
  const asw = number(measurement("asw.active_power")?.value);
  const solar = [external, aswPv]
    .filter((value) => value !== null)
    .reduce((sum, value) => sum + Math.max(0, value), 0);
  const battery = number(measurement("battery.power")?.value);
  const soc = number(measurement("battery.soc")?.value);
  const selfSufficiency = measurement("site.self_sufficiency_ratio")?.value;
  const selfConsumed = measurement("site.self_consumption_power")?.value;

  setText("gridPower", formatPower(grid === null ? null : Math.abs(grid)));
  setText("gridDirection", grid === null ? "—" : grid >= 0 ? "IMPORT" : "EXPORT");
  setText(
    "gridSub",
    grid === null
      ? "Waiting for meter"
      : `${grid >= 0 ? "Importing" : "Exporting"} · ${measurement("grid.active_power")?.quality || "unknown"}`,
  );
  setText("loadPower", formatPower(load));
  setText("selfSufficiency", `Self-sufficiency ${formatPercent(selfSufficiency, true)}`);
  setText("solarPower", solar ? formatPower(solar) : external === null && asw === null ? "—" : "0 W");
  setText("solarSplit", `${formatPower(external)} external · ${formatPower(aswPv)} Solplanet PV`);
  setText("batterySoc", formatPercent(soc));
  setText(
    "batteryState",
    battery === null ? "—" : battery > 30 ? "DISCHARGING" : battery < -30 ? "CHARGING" : "IDLE",
  );
  setText("batteryPower", `Power ${formatPower(battery, true)}`);
  $("socFill").style.width = `${Math.max(0, Math.min(100, soc || 0))}%`;

  setText("flowSolar", formatPower(solar));
  setText("flowGrid", formatPower(grid === null ? null : Math.abs(grid)));
  setText("flowGridLabel", grid === null ? "Grid" : grid >= 0 ? "Grid import" : "Grid export");
  setText("flowLoad", formatPower(load));
  setText("flowBattery", formatPower(battery === null ? null : Math.abs(battery)));
  setText("selfConsumed", formatPower(selfConsumed));

  const required = ["grid.active_power", "external_pv.active_power", "asw.pv.active_power"];
  const qualities = required.map((name) => measurement(name)?.quality || "missing");
  const quality = qualities.every((value) => value === "good")
    ? "Authoritative"
    : qualities.includes("stale") ? "Stale inputs" : "Partial data";
  setChip("flowQuality", quality, quality === "Authoritative" ? "ok" : "degraded");

  for (const [key, value] of [["grid", grid], ["load", load], ["solar", solar]]) {
    if (value !== null) {
      state.sparks[key].push(value);
      state.sparks[key] = state.sparks[key].slice(-36);
    }
    renderSpark(`${key}Spark`, state.sparks[key]);
  }
}

function currentRecommendation(plan) {
  const recommendations = plan?.recommendations || [];
  const now = Date.now() - 15 * 60e3;
  return recommendations.find((item) => new Date(item.timestamp).getTime() >= now)
    || recommendations[0]
    || null;
}

function renderRecommendation() {
  const plan = state.snapshot?.plan;
  const recommendation = currentRecommendation(plan);
  setChip("planStatus", title(plan?.status || "waiting"), plan?.status === "ready" ? "shadow" : "degraded");

  if (!recommendation) {
    setText("actionIcon", "—");
    setText("actionTime", plan?.generated_at ? `Plan ${formatTime(plan.generated_at)}` : "Waiting for a plan");
    setText("actionTitle", "No recommendation");
    setText("actionSummary", plan?.reason || "The optimiser is gathering fresh inputs.");
    $("actionReasons").replaceChildren();
    setText("expectedGrid", "—");
    setText("expectedSoc", "—");
    setText("estimatedSaving", "—");
    setText("baselinePolicy", "Baseline: native inverter mode unavailable.");
    return;
  }

  const action = recommendation.action || "hold";
  const battery = number(recommendation.battery_power_w);
  const icons = { charge: "↓", discharge: "↑", hold: "—" };
  setText("actionIcon", icons[action] || "—");
  setText("actionTime", `${formatTime(recommendation.timestamp, true)} recommendation`);
  setText("actionTitle", action === "hold" ? "Hold battery" : `${title(action)} at ${formatPower(Math.abs(battery))}`);
  setText(
    "actionSummary",
    `Forecast load ${formatPower(recommendation.forecast_load_w)}, PV ${formatPower(recommendation.forecast_pv_w)}.`,
  );
  const reasons = (recommendation.explanation || []).slice(0, 4).map((reason) => {
    const row = element("div");
    row.append(element("span", "", "✓"), element("p", "", reason));
    return row;
  });
  $("actionReasons").replaceChildren(...reasons);
  setText("expectedGrid", formatPower(recommendation.expected_grid_power_w, true));
  setText("expectedSoc", formatPercent(recommendation.expected_soc_percent));
  setText("estimatedSaving", formatMoney(plan?.simulation?.estimated_cost_improvement));
  const policy = plan?.simulation?.baseline?.policy;
  setText(
    "baselinePolicy",
    policy
      ? `No-change baseline: native ${policy.mode} at ${formatPower(policy.requested_power_w)} until ${formatPercent(policy.minimum_soc_percent)}–${formatPercent(policy.maximum_soc_percent)} bounds.`
      : "No-change baseline: native inverter mode unavailable.",
  );
}

function renderHealth() {
  const health = state.snapshot?.health || {};
  const status = health.status || "starting";
  const badge = $("healthBadge");
  badge.className = `health-badge health-badge--${status}`;
  badge.replaceChildren(
    element("span", "status-dot"),
    element("span", "", title(status)),
  );
  const counts = health.measurement_quality || {};
  const detail = Object.entries(counts).map(([name, count]) => `${count} ${name}`).join(" · ");
  setText("systemSummary", detail || "No measurements");
}

function renderWorkings() {
  const plan = state.snapshot?.plan || {};
  const forecast = state.snapshot?.forecast || {};
  const inputs = plan.inputs || {};
  const inputValues = Object.values(inputs).filter(Boolean);
  const fresh = inputValues.filter((item) => item.quality === "good").length;

  setText(
    "observeStep",
    inputValues.length
      ? `${fresh}/${inputValues.length} planning inputs are fresh; meter data remains authoritative for site accounting.`
      : "No planning input snapshot is available yet.",
  );
  setText(
    "forecastStep",
    forecast.status === "ok"
      ? `${forecast.points?.length || 0} PV points combined across ${forecast.planes?.length || 0} configured planes; ${plan.load_forecast?.method || "load model pending"}.`
      : `PV forecast is ${forecast.status || "unavailable"}; optimiser cannot safely build a schedule.`,
  );
  const current = currentRecommendation(plan);
  const constraints = current?.constraints || {};
  setText(
    "constraintStep",
    current
      ? `SOC ${formatPercent(constraints.reserve_soc_percent)}–${formatPercent(constraints.maximum_soc_percent)}; charge ${formatPower(constraints.charge_limit_w)}, discharge ${formatPower(constraints.discharge_limit_w)}.`
      : "BMS, SOC and configured site boundaries are checked before every recommendation.",
  );
  setText(
    "recommendStep",
    current
      ? `${title(current.action)} is feasible: ${current.feasible ? "yes" : "no"}. This remains a read-only shadow recommendation.`
      : plan.reason || "No executable control path is present.",
  );
  setChip(
    "inputStatus",
    inputValues.length ? `${fresh}/${inputValues.length} fresh` : "No inputs",
    fresh === inputValues.length && fresh > 0 ? "ok" : "degraded",
  );

  const rows = Object.entries(inputs).map(([name, item]) => {
    const row = element("tr");
    if (!item) {
      row.append(
        element("td", "", name),
        element("td", "", "Unavailable"),
        element("td", "", "—"),
        element("td", "", "—"),
      );
    } else {
      const value = String(item.unit).toLowerCase() === "w"
        ? formatPower(item.value)
        : item.unit === "%" ? formatPercent(item.value) : `${item.value} ${item.unit || ""}`.trim();
      row.append(
        element("td", "", name),
        element("td", "", value),
        element("td", "", formatAge(item.age_seconds)),
        element("td", "", item.source || "unknown"),
      );
    }
    return row;
  });
  if (!rows.length) {
    const row = element("tr");
    const cell = element("td", "quiet", "Waiting for plan inputs…");
    cell.colSpan = 4;
    row.append(cell);
    rows.push(row);
  }
  $("inputTable").replaceChildren(...rows);
}

function renderForecastAndTariff() {
  const forecast = state.snapshot?.forecast || {};
  setChip("forecastStatus", title(forecast.status || "unavailable"), forecast.status);
  setText(
    "forecastAge",
    forecast.age_seconds === null || forecast.age_seconds === undefined
      ? "No provider data"
      : `Updated ${formatAge(forecast.age_seconds)} ago`,
  );
  const comparison = forecast.actual_comparison;
  setText("forecastNow", formatPower(comparison?.forecast_power_w));
  setText("actualNow", formatPower(comparison?.actual_power_w));
  setText("forecastError", formatPower(comparison?.error_w, true));
  setText("forecastScope", forecast.comparison_scope || "Combined configured PV planes.");
  const correction = forecast.correction || {};
  setText(
    "forecastCorrection",
    correction.method
      ? `${title(correction.quality)} · long ${Number(correction.long_term_factor || 1).toFixed(3)} (${correction.long_term_days || 0}/${correction.long_term_required_days || 14} days) · short residual ${Number(correction.short_term_factor || 1).toFixed(3)} (${correction.short_term_samples || 0} samples) · weather ${correction.weather_available ? "active" : "unavailable"} · control gate ${correction.control_ready ? "passed" : "not passed"}.`
      : "Correction model is waiting for forecast history.",
  );
  const error = Math.abs(number(comparison?.error_w) || 0);
  setChip(
    "forecastQuality",
    comparison ? (error < 1000 ? "Tracking" : "Wide error") : "No comparison",
    comparison && error < 1000 ? "ok" : "degraded",
  );

  const quote = state.snapshot?.tariff?.quote || {};
  setText("importPrice", formatPrice(quote.import_price_per_kwh));
  setText("exportPrice", formatPrice(quote.export_price_per_kwh));
  setChip("tariffPeriod", title(quote.import_period || "unavailable"), quote.import_period ? "ok" : "degraded");
  const details = [];
  if (quote.zerohero_window) details.push("ZEROHERO import window active");
  if (quote.export_period) details.push(`Export: ${title(quote.export_period)}`);
  if (quote.exceptional_event) details.push(`Event: ${quote.exceptional_event}`);
  setText("tariffDetail", details.join(" · ") || state.snapshot?.tariff?.plan_id || "Waiting for the local tariff model.");
}

function renderDevicesAndEvents() {
  const devices = state.snapshot?.devices || [];
  const cards = devices.map((device) => {
    const card = element("article", "device-card");
    const header = element("header");
    header.append(
      element("h3", "", device.model || device.id),
      element("span", `quality-chip${device.health?.status && device.health.status !== "ok" ? ` quality-chip--${device.health.status}` : ""}`, title(device.health?.status || "unknown")),
    );
    const authority = (device.authoritative_for || []).length
      ? `Authoritative: ${device.authoritative_for.join(", ")}`
      : `Supplements: ${(device.supplements || []).join(", ") || "diagnostics"}`;
    card.append(
      header,
      element("p", "", `${title(device.type)} · ${title(device.access_mode)}`),
      element("p", "", authority),
      element("p", "", `Read-only · ${(device.capabilities || []).length} capabilities`),
    );
    return card;
  });
  if (!cards.length) cards.push(element("p", "quiet", "No enabled devices reported."));
  $("deviceGrid").replaceChildren(...cards);

  const events = state.snapshot?.events || [];
  const rows = events.map((event) => {
    const row = element("div", "event-row");
    row.append(
      element("time", "", formatTime(event.occurred_at, true)),
      element("span", `event-severity event-severity--${event.severity}`, event.severity),
      element("span", "", event.component),
      element("span", "", event.message),
    );
    return row;
  });
  if (!rows.length) rows.push(element("p", "quiet", "No diagnostic events recorded."));
  $("eventList").replaceChildren(...rows);
}

function renderSchedule() {
  const plan = state.snapshot?.plan;
  const recommendations = (plan?.recommendations || []).filter(
    (item) => new Date(item.timestamp).getTime() >= Date.now() - 15 * 60e3,
  );
  setText("scheduleCount", `${recommendations.length} intervals`);
  const rows = recommendations.slice(0, 12).map((item) => {
    const row = element("div", "schedule-row");
    row.append(
      element("time", "", formatTime(item.timestamp)),
      element("strong", `schedule-action schedule-action--${item.action}`, title(item.action)),
      element("span", "", `${formatPower(Math.abs(item.battery_power_w))} battery`),
      element("span", "", `${formatPower(item.expected_grid_power_w, true)} grid`),
      element("span", "", formatPercent(item.expected_soc_percent)),
      element("span", "", `${formatPrice(item.import_price_per_kwh)} / ${formatPrice(item.export_price_per_kwh)}`),
    );
    return row;
  });
  if (!rows.length) rows.push(element("p", "quiet", plan?.reason || "No scheduled recommendations."));
  $("schedule").replaceChildren(...rows);

  const futureSeries = [
    ["forecast_load_w", "Consumption", "#b86242"],
    ["forecast_pv_w", "PV", "#d9a629"],
    ["forecast_base_pv_w", "Provider PV", "#bd9d49", "4 4"],
    ["expected_grid_power_w", "Expected grid", "#76557f"],
    ["baseline_grid_power_w", "Baseline", "#9ba39e", "5 5"],
  ].map(([key, label, color, dash]) => ({
    label,
    color,
    dash,
    points: recommendations.map((item) => ({
      timestamp: item.timestamp,
      value: number(item[key]),
    })).filter((item) => item.value !== null),
  }));
  const socSeries = [{
    label: "SOC",
    color: "#7a638c",
    points: recommendations.map((item) => ({
      timestamp: item.timestamp,
      value: number(item.expected_soc_percent),
    })).filter((item) => item.value !== null),
  }, {
    label: "Native baseline SOC",
    color: "#9ba39e",
    dash: "5 5",
    points: recommendations.map((item) => ({
      timestamp: item.timestamp,
      value: number(item.baseline_expected_soc_percent),
    })).filter((item) => item.value !== null),
  }];
  renderChart("futureChart", futureSeries, { includeZero: true });
  renderChart("futureSocChart", socSeries, { percent: true });
  $("futureEmpty").hidden = recommendations.length > 0;
  setText(
    "futureSocEnd",
    recommendations.length ? formatPercent(recommendations.at(-1).expected_soc_percent) : "—",
  );

  const tariffPoints = state.historical.tariff || [];
  renderChart("tariffFutureChart", [
    {
      label: "Import",
      color: "#b86242",
      points: tariffPoints.map((item) => ({
        timestamp: item.timestamp,
        value: number(item.import_price_per_kwh),
      })).filter((item) => item.value !== null),
    },
    {
      label: "Export",
      color: "#3e8b68",
      points: tariffPoints.map((item) => ({
        timestamp: item.timestamp,
        value: number(item.export_price_per_kwh),
      })).filter((item) => item.value !== null),
    },
  ], { price: true, includeZero: true });
}

function renderWeather() {
  const weather = state.snapshot?.weather || {};
  const points = weather.points || [];
  const now = Date.now();
  const future = points.filter((item) => new Date(item.timestamp).getTime() >= now - 3600e3);
  const current = future[0] || null;
  setText("weatherStatus", weather.status ? `${title(weather.status)} · ${formatAge(weather.age_seconds)} old` : "Unavailable");
  setText("weatherCloud", current ? formatPercent(current.cloud_cover_percent) : "—");
  setText("weatherTemperature", current ? `${Number(current.temperature_c).toFixed(1)} °C` : "—");
  setText("weatherPvPotential", current ? formatPower(current.pv_potential_w) : "—");
  renderChart("weatherChart", [
    {
      label: "Cloud cover",
      color: "#587c95",
      points: future.map((item) => ({ timestamp: item.timestamp, value: number(item.cloud_cover_percent) })).filter((item) => item.value !== null),
    },
    {
      label: "Precipitation probability",
      color: "#76557f",
      points: future.map((item) => ({ timestamp: item.timestamp, value: number(item.precipitation_probability_percent) })).filter((item) => item.value !== null),
    },
  ], { percent: true });
  const forecast = state.snapshot?.forecast?.points || [];
  renderChart("weatherRadiationChart", [
    {
      label: "Weather PV potential",
      color: "#587c95",
      points: future.map((item) => ({ timestamp: item.timestamp, value: number(item.pv_potential_w) })).filter((item) => item.value !== null),
    },
    {
      label: "Corrected PV forecast",
      color: "#d9a629",
      points: forecast.map((item) => ({ timestamp: item.timestamp, value: number(item.power_w) })).filter((item) => item.value !== null),
    },
  ], { includeZero: true });
}

function svgNode(tag, attributes = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  return node;
}

function renderChart(id, series, options = {}) {
  const svg = $(id);
  svg.replaceChildren();
  const active = series.filter((item) => item.points.length);
  const all = active.flatMap((item) => item.points);
  if (!all.length) return;

  const width = 900;
  const height = id.toLowerCase().includes("soc") ? 130 : 300;
  const margin = { left: 54, right: 14, top: 12, bottom: 27 };
  const xs = all.map((point) => new Date(point.timestamp).getTime()).filter(Number.isFinite);
  const ys = all.map((point) => point.value).filter(Number.isFinite);
  let minX = Math.min(...xs);
  let maxX = Math.max(...xs);
  let minY = options.percent ? 0 : Math.min(...ys);
  let maxY = options.percent ? 100 : Math.max(...ys);
  if (options.includeZero) {
    minY = Math.min(0, minY);
    maxY = Math.max(0, maxY);
  }
  if (minX === maxX) maxX += 1;
  if (minY === maxY) {
    minY -= Math.max(1, Math.abs(minY) * 0.1);
    maxY += Math.max(1, Math.abs(maxY) * 0.1);
  }
  const pad = options.percent ? 0 : (maxY - minY) * 0.08;
  minY -= pad;
  maxY += pad;
  const x = (value) => margin.left + ((value - minX) / (maxX - minX)) * (width - margin.left - margin.right);
  const y = (value) => margin.top + ((maxY - value) / (maxY - minY)) * (height - margin.top - margin.bottom);

  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  for (let index = 0; index <= 4; index += 1) {
    const value = minY + ((maxY - minY) * index) / 4;
    const py = y(value);
    svg.append(svgNode("line", { x1: margin.left, y1: py, x2: width - margin.right, y2: py, class: "chart-grid" }));
    const label = svgNode("text", { x: margin.left - 8, y: py + 3, "text-anchor": "end", class: "chart-label" });
    label.textContent = options.percent
      ? `${Math.round(value)}%`
      : options.currency
        ? `$${value.toFixed(2)}`
        : options.price
          ? `${(value * 100).toFixed(0)}c`
          : `${(value / 1000).toFixed(1)}k`;
    svg.append(label);
  }
  if (minY < 0 && maxY > 0) {
    svg.append(svgNode("line", { x1: margin.left, y1: y(0), x2: width - margin.right, y2: y(0), class: "chart-zero" }));
  }
  for (let index = 0; index <= 4; index += 1) {
    const instant = minX + ((maxX - minX) * index) / 4;
    const label = svgNode("text", { x: x(instant), y: height - 7, "text-anchor": index === 0 ? "start" : index === 4 ? "end" : "middle", class: "chart-label" });
    label.textContent = formatTime(new Date(instant).toISOString(), maxX - minX > 36 * 3600e3);
    svg.append(label);
  }
  for (const item of active) {
    const ordered = [...item.points].sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));
    const path = ordered.map((point, index) => `${index ? "L" : "M"}${x(new Date(point.timestamp).getTime()).toFixed(2)},${y(point.value).toFixed(2)}`).join(" ");
    svg.append(svgNode("path", {
      d: path,
      class: "chart-path",
      stroke: item.color,
      ...(item.dash ? { "stroke-dasharray": item.dash } : {}),
    }));
  }
}

function renderSpark(id, values) {
  const node = $(id);
  node.replaceChildren();
  if (values.length < 2) return;
  const svg = svgNode("svg", { viewBox: "0 0 180 38", preserveAspectRatio: "none" });
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const path = values.map((value, index) => {
    const x = (index / (values.length - 1)) * 180;
    const y = 35 - ((value - min) / span) * 31;
    return `${index ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  svg.append(svgNode("path", { d: path }));
  node.append(svg);
}

async function loadHistory() {
  const range = ranges[state.range];
  const since = new Date(Date.now() - range.milliseconds).toISOString();
  const measurementPromise = Promise.all(powerSeries.concat([["battery.soc", "SOC", "#7a638c"]]).map(
    async ([name, label, color]) => {
      const query = new URLSearchParams({
        name,
        since,
        bucket_seconds: String(range.bucket),
        limit: "2000",
      });
      const payload = await api(`/measurements/history?${query}`);
      return {
        label,
        color,
        points: (payload.measurements || []).map((item) => ({
          timestamp: item.observed_at,
          value: number(item.value),
        })).filter((item) => item.value !== null),
      };
    },
  ));
  const common = new URLSearchParams({ since, limit: "2000" });
  const financialQuery = new URLSearchParams({
    since,
    bucket_seconds: String(Math.max(3600, range.bucket)),
    limit: "2000",
  });
  const [results, financialPayload, forecastPayload, planPayload, tariffPayload] = await Promise.all([
    measurementPromise,
    api(`/financials/history?${financialQuery}`),
    api(`/forecasts/pv?${common}`).catch(() => ({ historical_comparison: [] })),
    api(`/plans/history?${common}`),
    api("/tariffs/forecast?hours=48&step_minutes=15").catch(() => ({ points: [] })),
  ]);
  state.historical.financials = financialPayload.financials || [];
  state.historical.forecastComparison = forecastPayload.historical_comparison || [];
  state.historical.forecastBaseComparison = forecastPayload.historical_base_comparison || [];
  state.historical.plans = planPayload.plans || [];
  state.historical.tariff = tariffPayload.points || [];
  renderChart("historyChart", results.slice(0, 4), { includeZero: true });
  renderChart("historySocChart", [results[4]], { percent: true });
  $("historyEmpty").hidden = results.slice(0, 4).some((item) => item.points.length);
  const latestSoc = [...results[4].points].sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp))[0];
  setText("historySocLatest", latestSoc ? `${formatPercent(latestSoc.value)} latest` : "—");
  renderHistoricalInsights();
  renderSchedule();
}

function renderHistoricalInsights() {
  const financials = state.historical.financials || [];
  renderChart("costHistoryChart", [
    {
      label: "Import cost",
      color: "#b86242",
      points: financials.map((item) => ({
        timestamp: item.period_start,
        value: number(item.import_cost),
      })).filter((item) => item.value !== null),
    },
    {
      label: "Export revenue",
      color: "#3e8b68",
      points: financials.map((item) => ({
        timestamp: item.period_start,
        value: -(number(item.export_credit) || 0),
      })),
    },
    {
      label: "Net cost",
      color: "#76557f",
      points: financials.map((item) => ({
        timestamp: item.period_start,
        value: number(item.net_cost),
      })).filter((item) => item.value !== null),
    },
  ], { currency: true, includeZero: true });

  const today = state.snapshot?.financials?.today;
  const month = state.snapshot?.financials?.month;
  setText("costToday", formatNetCost(today?.net_cost));
  setText("costMonth", formatNetCost(month?.net_cost));
  setText(
    "monthEnergy",
    month
      ? `${Number(month.imported_kwh || 0).toFixed(1)} / ${Number(month.exported_kwh || 0).toFixed(1)} kWh`
      : "—",
  );

  const comparison = state.historical.forecastComparison || [];
  const baseComparison = state.historical.forecastBaseComparison || [];
  const actualComparison = comparison.length ? comparison : baseComparison;
  renderChart("accuracyHistoryChart", [
    {
      label: "Corrected PV",
      color: "#d9a629",
      points: comparison.map((item) => ({
        timestamp: item.forecast_at,
        value: number(item.forecast_power_w),
      })).filter((item) => item.value !== null),
    },
    {
      label: "Provider PV",
      color: "#bd9d49",
      dash: "4 4",
      points: baseComparison.map((item) => ({
        timestamp: item.forecast_at,
        value: number(item.forecast_power_w),
      })).filter((item) => item.value !== null),
    },
    {
      label: "Actual PV",
      color: "#3e8b68",
      points: actualComparison.map((item) => ({
        timestamp: item.forecast_at,
        value: number(item.actual_power_w),
      })).filter((item) => item.value !== null),
    },
  ], { includeZero: true });
  const errors = actualComparison.map((item) => Math.abs(number(item.error_w))).filter(Number.isFinite);
  setText(
    "accuracyHistorySummary",
    errors.length
      ? `${formatPower(errors.reduce((sum, value) => sum + value, 0) / errors.length)} mean absolute error`
      : "No matched history",
  );

  const plans = state.historical.plans || [];
  setText("planHistoryCount", `${plans.length} decisions`);
  renderChart("planCostHistoryChart", [
    {
      label: "Native baseline cost",
      color: "#9ba39e",
      dash: "5 5",
      points: plans.map((item) => ({
        timestamp: item.generated_at,
        value: number(item.baseline_cost),
      })).filter((item) => item.value !== null),
    },
    {
      label: "Recommended-plan cost",
      color: "#76557f",
      points: plans.map((item) => ({
        timestamp: item.generated_at,
        value: number(item.optimized_cost),
      })).filter((item) => item.value !== null),
    },
  ], { currency: true, includeZero: true });
  const planRows = plans.slice(0, 12).map((plan) => {
    const row = element("div", "schedule-row");
    row.append(
      element("time", "", formatTime(plan.generated_at, true)),
      element("strong", "", title(plan.status)),
      element("span", "", `${plan.recommendation_count} intervals`),
      element("span", "", plan.reason || "Plan generated"),
      element("span", "", `Δ ${formatMoney(plan.estimated_cost_improvement)}`),
    );
    return row;
  });
  if (!planRows.length) {
    planRows.push(element("p", "quiet", "No persisted optimiser decisions in this range."));
  }
  $("planHistory").replaceChildren(...planRows);
}

function renderAll() {
  renderCurrent();
  renderRecommendation();
  renderHealth();
  renderWorkings();
  renderForecastAndTariff();
  renderWeather();
  renderDevicesAndEvents();
  renderSchedule();
  const stamp = state.snapshot?.plant?.timestamp || state.snapshot?.health?.timestamp;
  setText("lastUpdated", stamp ? `Updated ${formatTime(stamp)}` : "Not updated");
}

async function refresh() {
  setText("refreshState", "Refreshing…");
  try {
    state.snapshot = await api("/diagnostics");
    renderAll();
    setText("refreshState", "Live · 5 second refresh");
    return true;
  } catch (error) {
    if (error.message !== "Authentication required") toast(`Diagnostics refresh failed: ${error.message}`);
    setText("refreshState", "Connection interrupted");
    return false;
  }
}

async function beginDataPolling(pollSeconds = 5) {
  if (!await refresh()) return false;
  try {
    await loadHistory();
  } catch (error) {
    toast(`History could not be loaded: ${error.message}`);
  }
  if (!state.refreshTimer) {
    state.refreshTimer = window.setInterval(
      refresh,
      Math.max(2, pollSeconds) * 1000,
    );
    state.historyTimer = window.setInterval(
      () => loadHistory().catch(() => {}),
      60000,
    );
  }
  return true;
}

function wireInteractions() {
  $("authForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    state.token = $("authToken").value.trim();
    sessionStorage.setItem("fasttalkDiagnosticsToken", state.token);
    setText("authError", "");
    if (await beginDataPolling()) {
      $("authDialog").close();
    } else {
      showAuthentication("That token was not accepted.");
    }
  });
  $("authButton").addEventListener("click", () => {
    $("authToken").value = "";
    showAuthentication();
  });
  document.querySelectorAll("[data-range]").forEach((button) => {
    button.addEventListener("click", async () => {
      state.range = button.dataset.range;
      document.querySelectorAll("[data-range]").forEach((item) => item.classList.toggle("is-active", item === button));
      try {
        await loadHistory();
      } catch (error) {
        toast(`History could not be loaded: ${error.message}`);
      }
    });
  });
  const observer = new IntersectionObserver((entries) => {
    const visible = entries.filter((entry) => entry.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
    if (!visible) return;
    document.querySelectorAll(".nav-link").forEach((link) => {
      link.classList.toggle("is-active", link.getAttribute("href") === `#${visible.target.id}`);
    });
  }, { rootMargin: "-20% 0px -65%", threshold: [0, 0.2, 0.5] });
  document.querySelectorAll(".section-anchor").forEach((section) => observer.observe(section));
}

async function start() {
  updateClock();
  window.setInterval(updateClock, 1000);
  wireInteractions();
  try {
    const response = await fetch("config.json", { cache: "no-store" });
    const config = await response.json();
    state.apiBase = config.api_base || "/v1";
    state.authRequired = Boolean(config.authentication_required);
    $("authButton").hidden = !state.authRequired;
    if (state.authRequired && !state.token) {
      showAuthentication();
      return;
    }
    await beginDataPolling(config.poll_seconds || 5);
  } catch (error) {
    setText("refreshState", "Daemon unavailable");
    toast(`Could not start diagnostics: ${error.message}`);
  }
}

start();
