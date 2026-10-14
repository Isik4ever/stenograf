"use strict";

/* Stenograf — klient. Ingen byggkedja, inga beroenden. */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));

const el = {
  archive: $("#archive"), archiveToggle: $("#archiveToggle"), jobList: $("#jobList"), archiveEmpty: $("#archiveEmpty"),
  deviceInfo: $("#deviceInfo"),
  compose: $("#compose"), linkForm: $("#linkForm"), urlInput: $("#urlInput"), goBtn: $("#goBtn"),
  fileInput: $("#fileInput"), formError: $("#formError"), speakersSel: $("#speakersSel"),
  job: $("#job"), jobMedia: $("#jobMedia"), mediaFrame: $("#mediaFrame"), jobThumb: $("#jobThumb"), jobSource: $("#jobSource"), jobTitle: $("#jobTitle"), jobMeta: $("#jobMeta"),
  progress: $("#progress"), stages: $("#stages"), bar: $("#bar"), barFill: $("#barFill"),
  progressLabel: $("#progressLabel"), progressDetail: $("#progressDetail"), cancelBtn: $("#cancelBtn"),
  jobError: $("#jobError"), jobErrorText: $("#jobErrorText"), retryBtn: $("#retryBtn"),
  authFix: $("#authFix"), browserSel: $("#browserSel"), authRetryBtn: $("#authRetryBtn"),
  toolbar: $("#toolbar"), searchInput: $("#searchInput"), searchCount: $("#searchCount"),
  searchPrev: $("#searchPrev"), searchNext: $("#searchNext"), followToggle: $("#followToggle"), unsureBtn: $("#unsureBtn"),
  editBtn: $("#editBtn"), copyBtn: $("#copyBtn"), exportBtn: $("#exportBtn"), exportMenu: $("#exportMenu"),
  editBar: $("#editBar"), editCount: $("#editCount"), editCancel: $("#editCancel"), editSave: $("#editSave"),
  pad: $("#pad"),
  player: $("#player"), playBtn: $("#playBtn"), playIcon: $("#playIcon"),
  back5: $("#back5"), fwd5: $("#fwd5"), timeNow: $("#timeNow"), timeTotal: $("#timeTotal"),
  scrub: $("#scrub"), scrubFill: $("#scrubFill"), scrubHead: $("#scrubHead"), speedBtn: $("#speedBtn"),
  drop: $("#drop"), toast: $("#toast"), confirmDlg: $("#confirmDlg"), confirmText: $("#confirmText"),
};

const S = {
  jobs: [],
  cur: null,        // publikt jobbobjekt
  transcript: null, // {paragraphs, cues}
  names: {},        // talar-id -> namn (tomt om bara en talare)
  words: [],        // [{s, e, el}]
  starts: null,     // Float64Array med ordstarter för binärsökning
  es: null,
  said: -1,
  now: -1,
  inking: false,
  editing: false,
  edits: new Map(),
  hits: [],
  hitIdx: -1,
  unsure: [],
  unsureIdx: -1,
  src: null,        // inbäddad källspelare (player.js)
  playing: false,
  followPausedUntil: 0,
  draft: null,      // {para, start, end, chars}
};

/* ------------------------------------------------------------ hjälpare */

function clock(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

function minutes(sec) {
  if (!sec) return "";
  if (sec < 60) return `${Math.round(sec)} s`;
  const m = Math.round(sec / 60);
  return m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m} min`;
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: opts.body && !(opts.body instanceof FormData) ? { "Content-Type": "application/json" } : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* tom kropp */ }
  if (!res.ok) throw new Error((data && (data.error || data.detail)) || `Fel ${res.status}`);
  return data;
}

let toastTimer = 0;
function toast(msg) {
  el.toast.textContent = msg;
  el.toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.toast.classList.remove("show"), 2600);
}

function confirmDialog(text) {
  el.confirmText.textContent = text;
  el.confirmDlg.returnValue = "";
  el.confirmDlg.showModal();
  return new Promise((resolve) => {
    el.confirmDlg.addEventListener("close", () => resolve(el.confirmDlg.returnValue === "yes"), { once: true });
  });
}

const norm = (t) => t.toLowerCase().replace(/ё/g, "е").replace(/[^\p{L}\p{N}-]+/gu, "");

function isTyping(t) {
  return t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
}

/* ------------------------------------------------------------ arkiv */

const BUSY = new Set(["queued", "probing", "downloading", "preparing", "vad", "loading_model", "transcribing", "speakers", "finishing"]);

async function refreshList() {
  try {
    S.jobs = await api("/api/jobs");
  } catch { return; }
  renderList();
}

function renderList() {
  el.archiveEmpty.hidden = S.jobs.length > 0;
  const curId = S.cur && S.cur.id;
  el.jobList.replaceChildren(...S.jobs.map((j) => {
    const li = document.createElement("li");
    li.className = "job-item";
    const a = document.createElement("a");
    a.href = `#/job/${j.id}`;
    if (j.id === curId) a.setAttribute("aria-current", "page");

    const th = document.createElement("span");
    const thumb = safeUrl(j.meta && j.meta.thumbnail);
    th.className = "ji-thumb" + (thumb ? "" : " file");
    if (thumb) th.style.backgroundImage = `url("${thumb}")`;

    const body = document.createElement("span");
    const title = document.createElement("span");
    title.className = "ji-title";
    title.textContent = j.meta.title || j.url || "Utan titel";
    const sub = document.createElement("span");
    sub.className = "ji-sub";
    if (BUSY.has(j.status)) {
      sub.innerHTML = `<span class="busy">● ${j.stage_label}</span> ${Math.round(j.progress * 100)} %`;
    } else if (j.status === "error" || j.status === "cancelled") {
      sub.innerHTML = `<span class="err">${j.status === "error" ? "Misslyckades" : "Avbruten"}</span>`;
    } else {
      sub.textContent = [clock(j.meta.duration), new Date(j.created * 1000).toLocaleDateString("sv-SE", { day: "numeric", month: "short" })]
        .filter(Boolean).join(" · ");
    }
    body.append(title, sub);

    const del = document.createElement("button");
    del.className = "del-btn";
    del.type = "button";
    del.setAttribute("aria-label", "Radera");
    del.textContent = "×";
    del.addEventListener("click", (ev) => { ev.preventDefault(); ev.stopPropagation(); deleteJob(j); });

    a.append(th, body, del);
    li.append(a);
    return li;
  }));
}

