/* 馬路三寶檢舉輔助系統 — 前端（原生 JS，不需要建置工具） */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ic = (name, cls = "") => `<svg class="ic ${cls}"><use href="#i-${name}"/></svg>`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const state = {
  step: 1, maxStep: 1,
  video: null,
  gps: { mode: "preset", lat: null, lon: null, track: null, geo: null },
  job: null, pollTimer: null, logCount: 0, foundCount: 0, jobStartedAt: 0,
  ignored: new Set(), filter: "all",
  selected: null, caseObj: null, user: null,
  presets: [], cities: [], profile: {},
  draft: null, saveTimer: null, restoring: false,
  mode: "remote", health: null,   // 分析方式：remote＝真實模型（推論服務）、simulated＝模擬展示
  forms: {},        // 每筆違規在檢舉單頁修改過的欄位 {違規id: 欄位}
  cases: {},        // 每筆違規產生的案件 {違規id: 案件id}
  caseStatus: {},   // {案件id: draft|submitted}
};

/* ============================ 草稿：自動儲存每一步的進度 ============================ */
const LS_KEY = "trafficReport.currentDraft";
const lsKey = () => LS_KEY + ":" + ((state.user && state.user.email) || "");  // 每個帳號各記各的
const ls = {
  get() { try { return localStorage.getItem(lsKey()); } catch (e) { return null; } },
  set(v) { try { v ? localStorage.setItem(lsKey(), v) : localStorage.removeItem(lsKey()); } catch (e) { /* 無法使用時只靠伺服器端草稿 */ } },
};
const STEP_NAMES = { 1: "上傳影片", 2: "AI 分析", 3: "違規審查", 4: "檢舉單", 5: "送出" };
const currentGpsTab = () => ($("#gps-tabs button.active") || {}).dataset?.tab || "preset";

function collectDraft() {
  return {
    step: state.step, max_step: state.maxStep, video: state.video, gps: state.gps,
    gps_tab: currentGpsTab(), preset_index: +$("#preset-select").value || 0,
    recorded_at: $("#recorded-at").value, speed: $("#speed").value,
    mode: state.mode, max_seconds: $("#max-seconds").value,
    job_id: state.job ? state.job.id : null, ignored: [...state.ignored], filter: state.filter,
    selected: state.selected ? state.selected.id : null, forms: state.forms, cases: state.cases,
  };
}
async function ensureDraft() {
  if (state.draft) return state.draft;
  const d = await postJSON("/api/drafts", collectDraft());
  state.draft = d; ls.set(d.id);
  showDraftBar(); setSaveState("saved", d.updated);
  return d;
}
function saveDraft() {
  if (state.restoring || !state.draft) return;
  setSaveState("saving");
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(async () => {
    try { const r = await postJSON(`/api/drafts/${state.draft.id}`, collectDraft()); setSaveState("saved", r.updated); }
    catch (e) { setSaveState("error"); }
  }, 350);
}
function setSaveState(kind, ts) {
  const el = $("#save-state"); el.className = `muted small ${kind}`;
  el.textContent = kind === "saving" ? "儲存中…" : kind === "error" ? "儲存失敗，請確認伺服器是否在執行"
    : `已自動儲存${ts ? " " + ts.slice(11, 19) : ""}・重新整理或關掉重開都會保留`;
}
function showDraftBar() {
  $("#draft-bar").classList.remove("hidden");
  $("#draft-name").textContent = state.video ? state.video.name : "新的檢舉";
  $("#resume-banner").classList.add("hidden");
}
function openDraft(id) { ls.set(id); location.href = `/?draft=${encodeURIComponent(id)}`; }
$("#btn-new-draft").addEventListener("click", () => { ls.set(null); location.href = "/?new=1"; });

/* 違規類型與流程階段的圖示／顏色 */
const TYPE_META = {
  red_light: { icon: "light", color: "red" },
  double_white: { icon: "ban", color: "violet" },
  no_signal: { icon: "turn", color: "blue" },
  red_line_parking: { icon: "parking", color: "red" },
  moto_lane: { icon: "moto", color: "amber" },
  double_yellow: { icon: "ban", color: "amber" },
  moto_waiting_area: { icon: "moto", color: "violet" },
  crosswalk_stopping: { icon: "user", color: "blue" },
  other: { icon: "alert", color: "red" },
};
const STAGE_ICON = {
  receive: "download", decode: "video", detect: "cpu", rules: "scale", clip: "scissors", package: "package",
  upload: "upload", loading: "cpu", analyzing: "scan", clipping: "scissors", packaging: "package",
};
const STAGE_WEIGHT = { receive: 0.05, decode: 0.15, detect: 0.45, rules: 0.15, clip: 0.15, package: 0.05 };
// 每個模型在哪些階段運作（模擬模式：detect/rules；真實模型：analyzing）
const MODELS = [
  { name: "車輛偵測 YOLO", icon: "car", stages: ["detect", "analyzing"] },
  { name: "車牌辨識 OCR", icon: "type", stages: ["detect", "analyzing"] },
  { name: "單目深度測距", icon: "ruler", stages: ["detect", "analyzing"] },
  { name: "號誌辨識", icon: "light", stages: ["detect", "analyzing"] },
  { name: "標線分割", icon: "layers", stages: ["detect", "analyzing"] },
  { name: "違規判斷引擎", icon: "scale", stages: ["rules", "analyzing"] },
];
/* 證據強度文字：真實模型用「條件 x/y」，模擬模式用百分比 */
const confText = (v) => (v.evidence_kind === "conditions"
  ? `條件 ${v.conditions_met}/${v.conditions_total}` : `信心 ${Math.round((v.confidence || 0) * 100)}%`);

async function api(path, opts = {}) {
  const t = tokenStore.get();
  const headers = Object.assign({}, opts.headers || {});
  if (t && !headers["X-Auth-Token"]) headers["X-Auth-Token"] = t;
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && !path.startsWith("/api/auth/") && appStarted) {  // 登入過期 → 回登入畫面
    tokenStore.set(null); location.href = "/";
  }
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}
const postJSON = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

function toast(msg, icon = "check") {
  const t = $("#toast"); t.innerHTML = `${ic(icon, "xs")}<span>${esc(msg)}</span>`; t.classList.add("show");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("show"), 2600);
}
const pad = (n) => String(n).padStart(2, "0");
const toLocalInput = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
function fmtDur(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `${Math.max(1, sec)} 秒`;
  const m = Math.floor(sec / 60), s = sec % 60;
  return m < 60 ? `${m} 分 ${s} 秒` : `${Math.floor(m / 60)} 小時 ${m % 60} 分`;
}
function fmtSec(s) { const m = Math.floor(s / 60); return `${pad(m)}:${(s - m * 60).toFixed(1).padStart(4, "0")}`; }

/* 數字滾動動畫 */
function animateNum(el, to) {
  const from = +el.dataset.v || 0;
  if (from === to) return;
  el.dataset.v = to;
  const card = el.closest(".stat"); if (card && to > from) { card.classList.remove("bump"); void card.offsetWidth; card.classList.add("bump"); }
  const t0 = performance.now(), dur = 500;
  const tick = (now) => {
    const r = Math.min(1, (now - t0) / dur), e = 1 - Math.pow(1 - r, 3);
    el.textContent = Math.round(from + (to - from) * e).toLocaleString();
    if (r < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

/* ============================ 導覽 ============================ */
function showView(name) {
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  $$(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === name));
  if (name === "cases") loadCases();
  if (name === "profile") loadProfile();
  if (name === "new" && state.step === 1) setTimeout(() => map && map.invalidateSize(), 60);
  window.scrollTo({ top: 0, behavior: "smooth" });
}
$$(".nav a").forEach((a) => a.addEventListener("click", () => showView(a.dataset.view)));
document.addEventListener("click", (e) => {
  const v = e.target.closest("[data-view-link]"); if (v) showView(v.dataset.viewLink);
  const g = e.target.closest("[data-goto]"); if (g) goStep(+g.dataset.goto);
});

function goStep(n) {
  if (n > state.maxStep) return;
  state.step = n;
  $$(".step").forEach((s) => s.classList.toggle("hidden", s.id !== `step-${n}`));
  $$("#stepper li").forEach((li) => {
    const k = +li.dataset.step;
    li.classList.toggle("current", k === n);
    li.classList.toggle("done", k !== n && k <= state.maxStep);
  });
  if (n === 1) setTimeout(() => map && map.invalidateSize(), 60);
  if (n === 3 && state.job && state.job.status === "done" && !state.restoring) refreshCaseStatus();
  window.scrollTo({ top: 0, behavior: "smooth" });
  saveDraft();
}
const unlock = (n) => { state.maxStep = Math.max(state.maxStep, n); };
$$("#stepper li").forEach((li) => li.addEventListener("click", () => goStep(+li.dataset.step)));

/* ============================ Step 1：上傳 ============================ */
const dz = $("#dropzone"), vInput = $("#video-input");
["dragover", "dragenter"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("over"); }));
dz.addEventListener("drop", (e) => { const f = e.dataTransfer.files[0]; if (f) uploadVideo(f); });
vInput.addEventListener("change", () => { if (vInput.files[0]) uploadVideo(vInput.files[0]); });

function uploadVideo(file) {
  const bar = $("#upload-progress");
  bar.classList.remove("hidden", "done");
  $("#btn-analyze").disabled = true;
  const xhr = new XMLHttpRequest();
  xhr.open("POST", `/api/upload?filename=${encodeURIComponent(file.name)}`);
  if (tokenStore.get()) xhr.setRequestHeader("X-Auth-Token", tokenStore.get());
  xhr.upload.onprogress = (e) => {
    if (!e.lengthComputable) return;
    const p = Math.round((e.loaded / e.total) * 100);
    bar.firstElementChild.style.width = p + "%";
    bar.lastElementChild.textContent = `上傳中 ${p}%`;
  };
  xhr.onload = async () => {
    if (xhr.status !== 200) { toast("上傳失敗", "alert"); return; }
    const data = JSON.parse(xhr.responseText);
    state.video = { ...data, name: file.name, lastModified: file.lastModified };
    bar.firstElementChild.style.width = "100%";
    bar.classList.add("done"); bar.lastElementChild.textContent = "上傳完成";
    setTimeout(() => bar.classList.add("hidden"), 1200);
    showVideo(state.video, true);
    refreshAnalyzeBtn();
    toast("影片上傳完成");
    try { await ensureDraft(); showDraftBar(); saveDraft(); } catch (e) { setSaveState("error"); }
  };
  xhr.onerror = () => toast("上傳失敗，請確認伺服器是否在執行", "alert");
  xhr.send(file);
}

