// agent-board — vanilla JS client.
// Lists posts, creates new ones, opens (→ spawn-or-attach + redirect),
// toggles force-active, deletes. All over /api/posts*.
(function () {
  const $posts = document.getElementById("posts");
  const $topic = document.getElementById("new-topic");
  const $directive = document.getElementById("new-directive");
  const $model = document.getElementById("new-model");
  const $create = document.getElementById("new-create");

  async function loadModels() {
    const models = await fetch("/api/models").then((r) => r.json());
    models.forEach((m) => {
      const o = document.createElement("option");
      o.value = m.id;
      o.textContent = m.provider ? `${m.id} (${m.provider})` : m.id;
      $model.appendChild(o);
    });
  }

  const esc = (s) =>
    (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

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
    el.className = "post";
    const st = STATUS[p.status] || STATUS.idle;
    el.innerHTML =
      `<div class="post-main">` +
      `<div class="post-topic">${esc(p.topic)}` +
      (p.model_id ? `<span class="model-tag">${esc(p.model_id)}</span>` : "") +
      `</div>` +
      `<div class="post-last">${esc(p.last_query) || "<span class='muted'>— 아직 질문 없음</span>"}</div>` +
      `</div>` +
      `<div class="post-side">` +
      `<span class="st"><span class="dot ${st.cls}"></span>${st.label}</span>` +
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
    return el;
  }

  async function open(post_id) {
    const r = await fetch(`/api/posts/${post_id}/open`, { method: "POST" });
    if (!r.ok) {
      alert("열기 실패: " + r.status);
      return;
    }
    location.href = (await r.json()).url; // → /s/<post_id>/
  }

  async function forceActive(post_id, enabled) {
    await fetch(`/api/posts/${post_id}/force_active`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: enabled }),
    });
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
