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

  // ── 연결 카운트 (v1.16.0 — 샘플링 복귀) ──
  // v1.15 의 Web Locks 는 secure context 전용이라 LAN http(주 운용)에서
  // 락 API 자체가 노출되지 않아 무동작이었다. 열기 게이트는 사람 속도의
  // 버튼 클릭이라 ping/pong 샘플링으로 충분 — 대량 동시 오픈은 board
  // 경유 운용 전제(agent-cli v7.7.0)에서 발생하지 않는다.
  //
  // ★샘플링 창 = 100ms (v1.22.2, 이전 300ms). 이 창은 열기 클릭 직후
  // about:blank 창이 대기하는 시간이라 재열기 체감 지연에 직결된다 —
  // 실측상 방 페이지 자체 로드는 87ms 인데 게이트가 300ms 를 먹어
  // "blank 이후" 지연의 ~77% 가 이 게이트였다. BroadcastChannel pong
  // 은 같은 브라우저 내 전달이라 유휴 탭이 ~5ms 에 답하므로 100ms 면
  // 20배 여유로 다 잡힌다. 렌더로 JS 가 블록된 탭은 300ms 로도 창 안에
  // 못 답하니(집계 누락) 잡히는 탭 집합은 100·300ms 가 실질 동일 =
  // 하드 블록 보장 불변, 새 레이스 없음. 클릭 시점 샘플링을 유지하므로
  // 스테일 캐시 위험도 없다.
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
  // main 유휴 + 상주 에이전트 작업 중 (v1.17.0) — 같은 원형 dot, 색만
  // 구분(보라). "응답 중"과 합치지 않는 이유: main 은 비어 있어 지금
  // 말 걸어도 되는 상태라는 정보가 사라짐.
  const AGENTS_BUSY = { cls: "agents-busy", label: "에이전트 작업 중" };

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
      ? { cls: "await", label: "❗ 응답 필요" }
      : p.status === "running" && agentsWorking
        ? AGENTS_BUSY
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
      (up ? agentsChip(p) : "") +
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
      `<button class="clone btn-ghost" type="button" title="이 글의 파일/대화를 복사해 새 글 시작">📋 복제</button>` +
      `<button class="open btn-primary" type="button">열기</button>` +
      `<button class="del btn-danger" type="button" title="삭제(영구)">🗑</button>` +
      `</div>`;

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
    const sel = el.querySelector(".model-sel");
    if (sel)
      sel.addEventListener("change", (e) =>
        changeModel(p, e.target.value, e.target)
      );
    return el;
  }

  async function open(post_id) {
    const sameTab = $sameTab.checked;
    // 게이트·POST 를 먼저 끝내고, 완성된 방 URL 로 새 탭을 **바로** 연다.
    // ★예전엔 클릭 제스처 안에서 빈 탭(window.open(""))을 먼저 열고 나중에
    // win.location 으로 navigate 했는데, 그 about:blank→실URL 전환이
    // 재열기를 ~1초 굼뜨게 하는 주범이었다 — 직접 URL 붙여넣기·현재 탭
    // 열기(둘 다 실 URL 로 바로 이동)가 빠른 것과 대비되어 실측 확정.
    // window.open 을 await(게이트~100ms + fetch~15ms) 뒤에 호출해도
    // Chrome 의 transient user activation(클릭 후 ~5초)이 살아 있어 팝업
    // 차단 없이 열린다(실측 확인) — 차단 시엔 현재 탭 이동으로 폴백.
    if (gatewayMode !== "caddy") {
      const held = await countHeldTabs();
      // 이 글의 탭이 이미 있으면(named-window 재사용 or sameTab 전환)
      // 연결이 늘지 않으므로 게이트 면제.
      const reusing = held.paths.some((p) => p.startsWith(`/s/${post_id}/`));
      if (!reusing) {
        if (held.count >= MAX_HELD_TABS) {
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
      alert("열기 실패: " + r.status);
      return;
    }
    const url = (await r.json()).url; // → /s/<post_id>/
    if (sameTab) {
      location.href = url;
    } else {
      // 완성된 URL 로 직접 — named target(post_id)은 같은 글을 다시 열면
      // 새 탭 대신 그 글의 기존 창을 재사용(연결이 안 늘어 게이트 면제
      // 대상). 팝업이 차단되면(null) 현재 탭 이동으로 폴백.
      const win = window.open(url, "agentcli-" + post_id);
      if (win) win.focus();
      else location.href = url;
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
      body: JSON.stringify({ topic: topic, model_id: $model.value || null }),
    });
    $topic.value = "";
    load();
  }

  $create.addEventListener("click", create);

  // ── 대화방 복제 모달 (v1.21.0) ─────────────────────────
  // 각 글 카드의 '복제' 버튼이 그 글을 원본으로 이 모달을 연다. 한 창에서
  // 주제·모델·복사 항목을 다 설정하고 [복제 생성]. 닫기(backdrop/Esc/✕/
  // 취소) = 중단. dir 체크 = 통째 복사(하위는 백엔드 copytree), 조상이
  // 체크되면 자식 rel 은 안 보냄(dedupe). .agent-cli/sessions/… 포함 시
  // 대화까지 이어받음.
  const $cloneDlg = document.getElementById("clone-dlg");
  const $cloneTopic = document.getElementById("clone-topic");
  const $cloneModel = document.getElementById("clone-model");
  const $cloneSrcLabel = document.getElementById("clone-src-label");
  const $cloneTree = document.getElementById("clone-tree");
  const $cloneMsg = document.getElementById("clone-msg");
  const $cloneGo = document.getElementById("clone-go");
  const cloneChecked = new Set(); // 체크된 rel 들
  let cloneFrom = null;

  async function openCloneDialog(p) {
    cloneFrom = p.post_id;
    cloneChecked.clear();
    $cloneSrcLabel.textContent = "원본: " + p.topic;
    $cloneTopic.value = p.topic + " (복제)";
    $cloneMsg.textContent = "";
    $cloneGo.disabled = false;
    // 모델 옵션 채우기 (new-model 과 동일 소스)
    $cloneModel.innerHTML = '<option value="">기본 모델</option>';
    MODELS.forEach((m) => {
      const o = document.createElement("option");
      o.value = m.id;
      o.textContent = m.provider ? `${m.id} (${m.provider})` : m.id;
      $cloneModel.appendChild(o);
    });
    $cloneModel.value = p.model_id || "";
    $cloneDlg.showModal();
    // 트리 로드
    $cloneTree.textContent = "불러오는 중…";
    const res = await fetchTree("");
    $cloneTree.textContent = "";
    if (!res.ok) {
      $cloneTree.innerHTML =
        '<div class="clone-empty">파일 목록을 불러오지 못했습니다 (HTTP ' +
        res.status +
        "). board 프로세스가 구버전일 수 있습니다 — <b>재시작</b> 후 다시 시도하세요.</div>";
      return;
    }
    if (!res.nodes.length) {
      $cloneTree.innerHTML =
        '<div class="clone-empty">이 방에는 아직 복사할 파일이 없습니다 ' +
        "(한 번도 열지 않았거나 빈 워크스페이스).</div>";
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

  async function submitClone() {
    const topic = $cloneTopic.value.trim();
    if (!topic) {
      $cloneTopic.focus();
      return;
    }
    $cloneGo.disabled = true;
    $cloneMsg.textContent = "복제 중…";
    const r = await fetch("/api/posts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        topic: topic,
        model_id: $cloneModel.value || null,
        clone_from: cloneFrom,
        clone_paths: cloneSelection(),
      }),
    });
    if (!r.ok) {
      const detail = await r.json().catch(() => ({}));
      $cloneMsg.textContent = "실패: " + (detail.detail || r.status);
      $cloneGo.disabled = false;
      return;
    }
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
  $cloneTopic.addEventListener("keydown", (e) => {
    if (e.key === "Enter") submitClone();
  });
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
