/* =============================================================================
   设备借用助手 · 前端逻辑（原生 JS，无构建、无依赖）

   三个必须手写的东西，也是这个文件存在的理由：
     1) 不能用 EventSource —— 它只支持 GET，而我们要 POST JSON。
        必须 fetch + response.body.getReader() + TextDecoder 自己解 SSE 帧。
     2) 必须处理半包 —— 网络分片可能把一帧劈成两半，按 "\n\n" 切完，
        最后不完整的那一截要留在缓冲区等下一个 chunk，否则会随机丢字。
     3) 打字机必须限速 —— 模型一次可能吐 50 个字，直接 append 会"蹦字"。
        用队列 + requestAnimationFrame，每帧只吐 1~3 字（队列积压时提速追赶）。

   安全硬要求：模型输出、制度片段一律用 textContent / createTextNode 写入。
   制度文档是可编辑内容，用 innerHTML 就是给 XSS 开门。
   ========================================================================== */

(() => {
  "use strict";

  const API = "/api/v1";
  const SESSION_KEY = "sdb_session_id";

  const el = {
    messages: document.getElementById("messages"),
    empty: document.getElementById("empty-state"),
    form: document.getElementById("form"),
    input: document.getElementById("input"),
    send: document.getElementById("send"),
    counter: document.getElementById("counter"),
    statusLine: document.getElementById("status-line"),
    statusText: document.getElementById("status-text"),
    newChat: document.getElementById("new-chat"),
    kbState: document.getElementById("kb-state"),
    llmState: document.getElementById("llm-state"),
    degraded: document.getElementById("degraded-badge"),
    toast: document.getElementById("toast"),
    meName: document.getElementById("me-name"),
    meSub: document.getElementById("me-sub"),
    meAdmin: document.getElementById("me-admin"),
    btnLogout: document.getElementById("btn-logout"),
  };

  const state = {
    sessionId: null,
    busy: false,
    abort: null,
    lastQuestion: "",
    // 打字机
    queue: [],
    node: null,
    raf: null,
  };

  // ---------------------------------------------------------------- 工具
  const uuid = () => (crypto.randomUUID
    ? crypto.randomUUID()
    : "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
        const r = (Math.random() * 16) | 0;
        return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
      }));

  function toast(text) {
    el.toast.textContent = text;
    el.toast.hidden = false;
    el.toast.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => {
      el.toast.classList.remove("show");
      setTimeout(() => { el.toast.hidden = true; }, 220);
    }, 3600);
  }

  // ---------------------------------------------------------------- 滚动跟随
  // 用「用户是否主动上滚」来决定要不要跟随，而不是每次算几何距离。
  // 原因：流式输出时内容长得比滚动快，一旦距离超过阈值，几何判断就会永久失效，
  // 表现为「用户什么都没做，视图却停在半截」。stick 只在用户真的往上滚时才关掉。
  state.stick = true;

  el.messages.addEventListener("scroll", () => {
    const m = el.messages;
    state.stick = m.scrollHeight - m.scrollTop - m.clientHeight < 80;
  });

  function scrollToBottom(force) {
    if (force || state.stick) {
      el.messages.scrollTop = el.messages.scrollHeight;
    }
  }

  function hideEmpty() {
    if (el.empty && el.empty.parentNode) el.empty.remove();
  }

  // ---------------------------------------------------------------- 打字机
  function pumpText(text) {
    state.queue.push(...text);
    if (!state.raf) state.raf = requestAnimationFrame(tick);
  }

  function tick() {
    state.raf = null;
    if (!state.node || state.queue.length === 0) return;
    const n = state.queue.length;
    // 队列长说明网络比屏幕快，多吐几个字追上去；否则一次一个字，看起来才像"在写"
    const take = n > 120 ? 12 : n > 30 ? 3 : 1;
    state.node.textContent += state.queue.splice(0, take).join("");
    scrollToBottom(false);
    state.raf = requestAnimationFrame(tick);
  }

  function flushText() {
    if (state.node && state.queue.length) {
      state.node.textContent += state.queue.join("");
      state.queue = [];
    }
    if (state.raf) {
      cancelAnimationFrame(state.raf);
      state.raf = null;
    }
  }

  // ---------------------------------------------------------------- SSE 解析
  function parseFrame(raw) {
    let event = "message";
    let data = "";
    for (const line of raw.split("\n")) {
      if (!line || line.startsWith(":")) continue;      // 心跳帧
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data += line.slice(5).trim();
    }
    if (!data) return null;
    try {
      return { event, data: JSON.parse(data) };
    } catch {
      return null;
    }
  }

  async function readSSE(response, onEvent) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const raw = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const evt = parseFrame(raw);
        if (evt) onEvent(evt.event, evt.data);
      }
    }
  }

  // ---------------------------------------------------------------- 渲染
  function addUserMessage(text) {
    const wrap = document.createElement("div");
    wrap.className = "msg user";
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text;              // 不用 innerHTML
    wrap.appendChild(bubble);
    el.messages.appendChild(wrap);
    scrollToBottom(true);
  }

  function addBotMessage() {
    const wrap = document.createElement("div");
    wrap.className = "msg bot";

    const bubble = document.createElement("div");
    bubble.className = "bubble";

    const body = document.createElement("div");
    body.className = "body typing";
    bubble.appendChild(body);

    const cites = document.createElement("div");
    cites.className = "cites";
    bubble.appendChild(cites);

    wrap.appendChild(bubble);
    el.messages.appendChild(wrap);
    scrollToBottom(true);
    return { wrap, bubble, body, cites };
  }

  // ------------------------------------------------------- 待确认卡片
  // 这是整个界面里**唯一会改变数据**的元素。三条约束都是安全性的，不是美观：
  //   1) 卡片内容全部来自服务端 summary，前端不做任何拼接或换算——
  //      日期、时段一旦被前端改写，用户确认的东西就和服务端执行的东西不一致了；
  //   2) 按钮只在 status=pending 时出现；
  //   3) 点击后立刻禁用。服务端有幂等闸门兜底，但界面不该鼓励重复提交。
  const PENDING_STATUS_TEXT = {
    confirmed: "已确认，预约已生效。",
    rejected: "已取消，没有提交任何预约。",
    expired: "这次确认已超时失效，请重新发起。",
    failed: "执行失败——可能这一格刚被别人约走了，请重新查一下空闲时段。",
    gone: "这条请求已失效。",
  };

  async function renderConfirmCard(container, summary, pendingId) {
    const card = document.createElement("div");
    card.className = "confirm-card";

    const head = document.createElement("div");
    head.className = "cc-head";
    head.textContent = summary.title || "待确认";

    const body = document.createElement("div");
    body.className = "cc-body";
    const rows = [
      ["设备", `${summary.asset_no || ""} ${summary.device_name || ""}`.trim()],
      ["位置", summary.location || ""],
      ["日期", summary.slot_date || ""],
      ["时段", summary.slot_text || ""],
    ];
    for (const [k, v] of rows) {
      if (!v) continue;
      const row = document.createElement("div");
      row.className = "cc-row";
      const kk = document.createElement("span");
      kk.textContent = k;
      const vv = document.createElement("b");
      vv.textContent = v;
      row.append(kk, vv);
      body.appendChild(row);
    }

    const note = document.createElement("div");
    note.className = "cc-note";
    note.textContent = summary.effect
      ? `${summary.effect}。在你点击之前，这一单还没有生效。`
      : "在你点击之前，这一单还没有生效。";

    const actions = document.createElement("div");
    actions.className = "cc-actions";

    card.append(head, body, note, actions);
    container.appendChild(card);
    container.classList.add("has-card");

    // 先查真实状态。刷新页面后这张卡片可能早就被处理过，
    // 只看历史里的快照会显示成"待确认"，用户点了才报错——体验最差的一种。
    let status = "pending";
    try {
      const r = await fetch(`${API}/agent/pending/${pendingId}`, { credentials: "same-origin" });
      if (r.ok) {
        status = (await r.json()).status || "pending";
      } else if (r.status === 404 || r.status === 403) {
        status = "gone";
      }
    } catch {
      /* 查询失败就按 pending 渲染；点击时服务端仍会兜底 */
    }

    if (status !== "pending") {
      note.textContent = PENDING_STATUS_TEXT[status] || "这条请求已经处理过了。";
      note.classList.add("cc-done");
      return card;
    }

    const okBtn = document.createElement("button");
    okBtn.type = "button";
    okBtn.className = "primary";
    okBtn.textContent = summary.kind === "cancel" ? "确认取消" : "确认预约";

    const noBtn = document.createElement("button");
    noBtn.type = "button";
    noBtn.className = "ghost";
    noBtn.textContent = "不用了";

    function reset() {
      okBtn.disabled = false;
      noBtn.disabled = false;
      okBtn.textContent = summary.kind === "cancel" ? "确认取消" : "确认预约";
    }

    function decide(decision) {
      okBtn.disabled = true;
      noBtn.disabled = true;
      if (decision === "confirm") okBtn.textContent = "提交中…";
      confirmAction(card, pendingId, decision, reset);
    }

    okBtn.addEventListener("click", () => decide("confirm"));
    noBtn.addEventListener("click", () => decide("reject"));

    actions.append(okBtn, noBtn);
    scrollToBottom(false);
    return card;
  }

  async function confirmAction(card, pendingId, decision, onFail) {
    const actions = card.querySelector(".cc-actions");
    try {
      const resp = await fetch(`${API}/agent/confirm`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ pending_id: pendingId, decision }),
      });
      const data = await resp.json().catch(() => null);

      if (!resp.ok) {
        actions.textContent = "";
        const err = document.createElement("div");
        err.className = "cc-error";
        // 服务端给的中文 message 优先，它比前端猜的准
        err.textContent = (data && data.message) || "操作失败，请重试";
        actions.appendChild(err);
        if (onFail) onFail();
        return;
      }

      // 成功：整张卡片换成结果。留着按钮会让人以为还能再点一次。
      card.dataset.state = decision === "confirm" ? "done" : "rejected";
      // 提示语必须跟着改：原来那句"在你点击之前，这一单还没有生效"，
      // 在已经点完之后继续显示就是自相矛盾——用户会怀疑到底生没生效。
      const note = card.querySelector(".cc-note");
      if (note) {
        note.textContent = decision === "confirm"
          ? "已确认，这次操作已完成。"
          : "已取消，没有提交任何预约。";
        note.classList.add("cc-done");
      }
      actions.textContent = "";
      const done = document.createElement("div");
      done.className = "cc-result";
      done.textContent = data.message || "已处理";
      actions.appendChild(done);
      scrollToBottom(false);
    } catch {
      actions.textContent = "";
      const err = document.createElement("div");
      err.className = "cc-error";
      err.textContent = "网络异常，请稍后重试";
      actions.appendChild(err);
      if (onFail) onFail();
    }
  }

  function renderCitations(container, hits) {
    container.textContent = "";
    if (!hits || hits.length === 0) return;

    // 待确认卡片与制度引用共用这个容器，但渲染方式完全不同，用 kind 分流。
    // 除 pending_action 外的 kind（booking_confirmed 等）不在这里渲染：
    // 它们的正文已经由服务端写进消息 content 了，再画一个徽章只是重复。
    const pending = hits.filter((h) => h && h.kind === "pending_action");
    const cites = hits.filter((h) => h && !h.kind);

    for (const p of pending) {
      renderConfirmCard(container, p.summary || {}, p.pending_id);
    }
    if (cites.length === 0) return;

    const title = document.createElement("div");
    title.className = "cites-title";
    title.textContent = `引用来源（${cites.length} 条）`;
    container.appendChild(title);

    for (const hit of cites) {
      const details = document.createElement("details");
      details.className = "cite";

      const summary = document.createElement("summary");
      const idx = document.createElement("span");
      idx.className = "idx";
      idx.textContent = `[${hit.index}]`;
      const src = document.createElement("span");
      src.className = "src";
      src.textContent = hit.source;                    // 文件名
      const hd = document.createElement("span");
      hd.className = "hd";
      hd.textContent = hit.heading || "";              // 章节（叶标题，一行放得下）
      if (hit.path) summary.title = hit.path;          // 完整路径只在悬浮时给
      summary.append(idx, src, hd);

      const snip = document.createElement("p");
      snip.className = "snip";
      snip.textContent = hit.snippet || "";

      details.append(summary, snip);
      container.appendChild(details);
    }
    scrollToBottom(false);
  }

  function addError(bubble, code, message, retry) {
    const box = document.createElement("div");
    box.className = "error-box";
    const span = document.createElement("span");
    // code 给排查用，message 给用户看
    span.textContent = `${message}${code ? `（${code}）` : ""}`;
    box.appendChild(span);
    if (retry) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = "重试";
      btn.addEventListener("click", () => {
        if (state.busy) return;
        box.remove();
        send(state.lastQuestion);
      });
      box.appendChild(btn);
    }
    bubble.appendChild(box);
    scrollToBottom(false);
  }

  // ---------------------------------------------------------------- 发送
  async function send(question) {
    if (state.busy || !question) return;
    state.busy = true;
    state.lastQuestion = question;
    setBusyUI(true);

    hideEmpty();
    addUserMessage(question);

    const view = addBotMessage();
    state.node = view.body;
    state.queue = [];

    let finished = false;
    // 不能是 const：智能体每次检索后发的是**合并后的完整列表**，
    // 所以要整体替换而不是 push。push 会让同一条制度在多次检索后重复出现。
    let citations = [];

    state.abort = new AbortController();

    try {
      const resp = await fetch(`${API}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",     // 必须带 cookie，否则会话归属校验不过
        signal: state.abort.signal,
        body: JSON.stringify({
          session_id: state.sessionId,
          question,
          history_turns: 6,
        }),
      });

      // 非 200：这是**开流前**的失败，body 是标准错误 JSON
      if (!resp.ok) {
        const err = await resp.json().catch(() => null);
        // 401 意味着登录态失效（Cookie 过期/被清）：跳登录页，不要在气泡里报错。
        // 用户看到的应该是登录框，而不是"请先登录"这句话。
        if (resp.status === 401) {
          window.location.href = "/login";
          return;
        }
        view.body.classList.remove("typing");
        addError(
          view.bubble,
          err ? err.code : `HTTP_${resp.status}`,
          err ? err.message : "请求失败，请稍后重试",
          err && !["SESSION_FORBIDDEN", "INVALID_SESSION_ID"].includes(err.code)
        );
        finished = true;
        return;
      }

      await readSSE(resp, (event, data) => {
        switch (event) {
          case "meta":
            if (data.session_id) {
              state.sessionId = data.session_id;
              localStorage.setItem(SESSION_KEY, data.session_id);
            }
            break;

          case "status":
            showStatus(data.text || "处理中…");
            break;

          case "citations":
            citations = data.hits || [];
            renderCitations(view.cites, citations);
            hideStatus();
            break;

          case "delta":
            pumpText(data.text || "");
            break;

          case "confirm_required":
            // 模型想写库，但被拦下了。正文由服务端给出（它保证说清"还没生效"），
            // 卡片由 renderConfirmCard 渲染，两者是配套的。
            finished = true;
            flushText();
            view.body.classList.remove("typing");
            hideStatus();
            renderConfirmCard(view.cites, data.summary || {}, data.pending_id);
            break;

          case "done":
            finished = true;
            flushText();
            view.body.classList.remove("typing");
            el.degraded.hidden = !data.degraded;
            hideStatus();
            // 中断时不算"引用了几条制度"，也不是普通回答，不画统计行。
            if (!data.degraded && !data.interrupted) {
              const meta = document.createElement("div");
              meta.className = "meta-line";
              const secs = (data.elapsed_ms / 1000).toFixed(1);
              const parts = [`${secs}s`];
              if (citations.length) parts.push(`引用 ${citations.length} 条制度`);
              if (data.rounds) parts.push(`${data.rounds} 轮分析`);
              meta.textContent = parts.join(" · ");
              view.bubble.appendChild(meta);
            }
            break;

          case "error":
            finished = true;
            flushText();
            view.body.classList.remove("typing");
            addError(view.bubble, data.code, data.message, data.recoverable);
            hideStatus();
            break;

          default:
            break;
        }
      });

      // 连接正常关闭但没收到 done：多半是网关断了，要明确告诉用户，别装作答完了
      if (!finished) {
        flushText();
        view.body.classList.remove("typing");
        addError(view.bubble, "STREAM_INTERRUPTED", "回答中断了，请重试", true);
      }
    } catch (err) {
      if (err.name === "AbortError") {
        addError(view.bubble, "ABORTED", "已取消本次提问", true);
      } else {
        addError(view.bubble, "NETWORK_ERROR", "网络异常，请检查连接后重试", true);
      }
      view.body.classList.remove("typing");
    } finally {
      state.abort = null;
      state.busy = false;
      setBusyUI(false);
      hideStatus();
      scrollToBottom(false);
    }
  }

  // ---------------------------------------------------------------- 状态条
  function showStatus(text) {
    el.statusText.textContent = text;
    el.statusLine.hidden = false;
  }
  function hideStatus() {
    el.statusLine.hidden = true;
  }
  function setBusyUI(busy) {
    el.send.disabled = busy;
    el.input.disabled = busy;
    el.send.textContent = busy ? "生成中" : "发送";
  }

  // ---------------------------------------------------------------- 历史
  async function loadHistory() {
    if (!state.sessionId) return;
    try {
      const resp = await fetch(`${API}/chat/sessions/${state.sessionId}/messages`, {
        credentials: "same-origin",
      });
      if (resp.status === 403 || resp.status === 404) {
        // cookie 对不上（换浏览器/清过缓存），当作新会话
        localStorage.removeItem(SESSION_KEY);
        state.sessionId = uuid();
        localStorage.setItem(SESSION_KEY, state.sessionId);
        return;
      }
      if (!resp.ok) return;

      const data = await resp.json();
      const list = data.messages || [];
      if (list.length === 0) return;

      hideEmpty();
      for (const m of list) {
        if (m.role === "user") {
          addUserMessage(m.content);
        } else {
          const view = addBotMessage();
          view.body.classList.remove("typing");
          view.body.textContent = m.content;
          if (m.citations && m.citations.length) renderCitations(view.cites, m.citations);
          if (m.status === "aborted" || m.status === "error") {
            const tip = document.createElement("div");
            tip.className = "meta-line";
            tip.textContent = m.status === "aborted" ? "（回答被中断）" : "（回答异常结束）";
            view.bubble.appendChild(tip);
          }
        }
      }
      scrollToBottom(true);
    } catch {
      /* 历史加载失败不影响新提问 */
    }
  }

  // ---------------------------------------------------------------- 身份
  // 登录态由服务端 Cookie 维持。这里只做两件事：显示"你是谁"、提供退出入口。
  // 401 时直接跳登录页——不要在聊天页弹一个"请登录"的气泡，那很怪。
  async function loadMe() {
    try {
      const resp = await fetch(`${API}/auth/me`, { credentials: "same-origin" });
      if (resp.status === 401) {
        window.location.href = "/login";
        return;
      }
      const u = await resp.json();
      el.meName.textContent = u.name || u.emp_no;
      el.meSub.textContent = `${u.emp_no} · ${u.dept || "未分配部门"}`;
      if (u.role === "admin") el.meAdmin.hidden = false;
    } catch {
      el.meName.textContent = "身份获取失败";
    }
  }

  function logout() {
    /*
      退出必须 POST + CSRF，不能用 <a href="/logout">。
      原因：GET 登出可以被任意页面用 <img src> 触发，属于 CSRF 的低危但真实形态；
      同时浏览器预取也会误登出。所以这里动态构造表单提交。
    */
    const csrf = (document.cookie.match(/(?:^|;\s*)sdb_csrf=([^;]+)/) || [])[1] || "";
    const form = document.createElement("form");
    form.method = "post";
    form.action = "/logout";
    const input = document.createElement("input");
    input.type = "hidden";
    input.name = "_csrf";
    input.value = decodeURIComponent(csrf);
    form.appendChild(input);
    document.body.appendChild(form);
    form.submit();
  }

  if (el.btnLogout) el.btnLogout.addEventListener("click", logout);

  // ---------------------------------------------------------------- 健康检查
  async function checkHealth() {
    try {
      const resp = await fetch(`${API}/health`);
      const data = await resp.json();
      const rag = data.rag || {};
      if (rag.ready) {
        el.kbState.textContent = `${rag.chunks} 切片`;
        el.kbState.className = "ok";
      } else {
        el.kbState.textContent = "未就绪";
        el.kbState.className = "warn";
        toast("检索服务未就绪，请检查服务端日志");
      }
      if (rag.llm_ready) {
        el.llmState.textContent = rag.llm_model || "已配置";
        el.llmState.className = "ok";
      } else {
        el.llmState.textContent = "未配置";
        el.llmState.className = "warn";
        el.degraded.hidden = false;
      }
    } catch {
      el.kbState.textContent = "不可达";
      el.kbState.className = "warn";
    }
  }

  // ---------------------------------------------------------------- 事件绑定
  el.form.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = el.input.value.trim();
    if (!q) return;
    el.input.value = "";
    el.input.style.height = "auto";
    el.counter.textContent = "0 / 500";
    send(q);
  });

  el.input.addEventListener("keydown", (e) => {
    // Enter 发送、Shift+Enter 换行：中文输入法组合期间必须放行，否则选词就发出去了
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      el.form.requestSubmit();
    }
  });

  el.input.addEventListener("input", () => {
    el.counter.textContent = `${el.input.value.length} / 500`;
    el.input.style.height = "auto";
    el.input.style.height = `${Math.min(el.input.scrollHeight, 140)}px`;
  });

  el.newChat.addEventListener("click", async () => {
    if (state.busy) {
      toast("正在生成回答，请稍候");
      return;
    }
    const old = state.sessionId;
    if (old) {
      // 尽力清掉服务端会话；失败也不阻塞前端换新会话
      fetch(`${API}/chat/sessions/${old}`, { method: "DELETE", credentials: "same-origin" }).catch(() => {});
    }
    state.sessionId = uuid();
    localStorage.setItem(SESSION_KEY, state.sessionId);
    el.messages.textContent = "";
    el.messages.appendChild(buildEmptyState());
    el.degraded.hidden = true;
  });

  // 新建对话后要恢复空态，所以把空态做成可重建的节点
  function buildEmptyState() {
    const wrap = document.createElement("div");
    wrap.className = "empty";
    wrap.id = "empty-state";

    const h2 = document.createElement("h2");
    h2.textContent = "问点什么";

    const p = document.createElement("p");
    p.textContent = "下面的问题可以直接点：";

    const samples = document.createElement("div");
    samples.className = "samples";
    const list = [
      "每天最多可以预约几格？",
      "取消预约后别人能马上约同一格吗？",
      "设备坏了怎么下架，员工能自己改状态吗？",
      "预约了没去会怎么样？",
    ];
    for (const q of list) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "sample";
      btn.textContent = q;
      btn.addEventListener("click", () => send(q));
      samples.appendChild(btn);
    }

    wrap.append(h2, p, samples);
    return wrap;
  }

  // 示例问题（首屏那份 HTML 里的）也要能点
  document.querySelectorAll(".sample").forEach((btn) => {
    btn.addEventListener("click", () => send(btn.textContent.trim()));
  });

  // ---------------------------------------------------------------- 启动
  // 支持 ?session=<id> 直接打开指定会话。用途有两个：
  //   - 调试：换浏览器、清缓存后能接回原来的会话，不必重放一遍对话；
  //   - 演示：把链接发给别人，对方打开就能看到同一段对话（含待确认卡片）。
  // 安全性不靠这个参数：服务端会按 user_id 校验归属，拿别人的 id 只会拿到 403，
  // 前端在 loadHistory 里遇到 403 会当作新会话处理。
  const urlSession = new URLSearchParams(location.search).get("session");
  state.sessionId = urlSession || localStorage.getItem(SESSION_KEY) || uuid();
  localStorage.setItem(SESSION_KEY, state.sessionId);
  loadMe();
  checkHealth();
  loadHistory();
})();
