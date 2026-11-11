"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let STATE = null, currentSet = null, currentQuestions = [];

async function api(path, opts = {}) {
  const init = { method: opts.method || (opts.body ? "POST" : "GET"), headers: {} };
  if (opts.body instanceof FormData) init.body = opts.body;
  else if (opts.body) { init.body = JSON.stringify(opts.body); init.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, init);
  let data = {};
  try { data = await r.json(); } catch { data = { ok: false, error: `Server error (${r.status})` }; }
  if (!data.ok) throw new Error(data.error || "Something went wrong.");
  return data;
}
function msg(el, text, kind = "") { el.textContent = text; el.className = "msg" + (kind ? " " + kind : ""); }
let toastTimer;
function toast(text, err = false) {
  const t = $("toast"); t.textContent = text; t.className = "toast" + (err ? " err" : "");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add("hidden"), 4000);
}
async function busy(btn, fn) {
  const label = btn.textContent; btn.disabled = true;
  try { return await fn(); } finally { btn.disabled = false; btn.textContent = label; }
}

/* ---------------- tabs ---------------- */
function showTab(name) {
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("hidden", t.id !== "tab-" + name));
  document.querySelectorAll(".side a").forEach(a => a.classList.toggle("active", a.dataset.tab === name));
  if (name === "golive") refreshGoLive();
  if (name === "history") loadHistory();
}
window.addEventListener("hashchange", () => showTab(location.hash.slice(1) || "setup"));

/* ---------------- state ---------------- */
async function loadState() {
  STATE = await api("/api/state");
  const { llm, youtube: yt, quota, remote, checks } = STATE;
  // LLM
  $("llmProvider").value = llm.provider || "anthropic";
  $("llmKey").placeholder = llm.has_key ? "Saved key (leave blank to keep it)" : (llm.provider === "openai" ? "sk-…" : "sk-ant-…");
  setBadge("llmBadge", llm.model ? `${llm.provider === "openai" ? "OpenAI" : "Claude"} · ${llm.model}` : (llm.has_key ? "Pick a model" : "Not set"), !!llm.model);
  // YouTube
  $("redirectUri").textContent = yt.redirect_uri;
  if (yt.client_id) $("gcid").value = yt.client_id;
  if (yt.client_configured) $("gsecret").placeholder = "Saved (leave blank to keep it)";
  $("ytSignin").disabled = !yt.client_configured;
  $("ytCheck").disabled = $("ytSignout").disabled = !yt.signed_in;
  $("ytHowto").open = !yt.client_configured;
  let ytLabel = "Not connected", ytOk = false;
  if (yt.signed_in && yt.channel) { ytLabel = yt.channel.title + (yt.live_enabled === false ? " · live not enabled" : ""); ytOk = yt.live_enabled !== false; }
  else if (yt.client_configured) ytLabel = "Sign in next";
  setBadge("ytBadge", ytLabel, ytOk);
  if (quota) {
    $("quotaLimit").value = quota.limit;
    $("quotaInfo").textContent = `${quota.remaining.toLocaleString()} units left today, enough for about ${quota.questions_possible} questions.`;
  }
  // remote
  $("remoteOn").checked = !!remote.enabled;
  setBadge("remoteBadge", remote.enabled ? "On" : "Off", remote.enabled);
  $("remoteInfo").classList.toggle("hidden", !remote.enabled);
  $("remoteUrl").textContent = remote.url; $("remotePin").textContent = remote.pin || "";
  $("remoteRestart").classList.toggle("hidden", !(remote.enabled && !remote.bound_lan));
  $("ffmpegPanel").classList.toggle("hidden", checks.ffmpeg);
  const u = STATE.update || {};
  $("versionBadge").textContent = u.current ? `v${u.current}` : "";
  $("updatesOn").checked = !!u.enabled;
  const showBanner = u.enabled && u.update_available && !sessionStorage.getItem("qc-dismissed-" + u.latest);
  $("updateBanner").classList.toggle("hidden", !showBanner);
  if (showBanner) {
    $("updateText").textContent = `QuizCaster v${u.latest} is out. You have v${u.current}.`;
    $("updateLink").href = u.url || "#";
  }
  $("setupDot").classList.toggle("need", !(llm.model && yt.signed_in));
  renderSets();
  applyStatus(STATE.session);
}
function setBadge(id, text, good) { const b = $(id); b.textContent = text; b.className = "badge" + (good ? " good" : ""); }