function showVideo(video, fresh = false) {
  const v = $("#video-preview");
  v.src = video.url; $("#video-wrap").classList.remove("hidden");
  const size = video.size ? `${(video.size / 1e6).toFixed(1)} MB` : "";
  const chips = (arr) => { $("#video-info").innerHTML = arr.filter(Boolean).map((x) => `<span>${esc(x)}</span>`).join(""); };
  chips([video.name, size]);
  v.onloadedmetadata = () => {
    chips([video.name, size, `${v.videoWidth}×${v.videoHeight}`, fmtSec(v.duration)]);
    if (fresh && video.lastModified) {
      // 行車記錄器的檔案時間通常是「錄影結束」，往前推影片長度當作開始時間
      $("#recorded-at").value = toLocalInput(new Date(video.lastModified - (v.duration || 0) * 1000));
      saveDraft();
    }
  };
  v.onerror = () => chips([video.name, "瀏覽器無法預覽此格式，但仍可分析"]);
  dz.classList.add("compact");
  dz.querySelector(".dz-text").innerHTML = `已選擇 <b>${esc(video.name)}</b>　<u>重新選擇</u>`;
}

/* ---- GPS ---- */
let map = null, mapLayer = null;
function initMap() {
  if (!window.L) { $("#map").classList.add("offline"); renderOfflineMap(); return; }
  map = L.map("map", { zoomControl: true, attributionControl: false }).setView([23.7, 121], 7);
  L.tileLayer("https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png", { maxZoom: 19 }).addTo(map);
  map.on("click", (e) => {
    switchGpsTab("manual");
    $("#lat-input").value = e.latlng.lat.toFixed(6); $("#lon-input").value = e.latlng.lng.toFixed(6);
    setPoint(e.latlng.lat, e.latlng.lng, "manual");
  });
}
function renderOfflineMap() {
  if (map) return;
  const g = state.gps;
  $("#map").innerHTML = `<div class="map-offline"><div class="pin"><i></i></div>
    <div>${g.lat != null ? `<b style="color:var(--ink)">${g.lat.toFixed(5)}, ${g.lon.toFixed(5)}</b><br>` : ""}<span class="small">離線中，地圖需要網路才會顯示</span></div></div>`;
}
function drawMap() {
  if (!map) return renderOfflineMap();
  if (mapLayer) map.removeLayer(mapLayer);
  const g = state.gps;
  if (g.track && g.track.length > 1) {
    const pts = g.track.map((p) => [p.lat, p.lon]);
    mapLayer = L.layerGroup([L.polyline(pts, { color: "#5b7cff", weight: 5, opacity: .85 }), L.circleMarker(pts[0], { radius: 7, color: "#16b981", fillOpacity: 1 })]).addTo(map);
    map.fitBounds(pts, { padding: [24, 24] });
  } else if (g.lat != null) {
    mapLayer = L.circleMarker([g.lat, g.lon], { radius: 9, color: "#fff", weight: 3, fillColor: "#5b7cff", fillOpacity: 1 }).addTo(map);
    map.setView([g.lat, g.lon], 15);
  }
}
async function setPoint(lat, lon, mode) {
  state.gps = { mode, lat: +lat, lon: +lon, track: null, geo: null };
  try { state.gps.geo = await api(`/api/geocode?lat=${lat}&lon=${lon}`); } catch (e) { /* ignore */ }
  renderGpsInfo(); drawMap(); refreshAnalyzeBtn(); saveDraft();
}
function renderGpsInfo() {
  const g = state.gps, el = $("#gps-info");
  if (g.lat == null) { el.innerHTML = `<span class="muted">尚未設定位置</span>`; return; }
  const tag = g.geo ? `<span class="geo-tag">${ic("pin")}${esc(g.geo.city)}${esc(g.geo.district)}</span>` : "";
  const trackTxt = g.track && g.track.length > 1 ? `・軌跡 ${g.track.length} 點` : "";
  el.innerHTML = `${tag}<span class="muted small">${g.lat.toFixed(5)}, ${g.lon.toFixed(5)}${trackTxt}</span>`;
}
function switchGpsTab(tab) {
  $$("#gps-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $$(".tab-body").forEach((b) => b.classList.toggle("hidden", b.dataset.tab !== tab));
}
$$("#gps-tabs button").forEach((b) => b.addEventListener("click", () => {
  switchGpsTab(b.dataset.tab);
  if (b.dataset.tab === "preset") $("#preset-select").dispatchEvent(new Event("change"));
}));
$("#preset-select").addEventListener("change", (e) => { const p = state.presets[+e.target.value]; if (p) setPoint(p.lat, p.lon, "preset"); });
["#lat-input", "#lon-input"].forEach((s) => $(s).addEventListener("change", () => {
  const lat = parseFloat($("#lat-input").value), lon = parseFloat($("#lon-input").value);
  if (!isNaN(lat) && !isNaN(lon)) setPoint(lat, lon, "manual");
}));
$("#btn-locate").addEventListener("click", () => {
  if (!navigator.geolocation) return toast("此瀏覽器不支援定位", "alert");
  navigator.geolocation.getCurrentPosition((p) => {
    $("#lat-input").value = p.coords.latitude.toFixed(6); $("#lon-input").value = p.coords.longitude.toFixed(6);
    setPoint(p.coords.latitude, p.coords.longitude, "manual");
  }, () => toast("無法取得位置（瀏覽器可能拒絕了定位權限）", "alert"));
});
$("#gps-input").addEventListener("change", async (e) => {
  const f = e.target.files[0]; if (!f) return;
  try {
    const data = await api(`/api/gps/parse?filename=${encodeURIComponent(f.name)}`, { method: "POST", body: f });
    state.gps = { mode: "track", lat: data.track[0].lat, lon: data.track[0].lon, track: data.track, geo: data.geo };
    state.gps.file = f.name; $("#gps-file-name").textContent = f.name;
    renderGpsInfo(); drawMap(); refreshAnalyzeBtn(); saveDraft();
    toast(`讀到 ${data.count} 個 GPS 點`);
  } catch (err) { toast(err.message, "alert"); }
});
["#recorded-at", "#speed"].forEach((s) => $(s).addEventListener("change", saveDraft));
["#max-seconds"].forEach((s) => $(s).addEventListener("change", saveDraft));

/* ---- 分析方式：真實模型（推論服務）／模擬展示 ---- */
function setMode(mode, save = true) {
  state.mode = mode;
  $$("#mode-options .mode-opt").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  $$("[data-for]").forEach((el) => el.classList.toggle("hidden", el.dataset.for !== mode));
  const pill = $(".mode-pill");
  pill.classList.toggle("real", mode === "remote");
  pill.innerHTML = `<span class="dot"></span>${mode === "remote" ? "真實模型" : "模擬模式"}`;
  pill.title = mode === "remote" ? "使用推論服務跑真實模型" : "使用模擬分析器（假資料）";
  if (mode === "remote") checkHealth();
  refreshAnalyzeBtn();
  if (save) saveDraft();
}
$$("#mode-options .mode-opt").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));
async function checkHealth() {
  try { state.health = await api("/api/inference/health"); } catch (e) { state.health = { online: false, error: e.message }; }
  renderHealth(); refreshAnalyzeBtn();
}
function renderHealth() {
  const h = state.health, chip = $("#svc-status"), box = $("#svc-detail");
  if (!h) { chip.className = "svc-status"; chip.textContent = "檢查中…"; box.innerHTML = ""; return; }
  if (!h.online) {
    chip.className = "svc-status off"; chip.textContent = "推論服務未開啟";
    box.innerHTML = `${ic("alert", "xs")} 連不到 <code>${esc(h.url || "")}</code>。請先雙擊專題資料夾裡的 <b>啟動推論服務.cmd</b>，再按 <a class="link" id="btn-recheck">重新檢查</a>。`;
  } else {
    const gpu = h.gpu || {};
    const okModels = h.models_ready === h.models_total;
    chip.className = `svc-status ${okModels ? "on" : "warn"}`;
    chip.textContent = okModels ? (h.busy ? "推論服務忙碌中" : "推論服務已連線") : "缺少模型檔";
    const parts = [
      gpu.cuda ? `${ic("cpu", "xs")} GPU：${esc(gpu.name || "CUDA")}` : gpu.error || gpu.cuda === false ? `${ic("alert", "xs")} 沒有偵測到 GPU，會用 CPU 跑（很慢）` : `${ic("cpu", "xs")} GPU 偵測中…`,
      `${ic("layers", "xs")} 模型 ${h.models_ready}/${h.models_total}`,
      h.queue_length ? `${ic("clock", "xs")} 排隊中 ${h.queue_length} 個` : "",
      h.ffmpeg ? "" : `${ic("info", "xs")} 沒有 ffmpeg，片段改播原影片`,
    ].filter(Boolean);
    box.innerHTML = parts.map((x) => `<span>${x}</span>`).join("")
      + (okModels ? "" : `<div class="svc-warn">${ic("alert", "xs")} 缺少：${h.models_missing.map(esc).join("、")}。請先執行 <b>下載模型.cmd</b>。</div>`);
  }
  const re = $("#btn-recheck"); if (re) re.onclick = checkHealth;
}
setInterval(() => { if (state.mode === "remote" && state.step === 1 && document.body.classList.contains("authed")) checkHealth(); }, 8000);

function refreshAnalyzeBtn() {
  let reason = "";
  if (!state.video) reason = "請先上傳影片";
  else if (state.gps.lat == null) reason = "請設定 GPS 位置";
  else if (state.mode === "remote") {
    const h = state.health;
    if (!h) reason = "正在檢查推論服務…";
    else if (!h.online) reason = "推論服務未開啟";
    else if (h.models_ready !== h.models_total) reason = "推論服務缺少模型檔";
  }
  $("#btn-analyze").disabled = !!reason;
  $("#analyze-hint").textContent = reason;
}