async function deleteJob(j) {
  const ok = await confirmDialog(`Radera ”${j.meta.title || "transkriptionen"}”? Ljud och text tas bort från datorn.`);
  if (!ok) return;
  await api(`/api/jobs/${j.id}`, { method: "DELETE" });
  if (S.cur && S.cur.id === j.id) location.hash = "#/";
  toast("Raderad");
  refreshList();
}

setInterval(() => {
  if (S.jobs.some((j) => BUSY.has(j.status))) refreshList();
}, 3000);

/* ------------------------------------------------------------ routing */

function route() {
  const m = location.hash.match(/^#\/job\/([a-f0-9]+)/);
  el.archive.classList.remove("open");
  el.archiveToggle.setAttribute("aria-expanded", "false");
  if (m) openJob(m[1]);
  else showCompose();
  refreshList();
}

function closeStream() {
  if (S.es) { S.es.close(); S.es = null; }
}

function resetReader() {
  closeStream();
  if (S.src) { S.src.destroy(); S.src = null; }
  clearInterval(tick);
  el.mediaFrame.classList.remove("floating");
  el.jobMedia.classList.remove("has-video");
  el.jobMedia.hidden = true;
  el.jobThumb.hidden = false;
  el.player.hidden = true;
  S.cur = null; S.transcript = null; S.words = []; S.starts = null;
  S.said = -1; S.now = -1; S.inking = false; S.playing = false; S.draft = null; S.names = {};
  S.hits = []; S.hitIdx = -1;
  setEditing(false);
  el.pad.replaceChildren();
  el.pad.classList.remove("inking", "show-unsure");
  el.unsureBtn.setAttribute("aria-pressed", "false");
  el.searchInput.value = "";
  el.searchCount.textContent = "";
}

function showCompose() {
  resetReader();
  el.job.hidden = true;
  el.compose.hidden = false;
  document.title = "Stenograf";
  renderList();
  setTimeout(() => el.urlInput.focus(), 30);
}

/* ------------------------------------------------------------ ny transkription */

function mode() {
  return $("input[name=mode]:checked").value;
}

function speakerOpts() {
  const v = el.speakersSel.value;
  return { speakers: v !== "1", num_speakers: v === "auto" || v === "1" ? null : Number(v) };
}

el.linkForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  el.formError.hidden = true;
  el.goBtn.disabled = true;
  try {
    const j = await api("/api/jobs", { method: "POST", body: JSON.stringify({ url: el.urlInput.value.trim(), mode: mode(), ...speakerOpts() }) });
    el.urlInput.value = "";
    location.hash = `#/job/${j.id}`;
    refreshList();
  } catch (e) {
    el.formError.textContent = e.message;
    el.formError.hidden = false;
  } finally {
    el.goBtn.disabled = false;
  }
});

// Klistra in en länk var som helst på startsidan → fyll i och starta direkt.
document.addEventListener("paste", (ev) => {
  if (el.compose.hidden || isTyping(ev.target)) return;
  const text = (ev.clipboardData.getData("text") || "").trim();
  if (/^https?:\/\/\S+$/i.test(text)) {
    el.urlInput.value = text;
    el.linkForm.requestSubmit();
  }
});

el.fileInput.addEventListener("change", () => {
  const f = el.fileInput.files[0];
  if (f) uploadFile(f);
  el.fileInput.value = "";
});

