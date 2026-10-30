const bridge = window.AstrBotPluginView;
const serviceStates = new Map();
const installStates = new Map();
const sourceControls = { python: document.getElementById("install-source-python"), cuda: document.getElementById("install-source-cuda"), model: document.getElementById("install-source-model") };
const sourceChoices = { python: ["auto", "tuna", "aliyun", "official"], cuda: ["auto", "aliyun", "sjtu", "official"], model: ["auto", "mirror", "official"] };
const sourceFields = document.getElementById("install-source-fields"), sourceMessage = document.getElementById("install-source-message"), sourceReset = document.getElementById("install-source-reset");
let selectedSources = { python: "auto", cuda: "auto", model: "auto" };
try {
  const stored = JSON.parse(window.localStorage?.getItem("breeze.install.sources") || "null");
  if (stored && typeof stored === "object") for (const key of Object.keys(sourceControls)) if (sourceChoices[key].includes(stored[key])) selectedSources[key] = stored[key];
} catch { /* Storage may be unavailable in a sandboxed plugin view. */ }
for (const [key, control] of Object.entries(sourceControls)) control.value = selectedSources[key];
function rememberSources() {
  if (sourceFields.disabled) return;
  selectedSources = Object.fromEntries(Object.entries(sourceControls).map(([key, control]) => [key, sourceChoices[key].includes(control.value) ? control.value : "auto"]));
  try {
    if (!window.localStorage) throw new Error("Storage unavailable");
    window.localStorage.setItem("breeze.install.sources", JSON.stringify(selectedSources));
    sourceMessage.textContent = "已记住下载源；手动选择仅使用所选源，下一次安装生效。";
  } catch { sourceMessage.textContent = "下载源已选择；浏览器不允许保存，关闭页面后需重新选择。"; }
}
sourceFields.addEventListener("change", rememberSources);
sourceReset.addEventListener("click", () => { if (sourceFields.disabled) return; for (const control of Object.values(sourceControls)) control.value = "auto"; rememberSources(); });