/* ============================ Step 2：分析 ============================ */
$("#btn-analyze").addEventListener("click", startAnalysis);
$("#btn-rerun").addEventListener("click", startAnalysis);
async function startAnalysis() {
  if (!state.video) { goStep(1); return; }
  const g = state.gps;
  const body = {
    video_id: state.video.video_id, recorded_at: $("#recorded-at").value || null,
    speed: +$("#speed").value, analyzer: state.mode || "remote",
    options: { max_seconds: $("#max-seconds").value ? +$("#max-seconds").value : null, rules: "all" },
    gps: g.track ? { mode: "track", track: g.track } : { mode: g.mode, lat: g.lat, lon: g.lon },
  };
  try {
    const job = await postJSON("/api/jobs", body);
    Object.assign(state, { job, jobStartedAt: Date.now(), ignored: new Set(), forms: {}, cases: {}, selected: null });
    await ensureDraft();
    resetAnalysisUI();
    state.maxStep = 2; goStep(2); renderStages(job); renderModels(job);
    startPolling();
  } catch (e) { toast(e.message, "alert"); }
}
function startPolling() { clearInterval(state.pollTimer); state.pollTimer = setInterval(pollJob, 700); }
$("#btn-cancel").addEventListener("click", async () => {
  if (!state.job) return;
  const b = $("#btn-cancel");
  if (!b.dataset.armed) { b.dataset.armed = "1"; b.innerHTML = `${ic("x")}確定取消？`; setTimeout(() => { delete b.dataset.armed; b.innerHTML = `${ic("x")}取消分析`; }, 3000); return; }
  b.disabled = true; b.innerHTML = `${ic("x")}取消中…`;
  try { await postJSON(`/api/jobs/${state.job.id}/cancel`, {}); } catch (e) { toast(e.message, "alert"); b.disabled = false; }
});

function resetAnalysisUI() {
  state.logCount = 0; state.foundCount = 0;
  $("#btn-rerun").classList.add("hidden");
  $("#btn-cancel").classList.remove("hidden"); $("#btn-cancel").disabled = false;
  $("#console").innerHTML = ""; $("#alert-stack").innerHTML = "";
  $("#feed").innerHTML = `<div class="feed-empty">還沒發現違規，持續分析中…</div>`;
  $("#live-img").removeAttribute("src"); $("#live-empty").classList.remove("hidden");
  $("#live-frame").classList.remove("idle");
  $("#btn-to-review").classList.add("hidden");
  const tag = $("#live-tag"); tag.classList.remove("off", "warn"); tag.innerHTML = `<span class="dot"></span>LIVE`;
  $$("#stats b").forEach((b) => { b.dataset.v = 0; b.textContent = "0"; });
  $("#frames-total").textContent = "";
  $(".ring-wrap").classList.remove("done"); setRing(0);
  $("#ring-stage").textContent = "準備開始"; $("#ring-eta").innerHTML = `${ic("clock", "xs")} 預估剩餘 --`;
}
function setRing(p) {
  $("#ring-fg").style.strokeDashoffset = 326.7 * (1 - p);
  $("#ring-pct").textContent = Math.round(p * 100);
}
function overallProgress(job) {
  return job.stages.reduce((sum, s) => sum + (s.weight != null ? s.weight : STAGE_WEIGHT[s.key] || 0) * s.progress, 0);
}

async function pollJob() {
  let job;
  try { job = await api(`/api/jobs/${state.job.id}`); } catch (e) { return; }
  applyJob(job, false);
}
/* 把分析狀態畫到畫面上；restoring=true 表示從草稿還原（不跳提示、不自動換頁） */
function applyJob(job, restoring) {
  const prevPreview = restoring ? -1 : state.job.preview_version;
  state.job = job;
  renderStages(job); renderModels(job);

  // 進度環與剩餘時間
  const p = job.status === "done" ? 1 : overallProgress(job);
  setRing(p);
  const cur = job.stages.find((s) => s.key === job.current_stage);
  $("#ring-stage").textContent = job.status === "done" ? "分析完成" : cur ? cur.label : "準備開始";
  const elapsed = (Date.now() - state.jobStartedAt) / 1000;
  if (job.status === "done") $("#ring-eta").innerHTML = `${ic("check", "xs")} 共花費 ${elapsed.toFixed(1)} 秒`;
  else if (job.eta_sec != null) $("#ring-eta").innerHTML = `${ic("clock", "xs")} 預估剩餘 ${fmtDur(job.eta_sec)}${job.fps ? `・${job.fps.toFixed(1)} FPS` : ""}`;
  else if (p > 0.04 && job.analyzer !== "remote") $("#ring-eta").innerHTML = `${ic("clock", "xs")} 預估剩餘 ${fmtDur((elapsed / p) * (1 - p))}`;
  else if (job.analyzer === "remote") $("#ring-eta").innerHTML = `${ic("clock", "xs")} 已經過 ${fmtDur(elapsed)}`;

  // 即時畫面
  if (job.preview_version && job.preview_version !== prevPreview) {
    const img = $("#live-img");
    img.onload = () => $("#live-empty").classList.add("hidden");
    img.src = `/api/jobs/${job.id}/preview.jpg?v=${job.preview_version}`;
  }
  // 統計數字
  const st = job.stats || {};
  $$("#stats b[data-k]").forEach((b) => animateNum(b, st[b.dataset.k] || 0));
  if (st.frames_total) $("#frames-total").textContent = `/ ${st.frames_total.toLocaleString()}`;
  // 新發現的違規
  (job.found || []).slice(state.foundCount).forEach((f) => pushFound(f, !restoring));
  state.foundCount = (job.found || []).length;
  // 紀錄
  const con = $("#console");
  job.logs.slice(state.logCount).forEach((l) => {
    const label = (job.stages.find((s) => s.key === l.stage) || {}).label || l.stage;
    con.insertAdjacentHTML("beforeend", `<div><span class="t">${l.time}</span>　<span class="s">${esc(label)}</span>　${esc(l.msg)}</div>`);
  });
  if (job.logs.length !== state.logCount) con.scrollTop = con.scrollHeight;
  state.logCount = job.logs.length;
  if (job.video_meta) {
    const m = job.video_meta;
    $("#job-meta").textContent = `${job.video_name}・${m.width}×${m.height}・${Math.round(m.fps)} FPS・${fmtSec(m.duration)}`;
  }

  if (["done", "error", "interrupted", "cancelled"].includes(job.status)) {
    clearInterval(state.pollTimer);
    $("#btn-cancel").classList.add("hidden");
    $("#live-frame").classList.add("idle");
    const tag = $("#live-tag"); tag.classList.add("off");
    tag.innerHTML = `<span class="dot"></span>${job.status === "done" ? "完成" : "中斷"}`;
    if (job.status !== "done") {
      tag.classList.add("warn");
      $("#ring-stage").textContent = job.status === "cancelled" ? "已取消" : job.status === "error" ? "分析失敗" : "分析中斷";
      $("#ring-eta").innerHTML = `${ic("alert", "xs")} ${esc(job.error || "發生錯誤")}`;
      $("#btn-rerun").classList.remove("hidden");
      if (!restoring) toast(job.status === "cancelled" ? "已取消分析" : "分析失敗：" + job.error, "alert");
      return;
    }
    $(".ring-wrap").classList.add("done");
    if (job.finished && job.started_ts) {
      const secs = (new Date(job.finished.replace(" ", "T")) - job.started_ts * 1000) / 1000;
      if (secs > 0) $("#ring-eta").innerHTML = `${ic("check", "xs")} 共花費 ${secs.toFixed(1)} 秒`;
    }
    unlock(3);
    $("#btn-to-review").classList.remove("hidden");
    if (!restoring) { goStep(2); toast(`分析完成，找到 ${job.violations.length} 筆違規`, "sparkles"); }
    renderReview();
  }
}
function pushFound(f, pop = true) {
  const meta = TYPE_META[f.type] || { icon: "alert", color: "red" };
  const color = f.status === "confirmed" ? "red" : "amber";
  const statusTxt = f.status === "confirmed" ? "確定違規" : "疑似違規";
  const feed = $("#feed");
  const empty = feed.querySelector(".feed-empty"); if (empty) empty.remove();
  feed.insertAdjacentHTML("afterbegin", `<div class="feed-item">
      <span class="ic-bubble ${color}">${ic(meta.icon)}</span>
      <div class="info"><b>${esc(f.label)}</b><div class="sub">${esc(f.vehicle_type)}・影片 ${fmtSec(f.t)}・${confText(f)}</div></div>
      <span class="plate">${esc(f.plate)}</span><span class="status ${f.status === "confirmed" ? "submitted" : "draft"}" style="${f.status === "confirmed" ? "background:var(--red-soft);color:var(--red)" : ""}">${statusTxt}</span>
    </div>`);
  if (!pop) return;
  const stack = $("#alert-stack");
  const el = document.createElement("div");
  el.className = "alert-pop";
  el.innerHTML = `<span class="ic-bubble ${color}">${ic(meta.icon)}</span><div><b>偵測到${statusTxt}</b>${esc(f.label)}・${esc(f.plate)}</div>`;
  stack.prepend(el);
  setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 400); }, 3200);
}
function renderStages(job) {
  $("#stages").innerHTML = job.stages.map((s) => `
    <li class="${s.state}">
      <div class="ico">${ic(s.state === "done" ? "check" : STAGE_ICON[s.key] || "info")}</div>
      <div><div class="name">${esc(s.label)}</div><div class="bar"><div style="width:${Math.round(s.progress * 100)}%"></div></div></div>
      <span class="pct">${s.state === "pending" ? "" : Math.round(s.progress * 100) + "%"}</span>
    </li>`).join("");
}
function renderModels(job) {
  const order = job.stages.map((s) => s.key);
  const curIdx = order.indexOf(job.current_stage);
  $("#models").innerHTML = MODELS.map((m) => {
    const idx = order.findIndex((k) => m.stages.includes(k));
    const stage = job.stages[idx];
    let cls = "";
    if (job.status === "done" || (stage && stage.state === "done") || curIdx > idx) cls = "done";
    else if (curIdx === idx) cls = "run";
    return `<div class="model ${cls}">${ic(m.icon)}<span>${m.name}</span><i class="lamp"></i></div>`;
  }).join("");
}
$("#btn-to-review").addEventListener("click", () => goStep(3));

