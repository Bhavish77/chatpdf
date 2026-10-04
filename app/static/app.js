(() => {
  "use strict";

  // ---------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------
  const state = {
    config: null,
    user: null,
    documents: [],
    threads: [],
    currentThreadId: null,
    taggedDocIds: [], // [{id, filename}]
    docsPollTimer: null,
    abortController: null,
  };

  const el = (id) => document.getElementById(id);

  // ---------------------------------------------------------------------
  // API helper
  // ---------------------------------------------------------------------
  const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

  async function api(path, options = {}) {
    const method = (options.method || "GET").toUpperCase();
    const headers = Object.assign({}, options.headers);
    if (!SAFE_METHODS.has(method)) {
      headers["X-Requested-With"] = "askdocs";
    }
    let response;
    try {
      response = await fetch(path, Object.assign({}, options, { method, headers }));
    } catch (networkError) {
      throw { networkError: true, original: networkError };
    }
    if (response.status === 401 && path !== "/api/auth/me") {
      showAuthScreen();
    }
    return response;
  }

  async function apiJson(path, options = {}) {
    const opts = Object.assign({}, options);
    if (opts.body && typeof opts.body !== "string") {
      opts.body = JSON.stringify(opts.body);
      opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers);
    }
    const response = await api(path, opts);
    let data = null;
    try {
      data = await response.json();
    } catch (_e) {
      data = null;
    }
    return { response, data };
  }

  // ---------------------------------------------------------------------
  // Boot
  // ---------------------------------------------------------------------
  async function boot() {
    try {
      const { response: configResp, data: config } = await apiJson("/api/config");
      if (!configResp.ok) throw new Error("config failed");
      state.config = config;
      applyConfigToAuthScreen(config);
    } catch (_e) {
      el("boot-message").textContent = "Waking the server, this can take about a minute…";
      setTimeout(boot, 4000);
      return;
    }

    const { response: meResp, data: me } = await apiJson("/api/auth/me");
    el("boot-message").hidden = true;
    if (meResp.ok) {
      state.user = me;
      await showApp();
    } else {
      showAuthScreen();
    }
  }

  function applyConfigToAuthScreen(config) {
    el("doc-ttl").textContent = config.doc_ttl_days;
    el("account-ttl").textContent = config.account_ttl_days;
    el("invite-field").hidden = !config.invite_required;
    if (!config.signups_enabled) {
      el("signups-closed-notice").hidden = false;
      el("tab-signup").hidden = true;
    }
  }

  // ---------------------------------------------------------------------
  // Auth screen
  // ---------------------------------------------------------------------
  function showAuthScreen() {
    state.user = null;
    if (state.docsPollTimer) clearTimeout(state.docsPollTimer);
    el("app").hidden = true;
    el("auth-screen").hidden = false;
  }

  async function showApp() {
    el("auth-screen").hidden = true;
    el("app").hidden = false;
    el("user-email").textContent = state.user.email;
    await Promise.all([refreshDocuments(), refreshThreads()]);
  }

  function setupTabs() {
    el("tab-signin").addEventListener("click", () => switchTab("signin"));
    el("tab-signup").addEventListener("click", () => switchTab("signup"));
  }

  function switchTab(which) {
    const isSignin = which === "signin";
    el("tab-signin").setAttribute("aria-selected", String(isSignin));
    el("tab-signup").setAttribute("aria-selected", String(!isSignin));
    el("signin-form").hidden = !isSignin;
    el("signup-form").hidden = isSignin;
  }

  function setupPasswordToggles() {
    document.querySelectorAll(".toggle-password").forEach((btn) => {
      btn.addEventListener("click", () => {
        const target = el(btn.dataset.target);
        const show = target.type === "password";
        target.type = show ? "text" : "password";
        btn.textContent = show ? "Hide" : "Show";
      });
    });
  }

  function showFieldError(id, message) {
    const node = el(id);
    node.textContent = message;
    node.hidden = !message;
  }

  function setupAuthForms() {
    el("signin-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      showFieldError("signin-error", "");
      const email = el("signin-email").value.trim();
      const password = el("signin-password").value;
      const { response, data } = await apiJson("/api/auth/login", { method: "POST", body: { email, password } });
      if (!response.ok) {
        showFieldError("signin-error", friendlyAuthError(response, data));
        return;
      }
      state.user = data;
      await showApp();
    });

    el("signup-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      showFieldError("signup-error", "");
      const email = el("signup-email").value.trim();
      const password = el("signup-password").value;
      const invite_code = el("signup-invite").value;
      const { response, data } = await apiJson("/api/auth/signup", {
        method: "POST",
        body: { email, password, invite_code },
      });
      if (!response.ok) {
        showFieldError("signup-error", friendlyAuthError(response, data));
        return;
      }
      state.user = data;
      await showApp();
    });
  }

  function friendlyAuthError(response, data) {
    if (response.status === 429) {
      const retryAfter = response.headers.get("Retry-After");
      return retryAfter
        ? `Too many attempts. Try again in ${retryAfter} seconds.`
        : "Too many attempts. Try again later.";
    }
    return (data && data.detail) || "Something went wrong. Please try again.";
  }

  // ---------------------------------------------------------------------
  // User menu / account
  // ---------------------------------------------------------------------
  function setupUserMenu() {
    const button = el("user-menu-button");
    const dropdown = el("user-menu-dropdown");
    button.addEventListener("click", () => {
      const open = !dropdown.hidden;
      dropdown.hidden = open;
      button.setAttribute("aria-expanded", String(!open));
    });
    document.addEventListener("click", (e) => {
      if (!dropdown.contains(e.target) && !button.contains(e.target)) {
        dropdown.hidden = true;
        button.setAttribute("aria-expanded", "false");
      }
    });

    el("logout-button").addEventListener("click", async () => {
      await api("/api/auth/logout", { method: "POST" });
      showAuthScreen();
    });

    el("delete-account-button").addEventListener("click", openDeleteAccountModal);
  }

  function openDeleteAccountModal() {
    const modal = el("confirm-modal");
    el("confirm-modal-password").value = "";
    showFieldError("confirm-modal-error", "");
    modal.hidden = false;
    el("confirm-modal-password").focus();

    const close = () => { modal.hidden = true; };
    el("confirm-modal-cancel").onclick = close;
    el("confirm-modal-ok").onclick = async () => {
      const password = el("confirm-modal-password").value;
      const { response, data } = await apiJson("/api/auth/me", { method: "DELETE", body: { password } });
      if (!response.ok) {
        showFieldError("confirm-modal-error", (data && data.detail) || "Could not delete account.");
        return;
      }
      close();
      showAuthScreen();
    };
  }

  // ---------------------------------------------------------------------
  // Documents: upload, list, polling
  // ---------------------------------------------------------------------
  function setupUpload() {
    const dropzone = el("dropzone");
    const fileInput = el("file-input");

    dropzone.addEventListener("click", () => fileInput.click());
    dropzone.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
    });
    fileInput.addEventListener("change", () => {
      if (fileInput.files.length) uploadFiles(fileInput.files);
      fileInput.value = "";
    });

    ["dragenter", "dragover"].forEach((evt) =>
      dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.add("dragover"); })
    );
    ["dragleave", "drop"].forEach((evt) =>
      dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.remove("dragover"); })
    );
    dropzone.addEventListener("drop", (e) => {
      if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files);
    });
  }

  async function uploadFiles(fileList) {
    showFieldError("upload-error", "");
    const formData = new FormData();
    Array.from(fileList).forEach((f) => formData.append("files", f));
    const response = await api("/api/documents", { method: "POST", body: formData });
    if (!response.ok) {
      const data = await response.json().catch(() => null);
      showFieldError("upload-error", (data && data.detail) || "Upload failed.");
      return;
    }
    await refreshDocuments();
  }

  async function refreshDocuments() {
    const { response, data } = await apiJson("/api/documents");
    if (!response.ok) return;
    state.documents = data;
    renderDocumentList();
    scheduleDocsPoll();
  }

  function renderDocumentList() {
    const list = el("document-list");
    list.innerHTML = "";
    state.documents.forEach((doc) => {
      const li = document.createElement("li");
      li.className = "document-row";

      const top = document.createElement("div");
      top.className = "row-top";
      const name = document.createElement("span");
      name.className = "filename";
      name.textContent = doc.filename;
      name.title = doc.filename;
      const badge = document.createElement("span");
      badge.className = `badge badge-${doc.status}`;
      badge.textContent = doc.status;
      top.append(name, badge);
      li.append(top);

      if (doc.status !== "ready" && doc.status !== "failed") {
        const bar = document.createElement("div");
        bar.className = "progress-bar";
        const fill = document.createElement("div");
        fill.style.width = `${doc.progress || 0}%`;
        bar.append(fill);
        li.append(bar);
      }

      if (doc.error) {
        const err = document.createElement("p");
        err.className = "error-text";
        err.textContent = doc.error;
        li.append(err);
      }

      const actions = document.createElement("div");
      actions.className = "row-actions";

      if (doc.status === "ready") {
        const askBtn = document.createElement("button");
        askBtn.type = "button";
        askBtn.textContent = "Ask about this";
        askBtn.addEventListener("click", () => addTag(doc));
        actions.append(askBtn);
      }

      if (!doc.is_public) {
        const delBtn = document.createElement("button");
        delBtn.type = "button";
        delBtn.textContent = "Delete";
        delBtn.addEventListener("click", () => deleteDocument(doc.id));
        actions.append(delBtn);
      }

      li.append(actions);
      list.append(li);
    });
  }

  async function deleteDocument(id) {
    const response = await api(`/api/documents/${id}`, { method: "DELETE" });
    if (response.ok) await refreshDocuments();
  }

  function scheduleDocsPoll() {
    if (state.docsPollTimer) clearTimeout(state.docsPollTimer);
    const busy = state.documents.some((d) => d.status !== "ready" && d.status !== "failed");
    state.docsPollTimer = setTimeout(refreshDocuments, busy ? 1500 : 20000);
  }

  // ---------------------------------------------------------------------
  // Threads
  // ---------------------------------------------------------------------
  async function refreshThreads() {
    const { response, data } = await apiJson("/api/threads");
    if (!response.ok) return;
    state.threads = data;
    renderThreadList();
  }

  function renderThreadList() {
    const list = el("thread-list");
    list.innerHTML = "";
    state.threads.forEach((thread) => {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = new Date(thread.created_at).toLocaleString();
      if (thread.id === state.currentThreadId) btn.classList.add("active");
      btn.addEventListener("click", () => openThread(thread.id));
      li.append(btn);
      list.append(li);
    });
  }

  async function openThread(threadId) {
    const { response, data } = await apiJson(`/api/threads/${threadId}/messages`);
    if (!response.ok) return;
    state.currentThreadId = threadId;
    el("messages").innerHTML = "";
    data.forEach((msg) => {
      if (msg.role === "user") {
        appendUserMessage(msg.content);
      } else {
        const { bubble } = appendAssistantMessage();
        renderMarkdownInto(bubble, msg.content);
        if (msg.citations && msg.citations.length) renderCitations(bubble, msg.citations);
        if (msg.trace && msg.trace.grounding) renderGrounding(bubble, msg.trace.grounding);
        if (msg.trace && msg.trace.steps) renderHowAnswered(bubble, msg.trace.steps, []);
      }
    });
    renderThreadList();
  }

  function startNewChat() {
    state.currentThreadId = null;
    el("messages").innerHTML = "";
    renderThreadList();
  }

  // ---------------------------------------------------------------------
  // Tagging
  // ---------------------------------------------------------------------
  function addTag(doc) {
    if (state.taggedDocIds.some((t) => t.id === doc.id)) return;
    state.taggedDocIds.push({ id: doc.id, filename: doc.filename });
    renderTagChips();
  }

  function removeTag(id) {
    state.taggedDocIds = state.taggedDocIds.filter((t) => t.id !== id);
    renderTagChips();
  }

  function renderTagChips() {
    const container = el("tag-chips");
    container.innerHTML = "";
    state.taggedDocIds.forEach((tag) => {
      const chip = document.createElement("span");
      chip.className = "tag-chip";
      chip.textContent = tag.filename;
      const removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.textContent = "×";
      removeBtn.setAttribute("aria-label", `Remove ${tag.filename}`);
      removeBtn.addEventListener("click", () => removeTag(tag.id));
      chip.append(removeBtn);
      container.append(chip);
    });

    const indicator = el("scope-indicator");
    indicator.textContent = state.taggedDocIds.length
      ? `Searching: ${state.taggedDocIds.map((t) => t.filename).join(", ")}`
      : "Searching all your documents";
  }

  function setupTaggingAutocomplete() {
    const input = el("chat-input");
    const list = el("autocomplete");

    input.addEventListener("input", () => {
      const value = input.value;
      const at = value.lastIndexOf("@");
      if (at === -1 || /\s/.test(value.slice(at + 1))) {
        list.hidden = true;
        return;
      }
      const query = value.slice(at + 1).toLowerCase();
      const ready = state.documents.filter(
        (d) => d.status === "ready" && d.filename.toLowerCase().includes(query)
      );
      if (!ready.length) {
        list.hidden = true;
        return;
      }
      list.innerHTML = "";
      ready.forEach((doc) => {
        const li = document.createElement("li");
        const btn = document.createElement("button");
        btn.type = "button";
        btn.textContent = doc.filename;
        btn.addEventListener("click", () => {
          input.value = value.slice(0, at);
          addTag(doc);
          list.hidden = true;
          input.focus();
        });
        li.append(btn);
        list.append(li);
      });
      list.hidden = false;
    });

    document.addEventListener("click", (e) => {
      if (!list.contains(e.target) && e.target !== input) list.hidden = true;
    });
  }

  // ---------------------------------------------------------------------
  // Chat / SSE
  // ---------------------------------------------------------------------
  function appendUserMessage(text) {
    const wrap = document.createElement("div");
    wrap.className = "message user";
    const bubble = document.createElement("div");
    bubble.className = "message-bubble";
    bubble.textContent = text;
    wrap.append(bubble);
    el("messages").append(wrap);
    el("messages").scrollTop = el("messages").scrollHeight;
  }

  function appendAssistantMessage() {
    const wrap = document.createElement("div");
    wrap.className = "message assistant";
    const bubble = document.createElement("div");
    bubble.className = "message-bubble";
    wrap.append(bubble);
    el("messages").append(wrap);
    el("messages").scrollTop = el("messages").scrollHeight;
    return { wrap, bubble };
  }

  function renderMarkdownInto(bubble, text) {
    const html = window.marked.parse(text);
    bubble.innerHTML = window.DOMPurify.sanitize(html);
  }

  function renderCitations(bubble, citations) {
    const row = document.createElement("div");
    row.className = "citation-chips";
    citations.forEach((c) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "citation-chip";
      chip.textContent = c.page ? `[${c.n}] ${c.filename} · p.${c.page}` : `[${c.n}] ${c.filename}`;
      chip.addEventListener("click", (e) => showCitationPopover(e.currentTarget, c));
      row.append(chip);
    });
    bubble.append(row);
  }

  function showCitationPopover(anchor, citation) {
    const popover = el("citation-popover");
    popover.innerHTML = "";
    const snippet = document.createElement("p");
    snippet.textContent = citation.snippet;
    const link = document.createElement("a");
    link.href = `/api/documents/${citation.document_id}/file${citation.page ? `#page=${citation.page}` : ""}`;
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = "Open at page";
    popover.append(snippet, link);

    const rect = anchor.getBoundingClientRect();
    popover.style.top = `${window.scrollY + rect.bottom + 4}px`;
    popover.style.left = `${window.scrollX + rect.left}px`;
    popover.hidden = false;

    const closeOnOutsideClick = (e) => {
      if (!popover.contains(e.target) && e.target !== anchor) {
        popover.hidden = true;
        document.removeEventListener("click", closeOnOutsideClick);
      }
    };
    setTimeout(() => document.addEventListener("click", closeOnOutsideClick), 0);
  }

  function renderGrounding(bubble, grounding) {
    const badge = document.createElement("span");
    badge.className = `grounding-badge ${grounding.grounded ? "grounded" : "ungrounded"}`;
    badge.textContent = grounding.grounded ? "Grounded" : "Some claims may not be supported";
    bubble.append(badge);
  }

  function renderHowAnswered(bubble, steps, timings) {
    const details = document.createElement("details");
    details.className = "how-answered";
    const summary = document.createElement("summary");
    summary.textContent = "How I answered";
    const ol = document.createElement("ol");
    steps.forEach((step, i) => {
      const li = document.createElement("li");
      const t = timings[i] !== undefined ? ` (${timings[i]}ms)` : "";
      li.textContent = `${step.node}: ${typeof step.detail === "string" ? step.detail : JSON.stringify(step.detail)}${t}`;
      ol.append(li);
    });
    details.append(summary, ol);
    bubble.append(details);
  }

  function parseSseBuffer(buffer) {
    const events = [];
    const blocks = buffer.split("\n\n");
    const remainder = blocks.pop();
    for (const block of blocks) {
      let eventType = null, data = null;
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) eventType = line.slice(7);
        else if (line.startsWith("data: ")) data = line.slice(6);
      }
      if (eventType && data !== null) {
        try { events.push({ type: eventType, data: JSON.parse(data) }); } catch (_e) { /* ignore */ }
      }
    }
    return { events, remainder };
  }

  function setupChatForm() {
    el("chat-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const input = el("chat-input");
      const message = input.value.trim();
      if (!message) return;
      input.value = "";
      await sendChatMessage(message);
    });

    el("new-chat-button").addEventListener("click", startNewChat);

    el("stop-button").addEventListener("click", () => {
      if (state.abortController) state.abortController.abort();
    });

    document.querySelectorAll("#suggested-questions-list button").forEach((btn) => {
      btn.addEventListener("click", () => sendChatMessage(btn.textContent));
    });
  }

  async function sendChatMessage(message) {
    appendUserMessage(message);
    const { bubble } = appendAssistantMessage();
    const startTime = Date.now();
    const steps = [];
    const timings = [];

    const body = {
      message,
      thread_id: state.currentThreadId,
      doc_ids: state.taggedDocIds.map((t) => t.id),
    };

    state.abortController = new AbortController();
    el("stop-button").hidden = false;
    el("send-button").disabled = true;

    let answerText = "";
    let citations = null;
    let grounding = null;

    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Requested-With": "askdocs" },
        body: JSON.stringify(body),
        signal: state.abortController.signal,
      });

      if (!response.ok) {
        const data = await response.json().catch(() => null);
        bubble.textContent = chatErrorMessage(response, data);
        return;
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const { events, remainder } = parseSseBuffer(buffer);
        buffer = remainder;

        for (const evt of events) {
          if (evt.type === "step") {
            steps.push(evt.data);
            timings.push(Date.now() - startTime);
          } else if (evt.type === "token") {
            answerText += evt.data.text;
            renderMarkdownInto(bubble, answerText);
          } else if (evt.type === "citations") {
            citations = evt.data;
          } else if (evt.type === "grounding") {
            grounding = evt.data;
          } else if (evt.type === "error") {
            bubble.textContent = evt.data.message;
            return;
          } else if (evt.type === "done") {
            state.currentThreadId = evt.data.thread_id;
          }
        }
      }

      renderMarkdownInto(bubble, answerText || "(no answer)");
      if (citations && citations.length) renderCitations(bubble, citations);
      if (grounding) renderGrounding(bubble, grounding);
      if (steps.length) renderHowAnswered(bubble, steps, timings);
      await refreshThreads();
    } catch (err) {
      if (err.name !== "AbortError") {
        bubble.textContent = "Something went wrong. Please try again.";
      }
    } finally {
      state.abortController = null;
      el("stop-button").hidden = true;
      el("send-button").disabled = false;
      state.taggedDocIds = [];
      renderTagChips();
    }
  }

  function chatErrorMessage(response, data) {
    if (response.status === 404) return "That document or chat could not be found.";
    if (response.status === 429) {
      const retryAfter = response.headers.get("Retry-After");
      return retryAfter ? `You're sending messages too fast. Try again in ${retryAfter} seconds.` : "You're sending messages too fast.";
    }
    return (data && data.detail) || "Something went wrong. Please try again.";
  }

  // ---------------------------------------------------------------------
  // Suggested questions (for the public seed document)
  // ---------------------------------------------------------------------
  async function loadSuggestedQuestions() {
    try {
      const response = await fetch("/api/seed-suggestions");
      if (!response.ok) return;
      const suggestions = await response.json();
      if (!suggestions.length) return;
      const list = el("suggested-questions-list");
      list.innerHTML = "";
      suggestions.forEach((q) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.textContent = q;
        btn.addEventListener("click", () => sendChatMessage(q));
        list.append(btn);
      });
      el("suggested-questions").hidden = false;
    } catch (_e) {
      // optional feature; ignore failures
    }
  }

  // ---------------------------------------------------------------------
  // Init
  // ---------------------------------------------------------------------
  function init() {
    setupTabs();
    setupPasswordToggles();
    setupAuthForms();
    setupUserMenu();
    setupUpload();
    setupTaggingAutocomplete();
    setupChatForm();
    loadSuggestedQuestions();
    boot();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
