// agent-board admin — 서버 목록(config.json endpoints) + 그 서버의 설정 폼 + 모델 표 (v1.48.0).
// 본체 app.js 와 격리된 별도 페이지 스크립트 (vanilla, 의존성 0).
// 서버가 없으면 이 화면이 곧 셋업 화면이다 — "첫 서버 추가" 폼 → 저장 → 모델 조회 → 기본 모델.
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
      const err = new Error(typeof detail === "string" ? detail : detail.error || JSON.stringify(detail));
      err.status = r.status;
      err.detail = detail;
      throw err;
    }
    return r.json();
  }

  // ── config.json ─────────────────────────────────────────────
  // extra_headers 표 — 행마다 체크박스(enabled)·name·value(password, 저장된 값은 ***)·Remove.
  // 저장은 config 의 Save 하나로 함께 간다(목록 전체를 보낸다 — 지운 행은 사라진다).
  function addHeaderRow(h) {
    const tr = document.createElement("tr");
    const on = document.createElement("input");
    on.type = "checkbox";
    on.className = "hdr-on";
    on.checked = h.enabled !== false;
    on.title = "Send this header";
    const name = document.createElement("input");
    name.type = "text";
    name.className = "hdr-name";
    name.placeholder = "x-service-id";
    name.autocomplete = "off";
    name.value = h.name || "";
    const value = document.createElement("input");
    value.type = "password";
    value.className = "hdr-value";
    value.placeholder = "value";
    value.autocomplete = "new-password";
    value.value = h.value || ""; // 저장된 값은 "***" 로만 온다
    const rm = document.createElement("button");
    rm.type = "button";
    rm.className = "btn-ghost hdr-remove";
    rm.textContent = "Remove";
    rm.addEventListener("click", () => tr.remove());
    for (const el of [on, name, value, rm]) {
      const td = document.createElement("td");
      td.appendChild(el);
      tr.appendChild(td);
    }
    $("cfg-hdrs-body").appendChild(tr);
  }

  function renderHeaders(list) {
    $("cfg-hdrs-body").innerHTML = "";
    for (const h of list || []) addHeaderRow(h);
  }

  function collectHeaders() {
    const out = [];
    for (const tr of $("cfg-hdrs-body").rows) {
      const name = tr.querySelector(".hdr-name").value.trim();
      const value = tr.querySelector(".hdr-value").value;
      if (!name && !value) continue; // 빈 새 행은 무시
      out.push({ name, value, enabled: tr.querySelector(".hdr-on").checked });
    }
    return out;
  }

  $("cfg-hdr-add").addEventListener("click", () => {
    addHeaderRow({ name: "", value: "", enabled: true });
    const rows = $("cfg-hdrs-body").rows;
    rows[rows.length - 1].querySelector(".hdr-name").focus();
  });

  // ── 서버 목록 ───────────────────────────────────────────────
  let serversView = { servers: [], default: null };
  let current = null; // 고른 서버 이름 — null 이면 "서버 추가" 폼
  const enc = encodeURIComponent;
  const srvUrl = (name) => "/api/admin/endpoints/" + enc(name);

  function hostOf(url) {
    try {
      return new URL(url).host;
    } catch (_e) {
      return url || "(no base_url)";
    }
  }

  function renderBanners(v) {
    const gate = $("cli-gate");
    if (v.cli && !v.cli.supports_endpoints) {
      gate.textContent = v.cli.version
        ? "agent-cli " + v.cli.version + " cannot read per-server settings — upgrade agent-cli to " +
          v.cli.required + " or later together with this board. Rooms will not start until then."
        : "The agent-cli version could not be read — per-server settings need agent-cli " + v.cli.required + " or later.";
      gate.hidden = false;
    } else {
      gate.hidden = true;
    }
    const warn = $("adm-warn");
    const lines = [];
    if (v.default_missing)
      lines.push("No default server is set — new rooms and the CLI stop until you pick one with Make default.");
    if (v.unreadable)
      lines.push("Could not read the settings (" + v.unreadable + "). Adding a server keeps the file as a .v1 copy; the servers in it will not be in the new file.");
    warn.textContent = lines.join(" ");
    warn.hidden = !lines.length;
  }

  function renderServerList() {
    const ul = $("srv-list");
    ul.innerHTML = "";
    for (const sv of serversView.servers) {
      const li = document.createElement("li");
      const b = document.createElement("button");
      b.type = "button";
      b.setAttribute("aria-current", String(sv.name === current));
      const name = document.createElement("span");
      name.className = "srv-name";
      name.textContent = sv.name;
      if (sv.default) {
        const d = document.createElement("span");
        d.className = "badge default";
        d.textContent = "● default";
        name.appendChild(d);
      }
      const meta = document.createElement("span");
      meta.className = "srv-meta";
      meta.textContent =
        hostOf(sv.base_url) + " · " + sv.models + " model" + (sv.models === 1 ? "" : "s") +
        (sv.rooms ? " · " + sv.rooms + " room" + (sv.rooms === 1 ? "" : "s") : "");
      b.append(name, meta);
      b.addEventListener("click", () => selectServer(sv.name));
      li.appendChild(b);
      ul.appendChild(li);
    }
    $("srv-add").setAttribute("aria-current", String(current === null));
  }

  async function loadServers(preferred) {
    try {
      serversView = await api("GET", "/api/admin/endpoints");
    } catch (e) {
      status($("cfg-status"), "Load failed: " + e.message, false);
      return;
    }
    renderBanners(serversView);
    const names = serversView.servers.map((x) => x.name);
    const pick =
      [preferred, current, serversView.default, names[0]].find((n) => n && names.includes(n)) || null;
    if (pick) selectServer(pick);
    else showAddForm();
  }

  // 폼을 채운 시점의 값 — 저장은 **바뀐 필드만** 보낸다. 폼을 연 뒤 방이 저장한 값(예: 대화형으로
  // 고른 default_model)이 손대지 않은 폼 칸으로 덮이지 않게(서버는 요청 시점에 파일을 다시 읽어
  // 받은 키만 고친다).
  let loadedFields = {};

  function fillForm(sv) {
    $("cfg-provider").value = sv.provider || "openai";
    $("cfg-base-url").value = sv.base_url || "";
    $("cfg-api-key").value = sv.api_key || ""; // "***" 또는 ""
    $("cfg-default-model").value = sv.default_model || "";
    renderHeaders(sv.extra_headers);
    loadedFields = allFields();
  }

  function selectServer(name) {
    const sv = serversView.servers.find((x) => x.name === name);
    if (!sv) return showAddForm();
    current = name;
    renderServerList();
    $("srv-title").textContent = name;
    $("srv-default-badge").hidden = !sv.default;
    $("cfg-path").textContent = serversView.path;
    $("srv-intro").hidden = true;
    $("cfg-name").hidden = $("cfg-name-label").hidden = true;
    $("cfg-api-key").placeholder = "(set: *** — enter only to change)";
    $("srv-default").hidden = $("srv-delete").hidden = false;
    $("srv-default").disabled = sv.default;
    $("srv-delete").disabled = sv.default;
    $("srv-delete").title = sv.default
      ? "The default server cannot be deleted — make another server the default first"
      : "Delete this server";
    $("cfg-save-note").hidden = false;
    fillForm(sv);
    status($("cfg-status"), "", true);
    $("models").hidden = false;
    refreshModels();
  }

  function showAddForm() {
    current = null;
    renderServerList();
    const first = !serversView.servers.length;
    $("srv-title").textContent = first ? "Add your first server" : "Add server";
    $("srv-default-badge").hidden = true;
    $("cfg-path").textContent = serversView.path || "";
    const intro = $("srv-intro");
    intro.textContent = first
      ? "No model server is saved yet. Name it, point it at the server, and save — the model list is read from the server next, then pick the default model." +
        (serversView.legacy
          ? " Settings saved before agent-cli 10.144.0 are not read; saving keeps the old files as config.json.v1 / models.json.v1 so you can copy values from them."
          : "")
      : "The name cannot be changed later — rooms remember a server by its name.";
    intro.hidden = false;
    $("cfg-name").hidden = $("cfg-name-label").hidden = false;
    $("cfg-api-key").placeholder = "optional — local servers usually need none";
    $("cfg-name").value = "";
    $("srv-default").hidden = $("srv-delete").hidden = true;
    $("cfg-save-note").hidden = true;
    fillForm({ provider: "openai", base_url: "", api_key: "", default_model: "", extra_headers: [] });
    status($("cfg-status"), "", true);
    $("models").hidden = true;
    $("cfg-name").focus();
  }

  $("srv-add").addEventListener("click", showAddForm);

  function allFields() {
    return {
      provider: $("cfg-provider").value,
      base_url: $("cfg-base-url").value,
      api_key: $("cfg-api-key").value,
      default_model: $("cfg-default-model").value,
      extra_headers: collectHeaders(),
    };
  }

  function changedFields() {
    const now = allFields();
    const out = {};
    for (const k of Object.keys(now)) {
      if (JSON.stringify(now[k]) !== JSON.stringify(loadedFields[k])) out[k] = now[k];
    }
    return out;
  }

  $("cfg-save").addEventListener("click", async () => {
    try {
      if (current === null) {
        const name = $("cfg-name").value.trim();
        const r = await api("POST", "/api/admin/endpoints", Object.assign({ name }, allFields()));
        await loadServers(name);
        const moved = (r.moved || []).length ? " Old files kept as: " + r.moved.join(", ") + "." : "";
        status($("cfg-status"), "Saved server '" + name + "'." + moved +
          ($("cfg-default-model").value ? "" : " Pick the default model below, then save."), true);
        if (!$("cfg-default-model").value) $("cfg-default-model").focus();
      } else {
        await api("PUT", srvUrl(current), changedFields());
        await loadServers(current);
        status($("cfg-status"), "Saved — applies to rooms opened from now on", true);
      }
    } catch (e) {
      status($("cfg-status"), "Save failed: " + e.message, false);
    }
  });

  $("srv-default").addEventListener("click", async () => {
    try {
      await api("POST", srvUrl(current) + "/default");
      await loadServers(current);
      status($("cfg-status"), "'" + current + "' is now the default for new rooms and the CLI — existing rooms keep their server", true);
    } catch (e) {
      status($("cfg-status"), "Make default failed: " + e.message, false);
    }
  });

  $("srv-delete").addEventListener("click", async () => {
    const sv = serversView.servers.find((x) => x.name === current);
    if (!sv) return;
    const roomsLine = sv.rooms
      ? "\n\n" + sv.rooms + " room(s) use this server. They will not open until you add a server named '" +
        sv.name + "' again in admin, or clone them to another server."
      : "";
    if (!confirm("Delete server '" + sv.name + "' and its model table?" + roomsLine)) return;
    try {
      await api("DELETE", srvUrl(sv.name) + "?confirm=1");
      current = null;
      await loadServers();
    } catch (e) {
      status($("cfg-status"), "Delete failed: " + e.message, false);
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

  function samplingCell(entry) {
    const parts = [];
    if (typeof entry.temperature === "number") parts.push("T " + entry.temperature);
    if (typeof entry.top_p === "number") parts.push("p " + entry.top_p);
    return parts.length ? esc(parts.join(" · ")) : "—";
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
      // 🎲 sampling (v1.39.0, agent-cli ≥ 10.69.0) — 모델 기본 temperature/top_p; 키 없음은 — (서버 기본)
      "<td>" + samplingCell(entry) + "</td>" +
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
        if (!confirm("Remove '" + row.id + "' from this server's models?")) return;
        try {
          await api("DELETE", srvUrl(current) + "/models/" + enc(row.id));
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
        "<td>—</td><td>—</td><td>—</td><td>—</td><td>—</td><td></td>";
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
          const r = await api("POST", srvUrl(current) + "/models/detect", { model: mid });
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

  // 기본 모델 입력의 제안 목록 — 이 서버가 서빙하는 것 + 표에 있는 것
  function fillModelSuggestions() {
    const list = $("cfg-model-list");
    list.innerHTML = "";
    const ids = [...new Set([...modelsView.models.map((m) => m.id), ...modelsView.new])];
    for (const id of ids) {
      const o = document.createElement("option");
      o.value = id;
      list.appendChild(o);
    }
  }

  async function refreshModels() {
    if (current === null) return;
    const name = current;
    try {
      const view = await api("GET", srvUrl(name) + "/models");
      if (name !== current) return; // 그사이 다른 서버를 골랐다
      modelsView = view;
      status($("models-status"), "", true);
      render();
      fillModelSuggestions();
      openModelFromHash();
    } catch (e) {
      status($("models-status"), "List failed: " + e.message, false);
    }
  }

  $("models-refresh").addEventListener("click", refreshModels);

  $("models-clean").addEventListener("click", async () => {
    const missing = modelsView.models.filter((m) => m.status === "missing");
    if (!missing.length) return;
    if (!confirm("Remove " + missing.length + " model(s) gone from the server from this server's models?\n" +
      missing.map((m) => "- " + m.id).join("\n"))) return;
    for (const m of missing) {
      try {
        await api("DELETE", srvUrl(current) + "/models/" + enc(m.id));
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
    // 🎲 기본 샘플링 — 키 없음은 빈 칸(미기록 → 서버 기본), 숫자만 채운다
    $("ef-temperature").value = typeof entry.temperature === "number" ? entry.temperature : "";
    $("ef-top-p").value = typeof entry.top_p === "number" ? entry.top_p : "";
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

  // 딥링크 ``/admin#server=<name>&model=<id>`` — 방 카드의 "⚙ Set up" 이 그 서버의 그 모델
  // 편집 창을 바로 연다 (등록된 모델이면 현재 entry, NEW 면 빈 entry). 서버만 주면 그 서버를 고른다.
  function hashParams() {
    const out = {};
    for (const part of (location.hash || "").replace(/^#/, "").split("&")) {
      const i = part.indexOf("=");
      if (i > 0) out[part.slice(0, i)] = decodeURIComponent(part.slice(i + 1));
    }
    return out;
  }
  function openModelFromHash() {
    const h = hashParams();
    if (!h.model || (h.server && h.server !== current)) return;
    history.replaceState(null, "", location.pathname); // 한 번만
    const row = modelsView.models.find((r) => r.id === h.model);
    openEntryDialog(h.model, row ? row.entry : {});
  }
  // 같은 문서에서 해시만 바뀌어도(어드민 탭이 이미 열려 있을 때) 연다.
  window.addEventListener("hashchange", () => {
    const h = hashParams();
    if (h.server && h.server !== current) selectServer(h.server);
    else openModelFromHash();
  });

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
    // 🎲 (v1.39.0): 빈 칸 = 키를 적지 않음(서버 기본). 범위는 서버가 다시 보지만 여기서 먼저 멈춘다.
    for (const [id, key, lo, hi] of [["ef-temperature", "temperature", 0, 2], ["ef-top-p", "top_p", 0, 1]]) {
      const raw = $(id).value.trim();
      if (!raw) continue;
      const num = Number(raw);
      if (!Number.isFinite(num) || num < lo || num > hi) {
        status($("entry-status"), key + " must be a number between " + lo + " and " + hi, false);
        $(id).focus();
        return;
      }
      entry[key] = num;
    }
    try {
      await api("PUT", srvUrl(current) + "/models/" + enc(dlgModelId), entry);
      $("entry-dlg").close();
      refreshModels();
    } catch (e) {
      status($("entry-status"), "Save failed: " + e.message, false);
    }
  });

  loadServers(hashParams().server);
})();
