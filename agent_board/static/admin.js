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

  function entryCells(entry) {
    return (
      "<td>" + (entry.context_window ?? "—") + "</td>" +
      "<td>" + (entry.max_output_tokens ?? "—") + "</td>" +
      "<td>" + (entry.supports_structured_output ? "✓" : "✗") + "</td>" +
      "<td>" + (entry.supports_thinking ? "✓" : "✗") + "</td>"
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
      edit.textContent = "✎";
      edit.title = "편집";
      edit.addEventListener("click", () => openEntryDialog(row.id, row.entry));
      actions.appendChild(edit);
      const del = document.createElement("button");
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
    $("ef-structured").checked = !!entry.supports_structured_output;
    $("ef-strict").checked = !!entry.supports_strict_schema;
    $("ef-thinking").checked = !!entry.supports_thinking;
    $("ef-budget").value = entry.thinking_budget ?? 0;
    $("ef-format").value = entry.thinking_format ?? "";
    status($("entry-status"), "", true);
    $("entry-dlg").showModal();
  }

  $("entry-cancel").addEventListener("click", () => $("entry-dlg").close());

  $("entry-save").addEventListener("click", async () => {
    const entry = {
      context_window: parseInt($("ef-ctx").value, 10) || 4096,
      max_output_tokens: parseInt($("ef-maxout").value, 10) || 2048,
      supports_structured_output: $("ef-structured").checked,
      supports_thinking: $("ef-thinking").checked,
      thinking_budget: parseInt($("ef-budget").value, 10) || 0,
      supports_strict_schema: $("ef-strict").checked,
      thinking_format: $("ef-format").value,
    };
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