function service(kind, prefix) {
  const el = (name) => document.getElementById(prefix + name);
  const serviceStatus = el("service-status");
  const modelStatus = el("model-status");
  const message = el("message");
  const start = el("start");
  const stop = el("stop");
  const refreshButton = el("refresh");
  const install = el("install");
  const cancelInstall = el("install-cancel"), installHint = el("install-hint"), installLogs = el("install-logs");
  const installWrap = el("install-wrap"), installStage = el("install-stage");
  const installProgress = el("install-progress"), installDetail = el("install-detail"), installLog = el("install-log");
  let installation = null;
  let installKnown = false;
  let disposed = false;
  let current = null;
  let busy = false;
  let stopping = false;
  let timer = null;
  let refreshQueued = false;

  function show() {
    const anotherRunning = [...serviceStates.entries()].some(([key, value]) => key !== kind && (value.running || value.starting));
    const blockers = installation?.blockers || [];
    serviceStatus.textContent = installation?.active ? "正在安装" : !current ? "状态未知" : stopping ? "正在关闭" : current.starting ? "正在启动" : current.running ? "运行中" : "未运行";
    serviceStatus.dataset.state = current?.ready ? "ready" : "idle";
    modelStatus.textContent = !current ? "状态未知" : current.ready ? "就绪" : current.running || current.starting ? "未就绪" : "未启动";
    start.disabled = Boolean(busy || stopping || !installKnown || installation?.busy || !current || current.enabled === false || current.running || current.starting || current.task_running);
    stop.disabled = Boolean(busy || stopping || !installKnown || installation?.busy || !current || (!current.running && !current.starting) || current.task_running);
    refreshButton.disabled = busy;
    install.disabled = Boolean(busy || stopping || !installKnown || installation?.busy || !current || current.running || current.starting || current.task_running || anotherRunning || blockers.length);
    install.textContent = installation?.active ? "正在安装…" : installation?.busy ? "另一服务正在安装" : installation?.state === "completed" ? "重新检查并补齐" : ["failed", "cancelled"].includes(installation?.state) ? "继续安装 / 修复" : kind === "tts" ? "一键安装普通语音" : "一键安装翻唱";
    cancelInstall.hidden = !installation?.active;
    cancelInstall.disabled = busy;
    installHint.textContent = blockers.length ? blockers.join("；") : installation?.active ? "可关闭页面，安装会继续；点击停止安装可保留文件，下次继续。" : anotherRunning || current?.running || current?.starting ? "请先关闭普通语音和翻唱两个服务，再安装或修复。" : kind === "tts" ? "首次安装建议预留16 GiB；自动补齐源码，复用完整文件，完成后点击启动。" : "首次安装建议预留12 GiB；无需Git，复用完整文件，保留歌库和音色。";
    installWrap.hidden = !installation || installation.state === "idle";
    installWrap.setAttribute("aria-busy", String(Boolean(installation?.active)));
    if (installation) {
      installWrap.dataset.state = installation.state;
      const percent = typeof installation.progress === "number" ? ` · 当前步骤 ${Math.round(installation.progress * 100)}%` : installation.active ? " · 处理中" : "";
      const elapsed = Number.isFinite(installation.elapsed_seconds) ? ` · 已用${Math.floor(installation.elapsed_seconds / 60)}分${Math.floor(installation.elapsed_seconds % 60)}秒` : "";
      installStage.textContent = `${installation.step ? `步骤 ${installation.step}/${installation.steps} · ` : ""}${installation.stage}${percent}${elapsed}`;
      if (installKnown && typeof installation.progress === "number") installProgress.value = installation.progress;
      else installProgress.removeAttribute("value");
      installDetail.textContent = installKnown ? installation.error || installation.detail || "" : "安装状态暂时无法刷新，请稍后重试。";
      installLog.textContent = installation.logs.join("\n") || "暂无额外日志。";
      if (installation.state === "failed") installLogs.open = true;
      if (installation.active && installation.last_output_at && Date.now() / 1000 - installation.last_output_at > 120) installDetail.textContent += " 当前步骤已超过2分钟没有新输出；可查看日志，必要时停止后继续安装。";
    }
  }
  function acceptInstallation(data) {
    if (!data || typeof data.active !== "boolean" || typeof data.busy !== "boolean"
        || !["idle", "running", "completed", "failed", "cancelled"].includes(data.state)
        || typeof data.stage !== "string" || !Number.isSafeInteger(data.step) || !Number.isSafeInteger(data.steps)
        || data.steps < 1 || data.step < 0 || data.step > data.steps
        || (data.progress !== null && (typeof data.progress !== "number" || !Number.isFinite(data.progress) || data.progress < 0 || data.progress > 1))
        || !Array.isArray(data.logs) || data.logs.some(line => typeof line !== "string")) {
      throw new Error("安装进度格式不正确");
    }
    installation = data;
    if (data.blockers !== undefined && (!Array.isArray(data.blockers) || data.blockers.some(value => typeof value !== "string"))) throw new Error("安装条件格式不正确");
    installKnown = true;
    installStates.set(kind, data);
    const anyInstalling = [...installStates.values()].some(state => state.busy);
    sourceFields.disabled = anyInstalling;
    sourceReset.disabled = anyInstalling;
  }
  function accept(data) {
    if (!data || typeof data.running !== "boolean" || typeof data.ready !== "boolean") {
      throw new Error("服务返回的状态格式不正确");
    }
    current = data;
    const previous = serviceStates.get(kind);
    serviceStates.set(kind, data);
    if (!previous || previous.running !== data.running || previous.starting !== data.starting) window.dispatchEvent(new CustomEvent("breeze-service-status"));
    if (data.stopping) stopping = true;
    else if (stopping && !data.running && !data.starting) stopping = false;
    message.textContent = data.error || data.message || "";
  }
  function scheduleRefresh() {
    if (timer) clearTimeout(timer);
    if (disposed) return;
    if (refreshQueued) {
      refreshQueued = false;
      timer = setTimeout(refresh, 0);
    } else {
      timer = installation?.busy ? setTimeout(refresh, 2000) : !installKnown ? setTimeout(refresh, 5000) : current && (current.starting || stopping) ? setTimeout(refresh, 3000) : null;
    }
  }
  async function refresh() {
    if (disposed) return;
    if (busy) { refreshQueued = true; return; }
    busy = true;
    show();
    try {
      const wasInstalling = installation?.busy;
      installKnown = false;
      acceptInstallation(await bridge.apiGet(`${kind}/install/status`));
      accept(await bridge.apiGet(`${kind}/status`));
      if (wasInstalling && !installation.busy) window.dispatchEvent(new CustomEvent("breeze-install-change", { detail: kind }));
    } catch (error) {
      current = null;
      stopping = false;
      message.textContent = `状态查询失败：${error.message || error}`;
    } finally {
      busy = false;
      show();
      scheduleRefresh();
    }
  }
  start.addEventListener("click", async () => {
    if (busy || stopping || !installKnown || installation?.busy || !current || current.enabled === false || current.running || current.starting || current.task_running) return;
    busy = true;
    show();
    try {
      accept(await bridge.apiPost(`${kind}/start`, {}));
    } catch (error) {
      message.textContent = `启动失败：${error.message || error}`;
    } finally {
      busy = false;
      show();
      scheduleRefresh();
    }
  });
  stop.addEventListener("click", async () => {
    if (busy || stopping || !installKnown || installation?.busy || !current || (!current.running && !current.starting) || current.task_running) return;
    busy = true;
    stopping = true;
    show();
    let refreshAfterStop = false;
    try {
      const state = await bridge.apiPost(`${kind}/stop`, {});
      accept(state);
      if (!state.error) {
        stopping = Boolean(state.stopping || state.running || state.starting);
        refreshAfterStop = true;
      }
    } catch (error) {
      stopping = false;
      message.textContent = `关闭失败：${error.message || error}`;
    } finally {
      busy = false;
      show();
      scheduleRefresh();
    }
    if (refreshAfterStop) await refresh();
  });
  install.addEventListener("click", async () => {
    if (install.disabled) return;
    busy = true;
    message.textContent = "正在提交安装请求；已安装的完整文件会复用。";
    sourceFields.disabled = true;
    sourceReset.disabled = true;
    show();
    try {
      acceptInstallation(await bridge.apiPost(`${kind}/install`, { sources: { ...selectedSources } }));
    } catch (error) {
      message.textContent = `安装请求未完成或结果未确认：${error.message || error}。正在重新查询实际状态。`;
    } finally {
      busy = false;
      refreshQueued = true;
      show();
      scheduleRefresh();
      window.dispatchEvent(new CustomEvent("breeze-install-change", { detail: kind }));
    }
  });
  cancelInstall.addEventListener("click", async () => {
    if (busy || !installation?.active) return;
    busy = true;
    message.textContent = "正在停止安装进程，已完成文件会保留。";
    show();
    try {
      acceptInstallation(await bridge.apiPost(`${kind}/install/cancel`, {}));
    } catch (error) {
      message.textContent = `停止结果尚未确认：${error.message || error}。正在查询实际状态。`;
    } finally {
      busy = false;
      refreshQueued = true;
      show();
      scheduleRefresh();
      window.dispatchEvent(new CustomEvent("breeze-install-change", { detail: kind }));
    }
  });
  window.addEventListener("breeze-service-status", show);
  window.addEventListener("breeze-install-change", event => { if (event.detail !== kind) refresh(); });
  refreshButton.addEventListener("click", refresh);
  window.addEventListener("beforeunload", () => { disposed = true; if (timer) clearTimeout(timer); });
  return refresh;
}

