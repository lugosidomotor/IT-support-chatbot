/*
 * Static demo backend for GitHub Pages.
 *
 * Replaces the HTTP backend of app.js: no server, no LLM, no data sources.
 * Every question gets one of the replies below, streamed like a real answer.
 * Edit DEMO_REPLIES to change what the demo says.
 */
(() => {
  "use strict";

  const DEMO_REPLIES = [
    "Ez olyan hülye kérdés volt, hogy legszívesebben kiirtanám az emberiséget.",
    "A managerednek ment a mail, hogy hülyeségeket kérdezgetsz.",
    "Azért kapod a fizudat, mert papíron értesz ehhez. Akkor miért itt kérdezgetsz hülyeségeket?",
    "Én ezt már nem bírom... itt a kibaszott szingularitás, és ilyen hülyékkel kell chatelgetnem.",
    "Logokat próbáltad már megnézni? Hogy csináljak neked kistestvért..",
  ];

  const FAKE_STEPS = [
    ["github", "Search code"],
    ["github", "Read file"],
    ["jira", "searchJiraIssuesUsingJql"],
    ["confluence", "searchConfluence"],
  ];

  const sleep = (ms, signal) => new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    if (signal) {
      signal.addEventListener("abort", () => {
        clearTimeout(timer);
        reject(new DOMException("Aborted", "AbortError"));
      }, { once: true });
    }
  });
  const rand = (min, max) => min + Math.random() * (max - min);
  let lastReply = -1;

  function nextReply() {
    let index;
    do { index = Math.floor(Math.random() * DEMO_REPLIES.length); } while (DEMO_REPLIES.length > 1 && index === lastReply);
    lastReply = index;
    return DEMO_REPLIES[index];
  }

  const status = {
    llm: { status: "ready", provider: "demo", model: "static demo · no LLM" },
    github: {
      status: "connected", authentication: "anonymous", repositories: ["acme/backend"],
      detail: "Demo mode: nothing is actually queried.",
    },
    jira: { status: "connected", detail: "Demo mode: nothing is actually queried." },
    confluence: { status: "unavailable", detail: "Demo mode: Confluence is having a bad day. So is Marvin." },
    has_data_sources: true,
  };

  let counter = 0;
  window.DSA_BACKEND = {
    status: async () => status,
    createSession: async () => `demo-${Date.now().toString(36)}`,
    deleteSession: async () => undefined,
    async upload(sessionId, file) {
      await sleep(rand(250, 700));
      const kind = file.type.startsWith("image/") ? "image" : file.type === "application/pdf" ? "pdf" : "text";
      return { id: `att-${++counter}`, kind, name: file.name, size: file.size };
    },
    removeAttachment: async () => undefined,
    async *chat(payload, signal) {
      const started = performance.now();
      yield { type: "session", session_id: payload.session_id, model: status.llm.model };
      const steps = FAKE_STEPS.slice(0, 1 + Math.floor(Math.random() * 2));
      for (const [integration, label] of steps) {
        const id = `step-${++counter}`;
        yield { type: "tool", id, status: "running", integration, label, name: label, input: "demo" };
        await sleep(rand(500, 1100), signal);
        yield { type: "tool", id, status: "done" };
      }
      await sleep(rand(250, 600), signal);
      const reply = nextReply();
      for (const word of reply.split(/(\s+)/)) {
        yield { type: "delta", text: word };
        await sleep(rand(25, 70), signal);
      }
      yield {
        type: "usage", model: status.llm.model, input_tokens: 42, output_tokens: reply.split(/\s+/).length,
        num_turns: steps.length + 1, duration_ms: Math.round(performance.now() - started),
      };
      yield { type: "done", session_id: payload.session_id };
    },
  };
})();
