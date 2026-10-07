"use strict";

const MIN_POINTS = 3;
const $ = (sel) => document.querySelector(sel);
const tbPoints = $("#pointsTable tbody");
const errBox = $("#diagError");

let records = [];
let selectedId = null;
let numPoints = MIN_POINTS;

/* 异步裁决状态：
 * - metaSeq：详情请求序号，仅最新一次详情回包可更新电压上限；
 * - diagSeq：诊断请求序号，仅最新一次诊断回包可更新结论/错误；
 * - displayedKey：当前页面所显示结论/错误对应的提交签名（记录 +
 *   实际提交的测点与容差），输入一旦偏离该签名，旧结论与旧错误立即撤下。 */
let metaSeq = 0;
let diagSeq = 0;
let displayedKey = null;

const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const fmt = (x, d = 4) =>
  (x === null || x === undefined) ? "—"
    : Number(x).toFixed(d).replace(/\.?0+$/, "");

function showError(msg) {
  errBox.textContent = "✗ " + msg;
  errBox.hidden = false;
}
function clearError() { errBox.hidden = true; }

/* 本地校验/批量导入错误：与当前输入绑定，输入一改即被撤下。 */
function showLocalError(msg) {
  $("#diagResultCard").hidden = true;
  displayedKey = currentInputKey();
  showError(msg);
}

/* ---------------- 记录选择 ---------------- */

async function loadRecords(prefillId) {
  const res = await fetch("/api/trials");
  const data = await res.json();
  records = data.trials || [];
  const sel = $("#recordSelect");
  sel.innerHTML = "";
  if (!records.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "（暂无试算记录，请先在试算台保存一条记录）";
    sel.appendChild(opt);
    $("#diagnoseBtn").disabled = true;
    $("#recordMeta").textContent = "";
    return;
  }
  records.forEach((t) => {
    const opt = document.createElement("option");
    opt.value = t.id;
    opt.textContent = `#${t.id} ${t.name}（${t.num_groups} 组 / ` +
      `${t.num_baseline_strings} 串，${t.created_at.replace("T", " ").replace("+00:00", "")}）`;
    sel.appendChild(opt);
  });
  const target = prefillId && records.some((t) => String(t.id) === String(prefillId))
    ? String(prefillId) : String(records[0].id);
  sel.value = target;
  selectedId = Number(target);
  fetchRecordMeta(selectedId);
}

async function fetchRecordMeta(id) {
  const seq = ++metaSeq;
  selectedId = Number(id);
  // 记录一切换，旧结论、旧错误与旧电压上限立即失效
  $("#diagResultCard").hidden = true;
  clearError();
  displayedKey = null;
  $("#recordMeta").textContent = "";
  const isCurrent = () => seq === metaSeq && selectedId === Number(id);
  let rec;
  try {
    const res = await fetch(`/api/trials/${id}`);
    if (!isCurrent()) return;  // 迟到回包：所属记录已非当前选中，丢弃
    if (!res.ok) { $("#recordMeta").textContent = "记录不存在"; return; }
    rec = await res.json();
  } catch (e) {
    if (isCurrent()) $("#recordMeta").textContent = "记录详情加载失败：" + e.message;
    return;
  }
  if (!isCurrent()) return;  // 解析期间又切换了记录，丢弃
  const voc = rec.result?.baseline?.voc;
  $("#recordMeta").textContent = voc !== undefined
    ? `基线开路电压（电压上限）：${fmt(voc, 3)} V`
    : "";
}

/* ---------------- 测点录入 ---------------- */