function songTask(onStatus) {
  const el = name => document.getElementById(`task-${name}`);
  const card = el("card"), status = el("status"), name = el("name"), kind = el("kind");
  const stage = el("stage"), elapsed = el("elapsed"), result = el("result"), detail = el("detail");
  const errorMessage = el("error"), refreshMessage = el("refresh-message"), freshness = el("freshness");
  const progressWrap = el("progress-wrap"), progress = el("progress"), progressText = el("progress-text");
  const announcement = el("announcement"), refreshButton = el("refresh");
  const states = { idle: "暂无任务", running: "进行中", completed: "已完成", failed: "失败", cancelled: "已取消" };
  const kinds = { "": "—", learn: "收录歌曲", full: "整首翻唱", clip: "片段翻唱" };
  let current = null, busy = false, stale = false, disposed = false, timer = null;
  let announcedStage = "", lastChecked = "";

  function text(node, value) {
    if (node.textContent !== value) node.textContent = value;
  }
  function validate(data) {
    const number = value => typeof value === "number" && Number.isFinite(value) && value >= 0;
    if (!data || typeof data.active !== "boolean" || !Object.hasOwn(states, data.state) || !Object.hasOwn(kinds, data.kind)
        || ["name", "stage", "label", "detail"].some(key => typeof data[key] !== "string")
        || !number(data.elapsed_seconds) || !Number.isSafeInteger(data.sent_parts) || data.sent_parts < 0
        || (data.total_parts !== null && (!Number.isSafeInteger(data.total_parts) || data.total_parts < data.sent_parts))
        || (data.progress !== null && (!number(data.progress) || data.progress > 1))
        || (data.error !== null && typeof data.error !== "string")
        || ["started_at", "updated_at"].some(key => data[key] !== null && !number(data[key]))) {
      throw new Error("任务状态返回格式不正确");
    }
    return data;
  }
  function elapsedText(value) {
    const seconds = Math.floor(value);
    const hours = Math.floor(seconds / 3600), minutes = Math.floor(seconds % 3600 / 60);
    return `${hours ? `${hours} 小时 ` : ""}${hours || minutes ? `${minutes} 分 ` : ""}${seconds % 60} 秒`;
  }
  function show(data, recovered) {
    card.dataset.state = data.state;
    card.dataset.stale = "false";
    text(status, states[data.state]);
    status.dataset.state = data.state === "completed" ? "ready" : data.state === "failed" ? "error" : "idle";
    text(name, data.name || "—");
    text(kind, kinds[data.kind]);
    text(stage, data.label || (data.state === "idle" ? "暂无歌曲任务" : data.stage));
    text(elapsed, data.state === "idle" ? "—" : elapsedText(data.elapsed_seconds));
    const fraction = data.state === "completed" ? 1 : data.progress !== null && data.progress < 1 ? data.progress : null;
    const showProgress = data.active || data.state === "completed" || fraction !== null;
    progressWrap.hidden = !showProgress;
    progress.hidden = !showProgress;
    if (fraction !== null) {
      progress.setAttribute("value", String(fraction));
      const label = data.state === "completed" ? "任务已完成" : `发送进度 ${Math.floor(fraction * 100)}%`;
      text(progressText, label);
      progress.setAttribute("aria-valuetext", label);
    } else {
      progress.removeAttribute("value");
      text(progressText, "处理中，当前阶段暂无可测百分比");
      progress.setAttribute("aria-valuetext", "处理中，进度百分比未知");
    }
    let summary = data.state === "idle" ? "暂无歌曲任务。" : data.state === "completed" ? "任务已完成。" : data.state === "failed" ? "任务未完成。" : data.state === "cancelled" ? "任务已取消。" : data.kind === "learn" ? "正在收录歌曲。" : "尚未开始发送音频。";
    if (data.total_parts !== null || data.sent_parts > 0) {
      const count = data.total_parts === null ? String(data.sent_parts) : `${data.sent_parts} / ${data.total_parts}`;
      const unit = data.delivery === "file" ? "个音频文件" : data.delivery === "voice" ? "条语音" : "条音频";
      summary = `${data.active ? "" : summary + " "}已发送 ${count} ${unit}。`;
    }
    text(result, summary);
    text(detail, data.detail || "暂无额外详情。");
    text(errorMessage, data.error || "");
    errorMessage.hidden = !data.error;
    text(freshness, `最近查询：${lastChecked}；${data.active ? "每 3 秒" : "每 5 秒"}自动刷新。`);
    refreshMessage.hidden = true;
    const nextStage = JSON.stringify([data.name, data.state, data.stage, data.label]);
    if (recovered || nextStage !== announcedStage) {
      text(announcement, `${recovered ? "任务状态已恢复更新。" : ""}${data.name ? `《${data.name}》：` : ""}${data.label || states[data.state]}`);
      announcedStage = nextStage;
    }
  }
  async function refresh() {
    if (busy || disposed) return;
    if (timer) clearTimeout(timer);
    busy = true;
    refreshButton.disabled = true;
    try {
      const data = validate(await bridge.apiGet("task/status"));
      if (disposed) return;
      current = data;
      lastChecked = new Date().toLocaleTimeString("zh-CN", { hour12: false });
      show(data, stale);
      stale = false;
      onStatus(data);
    } catch (error) {
      if (disposed) return;
      card.dataset.stale = "true";
      text(status, "状态刷新失败");
      status.dataset.state = "error";
      text(refreshMessage, `状态刷新失败：${error.message || error}。${current ? "已保留最近一次查询结果，当前状态尚未确认。" : "尚未获得任务状态，请稍后重试。"}`);
      refreshMessage.hidden = false;
      text(freshness, lastChecked ? `最近成功查询：${lastChecked}（当前未实时更新）。` : "尚无成功的查询记录。");
      if (current?.active) {
        progress.hidden = true;
        text(progressText, "进度已暂停更新");
      } else if (!current) {
        text(stage, "状态未知");
        text(result, "暂未获得歌曲任务状态。");
        progressWrap.hidden = true;
      }
      if (!stale) text(announcement, "任务状态刷新失败，当前状态尚未确认。");
      stale = true;
    } finally {
      busy = false;
      if (!disposed) {
        refreshButton.disabled = false;
        timer = setTimeout(refresh, current?.active && !stale ? 3000 : 5000);
      }
    }
  }
  refreshButton.addEventListener("click", refresh);
  window.addEventListener("beforeunload", () => {
    disposed = true;
    if (timer) clearTimeout(timer);
  });
  return refresh;
}