function uploadFile(file) {
  el.formError.hidden = true;
  const fd = new FormData();
  fd.append("file", file);
  fd.append("mode", mode());
  const so = speakerOpts();
  fd.append("speakers", so.speakers ? "true" : "false");
  fd.append("num_speakers", so.num_speakers || "");
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/upload");
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) toast(`Laddar in ${file.name} · ${Math.round((e.loaded / e.total) * 100)} %`);
  };
  xhr.onload = () => {
    let data = {};
    try { data = JSON.parse(xhr.responseText); } catch { /* */ }
    if (xhr.status >= 400) {
      showCompose();
      el.formError.textContent = data.error || "Filen kunde inte laddas in.";
      el.formError.hidden = false;
      return;
    }
    location.hash = `#/job/${data.id}`;
    refreshList();
  };
  xhr.onerror = () => toast("Uppladdningen avbröts.");
  xhr.send(fd);
}

let dragDepth = 0;
window.addEventListener("dragenter", (e) => {
  if (!e.dataTransfer || !Array.from(e.dataTransfer.types).includes("Files")) return;
  dragDepth++; el.drop.hidden = false;
});
window.addEventListener("dragleave", () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) el.drop.hidden = true; });
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => {
  e.preventDefault();
  dragDepth = 0; el.drop.hidden = true;
  const f = e.dataTransfer.files[0];
  if (f) uploadFile(f);
});

/* ------------------------------------------------------------ jobbvy */

async function openJob(id) {
  if (S.cur && S.cur.id === id) return;
  resetReader();
  el.compose.hidden = true;
  el.job.hidden = false;
  let j;
  try {
    j = await api(`/api/jobs/${id}`);
  } catch (e) {
    toast(e.message);
    location.hash = "#/";
    return;
  }
  S.cur = j;
  renderList();
  renderHead(j);
  if (j.status === "done") {
    showDone(j);
  } else if (BUSY.has(j.status)) {
    showProgress(j);
    listen(j.id);
  } else {
    showFailed(j);
  }
}

function renderHead(j) {
  const m = j.meta || {};
  document.title = `${m.title || "Transkription"} — Stenograf`;
  el.jobTitle.textContent = m.title || j.url || "Transkription";
  el.jobSource.textContent = m.extractor ? m.extractor.replace(/:.*/, "") : (j.source === "file" ? "Fil" : "Länk");
  const thumb = safeUrl(m.thumbnail);
  if (thumb || S.src) {
    el.jobMedia.hidden = false;
    if (thumb) el.jobThumb.style.backgroundImage = `url("${thumb}")`;
    el.jobThumb.hidden = !!S.src;
  } else {
    el.jobMedia.hidden = true;
  }
  const bits = [];
  if (m.uploader) bits.push(esc(m.uploader));
  if (m.duration) bits.push(clock(m.duration));
  if (j.engine) bits.push(esc(j.engine));
  if (j.status === "done" && j.timings && j.timings.total) bits.push(`klar på ${minutes(j.timings.total)}`);
  if (m.url) bits.push(`<a href="${esc(m.url)}" target="_blank" rel="noopener">Öppna källan ↗</a>`);
  el.jobMeta.innerHTML = bits.join(" · ");
}