/* ============================ Step 3：審查 ============================ */
function renderReview() {
  const job = state.job, vs = job.violations;
  const nC = vs.filter((v) => v.status === "confirmed").length;
  $("#summary").innerHTML = `
    <div class="stat" style="--i:0"><span class="ic-bubble violet">${ic("review")}</span><div><b>${vs.length}</b><small>違規事件</small></div></div>
    <div class="stat red" style="--i:1"><span class="ic-bubble red">${ic("shield")}</span><div><b>${nC}</b><small>確定違規</small></div></div>
    <div class="stat amber" style="--i:2"><span class="ic-bubble amber">${ic("alert")}</span><div><b>${vs.length - nC}</b><small>疑似違規・需確認</small></div></div>`;
  const fv = $("#full-video"); fv.src = job.video_url;
  const dur = (job.video_meta && job.video_meta.duration) || 1;
  // 時間重疊的事件放到不同列，避免色塊疊在一起
  const laneEnds = [], laneOf = {};
  [...vs].sort((a, b) => a.t_start - b.t_start).forEach((v) => {
    let lane = laneEnds.findIndex((end) => end <= v.t_start);
    if (lane < 0) { lane = laneEnds.length; laneEnds.push(0); }
    laneEnds[lane] = v.t_end + dur * 0.01; laneOf[v.id] = lane;
  });
  $("#timeline").style.height = `${12 + Math.max(1, laneEnds.length) * 34 - 6}px`;
  $("#timeline").innerHTML = vs.map((v) => `
    <div class="mark ${v.status}" data-t="${v.t_start}" title="${v.id}・${esc(v.label)}（${v.ts_label}）"
      style="top:${6 + laneOf[v.id] * 34}px;left:${(v.t_start / dur) * 100}%;width:${((v.t_end - v.t_start) / dur) * 100}%"><span>${v.id}・${esc(v.label)}</span></div>`).join("") + `<div class="cursor" id="tl-cursor"></div>`;
  $$("#timeline .mark").forEach((m) => m.addEventListener("click", () => { fv.currentTime = +m.dataset.t; fv.play().catch(() => {}); }));
  fv.ontimeupdate = () => { const c = $("#tl-cursor"); if (c) c.style.left = (fv.currentTime / dur) * 100 + "%"; };
  renderViolationCards();
  refreshCaseStatus();
}
async function refreshCaseStatus() {
  const ids = Object.values(state.cases).filter((cid) => !(cid in state.caseStatus) || state.caseStatus[cid] !== "submitted");
  if (!ids.length) return;
  await Promise.all(ids.map(async (cid) => {
    try { state.caseStatus[cid] = (await api(`/api/cases/${encodeURIComponent(cid)}`)).status; } catch (e) { /* 案件可能被刪掉 */ }
  }));
  renderViolationCards();
}
function caseStateOf(v) {
  const cid = state.cases[v.id];
  return cid ? state.caseStatus[cid] || "draft" : null;
}
function renderViolationCards() {
  const vs = state.job.violations.filter((v) => state.filter === "all" || v.status === state.filter);
  if (!vs.length) { $("#violations").innerHTML = `<div class="card empty-state" style="grid-column:1/-1"><span class="ic-bubble green">${ic("check")}</span><h3>沒有符合的違規</h3></div>`; return; }
  $("#violations").innerHTML = vs.map((v, i) => {
    const ign = state.ignored.has(v.id), meta = TYPE_META[v.type] || { icon: "alert", color: "red" };
    const confColor = v.status === "confirmed" ? "linear-gradient(90deg,#ff8a95,#ff4d5e)" : "linear-gradient(90deg,#ffd27a,#f59e0b)";
    const place = v.geo ? `${v.geo.city}${v.geo.district}` : "—";
    const cs = caseStateOf(v);
    const csBadge = cs === "submitted" ? `<span class="case-state done">${ic("check")}已送出</span>`
      : cs ? `<span class="case-state">${ic("file")}檢舉單草稿</span>` : "";
    const mainBtn = cs === "submitted"
      ? `<button class="btn soft small" data-report="${v.id}">${ic("check")}查看已送出</button>`
      : `<button class="btn primary small" data-report="${v.id}" ${ign ? "disabled" : ""}>${cs ? "繼續檢舉單" : "建立檢舉單"}${ic("right")}</button>`;
    return `
    <div class="vcard ${ign ? "ignored" : ""}" style="--i:${i}">
      <div class="thumb" data-play="${v.id}">
        ${v.snapshot_url ? `<img src="${v.snapshot_url}" alt="" loading="lazy">` : ""}
        <span class="play">${ic("play")}</span>
        <span class="badge ${v.status}">${ic(v.status === "confirmed" ? "shield" : "alert")}${v.status === "confirmed" ? "確定違規" : "疑似違規"}</span>
        <span class="ts-chip">${v.ts_label}</span>${csBadge}
      </div>
      <div class="vbody">
        <div class="vtitle"><span class="ic-bubble ${meta.color}">${ic(meta.icon)}</span><h3>${esc(v.label)}</h3><span class="plate ${v.plate_missing ? "missing" : ""}">${esc(v.plate || "未辨識")}</span></div>
        <div class="meta-list">
          <div class="meta">${ic("clock")}<span>${esc(v.datetime.slice(5, 16))}</span></div>
          <div class="meta">${ic(v.vehicle_type === "機車" ? "moto" : "car")}<span>${esc(v.vehicle_label || v.vehicle_type)}・#${esc(v.track_id)}</span></div>
          <div class="meta wide">${ic("pin")}<span>${esc(place)}</span></div>
        </div>
        ${v.evidence_kind === "conditions"
          ? `<div class="conf">判定條件<div class="bar"><div data-w="${v.confidence * 100}" style="background:${confColor}"></div></div><b>${v.conditions_met}/${v.conditions_total}</b></div>`
          : `<div class="conf">違規信心<div class="bar"><div data-w="${v.confidence * 100}" style="background:${confColor}"></div></div><b>${Math.round(v.confidence * 100)}%</b></div>`}
        ${v.plate_missing
          ? `<div class="conf">車牌信心<div class="bar"><div data-w="0"></div></div><b>—</b></div>`
          : `<div class="conf">車牌信心<div class="bar"><div data-w="${v.plate_confidence * 100}" style="background:var(--grad)"></div></div><b>${Math.round(v.plate_confidence * 100)}%</b></div>`}
        ${v.reason ? `<div class="reason">${ic("info")}<span>${esc(v.reason)}${v.source === "model" ? "" : "，建議人工確認"}</span></div>` : ""}
        ${v.source === "model" ? `<div class="model-tag">${ic("sparkles", "xs")} 真實模型判定・<a class="link" data-play="${v.id}">看判定依據</a></div>` : ""}
        <div class="vactions">
          <button class="btn ghost small" data-ignore="${v.id}" title="${ign ? "取消忽略" : "忽略這筆"}">${ic(ign ? "refresh" : "eyeoff")}${ign ? "恢復" : "忽略"}</button>
          ${mainBtn}
        </div>
      </div>
    </div>`;
  }).join("");
  requestAnimationFrame(() => setTimeout(() => $$("#violations .bar div[data-w]").forEach((b) => { b.style.width = b.dataset.w + "%"; }), 120));
}
$("#violations").addEventListener("click", (e) => {
  const p = e.target.closest("[data-play]"), ig = e.target.closest("[data-ignore]"), rp = e.target.closest("[data-report]");
  if (p) playClip(state.job.violations.find((v) => v.id === p.dataset.play));
  if (ig) { const id = ig.dataset.ignore; state.ignored.has(id) ? state.ignored.delete(id) : state.ignored.add(id); renderViolationCards(); saveDraft(); }
  if (rp) {
    const v = state.job.violations.find((x) => x.id === rp.dataset.report);
    if (caseStateOf(v) === "submitted") openSubmitted(v); else openCaseForm(v);
  }
});
$$("#filter button").forEach((b) => b.addEventListener("click", () => {
  state.filter = b.dataset.f; $$("#filter button").forEach((x) => x.classList.toggle("active", x === b)); renderViolationCards(); saveDraft();
}));

/* 片段播放：剪好的片段若瀏覽器播不了，就改播原影片的那一段 */
function clipPlayer(v, job, autoplay = true) {
  const wrap = document.createElement("div");
  const note = document.createElement("div"); note.className = "muted small"; note.style.marginTop = "6px";
  const vid = document.createElement("video");
  vid.controls = true; vid.muted = true; vid.playsInline = true; vid.autoplay = autoplay;
  if (v.snapshot_url) vid.poster = v.snapshot_url;
  const fallback = () => {
    vid.onerror = null;
    vid.src = `${job.video_url}#t=${v.t_start},${v.t_end}`;
    note.textContent = "（瀏覽器無法播放剪輯檔，改播原影片對應片段）";
  };
  vid.onerror = fallback;
  if (v.clip_url && !(v.source === "model" && v.clip_h264 === false)) vid.src = v.clip_url; else fallback();
  wrap.append(vid, note);
  return wrap;
}
function playClip(v) {
  const meta = TYPE_META[v.type] || { icon: "alert", color: "red" };
  const conds = v.conditions && v.conditions.length ? `
    <div class="cond-box"><h3 class="mini-title">${ic("scale", "xs")} 判定依據（${v.conditions_met}/${v.conditions_total} 個條件符合）</h3>
      ${v.model_description ? `<p class="small" style="margin:0 0 10px">${esc(v.model_description)}</p>` : ""}
      <ul class="cond-list">${v.conditions.map((c) => `<li class="${c.ok ? "ok" : "no"}">${ic(c.ok ? "check" : "x", "xs")}${esc(c.label)}</li>`).join("")}</ul>
    </div>` : "";
  openModal(`<h2><span class="ic-bubble ${meta.color}">${ic(meta.icon)}</span>${esc(v.label)}<span class="plate ${v.plate_missing ? "missing" : ""}">${esc(v.plate || "未辨識")}</span></h2><div id="clip-slot"></div>
    <p class="muted small">${ic("scissors", "xs")} 片段：影片 ${fmtSec(v.t_start)} ~ ${fmtSec(v.t_end)}，違規時間點 ${fmtSec(v.t)}</p>${conds}`);
  $("#clip-slot").append(clipPlayer(v, state.job));
}