function songLibrary() {
  const el = name => document.getElementById(`library-${name}`);
  const list = el("list"), count = el("count"), empty = el("empty"), message = el("message");
  const refreshButton = el("refresh"), form = el("upload-form"), fields = el("upload-fields");
  const name = el("upload-name"), fileInput = el("upload-file"), artist = el("upload-artist"), language = el("upload-language");
  const submitButton = el("upload-submit"), uploadMessage = el("upload-message"), uploadProgress = el("upload-progress-wrap");
  const deletePanel = el("delete-confirm"), deleteName = el("delete-name"), deleteSubmit = el("delete-submit"), deleteCancel = el("delete-cancel");
  const languages = { "": "自动识别", zh: "中文", en: "英文", ja: "日文", ko: "韩文" };
  const sources = { upload: "手动上传", url: "网络收录", local: "本地历史" };
  const maxBytes = 120 * 1024 * 1024;
  let ready = false, canUpload = false, loading = false, refreshQueued = false, hasList = false, uploading = false;
  let taskActive = false, awaitingTask = "", lastCompleted = "";
  let deleteTarget = null, deleting = false, deleteButtons = [];

  function say(node, text, error = false) {
    node.textContent = text;
    node.dataset.kind = error ? "error" : "info";
  }
  function controls() {
    const blocked = !ready || uploading || Boolean(awaitingTask) || taskActive || deleting;
    fields.disabled = blocked || !canUpload;
    submitButton.disabled = blocked || !canUpload || Boolean(deleteTarget) || !name.value.trim() || !fileInput.files?.length;
    submitButton.textContent = uploading ? "正在上传…" : awaitingTask ? "正在收录…" : taskActive ? "歌曲任务进行中" : "上传并永久收录";
    refreshButton.disabled = loading || deleting;
    deletePanel.hidden = !deleteTarget;
    deleteSubmit.disabled = blocked || loading || !deleteTarget;
    deleteCancel.disabled = deleting;
    for (const button of deleteButtons) button.disabled = blocked || loading;
    uploadProgress.hidden = !uploading;
    form.setAttribute("aria-busy", String(uploading || Boolean(awaitingTask)));
  }
  function durationText(seconds) {
    const whole = Math.floor(seconds);
    return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
  }
  function showSongs(data) {
    if (!data || data.persistent !== true || !Array.isArray(data.songs)
        || data.songs.some(song => !song || typeof song.id !== "string" || typeof song.name !== "string"
          || typeof song.duration !== "number" || !Number.isFinite(song.duration) || song.duration <= 0
          || song.persistent !== true || !Object.hasOwn(sources, song.source_type))) {
      throw new Error("歌曲库返回格式不正确");
    }
    const rows = document.createDocumentFragment();
    deleteButtons = [];
    for (const song of data.songs) {
      const row = document.createElement("li"), heading = document.createElement("div");
      const title = document.createElement("h3"), saved = document.createElement("span"), meta = document.createElement("p");
      const actions = document.createElement("div"), remove = document.createElement("button");
      heading.className = "song-heading";
      title.textContent = song.name;
      saved.className = "badge";
      saved.dataset.state = "ready";
      saved.textContent = "永久保存";
      actions.className = "song-actions";
      remove.type = "button";
      remove.className = "danger";
      remove.textContent = "删除音源";
      remove.setAttribute("aria-label", `删除《${song.name}》的原音源`);
      remove.addEventListener("click", () => {
        if (!ready || loading || uploading || awaitingTask || taskActive || deleting) return;
        deleteTarget = { id: song.id, name: song.name };
        deleteName.textContent = `《${song.name}》`;
        say(message, `请确认是否删除《${song.name}》的原始音源及对应时间轴缓存。`);
        controls();
        deleteCancel.focus();
      });
      deleteButtons.push(remove);
      actions.append(saved, remove);
      heading.append(title, actions);
      const information = [durationText(song.duration), sources[song.source_type]];
      if (typeof song.artist === "string" && song.artist) information.push(`歌手：${song.artist}`);
      if (typeof song.language === "string" && song.language) information.push(`语言：${languages[song.language] || song.language}`);
      if (typeof song.source_url === "string") {
        try { const host = new URL(song.source_url).hostname; if (host) information.push(host); } catch { /* Old local entries may have no source URL. */ }
      }
      meta.className = "song-meta";
      meta.textContent = information.join(" · ");
      row.append(heading, meta);
      rows.append(row);
    }
    list.replaceChildren(rows);
    list.hidden = data.songs.length === 0;
    empty.hidden = data.songs.length !== 0;
    empty.textContent = "还没有收录歌曲，可以手动上传音频，或让 Bot 联网收录。";
    count.textContent = `${data.songs.length} 首`;
    count.dataset.state = "ready";
    hasList = true;
    if (deleteTarget && !data.songs.some(song => song.id === deleteTarget.id && song.name === deleteTarget.name)) deleteTarget = null;
    if (awaitingTask && data.songs.some(song => song.name === awaitingTask)) {
      say(uploadMessage, `《${awaitingTask}》已在歌曲库中永久保存，可让 Bot 翻唱。`);
      awaitingTask = "";
      fileInput.value = "";
    }
  }
  async function refresh() {
    if (loading) { refreshQueued = true; return; }
    loading = true;
    let refreshed = false;
    controls();
    try {
      showSongs(await bridge.apiGet("library/songs"));
      say(message, "歌曲库已更新，全部歌曲均永久保存在本机。");
      refreshed = true;
    } catch (error) {
      if (!hasList) {
        count.textContent = "读取失败";
        count.dataset.state = "error";
        empty.textContent = "暂时无法读取歌曲库。";
      }
      say(message, `读取歌曲库失败：${error.message || error}。${hasList ? "已保留上次列表，请点击刷新重试。" : "请点击刷新重试。"}`, true);
    } finally {
      loading = false;
      controls();
      if (refreshQueued) { refreshQueued = false; await refresh(); }
    }
    return refreshed;
  }
  function onTaskStatus(data) {
    taskActive = data.active;
    let refreshNeeded = false;
    if (awaitingTask && data.name === awaitingTask && data.kind === "learn" && !data.active && ["completed", "failed", "cancelled"].includes(data.state)) {
      awaitingTask = "";
      if (data.state === "completed") {
        say(uploadMessage, `《${data.name}》已永久收录，可让 Bot 翻唱；你也可以继续上传下一首。`);
        fileInput.value = "";
      } else {
        say(uploadMessage, `收录未完成：${data.error || data.label || data.state}。歌曲名称和所选文件已保留，可核对后重试。`, true);
      }
    } else if (awaitingTask && !data.active) {
      awaitingTask = "";
      say(uploadMessage, "上传任务已不在最近任务记录中，正在刷新歌曲库核对结果；名称和文件已保留。", true);
      refreshNeeded = true;
    }
    const completed = JSON.stringify([data.started_at, data.name, data.kind]);
    if (ready && data.kind === "learn" && data.state === "completed" && completed !== lastCompleted) {
      lastCompleted = completed;
      refreshNeeded = true;
    }
    controls();
    if (refreshNeeded) void refresh();
  }
  form.addEventListener("input", controls);
  form.addEventListener("change", controls);
  refreshButton.addEventListener("click", refresh);
  deleteCancel.addEventListener("click", () => {
    if (deleting) return;
    deleteTarget = null;
    controls();
    say(message, "已取消删除，歌曲库未更改。");
  });
  deleteSubmit.addEventListener("click", async () => {
    if (!ready || loading || !deleteTarget || deleting || uploading || awaitingTask || taskActive) return;
    const target = deleteTarget;
    deleting = true;
    controls();
    say(message, `正在删除《${target.name}》的原音源和时间轴缓存。`);
    try {
      const reply = await bridge.apiPost("library/delete", target);
      if (!reply || reply.deleted !== true || reply.id !== target.id || reply.name !== target.name) {
        throw new Error("删除结果未能确认，请刷新歌曲库核对");
      }
      deleteTarget = null;
      const refreshed = await refresh();
      say(message, `已删除《${target.name}》的原始音源及对应时间轴缓存。${refreshed ? "" : "列表刷新未成功，请手动刷新核对。"}`, !refreshed);
    } catch (error) {
      say(message, `删除未完成或结果尚未确认：${error.message || error}。已保留当前列表与确认目标，不会自动重试；请核对后再操作。`, true);
    } finally {
      deleting = false;
      controls();
    }
  });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (!ready || !canUpload || uploading || awaitingTask || taskActive || deleting || deleteTarget || !form.reportValidity()) return;
    const file = fileInput.files?.[0], songName = name.value.trim(), songArtist = artist.value.trim();
    const extension = typeof file?.name === "string" ? (/^.+\.([^.]+)$/.exec(file.name)?.[1] || "").toLowerCase() : "";
    if (!songName || songName.length > 160 || songArtist.length > 160 || !Object.hasOwn(languages, language.value)) {
      say(uploadMessage, "请填写 1–160 字的歌曲名称、有效语言及最多 160 字的歌手。", true);
      return;
    }
    if (!file || !Number.isSafeInteger(file.size) || file.size <= 0 || file.size > maxBytes
        || !["wav", "mp3", "m4a", "flac", "ogg", "opus", "aac", "webm", "mp4"].includes(extension)) {
      say(uploadMessage, "请选择支持的音频文件，大小须为 1 字节至 120 MiB；音频最长 10 分钟，由服务端核验。", true);
      return;
    }
    uploading = true;
    controls();
    say(uploadMessage, "正在上传音频，请勿重复提交。上传后将在后台收录，进度显示在上方歌曲任务卡片。");
    try {
      const draft = await bridge.apiPost("library/upload/prepare", {
        name: songName, artist: songArtist, language: language.value, file_name: file.name, size: file.size,
      });
      if (!draft || typeof draft.upload_id !== "string" || !/^[A-Za-z0-9_-]{16,128}$/.test(draft.upload_id)) {
        throw new Error("未获得有效的上传标识");
      }
      const reply = await bridge.upload(`library/upload/${draft.upload_id}`, file);
      if (!reply || reply.started !== true || typeof reply.name !== "string" || !reply.name) {
        throw new Error("未能确认后台收录任务是否启动，请先刷新任务状态和歌曲库");
      }
      awaitingTask = reply.name;
      say(uploadMessage, `《${reply.name}》上传完成，正在后台收录；请查看上方歌曲任务进度。`);
    } catch (error) {
      say(uploadMessage, `上传未完成或结果尚未确认：${error.message || error}。名称和所选文件已保留；请先查看任务状态或刷新歌曲库，确认后再重试。`, true);
    } finally {
      uploading = false;
      controls();
    }
    await refreshTask();
  });
  async function initialize() {
    ready = true;
    canUpload = typeof bridge.upload === "function";
    if (!canUpload) say(uploadMessage, "当前 AstrBot 不支持原生文件上传，请更新后重试。", true);
    controls();
    await refresh();
  }
  return { initialize, onTaskStatus };
}