function safeUrl(u) {
  if (!u) return null;
  try {
    const x = new URL(u);
    return x.protocol === "https:" || x.protocol === "http:" ? x.toString().replace(/"/g, "%22") : null;
  } catch { return null; }
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const STAGE_ORDER = ["queued", "probing", "downloading", "preparing", "vad", "loading_model", "transcribing", "finishing", "done"];
const STAGE_GROUP = { queued: "downloading", probing: "downloading", downloading: "downloading", preparing: "vad", vad: "vad", loading_model: "transcribing", transcribing: "transcribing", speakers: "transcribing", finishing: "transcribing", done: "done" };

function showProgress(j) {
  el.progress.hidden = false;
  el.jobError.hidden = true;
  el.toolbar.hidden = true;
  updateProgress(j);
  if (!el.pad.childElementCount) {
    el.pad.innerHTML = `<div class="pad-empty"><span></span><p>Texten skrivs här så fort den första meningen är klar.</p></div>`;
  }
}

function updateProgress(j) {
  const pct = Math.round(j.progress * 100);
  el.barFill.style.transform = `scaleX(${j.progress})`;
  el.bar.setAttribute("aria-valuenow", pct);
  el.progressLabel.textContent = `${j.stage_label} · ${pct} %`;
  el.progressDetail.textContent = j.detail || "";
  const grp = STAGE_GROUP[j.status] || "downloading";
  const order = ["downloading", "vad", "transcribing", "done"];
  const gi = order.indexOf(grp);
  $$("li", el.stages).forEach((li, i) => {
    li.classList.toggle("done", i < gi || j.status === "done");
    li.classList.toggle("now", i === gi && j.status !== "done");
  });
}

function listen(id) {
  closeStream();
  const es = new EventSource(`/api/jobs/${id}/events`);
  S.es = es;
  const onJob = (ev) => {
    const d = JSON.parse(ev.data);
    const j = d.job || d;
    if (!S.cur || S.cur.id !== j.id) return;
    S.cur = j;
    return { d, j };
  };
  es.addEventListener("hello", (ev) => { const r = onJob(ev); if (r) updateProgress(r.j); });
  es.addEventListener("progress", (ev) => { const r = onJob(ev); if (r) updateProgress(r.j); });
  es.addEventListener("meta", (ev) => { const r = onJob(ev); if (r) renderHead(r.j); });
  es.addEventListener("partial", (ev) => { const r = onJob(ev); if (r) addDraft(r.d.chunk); });
  es.addEventListener("status", async (ev) => {
    const r = onJob(ev);
    if (!r) return;
    const j = r.j;
    if (j.status === "done") {
      closeStream();
      const full = await api(`/api/jobs/${j.id}`);
      S.cur = full;
      renderHead(full);
      showDone(full);
      refreshList();
      toast("Transkriptionen är klar");
    } else if (j.status === "error" || j.status === "cancelled") {
      closeStream();
      showFailed(j);
      refreshList();
    } else {
      updateProgress(j);
    }
  });
  es.onerror = () => {
    // Servern stänger strömmen när jobbet är klart; annars försöker EventSource igen av sig själv.
  };
}

function addDraft(chunk) {
  if (!chunk || !chunk.text) return;
  const empty = $(".pad-empty", el.pad);
  if (empty) empty.remove();
  const d = S.draft;
  if (d && chunk.start - d.end < 2.2 && d.chars < 650) {
    d.text.append(" " + chunk.text);
    d.end = chunk.end;
    d.chars += chunk.text.length + 1;
    return;
  }
  const para = document.createElement("div");
  para.className = "para draft fresh";
  const st = document.createElement("span");
  st.className = "stamp";
  st.textContent = clock(chunk.start);
  const p = document.createElement("p");
  p.className = "ptext";
  p.textContent = chunk.text;
  para.append(st, p);
  el.pad.append(para);
  S.draft = { para, text: p, start: chunk.start, end: chunk.end, chars: chunk.text.length };
}

let browsers = null;

async function showFailed(j) {
  el.progress.hidden = true;
  el.toolbar.hidden = true;
  el.jobError.hidden = false;
  el.jobErrorText.textContent = j.status === "cancelled" ? "Transkriberingen avbröts." : (j.error || "Något gick fel.");
  const auth = j.error_kind === "auth" && j.source === "url";
  el.authFix.hidden = !auth;
  el.retryBtn.textContent = auth ? "Försök igen utan inloggning" : "Försök igen";
  el.retryBtn.classList.toggle("btn-ink", !auth);
  el.retryBtn.classList.toggle("btn-ghost", auth);
  if (auth) {
    if (!browsers) { try { browsers = await api("/api/browsers"); } catch { browsers = []; } }
    el.browserSel.replaceChildren(...browsers.map((b) => new Option(b.label, b.key, false, b.key === j.cookies)));
    el.authRetryBtn.disabled = !browsers.length;
  }
}

el.cancelBtn.addEventListener("click", async () => {
  if (!S.cur) return;
  el.cancelBtn.disabled = true;
  try { await api(`/api/jobs/${S.cur.id}/cancel`, { method: "POST" }); } finally { el.cancelBtn.disabled = false; }
});

el.retryBtn.addEventListener("click", () => retryJob(""));
el.authRetryBtn.addEventListener("click", () => retryJob(el.browserSel.value));

async function retryJob(cookies) {
  if (!S.cur) return;
  const id = S.cur.id;
  try {
    const j = await api(`/api/jobs/${id}/retry`, { method: "POST", body: JSON.stringify({ cookies }) });
    S.cur = j;
    el.pad.replaceChildren();
    S.draft = null;
    showProgress(j);
    listen(id);
    refreshList();
  } catch (e) { toast(e.message); }
}

/* ------------------------------------------------------------ läsvy */

async function showDone(j) {
  el.progress.hidden = true;
  el.jobError.hidden = true;
  el.toolbar.hidden = false;
  S.transcript = j.transcript;
  S.names = j.speaker_names || {};
  renderTranscript();
  $$("a[data-fmt]", el.exportMenu).forEach((a) => {
    a.href = `/api/jobs/${j.id}/export/${a.dataset.fmt}`;
    a.setAttribute("download", "");
  });
  if (mountSource(j)) el.jobMedia.hidden = false;
  el.pad.classList.toggle("no-player", !S.src);
}

function renderTranscript() {
  const frag = document.createDocumentFragment();
  const words = [];
  const paras = (S.transcript && S.transcript.paragraphs) || [];
  const multi = Object.keys(S.names).length > 1;
  let lastSpk = null;
  paras.forEach((p, pi) => {
    const para = document.createElement("div");
    para.className = "para";
    if (multi && p.speaker != null) para.dataset.spk = p.speaker % 6;
    const st = document.createElement("button");
    st.className = "stamp";
    st.type = "button";
    st.textContent = clock(p.start);
    st.dataset.t = p.start;
    st.setAttribute("aria-label", `Spela från ${clock(p.start)}`);
    const pt = document.createElement("p");
    pt.className = "ptext";
    if (multi && p.speaker != null && p.speaker !== lastSpk) {
      const who = document.createElement("button");
      who.type = "button";
      who.className = "who";
      who.dataset.id = p.speaker;
      who.textContent = S.names[p.speaker] || `Talare ${p.speaker + 1}`;
      who.title = "Klicka för att byta namn";
      pt.append(who);
    }
    lastSpk = p.speaker;
    p.sentences.forEach((s, si) => {
      const sent = document.createElement("span");
      sent.className = "sent" + (s.edited ? " edited" : "");
      sent.dataset.k = `${pi}.${si}`;
      const ws = s.words && s.words.length ? s.words : [[s.text, s.start, s.end]];
      ws.forEach((w, wi) => {
        const span = document.createElement("span");
        span.className = w[3] != null && w[3] < UNSURE ? "w unsure" : "w";
        span.textContent = w[0];
        span.dataset.i = words.length;
        words.push({ s: w[1], e: w[2], el: span });
        sent.append(span);
        if (wi < ws.length - 1) sent.append(" ");
      });
      pt.append(sent);
      if (si < p.sentences.length - 1) pt.append(" ");
    });
    para.append(st, pt);
    frag.append(para);
  });
  el.pad.replaceChildren(frag);
  if (!paras.length) {
    el.pad.innerHTML = `<div class="pad-empty"><span></span><p>Inget tal hittades.</p></div>`;
  }
  S.words = words;
  S.starts = Float64Array.from(words, (w) => w.s);
  S.said = -1; S.now = -1;
  S.unsure = $$(".w.unsure", el.pad);
  S.unsureIdx = -1;
  el.unsureBtn.textContent = S.unsure.length ? `Osäkra ord · ${S.unsure.length}` : "Inga osäkra ord";
  el.unsureBtn.disabled = !S.unsure.length;
  if (el.searchInput.value) runSearch();
}

/* Granskning: modellen markerar ord den var osäker på (sannolikhet under 50 %). */
const UNSURE = 0.5;
el.unsureBtn.addEventListener("click", () => {
  if (!S.unsure.length) return;
  el.pad.classList.add("show-unsure");
  el.unsureBtn.setAttribute("aria-pressed", "true");
  if (S.unsureIdx >= 0) S.unsure[S.unsureIdx].classList.remove("focus");
  // Nästa osäkra ord nedanför mitten av skärmen, annars börja om uppifrån.
  const mid = window.innerHeight / 2;
  let i = S.unsure.findIndex((w, k) => k > S.unsureIdx && w.getBoundingClientRect().top > mid - 40);
  if (i < 0) i = S.unsure.findIndex((_, k) => k > S.unsureIdx);
  if (i < 0) i = 0;
  S.unsureIdx = i;
  const w = S.unsure[i];
  w.classList.add("focus");
  w.scrollIntoView({ block: "center", behavior: "smooth" });
  S.followPausedUntil = Date.now() + 8000;
  el.unsureBtn.textContent = `Osäkra ord · ${i + 1} / ${S.unsure.length}`;
});

el.pad.addEventListener("click", (ev) => {
  const who = ev.target.closest(".who");
  if (who) { renameSpeaker(who); return; }
  if (S.editing) return;
  const st = ev.target.closest(".stamp");
  if (st && st.dataset.t) { seek(+st.dataset.t, true); return; }
  const w = ev.target.closest(".w");
  if (w && S.words.length && S.src && !window.getSelection().toString()) {
    seek(S.words[+w.dataset.i].s, true);
  }
});

function renameSpeaker(btn) {
  const id = btn.dataset.id;
  const input = document.createElement("input");
  input.className = "who-input";
  input.value = btn.textContent;
  input.setAttribute("aria-label", "Namn på talaren");
  btn.replaceWith(input);
  input.focus();
  input.select();
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const name = input.value.trim();
    input.replaceWith(btn);
    if (!save || !name || name === btn.textContent) return;
    try {
      const old = btn.textContent;
      const res = await api(`/api/jobs/${S.cur.id}/speakers`, { method: "PUT", body: JSON.stringify({ names: { [id]: name } }) });
      S.names = res.speaker_names;
      if (res.merged) {
        // Samma namn som en annan talare: de är nu en och samma.
        S.transcript = res.transcript;
        renderTranscript();
        toast(`${old} slogs ihop med ${name}`);
      } else {
        $$(`.who[data-id="${id}"]`, el.pad).forEach((b) => { b.textContent = S.names[id]; });
        toast(`Talaren heter nu ${S.names[id]}`);
      }
    } catch (e) { toast(e.message); }
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    if (e.key === "Escape") { e.preventDefault(); finish(false); }
  });
  input.addEventListener("blur", () => finish(true));
}