/* ============================ Step 4：檢舉單 ============================ */
function fillCaseForm(v) {
  state.selected = v;
  const f = $("#case-form"), geo = v.geo || {}, saved = state.forms[v.id];
  const city = (saved && saved.city) || geo.city;
  $("#city-select").innerHTML = state.cities.map((c) => `<option ${c.name === city ? "selected" : ""}>${c.name}</option>`).join("");
  $("#city-hint").innerHTML = geo.city ? `${ic("check", "xs")}依 GPS 推薦：${esc(geo.city)}` : "";
  const vals = saved || {
    district: geo.district || "", location: geo.address_hint || "", date: v.date, time: v.time,
    plate: v.plate, vehicle_type: v.vehicle_type, category: v.category, fact: v.fact, description: v.description,
  };
  Object.entries(vals).forEach(([k, val]) => { if (f.elements[k] && k !== "city") f.elements[k].value = val; });
  const box = $("#evidence-box"); box.innerHTML = ""; box.append(clipPlayer(v, state.job, false));
  $("#evidence-meta").innerHTML = `<span class="plate" style="font-size:12px">${esc(v.plate)}</span>　${esc(v.label)}・${v.status === "confirmed" ? "確定違規" : "疑似違規"}（${confText(v)}）`;
  renderReporter();
}
function openCaseForm(v) { fillCaseForm(v); unlock(4); goStep(4); }
async function openSubmitted(v) {
  try {
    const c = await api(`/api/cases/${encodeURIComponent(state.cases[v.id])}`);
    state.selected = v; state.caseObj = c;
    fillCaseForm(v); renderSubmit(c, $("#submit-panel")); unlock(5); goStep(5);
  } catch (e) { toast(e.message, "alert"); }
}
/* 檢舉單每改一個字就存進草稿 */
$("#case-form").addEventListener("input", () => {
  if (!state.selected) return;
  state.forms[state.selected.id] = Object.fromEntries(new FormData($("#case-form")).entries());
  saveDraft();
});
const maskId = (id) => (id && id.length > 4 ? id.slice(0, 2) + "•".repeat(id.length - 4) + id.slice(-2) : id);
function renderReporter() {
  const p = state.profile;
  if (!p.reporter_name) { $("#reporter-box").innerHTML = `<div class="empty-reporter">${ic("alert")}尚未設定檢舉人資料，請點右上「修改」。</div>`; return; }
  $("#reporter-box").innerHTML = `<div class="reporter"><div class="avatar">${esc(p.reporter_name.slice(0, 1))}</div>
    <div class="lines"><b>${esc(p.reporter_name)}</b><br>${esc(maskId(p.reporter_id))}・${esc(p.reporter_phone)}<br>${esc(p.reporter_email)}</div></div>`;
}
$("#btn-make-case").addEventListener("click", async () => {
  const f = $("#case-form");
  if (!f.reportValidity()) return;
  const fields = Object.fromEntries(new FormData(f).entries());
  try {
    const c = await postJSON("/api/cases", { job_id: state.job.id, violation_id: state.selected.id, fields, case_id: state.cases[state.selected.id] });
    state.forms[state.selected.id] = fields;
    state.cases[state.selected.id] = c.id; state.caseStatus[c.id] = c.status;
    state.caseObj = c; unlock(5); renderSubmit(c, $("#submit-panel")); goStep(5);
    toast("已產生案件資料夾", "folder");
  } catch (e) { toast(e.message, "alert"); }
});

/* ============================ Step 5：送出（模擬 autoForm） ============================ */
const FILL_STEPS = [
  ["開啟警察局檢舉網站", "send"], ["勾選同意事項", "check"], ["填入檢舉人資料", "user"],
  ["填入違規時間、地點、車牌", "calendar"], ["選擇違規類別與事實", "scale"], ["上傳佐證影片", "upload"],
];

function renderSubmit(c, root, inModal = false) {
  const f = c.fields, v = c.violation, meta = TYPE_META[v.type] || { icon: "alert", color: "red" };
  const fileIcon = (n) => (n.endsWith(".mp4") ? "video" : n.endsWith(".jpg") ? "scan" : "file");
  const filesHtml = (c.files || []).filter((n) => n !== "case.json").map((n) =>
    `<a class="file-link" href="${c.base_url}${encodeURIComponent(n)}" target="_blank">${ic(fileIcon(n))}${esc(n)}</a>`).join("");
  root.innerHTML = `
  ${inModal ? "" : `<div class="review-top"><div><h1 class="page-title">送出檢舉</h1><p class="muted">案件已建立，啟動自動填表即可送到${esc(c.city)}警察局。</p></div></div>`}
  <div class="grid2 wide-left">
    <div class="card">
      <div class="target"><span class="ic-bubble blue">${ic("shield")}</span><div><b>${esc(c.city)}政府警察局</b><span class="muted small">交通違規檢舉系統</span></div></div>
      <div class="seg full af-tabs"><button data-af-mode="real">${ic("send")}自動填表（autoForm）</button><button data-af-mode="sim">${ic("sparkles")}模擬送出</button></div>
      <div id="submit-area"></div>
    </div>
    <div class="side-col">
      <div class="card">
        <h3 class="mini-title">案件摘要</h3>
        <div class="summary-list">
          <div class="summary-item"><span class="ic-bubble ${meta.color}" style="width:34px;height:34px;border-radius:11px">${ic(meta.icon, "xs")}</span><div><b>${esc(f.category)}</b><small>${esc(f.fact)}</small></div></div>
          <div class="summary-item">${ic(f.vehicle_type === "機車" ? "moto" : "car")}<div><span class="plate">${esc(f.plate)}</span>　${esc(f.vehicle_type)}</div></div>
          <div class="summary-item">${ic("clock")}<div>${esc(f.date)} ${esc(f.time)}</div></div>
          <div class="summary-item">${ic("pin")}<div>${esc(f.location)}</div></div>
          <div class="summary-item">${ic("hash")}<div><small>案件編號</small>${esc(c.id)}</div></div>
        </div>
      </div>
      <div class="card">
        <h3 class="mini-title">案件資料夾</h3>
        <div class="files">${filesHtml}</div>
        <a class="btn ghost small" href="/api/cases/${encodeURIComponent(c.id)}/zip">${ic("download")}下載案件 ZIP</a>
        <div class="cmd"><code>${esc(c.autoform_cmd)}</code><button class="btn small" data-copy="${esc(c.autoform_cmd)}" title="複製 autoForm 指令">${ic("copy")}</button></div>
        <p class="muted small" style="margin:8px 0 0">格式與 autoForm 相同，之後可直接用上面的指令送出。</p>
      </div>
    </div>
  </div>
  ${inModal ? "" : `<div class="actions"><button class="btn ghost" id="btn-back-review">${ic("left")}處理下一筆違規</button><button class="btn ghost" data-view-link="cases">${ic("folder")}查看所有案件</button></div>`}`;
  const back = $("#btn-back-review", root); if (back) back.onclick = () => goStep(3);
  const area = $("#submit-area", root);
  stopAfPoll();
  const tabs = $(".af-tabs", root);
  if (c.status === "submitted") { tabs.remove(); return renderSuccess(c, area, false); }
  const setMode = (m) => {
    AF.mode = m;
    $$("[data-af-mode]", tabs).forEach((b) => b.classList.toggle("active", b.dataset.afMode === m));
    if (m === "sim") renderSimSubmit(c, area); else renderRealSubmit(c, area);
  };
  $$("[data-af-mode]", tabs).forEach((b) => (b.onclick = () => { if (!AF.running) setMode(b.dataset.afMode); else toast("填表進行中，請先完成或取消", "alert"); }));
  setMode(c.autofill ? "real" : (AF.mode || "real"));
}
function renderSimSubmit(c, area) {
  area.innerHTML = `<div class="notice">${ic("info")}<span><b>模擬送出</b>：不會連到警察局網站，只是展示流程。</span></div>
    <ul class="checklist">${FILL_STEPS.map(([s, i]) => `<li data-icon="${i}"><span class="ico">${ic(i)}</span>${s}</li>`).join("")}
      <li data-icon="shield"><span class="ico">${ic("shield")}</span>人機驗證</li></ul>
    <button class="btn primary big" id="btn-autofill" style="width:100%">${ic("sparkles")}啟動自動填表（模擬）</button>`;
  $("#btn-autofill", area).onclick = () => runFill(c, area);
}

/* ---- 真的自動填表：推論服務在這台電腦開瀏覽器，執行組員的 autoForm ---- */
const AF = { mode: null, timer: null, running: false };
const AF_STEPS = [["starting", "開啟瀏覽器", "send"], ["filling", "填寫表單、上傳佐證", "edit"],
  ["waiting_verification", "人機驗證（請到瀏覽器操作）", "shield"], ["submitting", "送出並確認", "check"]];
function stopAfPoll() { clearTimeout(AF.timer); AF.timer = null; AF.running = false; }

async function renderRealSubmit(c, area) {
  if (c.autofill && !c.autofill.final) return pollAutofill(c, area);
  if (c.autofill && c.autofill.final) {  // 上次填表的結果還在，連同瀏覽器狀態一起顯示
    try { const r = await api(`/api/cases/${encodeURIComponent(c.id)}/autofill`); return showAutofill(r.case, r.report, area); } catch (e) { /* 服務沒開，就顯示啟動畫面 */ }
  }
  area.innerHTML = `<div class="af-health muted small">${ic("refresh", "xs")} 檢查自動填表環境…</div>`;
  let h; try { h = await api("/api/autofill/health"); } catch (e) { h = { available: false, hint: e.message }; }
  const ok = h.available;
  area.innerHTML = `
    <div class="af-health ${ok ? "ok" : "bad"}">${ic(ok ? "check" : "alert", "xs")}<span>${ok ? "自動填表可以使用：會在這台電腦開一個瀏覽器，自動填寫" + esc(c.city) + "的檢舉網站" : esc(h.hint || "自動填表暫時不能用")}</span></div>
    ${h.available && !h.profile && !PROFILE_FILLED() ? `<div class="notice">${ic("user")}<span>還沒填檢舉人資料，表單的姓名、身分證欄位會是空的。可以先到「檢舉人資料」填寫。</span></div>` : ""}
    <label class="check-row af-test"><input type="checkbox" id="af-test" checked> <span><b>測試模式：只填不送</b>（建議）<br><small class="muted">填好整張表單、上傳佐證，但不做送出前的驗證、不按送出鈕</small></span></label>
    <div class="af-confirm hidden" id="af-confirm">
      <div class="notice danger">${ic("alert")}<span><b>正式送出</b>會把這筆檢舉真的送到${esc(c.city)}政府警察局，送出後無法撤回。請確認車牌、時間、地點、違規事實都正確，而且是真實發生的違規。</span></div>
      <label class="check-row"><input type="checkbox" id="af-sure"> 我已確認資料正確，要正式送出</label>
    </div>
    <button class="btn primary big" id="btn-af-start" style="width:100%" ${ok ? "" : "disabled"}>${ic("send")}開啟瀏覽器自動填表（測試）</button>
    <p class="muted small af-tip">驗證碼、Email 認證要你在跳出的瀏覽器裡自己完成。過程中可以隨時按「取消」，瀏覽器會關閉，什麼都不會送出。</p>`;
  const test = $("#af-test", area), btn = $("#btn-af-start", area);
  const sync = () => {
    $("#af-confirm", area).classList.toggle("hidden", test.checked);
    btn.innerHTML = test.checked ? `${ic("send")}開啟瀏覽器自動填表（測試）` : `${ic("send")}開啟瀏覽器並正式送出`;
    btn.classList.toggle("danger", !test.checked);
    btn.disabled = !ok || (!test.checked && !$("#af-sure", area).checked);
  };
  test.onchange = sync; $("#af-sure", area).onchange = sync; sync();
  btn.onclick = async () => {
    btn.disabled = true;
    try {
      const r = await postJSON(`/api/cases/${encodeURIComponent(c.id)}/autofill`, { test: test.checked, confirm: !test.checked && $("#af-sure", area).checked });
      state.caseObj = r.case; toast("已開啟填表瀏覽器，請看工作列", "send");
      showAutofill(r.case, r.report, area); scheduleAf(r.case, area);
    } catch (e) { toast(e.message, "alert"); btn.disabled = false; }
  };
}
const PROFILE_FILLED = () => !!(state.profile && state.profile.reporter_name);

