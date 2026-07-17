// agent-board — vanilla JS client.
// Lists posts, creates new ones, opens (→ spawn-or-attach + redirect),
// toggles force-active, deletes. All over /api/posts*.
(function () {
  const $posts = document.getElementById("posts");
  const $topic = document.getElementById("new-topic");
  const $model = document.getElementById("new-model");
  const $create = document.getElementById("new-create");
  const $sameTab = document.getElementById("same-tab");

  // "open in current page" preference persists across reloads (default: new tab)
  $sameTab.checked = localStorage.getItem("agentboard_same_tab") === "1";
  $sameTab.addEventListener("change", () =>
    localStorage.setItem("agentboard_same_tab", $sameTab.checked ? "1" : "0")
  );

  let MODELS = []; // registry, shared by the new-post form + per-post dropdowns

  // ── 탭 가드 (v1.14.0) ─────────────────────
  // board-proxy 게이트웨이에서는 모든 방이 이 origin 으로 프록시되고,
  // 방 탭과 대시보드 탭이 각각 SSE 로 연결 1개를 계속 점유한다. 브라우저의
  // HTTP/1.1 origin 당 동시 연결은 6개(프로필 전체 합산)뿐이라 6개가 차면
  // 승인 클릭·채팅 전송까지 모든 요청이 조용히 멈춘다(실사고: agent-cli
  // v7.2.0 confirm-starvation). 새 방을 열기 전에 BroadcastChannel 로
  // "연결을 잡고 있는 탭"을 세어(방 탭은 agent-cli 의 비콘이, 대시보드
  // 탭은 아래 응답기가 pong) 한도 앞에서 막는다. caddy(h2) 모드는 연결
  // 1개에 스트림을 멀티플렉스하므로 가드가 스스로 물러난다(/api/gateway).
  // 한계: board 를 거치지 않은 탭 진입(세션 복원·URL 직접·탭 복제)은
  // 게이트를 안 지나므로, 그 케이스는 agent-cli 쪽 무응답 경고가 받친다.
  const TAB_CHANNEL = "agentcli_tab_presence";
  // 보유 SSE 가 6개가 되는 순간 풀이 포화되므로 5개에서 차단(=열면 6),
  // 4개에서 경고(=열면 5, fetch 여유 1개).
  const MAX_HELD_TABS = 5;
  let gatewayMode = "board-proxy"; // /api/gateway 로 갱신 (모르면 보수적으로 가드)

  fetch("/api/gateway")
    .then((r) => r.json())
    .then((d) => {
      if (d && d.gateway) gatewayMode = d.gateway;
    })
    .catch(() => {});

  // 대시보드 탭도 /api/events SSE 로 연결 1개를 점유 — 같은 채널에 pong.
  if (typeof BroadcastChannel !== "undefined") {
    const presence = new BroadcastChannel(TAB_CHANNEL);
    presence.addEventListener("message", (e) => {
      const d = e.data || {};
      if (d.type === "ping")
        presence.postMessage({ type: "pong", nonce: d.nonce, path: "/" });
    });
  }

  // ping 을 쏘고 150ms 동안 pong 을 수집 — 살아 있는 탭만 답하므로
  // 크래시/닫힘 탭이 세어지는 일이 없다. 반환: {count, paths}.
  function countHeldTabs() {
    return new Promise((resolve) => {
      if (typeof BroadcastChannel === "undefined") {
        resolve({ count: 0, paths: [] });
        return;
      }
      const ch = new BroadcastChannel(TAB_CHANNEL);
      const nonce = String(Date.now()) + Math.random();
      const paths = [];
      ch.addEventListener("message", (e) => {
        const d = e.data || {};
        if (d.type === "pong" && d.nonce === nonce) paths.push(d.path || "");
      });
      ch.postMessage({ type: "ping", nonce });
      setTimeout(() => {
        ch.close();
        resolve({ count: paths.length, paths });
      }, 150);
    });
  }

  async function loadModels() {
    MODELS = await fetch("/api/models").then((r) => r.json());
    MODELS.forEach((m) => {
      const o = document.createElement("option");
      o.value = m.id;
      o.textContent = m.provider ? `${m.id} (${m.provider})` : m.id;
      $model.appendChild(o);
    });
  }

  // per-post model <select>: current model selected, disabled when the gate
  // (busy / someone watching) forbids a change — with a reason in the tooltip.
  function modelSelect(p) {
    const opts = ['<option value="">(기본)</option>'].concat(
      MODELS.map(
        (m) =>
          `<option value="${esc(m.id)}"${m.id === p.model_id ? " selected" : ""}>${esc(m.id)}</option>`
      )
    );
    let reason = "모델 변경";
    if (!p.model_changeable) {
      reason =
        p.status === "working"
          ? "응답 중 — 변경 불가"
          : p.viewers > 0
            ? `접속자 ${p.viewers}명 — 변경 불가`
            : "변경 불가";
    }
    return `<select class="model-sel" title="${reason}"${p.model_changeable ? "" : " disabled"}>${opts.join("")}</select>`;
  }

  const esc = (s) =>
    (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  // ISO → "MM-DD HH:MM" local (empty for missing)
  function fmtDate(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d)) return "";
    const p = (n) => String(n).padStart(2, "0");
    return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  async function load() {
    const posts = await fetch("/api/posts").then((r) => r.json());
    $posts.innerHTML = "";
    if (!posts.length) {
      $posts.innerHTML = '<div class="empty">아직 글이 없습니다. 새 글을 만들어 보세요.</div>';
      return;
    }
    posts.forEach((p) => $posts.appendChild(card(p)));
  }

  // status → {dot css class, label}. working = LLM 응답 중, running = 대기, idle = 꺼짐
  const STATUS = {
    working: { cls: "busy", label: "응답 중" },
    running: { cls: "on", label: "대기" },
    idle: { cls: "off", label: "꺼짐" },
  };

  function card(p) {
    const el = document.createElement("div");
    el.dataset.id = p.post_id; // for in-place live updates over SSE
    el.className = "post" + (p.awaiting_input ? " needs-input" : "");
    // awaiting an ask/confirm reply takes precedence over the busy/idle label
    const st = p.awaiting_input
      ? { cls: "await", label: "❗ 응답 필요" }
      : STATUS[p.status] || STATUS.idle;
    const up = p.status === "running" || p.status === "working";
    el.innerHTML =
      `<div class="post-main">` +
      `<div class="post-topic">${esc(p.topic)}</div>` +
      `<div class="post-last">${esc(p.last_query) || "<span class='muted'>— 아직 질문 없음</span>"}</div>` +
      `<div class="post-meta">생성 ${fmtDate(p.created_at)}` +
      (p.last_query_at ? ` · 마지막 ${fmtDate(p.last_query_at)}` : "") +
      `</div>` +
      `</div>` +
      `<div class="post-side">` +
      `<span class="st ${p.awaiting_input ? "await" : ""}"><span class="dot ${st.cls}"></span>${st.label}` +
      (up ? ` <span class="viewers" title="접속자 수">👁 ${p.viewers}</span>` : "") +
      `</span>` +
      modelSelect(p) +
      `<label class="fa" title="force-active: 접속자 없어도 계속 살려둠">` +
      `<input type="checkbox" class="fa-cb" ${p.force_active ? "checked" : ""}> 유지</label>` +
      // 🔄 restart only when the instance is up — a down room's "열기" already
      // spawns fresh, so restart is the ONLY way to force-replace a running
      // process (e.g. to pick up an agent-cli update).
      (up
        ? `<button class="restart btn-ghost" type="button" title="재실행 — 프로세스를 재시작(새로 설치한 agent-cli 반영). 세션은 이어집니다.">🔄</button>`
        : "") +
      `<button class="open btn-primary" type="button">열기</button>` +
      `<button class="del btn-danger" type="button" title="삭제(영구)">🗑</button>` +
      `</div>`;

    el.querySelector(".open").addEventListener("click", () => open(p.post_id));
    el.querySelector(".del").addEventListener("click", () => del(p));
    const restartBtn = el.querySelector(".restart");
    if (restartBtn)
      restartBtn.addEventListener("click", () =>
        restart(p.post_id, restartBtn, p.topic)
      );
    el.querySelector(".fa-cb").addEventListener("change", (e) =>
      forceActive(p.post_id, e.target.checked)
    );
    const sel = el.querySelector(".model-sel");
    if (sel)
      sel.addEventListener("change", (e) =>
        changeModel(p, e.target.value, e.target)
      );
    return el;
  }

  async function open(post_id) {
    const sameTab = $sameTab.checked;
    // Open the new tab SYNCHRONOUSLY (inside the click gesture) so the popup
    // blocker allows it; the await below would otherwise break the gesture.
    // Named target(post_id): 같은 글을 다시 열면 새 탭 대신 그 글의 기존
    // 창을 재사용 — 실수로 연결 잡는 탭이 하나 더 생기는 걸 막고, 의도적
    // 두 번째 창은 URL 복사로 여전히 가능(다중 뷰어는 설계 기능).
    const win = sameTab ? null : window.open("", "agentcli-" + post_id);
    if (gatewayMode !== "caddy") {
      const held = await countHeldTabs();
      // 이 글의 탭이 이미 있으면(named-window 재사용 or sameTab 전환)
      // 연결이 늘지 않으므로 게이트 면제.
      const reusing = held.paths.some((p) => p.startsWith(`/s/${post_id}/`));
      if (!reusing) {
        if (held.count >= MAX_HELD_TABS) {
          if (win) win.close();
          toast(
            `연결 한도 — 이 브라우저에 연결을 잡은 탭이 ${held.count}개입니다. ` +
              `HTTP/1.1 은 주소당 동시 연결 6개뿐이라 더 열면 모든 탭이 멈춥니다. ` +
              `안 쓰는 방 탭을 닫고 다시 여세요.`,
            true
          );
          return;
        }
        if (held.count === MAX_HELD_TABS - 1) {
          toast(
            `연결을 잡은 탭 ${held.count + 1}개 — 한도(6)에 근접했습니다. 안 쓰는 탭을 닫아주세요.`,
            true
          );
        }
      }
    }
    const r = await fetch(`/api/posts/${post_id}/open`, { method: "POST" });
    if (!r.ok) {
      if (win) win.close();
      alert("열기 실패: " + r.status);
      return;
    }
    const url = (await r.json()).url; // → /s/<post_id>/
    if (sameTab) {
      location.href = url;
    } else {
      win.location.href = url;
      win.focus();
    }
  }

  // Transient bottom-center notice — the board had no feedback channel, so a
  // one-off toast confirms actions (currently: restart) that otherwise leave no
  // visible trace. Reuses one #toast node; each call resets its 2.6s timer.
  let toastTimer = null;
  function toast(msg, isError) {
    let el = document.getElementById("toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "toast";
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.className = "show" + (isError ? " err" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      el.className = "";
    }, 2600);
  }

  async function restart(post_id, btn, topic) {
    // Force-restart (always allowed — no busy/viewer gate). The reused token
    // means anyone already in the room reconnects on their own; here we just
    // refresh the board so the status reflects the respawn.
    //
    // The button POST alone gave NO feedback — a fast respawn leaves the status
    // already "running", so the user couldn't tell the click even registered.
    // Spin+disable the button for the duration (the backend awaits stop+respawn
    // before returning, so this covers the real work), then a toast confirms it
    // actually restarted.
    if (btn) {
      btn.disabled = true;
      btn.classList.add("spinning");
    }
    try {
      const r = await fetch(`/api/posts/${post_id}/restart`, { method: "POST" });
      if (!r.ok) {
        toast("재실행 실패: " + r.status, true);
        return;
      }
      toast("🔄 재실행되었습니다" + (topic ? " — " + topic : ""));
      load(); // re-render: the row's status reflects the fresh process
    } catch (e) {
      toast("재실행 실패: 네트워크 오류", true);
    } finally {
      // load() may already have replaced this button node — harmless no-op then.
      if (btn) {
        btn.disabled = false;
        btn.classList.remove("spinning");
      }
    }
  }

  async function forceActive(post_id, enabled) {
    await fetch(`/api/posts/${post_id}/force_active`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: enabled }),
    });
  }

  async function changeModel(p, model_id, selectEl) {
    const r = await fetch(`/api/posts/${p.post_id}/model`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model_id: model_id || null }),
    });
    if (!r.ok) {
      // 409 = gate refused (state changed between render and click); revert + explain
      let why = "변경 실패";
      if (r.status === 409) {
        const d = (await r.json()).detail;
        why = d === "busy" ? "응답 중" : d === "viewers" ? "접속자 있음" : "변경 불가";
      }
      alert("모델 변경 실패: " + why);
      selectEl.value = p.model_id || "";
      return;
    }
    load(); // refresh — model + status (kill→DEAD, or force-active respawn)
  }

  async function del(p) {
    if (!confirm(`삭제할까요? (영구 — 워크스페이스도 삭제)\n${p.topic}`)) return;
    await fetch(`/api/posts/${p.post_id}`, { method: "DELETE" });
    load();
  }

  async function create() {
    const topic = $topic.value.trim();
    if (!topic) {
      $topic.focus();
      return;
    }
    await fetch("/api/posts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        topic: topic,
        model_id: $model.value || null,
      }),
    });
    $topic.value = "";
    load();
  }

  $create.addEventListener("click", create);
  $topic.addEventListener("keydown", (e) => {
    if (e.key === "Enter") create();
  });

  // ── Live push over SSE (replaces the old 5s /api/posts poll) ──────────
  // The board scans each post's on-disk signature and pushes changed rows.
  // A ``ping`` every 15s feeds a watchdog: if nothing arrives for 30s the
  // connection is treated as half-open (sleep/wake, flaky net) and force-
  // reconnected. On every (re)connect we full-reload, so a gap loses nothing.
  let evtSource = null;
  let watchdog = null;

  function upsertCard(p) {
    const existing = $posts.querySelector(`[data-id="${p.post_id}"]`);
    const fresh = card(p);
    if (existing) {
      existing.replaceWith(fresh);
    } else {
      const empty = $posts.querySelector(".empty");
      if (empty) empty.remove();
      $posts.appendChild(fresh);
    }
  }

  function removeCard(postId) {
    const el = $posts.querySelector(`[data-id="${postId}"]`);
    if (el) el.remove();
    if (!$posts.querySelector(".post")) {
      $posts.innerHTML =
        '<div class="empty">아직 글이 없습니다. 새 글을 만들어 보세요.</div>';
    }
  }

  function petWatchdog() {
    if (watchdog) clearTimeout(watchdog);
    // no message (not even a ping) for 30s → connection is dead-but-silent
    watchdog = setTimeout(() => {
      if (evtSource) evtSource.close();
      connectEvents(); // reconnect → onopen reloads the full list
    }, 30000);
  }

  function connectEvents() {
    evtSource = new EventSource("/api/events");
    evtSource.onopen = () => {
      petWatchdog();
      load(); // catch up on anything missed before/while (re)connecting
    };
    evtSource.onmessage = (e) => {
      petWatchdog();
      let msg;
      try {
        msg = JSON.parse(e.data);
      } catch {
        return;
      }
      if (msg.type === "post_update") upsertCard(msg.post);
      else if (msg.type === "post_removed") removeCard(msg.post_id);
      // type === "ping" → watchdog already fed above
    };
    // onerror: EventSource auto-reconnects; the watchdog covers half-open.
  }

  loadModels();
  load();
  connectEvents();
})();

