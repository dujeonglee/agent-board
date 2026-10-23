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
      $("cfg-path").textContent = c.path + (c.exists ? "" : " (missing — created on save)");
      $("cfg-provider").value = c.provider || "openai";
      $("cfg-base-url").value = c.base_url;
      $("cfg-api-key").value = c.api_key; // "***" 또는 ""
      $("cfg-default-model").value = c.default_model;
    } catch (e) {
      status($("cfg-status"), "Load failed: " + e.message, false);
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
      status($("cfg-status"), "Saved — applies to newly opened instances", true);
      loadConfig();
    } catch (e) {
      status($("cfg-status"), "Save failed: " + e.message, false);
    }
  });

  // ── models.json ─────────────────────────────────────────────
  let modelsView = { models: [], new: [], probe_error: "" };
  const esc = (s) =>
    String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  // 방언 선택 가이드 — 어떤 모양으로 돌릴지 모르는 사용자용 한 줄. 이름은
  // agent-cli 등록명(옵션 소스)이고, 사전에 없는 이름은 이름만 보인다.
  const DIALECT_GUIDE = {
    json_fc: "Recommended default — prose reasoning + a JSON array of tool calls. Pick this if unsure.",
    native_fc: "When the server parses function calls itself (tools/tool_calls) — OpenAI-compatible servers only.",
    xml_fc: "For models that prefer <tool_call><function=…> tags (no JSON escaping in values).",
    hermes_json: "<tool_call>{\"name\",\"arguments\"}</tool_call> — the native shape of Hermes and Qwen models.",
    glm_argkey: "<tool_call>NAME<arg_key>…</arg_key><arg_value>…</arg_value> — GLM models.",
  };
  function showDialectGuide() {
    const v = $("ef-wire").value;
    $("ef-wire-guide").textContent = v
      ? DIALECT_GUIDE[v] || v
      : "Required — choose the reply shape (dialect) this model runs with. Without it agent-cli will not run the model.";
  }

  function entryCells(entry) {
    return (
      "<td>" + (entry.context_window ?? "—") + "</td>" +
      "<td>" + (entry.max_output_tokens ?? "—") + "</td>" +
      "<td>" + (entry.supports_thinking ? "✓" : "✗") + "</td>" +
      // 📐 3값 — 모름(키 없음)은 ? 로, false 로 뭉개지 않는다
      "<td>" + (entry.supports_grammar === true ? "✓" : entry.supports_grammar === false ? "✗" : "?") + "</td>" +
      // 👁 vision 도 3값 (v1.38.0) — 키 없음은 첫 이미지에서 프로브해 채워진다(agent-cli ≥ 10.68.0)
      "<td>" + (entry.supports_vision === true ? "✓" : entry.supports_vision === false ? "✗" : "?") + "</td>" +
      // dialect — 없으면 미설정: agent-cli(≥ 10.3.0)가 이 모델을 실행하지 않는다
      "<td>" + (entry.dialect ? esc(entry.dialect) : "<span class='badge unbound'>⚠ unbound</span>") + "</td>"
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
      edit.title = "Edit";
      edit.addEventListener("click", () => openEntryDialog(row.id, row.entry));
      actions.appendChild(edit);
      const del = document.createElement("button");
      del.className = "btn-danger";
      del.textContent = "🗑";
      del.title = "Remove from the registry";
      del.addEventListener("click", async () => {
        if (!confirm("Remove '" + row.id + "' from models.json?")) return;
        try {
          await api("DELETE", "/api/admin/models/" + encodeURIComponent(row.id));
          refreshModels();
        } catch (e) {
          status($("models-status"), "Remove failed: " + e.message, false);
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
      detect.textContent = "🔍 Detect";
      detect.title = "Auto-detect capabilities (may take tens of seconds)";
      detect.addEventListener("click", async () => {
        detect.disabled = true;
        detect.textContent = "Detecting…";
        status($("models-status"), "Detecting '" + mid + "' capabilities — may take tens of seconds", true);
        try {
          const r = await api("POST", "/api/admin/models/detect", { model: mid });
          status($("models-status"), "Detection done — review the values, then save", true);
          openEntryDialog(mid, r.entry);
        } catch (e) {
          status($("models-status"), "Detection failed: " + e.message + " — you can still save manual values", false);
          openEntryDialog(mid, {});
        } finally {
          detect.disabled = false;
          detect.textContent = "🔍 Detect";
        }
      });
      actions.appendChild(detect);
      const manual = document.createElement("button");
      manual.className = "btn-ghost";
      manual.textContent = "✎ Manual";
      manual.title = "Enter values without detection";
      manual.addEventListener("click", () => openEntryDialog(mid, {}));
      actions.appendChild(manual);
      tbody.appendChild(tr);
    }
    const missing = modelsView.models.filter((m) => m.status === "missing").length;
    $("models-clean").disabled = missing === 0;
    $("models-clean").textContent = "🗑 Clean up all missing" + (missing ? " (" + missing + ")" : "");
    if (modelsView.probe_error) {
      status($("models-status"), "Endpoint probe failed: " + modelsView.probe_error +
        " — showing the registry only, without status", false);
    }
  }

  async function refreshModels() {
    try {
      modelsView = await api("GET", "/api/admin/models");
      if (!modelsView.probe_error) status($("models-status"), "", true);
      render();
      openModelFromHash();
    } catch (e) {
      status($("models-status"), "List failed: " + e.message, false);
    }
  }

  $("models-refresh").addEventListener("click", refreshModels);

  $("models-clean").addEventListener("click", async () => {
    const missing = modelsView.models.filter((m) => m.status === "missing");
    if (!missing.length) return;
    if (!confirm("Remove " + missing.length + " model(s) gone from the server from models.json?\n" +
      missing.map((m) => "- " + m.id).join("\n"))) return;
    for (const m of missing) {
      try {
        await api("DELETE", "/api/admin/models/" + encodeURIComponent(m.id));
      } catch (e) {
        status($("models-status"), "Remove '" + m.id + "' failed: " + e.message, false);
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
    $("ef-vision").value =
      entry.supports_vision === true ? "true" : entry.supports_vision === false ? "false" : "";
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
    pick.textContent = "— pick a dialect —";
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
    status($("entry-status"), current ? "" : "Without a dialect binding this model will not run", !!current);
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
  // 같은 문서에서 해시만 바뀌어도(어드민 탭이 이미 열려 있을 때) 연다.
  window.addEventListener("hashchange", openModelFromHash);

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
      status($("entry-status"), "Pick a dialect — without one this model will not run", false);
      $("ef-wire").focus();
      return;
    }
    entry.dialect = dialect;
    // "" = 필드 미기록 → 인스턴스에선 미확인(잠김), 감지(프로브)가 판정해 적는다.
    // 여기서는 true/false 만 적는다.
    const sg = $("ef-grammar").value;
    if (sg) entry.supports_grammar = sg === "true";
    const sv = $("ef-vision").value;
    if (sv) entry.supports_vision = sv === "true";
    try {
      await api("PUT", "/api/admin/models/" + encodeURIComponent(dlgModelId), entry);
      $("entry-dlg").close();
      refreshModels();
    } catch (e) {
      status($("entry-status"), "Save failed: " + e.message, false);
    }
  });

  loadConfig();
  refreshModels();
})();