function renderPointRows() {
  const prev = collectPointsRaw();
  tbPoints.innerHTML = "";
  for (let i = 0; i < numPoints; i++) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td class="num">${i + 1}</td>` +
      `<td><input class="p-v" type="text" inputmode="decimal" placeholder="0.00～基线 Voc"></td>` +
      `<td><input class="p-i" type="text" inputmode="decimal" placeholder="≥ 0"></td>`;
    tbPoints.appendChild(tr);
    if (prev[i]) {
      tr.querySelector(".p-v").value = prev[i].v;
      tr.querySelector(".p-i").value = prev[i].i;
    }
  }
}

function collectPointsRaw() {
  return [...tbPoints.querySelectorAll("tr")].map((tr) => ({
    v: tr.querySelector(".p-v").value.trim(),
    i: tr.querySelector(".p-i").value.trim(),
  }));
}

/* 当前提交签名：选中的记录 + 容差原文 + 实际会被提交的测点原文
 * （批量框非空时以批量框为准，否则以表格为准，与 gatherBody 的取源一致）。 */
function currentInputKey() {
  const bulk = $("#bulkPoints").value;
  return JSON.stringify({
    id: selectedId,
    tol: $("#tolerance").value,
    src: bulk.trim() !== "" ? bulk : collectPointsRaw(),
  });
}

/* 输入变化后调用：当前输入一旦偏离已显示结论/错误所属的提交，
 * 立即撤下旧结论与旧错误（在途回包由提交时捕获的签名另行裁决丢弃）。 */
function dropStaleOutcome() {
  if (displayedKey !== null && displayedKey !== currentInputKey()) {
    $("#diagResultCard").hidden = true;
    clearError();
    displayedKey = null;
  }
}

/* 批量文本：每行一个测点，支持 "V, I"（逗号/空格/Tab/分号分隔）或 JSON 对象。
 * 返回 [{v, i}]（原始字符串，交由 gatherBody 统一做数值校验）。 */
function parseBulkPoints(text) {
  const out = [];
  const lines = text.split(/\r?\n/);
  lines.forEach((raw, idx) => {
    const line = raw.trim();
    if (!line) return;
    let v = null, i = null;
    if (line.startsWith("{")) {
      let obj;
      try { obj = JSON.parse(line); }
      catch (e) { throw new Error(`批量测点第 ${idx + 1} 行不是合法的 JSON：${line}`); }
      if (obj.v === undefined || obj.i === undefined) {
        throw new Error(`批量测点第 ${idx + 1} 行必须包含 v 和 i：${line}`);
      }
      v = String(obj.v); i = String(obj.i);
    } else {
      const parts = line.split(/[,\t;，；\s]+/).filter((s) => s !== "");
      if (parts.length !== 2) {
        throw new Error(`批量测点第 ${idx + 1} 行应为 “V, I” 两个数：${line}`);
      }
      [v, i] = parts;
    }
    out.push({ v, i });
  });
  return out;
}

function importBulkPoints() {
  const text = $("#bulkPoints").value;
  let parsed;
  try { parsed = parseBulkPoints(text); }
  catch (e) { showLocalError(e.message); return; }
  clearError();
  if (parsed.length < MIN_POINTS) {
    showLocalError(`批量测点至少需要 ${MIN_POINTS} 个，当前仅 ${parsed.length} 个`);
    return;
  }
  numPoints = parsed.length;
  renderPointRows();
  tbPoints.querySelectorAll("tr").forEach((tr, idx) => {
    tr.querySelector(".p-v").value = parsed[idx].v;
    tr.querySelector(".p-i").value = parsed[idx].i;
  });
  $("#bulkMeta").textContent = `已导入 ${parsed.length} 个测点`;
}

function gatherBody() {
  const tolRaw = $("#tolerance").value.trim();
  if (tolRaw === "" || Number.isNaN(Number(tolRaw))) {
    throw new Error("电流容差必须是有限非负数");
  }
  // 批量框非空时直接以其内容为准，免去先点“导入表格”
  const rawPoints = $("#bulkPoints").value.trim()
    ? parseBulkPoints($("#bulkPoints").value)
    : collectPointsRaw();
  const points = rawPoints.map((p, idx) => {
    if (p.v === "" || p.i === "" || Number.isNaN(Number(p.v)) || Number.isNaN(Number(p.i))) {
      throw new Error(`第 ${idx + 1} 个测点的电压/电流必须是有限数值`);
    }
    return { v: Number(p.v), i: Number(p.i) };
  });
  return { tolerance: Number(tolRaw), points };
}

/* ---------------- 提交与渲染 ---------------- */

async function submitDiagnosis() {
  clearError();
  if (!selectedId) { showLocalError("请先选择一条试算记录"); return; }
  let body;
  try { body = gatherBody(); }
  catch (e) { showLocalError(e.message); return; }

  // 捕获发起时的记录、实际提交的测点与容差（签名）及请求序号；
  // 回包时三者共同裁决：只有仍对应当前输入的最新请求可更新页面。
  const reqId = selectedId;
  const reqKey = currentInputKey();
  const seq = ++diagSeq;
  const isCurrent = () =>
    seq === diagSeq && selectedId === reqId && currentInputKey() === reqKey;

  let res, data;
  try {
    res = await fetch(`/api/trials/${reqId}/diagnose`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    data = await res.json();
  } catch (e) {
    // 有效（仍属当前输入的最新）请求失败：显示错误；失效回包静默丢弃
    if (isCurrent()) {
      $("#diagResultCard").hidden = true;
      displayedKey = reqKey;
      showError("网络/服务错误：" + e.message);
    }
    return;
  }
  if (!isCurrent()) return;  // 迟到/失效回包：记录已切换或输入已改，丢弃
  displayedKey = reqKey;
  if (res.status === 404) {
    $("#diagResultCard").hidden = true;
    showError("试算记录不存在");
    return;
  }
  if (!res.ok) {
    $("#diagResultCard").hidden = true;
    showError(data.detail || "诊断失败");
    return;
  }
  clearError();
  renderDiagnosis(data.diagnosis);
}

const VERDICT_STYLE = {
  unconfirmed: ["verdict-neutral", "无法确认异常"],
  single_group: ["verdict-single", "定位到单组退化"],
  ambiguous: ["verdict-ambiguous", "多组歧义"],
  out_of_model: ["verdict-out", "模型外偏差"],
};

function renderDiagnosis(d) {
  $("#diagResultCard").hidden = false;
  const box = $("#verdictBox");
  const [cls, title] = VERDICT_STYLE[d.verdict] || ["", d.verdict];
  box.className = "verdict " + cls;
  box.innerHTML = `<div class="verdict-title">${title}</div>` +
    `<div class="verdict-msg">${escapeHtml(d.message)}</div>` +
    `<div class="verdict-sub">健康假设可解释：${d.healthy_explainable ? "是" : "否"}
       ｜ 电流容差：${fmt(d.tolerance, 3)} A
       ｜ 基线开路电压：${fmt(d.baseline_voc, 3)} V</div>`;

  const tbP = $("#diagPointsTable tbody");
  tbP.innerHTML = "";
  d.points.forEach((p, i) => {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td class="num">${i + 1}</td>` +
      `<td class="num">${fmt(p.v)}</td>` +
      `<td class="num">${fmt(p.i_measured)}</td>` +
      `<td class="num">${fmt(p.i_theory_healthy)}</td>` +
      `<td class="num ${p.deviation > 0 ? "dev-pos" : p.deviation < 0 ? "dev-neg" : ""}">` +
      `${p.deviation > 0 ? "+" : ""}${fmt(p.deviation)}</td>` +
      `<td>${p.within_tolerance ? '<span class="tag-ok">是</span>'
        : '<span class="tag-no">否</span>'}</td>`;
    tbP.appendChild(tr);
  });

  const tbG = $("#diagGroupsTable tbody");
  tbG.innerHTML = "";
  d.groups.forEach((g) => {
    let range = "—", count = "—";
    if (g.feasible) {
      range = g.isc_min === g.isc_max
        ? `${fmt(g.isc_min, 2)}`
        : `${fmt(g.isc_min, 2)} ～ ${fmt(g.isc_max, 2)}`;
      count = g.isc_candidates;
    }
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td><b>${escapeHtml(g.group)}</b></td>` +
      `<td class="num">${fmt(g.isc_original, 2)}</td>` +
      `<td class="num">${g.sensitive_points}</td>` +
      `<td>${g.feasible ? '<span class="tag-ok">可解释</span>'
        : '<span class="tag-no">不可解释</span>'}</td>` +
      `<td class="num">${range}</td>` +
      `<td class="num">${count}</td>`;
    tbG.appendChild(tr);
  });
  $("#diagResultCard").scrollIntoView?.({ behavior: "smooth" });
}

/* ---------------- 事件与初始化 ---------------- */

$("#recordSelect").addEventListener("change", (e) => fetchRecordMeta(e.target.value));
$("#addPointBtn").addEventListener("click", () => {
  numPoints++; renderPointRows(); dropStaleOutcome();
});
$("#removePointBtn").addEventListener("click", () => {
  if (numPoints <= MIN_POINTS) return;
  numPoints--; renderPointRows(); dropStaleOutcome();
});
$("#diagnoseBtn").addEventListener("click", submitDiagnosis);
$("#bulkLoadBtn").addEventListener("click", importBulkPoints);

// 当前提交源（容差 / 批量框 / 表格测点）的任何编辑，
// 立即使旧结论与旧错误失效（批量框非空时表格编辑不影响提交源，不会误清）
$("#tolerance").addEventListener("input", dropStaleOutcome);
$("#bulkPoints").addEventListener("input", dropStaleOutcome);
$("#pointsTable").addEventListener("input", dropStaleOutcome);

renderPointRows();
const prefill = new URLSearchParams(location.search).get("id");
loadRecords(prefill);