/* ---------------- setup: LLM ---------------- */
$("llmVerify").onclick = (e) => busy(e.target, async () => {
  e.target.textContent = "Checking…"; msg($("llmMsg"), "");
  try {
    const r = await api("/api/llm/verify", { body: { provider: $("llmProvider").value, api_key: $("llmKey").value } });
    $("llmModel").innerHTML = r.models.map(m => `<option value="${esc(m.id)}">${esc(m.name)}</option>`).join("");
    if (STATE?.llm?.model) $("llmModel").value = STATE.llm.model;
    $("modelRow").classList.remove("hidden"); $("llmKey").value = "";
    msg($("llmMsg"), `Key works. ${r.models.length} models available. Pick one.`, "ok");
  } catch (err) { msg($("llmMsg"), err.message, "err"); }
});
$("llmSave").onclick = async () => {
  try { await api("/api/llm/model", { body: { model: $("llmModel").value } }); msg($("llmMsg"), "Model saved.", "ok"); await loadState(); }
  catch (err) { msg($("llmMsg"), err.message, "err"); }
};

$("updateLater").onclick = () => { sessionStorage.setItem("qc-dismissed-" + STATE.update.latest, "1"); $("updateBanner").classList.add("hidden"); };
$("updatesOn").onchange = async () => { await api("/api/updates", { body: { enabled: $("updatesOn").checked } }); loadState(); };

/* ---------------- setup: YouTube ---------------- */
$("gSave").onclick = async () => {
  const secret = $("gsecret").value || (STATE.youtube.client_configured ? null : "");
  if (secret === null && $("gcid").value === STATE.youtube.client_id) { msg($("ytMsg"), "Already saved.", "ok"); return; }
  try { await api("/api/youtube/client", { body: { client_id: $("gcid").value, client_secret: $("gsecret").value } });
        $("gsecret").value = ""; msg($("ytMsg"), "Saved. Now sign in with Google.", "ok"); await loadState(); }
  catch (err) { msg($("ytMsg"), err.message, "err"); }
};
$("ytSignin").onclick = async () => {
  try { const r = await api("/api/youtube/auth"); location.href = r.url; } catch (err) { msg($("ytMsg"), err.message, "err"); }
};
$("ytCheck").onclick = (e) => busy(e.target, async () => {
  e.target.textContent = "Checking…";
  try { const r = await api("/api/youtube/check", { body: {} }); msg($("ytMsg"), `${r.channel.title}: ${r.message}`, r.live_enabled ? "ok" : "err"); await loadState(); }
  catch (err) { msg($("ytMsg"), err.message, "err"); }
});
$("ytSignout").onclick = async () => { await api("/api/youtube/signout", { body: {} }); msg($("ytMsg"), "Signed out."); loadState(); };
$("quotaSave").onclick = async () => {
  try { await api("/api/youtube/quota", { body: { daily_limit: +$("quotaLimit").value } }); toast("Quota updated"); loadState(); }
  catch (err) { toast(err.message, true); }
};
$("remoteOn").onchange = async () => { await api("/api/remote-settings", { body: { enabled: $("remoteOn").checked } }); loadState(); };
$("remoteNewPin").onclick = async () => { await api("/api/remote-settings", { body: { enabled: true, new_pin: true } }); toast("New PIN made. Phones need to sign in again."); loadState(); };