const library = songLibrary();
const refreshTask = songTask(library.onTaskStatus);
const refreshTts = service("tts", "tts-");
const refreshCover = service("cover", "");
const form = document.getElementById("config-form");
const fields = document.getElementById("config-fields");
const saveButton = document.getElementById("config-save");
const reloadButton = document.getElementById("config-reload");
const configState = document.getElementById("config-state");
const configMessage = document.getElementById("config-message");
const discardConfirm = document.getElementById("config-discard-confirm");
const confirmReloadButton = document.getElementById("config-confirm-reload");
const cancelReloadButton = document.getElementById("config-cancel-reload");
const configFields = [
  { key: "instruction", id: "config-instruction", label: "音色描述", type: "string" },
  { key: "cfg_scale", id: "config-cfg-scale", label: "CFG 强度", type: "number", min: 1, max: 10 },
  { key: "seed", id: "config-seed", label: "随机种子", type: "integer", min: 0, max: 4294967295 },
  { key: "ref_audio_path", id: "config-ref-audio-path", label: "参考音频路径", type: "string", trim: true },
  { key: "ref_text", id: "config-ref-text", label: "参考音频原文", type: "string" },
  { key: "clone_use_instruction", id: "config-clone-use-instruction", label: "克隆语音引导", type: "boolean" },
  { key: "api_base", id: "config-api-base", label: "普通语音服务地址", type: "string", trim: true },
  { key: "timeout", id: "config-timeout", label: "普通语音操作超时", type: "integer", min: 1 },
  { key: "max_retries", id: "config-max-retries", label: "最多请求次数", type: "integer", min: 1 },
  { key: "singing.enabled", id: "config-singing-enabled", label: "启用翻唱", type: "boolean" },
  { key: "singing.api_base", id: "config-singing-api-base", label: "翻唱服务地址", type: "string", trim: true },
  { key: "singing.timeout", id: "config-singing-timeout", label: "翻唱操作超时", type: "integer", min: 1 },
].map(field => ({ ...field, input: document.getElementById(field.id) }));
let revision = null;
let loaded = false;
let loading = false;
let saving = false;
let conflict = false;
let loadFailed = false;
let baseline = "";
let repairedDraft = false;
let confirmingDiscard = false;

