// agent-board admin — config.json 폼 + models.json 상태 테이블.
// 본체 app.js 와 격리된 별도 페이지 스크립트 (vanilla, 의존성 0).
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  function status(el, msg, ok) {
    el.textContent = msg || "";
    el.className = "adm-status" + (msg ? (ok ? " ok" : " err") : "");
  }

  async function api(method, url, body) {
    const r = await fetch(url, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!r.ok) {
      let detail = r.status + " " + r.statusText;
      try {
        detail = (await r.json()).detail || detail;
      } catch (_e) { /* non-JSON error body */ }
      throw new Error(detail);
    }
    return r.json();
  }

  // ── config.json ─────────────────────────────────────────────
  async function loadConfig() {
    try {
      const c = await api("GET", "/api/admin/config");
      $("cfg-path").textContent = c.path + (c.exists ? "" : " (없음 — 저장 시 생성)");
      $("cfg-provider").value = c.provider || "openai";
      $("cfg-base-url").value = c.base_url;
      $("cfg-api-key").value = c.api_key; // "***" 또는 ""
      $("cfg-default-model").value = c.default_model;
    } catch (e) {
      status($("cfg-status"), "불러오기 실패: " + e.message, false);
    }
  }

  $("cfg-save").addEventListener("click", async () => {
    try {
      await api("PUT", "/api/admin/config", {
        provider: $("cfg-provider").value,
        base_url: $("cfg-base-url").value,
        api_key: $("cfg-api-key").value,
        default_model: $("cfg-default-model").value,
      });
      status($("cfg-status"), "저장됨 — 새로 여는 인스턴스부터 적용", true);
      loadConfig();
    } catch (e) {
      status($("cfg-status"), "저장 실패: " + e.message, false);
    }
  });

  // ── models.json ─────────────────────────────────────────────
  let modelsView = { models: [], new: [], probe_error: "" };
  const esc = (s) =>
    String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  // 방언 선택 가이드 — 어떤 모양으로 돌릴지 모르는 사용자용 한 줄. 이름은
  // agent-cli 등록명(옵션 소스)이고, 사전에 없는 이름은 이름만 보인다.
  const DIALECT_GUIDE = {
    json_fc: "일반 권장 — 산문 reasoning + JSON 배열 tool-call. 모르면 이것.",
    native_fc: "서버가 함수 호출(tools/tool_calls)을 직접 파싱할 때 — OpenAI 호환 서버 전용.",
    xml_fc: "<tool_call><function=…> 태그 모양을 선호하는 모델용 (값에 JSON escaping 없음).",
    hermes_json: "<tool_call>{\"name\",\"arguments\"}</tool_call> — Hermes·Qwen 계열 네이티브 모양.",
    glm_argkey: "<tool_call>NAME<arg_key>…</arg_key><arg_value>…</arg_value> — GLM 계열.",
  };
  function showDialectGuide() {
    const v = $("ef-wire").value;
    $("ef-wire-guide").textContent = v
      ? DIALECT_GUIDE[v] || v
      : "필수 — 이 모델이 어떤 응답 모양(방언)으로 돌지 고르세요. 없으면 agent-cli 가 이 모델을 실행하지 않습니다.";
  }

  function entryCells(entry) {
    return (
      "<td>" + (entry.context_window ?? "—") + "</td>" +
      "<td>" + (entry.max_output_tokens ?? "—") + "</td>" +
      "<td>" + (entry.supports_thinking ? "✓" : "✗") + "</td>" +
      // 📐 3값 — 모름(키 없음)은 ? 로, false 로 뭉개지 않는다
      "<td>" + (entry.supports_grammar === true ? "✓" : entry.supports_grammar === false ? "✗" : "?") + "</td>" +
      // dialect — 없으면 미설정: agent-cli(≥ 10.3.0)가 이 모델을 실행하지 않는다
      "<td>" + (entry.dialect ? esc(entry.dialect) : "<span class='badge unbound'>⚠ 미설정</span>") + "</td>"
    );
  }

  function render() {
    const tbody = $("models-body");
    tbody.innerHTML = "";
    for (const row of modelsView.models) {
      const tr = document.createElement("tr");
      tr.innerHTML =
        "<td></td><td><span class='badge " + row.status + "'>" +
        row.status + "</span></td>" + entryCells(row.entry) + "<td></td>";
      tr.cells[0].textContent = row.id; // textContent — id 는 이스케이프
      const actions = tr.cells[tr.cells.length - 1];
      const edit = document.createElement("button");
      edit.className = "btn-icon";
      edit.textContent = "✎";
      edit.title = "편집";
      edit.addEventListener("click", () => openEntryDialog(row.id, row.entry));
      actions.appendChild(edit);
      const del = document.createElement("button");
      del.className = "btn-danger";
      del.textContent = "🗑";
      del.title = "registry 에서 삭제";
      del.addEventListener("click", async () => {
        if (!confirm("'" + row.id + "' 를 models.json 에서 삭제할까요?")) return;
        try {
          await api("DELETE", "/api/admin/models/" + encodeURIComponent(row.id));
          refreshModels();
        } catch (e) {
          status($("models-status"), "삭제 실패: " + e.message, false);
        }
      });
      actions.appendChild(del);
      tbody.appendChild(tr);
    }
    for (const mid of modelsView.new) {
      const tr = document.createElement("tr");
      tr.innerHTML =
        "<td></td><td><span class='badge new'>NEW</span></td>" +
        "<td>—</td><td>—</td><td>—</td><td>—</td><td></td>";
      tr.cells[0].textContent = mid;
      const actions = tr.cells[tr.cells.length - 1];
      const detect = document.createElement("button");
      detect.className = "btn-primary";
      detect.textContent = "🔍 탐지";
      detect.title = "capability 자동 탐지 (수십 초 걸릴 수 있음)";
      detect.addEventListener("click", async () => {
        detect.disabled = true;
        detect.textContent = "탐지 중…";
        status($("models-status"), "'" + mid + "' capability 탐지 중 — 수십 초 걸릴 수 있습니다", true);
        try {
          const r = await api("POST", "/api/admin/models/detect", { model: mid });
          status($("models-status"), "탐지 완료 — 값 검토 후 저장하세요", true);
          openEntryDialog(mid, r.entry);
        } catch (e) {
          status($("models-status"), "탐지 실패: " + e.message + " — 수동 입력으로 저장 가능", false);
          openEntryDialog(mid, {});
        } finally {
          detect.disabled = false;
          detect.textContent = "🔍 탐지";
        }
      });
      actions.appendChild(detect);
      const manual = document.createElement("button");
      manual.className = "btn-ghost";
      manual.textContent = "✎ 수동";
      manual.title = "탐지 없이 직접 입력";
      manual.addEventListener("click", () => openEntryDialog(mid, {}));
      actions.appendChild(manual);
      tbody.appendChild(tr);
    }
    const missing = modelsView.models.filter((m) => m.status === "missing").length;
    $("models-clean").disabled = missing === 0;
    $("models-clean").textContent = "🗑 missing 전체 정리" + (missing ? " (" + missing + ")" : "");
    if (modelsView.probe_error) {
      status($("models-status"), "endpoint 프로브 실패: " + modelsView.probe_error +
        " — 상태 분류 없이 registry 만 표시", false);
    }
  }

  async function refreshModels() {
    try {
      modelsView = await api("GET", "/api/admin/models");
      if (!modelsView.probe_error) status($("models-status"), "", true);
      render();
      openModelFromHash();
    } catch (e) {
      status($("models-status"), "목록 실패: " + e.message, false);
    }
  }

  $("models-refresh").addEventListener("click", refreshModels);

  $("models-clean").addEventListener("click", async () => {
    const missing = modelsView.models.filter((m) => m.status === "missing");
    if (!missing.length) return;
    if (!confirm("서버에서 사라진 " + missing.length + "개 모델을 models.json 에서 삭제할까요?\n" +
      missing.map((m) => "- " + m.id).join("\n"))) return;
    for (const m of missing) {
      try {
        await api("DELETE", "/api/admin/models/" + encodeURIComponent(m.id));
      } catch (e) {
        status($("models-status"), "'" + m.id + "' 삭제 실패: " + e.message, false);
        break;
      }
    }
    refreshModels();
  });

  // ── entry 편집 다이얼로그 ───────────────────────────────────
  let dlgModelId = "";

  function openEntryDialog(mid, entry) {
    dlgModelId = mid;
    $("entry-title").textContent = mid;
    $("ef-ctx").value = entry.context_window ?? 4096;
    $("ef-maxout").value = entry.max_output_tokens ?? 2048;
    $("ef-thinking").checked = !!entry.supports_thinking;
    $("ef-grammar").value =
      entry.supports_grammar === true ? "true" : entry.supports_grammar === false ? "false" : "";
    // dialect 바인딩 — 필수, 등록명 드롭다운만 (자유입력 금지: agent-cli 가
    // unknown 이름에 fail-fast). auto 없음 (v1.33.0): 빈 값은 저장이 거절된다.
    // 미설치로 목록이 비어도 현재값은 보존.
    const sel = $("ef-wire");
    const current = entry.dialect || "";
    const names = [...(modelsView.dialects || [])];
    if (current && !names.includes(current)) names.push(current);
    sel.innerHTML = "";
    const pick = document.createElement("option");
    pick.value = "";
    pick.textContent = "— 방언을 고르세요 —";
    pick.disabled = true;
    sel.appendChild(pick);
    for (const n of names) {
      const o = document.createElement("option");
      o.value = n;
      o.textContent = n;
      sel.appendChild(o);
    }
    sel.value = current;
    showDialectGuide();
    status($("entry-status"), current ? "" : "방언 바인딩이 없으면 이 모델은 실행되지 않습니다", !!current);
    $("entry-dlg").showModal();
    if (!current) sel.focus();
  }

  $("ef-wire").addEventListener("change", showDialectGuide);

  // 딥링크 ``/admin#model=<id>`` — 방 카드의 "⚙ 설정" 이 그 모델의 편집
  // 창을 바로 연다 (등록된 모델이면 현재 entry, NEW 면 빈 entry).
  function openModelFromHash() {
    const m = /^#model=(.+)$/.exec(location.hash || "");
    if (!m) return;
    const mid = decodeURIComponent(m[1]);
    history.replaceState(null, "", location.pathname); // 한 번만
    const row = modelsView.models.find((r) => r.id === mid);
    openEntryDialog(mid, row ? row.entry : {});
  }

  $("entry-cancel").addEventListener("click", () => $("entry-dlg").close());

  $("entry-save").addEventListener("click", async () => {
    const entry = {
      context_window: parseInt($("ef-ctx").value, 10) || 4096,
      max_output_tokens: parseInt($("ef-maxout").value, 10) || 2048,
      supports_thinking: $("ef-thinking").checked,
    };
    // 방언은 필수 (v1.33.0) — 서버도 거절하지만 여기서 먼저 멈춘다.
    const dialect = $("ef-wire").value;
    if (!dialect) {
      status($("entry-status"), "방언을 고르세요 — 없으면 이 모델은 실행되지 않습니다", false);
      $("ef-wire").focus();
      return;
    }
    entry.dialect = dialect;
    // "" = 필드 미기록 → 인스턴스에선 미확인(잠김), 감지(프로브)가 판정해 적는다.
    // 여기서는 true/false 만 적는다.
    const sg = $("ef-grammar").value;
    if (sg) entry.supports_grammar = sg === "true";
    try {
      await api("PUT", "/api/admin/models/" + encodeURIComponent(dlgModelId), entry);
      $("entry-dlg").close();
      refreshModels();
    } catch (e) {
      status($("entry-status"), "저장 실패: " + e.message, false);
    }
  });

  loadConfig();
  refreshModels();
})();