/* ---------------- questions ---------------- */
function renderSets() {
  const sets = STATE.sets;
  $("setList").innerHTML = sets.length ? sets.map(s => `
    <button class="setitem ${currentSet === s.id ? "sel" : ""}" data-id="${s.id}">
      <span class="name">${esc(s.name)}</span>
      <span class="meta">${s.source === "generated" ? "AI" : "Uploaded"} · ${s.approved}/${s.total} approved</span>
    </button>`).join("") : `<p class="help">No question sets yet. Generate some with AI or upload your own above.</p>`;
  $("setList").querySelectorAll(".setitem").forEach(b => b.onclick = () => openSet(+b.dataset.id));
}
async function openSet(id) {
  currentSet = id;
  const r = await api(`/api/sets/${id}`);
  currentQuestions = r.questions;
  $("editor").classList.remove("hidden");
  $("edTitle").textContent = r.set.name;
  $("edMore").classList.toggle("hidden", !STATE.llm.model);
  renderQuestions(); renderSets();
}
function renderQuestions() {
  const n = currentQuestions.length, a = currentQuestions.filter(q => q.approved).length;
  $("edCount").textContent = `${a} of ${n} approved. Check each answer before approving: AI can be confidently wrong.`;
  $("qlist").innerHTML = currentQuestions.map((q, i) => `
    <div class="q ${q.approved ? "approved" : ""}" data-id="${q.id}">
      <div class="q-top"><span class="q-num">${i + 1}</span><span class="q-text">${esc(q.question)}</span>
        <span class="q-actions">
          <button class="btn ${q.approved ? "ghost" : ""}" data-act="toggle">${q.approved ? "Unapprove" : "Approve"}</button>
          <button class="btn ghost" data-act="edit">Edit</button>
          <button class="btn ghost danger" data-act="del" aria-label="Delete question ${i + 1}">Delete</button>
        </span></div>
      <div class="q-opts">${["A","B","C","D"].map((l, j) => `<span class="q-opt ${q.answer === l ? "right" : ""}"><span class="k" style="background:var(--${l.toLowerCase()})">${l}</span>${esc(q.options[j])}</span>`).join("")}</div>
      <div class="q-exp">${esc(q.explanation)}</div>
    </div>`).join("");
  $("qlist").querySelectorAll("[data-act]").forEach(b => b.onclick = () => questionAction(b));
}
async function questionAction(btn) {
  const card = btn.closest(".q"), id = +card.dataset.id, q = currentQuestions.find(x => x.id === id);
  const act = btn.dataset.act;
  try {
    if (act === "toggle") {
      await api(`/api/sets/${currentSet}/approve`, { body: { approved: !q.approved, ids: [id] } });
      q.approved = !q.approved; renderQuestions(); loadState();
    } else if (act === "del") {
      if (!confirm("Delete this question?")) return;
      await api(`/api/questions/${id}`, { method: "DELETE" });
      currentQuestions = currentQuestions.filter(x => x.id !== id); renderQuestions(); loadState();
    } else if (act === "edit") {
      card.innerHTML = `<div class="q-edit">
        <label>Question <textarea rows="2" data-f="question">${esc(q.question)}</textarea></label>
        <div class="grid4">${["A","B","C","D"].map((l, j) => `<label>Option ${l} <input data-f="o${j}" value="${esc(q.options[j])}"></label>`).join("")}</div>
        <label>Correct answer <select data-f="answer">${["A","B","C","D"].map(l => `<option ${q.answer === l ? "selected" : ""}>${l}</option>`).join("")}</select></label>
        <label>Why it's correct <textarea rows="2" data-f="explanation">${esc(q.explanation)}</textarea></label>
        <div class="row tight"><button class="btn primary" data-save>Save and approve</button><button class="btn ghost" data-cancel>Cancel</button></div>
        <p class="msg" data-msg></p></div>`;
      card.querySelector("[data-cancel]").onclick = renderQuestions;
      card.querySelector("[data-save]").onclick = async () => {
        const f = (k) => card.querySelector(`[data-f="${k}"]`).value;
        try {
          const r = await api(`/api/questions/${id}`, { method: "PUT", body: { question: f("question"), options: [f("o0"), f("o1"), f("o2"), f("o3")], answer: f("answer"), explanation: f("explanation"), approved: true } });
          Object.assign(q, r.question); renderQuestions(); loadState();
        } catch (err) { msg(card.querySelector("[data-msg]"), err.message, "err"); }
      };
    }
  } catch (err) { toast(err.message, true); }
}
$("edApproveAll").onclick = async () => { await api(`/api/sets/${currentSet}/approve`, { body: { approved: true } }); currentQuestions.forEach(q => q.approved = true); renderQuestions(); loadState(); };
$("edUnapproveAll").onclick = async () => { await api(`/api/sets/${currentSet}/approve`, { body: { approved: false } }); currentQuestions.forEach(q => q.approved = false); renderQuestions(); loadState(); };
$("edDelete").onclick = async () => {
  if (!confirm("Delete this whole question set?")) return;
  await api(`/api/sets/${currentSet}`, { method: "DELETE" }); currentSet = null; $("editor").classList.add("hidden"); loadState();
};
$("edMore").onclick = () => startGeneration({ set_id: currentSet, count: 10 });
$("genGo").onclick = () => startGeneration({ topic: $("genTopic").value, count: +$("genCount").value, difficulty: $("genDiff").value });