/* ------------------------------------------------------------ uppspelning */
/* Videon spelas från källan (YouTube/VK/RuTube). Inget ljud finns lagrat lokalt. */

function setPlayIcon(playing) {
  el.playIcon.setAttribute("d", playing ? "M6.5 4.5h4v15h-4zM13.5 4.5h4v15h-4z" : "M7 4.5v15l12-7.5z");
  el.playBtn.setAttribute("aria-label", playing ? "Pausa" : "Spela");
}

const nowT = () => (S.src ? S.src.time() : 0);

function seek(t, play) {
  if (!S.src) {
    // Ingen inbäddad spelare: öppna källan vid rätt tid i en ny flik.
    const url = S.cur && SourcePlayer.linkAt(S.cur.meta, t);
    if (play && url) window.open(url, "_blank", "noopener");
    return;
  }
  S.src.seek(Math.max(0, t - 0.05));
  S.followPausedUntil = 0;
  if (play) S.src.play();
  setTimeout(() => sync(true), 60);
}

function togglePlay() {
  if (!S.src) return;
  if (S.src.paused()) S.src.play(); else S.src.pause();
}

el.playBtn.addEventListener("click", togglePlay);
el.back5.addEventListener("click", () => seek(nowT() - 5, false));
el.fwd5.addEventListener("click", () => seek(nowT() + 5, false));