function snapshot() {
  return JSON.stringify(configFields.map(({ input, type }) => type === "boolean" ? [input.checked, input.indeterminate] : input.value));
}
function isDirty() {
  return loaded && (repairedDraft || snapshot() !== baseline);
}
function showConfigState() {
  const dirty = isDirty();
  fields.disabled = loading || saving || !loaded;
  saveButton.disabled = loading || saving || !loaded || !dirty || conflict;
  reloadButton.disabled = loading || saving;
  discardConfirm.hidden = !confirmingDiscard;
  confirmReloadButton.disabled = loading || saving;
  cancelReloadButton.disabled = loading || saving;
  form.setAttribute("aria-busy", String(loading || saving));
  configState.textContent = saving ? "正在保存" : loading ? "正在读取" : conflict ? "版本冲突" : loadFailed ? "读取失败" : !loaded ? "尚未读取" : dirty ? "未保存" : "已保存";
  configState.dataset.state = conflict || loadFailed ? "error" : dirty ? "dirty" : loaded ? "ready" : "idle";
}
function showConfigMessage(text, error = false) {
  configMessage.textContent = text;
  configMessage.dataset.kind = error ? "error" : "info";
}
function applyConfig(data) {
  if (!data || typeof data.revision !== "string" || !data.revision || !data.config || typeof data.config !== "object" || Array.isArray(data.config)) {
    throw new Error("配置返回格式不正确，请重新读取");
  }
  const config = data.config;
  let singing = config.singing;
  let repair = !singing || typeof singing !== "object" || Array.isArray(singing);
  if (repair) singing = { enabled: true, api_base: "http://127.0.0.1:7861", timeout: 1800 };
  for (const field of configFields) {
    const value = field.key.startsWith("singing.") ? singing[field.key.slice(8)] : config[field.key];
    if (field.type === "boolean") {
      field.input.checked = value === true;
      field.input.indeterminate = typeof value !== "boolean";
      repair ||= field.input.indeterminate;
    } else if (field.type === "string") {
      field.input.value = typeof value === "string" ? value : "";
      repair ||= typeof value !== "string";
    } else {
      field.input.value = typeof value === "number" || typeof value === "string" ? String(value) : "";
      repair ||= typeof value !== "number" || !Number.isFinite(value);
    }
  }
  revision = data.revision;
  loaded = true;
  conflict = false;
  confirmingDiscard = false;
  loadFailed = false;
  baseline = snapshot();
  repairedDraft = repair;
  return repair;
}
function readConfig() {
  const config = { singing: {} };
  for (const field of configFields) {
    let value;
    if (field.type === "boolean") {
      if (field.input.indeterminate) throw new Error(`请明确勾选或取消“${field.label}”`);
      value = field.input.checked;
    } else if (field.type === "string") {
      value = field.trim ? field.input.value.trim() : field.input.value;
    } else {
      value = Number(field.input.value);
      if (!field.input.value.trim() || !Number.isFinite(value) || (field.type === "integer" && !Number.isSafeInteger(value)) || (field.min !== undefined && value < field.min) || (field.max !== undefined && value > field.max)) {
        throw new Error(`请为“${field.label}”填写范围内的有效${field.type === "integer" ? "整数" : "数字"}`);
      }
    }
    if (field.key.startsWith("singing.")) config.singing[field.key.slice(8)] = value;
    else config[field.key] = value;
  }
  return config;
}
function canonicalReply(data) {
  if (!data || typeof data.revision !== "string" || !data.revision || !data.config || typeof data.config !== "object" || Array.isArray(data.config)) return false;
  return configFields.every(field => {
    const value = field.key.startsWith("singing.") ? data.config.singing?.[field.key.slice(8)] : data.config[field.key];
    if (field.type === "boolean" || field.type === "string") return typeof value === field.type;
    return typeof value === "number" && Number.isFinite(value) && (field.type !== "integer" || Number.isSafeInteger(value)) && (field.min === undefined || value >= field.min) && (field.max === undefined || value <= field.max);
  });
}
async function loadConfig(confirmChanges = true) {
  if (loading || saving) return;
  if (confirmChanges && isDirty()) {
    confirmingDiscard = true;
    showConfigState();
    confirmReloadButton.focus();
    return;
  }
  confirmingDiscard = false;
  loading = true;
  showConfigState();
  showConfigMessage("正在读取已保存的配置。");
  try {
    const repaired = applyConfig(await bridge.apiGet("config"));
    showConfigMessage(repaired ? "部分旧配置格式无效，已显示可修正的草稿；请核对全部选项后保存。" : "已读取配置，修改后请点击保存。", repaired);
  } catch (error) {
    loadFailed = true;
    showConfigMessage(`读取配置失败：${error.message || error}。${loaded ? "当前输入已保留。" : "保存暂不可用，请点击重新读取。"}`, true);
  } finally {
    loading = false;
    showConfigState();
  }
}
form.addEventListener("input", showConfigState);
form.addEventListener("change", showConfigState);
reloadButton.addEventListener("click", () => loadConfig());
confirmReloadButton.addEventListener("click", () => loadConfig(false));
cancelReloadButton.addEventListener("click", () => {
  confirmingDiscard = false;
  showConfigState();
  reloadButton.focus();
});
form.addEventListener("submit", async event => {
  event.preventDefault();
  if (saving || loading || !loaded || !isDirty() || conflict || !form.reportValidity()) return;
  let config;
  try { config = readConfig(); }
  catch (error) { showConfigMessage(error.message || String(error), true); return; }
  saving = true;
  showConfigState();
  showConfigMessage("正在保存配置。");
  let saved = false;
  try {
    const reply = await bridge.apiPost("config", { config, revision });
    if (!canonicalReply(reply)) throw new Error("未能确认已保存的配置，输入已保留；请重新读取并核对");
    applyConfig(reply);
    saved = true;
    showConfigMessage("配置已保存，后续请求使用新设置。");
  } catch (error) {
    const message = error.message || String(error);
    conflict = /配置已变化|版本冲突|revision/i.test(message);
    showConfigMessage(conflict ? "配置已在其他位置变化。当前输入已保留，请重新读取最新配置后再修改；不会覆盖其他修改。" : `保存失败：${message}。当前输入已保留，可重试保存。`, true);
  } finally {
    saving = false;
    showConfigState();
  }
  if (saved) await Promise.all([refreshTts(), refreshCover()]);
});

try {
  await bridge.ready();
  await Promise.all([refreshTts(), refreshCover(), loadConfig(false), refreshTask(), library.initialize()]);
} catch (error) {
  loadFailed = true;
  showConfigMessage(`页面连接失败：${error.message || error}。请点击重新读取。`, true);
  showConfigState();
}