let activeJob = null;
async function startGeneration(body) {
  try {
    const r = await api("/api/sets/generate", { body });
    activeJob = r.job.id;
    $("genProg").classList.remove("hidden"); $("genProg").firstElementChild.style.width = "3%";
    msg($("genMsg"), `Writing ${r.job.total} questions about ${r.job.topic}…`);
    if (location.hash !== "#questions") location.hash = "#questions";
  } catch (err) { msg($("genMsg"), err.message, "err"); }
}
async function onGen(job) {
  if (job.id !== activeJob) return;
  $("genProg").firstElementChild.style.width = `${Math.max(3, 100 * job.done / job.total)}%`;
  if (job.state === "running") { msg($("genMsg"), `Written ${job.done} of ${job.total}…`); return; }
  activeJob = null; $("genProg").classList.add("hidden");
  if (job.state === "error") { msg($("genMsg"), job.error, "err"); return; }
  msg($("genMsg"), `Done: ${job.done} questions. Review and approve them below.` + (job.warnings?.length ? ` (${job.warnings.length} skipped)` : ""), "ok");
  await loadState(); openSet(job.set_id);
}

$("upGo").onclick = (e) => busy(e.target, async () => {
  const f = $("upFile").files[0];
  if (!f) { msg($("upMsg"), "Choose a file first.", "err"); return; }
  const fd = new FormData(); fd.append("file", f); fd.append("name", $("upName").value || f.name.replace(/\.[^.]+$/, ""));
  try {
    const r = await api("/api/sets/upload", { body: fd });
    msg($("upMsg"), `Imported ${r.imported} questions.` + (r.errors.length ? ` Skipped ${r.errors.length}: ${r.errors.slice(0, 3).join("; ")}` : ""), r.errors.length ? "err" : "ok");
    await loadState(); openSet(r.set_id);
  } catch (err) { msg($("upMsg"), err.message, "err"); }
});