// ── 테마 피커 (🎨) — agent-cli 와 동일 5테마·localStorage 'agentcli_theme'
// 공유 (board 프록시로 여는 방들과 같은 origin 이라 테마가 함께 움직인다).
(function () {
  "use strict";
  var root = document.documentElement;
  var THEMES = [
    { id: "amber", name: "Amber", bg: "#18140f", accent: "#e0a458" },
    { id: "slate", name: "Slate", bg: "#15171c", accent: "#7e8db0" },
    { id: "midnight", name: "Midnight", bg: "#111725", accent: "#4d8eff" },
    { id: "terminal", name: "Terminal", bg: "#101413", accent: "#2dd4bf" },
    { id: "light", name: "Light", bg: "#ffffff", accent: "#6366f1" },
  ];
  var $btn = document.getElementById("theme-btn");
  var $menu = document.getElementById("theme-menu");
  if (!$btn || !$menu) return;
  function current() {
    var t = root.getAttribute("data-theme");
    return THEMES.some(function (x) { return x.id === t; }) ? t : "amber";
  }
  function render() {
    var cur = current();
    $menu.innerHTML = "";
    THEMES.forEach(function (t) {
      var item = document.createElement("button");
      item.type = "button";
      item.className = "theme-item" + (t.id === cur ? " active" : "");
      item.setAttribute("role", "menuitem");
      var sw = document.createElement("span");
      sw.className = "theme-swatch";
      sw.style.background = "linear-gradient(135deg, " + t.bg + " 55%, " + t.accent + " 55%)";
      item.appendChild(sw);
      item.appendChild(document.createTextNode(t.name));
      if (t.id === cur) {
        var chk = document.createElement("span");
        chk.className = "theme-check";
        chk.textContent = "✓";
        item.appendChild(chk);
      }
      item.addEventListener("click", function () {
        root.setAttribute("data-theme", t.id);
        try { localStorage.setItem("agentcli_theme", t.id); } catch (e) { /* private mode */ }
        close();
      });
      $menu.appendChild(item);
    });
  }
  function open() { render(); $menu.hidden = false; $btn.setAttribute("aria-expanded", "true"); }
  function close() { $menu.hidden = true; $btn.setAttribute("aria-expanded", "false"); }
  $btn.addEventListener("click", function (e) {
    e.stopPropagation();
    if ($menu.hidden) open(); else close();
  });
  document.addEventListener("click", function (e) {
    if (!$menu.hidden && !$menu.contains(e.target)) close();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !$menu.hidden) close();
  });
})();