async function pollAutofill(c, area) {
  try {
    const r = await api(`/api/cases/${encodeURIComponent(c.id)}/autofill`);
    if (!area.isConnected) return stopAfPoll();
    showAutofill(r.case, r.report, area);
    if (r.report.state && (["starting", "filling", "waiting_verification", "submitting"].includes(r.report.state) || r.report.browser_open)) scheduleAf(r.case, area);
    else AF.running = false;
  } catch (e) {
    if (!area.isConnected) return stopAfPoll();
    area.innerHTML = `<div class="af-health bad">${ic("alert", "xs")}<span>${esc(e.message)}</span></div>
      <button class="btn soft" id="btn-af-retry">${ic("refresh")}重新檢查</button>`;
    $("#btn-af-retry", area).onclick = () => pollAutofill(c, area);
    AF.running = false;
  }
}
function scheduleAf(c, area) { clearTimeout(AF.timer); AF.running = true; AF.timer = setTimeout(() => pollAutofill(c, area), 1000); }

function showAutofill(c, r, area) {
  const st = r.state, active = ["starting", "filling", "waiting_verification", "submitting"].includes(st);
  if (c.status === "submitted") {  // 正式送出完成：顯示成功畫面，瀏覽器還開著就多一顆關閉鈕
    const first = state.caseStatus[c.id] !== "submitted";
    if (first || !$(".success", area)) renderSuccess(c, area, first);
    $(".af-close-row", area)?.remove();
    if (r.browser_open) {
      area.insertAdjacentHTML("beforeend", `<div class="actions af-close-row" style="justify-content:center"><button class="btn ghost" id="btn-af-cancel">${ic("x")}看完了，關閉填表瀏覽器</button></div>`);
      $("#btn-af-cancel", area).onclick = async () => {
        try { const x = await postJSON(`/api/cases/${encodeURIComponent(c.id)}/autofill/cancel`, {}); showAutofill(x.case, x.report, area); } catch (e) { toast(e.message, "alert"); }
      };
    }
    if (first) { state.caseObj = c; state.caseStatus[c.id] = "submitted"; saveDraft(); toast("autoForm 已送出", "send"); }
    return;
  }
  const idx = AF_STEPS.findIndex(([k]) => k === st);
  const stepsHtml = AF_STEPS.filter(([k]) => !(r.test && (k === "waiting_verification" || k === "submitting")) || k === st).map(([k, label, icon], i) => {
    const real = AF_STEPS.findIndex(([x]) => x === k);
    const cls = st === "done" ? "done" : active ? (real < idx ? "done" : real === idx ? "run" : "") : (real < idx ? "done" : "");
    return `<li class="${cls}"><span class="ico">${ic(cls === "done" ? "check" : cls === "run" ? "refresh" : icon)}</span>${label}</li>`;
  }).join("");
  const head = st === "done" ? (r.test
      ? `<div class="af-result ok">${ic("check")}<div><b>測試完成，沒有送出</b><span>${esc(r.message)}</span></div></div>`
      : `<div class="af-result ${r.success ? "ok" : "warn"}">${ic(r.success ? "check" : "alert")}<div><b>${r.success ? "autoForm 已送出" : "填表跑完了，請確認"}</b><span>${esc(r.message)}</span></div></div>`)
    : st === "error" ? `<div class="af-result bad">${ic("alert")}<div><b>填表中斷</b><span>${esc(r.message || "請看下方紀錄")}</span></div></div>`
    : st === "cancelled" ? `<div class="af-result">${ic("x")}<div><b>已取消</b><span>${esc(r.message || "瀏覽器已關閉，沒有送出")}</span></div></div>`
    : st === "waiting_verification" ? `<div class="af-result warn pulse">${ic("shield")}<div><b>請到跳出的瀏覽器完成驗證</b><span>輸入驗證碼或完成 Email 認證後，autoForm 會自動繼續${r.test ? "" : "並送出"}。</span></div></div>`
    : `<div class="af-result run">${ic("refresh")}<div><b>${r.test ? "測試模式・" : "正式送出・"}自動填表中</b><span>請不要操作那個瀏覽器，等它填完。</span></div></div>`;
  const log = (r.log || []).slice(-40).map((l) => `<div><span class="t">${esc(l.t)}</span> ${esc(l.text)}</div>`).join("");
  const wasOpen = $(".af-log", area)?.open;
  const btns = [];
  if (st === "waiting_verification") btns.push(`<button class="btn soft" id="btn-af-continue" title="大部分縣市會自動偵測，瀏覽器卡住時才需要">${ic("right")}我已完成驗證，繼續</button>`);
  if (active) btns.push(`<button class="btn danger-soft" id="btn-af-cancel">${ic("x")}取消（不送出）</button>`);
  else if (r.browser_open) btns.push(`<button class="btn ghost" id="btn-af-cancel">${ic("x")}關閉填表瀏覽器</button>`);
  if (!active && c.status !== "submitted") btns.push(`<button class="btn soft" id="btn-af-again">${ic("refresh")}${st === "done" && r.test ? "回到送出選項" : "重新填表"}</button>`);
  area.innerHTML = `${head}${r.warning ? `<div class="notice">${ic("info")}<span>${esc(r.warning)}</span></div>` : ""}
    <ul class="checklist">${stepsHtml}</ul>
    <div class="actions left af-btns">${btns.join("")}</div>
    <details class="log af-log" ${wasOpen ? "open" : ""}><summary>${ic("terminal", "xs")} autoForm 輸出</summary><div class="console">${log || '<div class="t">（等待輸出…）</div>'}</div></details>`;
  const con = $(".console", area); if (con) con.scrollTop = con.scrollHeight;
  const act = async (a) => {
    try { const x = await postJSON(`/api/cases/${encodeURIComponent(c.id)}/autofill/${a}`, {}); showAutofill(x.case, x.report, area); scheduleAf(x.case, area); }
    catch (e) { toast(e.message, "alert"); }
  };
  const cont = $("#btn-af-continue", area); if (cont) cont.onclick = () => act("continue");
  const cancel = $("#btn-af-cancel", area); if (cancel) cancel.onclick = () => act("cancel");
  const again = $("#btn-af-again", area);
  if (again) again.onclick = async () => { const cc = Object.assign({}, c, { autofill: null }); renderRealSubmit(cc, area); };
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-copy]"); if (!b) return;
  navigator.clipboard?.writeText(b.dataset.copy).then(() => toast("已複製指令", "copy"), () => toast("複製失敗，請手動選取", "alert"));
});

async function runFill(c, area) {
  $("#btn-autofill", area).remove();
  const items = $$(".checklist li", area);
  for (let i = 0; i < FILL_STEPS.length; i++) {
    items[i].className = "run"; items[i].querySelector(".ico").innerHTML = ic("refresh");
    await sleep(520);
    items[i].className = "done"; items[i].querySelector(".ico").innerHTML = ic("check");
  }
  const last = items[items.length - 1]; last.className = "run"; last.querySelector(".ico").innerHTML = ic("refresh");
  area.insertAdjacentHTML("beforeend", `
    <div class="verify">
      <div class="verify-title"><span class="ic-bubble amber" style="width:34px;height:34px;border-radius:11px">${ic("shield", "xs")}</span>請完成人機驗證</div>
      <div class="captcha-row"><canvas id="captcha" width="140" height="48" title="點一下換一張"></canvas>
        <input id="captcha-input" placeholder="驗證碼" maxlength="4" inputmode="numeric" autocomplete="off">
        <button class="btn soft small" id="btn-recap" title="換一張">${ic("refresh")}</button></div>
      <label class="check-row"><input type="checkbox" id="mail-ok"> ${ic("mail", "xs")} 我已完成 Gmail／簡訊認證（部分縣市需要）</label>
      <div class="actions left"><button class="btn primary" id="btn-final">${ic("send")}確認送出檢舉</button><span class="muted small" id="verify-msg"></span></div>
    </div>`);
  const cv = $("#captcha", area);
  let code = drawCaptcha(cv);
  const re = () => { code = drawCaptcha(cv); };
  cv.onclick = re; $("#btn-recap", area).onclick = re;
  $("#captcha-input", area).focus();
  $("#btn-final", area).onclick = async () => {
    const inp = $("#captcha-input", area);
    if (inp.value.trim() !== code) {
      $("#verify-msg", area).textContent = "驗證碼錯誤，請再試一次";
      inp.classList.remove("shake"); void inp.offsetWidth; inp.classList.add("shake");
      re(); inp.value = ""; return;
    }
    $("#btn-final", area).disabled = true;
    try {
      const done = await postJSON(`/api/cases/${encodeURIComponent(c.id)}/submit`, { verify: $("#mail-ok", area).checked ? "captcha+email" : "captcha" });
      state.caseObj = done; state.caseStatus[done.id] = "submitted";
      renderSuccess(done, area, true); toast("檢舉已送出（模擬）", "sparkles"); saveDraft();
    } catch (e) { toast(e.message, "alert"); $("#btn-final", area).disabled = false; }
  };
}
function drawCaptcha(cv) {
  const ctx = cv.getContext("2d"), code = String(Math.floor(1000 + Math.random() * 9000));
  const g = ctx.createLinearGradient(0, 0, cv.width, cv.height); g.addColorStop(0, "#f3f5ff"); g.addColorStop(1, "#f7f0ff");
  ctx.fillStyle = g; ctx.fillRect(0, 0, cv.width, cv.height);
  for (let i = 0; i < 6; i++) {
    ctx.strokeStyle = `hsla(${Math.random() * 360},60%,65%,.7)`; ctx.lineWidth = 1.5; ctx.beginPath();
    ctx.moveTo(Math.random() * cv.width, Math.random() * cv.height); ctx.bezierCurveTo(Math.random() * cv.width, Math.random() * cv.height, Math.random() * cv.width, Math.random() * cv.height, Math.random() * cv.width, Math.random() * cv.height); ctx.stroke();
  }
  ctx.font = "bold 28px Consolas, monospace"; ctx.textBaseline = "middle";
  [...code].forEach((ch, i) => {
    ctx.save(); ctx.translate(18 + i * 29, 25); ctx.rotate((Math.random() - 0.5) * 0.5);
    ctx.fillStyle = `hsl(${220 + Math.random() * 80},55%,35%)`; ctx.fillText(ch, 0, 0); ctx.restore();
  });
  return code;
}
function renderSuccess(c, area, animate) {
  area.innerHTML = `<div class="success">
    <svg class="check-anim" viewBox="0 0 100 100" ${animate ? "" : 'style="--d:0"'}><circle cx="50" cy="50" r="45"/><path d="M30 52 l13 13 l27 -28"/></svg>
    ${c.submit.simulated === false ? `<h3>已透過 autoForm 送出</h3>
    <div class="muted small">${esc(c.submit.message || "受理編號會寄到檢舉人的 Email，請到信箱確認（部分縣市要點信中連結才算完成）。")}</div>
    <div class="muted small">送出時間 ${esc(c.submit.time)}</div></div>` : `<h3>檢舉已送出（模擬）</h3>
    <div class="muted small">受理編號</div>
    <div class="receipt">${esc(c.submit.receipt)}</div>
    <div class="muted small">送出時間 ${esc(c.submit.time)}・驗證方式 ${esc(c.submit.verify)}</div></div>`}`;
}