const SPEEDS = [1, 1.25, 1.5, 1.75, 2, 0.75];
let speedIdx = 0;
el.speedBtn.addEventListener("click", () => {
  if (!S.src) return;
  speedIdx = (speedIdx + 1) % SPEEDS.length;
  S.src.setRate(SPEEDS[speedIdx]);
  el.speedBtn.textContent = `${SPEEDS[speedIdx]}×`.replace(".", ",");
});

let tick = 0;
function startTicking() {
  clearInterval(tick);
  tick = setInterval(() => sync(false), 90);
}

function mountSource(j) {
  const emb = SourcePlayer.embedFor(j.meta);
  if (!emb) return false;
  el.jobMedia.classList.add("has-video");
  el.jobThumb.hidden = true;
  S.src = SourcePlayer.create(el.mediaFrame, emb, {
    ready: (dur) => {
      if (dur) el.timeTotal.textContent = clock(dur);
      sync(true);
    },
    play: () => {
      S.playing = true;
      setPlayIcon(true);
      if (!S.inking) { S.inking = true; el.pad.classList.add("inking"); }
      startTicking();
      floatCheck();
    },
    pause: () => {
      S.playing = false;
      setPlayIcon(false);
      clearInterval(tick);
      sync(false);
      floatCheck();
    },
    seeked: () => sync(true),
    error: () => unmountSource(),
  });
  el.player.hidden = false;
  el.speedBtn.hidden = !S.src.canRate;
  speedIdx = 0;
  el.speedBtn.textContent = "1×";
  setPlayIcon(false);
  el.timeTotal.textContent = clock(j.meta.duration);
  return true;
}

// Källan vägrade spela inbäddat (upphovsrätt, region, ägarens val): gå tillbaka till länkar.
function unmountSource() {
  if (!S.src) return;
  S.src.destroy();
  S.src = null;
  S.playing = false;
  clearInterval(tick);
  el.player.hidden = true;
  el.mediaFrame.classList.remove("floating");
  el.jobMedia.classList.remove("has-video");
  el.jobThumb.hidden = false;
  el.pad.classList.add("no-player");
  el.pad.classList.remove("inking");
  toast("Källan tillåter inte uppspelning här. Klicka på en tid för att öppna videon där.");
}

// Miniatyrspelare när rubriken med videon scrollats bort och videon spelar.
let headVisible = true;
const mediaObserver = new IntersectionObserver((entries) => {
  headVisible = entries[0].isIntersecting;
  floatCheck();
}, { threshold: 0.25 });
mediaObserver.observe(el.jobMedia);
function floatCheck() {
  el.mediaFrame.classList.toggle("floating", !!S.src && S.playing && !headVisible);
}

function wordAt(t) {
  const a = S.starts;
  if (!a || !a.length || t < a[0]) return -1;
  let lo = 0, hi = a.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (a[mid] <= t) lo = mid; else hi = mid - 1;
  }
  return lo;
}