/* ---------------- go live ---------------- */
function modeValue() { return document.querySelector('input[name="mode"]:checked').value; }
function refreshGoLive() {
  if (!STATE) return;
  const sets = STATE.sets.filter(s => s.approved > 0), prev = $("glSet").value;
  $("glSet").innerHTML = sets.length ? sets.map(s => `<option value="${s.id}" data-topic="${esc(s.topic || s.name)}">${esc(s.name)} (${s.approved} approved)</option>`).join("")
    : `<option value="">No approved questions yet</option>`;
  if (prev && sets.some(s => String(s.id) === prev)) $("glSet").value = prev;
  if (!$("glTheme").options.length) { $("glTheme").innerHTML = STATE.themes.map(t => `<option value="${t}">${t[0].toUpperCase() + t.slice(1)}</option>`).join(""); $("glTheme").value = "dark"; }
  const q = STATE.quota;
  $("glQuota").textContent = q ? `${q.remaining.toLocaleString()} of ${q.limit.toLocaleString()} units left. Each question uses up to ~${q.per_question}, so today allows about ${q.questions_possible} questions. Previews use none.` : "";
  updateGoLiveForm(true);
}
async function updateGoLiveForm(reloadMeta) {
  const mode = modeValue(), yt = mode !== "preview";
  $("previewOpts").classList.toggle("hidden", yt);
  $("ytOpts").classList.toggle("hidden", !yt);
  $("glPrivacy").disabled = mode === "test";
  if (mode === "test") $("glPrivacy").value = "unlisted";
  $("glGo").textContent = { preview: "Start preview", test: "Start test run", live: "Go live" }[mode];
  const opt = $("glSet").selectedOptions[0], topic = opt?.dataset.topic || "Trivia", theme = $("glTheme").value;
  $("thumbPrev").src = `/api/thumbnail?topic=${encodeURIComponent(topic)}&theme=${theme}&t=${Date.now()}`;
  $("glOverlay").src = `/overlay?demo=options&theme=${theme}`;
  if (reloadMeta) {
    const m = await api(`/api/metadata?topic=${encodeURIComponent(topic)}`);
    $("glTitle").value = m.title; $("glDesc").value = m.description; $("glTags").value = m.tags.join(", ");
  }
}
document.querySelectorAll('input[name="mode"]').forEach(r => r.onchange = () => updateGoLiveForm(false));
$("glTheme").onchange = () => updateGoLiveForm(false);
$("glSet").onchange = () => updateGoLiveForm(true);
$("glGo").onclick = (e) => busy(e.target, async () => {
  const mode = modeValue();
  if (!$("glSet").value) { msg($("glMsg"), "Approve some questions first (Questions tab).", "err"); return; }
  if (mode !== "preview" && !STATE.youtube.signed_in) { msg($("glMsg"), "Sign in to YouTube first (Setup).", "err"); return; }
  if (mode === "live" && !confirm(`Go live ${$("glPrivacy").value === "public" ? "publicly " : ""}on ${STATE.youtube.channel?.title || "your channel"}?`)) return;
  try {
    await api("/api/golive", { body: {
      mode, set_id: +$("glSet").value, theme: $("glTheme").value, max_questions: +$("glMax").value || 0,
      privacy: $("glPrivacy").value, title: $("glTitle").value, description: $("glDesc").value, tags: $("glTags").value,
      bots: $("glBots").checked, record: $("glRecord").checked } });
    msg($("glMsg"), ""); location.hash = "#live";
  } catch (err) { msg($("glMsg"), err.message, "err"); }
});