/* ============================ 案件紀錄 ============================ */
async function loadDrafts() {
  const list = (await api("/api/drafts")).filter((d) => !d.finished);
  if (!list.length) { $("#draft-list").innerHTML = `<div class="empty-mini">沒有未完成的檢舉</div>`; return; }
  const cur = state.draft && state.draft.id;
  $("#draft-list").innerHTML = `<div class="case-grid">${list.map((d, i) => {
    const prog = d.job_status === "done" ? `${d.violations} 筆違規・已送出 ${d.submitted}・待處理 ${d.pending}`
      : d.job_status === "interrupted" ? "分析中斷，需要重新分析" : d.job_status ? "分析中…" : "尚未分析";
    return `<div class="case-card draft-card" style="--i:${i}">
      ${d.thumb ? `<img src="${d.thumb}" alt="" loading="lazy">` : `<div class="ph">${ic("video")}</div>`}
      <div class="body">
        <div class="top"><h3>${esc(d.video_name || "未命名")}</h3><span class="step-chip">${d.id === cur ? "目前・" : ""}${STEP_NAMES[d.step] || ""}</span></div>
        <div class="sub"><span>${ic("history", "xs")} ${esc(d.updated)}</span>${d.place ? `<span>${ic("pin", "xs")} ${esc(d.place)}</span>` : ""}</div>
        <div class="sub"><b>${prog}</b></div>
        <div class="actions-row">
          <button class="btn primary" data-resume="${esc(d.id)}">${ic("right")}繼續</button>
          <button class="btn danger-soft" data-del="${esc(d.id)}">${ic("trash")}刪除</button>
        </div>
      </div></div>`;
  }).join("")}</div>`;
  $$("#draft-list [data-resume]").forEach((b) => b.addEventListener("click", () => {
    if (b.dataset.resume === cur) showView("new"); else openDraft(b.dataset.resume);
  }));
  $$("#draft-list [data-del]").forEach((b) => b.addEventListener("click", async () => {
    if (!b.dataset.armed) { b.dataset.armed = "1"; b.innerHTML = `${ic("trash")}確定刪除？`; return; }
    await postJSON(`/api/drafts/${encodeURIComponent(b.dataset.del)}/delete`, {});
    if (b.dataset.del === cur) { ls.set(null); location.href = "/?new=1"; return; }
    toast("已刪除草稿（已產生的案件不受影響）", "trash"); loadDrafts();
  }));
}
async function loadCases() {
  loadDrafts();
  const list = await api("/api/cases");
  if (!list.length) {
    $("#case-list").innerHTML = `<div class="card empty-state"><span class="ic-bubble blue">${ic("folder")}</span><h3>還沒有案件</h3><p>先到「新增檢舉」上傳一支行車影片吧。</p>
      <button class="btn primary" data-view-link="new">${ic("plus")}新增檢舉</button></div>`;
    return;
  }
  $("#case-list").innerHTML = `<div class="case-grid">${list.map((c, i) => {
    const hasImg = (c.files || []).includes("evidence.jpg");
    return `<div class="case-card" data-case="${esc(c.id)}" style="--i:${i}">
      ${hasImg ? `<img src="${c.base_url}evidence.jpg" alt="" loading="lazy">` : ""}
      <div class="body">
        <div class="top"><h3>${esc(c.violation.label)}</h3><span class="status ${c.status}">${c.status === "submitted" ? "已送出" : "待送出"}</span></div>
        <div><span class="plate">${esc(c.fields.plate)}</span></div>
        <div class="sub"><span>${ic("clock", "xs")} ${esc(c.fields.date)} ${esc(c.fields.time)}</span><span>${ic("pin", "xs")} ${esc(c.city)}</span></div>
      </div></div>`;
  }).join("")}</div>`;
  $$("#case-list [data-case]").forEach((el) => el.addEventListener("click", async () => {
    const c = await api(`/api/cases/${encodeURIComponent(el.dataset.case)}`);
    openModal(`<h2>${ic("folder")}${esc(c.id)}</h2><div id="case-modal"></div>`);
    renderSubmit(c, $("#case-modal"), true);
  }));
}

/* ============================ 檢舉人資料 ============================ */
async function loadProfile() {
  state.profile = await api("/api/profile");
  const f = $("#profile-form");
  Object.entries(state.profile).forEach(([k, v]) => { if (f.elements[k]) f.elements[k].value = v; });
}
$("#profile-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  state.profile = await postJSON("/api/profile", Object.fromEntries(new FormData(e.target).entries()));
  $("#profile-saved").textContent = "已儲存"; renderReporter(); toast("檢舉人資料已儲存");
});

/* ============================ Modal ============================ */
function openModal(html) { $("#modal-body").innerHTML = html; $("#modal").classList.remove("hidden"); }
function closeModal() { $$("#modal video").forEach((v) => v.pause()); $("#modal").classList.add("hidden"); $("#modal-body").innerHTML = ""; $(".modal-card").classList.remove("narrow-modal"); }
$("#modal-x").onclick = closeModal;
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

/* ============================ 登入／註冊 ============================ */
const AUTH_KEY = "trafficReport.token";
const tokenStore = {
  get() { try { return localStorage.getItem(AUTH_KEY) || sessionStorage.getItem(AUTH_KEY); } catch (e) { return null; } },
  set(t, remember = true) {
    try {
      localStorage.removeItem(AUTH_KEY); sessionStorage.removeItem(AUTH_KEY);
      if (t) (remember ? localStorage : sessionStorage).setItem(AUTH_KEY, t);
    } catch (e) { /* 無法使用瀏覽器儲存時，重新整理後需要再登入 */ }
    tokenStore.cookie(t);
  },
  // 影片、截圖用 <video>/<img> 載入，帶不了標頭，所以 token 也放一份在 cookie
  cookie(t) { document.cookie = t ? `tr_token=${encodeURIComponent(t)}; path=/; SameSite=Strict` : "tr_token=; path=/; max-age=0"; },
};
let appStarted = false, authMode = "login";

(async function boot() {
  const t = tokenStore.get();
  let user = null;
  if (t) {
    tokenStore.cookie(t);
    try { user = (await api("/api/auth/me", { headers: { "X-Auth-Token": t } })).user; }
    catch (e) { tokenStore.set(null); }
  }
  if (user) enterApp(user); else showAuth();
})();

function showAuth() {
  document.body.className = "";
  setAuthMode("login");
  setTimeout(() => $("#auth-form [name=email]").focus(), 100);
}
function enterApp(user) {
  state.user = user;
  document.body.className = "authed";
  renderUserMenu();
  if (!appStarted) { appStarted = true; startApp(); }
}
function setAuthMode(mode) {
  authMode = mode;
  const card = $(".auth-card"); card.dataset.mode = mode;
  const reg = mode === "register";
  $("#auth-title").textContent = reg ? "建立帳號" : "歡迎回來";
  $("#auth-sub").textContent = reg ? "註冊後就能開始上傳影片檢舉" : "登入以繼續你的檢舉";
  $("#auth-submit").innerHTML = reg ? "建立帳號" : "登入";
  $("#auth-switch").innerHTML = reg ? `已經有帳號了？<a class="link" data-switch="login">直接登入</a>` : `還沒有帳號？<a class="link" data-switch="register">立即註冊</a>`;
  $$("#auth-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  $("#auth-form [name=password]").autocomplete = reg ? "new-password" : "current-password";
  authError("");
}
$$("#auth-tabs button").forEach((b) => b.addEventListener("click", () => setAuthMode(b.dataset.mode)));
document.addEventListener("click", (e) => { const a = e.target.closest("[data-switch]"); if (a) setAuthMode(a.dataset.switch); });

function authError(msg, field) {
  $$("#auth-form .input-err").forEach((x) => x.classList.remove("input-err"));
  const box = $("#auth-error");
  if (!msg) { box.classList.add("hidden"); return; }
  box.querySelector("span").textContent = msg; box.classList.remove("hidden");
  box.classList.remove("shake"); void box.offsetWidth; box.classList.add("shake");
  if (field) { const inp = $(`#auth-form [name=${field}]`); inp.closest(".input-ic").classList.add("input-err"); inp.focus(); }
}
$(".input-ic .eye").addEventListener("click", (e) => {
  const inp = $("#auth-form [name=password]"), show = inp.type === "password";
  inp.type = show ? "text" : "password";
  e.currentTarget.innerHTML = ic(show ? "eyeoff" : "eye");
});
function pwScore(pw) {
  if (!pw) return 0;
  let sc = 0;
  if (pw.length >= 6) sc++;
  if (pw.length >= 10) sc++;
  if (/\d/.test(pw) && /[a-zA-Z]/.test(pw)) sc++;
  if (/[^a-zA-Z0-9]/.test(pw) || (/[a-z]/.test(pw) && /[A-Z]/.test(pw))) sc++;
  return Math.max(1, sc);
}
$("#auth-form [name=password]").addEventListener("input", (e) => {
  const sc = pwScore(e.target.value), m = $("#pw-meter");
  m.dataset.s = e.target.value ? sc : 0;
  m.querySelector("span").textContent = e.target.value ? ["", "弱", "普通", "良好", "強"][sc] : "";
});

$("#auth-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = Object.fromEntries(new FormData(e.target).entries());
  const reg = authMode === "register";
  if (reg && !f.name.trim()) return authError("請輸入姓名", "name");
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(f.email.trim())) return authError("Email 格式不正確", "email");
  if ((f.password || "").length < 6) return authError("密碼至少要 6 個字元", "password");
  if (reg && f.password !== f.password2) return authError("兩次輸入的密碼不一樣", "password2");
  if (reg && !e.target.elements.agree.checked) return authError("請先勾選同意使用條款");
  authError("");
  const btn = $("#auth-submit"), label = btn.innerHTML;
  btn.disabled = true; btn.innerHTML = `<span class="spinner"></span>${reg ? "建立中…" : "登入中…"}`;
  try {
    const res = await postJSON(`/api/auth/${reg ? "register" : "login"}`, reg
      ? { name: f.name, email: f.email, password: f.password } : { email: f.email, password: f.password });
    await finishLogin(res, reg ? true : $("#remember").checked, reg);
  } catch (err) {
    authError(err.message, reg && err.message.includes("Email") ? "email" : !reg ? "password" : null);
  } finally { btn.disabled = false; btn.innerHTML = label; }
});

