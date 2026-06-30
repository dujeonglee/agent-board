// agent-board — vanilla JS client.
// Lists posts, creates new ones, opens (→ spawn-or-attach + redirect),
// toggles force-active, deletes. All over /api/posts*.
(function () {
  const $posts = document.getElementById("posts");
  const $topic = document.getElementById("new-topic");
  const $directive = document.getElementById("new-directive");
  const $model = document.getElementById("new-model");
  const $create = document.getElementById("new-create");
  const $sameTab = document.getElementById("same-tab");

  // "open in current page" preference persists across reloads (default: new tab)
  $sameTab.checked = localStorage.getItem("agentboard_same_tab") === "1";
  $sameTab.addEventListener("change", () =>
    localStorage.setItem("agentboard_same_tab", $sameTab.checked ? "1" : "0")
  );

  let MODELS = []; // registry, shared by the new-post form + per-post dropdowns

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
      `<button class="open" type="button">열기</button>` +
      `<button class="del" type="button" title="삭제(영구)">🗑</button>` +
      `</div>`;

    el.querySelector(".open").addEventListener("click", () => open(p.post_id));
    el.querySelector(".del").addEventListener("click", () => del(p));
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
    const win = sameTab ? null : window.open("", "_blank");
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
    const directive = $directive.value.trim();
    await fetch("/api/posts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        topic: topic,
        directive: directive || null,
        model_id: $model.value || null,
      }),
    });
    $topic.value = "";
    $directive.value = "";
    load();
  }

  $create.addEventListener("click", create);
  $topic.addEventListener("keydown", (e) => {
    if (e.key === "Enter") create();
  });

  loadModels();
  load();
  setInterval(load, 5000); // refresh status periodically
})();
