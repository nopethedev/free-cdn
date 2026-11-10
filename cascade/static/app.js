/* Cascade panel — no framework, no build step. */
(() => {
  "use strict";

  const C = window.CASCADE || {};
  const csrf = C.csrf || "";
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function escapeHtml(value) {
    const div = document.createElement("div");
    div.textContent = value === null || value === undefined ? "" : String(value);
    return div.innerHTML;
  }

  function fmtBytes(num) {
    const value = Number(num);
    if (!Number.isFinite(value) || value <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let index = 0;
    let amount = value;
    while (amount >= 1024 && index < units.length - 1) {
      amount /= 1024;
      index += 1;
    }
    return `${index === 0 ? Math.round(amount) : amount.toFixed(1)} ${units[index]}`;
  }

  function toast(message, kind = "ok") {
    const host = $("#toasts");
    if (!host) return;
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    const icon = kind === "bad" ? "i-x" : kind === "info" ? "i-activity" : "i-check";
    el.innerHTML = `<svg class="icon"><use href="#${icon}"/></svg><span>${escapeHtml(message)}</span>`;
    host.appendChild(el);
    setTimeout(() => {
      el.classList.add("out");
      setTimeout(() => el.remove(), 220);
    }, kind === "bad" ? 6000 : 3800);
  }

  async function api(path, options = {}) {
    const opts = {
      method: options.method || "GET",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    };
    if (options.json !== undefined) {
      opts.method = options.method || "POST";
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(options.json);
    }
    if (options.body !== undefined) opts.body = options.body;
    if (!["GET", "HEAD"].includes(opts.method)) {
      opts.headers["X-CSRF-Token"] = csrf;
    }
    const response = await fetch(path, opts);
    const type = response.headers.get("content-type") || "";
    let data = null;
    if (type.includes("application/json")) {
      data = await response.json().catch(() => null);
    } else {
      data = await response.text().catch(() => "");
    }
    if (response.status === 401) {
      window.location.href = `/login?next=${encodeURIComponent(location.pathname)}`;
      throw new Error("Please sign in again.");
    }
    if (!response.ok) {
      const message =
        (data && (data.detail || data.error)) ||
        (typeof data === "string" && data.trim()) ||
        `Request failed (HTTP ${response.status})`;
      throw new Error(message);
    }
    return data;
  }

  async function copyText(text, label = "Copied") {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        const area = document.createElement("textarea");
        area.value = text;
        area.style.position = "fixed";
        area.style.opacity = "0";
        document.body.appendChild(area);
        area.select();
        document.execCommand("copy");
        area.remove();
      }
      toast(`${label}: ${text.length > 64 ? text.slice(0, 61) + "…" : text}`);
    } catch (err) {
      toast("Could not copy to clipboard", "bad");
    }
  }

  async function busy(button, task) {
    if (!button) return task();
    const original = button.innerHTML;
    button.disabled = true;
    button.classList.add("is-busy");
    try {
      return await task();
    } finally {
      button.disabled = false;
      button.innerHTML = original;
      button.classList.remove("is-busy");
    }
  }

  /* Modals ----------------------------------------------------------------- */

  let activeModal = null;

  function closeModal() {
    if (activeModal) {
      activeModal.remove();
      activeModal = null;
    }
    const backdrop = $("#modal-backdrop");
    if (backdrop) backdrop.hidden = true;
  }

  function openModal({ title, subtitle = "", body = "", actions = [], wide = false }) {
    closeModal();
    const wrap = document.createElement("div");
    wrap.className = "modal";
    wrap.innerHTML = `
      <div class="modal-card ${wide ? "wide" : ""}" role="dialog" aria-modal="true" aria-label="${escapeHtml(title)}">
        <div class="modal-head">
          <div><h2>${escapeHtml(title)}</h2>${subtitle ? `<p>${escapeHtml(subtitle)}</p>` : ""}</div>
          <button class="icon-btn" type="button" data-modal-close aria-label="Close">
            <svg class="icon"><use href="#i-x"/></svg>
          </button>
        </div>
        <div class="modal-body"></div>
        <div class="modal-foot"></div>
      </div>`;
    const bodyEl = $(".modal-body", wrap);
    bodyEl.innerHTML = body;
    const footEl = $(".modal-foot", wrap);
    actions.forEach((action) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `btn ${action.className || ""}`.trim();
      button.textContent = action.label;
      button.addEventListener("click", () => action.onClick && action.onClick({ modal: wrap, body: bodyEl, foot: footEl, close: closeModal }));
      footEl.appendChild(button);
    });
    if (!actions.length) footEl.remove();
    $("#modal-host").appendChild(wrap);
    $("#modal-backdrop").hidden = false;
    activeModal = wrap;
    $("[data-modal-close]", wrap).addEventListener("click", closeModal);
    const first = $("input:not([type=hidden]), select, textarea", bodyEl);
    if (first) setTimeout(() => first.focus(), 50);
    return { root: wrap, body: bodyEl, foot: footEl, close: closeModal };
  }

  function confirmDialog(title, message, confirmLabel = "Delete") {
    return new Promise((resolve) => {
      const modal = openModal({
        title,
        body: `<p class="muted">${escapeHtml(message)}</p>`,
        actions: [
          { label: "Cancel", className: "btn-ghost", onClick: () => { modal.close(); resolve(false); } },
          { label: confirmLabel, className: "btn-danger", onClick: () => { modal.close(); resolve(true); } },
        ],
      });
    });
  }

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeModal();
  });
  const backdrop = $("#modal-backdrop");
  if (backdrop) backdrop.addEventListener("click", closeModal);

  /* Global click delegation ------------------------------------------------ */

  document.addEventListener("click", (event) => {
    const copyButton = event.target.closest("[data-copy]");
    if (copyButton) {
      event.preventDefault();
      copyText(copyButton.dataset.copy || "");
      return;
    }
    if (event.target.closest("#btn-new-link, [data-open-new-link]")) {
      event.preventDefault();
      openLinkCreator();
    }
  });

  /* Files ------------------------------------------------------------------ */

  const F = {
    node: C.initialNode || null,
    path: "/",
    caps: [],
    items: [],
    nodeName: "",
    loading: false,
  };

  function fileIcon(item) {
    return item.is_dir ? "i-folder" : "i-file";
  }

  function renderBreadcrumb() {
    const nav = $("#breadcrumb");
    if (!nav) return;
    const parts = F.path.split("/").filter(Boolean);
    const chunks = [
      `<button type="button" data-crumb="/">${escapeHtml(F.nodeName || "root")}</button>`,
    ];
    let built = "";
    parts.forEach((part) => {
      built += `/${part}`;
      chunks.push(`<span class="sep">/</span>`);
      chunks.push(`<button type="button" data-crumb="${escapeHtml(built)}">${escapeHtml(part)}</button>`);
    });
    nav.innerHTML = chunks.join("");
    $$("[data-crumb]", nav).forEach((button) => {
      button.addEventListener("click", () => navigate(button.dataset.crumb));
    });
  }

  function renderFiles() {
    const rows = $("#file-rows");
    if (!rows) return;
    const count = $("#file-count");
    if (count) count.textContent = `${F.items.length} item${F.items.length === 1 ? "" : "s"}`;
    if (!F.items.length) {
      rows.innerHTML = `<tr><td colspan="4" class="empty-row">This folder is empty.</td></tr>`;
      return;
    }
    rows.innerHTML = F.items
      .map((item) => {
        const name = escapeHtml(item.name);
        const path = escapeHtml(item.path);
        const actions = [];
        actions.push(`<button class="btn btn-ghost btn-sm" data-act="${item.is_dir ? "open" : "download"}" data-path="${path}" data-name="${name}" data-size="${item.size || 0}" data-mime="${escapeHtml(item.mime || "")}" title="${item.is_dir ? "Open folder" : "Download"}"><svg class="icon"><use href="#i-${item.is_dir ? "folder" : "download"}"/></svg></button>`);
        if (!item.is_dir) {
          actions.push(`<button class="btn btn-ghost btn-sm" data-act="link" data-path="${path}" data-name="${name}" data-size="${item.size || 0}" data-mime="${escapeHtml(item.mime || "")}" title="Create a link"><svg class="icon"><use href="#i-link"/></svg></button>`);
          if (C.canReplicate) {
            actions.push(`<button class="btn btn-ghost btn-sm" data-act="replicate" data-path="${path}" data-name="${name}" title="Replicate"><svg class="icon"><use href="#i-activity"/></svg></button>`);
          }
        }
        if (C.canWrite) {
          actions.push(`<button class="btn btn-ghost btn-sm" data-act="rename" data-path="${path}" data-name="${name}" title="Rename"><svg class="icon"><use href="#i-edit"/></svg></button>`);
          actions.push(`<button class="btn btn-ghost btn-sm btn-danger-ghost" data-act="delete" data-path="${path}" data-name="${name}" data-dir="${item.is_dir ? 1 : 0}" title="Delete"><svg class="icon"><use href="#i-trash"/></svg></button>`);
        }
        return `<tr>
          <td class="cell-main">
            <button type="button" class="file-name" data-act="${item.is_dir ? "open" : "link"}" data-path="${path}" data-name="${name}" data-size="${item.size || 0}" data-mime="${escapeHtml(item.mime || "")}">
              <span class="file-icon ${item.is_dir ? "dir" : ""}"><svg class="icon"><use href="#${fileIcon(item)}"/></svg></span>
              <span class="file-meta"><strong>${name}</strong><small>${item.is_dir ? "folder" : escapeHtml(item.mime || "file")}</small></span>
            </button>
          </td>
          <td class="mono">${item.is_dir ? "—" : fmtBytes(item.size)}</td>
          <td class="muted">${item.modified ? new Date(item.modified * 1000).toLocaleString() : "—"}</td>
          <td class="cell-actions"><div class="row-actions">${actions.join("")}</div></td>
        </tr>`;
      })
      .join("");
  }

  async function loadFiles() {
    if (!F.node) return;
    const rows = $("#file-rows");
    if (rows) rows.innerHTML = `<tr><td colspan="4" class="empty-row">Loading…</td></tr>`;
    F.loading = true;
    try {
      const data = await api(`/api/files?node=${encodeURIComponent(F.node)}&path=${encodeURIComponent(F.path)}`);
      F.path = data.path || "/";
      F.caps = data.capabilities || [];
      F.items = data.items || [];
      F.nodeName = (data.node && data.node.name) || "";
      renderFiles();
      renderBreadcrumb();
    } catch (err) {
      if (rows) rows.innerHTML = `<tr><td colspan="4" class="empty-row">${escapeHtml(err.message)}</td></tr>`;
    } finally {
      F.loading = false;
    }
  }

  function navigate(path) {
    F.path = path || "/";
    loadFiles();
  }

  function openLinkModalForFile(item) {
    const body = `
      <div class="field"><span>Default delivery</span>
        <select class="select" name="mode">
          <option value="stream">Stream through this server (/s/)</option>
          <option value="forward">Forward to the source (/f/)</option>
        </select>
      </div>
      <div class="field-row">
        <label class="field"><span>Expires in (hours)</span><input class="input" name="expires_hours" type="number" min="0" placeholder="never"></label>
        <label class="field"><span>Max downloads</span><input class="input" name="max_downloads" type="number" min="0" placeholder="unlimited"></label>
      </div>
      <label class="field"><span>Password (optional)</span><input class="input" name="password" type="password" autocomplete="new-password" placeholder="Leave empty for a public link"></label>
      <label class="check"><input type="checkbox" name="landing"> Show a landing page instead of redirecting</label>
      <p class="muted tiny">Both /s/ and /f/ are generated for every link. Forwarding a local file falls back to streaming.</p>`;
    const modal = openModal({
      title: `Share “${item.name}”`,
      subtitle: F.nodeName ? `On ${F.nodeName}` : "",
      body,
      actions: [
        { label: "Cancel", className: "btn-ghost", onClick: () => modal.close() },
        {
          label: "Create link",
          className: "btn-primary",
          onClick: async ({ body: bodyEl, foot }) => {
            const button = foot.querySelector(".btn-primary");
            await busy(button, async () => {
              try {
                const data = await api("/api/links", {
                  method: "POST",
                  json: {
                    node_id: Number(F.node),
                    path: item.path,
                    name: item.name,
                    size: item.size || 0,
                    mime: item.mime || "",
                    mode: bodyEl.querySelector("[name=mode]").value,
                    expires_hours: bodyEl.querySelector("[name=expires_hours]").value || 0,
                    max_downloads: bodyEl.querySelector("[name=max_downloads]").value || 0,
                    password: bodyEl.querySelector("[name=password]").value || null,
                    landing: bodyEl.querySelector("[name=landing]").checked,
                  },
                });
                modal.body.innerHTML = linkResultHtml(data.link);
                modal.foot.innerHTML = "";
                const done = document.createElement("button");
                done.className = "btn btn-primary";
                done.textContent = "Done";
                done.addEventListener("click", () => modal.close());
                modal.foot.appendChild(done);
                toast("Link created");
              } catch (err) {
                toast(err.message, "bad");
              }
            });
          },
        },
      ],
    });
  }

  function linkResultHtml(link) {
    const urls = link.urls || {};
    const row = (label, url) => `
      <div class="url-row">
        <div><span class="url-label">${label}</span><code class="mono">${escapeHtml(url)}</code></div>
        <button class="btn btn-sm" type="button" data-copy="${escapeHtml(url)}"><svg class="icon"><use href="#i-copy"/></svg>Copy</button>
      </div>`;
    return `
      <div class="alert alert-good">Link <span class="mono">/${escapeHtml(link.id)}</span> is live.</div>
      <div class="url-list">
        ${row("Short link", urls.short)}
        ${row("Stream proxy", urls.stream)}
        ${row("Forward", urls.forward)}
      </div>
      ${link.password ? `<p class="muted tiny">This link asks for a password.</p>` : ""}`;
  }

  async function uploadFiles(fileList) {
    if (!fileList || !fileList.length || !F.node) return;
    if (!C.canWrite) return toast("Only administrators can upload", "bad");
    const status = $("#upload-status");
    const fd = new FormData();
    fd.append("node", F.node);
    fd.append("path", F.path);
    Array.from(fileList).forEach((file) => fd.append("files", file));
    if (status) {
      status.hidden = false;
      status.innerHTML = `<span>Uploading ${fileList.length} file(s)…</span><div class="progress"><span style="width:0%"></span></div>`;
    }
    const bar = status ? status.querySelector(".progress span") : null;
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/files/upload");
    xhr.setRequestHeader("X-CSRF-Token", csrf);
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable && bar) bar.style.width = `${Math.round((event.loaded / event.total) * 100)}%`;
    });
    xhr.addEventListener("load", () => {
      if (status) status.hidden = true;
      try {
        const data = JSON.parse(xhr.responseText || "{}");
        if (xhr.status >= 400) throw new Error(data.detail || "upload failed");
        toast(`Uploaded ${data.count} file(s)`);
        loadFiles();
      } catch (err) {
        toast(err.message, "bad");
      }
    });
    xhr.addEventListener("error", () => {
      if (status) status.hidden = true;
      toast("Upload failed", "bad");
    });
    xhr.send(fd);
  }

  function openMkdirModal() {
    const modal = openModal({
      title: "New folder",
      body: `<label class="field"><span>Folder name</span><input class="input" name="name" placeholder="media" required></label>`,
      actions: [
        { label: "Cancel", className: "btn-ghost", onClick: () => modal.close() },
        {
          label: "Create folder",
          className: "btn-primary",
          onClick: async ({ body, foot }) => {
            const name = body.querySelector("[name=name]").value.trim();
            if (!name) return toast("Enter a folder name", "bad");
            await busy(foot.querySelector(".btn-primary"), async () => {
              try {
                await api("/api/files/mkdir", { method: "POST", json: { node: Number(F.node), path: F.path, name } });
                modal.close();
                toast("Folder created");
                loadFiles();
              } catch (err) {
                toast(err.message, "bad");
              }
            });
          },
        },
      ],
    });
  }

  function openRenameModal(item) {
    const modal = openModal({
      title: `Rename “${item.name}”`,
      body: `<label class="field"><span>New name</span><input class="input" name="name" value="${escapeHtml(item.name)}" required></label>`,
      actions: [
        { label: "Cancel", className: "btn-ghost", onClick: () => modal.close() },
        {
          label: "Rename",
          className: "btn-primary",
          onClick: async ({ body, foot }) => {
            const newName = body.querySelector("[name=name]").value.trim();
            if (!newName) return toast("Enter a name", "bad");
            await busy(foot.querySelector(".btn-primary"), async () => {
              try {
                await api("/api/files/rename", { method: "POST", json: { node: Number(F.node), path: item.path, new_name: newName } });
                modal.close();
                toast("Renamed");
                loadFiles();
              } catch (err) {
                toast(err.message, "bad");
              }
            });
          },
        },
      ],
    });
  }

  async function deleteFile(item) {
    const ok = await confirmDialog("Delete file", `“${item.name}” will be removed from ${F.nodeName || "the node"}. This cannot be undone.`);
    if (!ok) return;
    try {
      await api("/api/files/delete", { method: "POST", json: { node: Number(F.node), path: item.path } });
      toast("Deleted");
      loadFiles();
    } catch (err) {
      toast(err.message, "bad");
    }
  }

  function openReplicateModal(item) {
    const targets = (C.nodes || []).filter(
      (node) => node.id !== Number(F.node) && (node.caps || []).includes("write")
    );
    if (!targets.length) {
      return toast("No writable target nodes connected", "bad");
    }
    const body = `
      <p class="muted">Copy <strong>${escapeHtml(item.name)}</strong> to another node. The transfer runs in the background.</p>
      <label class="field"><span>Target node</span>
        <select class="select" name="target">
          ${targets.map((node) => `<option value="${node.id}">${escapeHtml(node.name)} · ${escapeHtml(node.kind)}</option>`).join("")}
        </select>
      </label>
      <label class="field"><span>Destination path (including file name)</span>
        <input class="input mono" name="path" value="/${escapeHtml(item.name)}">
      </label>
      <p class="muted tiny">Existing files with the same name are overwritten.</p>`;
    const modal = openModal({
      title: "Replicate file",
      subtitle: F.nodeName ? `From ${F.nodeName}` : "",
      body,
      actions: [
        { label: "Cancel", className: "btn-ghost", onClick: () => modal.close() },
        {
          label: "Start copy",
          className: "btn-primary",
          onClick: async ({ body: bodyEl, foot }) => {
            await busy(foot.querySelector(".btn-primary"), async () => {
              try {
                const data = await api("/api/files/replicate", {
                  method: "POST",
                  json: {
                    node: Number(F.node),
                    path: item.path,
                    targets: [
                      {
                        node: Number(bodyEl.querySelector("[name=target]").value),
                        path: bodyEl.querySelector("[name=path]").value.trim() || `/${item.name}`,
                      },
                    ],
                  },
                });
                modal.close();
                watchJob(data.job_id, `Copying ${item.name}`);
              } catch (err) {
                toast(err.message, "bad");
              }
            });
          },
        },
      ],
    });
  }

  async function watchJob(jobId, label) {
    const status = $("#upload-status");
    if (status) status.hidden = false;
    for (let attempt = 0; attempt < 900; attempt += 1) {
      try {
        const { job } = await api(`/api/jobs/${jobId}`);
        const percent = job.total ? Math.min(100, Math.round((job.done / job.total) * 100)) : 0;
        if (status) {
          status.innerHTML = `<span>${escapeHtml(label)} · ${fmtBytes(job.done)}${job.total ? ` / ${fmtBytes(job.total)}` : ""} · ${job.status}</span><div class="progress"><span style="width:${percent}%"></span></div>`;
        }
        if (job.status === "done" || job.status === "error") {
          toast(job.status === "done" ? job.message || "Copy finished" : job.message || "Copy failed", job.status === "done" ? "ok" : "bad");
          setTimeout(() => { if (status) status.hidden = true; }, 4000);
          loadFiles();
          return job;
        }
      } catch (err) {
        if (status) status.hidden = true;
        toast(err.message, "bad");
        return null;
      }
      await sleep(1200);
    }
    return null;
  }

  function initFiles() {
    if (!$("#file-rows")) return;
    const select = $("#node-select");
    if (select) {
      select.addEventListener("change", () => {
        F.node = select.value;
        F.path = "/";
        loadFiles();
      });
    }
    const up = $("#btn-up");
    if (up) {
      up.addEventListener("click", () => {
        const parts = F.path.split("/").filter(Boolean);
        parts.pop();
        navigate(parts.length ? `/${parts.join("/")}` : "/");
      });
    }
    const refresh = $("#btn-refresh");
    if (refresh) refresh.addEventListener("click", loadFiles);
    const mkdir = $("#btn-mkdir");
    if (mkdir) mkdir.addEventListener("click", openMkdirModal);
    const upload = $("#btn-upload");
    const input = $("#file-input");
    if (upload && input) {
      upload.addEventListener("click", () => input.click());
      input.addEventListener("change", () => {
        uploadFiles(input.files);
        input.value = "";
      });
    }
    const dropzone = $("#dropzone");
    if (dropzone) {
      dropzone.addEventListener("click", () => input && input.click());
      ["dragenter", "dragover"].forEach((name) =>
        dropzone.addEventListener(name, (event) => {
          event.preventDefault();
          dropzone.classList.add("is-over");
        })
      );
      ["dragleave", "drop"].forEach((name) =>
        dropzone.addEventListener(name, (event) => {
          event.preventDefault();
          dropzone.classList.remove("is-over");
        })
      );
      dropzone.addEventListener("drop", (event) => {
        if (event.dataTransfer && event.dataTransfer.files) uploadFiles(event.dataTransfer.files);
      });
    }
    const rows = $("#file-rows");
    rows.addEventListener("click", (event) => {
      const button = event.target.closest("[data-act]");
      if (!button) return;
      const item = {
        path: button.dataset.path,
        name: button.dataset.name,
        size: Number(button.dataset.size || 0),
        mime: button.dataset.mime || "",
        is_dir: button.closest("tr")?.querySelector(".file-icon.dir") !== null,
      };
      const action = button.dataset.act;
      if (action === "open") navigate(item.path);
      else if (action === "download") window.location.href = `/api/files/raw?node=${encodeURIComponent(F.node)}&path=${encodeURIComponent(item.path)}`;
      else if (action === "link") openLinkModalForFile(item);
      else if (action === "rename") openRenameModal(item);
      else if (action === "replicate") openReplicateModal(item);
      else if (action === "delete") deleteFile(item);
    });
    loadFiles();
  }

  /* Links ------------------------------------------------------------------ */

  function openLinkCreator() {
    const nodes = C.nodes || [];
    const nodeOptions = nodes
      .map((node) => `<option value="${node.id}">${escapeHtml(node.name)} · ${escapeHtml(node.kind)}</option>`)
      .join("");
    const body = `
      <label class="field"><span>Source</span>
        <select class="select" name="source">
          <option value="url">Paste a share URL</option>
          ${nodeOptions ? '<option value="node">File on a connected node</option>' : ""}
        </select>
      </label>
      <div data-panel="url">
        <label class="field"><span>URL</span><input class="input" name="url" type="url" placeholder="https://www.dropbox.com/s/…"></label>
      </div>
      <div data-panel="node" hidden>
        <label class="field"><span>Node</span><select class="select" name="node_id">${nodeOptions}</select></label>
        <label class="field"><span>Path</span><input class="input mono" name="path" placeholder="/media/movie.mp4"></label>
      </div>
      <label class="field"><span>Display name</span><input class="input" name="name" placeholder="Optional, defaults to the file name"></label>
      <div class="field-row">
        <label class="field"><span>Default delivery</span>
          <select class="select" name="mode">
            <option value="stream" ${C.defaultMode === "stream" ? "selected" : ""}>Stream (/s/)</option>
            <option value="forward" ${C.defaultMode === "forward" ? "selected" : ""}>Forward (/f/)</option>
          </select>
        </label>
        <label class="field"><span>Expires in (hours)</span><input class="input" name="expires_hours" type="number" min="0" placeholder="never"></label>
      </div>
      <div class="field-row">
        <label class="field"><span>Max downloads</span><input class="input" name="max_downloads" type="number" min="0" placeholder="unlimited"></label>
        <label class="field"><span>Password</span><input class="input" name="password" type="password" placeholder="optional"></label>
      </div>
      <label class="check"><input type="checkbox" name="landing" ${C.landingDefault ? "checked" : ""}> Show a landing page</label>`;
    const modal = openModal({
      title: "New link",
      subtitle: "Route a file or share URL through a short id.",
      body,
      actions: [
        { label: "Cancel", className: "btn-ghost", onClick: () => modal.close() },
        {
          label: "Create link",
          className: "btn-primary",
          onClick: async ({ body: bodyEl, foot }) => {
            const source = bodyEl.querySelector("[name=source]").value;
            const payload = {
              name: bodyEl.querySelector("[name=name]").value.trim() || null,
              mode: bodyEl.querySelector("[name=mode]").value,
              expires_hours: bodyEl.querySelector("[name=expires_hours]").value || 0,
              max_downloads: bodyEl.querySelector("[name=max_downloads]").value || 0,
              password: bodyEl.querySelector("[name=password]").value || null,
              landing: bodyEl.querySelector("[name=landing]").checked,
            };
            if (source === "url") {
              payload.url = bodyEl.querySelector("[name=url]").value.trim();
              if (!payload.url) return toast("Paste a URL", "bad");
            } else {
              payload.node_id = Number(bodyEl.querySelector("[name=node_id]").value);
              payload.path = bodyEl.querySelector("[name=path]").value.trim();
              if (!payload.path) return toast("Enter a file path", "bad");
            }
            await busy(foot.querySelector(".btn-primary"), async () => {
              try {
                const data = await api("/api/links", { method: "POST", json: payload });
                modal.body.innerHTML = linkResultHtml(data.link);
                modal.foot.innerHTML = "";
                const done = document.createElement("button");
                done.className = "btn btn-primary";
                done.textContent = "Done";
                done.addEventListener("click", () => { modal.close(); location.reload(); });
                modal.foot.appendChild(done);
                toast("Link created");
              } catch (err) {
                toast(err.message, "bad");
              }
            });
          },
        },
      ],
    });
    const sourceSelect = modal.body.querySelector("[name=source]");
    sourceSelect.addEventListener("change", () => {
      $$("[data-panel]", modal.body).forEach((panel) => {
        panel.hidden = panel.dataset.panel !== sourceSelect.value;
      });
    });
  }

  async function deleteLink(id) {
    const ok = await confirmDialog("Delete link", "The short id stops working immediately. Files are not touched.");
    if (!ok) return;
    try {
      await api(`/api/links/${encodeURIComponent(id)}`, { method: "DELETE" });
      toast("Link deleted");
      setTimeout(() => {
        if (location.pathname.startsWith("/admin/links/")) location.href = "/admin/links";
        else location.reload();
      }, 300);
    } catch (err) {
      toast(err.message, "bad");
    }
  }

  async function toggleLink(id, current, button) {
    try {
      await api(`/api/links/${encodeURIComponent(id)}`, { method: "PATCH", json: { enabled: !current } });
      toast(current ? "Link disabled" : "Link enabled");
      setTimeout(() => location.reload(), 300);
    } catch (err) {
      toast(err.message, "bad");
      if (button) button.disabled = false;
    }
  }

  function initLinks() {
    if (!$("#links-table") && !$("#btn-new-link") && !$("#link-settings-form")) return;
    const search = $("#link-search");
    if (search) {
      search.addEventListener("input", () => {
        const term = search.value.trim().toLowerCase();
        let visible = 0;
        $$("[data-link-row]", document).forEach((row) => {
          const match = !term || (row.dataset.search || "").includes(term);
          row.hidden = !match;
          if (match) visible += 1;
        });
        const count = $("#link-count");
        if (count) count.textContent = `${visible} link${visible === 1 ? "" : "s"}`;
      });
    }
    document.addEventListener("click", (event) => {
      const toggle = event.target.closest("[data-link-toggle]");
      if (toggle) {
        toggleLink(toggle.dataset.linkToggle, toggle.dataset.linkEnabled === "1", toggle);
        return;
      }
      const remove = event.target.closest("[data-link-delete]");
      if (remove) {
        deleteLink(remove.dataset.linkDelete);
      }
    });

    const form = $("#link-settings-form");
    if (form) {
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const submit = form.querySelector("[type=submit]");
        const expires = form.querySelector("[name=expires_hours]");
        await busy(submit, async () => {
          try {
            const payload = {
              name: form.querySelector("[name=name]").value,
              mode: form.querySelector("[name=mode]").value,
              allow_stream: form.querySelector("[name=allow_stream]").checked,
              allow_forward: form.querySelector("[name=allow_forward]").checked,
              landing: form.querySelector("[name=landing]").checked,
              enabled: form.querySelector("[name=enabled]").checked,
              expires_hours: expires && expires.value !== "" ? Number(expires.value) : 0,
              max_downloads: form.querySelector("[name=max_downloads]").value || 0,
              note: form.querySelector("[name=note]").value,
            };
            const password = form.querySelector("[name=password]").value;
            if (password) payload.password = password;
            const clear = form.querySelector("[name=clear_password]");
            if (clear && clear.checked) payload.clear_password = true;
            await api(`/api/links/${encodeURIComponent(form.dataset.linkId)}`, { method: "PATCH", json: payload });
            toast("Link updated");
            setTimeout(() => location.reload(), 400);
          } catch (err) {
            toast(err.message, "bad");
          }
        });
      });
    }
  }

  /* Nodes ------------------------------------------------------------------ */

  const SECRET_KEYS = ["access_token", "password", "cookie", "token", "secret_key", "session_token"];

  function nodeKindMeta(kind) {
    return (C.kinds || []).find((item) => item.kind === kind) || { fields: [] };
  }

  function nodeTypeOptions() {
    const groups = [];
    (C.kinds || []).forEach((kind) => {
      const name = kind.group || "Other";
      let group = groups.find((item) => item.name === name);
      if (!group) {
        group = { name, kinds: [] };
        groups.push(group);
      }
      group.kinds.push(kind);
    });
    return groups
      .map(
        (group) =>
          `<optgroup label="${escapeHtml(group.name)}">${group.kinds
            .map((kind) => `<option value="${escapeHtml(kind.kind)}">${escapeHtml(kind.label)}</option>`)
            .join("")}</optgroup>`
      )
      .join("");
  }

  function nodeFieldsHtml(kind, config = {}, secrets = {}) {
    const meta = nodeKindMeta(kind);
    return (meta.fields || [])
      .map((field) => {
        const key = field.key;
        const isSecret = SECRET_KEYS.includes(key);
        const value =
          config[key] === undefined || config[key] === null
            ? field.default || ""
            : config[key];
        const placeholder = isSecret && secrets[key]
          ? "configured — leave blank to keep"
          : field.placeholder || "";
        const help = field.help ? `<small>${escapeHtml(field.help)}</small>` : "";
        const required = field.required ? " required" : "";
        const label = `${escapeHtml(field.label)}${field.required ? ' <em class="req">*</em>' : ' <em class="opt">optional</em>'}`;
        if (field.type === "textarea") {
          return `<label class="field"><span>${label}</span>
            <textarea class="input" name="cfg_${key}" placeholder="${escapeHtml(placeholder)}">${escapeHtml(
              typeof value === "object" ? JSON.stringify(value, null, 2) : value
            )}</textarea>${help}</label>`;
        }
        if (field.type === "select") {
          const options = (field.options || [])
            .map((option) => {
              const selected = String(value) === String(option.value) ? " selected" : "";
              return `<option value="${escapeHtml(option.value)}"${selected}>${escapeHtml(option.label)}</option>`;
            })
            .join("");
          return `<label class="field"><span>${label}</span>
            <select class="select" name="cfg_${key}"${required}>${options}</select>${help}</label>`;
        }
        return `<label class="field"><span>${label}</span>
          <input class="input" type="${field.type || "text"}" name="cfg_${key}" value="${escapeHtml(
            typeof value === "object" ? JSON.stringify(value) : value
          )}" placeholder="${escapeHtml(placeholder)}"${required}>${help}</label>`;
      })
      .join("");
  }

  function openNodeModal(node) {
    const editing = Boolean(node);
    const kinds = C.kinds || [];
    if (!kinds.length) return toast("Node types unavailable", "bad");
    const initialKind = editing ? node.kind : kinds[0].kind;
    const initialMeta = nodeKindMeta(initialKind);
    const body = `
      <div class="alert alert-bad" id="node-form-error" hidden></div>
      <label class="field"><span>Name <em class="req">*</em></span><input class="input" name="name" value="${escapeHtml(
        node ? node.name : ""
      )}" placeholder="Main drive" required><small>Required. A label only you see.</small></label>
      ${
        editing
          ? ""
          : `<label class="field"><span>Provider <em class="req">*</em></span><select class="select" name="kind">${nodeTypeOptions()}</select></label>`
      }
      <p class="muted tiny" id="node-kind-help">${escapeHtml(initialMeta.blurb || "")}</p>
      <div class="node-fields form-stack" id="node-fields">${nodeFieldsHtml(
        initialKind,
        node ? node.config : {},
        node ? node.secrets : {}
      )}</div>
      <p class="muted tiny">${
        editing
          ? "Secrets are never sent back to the browser. Leave a secret blank to keep the saved value."
          : "Fields marked * are required. Leave the optional ones blank; credentials are stored in the local SQLite database."
      }</p>`;
    const modal = openModal({
      title: editing ? `Edit ${node.name}` : "Add a node",
      subtitle: editing ? "Change credentials or endpoints" : "Pick a provider, give it a name, then test it",
      body,
      actions: [
        { label: "Cancel", className: "btn-ghost", onClick: () => modal.close() },
        {
          label: editing ? "Save node" : "Add node",
          className: "btn-primary",
          onClick: async ({ body: bodyEl, foot }) => {
            const errorEl = bodyEl.querySelector("#node-form-error");
            const showError = (message) => {
              if (errorEl) {
                errorEl.textContent = message;
                errorEl.hidden = false;
              }
              toast(message, "bad");
            };
            const nameInput = bodyEl.querySelector("[name=name]");
            const kindSelect = bodyEl.querySelector("[name=kind]");
            const kind = editing ? node.kind : kindSelect.value;
            const meta = nodeKindMeta(kind);
            const name = nameInput.value.trim();
            const config = {};
            $$("[name^=cfg_]", bodyEl).forEach((input) => {
              const key = input.name.slice(4);
              const value = input.value;
              if (value === "" && SECRET_KEYS.includes(key)) return;
              config[key] = value;
            });
            const missing = [];
            let firstMissing = null;
            if (!name) {
              missing.push("Name");
              firstMissing = nameInput;
            }
            (meta.fields || []).forEach((field) => {
              if (!field.required) return;
              const input = bodyEl.querySelector(`[name="cfg_${field.key}"]`);
              if (!input || !String(input.value || "").trim()) {
                missing.push(field.label);
                if (!firstMissing) firstMissing = input;
              }
            });
            if (missing.length) {
              showError(`Fill in: ${missing.join(", ")}.`);
              if (firstMissing) firstMissing.focus();
              return;
            }
            const payload = { name };
            if (editing) payload.config = config;
            else {
              payload.kind = kind;
              payload.config = config;
            }
            await busy(foot.querySelector(".btn-primary"), async () => {
              try {
                if (editing) {
                  await api(`/api/nodes/${node.id}`, { method: "PATCH", json: payload });
                } else {
                  await api("/api/nodes", { method: "POST", json: payload });
                }
                toast(editing ? "Node saved" : "Node added");
                setTimeout(() => location.reload(), 350);
              } catch (err) {
                showError(err.message);
              }
            });
          },
        },
      ],
    });
    const errorEl = modal.body.querySelector("#node-form-error");
    modal.body.addEventListener("input", () => {
      if (errorEl) errorEl.hidden = true;
    });
    const kindSelect = modal.body.querySelector("[name=kind]");
    if (kindSelect) {
      kindSelect.addEventListener("change", () => {
        const meta = nodeKindMeta(kindSelect.value);
        const help = $("#node-kind-help", modal.body);
        if (help) help.textContent = meta.blurb || "";
        $("#node-fields", modal.body).innerHTML = nodeFieldsHtml(kindSelect.value);
        if (errorEl) errorEl.hidden = true;
      });
    }
  }

  function initNodes() {
    if (!$(".node-grid") && !$("#btn-add-node")) return;
    const addButton = $("#btn-add-node") || $("[data-add-node]");
    if (addButton) addButton.addEventListener("click", () => openNodeModal(null));
    document.addEventListener("click", (event) => {
      if (event.target.closest("[data-add-node]")) openNodeModal(null);
    });
    document.addEventListener("click", async (event) => {
      const test = event.target.closest("[data-node-test]");
      if (test) {
        await busy(test, async () => {
          try {
            const result = await api(`/api/nodes/${test.dataset.nodeTest}/test`, { method: "POST" });
            const card = document.querySelector(`[data-node-card="${test.dataset.nodeTest}"]`);
            if (card) {
              const pill = card.querySelector(".status-pill");
              if (pill) {
                pill.className = `status-pill status-${result.status}`;
                pill.innerHTML = `<span class="status-dot"></span>${result.status}`;
              }
              const message = card.querySelector(".node-msg");
              if (message) message.textContent = result.message || "OK";
            }
            toast(result.ok ? `Online · ${result.latency_ms} ms` : result.message, result.ok ? "ok" : "bad");
          } catch (err) {
            toast(err.message, "bad");
          }
        });
        return;
      }
      const edit = event.target.closest("[data-node-edit]");
      if (edit) {
        const node = (C.nodes || []).find((item) => item.id === Number(edit.dataset.nodeEdit));
        if (node) openNodeModal(node);
        return;
      }
      const remove = event.target.closest("[data-node-delete]");
      if (remove) {
        const ok = await confirmDialog("Delete node", "Links that reference this node will keep their short ids but stop resolving.");
        if (!ok) return;
        try {
          await api(`/api/nodes/${remove.dataset.nodeDelete}`, { method: "DELETE" });
          toast("Node deleted");
          setTimeout(() => location.reload(), 300);
        } catch (err) {
          toast(err.message, "bad");
        }
      }
    });
    document.addEventListener("change", async (event) => {
      const toggle = event.target.closest("[data-node-toggle]");
      if (!toggle) return;
      try {
        await api(`/api/nodes/${toggle.dataset.nodeToggle}`, { method: "PATCH", json: { enabled: toggle.checked } });
        toast(toggle.checked ? "Node enabled" : "Node disabled");
      } catch (err) {
        toggle.checked = !toggle.checked;
        toast(err.message, "bad");
      }
    });
  }

  /* Users ------------------------------------------------------------------ */

  function initUsers() {
    if (!$(".users-table")) return;
    const add = $("#btn-add-user");
    if (add) {
      add.addEventListener("click", () => {
        const modal = openModal({
          title: "Add a person",
          body: `
            <label class="field"><span>Name</span><input class="input" name="name" placeholder="Sam"></label>
            <label class="field"><span>Email</span><input class="input" type="email" name="email" required></label>
            <label class="field"><span>Password</span><input class="input" type="password" name="password" minlength="8" required><small>At least 8 characters.</small></label>
            <label class="field"><span>Role</span><select class="select" name="role"><option value="user">user</option><option value="admin">admin</option></select></label>`,
          actions: [
            { label: "Cancel", className: "btn-ghost", onClick: () => modal.close() },
            {
              label: "Create account",
              className: "btn-primary",
              onClick: async ({ body, foot }) => {
                const payload = {
                  name: body.querySelector("[name=name]").value,
                  email: body.querySelector("[name=email]").value,
                  password: body.querySelector("[name=password]").value,
                  role: body.querySelector("[name=role]").value,
                };
                await busy(foot.querySelector(".btn-primary"), async () => {
                  try {
                    await api("/api/users", { method: "POST", json: payload });
                    toast("Account created");
                    setTimeout(() => location.reload(), 300);
                  } catch (err) {
                    toast(err.message, "bad");
                  }
                });
              },
            },
          ],
        });
      });
    }
    document.addEventListener("change", async (event) => {
      const select = event.target.closest("[data-user-role]");
      if (!select) return;
      try {
        await api(`/api/users/${select.dataset.userRole}`, { method: "PATCH", json: { role: select.value } });
        toast("Role updated");
      } catch (err) {
        toast(err.message, "bad");
        setTimeout(() => location.reload(), 600);
      }
    });
    document.addEventListener("click", async (event) => {
      const disable = event.target.closest("[data-user-disable]");
      const enable = event.target.closest("[data-user-enable]");
      if (disable || enable) {
        const id = (disable || enable).dataset.userDisable || (disable || enable).dataset.userEnable;
        try {
          await api(`/api/users/${id}`, { method: "PATCH", json: { disabled: Boolean(disable) } });
          toast(disable ? "Account disabled" : "Account enabled");
          setTimeout(() => location.reload(), 300);
        } catch (err) {
          toast(err.message, "bad");
        }
        return;
      }
      const password = event.target.closest("[data-user-password]");
      if (password) {
        const value = window.prompt("New password (at least 8 characters):");
        if (!value) return;
        try {
          await api(`/api/users/${password.dataset.userPassword}`, { method: "PATCH", json: { password: value } });
          toast("Password updated");
        } catch (err) {
          toast(err.message, "bad");
        }
        return;
      }
      const remove = event.target.closest("[data-user-delete]");
      if (remove) {
        const ok = await confirmDialog("Delete account", "Their sessions end immediately and their links lose their owner. Links stay live.", "Delete person");
        if (!ok) return;
        try {
          await api(`/api/users/${remove.dataset.userDelete}`, { method: "DELETE" });
          toast("Account deleted");
          setTimeout(() => location.reload(), 300);
        } catch (err) {
          toast(err.message, "bad");
        }
      }
    });
  }

  /* Settings --------------------------------------------------------------- */

  function initSettings() {
    const form = $("#settings-form");
    if (form) {
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const button = form.querySelector("[type=submit]");
        const payload = {};
        $$("input, select, textarea", form).forEach((field) => {
          if (!field.name) return;
          payload[field.name] = field.type === "checkbox" ? (field.checked ? "1" : "0") : field.value;
        });
        await busy(button, async () => {
          try {
            await api("/api/settings", { method: "POST", json: payload });
            const state = $("#settings-state");
            if (state) state.textContent = "Saved just now";
            toast("Settings saved");
          } catch (err) {
            toast(err.message, "bad");
          }
        });
      });
    }
    const createKey = $("#btn-create-key");
    if (createKey) {
      createKey.addEventListener("click", async () => {
        const name = window.prompt("Name this API key:", "Automation") || "API key";
        try {
          const data = await api("/api/keys", { method: "POST", json: { name } });
          const modal = openModal({
            title: "API key created",
            subtitle: "This value is shown once. Store it now.",
            body: `<div class="code-block mono">${escapeHtml(data.key)}</div>
                   <p class="muted tiny">Use it as <span class="mono">Authorization: Bearer …</span> against <span class="mono">POST /api/v1/links</span>.</p>`,
            actions: [
              { label: "Copy key", className: "btn-ghost", onClick: () => copyText(data.key, "API key copied") },
              { label: "Done", className: "btn-primary", onClick: () => { modal.close(); location.reload(); } },
            ],
          });
        } catch (err) {
          toast(err.message, "bad");
        }
      });
    }
    document.addEventListener("click", async (event) => {
      const remove = event.target.closest("[data-key-delete]");
      if (!remove) return;
      const ok = await confirmDialog("Revoke key", "Any script using this key stops working immediately.", "Revoke");
      if (!ok) return;
      try {
        await api(`/api/keys/${remove.dataset.keyDelete}`, { method: "DELETE" });
        toast("Key revoked");
        setTimeout(() => location.reload(), 300);
      } catch (err) {
        toast(err.message, "bad");
      }
    });
  }

  /* Dashboard -------------------------------------------------------------- */

  function initDashboard() {
    const quick = $("#quick-link-form");
    if (quick) {
      quick.addEventListener("submit", async (event) => {
        event.preventDefault();
        const button = quick.querySelector("[type=submit]");
        const url = $("#quick-url").value.trim();
        if (!url) return;
        await busy(button, async () => {
          try {
            const data = await api("/api/links", {
              method: "POST",
              json: { url, mode: $("#quick-mode").value },
            });
            const result = $("#quick-result");
            result.hidden = false;
            result.innerHTML = linkResultHtml(data.link);
          } catch (err) {
            toast(err.message, "bad");
          }
        });
      });
    }
    const jobCards = $$("[data-job]");
    if (!jobCards.length) return;
    let polling = false;
    let reloaded = false;
    async function poll() {
      if (polling) return;
      polling = true;
      let pending = false;
      for (const card of jobCards) {
        const badge = card.querySelector(".badge");
        if (!badge || !/queued|running/.test(badge.textContent)) continue;
        try {
          const { job } = await api(`/api/jobs/${card.dataset.job}`);
          const bar = card.querySelector(".progress span");
          if (bar && job.total) bar.style.width = `${Math.min(100, Math.round((job.done / job.total) * 100))}%`;
          const message = card.querySelector(".job-msg");
          if (message && job.message) message.textContent = job.message;
          badge.textContent = job.status;
          badge.className = `badge badge-${job.status === "done" ? "ok" : job.status === "error" ? "bad" : "warn"}`;
          if (job.status === "done" || job.status === "error") {
            if (!reloaded) {
              reloaded = true;
              toast(job.status === "done" ? "Job finished" : "Job failed", job.status === "done" ? "ok" : "bad");
              setTimeout(() => location.reload(), 900);
            }
          } else {
            pending = true;
          }
        } catch (err) {
          pending = true;
        }
      }
      polling = false;
      if (pending) setTimeout(poll, 3000);
    }
    poll();
  }

  /* Boot ------------------------------------------------------------------- */

  function boot() {
    initFiles();
    initLinks();
    initNodes();
    initUsers();
    initSettings();
    initDashboard();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