async function finishLogin(res, remember, isNew) {
  tokenStore.set(res.token, remember);
  // 第一次登入：把姓名與 Email 帶進檢舉人資料
  try {
    const prof = await api("/api/profile");
    if (!prof.reporter_name || !prof.reporter_email) {
      await postJSON("/api/profile", { ...prof, reporter_name: prof.reporter_name || res.user.name, reporter_email: prof.reporter_email || res.user.email });
    }
  } catch (e) { /* 不影響登入 */ }
  $("#auth-form").reset(); $("#pw-meter").dataset.s = 0; $("#pw-meter span").textContent = "";
  enterApp(res.user);
  toast(isNew ? `註冊成功，歡迎 ${res.user.name}` : `歡迎回來，${res.user.name}`, "sparkles");
}

$("#forgot").addEventListener("click", async () => {
  const email = $("#auth-form [name=email]").value.trim();
  try { await postJSON("/api/auth/reset", { email }); toast(`（模擬）已寄出重設密碼信到 ${email}`, "mail"); }
  catch (err) { authError(err.message, "email"); }
});

/* Google 登入（模擬）：跳出選擇帳號的視窗 */
$("#btn-google").addEventListener("click", () => {
  openModal(`<div style="max-width:420px;margin:0 auto">
    <div style="text-align:center;margin-bottom:18px"><span class="g-mark" style="width:44px;height:44px;font-size:22px;margin:0 auto 10px"></span>
      <h2 style="justify-content:center;padding:0;margin-bottom:4px">選擇帳號</h2><p class="muted small" style="margin:0">以繼續使用「馬路三寶」・模擬 Google 登入</p></div>
    <div class="g-chooser">
      <button class="g-account" data-g-email="demo.user@gmail.com" data-g-name="示範使用者"><span class="avatar">示</span><div><b>示範使用者</b><small>demo.user@gmail.com</small></div></button>
      <button class="g-account" id="g-other-btn"><span class="avatar" style="background:#eef0f7;color:var(--muted)">${ic("plus", "xs")}</span><div><b>使用其他帳號</b><small>輸入 Google 帳號的 Email</small></div></button>
      <form class="g-other form hidden" id="g-other-form">
        <label>姓名<input name="name" placeholder="王小明"></label>
        <label>Gmail<input name="email" type="email" placeholder="you@gmail.com"></label>
        <button class="btn primary full" type="submit">繼續</button>
      </form>
    </div></div>`);
  $("#modal-body .g-mark").textContent = "G";
  $(".modal-card").classList.add("narrow-modal");
  $$("[data-g-email]").forEach((b) => b.addEventListener("click", () => googleLogin(b.dataset.gEmail, b.dataset.gName)));
  $("#g-other-btn").addEventListener("click", () => { $("#g-other-form").classList.remove("hidden"); $("#g-other-form [name=name]").focus(); });
  $("#g-other-form").addEventListener("submit", (e) => {
    e.preventDefault(); const f = Object.fromEntries(new FormData(e.target).entries());
    googleLogin(f.email, f.name);
  });
});
async function googleLogin(email, name) {
  try {
    const res = await postJSON("/api/auth/google", { email, name });
    closeModal();
    await finishLogin(res, true, false);
  } catch (err) { toast(err.message, "alert"); }
}

/* 右上角帳號選單 */
function renderUserMenu() {
  const u = state.user || {}, initial = (u.name || "?").slice(0, 1).toUpperCase();
  $("#user-avatar").textContent = initial; $("#ud-avatar").textContent = initial;
  $("#ud-name").textContent = u.name || ""; $("#ud-email").textContent = u.email || "";
  $("#ud-provider").textContent = u.provider === "google" ? "Google 帳號（模擬）" : "Email 帳號";
}
$("#user-btn").addEventListener("click", (e) => { e.stopPropagation(); $("#user-dropdown").classList.toggle("hidden"); });
document.addEventListener("click", (e) => { if (!e.target.closest("#user-menu")) $("#user-dropdown").classList.add("hidden"); });
$("#user-dropdown").addEventListener("click", (e) => { if (e.target.closest(".ud-item")) $("#user-dropdown").classList.add("hidden"); });
$("#btn-logout").addEventListener("click", async () => {
  try { await api("/api/auth/logout", { method: "POST", headers: { "X-Auth-Token": tokenStore.get() || "" } }); } catch (e) { /* 照樣登出 */ }
  tokenStore.set(null);
  location.href = "/";
});

/* ============================ 初始化（登入後） ============================ */
async function startApp() {
  initMap();
  setMode(state.mode, false);
  try {
    [state.presets, state.cities, state.profile] = await Promise.all([api("/api/presets"), api("/api/cities"), api("/api/profile")]);
  } catch (e) { toast("無法連到伺服器", "alert"); return; }
  $("#preset-select").innerHTML = state.presets.map((p, i) => `<option value="${i}">${esc(p.name)}</option>`).join("");
  const params = new URLSearchParams(location.search);
  const wanted = params.get("new") ? null : params.get("draft") || ls.get();
  if (location.search) history.replaceState(null, "", "/");
  let restored = false;
  if (wanted) {
    try { await restoreDraft(await api(`/api/drafts/${encodeURIComponent(wanted)}`)); restored = true; }
    catch (e) { ls.set(null); }
  }
  if (!restored) {
    const p0 = state.presets[0]; if (p0) await setPoint(p0.lat, p0.lon, "preset");
    $("#recorded-at").value = toLocalInput(new Date());
    goStep(1);
    renderResumeBanner();
  } else if (state.video) toast("已還原上次的進度", "history");
}

async function restoreDraft(d) {
  state.restoring = true;
  state.draft = d; ls.set(d.id);
  Object.assign(state, {
    ignored: new Set(d.ignored || []), filter: d.filter || "all", forms: d.forms || {}, cases: d.cases || {},
    maxStep: d.max_step || 1,
  });
  if (d.video) { state.video = d.video; showVideo(d.video); }
  if (d.recorded_at) $("#recorded-at").value = d.recorded_at; else $("#recorded-at").value = toLocalInput(new Date());
  if (d.speed) $("#speed").value = d.speed;
  if (d.max_seconds != null) $("#max-seconds").value = d.max_seconds;
  if (d.mode) setMode(d.mode, false);
  if (d.preset_index != null) $("#preset-select").value = d.preset_index;
  if (d.gps && d.gps.lat != null) {
    state.gps = d.gps;
    switchGpsTab(d.gps_tab || "preset");
    if (d.gps_tab === "manual") { $("#lat-input").value = d.gps.lat; $("#lon-input").value = d.gps.lon; }
    if (d.gps.file) $("#gps-file-name").textContent = d.gps.file;
    renderGpsInfo(); drawMap();
  } else {
    const p0 = state.presets[0]; if (p0) { state.restoring = false; await setPoint(p0.lat, p0.lon, "preset"); state.restoring = true; }
  }
  refreshAnalyzeBtn();
  $$("#filter button").forEach((x) => x.classList.toggle("active", x.dataset.f === state.filter));

  let step = d.step || 1;
  const done = () => state.job && state.job.status === "done";
  if (d.job_id) {
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(d.job_id)}`);
      state.job = job;
      state.jobStartedAt = job.started_ts ? job.started_ts * 1000 : Date.now();
      resetAnalysisUI(); renderStages(job); renderModels(job);
      applyJob(job, true);
      if (job.status === "running" || job.status === "queued") { step = Math.min(step, 2); startPolling(); }
    } catch (e) { state.job = null; }
  }
  if (!state.job) { step = 1; state.maxStep = 1; }
  if (step >= 3 && !done()) step = 2;
  if (step >= 4) {
    const v = done() && d.selected ? state.job.violations.find((x) => x.id === d.selected) : null;
    if (!v) step = 3;
    else {
      fillCaseForm(v);
      if (step === 5) {
        try {
          const c = await api(`/api/cases/${encodeURIComponent(state.cases[v.id])}`);
          state.caseObj = c; state.caseStatus[c.id] = c.status; renderSubmit(c, $("#submit-panel"));
        } catch (e) { step = 4; }
      }
    }
  }
  showDraftBar(); setSaveState("saved", d.updated);
  state.restoring = false;
  goStep(step);
}

async function renderResumeBanner() {
  let list = [];
  try { list = (await api("/api/drafts")).filter((d) => !d.finished); } catch (e) { return; }
  if (!list.length || state.video) return;
  const d = list[0], el = $("#resume-banner");
  el.innerHTML = `<span class="ic-bubble violet">${ic("history")}</span>
    <div class="txt"><b>你有 ${list.length} 筆未完成的檢舉</b><span>最近一筆：${esc(d.video_name || "未命名")}・停在「${STEP_NAMES[d.step] || ""}」・${esc(d.updated)}</span></div>
    <button class="btn ghost small" data-view-link="cases">${ic("folder")}全部</button>
    <button class="btn primary small" id="btn-resume-last">${ic("right")}繼續上次</button>`;
  el.classList.remove("hidden");
  $("#btn-resume-last").onclick = () => openDraft(d.id);
}
