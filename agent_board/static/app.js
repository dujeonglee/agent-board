// agent-board — vanilla JS client.
// Lists posts, creates new ones, opens (→ spawn-or-attach + redirect),
// toggles force-active, deletes. All over /api/posts*.
(function () {
  const $posts = document.getElementById("posts");
  const $topic = document.getElementById("new-topic");
  const $model = document.getElementById("new-model");
  const $create = document.getElementById("new-create");
  const $newAdv = document.getElementById("new-adv");
  const $newKnobs = document.getElementById("new-knobs");
  const $sameTab = document.getElementById("same-tab");

  // "open in current page" preference persists across reloads (default: new tab)
  $sameTab.checked = localStorage.getItem("agentboard_same_tab") === "1";
  $sameTab.addEventListener("change", () =>
    localStorage.setItem("agentboard_same_tab", $sameTab.checked ? "1" : "0")
  );

  let MODELS = []; // registry (전부), shared by the new-post form + per-post dropdowns
  // 방언(dialect) 바인딩이 있는 모델만 — 방 쪽 선택지는 이것뿐이다 (v1.33.0,
  // agent-cli v10.3.0 짝: 바인딩 없는 모델은 agent-cli 가 부트에서 거부한다).
  // 어드민(⚙)은 설정하는 곳이라 전부 보여 준다.
  let BOUND = [];
  const adminLink = (mid) =>
    `<a href="/admin#model=${encodeURIComponent(mid)}" title="Set this model's dialect in admin">⚙ Set up</a>`;
  // 서버 게이트의 사유(detail.reason) → 사람 문장. 400(생성·변경)/409(열기·재실행) 공용.
  function modelProblemText(detail) {
    if (!detail || typeof detail !== "object") return null;
    if (detail.reason === "model_required") return "This room has no model — pick one";
    if (detail.reason === "dialect_unbound")
      return `Model '${detail.model}' has no dialect binding — set it in ⚙ admin`;
    if (detail.reason === "bad_knob") return `Bad knob value: ${detail.error}`;
    return null;
  }

  // ── 노브 (v1.36.0, agent-cli ≥ 10.45.0 `agent-cli knobs --json`) ─────────
  // 보드는 노브 이름·플래그를 모른다: 표(SPEC)가 폼의 컨트롤·카드의 라벨·단위를
  // 준다. 입력은 **새 글/복제 폼 한 번**뿐 — 만든 뒤에는 방 안의 칩에서 바꾸고,
  // 보드는 방이 보고한 값(status.json knobs)을 거울처럼 보여 주기만 한다.
  let SPEC = { full: false, knobs: [] };
  const knobLabel = (k) => ((k.ui && k.ui.icon) ? k.ui.icon + " " : "") + k.label;
  const knobChoiceLabel = (k, v) =>
    (k.ui && k.ui.choice_labels && k.ui.choice_labels[v]) || v;
  const formKnobs = () => SPEC.knobs.filter((k) => k.name !== "model");

  async function loadSpec() {
    try {
      const d = await fetch("/api/knobs/spec").then((r) => (r.ok ? r.json() : null));
      if (d && Array.isArray(d.knobs)) SPEC = d;
    } catch (_e) {
      /* 구버전 보드/agent-cli — 폼 없음 */
    }
  }

  // 폼 한 벌 — 새 글·복제가 같은 함수로 그린다. 값 비움 = agent-cli 기본값.
  function knobForm(container, values) {
    container.innerHTML = "";
    values = values || {};
    formKnobs().forEach((k) => {
      const row = document.createElement("label");
      row.className = "kf-row";
      row.title = k.help || "";
      const name = document.createElement("span");
      name.className = "kf-name";
      name.textContent = knobLabel(k);
      row.appendChild(name);
      let ctl;
      if (k.kind === "choice" || k.kind === "bool") {
        ctl = document.createElement("select");
        const opts = k.kind === "bool" ? ["on", "off"] : k.choices || [];
        const def = document.createElement("option");
        def.value = "";
        def.textContent = k.default === null || k.default === undefined
          ? "(default)"
          : `(default — ${k.kind === "bool" ? (k.default ? "on" : "off") : knobChoiceLabel(k, k.default)})`;
        ctl.appendChild(def);
        opts.forEach((v) => {
          const o = document.createElement("option");
          o.value = v;
          o.textContent = k.kind === "bool" ? (v === "on" ? "on" : "off") : knobChoiceLabel(k, v);
          ctl.appendChild(o);
        });
        const cur = values[k.name];
        ctl.value = cur === undefined || cur === null ? "" : k.kind === "bool" ? (cur ? "on" : "off") : String(cur);
      } else {
        ctl = document.createElement("input");
        ctl.type = "text";
        ctl.placeholder = k.default === null || k.default === undefined ? "(default)" : String(k.default);
        const cur = values[k.name];
        ctl.value = cur === undefined || cur === null ? "" : String(cur);
      }
      ctl.dataset.knob = k.name;
      ctl.dataset.kind = k.kind;
      row.appendChild(ctl);
      if (k.ui && k.ui.unit) {
        const u = document.createElement("span");
        u.className = "kf-unit";
        u.textContent = k.ui.unit;
        row.appendChild(u);
      }
      container.appendChild(row);
    });
  }

  function readKnobForm(container) {
    const out = {};
    container.querySelectorAll("[data-knob]").forEach((ctl) => {
      const v = ctl.value.trim();
      if (!v) return;
      out[ctl.dataset.knob] = ctl.dataset.kind === "bool" ? v === "on" : v;
    });
    return out;
  }

  function wireAdvToggle(btn, panel) {
    btn.addEventListener("click", () => {
      panel.hidden = !panel.hidden;
      btn.setAttribute("aria-expanded", String(!panel.hidden));
    });
  }

  // 카드: 🤖 모델 칩 + 기본과 다른 노브 칩. 값은 방이 보고한 것(저장값과 같다).
  function knobChips(p) {
    const knobs = p.knobs || {};
    const parts = [];
    SPEC.knobs.forEach((k) => {
      if (k.name === "model") return;
      const v = knobs[k.name];
      if (v === undefined || v === null || v === k.default) return;
      const shown = k.kind === "bool" ? (v ? "on" : "off") : knobChoiceLabel(k, String(v));
      const icon = (k.ui && k.ui.icon) || "⚙";
      parts.push(
        `<span class="knob-chip" title="${esc(k.label)}: ${esc(String(shown))}">${icon} ${esc(String(shown))}</span>`
      );
    });
    return parts.join("");
  }

  const openPanels = new Set(); // SSE 재렌더에도 펼쳐 둔 패널을 기억

  // 읽기 전용 패널 — 표의 모든 노브와 지금 값("(기본)" = 저장 안 됨).
  function knobPanel(p) {
    if (!SPEC.full) return "";
    const knobs = p.knobs || {};
    const up = p.status === "running" || p.status === "working";
    const rows = SPEC.knobs.map((k) => {
      const v = knobs[k.name];
      const shown =
        v === undefined || v === null
          ? "(default)"
          : k.kind === "bool"
            ? (v ? "on" : "off")
            : knobChoiceLabel(k, String(v));
      return `<div class="kp-row"><span class="kp-name" title="${esc(k.help || "")}">${esc(knobLabel(k))}</span><span class="kp-val">${esc(String(shown))}</span><span class="kp-unit">${esc((k.ui && k.ui.unit) || "")}</span></div>`;
    });
    const note = up
      ? "Values reported by the room — change them from the chips inside."
      : "Values for the next open — open the room and change them from the chips.";
    return `<div class="knob-panel"${openPanels.has(p.post_id) ? "" : " hidden"}><div class="kp-head"><span>⚙ Knobs</span><span class="kp-note">${note}</span></div>${rows.join("")}</div>`;
  }

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

  // 헤더 버전 표시 — board 자신 + board 가 spawn 하는 agent-cli 바이너리.
  // cli 부재/미인식이면 board 만. 서버 값은 x.y.z 정규식으로 뽑은 것이라
  // textContent 로만 넣는다(마크업 주입 없음).
  fetch("/api/version")
    .then((r) => (r.ok ? r.json() : null))
    .then((d) => {
      const el = document.getElementById("version");
      if (!d || !el) return;
      el.textContent = "v" + d.board + (d.cli ? "  ·  cli " + d.cli : "");
    })
    .catch(() => {});

  // ── 연결 카운트 (v1.16.0 — 샘플링 복귀) ──
  // v1.15 의 Web Locks 는 secure context 전용이라 LAN http(주 운용)에서
  // 락 API 자체가 노출되지 않아 무동작이었다. 열기 게이트는 사람 속도의
  // 버튼 클릭이라 ping/pong 샘플링으로 충분 — 대량 동시 오픈은 board
  // 경유 운용 전제(agent-cli v7.7.0)에서 발생하지 않는다.
  //
  // 샘플링 창 = 100ms (v1.22.2, 이전 300ms). 게이트는 window.open 앞에서
  // 도는 pre-open 지연이라 이 창이 재열기 체감에 더해진다 → 작게 유지.
  // BroadcastChannel pong 은 같은 브라우저 내 전달이라 유휴 탭이 ~5ms 에
  // 답하므로 100ms 면 20배 여유로 다 잡힌다. 렌더로 JS 가 블록된 탭은
  // 300ms 로도 창 안에 못 답하니(집계 누락) 잡히는 탭 집합은 100·300ms
  // 가 실질 동일 = 하드 블록 보장 불변, 새 레이스 없음. 클릭 시점 샘플링을
  // 유지하므로 스테일 캐시 위험도 없다.
  // ※경위: v1.22.2 는 이 창을 재열기 굼뜸의 주범으로 보고 줄였으나,
  //   실제 주범은 빈 탭 window.open("")→navigate 였고 v1.22.3 의 직접
  //   open 으로 해결됨. 이 축소는 pre-open 지연을 더는 부차적 이득으로 유지.
  const HELD_SAMPLE_MS = 100;
  let dashHeld = false; // 이 대시보드 탭의 SSE 가 연결됐나

  // 대시보드 탭 presence 비콘 — 카운트·재사용 path 판정에 응답.
  if (typeof BroadcastChannel !== "undefined") {
    const presence = new BroadcastChannel(TAB_CHANNEL);
    presence.addEventListener("message", (e) => {
      const d = e.data || {};
      if (d.type === "ping")
        presence.postMessage({
          type: "pong",
          nonce: d.nonce,
          path: "/",
          held: dashHeld,
        });
    });
  }

  // ping 을 쏘고 HELD_SAMPLE_MS 동안 pong 을 수집 — 살아 있는 탭만 답하므로
  // 크래시/닫힘 탭이 세어지지 않는다. held:false pong(연결 미보유 탭)
  // 은 카운트에서 제외, held 필드 없는 pong(구버전 agent-cli 탭)은
  // 보유로 집계. paths 는 재사용 판정("이 글의 탭이 이미 있나")용.
  function countHeldTabs() {
    return new Promise((resolve) => {
      if (typeof BroadcastChannel === "undefined") {
        resolve({ count: 0, paths: [] });
        return;
      }
      const ch = new BroadcastChannel(TAB_CHANNEL);
      const nonce = String(Date.now()) + Math.random();
      const paths = [];
      let count = 0;
      ch.addEventListener("message", (e) => {
        const d = e.data || {};
        if (d.type === "pong" && d.nonce === nonce) {
          paths.push(d.path || "");
          if (d.held !== false) count++;
        }
      });
      ch.postMessage({ type: "ping", nonce });
      setTimeout(() => {
        ch.close();
        resolve({ count, paths });
      }, HELD_SAMPLE_MS);
    });
  }

  // 새 글·복제 폼의 모델 <select> 채우기 — 바인딩 있는 모델만, "(기본)" 없음.
  // 하나뿐이면 미리 고르고, 없으면 안내 한 줄 + 비활성(만들기 불가).
  function fillModelPicker(sel, preferred) {
    sel.innerHTML = "";
    if (!BOUND.length) {
      const o = document.createElement("option");
      o.value = "";
      o.textContent = "⚠ No model has a dialect — set one in ⚙ admin";
      sel.appendChild(o);
      sel.disabled = true;
      return;
    }
    sel.disabled = false;
    if (BOUND.length > 1) {
      const o = document.createElement("option");
      o.value = "";
      o.textContent = "— pick a model —";
      sel.appendChild(o);
    }
    BOUND.forEach((m) => {
      const o = document.createElement("option");
      o.value = m.id;
      o.textContent = m.provider ? `${m.id} (${m.provider})` : m.id;
      sel.appendChild(o);
    });
    sel.value = preferred && BOUND.some((m) => m.id === preferred) ? preferred : BOUND.length === 1 ? BOUND[0].id : "";
  }

  async function loadModels() {
    const [models] = await Promise.all([
      fetch("/api/models").then((r) => r.json()),
      loadSpec(),
    ]);
    MODELS = models;
    BOUND = MODELS.filter((m) => m.dialect);
    fillModelPicker($model, $model.value);
    $create.disabled = !BOUND.length;
    $newAdv.hidden = !SPEC.full; // 구버전 agent-cli 는 표가 없다 → 폼 숨김
    knobForm($newKnobs, readKnobForm($newKnobs));
  }

  const esc = (s) =>
    (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  // 카드 경고 줄 — 모델 없음 / 방언 미설정 (model_bound=false 일 때만).
  function modelWarning(p) {
    if (p.model_bound) return "";
    if (!p.model)
      return `<div class="model-warn">⚠ This room has no model — 📋 clone it, pick a model and continue in the new room.</div>`;
    return (
      `<div class="model-warn">⚠ Model '${esc(p.model)}' has no dialect binding, so the room cannot open — ` +
      adminLink(p.model) +
      " or 📋 clone it and continue with another model" +
      `</div>`
    );
  }

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
      $posts.innerHTML = '<div class="empty">No posts yet. Create one.</div>';
      return;
    }
    posts.forEach((p) => $posts.appendChild(card(p)));
  }

  // status → {dot css class, label}. working = LLM 응답 중, running = 대기, idle = 꺼짐
  const STATUS = {
    working: { cls: "busy", label: "Responding" },
    running: { cls: "on", label: "Idle" },
    idle: { cls: "off", label: "Off" },
  };
  // main 유휴 + 상주 에이전트 작업 중 (v1.17.0) — 같은 원형 dot, 색만
  // 구분(보라). "응답 중"과 합치지 않는 이유: main 은 비어 있어 지금
  // 말 걸어도 되는 상태라는 정보가 사라짐.
  const AGENTS_BUSY = { cls: "agents-busy", label: "Agents working" };
  // main 유휴 + 세션 감사 런 진행 중 (v1.35.0, agent-cli ≥ 10.42.0 `auditing`) —
  // 하니스가 방금 끝난 런의 결과를 확인하는 중. 감사자는 팀원이 아니라 🤖 칩에
  // 없고, 이 상태 한 줄이 그 자리다. 우선순위: 응답 필요 > 응답 중 > 결과 확인 중
  // > 에이전트 작업 중 > 대기.
  const AUDITING = { cls: "auditing", label: "Checking result" };

  function agentsChip(p) {
    const a = p.agents;
    if (!a || !a.alive) return "";
    const detail = (a.list || [])
      .map((x) => {
        const who = x.name ? `${x.profile} · ${x.name}` : x.profile || x.key;
        return `${who}: ${x.state}`;
      })
      .join("\n");
    return `<span class="agents-chip" title="${esc(detail)}">🤖 ${a.working}/${a.alive}</span>`;
  }

  function card(p) {
    const el = document.createElement("div");
    el.dataset.id = p.post_id; // for in-place live updates over SSE
    el.className = "post" + (p.awaiting_input ? " needs-input" : "");
    // awaiting an ask/confirm reply takes precedence over the busy/idle label
    const agentsWorking = p.agents && p.agents.working > 0;
    const st = p.awaiting_input
      ? { cls: "await", label: "❗ Needs your reply" }
      : p.status === "running" && p.auditing
        ? AUDITING
        : p.status === "running" && agentsWorking
          ? AGENTS_BUSY
          : STATUS[p.status] || STATUS.idle;
    const up = p.status === "running" || p.status === "working";
    el.innerHTML =
      `<div class="post-main">` +
      `<div class="post-topic">${esc(p.topic)}</div>` +
      `<div class="post-last">${esc(p.last_query) || "<span class='muted'>— no request yet</span>"}</div>` +
      // 워크스페이스 id — 디스크의 `<workspaces_root>/<id>` 와 이 카드를 잇는
      // 유일한 표식이라 반드시 보여야 한다(짧은 랜덤 id 는 그 자체로는 어떤
      // 글인지 말해주지 않는다). 클릭하면 복사 → 터미널에 바로 붙여넣기.
      `<div class="post-meta">` +
      `<button type="button" class="ws-id" data-id="${esc(p.post_id)}" title="workspace id — click to copy">${esc(p.post_id)}</button>` +
      ` · created ${fmtDate(p.created_at)}` +
      (p.last_query_at ? ` · last ${fmtDate(p.last_query_at)}` : "") +
      `</div>` +
      `</div>` +
      `<div class="post-side">` +
      `<span class="st ${p.awaiting_input ? "await" : ""}"><span class="dot ${st.cls}"></span>${st.label}` +
      (up ? ` <span class="viewers" title="viewers">👁 ${p.viewers}</span>` : "") +
      (up ? agentsChip(p) : "") +
      // 📐 문법 제약이 실리는 세션 — 인스턴스의 칩과 같은 값(status.json)
      (up && p.grammar ? ` <span class="grammar-chip" title="Decoding grammar on — the server enforces the tool-call shape">📐</span>` : "") +
      `</span>` +
      // 🤖 모델 칩 (v1.36.0): 드롭다운이 아니다 — 방이 보고한 현재 모델. 바꾸는
      // 곳은 방 안의 🤖 칩이고 보드는 그 값을 기록해 다음 재시작에 넘긴다.
      `<span class="model-chip" title="Model — change it from the 🤖 chip inside the room">🤖 ${esc(p.model) || "⚠ none"}</span>` +
      knobChips(p) +
      (SPEC.full
        ? `<button class="knobs btn-ghost" type="button" aria-expanded="${openPanels.has(p.post_id)}" title="View knobs (read-only)">⚙</button>`
        : "") +
      `<label class="fa" title="force-active: keep the room alive with no viewers">` +
      `<input type="checkbox" class="fa-cb" ${p.force_active ? "checked" : ""}> Keep</label>` +
      // 🔄 restart only when the instance is up — a down room's "열기" already
      // spawns fresh, so restart is the ONLY way to force-replace a running
      // process (e.g. to pick up an agent-cli update).
      (up
        ? `<button class="restart btn-ghost" type="button" title="Restart — restarts the process (picks up a newly installed agent-cli). The session continues.">🔄</button>`
        : "") +
      // ⏰ 예약 배지 — 예약은 그 방(agent-cli 세션)의 것이다(v1.34.0). 보드는
      // 개수만 보여 주고, 목록·추가·답하기는 방 화면의 ⏰ 서랍에서 한다.
      (p.schedules && p.schedules.count
        ? `<button class="sched btn-ghost" type="button" title="${p.schedules.count} schedule(s) — open the room and manage them under ⏰">⏰<span class="scnt">${p.schedules.count}</span></button>`
        : "") +
      `<button class="clone btn-ghost" type="button" title="Copy this post's files and conversation into a new post">📋 Clone</button>` +
      // 모델이 없거나 방언 미설정이면 열 수 없다 — 서버(409)와 같은 판정을
      // 버튼에 먼저 반영하고, 아래 경고 줄이 사유와 ⚙ 링크를 보여 준다.
      `<button class="open btn-primary" type="button"${p.model_bound ? "" : ' disabled title="Cannot open until a model and dialect are set"'}>Open</button>` +
      `<button class="del btn-danger" type="button" title="Delete (permanent)">🗑</button>` +
      `</div>` +
      modelWarning(p) +
      knobPanel(p) +
      missedBanner(p);

    wireSchedule(el, p);
    const wsId = el.querySelector(".ws-id");
    if (wsId)
      wsId.addEventListener("click", () => {
        // navigator.clipboard 는 secure context 전용 → LAN http 에선 미정의라
        // execCommand 폴백이 필수 (agent-cli 쪽 copyToClipboard 와 같은 이유).
        const done = () => {
          const orig = wsId.dataset.id;
          wsId.textContent = "✓ copied";
          setTimeout(() => (wsId.textContent = orig), 1200);
        };
        // execCommand 폴백은 clipboard API 가 *없을* 때뿐 아니라 **거부될 때**도
        // 필요하다: 권한이 없으면 writeText 는 존재하지만 reject 하므로,
        // 거부를 무시하면 클릭이 조용히 아무 일도 안 한 것처럼 보인다.
        const fallback = () => {
          const ta = document.createElement("textarea");
          ta.value = p.post_id;
          ta.style.position = "fixed";
          ta.style.opacity = "0";
          document.body.appendChild(ta);
          ta.select();
          try {
            document.execCommand("copy");
            done();
          } catch (e) {
            /* no-op */
          }
          document.body.removeChild(ta);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(p.post_id).then(done, fallback);
        } else {
          fallback();
        }
      });
    el.querySelector(".open").addEventListener("click", () => open(p.post_id));
    el.querySelector(".clone").addEventListener("click", () =>
      openCloneDialog(p)
    );
    el.querySelector(".del").addEventListener("click", () => del(p));
    const restartBtn = el.querySelector(".restart");
    if (restartBtn)
      restartBtn.addEventListener("click", () =>
        restart(p.post_id, restartBtn, p.topic)
      );
    el.querySelector(".fa-cb").addEventListener("change", (e) =>
      forceActive(p.post_id, e.target.checked)
    );
    const knobsBtn = el.querySelector(".knobs");
    if (knobsBtn)
      knobsBtn.addEventListener("click", () => {
        const panel = el.querySelector(".knob-panel");
        if (!panel) return;
        panel.hidden = !panel.hidden;
        if (panel.hidden) openPanels.delete(p.post_id);
        else openPanels.add(p.post_id);
        knobsBtn.setAttribute("aria-expanded", String(!panel.hidden));
      });
    return el;
  }

  // ── ⏰ 예약 (v1.34.0) ─────────────────────────────────────
  // 예약은 agent-cli 세션의 것이다 — 그 방의 프로세스가 발화하고, 목록·추가·
  // "지금 실행/건너뛰기" 는 방 화면의 ⏰ 서랍에서 한다. 보드는 세션 폴더의
  // 파일을 읽어 개수 배지와 놓친 예약 안내만 그린다.
  function missedBanner(p) {
    const missed = (p.schedules && p.schedules.missed) || [];
    if (!missed.length) return "";
    return missed
      .map(
        (m) =>
          `<div class="sched-missed">` +
          `<span class="lbl">⏰ Missed schedule: ${esc(m.label)}</span>` +
          `<span class="why">due ${fmtDate(m.missed_at)} — did not run</span>` +
          `<span class="acts">` +
          `<button class="m-open btn-primary" type="button"${p.model_bound ? "" : " disabled"}>Answer in the room</button>` +
          `</span></div>`
      )
      .join("");
  }

  function wireSchedule(el, p) {
    const btn = el.querySelector(".sched");
    if (btn) btn.addEventListener("click", () => open(p.post_id));
    el.querySelectorAll(".sched-missed .m-open").forEach((b) =>
      b.addEventListener("click", () => open(p.post_id))
    );
  }

  async function open(post_id) {
    const sameTab = $sameTab.checked;
    // 이 글의 룸을 **실제로 띄우고 있는 탭**이 있는지 — presence pong(살아있는
    // agent-cli 탭만 자기 path 로 답한다)이 유일한 진실원. 두 곳에 쓴다:
    //   ① 연결-한도 게이트 면제(재사용이면 연결이 안 늘어난다)
    //   ② 탭 타깃 결정 (아래)
    let reusing = false;
    // 게이트·POST 를 먼저 끝내고, 완성된 방 URL 로 새 탭을 **바로** 연다.
    // ★예전엔 클릭 제스처 안에서 빈 탭(window.open(""))을 먼저 열고 나중에
    // win.location 으로 navigate 했는데, 그 about:blank→실URL 전환이
    // 재열기를 ~1초 굼뜨게 하는 주범이었다 — 직접 URL 붙여넣기·현재 탭
    // 열기(둘 다 실 URL 로 바로 이동)가 빠른 것과 대비되어 실측 확정.
    // window.open 을 await(게이트~100ms + fetch~15ms) 뒤에 호출해도
    // Chrome 의 transient user activation(클릭 후 ~5초)이 살아 있어 팝업
    // 차단 없이 열린다(실측 확인) — 차단 시엔 현재 탭 이동으로 폴백.
    if (!sameTab) {
      // ★caddy 모드에서도 샘플링한다: 연결 한도는 없어도 **탭 타깃 판정**에
      // presence 가 필요하기 때문(아래). 비용은 HELD_SAMPLE_MS(100ms).
      const held = await countHeldTabs();
      reusing = held.paths.some((p) => p.startsWith(`/s/${post_id}/`));
      if (gatewayMode !== "caddy" && !reusing) {
        if (held.count >= MAX_HELD_TABS) {
          toast(
            `Connection limit — ${held.count} tabs in this browser hold a connection. ` +
              `HTTP/1.1 allows only 6 concurrent connections per host; one more and every tab stalls. ` +
              `Close unused room tabs and try again.`,
            true
          );
          return;
        }
        if (held.count === MAX_HELD_TABS - 1) {
          toast(
            `${held.count + 1} tabs hold a connection — close to the limit (6). Close unused tabs.`,
            true
          );
        }
      }
    }
    const r = await fetch(`/api/posts/${post_id}/open`, { method: "POST" });
    if (!r.ok) {
      // 409 = 모델/방언 게이트, 502 = 인스턴스 부트 실패(사유 = instance.log 꼬리)
      const d = (await r.json().catch(() => ({}))).detail;
      toast("Open failed: " + (modelProblemText(d) || (typeof d === "string" ? d : r.status)), true);
      load();
      return;
    }
    const url = (await r.json()).url; // → /s/<post_id>/
    if (sameTab) {
      location.href = url;
    } else {
      // 탭 타깃은 **presence 로 판정**한다 (v1.29.0):
      //   · 이 글의 룸을 띄운 탭이 살아있다 → named target 으로 그 탭 재사용
      //   · 없다 → "_blank" 로 **새 탭**. 종전엔 항상 named target 이었는데,
      //     ``window.name`` 은 사용자가 그 탭을 딴 주소로 옮겨도 남아 있어서
      //     (같은 origin 이면 유지) **사용자가 다른 용도로 쓰던 탭을 빼앗아**
      //     갔다. 브라우저는 "이름표만" 볼 뿐 그 안에 룸이 떠 있는지 모른다.
      //     반면 presence pong 은 살아있는 룸 탭만 답하므로 정확하다.
      // 새 탭에도 이름을 붙여 다음 재열기의 재사용 대상이 되게 한다(보드와
      // 룸은 같은 origin 이라 win.name 접근 가능). 팝업 차단(null)이면 현재
      // 탭 이동으로 폴백 — 종전과 동일.
      const winName = "agentcli-" + post_id;
      const win = window.open(url, reusing ? winName : "_blank");
      if (win) {
        try {
          win.name = winName;
        } catch (e) {
          /* 크로스 오리진 배치 등 — 이름 없이도 열기는 성공 */
        }
        win.focus();
      } else location.href = url;
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
        const d = (await r.json().catch(() => ({}))).detail;
        toast("Restart failed: " + (modelProblemText(d) || (typeof d === "string" ? d : r.status)), true);
        return;
      }
      toast("🔄 Restarted" + (topic ? " — " + topic : ""));
      load(); // re-render: the row's status reflects the fresh process
    } catch (e) {
      toast("Restart failed: network error", true);
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

  async function del(p) {
    if (!confirm(`Delete? (permanent — the workspace is deleted too)\n${p.topic}`)) return;
    await fetch(`/api/posts/${p.post_id}`, { method: "DELETE" });
    load();
  }

  // 같은 제목의 글이 **둘** 생기던 버그 (사용자 제보). 두 경로가 겹쳤다:
  //   ① IME — 한글 조합을 Enter 로 확정하면 `keydown` 이 한 번
  //      (`isComposing=true`), 실제 제출로 또 한 번. 둘 다 같은 제목을 읽는다.
  //   ② 가드 부재 — 입력을 `await` **뒤에** 비워서, 그 사이 두 번째 호출이
  //      같은 값을 그대로 통과시킨다. 더블클릭·Enter+클릭도 같다.
  // 막는 것은 **둘**: 조합 중 Enter 무시(아래 keydown) + 진행 중 가드.
  //
  // 한때 "요청 전에 입력 비우기"도 같이 뒀는데 뺐다 — 증상은 같이 막지만
  // 두 방어가 **서로를 가려** 어느 쪽도 사보타주에 안 걸렸다(둘 중 하나를
  // 없애도 나머지가 통과시킨다). 기계가 하나면 그게 무엇을 지키는지도
  // 테스트가 말할 수 있다. 입력은 **성공한 뒤에만** 비운다 — 실패하면
  // 사용자가 친 제목이 남아 다시 칠 필요가 없다.
  let creating = false;
  async function create() {
    if (creating) return;
    const topic = $topic.value.trim();
    if (!topic) {
      $topic.focus();
      return;
    }
    if (!$model.value) {
      toast(BOUND.length ? "Pick a model" : "No model has a dialect — set one in ⚙ admin", true);
      $model.focus();
      return;
    }
    creating = true;
    $create.disabled = true;
    try {
      const r = await fetch("/api/posts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          topic: topic,
          knobs: Object.assign({ model: $model.value }, readKnobForm($newKnobs)),
        }),
      });
      if (!r.ok) {
        const d = (await r.json().catch(() => ({}))).detail;
        toast("Create failed: " + (modelProblemText(d) || r.status), true);
        $topic.focus();
        return;
      }
    } catch (_e) {
      $topic.focus();
      return;
    } finally {
      creating = false;
      $create.disabled = false;
    }
    $topic.value = "";
    knobForm($newKnobs, {}); // 다음 글은 다시 기본값부터
    load();
  }

  $create.addEventListener("click", create);
  wireAdvToggle($newAdv, $newKnobs);

  // ── 대화방 복제 모달 (v1.21.0) ─────────────────────────
  // 각 글 카드의 '복제' 버튼이 그 글을 원본으로 이 모달을 연다. 한 창에서
  // 주제·모델·복사 항목을 다 설정하고 [복제 생성]. 닫기(backdrop/Esc/✕/
  // 취소) = 중단. dir 체크 = 통째 복사(하위는 백엔드 copytree), 조상이
  // 체크되면 자식 rel 은 안 보냄(dedupe). .agent-cli/sessions/… 포함 시
  // 대화까지 이어받음.
  const $cloneDlg = document.getElementById("clone-dlg");
  const $cloneTopic = document.getElementById("clone-topic");
  const $cloneModel = document.getElementById("clone-model");
  const $cloneAdv = document.getElementById("clone-adv");
  const $cloneKnobs = document.getElementById("clone-knobs");
  wireAdvToggle($cloneAdv, $cloneKnobs);
  const $cloneSrcLabel = document.getElementById("clone-src-label");
  const $cloneTree = document.getElementById("clone-tree");
  const $cloneMsg = document.getElementById("clone-msg");
  const $cloneGo = document.getElementById("clone-go");
  const cloneChecked = new Set(); // 체크된 rel 들
  let cloneFrom = null;

  async function openCloneDialog(p) {
    cloneFrom = p.post_id;
    cloneChecked.clear();
    $cloneSrcLabel.textContent = "Source: " + p.topic;
    $cloneTopic.value = p.topic + " (clone)";
    $cloneMsg.textContent = "";
    $cloneGo.disabled = false;
    // 모델 옵션 채우기 (new-model 과 동일 소스 — 바인딩 있는 모델만)
    fillModelPicker($cloneModel, p.model);
    // 노브는 원본 값으로 채운다 — 복제는 설정까지 이어받는다 (비우면 기본값)
    knobForm($cloneKnobs, p.knobs || {});
    $cloneAdv.hidden = !SPEC.full;
    $cloneKnobs.hidden = true;
    $cloneAdv.setAttribute("aria-expanded", "false");
    $cloneDlg.showModal();
    // 트리 로드
    $cloneTree.textContent = "Loading…";
    const res = await fetchTree("");
    $cloneTree.textContent = "";
    if (!res.ok) {
      $cloneTree.innerHTML =
        '<div class="clone-empty">Could not load the file list (HTTP ' +
        res.status +
        "). The board process may be an old version — <b>restart</b> it and try again.</div>";
      return;
    }
    if (!res.nodes.length) {
      $cloneTree.innerHTML =
        '<div class="clone-empty">This room has no files to copy yet ' +
        "(never opened, or an empty workspace).</div>";
      return;
    }
    res.nodes.forEach((n) => $cloneTree.appendChild(cloneNode(n)));
  }

  function closeCloneDialog() {
    if ($cloneDlg.open) $cloneDlg.close();
    cloneFrom = null;
    cloneChecked.clear();
    $cloneTree.innerHTML = "";
  }

  function cloneSelection() {
    const all = [...cloneChecked];
    // 조상이 이미 체크된 rel 은 제거 (dir 통째 복사가 커버).
    return all.filter(
      (r) => !all.some((a) => a !== r && r.startsWith(a + "/"))
    );
  }

  let cloning = false;
  async function submitClone() {
    // `$cloneGo.disabled` 는 **버튼 클릭만** 막는다 — Enter 경로는 그대로
    // 들어오므로 플래그가 따로 있어야 한다 (create 와 같은 사고).
    if (cloning) return;
    const topic = $cloneTopic.value.trim();
    if (!topic) {
      $cloneTopic.focus();
      return;
    }
    if (!$cloneModel.value) {
      $cloneMsg.textContent = BOUND.length ? "Pick a model" : "No model has a dialect — set one in ⚙ admin";
      $cloneModel.focus();
      return;
    }
    cloning = true;
    $cloneGo.disabled = true;
    $cloneMsg.textContent = "Cloning…";
    const r = await fetch("/api/posts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        topic: topic,
        knobs: Object.assign({ model: $cloneModel.value }, readKnobForm($cloneKnobs)),
        clone_from: cloneFrom,
        clone_paths: cloneSelection(),
      }),
    });
    if (!r.ok) {
      const detail = await r.json().catch(() => ({}));
      $cloneMsg.textContent = "Failed: " + (modelProblemText(detail.detail) || detail.detail || r.status);
      $cloneGo.disabled = false;
      cloning = false;
      return;
    }
    cloning = false;
    closeCloneDialog();
    load();
  }

  // {ok, status, nodes} — 부재/에러(404 등)를 빈 디렉토리와 구분한다.
  async function fetchTree(rel) {
    try {
      const resp = await fetch(
        `/api/posts/${cloneFrom}/tree?path=${encodeURIComponent(rel)}`
      );
      if (!resp.ok) return { ok: false, status: resp.status, nodes: [] };
      return { ok: true, status: 200, nodes: await resp.json() };
    } catch (e) {
      return { ok: false, status: 0, nodes: [] };
    }
  }

  async function renderCloneTree(container, rel) {
    const res = await fetchTree(rel);
    res.nodes.forEach((n) => container.appendChild(cloneNode(n)));
  }

  function cloneNode(n) {
    const row = document.createElement("div");
    row.className = "clone-node";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.addEventListener("change", () => {
      if (cb.checked) cloneChecked.add(n.rel);
      else cloneChecked.delete(n.rel);
    });
    const label = document.createElement("span");
    label.className = "clone-name";
    label.textContent =
      (n.type === "dir" ? "📁 " : "📄 ") + n.name + " (" + fmtSize(n.size) + ")";
    row.appendChild(cb);
    row.appendChild(label);
    if (n.type === "dir") {
      const kids = document.createElement("div");
      kids.className = "clone-kids";
      kids.hidden = true;
      let loaded = false;
      label.style.cursor = "pointer";
      label.addEventListener("click", async () => {
        kids.hidden = !kids.hidden;
        if (!loaded) {
          loaded = true;
          await renderCloneTree(kids, n.rel);
        }
      });
      row.appendChild(kids);
    }
    return row;
  }

  function fmtSize(b) {
    if (b < 1024) return b + "B";
    if (b < 1024 * 1024) return (b / 1024).toFixed(0) + "K";
    return (b / 1024 / 1024).toFixed(1) + "M";
  }

  document.getElementById("clone-go").addEventListener("click", submitClone);
  document
    .getElementById("clone-cancel")
    .addEventListener("click", closeCloneDialog);
  document.getElementById("clone-x").addEventListener("click", closeCloneDialog);
  // backdrop 클릭(다이얼로그 바깥) = 닫기
  $cloneDlg.addEventListener("click", (e) => {
    if (e.target === $cloneDlg) closeCloneDialog();
  });
  // Esc(native cancel) 로 닫을 때도 상태 정리
  $cloneDlg.addEventListener("close", () => {
    cloneFrom = null;
    cloneChecked.clear();
  });
  // `isComposing` — 한글/일어 입력에서 조합 확정 Enter 와 제출 Enter 가
  // **각각** keydown 을 낸다. 확정 쪽을 걸러야 한 번만 제출된다.
  $cloneTopic.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.isComposing) submitClone();
  });
  $topic.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.isComposing) create();
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
        '<div class="empty">No posts yet. Create one.</div>';
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
  dashHeld = true; // 대시보드 SSE 는 무조건 연결(카운트에 자기 포함)
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
