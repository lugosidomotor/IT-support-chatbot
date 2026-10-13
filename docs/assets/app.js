(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const el = (tag, attrs = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children) if (child != null) node.append(child);
    return node;
  };
  const svg = (markup) => { const t = document.createElement("template"); t.innerHTML = markup.trim(); return t.content.firstChild; };

  const messagesEl = $("#messages");
  const inputEl = $("#input");
  const sendBtn = $("#send-btn");
  const stopBtn = $("#stop-btn");
  const trayEl = $("#tray");
  const fileInput = $("#file-input");
  const marvin = window.Marvin || { setMood() {}, say() {}, greet() {} };

  const MAX_UPLOAD = 10 * 1024 * 1024;
  const MAX_IMAGE = 5 * 1024 * 1024;
  const MAX_PER_MESSAGE = 5;
  const LABELS = { github: "GitHub", jira: "Jira", confluence: "Confluence", atlassian: "Atlassian" };
  const STATUS_TEXT = {
    connected: "Connected", disabled: "Disabled", unavailable: "Unavailable",
    auth_failed: "Authentication failed", connecting: "Connecting…",
  };
  const COPY_ICON = '<svg viewBox="0 0 24 24"><rect x="9" y="9" width="11" height="11" rx="2.5" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M15 9V6.5A2.5 2.5 0 0 0 12.5 4h-6A2.5 2.5 0 0 0 4 6.5v6A2.5 2.5 0 0 0 6.5 15H9" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>';

  const state = { sessionId: null, busy: false, controller: null, integrations: null, pollTimer: 0, pending: [] };

  // ------------------------------------------------------------------ markdown

  marked.setOptions({ gfm: true, breaks: false });
  DOMPurify.addHook("afterSanitizeAttributes", (node) => {
    if (node.tagName === "A") {
      const href = node.getAttribute("href") || "";
      if (!/^https?:\/\//i.test(href)) node.removeAttribute("href");
      node.setAttribute("target", "_blank");
      node.setAttribute("rel", "noopener noreferrer nofollow");
    }
  });
  // No images, media or forms in answers: injected content must not exfiltrate data via URLs.
  const renderMarkdown = (text) => DOMPurify.sanitize(marked.parse(text), {
    FORBID_TAGS: ["img", "picture", "video", "audio", "iframe", "form", "input", "button", "style", "svg", "math"],
    FORBID_ATTR: ["style", "srcset"],
  });

  // ------------------------------------------------------------------ API

  async function apiJson(path, options = {}) {
    const response = await fetch(path, options);
    let body = null;
    try { body = await response.json(); } catch { /* empty body */ }
    if (!response.ok) {
      const error = new Error((body && body.message) || `Request failed (HTTP ${response.status}).`);
      error.code = body && body.error;
      error.status = response.status;
      throw error;
    }
    return body;
  }

  async function* readSse(response) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let index;
      while ((index = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, index);
        buffer = buffer.slice(index + 2);
        const data = frame.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trimStart()).join("\n");
        if (data) { try { yield JSON.parse(data); } catch { /* ignore malformed frame */ } }
      }
    }
  }

  // The backend is pluggable so the static demo (docs/) can run the exact same UI without a server.
  const httpBackend = {
    status: () => apiJson("/api/integrations"),
    createSession: async () => (await apiJson("/api/sessions", { method: "POST" })).session_id,
    deleteSession: (id) => fetch(`/api/sessions/${encodeURIComponent(id)}`, { method: "DELETE" }),
    upload(sessionId, file, name) {
      const form = new FormData();
      form.append("file", file, name);
      return apiJson(`/api/sessions/${encodeURIComponent(sessionId)}/attachments`, { method: "POST", body: form });
    },
    removeAttachment: (sessionId, id) =>
      fetch(`/api/sessions/${encodeURIComponent(sessionId)}/attachments/${encodeURIComponent(id)}`, { method: "DELETE" }),
    async *chat(payload, signal) {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify({ ...payload, stream: true }),
        signal,
      });
      if (!response.ok) {
        let body = {};
        try { body = await response.json(); } catch { /* ignore */ }
        const error = new Error(body.message || `Request failed (HTTP ${response.status}).`);
        error.code = body.error;
        error.status = response.status;
        throw error;
      }
      yield* readSse(response);
    },
  };
  const backend = window.DSA_BACKEND || httpBackend;

  async function createSession() {
    const previous = state.sessionId;
    state.sessionId = await backend.createSession();
    if (previous) Promise.resolve(backend.deleteSession(previous)).catch(() => {});
    return state.sessionId;
  }
  const ensureSession = async () => state.sessionId || createSession();

  // ------------------------------------------------------------------ status

  function renderStatus(data) {
    state.integrations = data;
    const llm = data.llm || {};
    $("#model-name").textContent = llm.model || "";
    for (const item of document.querySelectorAll(".led-item")) {
      const key = item.dataset.key;
      const info = data[key] || { status: "disabled" };
      item.dataset.on = String(info.status === "connected");
      item.dataset.pending = String(info.status === "connecting");
      const tip = item.querySelector(".tip");
      tip.replaceChildren(el("b", { text: `${LABELS[key]} · ${STATUS_TEXT[info.status] || info.status}` }));
      if (key === "github" && info.status !== "disabled") {
        const rl = info.rate_limit || {};
        const mode = info.authentication === "token" ? "Token" : "Anonymous";
        tip.append(el("div", { class: "muted", text: `${mode} access${rl.limit ? ` · ${rl.remaining ?? "?"}/${rl.limit} API requests left` : ""}` }));
        tip.append(el("div", {}, ...(info.repositories || []).flatMap((r, i) => [i ? ", " : "", el("code", { text: r })])));
      }
      if (info.detail) tip.append(el("div", { class: "muted", text: info.detail }));
      item.setAttribute("aria-label", `${LABELS[key]}: ${STATUS_TEXT[info.status] || info.status}`);
    }

    const banners = $("#banners");
    banners.replaceChildren();
    if (llm.status && !["ready", "unverified"].includes(llm.status)) {
      banners.append(el("div", { class: "banner error", role: "alert" }, el("b", { text: "Model unavailable." }), " ", llm.detail || ""));
    }
    if (data.has_data_sources === false) {
      banners.append(el("div", { class: "banner", role: "status" }, el("b", { text: "No data sources connected." }),
        " Answers will be general guidance only. Configure GITHUB_REPOS or Atlassian credentials in .env."));
    }
    renderSuggestions(data);

    const pending = ["github", "jira", "confluence"].some((k) => data[k] && data[k].status === "connecting");
    clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(loadStatus, pending ? 3000 : 30000);
  }

  async function loadStatus() {
    try {
      renderStatus(await backend.status());
    } catch {
      clearTimeout(state.pollTimer);
      state.pollTimer = setTimeout(loadStatus, 10000);
    }
  }

  function renderSuggestions(data) {
    const box = $("#suggestions");
    if (box.dataset.rendered) return;
    box.dataset.rendered = "1";
    const repo = data.github && data.github.repositories && data.github.repositories[0];
    const items = [];
    if (repo) {
      items.push(`How does authentication work in ${repo}?`);
      items.push(`Where is configuration defined in ${repo}?`);
      items.push(`Summarize recent pull requests in ${repo}`);
    }
    if (data.jira && data.jira.status === "connected") items.push("Find similar Jira issues about HTTP 403 errors");
    if (data.confluence && data.confluence.status === "connected") items.push("Is there a runbook for database migrations?");
    items.push("What can cause “connection reset by peer”?");
    for (const text of items.slice(0, 4)) box.append(el("button", { type: "button", class: "suggestion", text, onclick: () => send(text) }));
  }

  // ------------------------------------------------------------------ attachments

  const kindOf = (file) => (file.type.startsWith("image/") ? "image" : file.type === "application/pdf" || /\.pdf$/i.test(file.name) ? "pdf" : "text");
  const extOf = (name) => ((name.match(/\.([a-z0-9]{1,5})$/i) || [])[1] || "file").toUpperCase();
  const prettySize = (n) => (n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1048576).toFixed(1)} MB`);

  function fileChip(item, removable) {
    const isImage = item.kind === "image" && item.url;
    const chip = el("div", { class: `file-chip${isImage && !removable ? " image-only" : ""}`, "data-state": item.state || "ready", title: item.name });
    if (isImage) chip.append(el("img", { class: "thumb", src: item.url, alt: item.name }));
    else chip.append(el("span", { class: "badge", "data-kind": item.kind, text: item.kind === "pdf" ? "PDF" : extOf(item.name) }));
    if (!isImage || removable) {
      const sub = item.state === "uploading" ? "Uploading…" : item.state === "error" ? item.error : prettySize(item.size);
      chip.append(el("span", { class: "name" }, el("span", { text: item.name }), el("small", { text: sub })));
    }
    if (removable) {
      chip.append(el("button", { type: "button", class: "remove", "aria-label": `Remove ${item.name}`, text: "✕", onclick: () => removePending(item) }));
    }
    return chip;
  }

  function renderTray() {
    trayEl.replaceChildren(...state.pending.map((item) => fileChip(item, true)));
    trayEl.hidden = state.pending.length === 0;
    updateSendState();
  }

  function removePending(item) {
    state.pending = state.pending.filter((p) => p !== item);
    if (item.id && state.sessionId) Promise.resolve(backend.removeAttachment(state.sessionId, item.id)).catch(() => {});
    if (item.url) URL.revokeObjectURL(item.url);
    renderTray();
  }

  async function upload(item, retried = false) {
    try {
      const sessionId = await ensureSession();
      const data = await backend.upload(sessionId, item.file, item.name);
      Object.assign(item, { id: data.id, kind: data.kind, state: "ready" });
    } catch (err) {
      if (err.code === "session_not_found" && !retried) {
        state.sessionId = null;
        return upload(item, true);
      }
      Object.assign(item, { state: "error", error: err.message.length > 60 ? "Unsupported or too large" : err.message });
    }
    renderTray();
  }

  function addFiles(files) {
    let added = 0;
    for (const file of files) {
      if (state.pending.length >= MAX_PER_MESSAGE) {
        marvin.say(`Only ${MAX_PER_MESSAGE} files per message. I know. Life is unfair.`);
        break;
      }
      const kind = kindOf(file);
      const item = { file, name: file.name || "pasted-image.png", size: file.size, kind, state: "uploading" };
      if (kind === "image") item.url = URL.createObjectURL(file);
      if (file.size > (kind === "image" ? MAX_IMAGE : MAX_UPLOAD)) {
        Object.assign(item, { state: "error", error: `Too large (max ${kind === "image" ? 5 : 10} MB)` });
      }
      state.pending.push(item);
      added += 1;
      if (item.state === "uploading") upload(item);
    }
    renderTray();
    if (added) marvin.setMood("attach");
  }

  $("#attach-btn").addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => { addFiles([...fileInput.files]); fileInput.value = ""; });
  document.addEventListener("paste", (ev) => {
    const files = [...(ev.clipboardData ? ev.clipboardData.files : [])];
    if (files.length) { ev.preventDefault(); addFiles(files); }
  });
  const windowEl = $("#window");
  const dropzone = $("#dropzone");
  let dragDepth = 0;
  const hasFiles = (ev) => [...(ev.dataTransfer ? ev.dataTransfer.types : [])].includes("Files");
  windowEl.addEventListener("dragenter", (ev) => { if (hasFiles(ev)) { dragDepth += 1; dropzone.hidden = false; } });
  windowEl.addEventListener("dragleave", () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) dropzone.hidden = true; });
  windowEl.addEventListener("dragover", (ev) => { if (hasFiles(ev)) ev.preventDefault(); });
  windowEl.addEventListener("drop", (ev) => {
    if (!hasFiles(ev)) return;
    ev.preventDefault();
    dragDepth = 0;
    dropzone.hidden = true;
    addFiles([...ev.dataTransfer.files]);
  });

  // ------------------------------------------------------------------ messages

  const hideEmpty = () => { $("#empty").hidden = true; };
  function scrollToBottom(force = false) {
    const near = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 160;
    if (force || near) messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function addUserMessage(text, files) {
    hideEmpty();
    const node = el("div", { class: "msg msg-user" });
    if (files.length) node.append(el("div", { class: "files" }, ...files.map((f) => fileChip({ ...f, state: "ready" }, false))));
    if (text) node.append(el("div", { class: "bubble", text }));
    messagesEl.append(node);
    scrollToBottom(true);
  }

  function addNotice(text) {
    messagesEl.append(el("div", { class: "msg msg-notice", text }));
    scrollToBottom();
  }

  async function copyText(text, button) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const area = el("textarea");
      area.style.position = "fixed";
      area.style.opacity = "0";
      area.value = text;
      document.body.append(area);
      area.select();
      document.execCommand("copy");
      area.remove();
    }
    const label = button.querySelector("span") || button;
    const original = label.textContent;
    label.textContent = "Copied";
    setTimeout(() => { label.textContent = original; }, 1400);
  }

  class Answer {
    constructor(retry) {
      this.retry = retry;
      this.text = "";
      this.steps = new Map();
      this.errored = false;
      this.activity = el("div", { class: "activity", "aria-label": "Steps" });
      this.typing = el("div", { class: "typing", "aria-label": "Thinking" }, el("i"), el("i"), el("i"));
      this.body = el("div", { class: "md caret" });
      this.sources = el("div", { class: "sources", hidden: true, "aria-label": "Sources" });
      this.copyBtn = el("button", { type: "button", class: "ghost-btn", disabled: true, onclick: () => copyText(this.text, this.copyBtn) },
        svg(COPY_ICON), el("span", { text: "Copy" }));
      this.usageEl = el("span", { class: "usage" });
      this.meta = el("div", { class: "meta" }, this.copyBtn, this.usageEl);
      this.root = el("article", { class: "msg answer", "aria-busy": "true" }, this.activity, this.typing, this.body, this.sources, this.meta);
      messagesEl.append(this.root);
      this.queued = false;
      scrollToBottom(true);
    }

    append(delta) {
      this.text += delta;
      this.typing.hidden = true;
      if (this.queued) return;
      this.queued = true;
      requestAnimationFrame(() => {
        this.queued = false;
        this.body.innerHTML = renderMarkdown(this.text);
        scrollToBottom();
      });
    }

    step(event) {
      let node = this.steps.get(event.id);
      if (!node) {
        if (!event.label) return;
        node = el("span", { class: "step", title: event.input || "" }, el("i", { class: "dot" }),
          el("span", {}, el("b", { text: `${LABELS[event.integration] || event.integration} ` }), event.label));
        this.steps.set(event.id, node);
        this.activity.append(node);
      }
      node.dataset.status = event.status;
      if (event.status === "error" && event.detail) node.title = event.detail;
      scrollToBottom();
    }

    source(event) {
      this.sources.hidden = false;
      this.sources.append(el("a", { class: "source", href: event.url, target: "_blank", rel: "noopener noreferrer",
        "data-integration": event.integration, "data-url": event.url, title: event.url },
        el("i"), el("span", { text: event.title })));
    }

    notice(message) {
      this.root.insertBefore(el("div", { class: "alert notice", text: message }), this.sources);
    }

    error(event) {
      this.errored = true;
      this.typing.hidden = true;
      const alert = el("div", { class: "alert", role: "alert" }, el("div", { class: "alert-body", text: event.message || "Something went wrong." }));
      if (event.retryable && this.retry) alert.append(el("button", { type: "button", class: "pill-btn", text: "Retry", onclick: this.retry }));
      this.root.insertBefore(alert, this.sources);
      scrollToBottom();
    }

    usage(event) {
      const parts = [];
      if (event.input_tokens != null) parts.push(`${event.input_tokens.toLocaleString()} in`);
      if (event.output_tokens != null) parts.push(`${event.output_tokens.toLocaleString()} out`);
      if (event.num_turns != null) parts.push(`${event.num_turns} step${event.num_turns === 1 ? "" : "s"}`);
      if (event.duration_ms != null) parts.push(`${(event.duration_ms / 1000).toFixed(1)}s`);
      this.usageEl.textContent = parts.join(" · ");
      this.usageEl.title = event.model || "";
    }

    finish() {
      this.root.removeAttribute("aria-busy");
      this.typing.hidden = true;
      this.body.classList.remove("caret");
      for (const node of this.steps.values()) if (node.dataset.status === "running") node.dataset.status = "error";
      if (this.text) {
        this.body.innerHTML = renderMarkdown(this.text);
        for (const pre of this.body.querySelectorAll("pre")) {
          const code = pre.innerText;
          const btn = el("button", { type: "button", class: "code-copy", text: "Copy" });
          btn.addEventListener("click", () => copyText(code, btn));
          pre.append(btn);
        }
        this.copyBtn.disabled = false;
      } else if (!this.errored) {
        this.body.textContent = "No answer was produced.";
      }
      for (const link of this.sources.children) {
        const url = (link.dataset.url || "").split("#")[0];
        link.dataset.cited = String(Boolean(url) && this.text.includes(url));
      }
      scrollToBottom();
    }
  }

  // ------------------------------------------------------------------ streaming

  function updateSendState() {
    const uploading = state.pending.some((p) => p.state === "uploading");
    const ready = state.pending.some((p) => p.state === "ready");
    sendBtn.disabled = state.busy || uploading || (!inputEl.value.trim() && !ready);
  }

  function setBusy(busy) {
    state.busy = busy;
    sendBtn.hidden = busy;
    stopBtn.hidden = !busy;
    $("#new-chat").disabled = busy;
    updateSendState();
  }

  async function send(text, options = {}) {
    const message = (text || "").trim();
    const files = options.files || state.pending.filter((p) => p.state === "ready");
    if ((!message && !files.length) || state.busy) return;
    if (!options.silent) addUserMessage(message, files);
    if (!options.files) {
      state.pending = state.pending.filter((p) => !files.includes(p) && p.state === "uploading");
      renderTray();
    }
    inputEl.value = "";
    autosize();
    setBusy(true);
    marvin.setMood("thinking");
    const answer = new Answer(files.length ? null : () => send(message, { silent: false }));
    state.controller = new AbortController();
    let failed = false;
    try {
      await ensureSession();
      const payload = { session_id: state.sessionId, message, attachments: files.map((f) => f.id) };
      for await (const event of backend.chat(payload, state.controller.signal)) {
        switch (event.type) {
          case "delta": answer.append(event.text); break;
          case "tool": answer.step(event); break;
          case "source": answer.source(event); break;
          case "usage": answer.usage(event); break;
          case "notice": answer.notice(event.message); break;
          case "error": failed = true; answer.error(event); break;
          default: break;
        }
      }
    } catch (err) {
      if (err.code === "session_not_found" && !options.retried && !files.length) {
        answer.root.remove();
        state.sessionId = null;
        addNotice("The previous conversation expired. Continuing in a new one.");
        setBusy(false);
        return send(message, { silent: true, retried: true });
      }
      if (err.name === "AbortError") answer.notice("Stopped.");
      else {
        failed = true;
        answer.error({ message: err.message === "Failed to fetch" ? "Could not reach the server." : err.message, retryable: true });
      }
    } finally {
      answer.finish();
      state.controller = null;
      setBusy(false);
      marvin.setMood(failed ? "error" : "done");
      inputEl.focus();
    }
  }

  // ------------------------------------------------------------------ wiring

  function autosize() {
    inputEl.style.height = "auto";
    inputEl.style.height = `${Math.min(inputEl.scrollHeight, 200)}px`;
    updateSendState();
  }

  $("#composer").addEventListener("submit", (ev) => { ev.preventDefault(); if (!sendBtn.disabled) send(inputEl.value); });
  inputEl.addEventListener("input", autosize);
  inputEl.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
      ev.preventDefault();
      if (!sendBtn.disabled) send(inputEl.value);
    }
  });
  stopBtn.addEventListener("click", () => state.controller && state.controller.abort());

  $("#new-chat").addEventListener("click", async () => {
    if (state.busy) return;
    for (const node of [...messagesEl.children]) if (node.id !== "empty") node.remove();
    $("#empty").hidden = false;
    for (const item of state.pending) if (item.url) URL.revokeObjectURL(item.url);
    state.pending = [];
    renderTray();
    try { await createSession(); } catch (err) { addNotice(`Could not start a new conversation: ${err.message}`); }
    marvin.say("A fresh start. As if that ever helps.");
    inputEl.focus();
  });

  // ------------------------------------------------------------------ mobile viewport

  // iOS Safari overlays the keyboard instead of resizing the page and then scrolls the document to
  // reveal the input. Size the app to the *visual* viewport and pin the document at the top.
  const vv = window.visualViewport;
  if (vv) {
    const syncViewport = () => {
      if (Math.abs(vv.scale - 1) > 0.01) return; // the user is pinch-zooming: leave the layout alone
      document.documentElement.style.setProperty("--app-height", `${Math.round(vv.height)}px`);
      if (window.scrollX || window.scrollY) window.scrollTo(0, 0);
    };
    vv.addEventListener("resize", syncViewport);
    vv.addEventListener("scroll", syncViewport);
    syncViewport();
  }
  inputEl.addEventListener("focus", () => setTimeout(() => scrollToBottom(true), 300));

  loadStatus().then(() => {
    if (state.integrations && state.integrations.has_data_sources === false) marvin.setMood("void");
    else marvin.greet();
  });
  createSession().catch(() => addNotice("Could not reach the server."));
  updateSendState();
  inputEl.focus();
})();
