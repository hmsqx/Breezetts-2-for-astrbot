const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");

async function main() {
  const html = fs.readFileSync(path.join(__dirname, "views", "service", "index.html"), "utf8");
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
  function node(tagName = "") { return {
    textContent: "", value: "", checked: false, indeterminate: false, hidden: true,
    tagName, children: [], files: [], dataset: {}, disabled: false, handlers: {}, attributes: {},
    addEventListener(type, callback) { this.handlers[type] = callback; },
    setAttribute(name, value) { this.attributes[name] = value; },
    removeAttribute(name) { delete this.attributes[name]; },
    append(...items) { for (const item of items) this.children.push(...(item.tagName === "fragment" ? item.children : [item])); },
    replaceChildren(...items) { this.children = []; this.append(...items); },
    focus() {},
    reportValidity() { return true; },
  }; }
  const nodes = Object.fromEntries(ids.map(id => [id, node()]));
  function findNode(root, predicate) {
    if (predicate(root)) return root;
    for (const child of root.children) { const found = findNode(child, predicate); if (found) return found; }
  }
  const states = { cover: { running: false, ready: false }, tts: { running: false, ready: false } };
  const copy = value => JSON.parse(JSON.stringify(value));
  let config = {
    instruction: "柔和的声音", cfg_scale: 4, seed: 42,
    ref_audio_path: "D:/Echok/sample.wav", ref_text: "原文", clone_use_instruction: false,
    api_base: "http://127.0.0.1:7860", timeout: 120, max_retries: 3,
    singing: { enabled: true, api_base: "http://127.0.0.1:7861", timeout: 1800 },
  };
  let revision = "rev-1", configMode = "pending", failRead = false, configGets = 0;
  let task = {
    active: false, state: "idle", name: "", kind: "", stage: "idle", label: "暂无歌曲任务", detail: "",
    started_at: null, updated_at: null, elapsed_seconds: 0,
    progress: null, sent_parts: 0, total_parts: null, error: null,
  };
  let failTask = false;
  let songs = [{ id: "1".repeat(32), name: "旧歌曲<img>", duration: 209.4, persistent: true, source_type: "local" }];
  let libraryReads = 0, prepareCalls = [], uploadCalls = [], deleteCalls = [], deleteMode = "success", resolveUpload;
  let posts = [], configPosts = [], timers = [], timerDelays = [], clearedTimers = [], unload = [], resolveStart, resolveConfig;
  const installations = {};
  const storedOptions = new Map();
  let installPayload;
  const bridge = {
    ready: async () => {},
    apiGet: async endpoint => {
      if (endpoint.endsWith("/install/status")) return copy(installations[endpoint.split("/")[0]] || {
        active: false, busy: false, state: "idle", stage: "尚未安装或检查",
        step: 0, steps: 5, progress: null, detail: "", error: null, logs: [],
      });
      if (endpoint === "library/songs") { libraryReads++; return { songs: copy(songs), persistent: true }; }
      if (endpoint === "task/status") {
        if (failTask) throw new Error("任务状态网络失败");
        return copy(task);
      }
      if (endpoint !== "config") return states[endpoint.split("/")[0]];
      configGets++;
      if (failRead) throw new Error("读取网络失败");
      return { config: copy(config), revision };
    },
    apiPost: (endpoint, body) => {
      if (endpoint.endsWith("/install")) {
        installPayload = copy(body);
        posts.push(endpoint);
        return new Promise(resolve => { resolveStart = resolve; });
      }
      if (endpoint === "library/upload/prepare") {
        prepareCalls.push(copy(body));
        return { upload_id: "a".repeat(32) };
      }
      if (endpoint === "library/delete") {
        deleteCalls.push(copy(body));
        if (deleteMode === "busy") throw new Error("歌曲任务正在进行，请稍后再删除（409）");
        songs = songs.filter(song => song.id !== body.id);
        return { deleted: true, ...body };
      }
      if (endpoint === "config") {
        configPosts.push(copy(body));
        if (configMode === "network") throw new Error("网络中断");
        if (configMode === "conflict") throw new Error("配置已变化，请重新读取后再保存（409）");
        if (configMode === "busy") throw new Error("歌曲任务正在进行，请等任务完成（409）");
        return new Promise(resolve => { resolveConfig = resolve; });
      }
      assert.deepEqual(body, {}); posts.push(endpoint);
      return new Promise(resolve => { resolveStart = resolve; });
    },
    upload: (endpoint, file) => {
      uploadCalls.push({ endpoint, file });
      return new Promise(resolve => { resolveUpload = resolve; });
    },
  };
  const listeners = {};
  global.CustomEvent = class { constructor(type, options = {}) { this.type = type; this.detail = options.detail; } };
  global.window = { AstrBotPluginView: bridge, localStorage: { getItem: key => storedOptions.get(key) || null, setItem: (key, value) => storedOptions.set(key, value) }, addEventListener(type, callback) { (listeners[type] ||= []).push(callback); if (type === "beforeunload") unload.push(callback); }, dispatchEvent(event) { for (const callback of listeners[event.type] || []) callback(event); } };
  global.document = { getElementById: id => nodes[id], createElement: tag => node(tag), createDocumentFragment: () => node("fragment") };
  global.setTimeout = (callback, delay) => { timers.push(callback); timerDelays.push(delay); return timers.length; };
  global.clearTimeout = timer => { clearedTimers.push(timer); };
  const source = fs.readFileSync(path.join(__dirname, "views", "service", "app.js"), "utf8");
  await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
  assert.equal(nodes["config-instruction"].value, config.instruction);
  assert.equal(nodes["config-singing-enabled"].checked, true);
  assert.equal(nodes["config-fields"].disabled, false);
  assert.equal(nodes["config-save"].disabled, true);
  assert.equal(nodes["task-status"].textContent, "暂无任务");
  assert.equal(nodes["task-progress-wrap"].hidden, true);
  assert.equal(timerDelays.at(-1), 5000);
  assert.equal(nodes["library-count"].textContent, "1 首");
  assert.ok(findNode(nodes["library-list"], item => item.textContent === "旧歌曲<img>"));
  assert.ok(findNode(nodes["library-list"], item => item.textContent.includes("本地历史")));
  for (const [kind, prefix] of [["tts", "tts-"], ["cover", ""]]) {
    assert.equal(nodes[prefix + "service-status"].textContent, "未运行");
    const click = nodes[prefix + "start"].handlers.click();
    await nodes[prefix + "start"].handlers.click();
    assert.equal(posts.filter(p => p === kind + "/start").length, 1);
    resolveStart({ running: false, ready: false, starting: true });
    await click;
    assert.equal(nodes[prefix + "start"].disabled, true);
    states[kind] = { running: true, ready: true };
    await timers.pop()();
    assert.equal(nodes[prefix + "model-status"].textContent, "就绪");
    states[kind] = { running: true, ready: false, error: "模型缺失" };
    await nodes[prefix + "refresh"].handlers.click();
    assert.equal(nodes[prefix + "message"].textContent, "模型缺失");
  }
  const form = nodes["config-form"];
  const submit = () => form.handlers.submit({ preventDefault() {} });
  nodes["config-instruction"].value = "新的音色";
  nodes["config-ref-audio-path"].value = "  D:/Echok/sample.wav  ";
  nodes["config-api-base"].value = " http://localhost:7860 ";
  nodes["config-singing-enabled"].checked = false;
  form.handlers.input();
  assert.equal(nodes["config-save"].disabled, false);
  const save = submit();
  await submit();
  assert.equal(configPosts.length, 1);
  assert.equal(nodes["config-fields"].disabled, true);
  assert.equal(configPosts[0].revision, "rev-1");
  assert.equal(configPosts[0].config.ref_audio_path, "D:/Echok/sample.wav");
  assert.equal(configPosts[0].config.api_base, "http://localhost:7860");
  assert.equal(configPosts[0].config.singing.enabled, false);
  assert.equal(Object.keys(configPosts[0].config).length, 10);
  assert.equal(Object.keys(configPosts[0].config.singing).length, 3);
  config = copy(configPosts[0].config); revision = "rev-2";
  resolveConfig({ config: copy(config), revision });
  await save;
  assert.equal(nodes["config-save"].disabled, true);
  assert.equal(nodes["config-fields"].disabled, false);
  assert.equal(nodes["config-api-base"].value, "http://localhost:7860");
  assert.equal(posts.length, 2, "saving must not start or stop services");

  configMode = "network";
  nodes["config-instruction"].value = "保留的草稿"; form.handlers.input();
  await submit();
  assert.equal(nodes["config-instruction"].value, "保留的草稿");
  assert.equal(nodes["config-save"].disabled, false);
  assert.match(nodes["config-message"].textContent, /保留/);
  configMode = "busy";
  await submit();
  assert.equal(nodes["config-save"].disabled, false, "busy 409 should allow retry");
  configMode = "conflict";
  await submit();
  assert.equal(nodes["config-save"].disabled, true);
  assert.match(nodes["config-message"].textContent, /重新读取/);
  const beforeReload = configGets;
  await nodes["config-reload"].handlers.click();
  assert.equal(configGets, beforeReload);
  assert.equal(nodes["config-discard-confirm"].hidden, false);
  nodes["config-cancel-reload"].handlers.click();
  assert.equal(nodes["config-instruction"].value, "保留的草稿");
  assert.equal(nodes["config-discard-confirm"].hidden, true);
  await nodes["config-reload"].handlers.click();
  failRead = true;
  await nodes["config-confirm-reload"].handlers.click();
  assert.equal(nodes["config-instruction"].value, "保留的草稿");
  assert.equal(nodes["config-fields"].disabled, false);
  assert.match(nodes["config-message"].textContent, /当前输入已保留/);
  failRead = false;
  await nodes["config-reload"].handlers.click();
  await nodes["config-confirm-reload"].handlers.click();
  assert.equal(nodes["config-instruction"].value, config.instruction);
  assert.equal(nodes["config-save"].disabled, true);
  config = { ...config, singing: "legacy invalid container" }; revision = "rev-3";
  await nodes["config-reload"].handlers.click();
  assert.equal(nodes["config-singing-enabled"].checked, true);
  assert.equal(nodes["config-singing-timeout"].value, "1800");
  assert.equal(nodes["config-save"].disabled, false);
  assert.match(nodes["config-message"].textContent, /草稿/);

  task = { ...task, active: true, state: "running", name: "Daisy Crown", kind: "clip",
    stage: "transcribing", label: "识别音频时间轴", detail: "CPU识别管理详情",
    started_at: 1, updated_at: 2, elapsed_seconds: 130 };
  await nodes["task-refresh"].handlers.click();
  assert.equal(nodes["task-name"].textContent, "Daisy Crown");
  assert.equal(nodes["task-stage"].textContent, "识别音频时间轴");
  assert.equal(nodes["task-elapsed"].textContent, "2 分 10 秒");
  assert.equal(nodes["task-progress-wrap"].hidden, false);
  assert.equal(Object.hasOwn(nodes["task-progress"].attributes, "value"), false);
  assert.match(nodes["task-progress-text"].textContent, /暂无可测百分比/);
  assert.equal(nodes["task-detail"].textContent, "CPU识别管理详情");
  assert.equal(timerDelays.at(-1), 3000);
  const announcement = nodes["task-announcement"].textContent;
  task.elapsed_seconds = 133;
  await nodes["task-refresh"].handlers.click();
  assert.equal(nodes["task-announcement"].textContent, announcement, "elapsed updates should not announce the whole task again");
  task = { ...task, stage: "sending", label: "发送语音", progress: 0.5, sent_parts: 1, total_parts: 2 };
  await nodes["task-refresh"].handlers.click();
  assert.equal(nodes["task-progress"].attributes.value, "0.5");
  assert.match(nodes["task-result"].textContent, /1 \/ 2/);
  failTask = true;
  await nodes["task-refresh"].handlers.click();
  assert.equal(nodes["task-status"].textContent, "状态刷新失败");
  assert.equal(nodes["task-stage"].textContent, "发送语音");
  assert.equal(nodes["task-progress"].hidden, true);
  assert.match(nodes["task-refresh-message"].textContent, /保留/);
  assert.match(nodes["task-freshness"].textContent, /未实时更新/);
  assert.equal(timerDelays.at(-1), 5000);
  failTask = false;
  task = { ...task, active: false, state: "failed", stage: "failed", label: "任务失败", progress: 1, error: "发送失败" };
  await nodes["task-refresh"].handlers.click();
  assert.equal(nodes["task-status"].textContent, "失败");
  assert.equal(Object.hasOwn(nodes["task-progress"].attributes, "value"), false, "a failed task must not appear 100% complete");
  assert.equal(nodes["task-error"].textContent, "发送失败");
  task = { ...task, state: "completed", stage: "completed", label: "任务完成", error: null, sent_parts: 2 };
  await nodes["task-refresh"].handlers.click();
  assert.equal(nodes["task-status"].textContent, "已完成");
  assert.equal(nodes["task-progress"].attributes.value, "1");
  assert.match(nodes["task-result"].textContent, /2 \/ 2/);
  for (const [kind, delivery, count, unit] of [
    ["full", "voice", 2, "条语音"],
    ["full", "file", 1, "个音频文件"],
    ["clip", "file", 1, "个音频文件"],
    ["full", undefined, 2, "条音频"],
    ["full", "unknown", 2, "条音频"],
  ]) {
    task = { ...task, kind, delivery, sent_parts: count, total_parts: count };
    await nodes["task-refresh"].handlers.click();
    assert.equal(nodes["task-result"].textContent, `任务已完成。 已发送 ${count} / ${count} ${unit}。`);
  }
  const uploadForm = nodes["library-upload-form"], uploadName = nodes["library-upload-name"], uploadFile = nodes["library-upload-file"];
  const file = { name: "manual.wav", size: 1024, type: "audio/wav", arrayBuffer: async () => new ArrayBuffer(0) };
  uploadName.value = "手动歌曲";
  uploadFile.files = [file]; uploadFile.value = "manual.wav";
  nodes["library-upload-artist"].value = " 歌手 ";
  nodes["library-upload-language"].value = "zh";
  uploadForm.handlers.input();
  const upload = uploadForm.handlers.submit({ preventDefault() {} });
  await Promise.resolve();
  await uploadForm.handlers.submit({ preventDefault() {} });
  assert.equal(prepareCalls.length, 1);
  assert.equal(uploadCalls.length, 1);
  assert.equal(uploadCalls[0].endpoint, "library/upload/" + "a".repeat(32));
  assert.equal(uploadCalls[0].file, file, "native bridge receives the original File, not JSON or base64");
  assert.equal(prepareCalls[0].artist, "歌手");
  assert.equal(nodes["library-upload-fields"].disabled, true);
  assert.equal(nodes["library-upload-progress-wrap"].hidden, false);
  task = { ...task, active: true, state: "running", name: "手动歌曲", kind: "learn", stage: "uploading", label: "收录上传音频", progress: null, sent_parts: 0, total_parts: null, started_at: 10 };
  resolveUpload({ started: true, name: "手动歌曲" });
  await upload;
  assert.equal(nodes["library-upload-fields"].disabled, true);
  task = { ...task, active: false, state: "failed", label: "收录失败", error: "无效音频" };
  await nodes["task-refresh"].handlers.click();
  assert.equal(uploadName.value, "手动歌曲");
  assert.equal(uploadFile.files[0], file);
  assert.match(nodes["library-upload-message"].textContent, /已保留/);
  const retry = uploadForm.handlers.submit({ preventDefault() {} });
  await Promise.resolve();
  task = { ...task, active: true, state: "running", error: null, started_at: 20 };
  resolveUpload({ started: true, name: "手动歌曲" });
  await retry;
  songs.push({ id: "2".repeat(32), name: "手动歌曲", duration: 60, persistent: true, source_type: "upload", artist: "歌手", language: "zh" });
  const beforeComplete = libraryReads;
  task = { ...task, active: false, state: "completed", label: "收录完成", progress: 1 };
  await nodes["task-refresh"].handlers.click();
  await Promise.resolve();
  assert.equal(libraryReads, beforeComplete + 1);
  assert.equal(nodes["library-count"].textContent, "2 首");
  assert.equal(uploadFile.value, "");
  await nodes["task-refresh"].handlers.click();
  assert.equal(libraryReads, beforeComplete + 1, "do not reload the whole library on every task poll");
  uploadName.value = "过大音频";
  uploadFile.files = [{ ...file, size: 120 * 1024 * 1024 + 1 }];
  const beforeOversize = prepareCalls.length;
  await uploadForm.handlers.submit({ preventDefault() {} });
  assert.equal(prepareCalls.length, beforeOversize);
  assert.match(nodes["library-upload-message"].textContent, /120 MiB/);

  const removeOld = () => findNode(nodes["library-list"], item => item.attributes["aria-label"] === "删除《旧歌曲<img>》的原音源");
  removeOld().handlers.click();
  assert.equal(deleteCalls.length, 0);
  assert.equal(nodes["library-delete-name"].textContent, "《旧歌曲<img>》");
  nodes["library-delete-cancel"].handlers.click();
  assert.equal(nodes["library-delete-confirm"].hidden, true);
  removeOld().handlers.click();
  deleteMode = "busy";
  await nodes["library-delete-submit"].handlers.click();
  assert.equal(deleteCalls.length, 1);
  assert.equal(nodes["library-delete-confirm"].hidden, false);
  assert.equal(nodes["library-count"].textContent, "2 首");
  task = { ...task, active: true, state: "running", kind: "clip", stage: "converting", label: "转换歌声", progress: null };
  await nodes["task-refresh"].handlers.click();
  assert.equal(nodes["library-delete-submit"].disabled, true);
  await nodes["library-delete-submit"].handlers.click();
  assert.equal(deleteCalls.length, 1);
  task = { ...task, active: false, state: "completed", stage: "completed", label: "任务完成", progress: 1 };
  await nodes["task-refresh"].handlers.click();
  deleteMode = "success";
  await nodes["library-delete-submit"].handlers.click();
  assert.equal(deleteCalls.length, 2);
  assert.deepEqual(deleteCalls[1], { id: "1".repeat(32), name: "旧歌曲<img>" });
  assert.equal(nodes["library-count"].textContent, "1 首");
  assert.equal(nodes["library-delete-confirm"].hidden, true);
  assert.match(nodes["library-message"].textContent, /已删除.*时间轴缓存/);
  states.tts = { running: false, ready: false };
  states.cover = { running: true, ready: true };
  await nodes["tts-refresh"].handlers.click();
  await nodes["refresh"].handlers.click();
  assert.equal(nodes["tts-install"].disabled, true, "the other service must also be stopped");
  assert.match(nodes["tts-install-hint"].textContent, /两个服务/);
  states.cover = { running: false, ready: false };
  await nodes["refresh"].handlers.click();
  assert.equal(nodes["tts-install"].disabled, false);
  nodes["install-source-python"].value = "aliyun";
  nodes["install-source-cuda"].value = "official";
  nodes["install-source-model"].value = "mirror";
  nodes["install-source-fields"].handlers.change();
  assert.deepEqual(JSON.parse(storedOptions.get("breeze.install.sources")), { python: "aliyun", cuda: "official", model: "mirror" });
  const beginInstall = nodes["tts-install"].handlers.click();
  assert.deepEqual(installPayload, { sources: { python: "aliyun", cuda: "official", model: "mirror" } });
  assert.equal(nodes["install-source-fields"].disabled, true);
  installations.tts = { active: true, busy: true, state: "running", stage: "下载模型", step: 4, steps: 5, progress: 0.2, logs: [], elapsed_seconds: 135, last_output_at: Date.now() / 1000 - 150 };
  resolveStart(copy(installations.tts));
  await beginInstall;
  await nodes["tts-refresh"].handlers.click();
  assert.equal(nodes["tts-install-cancel"].hidden, false);
  assert.match(nodes["tts-install-stage"].textContent, /2分15秒/);
  assert.match(nodes["tts-install-detail"].textContent, /2分钟/);
  const cancel = nodes["tts-install-cancel"].handlers.click();
  assert.equal(posts.at(-1), "tts/install/cancel");
  installations.tts = { ...installations.tts, active: false, busy: false, state: "cancelled", stage: "安装已停止", progress: null };
  resolveStart(copy(installations.tts));
  await cancel;
  assert.match(nodes["tts-install"].textContent, /继续安装/);
  assert.equal(nodes["tts-install-cancel"].hidden, true);
  nodes["install-source-reset"].handlers.click();
  assert.deepEqual(JSON.parse(storedOptions.get("breeze.install.sources")), { python: "auto", cuda: "auto", model: "auto" });
  installations.tts = { ...installations.tts, state: "failed", stage: "检查失败", error: "missing DLL" };
  await nodes["tts-refresh"].handlers.click();
  assert.equal(nodes["tts-install-logs"].open, true);
  assert.match(nodes["tts-install-detail"].textContent, /DLL/);
  installations.tts.blockers = ["需要Python 3.12"];
  await nodes["tts-refresh"].handlers.click();
  assert.equal(nodes["tts-install"].disabled, true);
  assert.match(nodes["tts-install-hint"].textContent, /3.12/);
  bridge.apiGet = async () => { throw new Error("未授权"); };
  await nodes["tts-refresh"].handlers.click();
  assert.match(nodes["tts-message"].textContent, /未授权/);
  assert.equal(nodes["tts-start"].disabled, true);
  const lastTaskTimer = timers.length;
  for (const callback of unload) callback();
  assert.ok(clearedTimers.includes(lastTaskTimer));
  console.log("PASS: service cards, config, task progress, native upload, guarded source deletion and timer cleanup");
}
main().catch(error => { console.error(error); process.exitCode = 1; });