function sync(force) {
  if (!S.src) return;
  const t = S.src.time() || 0;
  const dur = S.src.duration() || (S.cur && S.cur.meta.duration) || 1;
  el.timeNow.textContent = clock(t);
  const frac = Math.min(1, t / dur);
  el.scrubHead.style.left = `${frac * 100}%`;
  el.scrubFill.style.transform = `scaleX(${frac})`;
  el.scrub.setAttribute("aria-valuenow", Math.round(t));
  el.scrub.setAttribute("aria-valuemax", Math.round(dur));
  if (!S.words.length) return;

  const idx = wordAt(t);
  // "said": allt fram till aktuellt ord är i bläck. Uppdatera bara skillnaden.
  if (idx > S.said) {
    for (let i = S.said + 1; i <= idx; i++) S.words[i].el.classList.add("said");
  } else if (idx < S.said) {
    for (let i = idx + 1; i <= S.said; i++) S.words[i].el.classList.remove("said");
  }
  S.said = idx;
  if (idx !== S.now || force) {
    if (S.now >= 0 && S.words[S.now]) S.words[S.now].el.classList.remove("now");
    const w = S.words[idx];
    const active = w && t <= w.e + 0.6;
    if (active) w.el.classList.add("now");
    S.now = active ? idx : -1;
    if (w && el.followToggle.checked && !S.editing && Date.now() > S.followPausedUntil && S.playing) {
      const r = w.el.getBoundingClientRect();
      const vh = window.innerHeight;
      if (r.top < vh * 0.18 || r.bottom > vh * 0.72) {
        w.el.scrollIntoView({ block: "center", behavior: force ? "auto" : "smooth" });
      }
    }
  }
}

// Användaren scrollar själv → låt bli att dra tillbaka texten en stund.
["wheel", "touchmove"].forEach((t) => window.addEventListener(t, () => { S.followPausedUntil = Date.now() + 5000; }, { passive: true }));

function scrubTo(clientX) {
  if (!S.src) return;
  const r = el.scrub.getBoundingClientRect();
  const frac = Math.min(1, Math.max(0, (clientX - r.left) / r.width));
  seek(frac * (S.src.duration() || (S.cur && S.cur.meta.duration) || 0), false);
}
el.scrub.addEventListener("pointerdown", (e) => {
  el.scrub.setPointerCapture(e.pointerId);
  scrubTo(e.clientX);
  const move = (ev) => scrubTo(ev.clientX);
  el.scrub.addEventListener("pointermove", move);
  el.scrub.addEventListener("pointerup", () => el.scrub.removeEventListener("pointermove", move), { once: true });
});
el.scrub.addEventListener("keydown", (e) => {
  if (e.key === "ArrowLeft") { seek(nowT() - 5); e.preventDefault(); }
  if (e.key === "ArrowRight") { seek(nowT() + 5); e.preventDefault(); }
});

/* ------------------------------------------------------------ sök */

let searchTimer = 0;
el.searchInput.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 120); });
el.searchInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); stepHit(e.shiftKey ? -1 : 1); }
  if (e.key === "Escape") { el.searchInput.value = ""; runSearch(); el.searchInput.blur(); }
});
el.searchNext.addEventListener("click", () => stepHit(1));
el.searchPrev.addEventListener("click", () => stepHit(-1));

function runSearch() {
  S.hits.forEach(([i, n]) => { for (let k = i; k < i + n; k++) S.words[k].el.classList.remove("hit", "cur"); });
  S.hits = []; S.hitIdx = -1;
  const q = el.searchInput.value.trim().split(/\s+/).map(norm).filter(Boolean);
  if (!q.length) { el.searchCount.textContent = ""; return; }
  const W = S.words.map((w) => w._n ?? (w._n = norm(w.el.textContent)));
  const n = q.length;
  for (let i = 0; i + n <= W.length; i++) {
    let ok = true;
    for (let k = 0; k < n && ok; k++) {
      const w = W[i + k], t = q[k];
      if (n === 1) ok = w.includes(t);
      else if (k === 0) ok = w.endsWith(t);
      else if (k === n - 1) ok = w.startsWith(t);
      else ok = w === t;
    }
    if (ok) S.hits.push([i, n]);
  }
  S.hits.forEach(([i, len]) => { for (let k = i; k < i + len; k++) S.words[k].el.classList.add("hit"); });
  if (S.hits.length) {
    // Första träffen efter nuvarande uppspelningsposition.
    const from = Math.max(0, S.said);
    const first = S.hits.findIndex(([i]) => i >= from);
    setHit(first >= 0 ? first : 0);
  } else {
    el.searchCount.textContent = "0 träffar";
  }
}

function setHit(k) {
  if (S.hitIdx >= 0) {
    const [i, n] = S.hits[S.hitIdx];
    for (let x = i; x < i + n; x++) S.words[x].el.classList.remove("cur");
  }
  S.hitIdx = k;
  const [i, n] = S.hits[k];
  for (let x = i; x < i + n; x++) S.words[x].el.classList.add("cur");
  S.words[i].el.scrollIntoView({ block: "center", behavior: "smooth" });
  S.followPausedUntil = Date.now() + 6000;
  el.searchCount.textContent = `${k + 1} / ${S.hits.length}`;
}

function stepHit(d) {
  if (!S.hits.length) return;
  setHit((S.hitIdx + d + S.hits.length) % S.hits.length);
}

/* ------------------------------------------------------------ redigering */