/* ---------------- live ---------------- */
function applyStatus(s) {
  if (!s) return;
  const active = ["starting", "live", "preview"].includes(s.state);
  $("liveDot").classList.toggle("on", active);
  const titles = { starting: "Starting…", live: "You're live", preview: "Preview running", idle: "Nothing running" };
  $("liveTitle").textContent = titles[s.state] || "Nothing running";
  let sub = "Start a preview or a stream from Go live.";
  if (active) {
    sub = s.mode === "preview" ? "Pretend viewers are answering. Type your own comments below to test." : "Comments are read only while answers are open, to save quota.";
    if (s.question) sub = `Question ${s.question}${s.total ? " of " + s.total : ""}. ` + sub;
    if (s.tech_break) sub = "Technical break on screen while the connection recovers.";
  } else if (s.ended_reason) {
    sub = { completed: "Finished: all questions played.", out_of_questions: "Finished: all approved questions played.",
            stopped: "You stopped the show.", quota: "Ended early to stay inside today's YouTube quota.",
            error: "Stopped because of an error. See Activity.", encoder_failed: "The video encoder kept failing. See Activity.",
            chat_unavailable: "Live chat couldn't be read. See Activity." }[s.ended_reason] || "Finished.";
  }
  $("liveSub").textContent = sub;
  $("stopSoft").disabled = $("stopHard").disabled = !active;
  $("fakeForm").classList.toggle("hidden", !(active && s.mode === "preview"));
  const w = $("watchLink"); w.classList.toggle("hidden", !s.video_url); if (s.video_url) w.href = s.video_url;
}
function addComment(c) {
  const ul = $("comments"); ul.querySelector(".empty")?.remove();
  const li = document.createElement("li");
  li.innerHTML = `<b>${esc(c.name)}</b><span>${esc(c.text)}</span>${c.tag ? `<span class="tag ${c.tag}">${c.tag}</span>` : ""}<span class="qn">Q${c.q}</span>`;
  ul.prepend(li); while (ul.children.length > 120) ul.lastChild.remove();
}
function addLog(e) {
  const li = document.createElement("li"); li.className = e.level;
  li.textContent = `${new Date(e.ts * 1000).toLocaleTimeString()}  ${e.message}`;
  $("log").prepend(li); while ($("log").children.length > 100) $("log").lastChild.remove();
}
function renderBoard(rows) {
  $("board").innerHTML = rows?.length ? rows.map((r, i) => `<li><span>${i + 1}. ${esc(r.name)}</span><b>${r.points}</b></li>`).join("") : `<li class="empty">No points yet.</li>`;
}
$("stopSoft").onclick = async () => { try { await api("/api/stop", { body: {} }); } catch (e) { toast(e.message, true); } };
$("stopHard").onclick = async () => { if (confirm("Stop immediately?")) try { await api("/api/stop", { body: { now: true } }); } catch (e) { toast(e.message, true); } };
$("fakeForm").onsubmit = async (e) => {
  e.preventDefault();
  try { await api("/api/preview/comment", { body: { name: $("fakeName").value, text: $("fakeText").value } }); $("fakeText").value = ""; }
  catch (err) { toast(err.message, true); }
};

function connectControl() {
  const es = new EventSource("/events/control");
  es.addEventListener("status", e => { const s = JSON.parse(e.data); applyStatus(s); if (s.state === "idle" && STATE) loadState(); });
  es.addEventListener("comment", e => addComment(JSON.parse(e.data)));
  es.addEventListener("log", e => addLog(JSON.parse(e.data)));
  es.addEventListener("winner", e => renderBoard(JSON.parse(e.data).leaderboard));
  es.addEventListener("gen", e => onGen(JSON.parse(e.data)));
  es.onerror = () => { es.close(); setTimeout(connectControl, 1500); };
}

/* ---------------- history ---------------- */
async function loadHistory() {
  const r = await api("/api/history");
  $("histTable").innerHTML = r.streams.length ? `<tr><th>When</th><th>Mode</th><th>Title</th><th>Questions</th><th>Winner</th><th>Ended</th></tr>` +
    r.streams.map(s => `<tr><td>${new Date(s.started_at).toLocaleString()}</td><td>${esc(s.mode)}</td>
      <td>${s.broadcast_id ? `<a href="https://www.youtube.com/watch?v=${esc(s.broadcast_id)}" target="_blank" rel="noopener">${esc(s.title)}</a>` : esc(s.title)}</td>
      <td>${s.questions_played ?? 0}</td><td>${esc(s.leaderboard[0]?.name || "—")}</td><td>${esc((s.end_reason || "running").replace(/_/g, " "))}</td></tr>`).join("")
    : `<tr><td class="empty">No streams yet.</td></tr>`;
}

/* ---------------- boot ---------------- */
(async () => {
  const p = new URLSearchParams(location.search);
  if (p.get("yt") === "ok") toast("Signed in to YouTube.");
  if (p.get("yt") === "denied") toast("Google sign-in was cancelled.", true);
  if (p.has("yt")) history.replaceState(null, "", "/" + location.hash);
  try { await loadState(); } catch (e) { toast(e.message, true); }
  showTab(location.hash.slice(1) || (STATE && STATE.llm.model && STATE.youtube.signed_in ? "questions" : "setup"));
  connectControl();
})();