function setEditing(on) {
  S.editing = on;
  el.pad.classList.toggle("editing", on);
  el.editBar.hidden = !on;
  el.editBtn.setAttribute("aria-pressed", String(on));
  el.editBtn.textContent = on ? "Redigerar …" : "Redigera";
  $$(".sent", el.pad).forEach((s) => {
    if (on) { s.contentEditable = "true"; s.spellcheck = true; }
    else s.removeAttribute("contenteditable");
  });
  if (!on) S.edits.clear();
  updateEditCount();
}

function updateEditCount() {
  const n = S.edits.size;
  el.editCount.textContent = n ? `${n} ${n === 1 ? "mening ändrad" : "meningar ändrade"}` : "";
  el.editSave.disabled = !n;
}

el.editBtn.addEventListener("click", () => {
  if (S.editing) return;
  if (S.src) S.src.pause();
  setEditing(true);
  toast("Klicka i en mening för att rätta den");
});
el.pad.addEventListener("input", (ev) => {
  const s = ev.target.closest(".sent");
  if (!s) return;
  S.edits.set(s.dataset.k, s.textContent);
  updateEditCount();
});
el.pad.addEventListener("keydown", (ev) => {
  // Enter i en mening ska inte skapa nya block.
  if (S.editing && ev.key === "Enter") { ev.preventDefault(); ev.target.blur(); }
});
el.editCancel.addEventListener("click", () => {
  setEditing(false);
  renderTranscript();
});
el.editSave.addEventListener("click", async () => {
  if (!S.edits.size) return;
  el.editSave.disabled = true;
  try {
    const res = await api(`/api/jobs/${S.cur.id}/transcript`, {
      method: "PUT",
      body: JSON.stringify({ edits: Object.fromEntries(S.edits) }),
    });
    S.transcript = res.transcript;
    setEditing(false);
    renderTranscript();
    toast("Ändringarna är sparade");
  } catch (e) {
    toast(e.message);
    el.editSave.disabled = false;
  }
});

/* ------------------------------------------------------------ kopiera & exportera */

function plainText() {
  const multi = Object.keys(S.names).length > 1;
  return ((S.transcript && S.transcript.paragraphs) || [])
    .map((p) => {
      const text = p.sentences.map((s) => s.text).join(" ");
      return multi && p.speaker != null ? `${S.names[p.speaker]}: ${text}` : text;
    })
    .join("\n\n");
}

el.copyBtn.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(plainText());
    toast("Texten är kopierad");
  } catch {
    toast("Kunde inte kopiera. Använd Exportera i stället.");
  }
});

function setMenu(open) {
  el.exportMenu.hidden = !open;
  el.exportBtn.setAttribute("aria-expanded", String(open));
  if (open) $("a", el.exportMenu).focus();
}
el.exportBtn.addEventListener("click", (e) => { e.stopPropagation(); setMenu(el.exportMenu.hidden); });
el.exportMenu.addEventListener("click", () => setTimeout(() => setMenu(false), 50));
el.exportMenu.addEventListener("keydown", (e) => {
  const items = $$("a", el.exportMenu);
  const i = items.indexOf(document.activeElement);
  if (e.key === "ArrowDown") { items[(i + 1) % items.length].focus(); e.preventDefault(); }
  if (e.key === "ArrowUp") { items[(i - 1 + items.length) % items.length].focus(); e.preventDefault(); }
  if (e.key === "Escape") { setMenu(false); el.exportBtn.focus(); }
});
document.addEventListener("click", (e) => { if (!el.exportMenu.hidden && !e.target.closest(".export")) setMenu(false); });

/* ------------------------------------------------------------ tangentbord */

document.addEventListener("keydown", (e) => {
  const inReader = !el.job.hidden && !el.toolbar.hidden;
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "f" && inReader) {
    e.preventDefault();
    el.searchInput.focus();
    el.searchInput.select();
    return;
  }
  if (isTyping(e.target) || e.metaKey || e.ctrlKey || e.altKey) return;
  if (!inReader || el.player.hidden) return;
  if (e.key === " " && !(e.target instanceof HTMLButtonElement)) { e.preventDefault(); togglePlay(); }
  else if (e.key === "ArrowLeft") { e.preventDefault(); seek(nowT() - 5); }
  else if (e.key === "ArrowRight") { e.preventDefault(); seek(nowT() + 5); }
});

/* ------------------------------------------------------------ start */

el.archiveToggle.addEventListener("click", () => {
  const open = !el.archive.classList.contains("open");
  el.archive.classList.toggle("open", open);
  el.archiveToggle.setAttribute("aria-expanded", String(open));
});

window.addEventListener("hashchange", route);
api("/api/health").then((h) => {
  const dev = { mps: "Apple-GPU", cuda: "NVIDIA-GPU", cpu: "CPU" }[h.device] || h.device;
  el.deviceInfo.textContent = `Körs lokalt · ${dev} · inget lämnar datorn`;
}).catch(() => {});
route();
