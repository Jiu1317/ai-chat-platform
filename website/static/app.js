const $ = (selector, root = document) => root.querySelector(selector);
const siteAccount = {
  id: document.body.dataset.siteUserId || "anonymous",
  username: document.body.dataset.siteUsername || "",
  isAdmin: document.body.dataset.siteAdmin === "true",
};
const legacyStorageKey = "ai-chat-state-v1";
const storageKey = `${legacyStorageKey}:${siteAccount.id}`;
const MEBIBYTE = 1024 * 1024;
const MAX_UPLOAD_BYTES = 30 * MEBIBYTE;
const UPLOAD_CHUNK_BYTES = 3 * MEBIBYTE;
const MAX_TURN_FILES = 5;
const INLINE_TEXT_BYTES = 60_000;
const utf8Encoder = new TextEncoder();
try {
  if (siteAccount.isAdmin && !localStorage.getItem(storageKey) && localStorage.getItem(legacyStorageKey)) {
    localStorage.setItem(storageKey, localStorage.getItem(legacyStorageKey));
  }
} catch {}
const effortNames = { default: "默认", low: "轻度", medium: "中", high: "高", xhigh: "极高", max: "最高", ultra: "Ultra" };
const androidShellVersion = navigator.userAgent.match(/AIChatAndroid\/([0-9.]+)/)?.[1] || "";
if (["1.0", "1.0.0", "1.0.1"].includes(androidShellVersion)) {
  document.documentElement.classList.add("legacy-android-insets");
}

const elements = {
  appShell: $(".app-shell"),
  sidebar: $("#sidebar"),
  scrim: $("#sidebar-scrim"),
  menuButton: $("#menu-button"),
  sidebarClose: $("#sidebar-close"),
  newChat: $("#new-chat"),
  newProject: $("#new-project"),
  allChats: $("#all-chats"),
  projectList: $("#project-list"),
  projectsEmpty: $("#projects-empty"),
  historyList: $("#history-list"),
  historyEmpty: $("#history-empty"),
  historyTitle: $("#history-title"),
  syncStatus: $("#sync-status"),
  historyMenu: $("#history-context-menu"),
  historyPinLabel: $("#history-pin-label"),
  historyDeleteLabel: $("#history-delete-label"),
  historyMenuStatus: $("#history-menu-status"),
  accountLabel: $("#account-label"),
  accountDetail: $("#account-detail"),
  accountAction: $("#account-action"),
  statusDot: $("#status-dot"),
  modelSelect: $("#model-select"),
  effortSelect: $("#effort-select"),
  modelTrigger: $("#model-trigger"),
  modelTriggerLabel: $("#model-trigger-label"),
  effortTrigger: $("#effort-trigger"),
  effortTriggerLabel: $("#effort-trigger-label"),
  picker: $("#mobile-picker"),
  pickerScrim: $("#picker-scrim"),
  pickerTitle: $("#picker-title"),
  pickerOptions: $("#picker-options"),
  pickerClose: $("#picker-close"),
  modelDescription: $("#model-description"),
  themeButton: $("#theme-button"),
  settingsButton: $("#settings-button"),
  settingsPanel: $("#settings-panel"),
  settingsScrim: $("#settings-scrim"),
  settingsClose: $("#settings-close"),
  siteUserForm: $("#site-user-form"),
  siteUserName: $("#site-user-name"),
  siteUserPassword: $("#site-user-password"),
  siteUserLimit: $("#site-user-limit"),
  siteUserExpiry: $("#site-user-expiry"),
  siteUserExpiryUnit: $("#site-user-expiry-unit"),
  siteUserAdmin: $("#site-user-admin"),
  siteUserCreate: $("#site-user-create"),
  siteUserStatus: $("#site-user-status"),
  siteUserList: $("#site-user-list"),
  siteUsersEmpty: $("#site-users-empty"),
  codexModelList: $("#codex-model-list"),
  codexModelsEmpty: $("#codex-models-empty"),
  codexModelStatus: $("#codex-model-status"),
  codexModelRefresh: $("#codex-model-refresh"),
  providerList: $("#provider-list"),
  providerEmpty: $("#provider-empty"),
  providerListStatus: $("#provider-list-status"),
  providerNew: $("#provider-new"),
  providerForm: $("#provider-form"),
  providerEditorTitle: $("#provider-editor-title"),
  providerId: $("#provider-id"),
  providerPreset: $("#provider-preset"),
  providerName: $("#provider-name"),
  providerBaseUrl: $("#provider-base-url"),
  providerTransportHint: $("#provider-transport-hint"),
  providerProtocol: $("#provider-protocol"),
  providerKey: $("#provider-key"),
  providerKeyHint: $("#provider-key-hint"),
  providerPriceCurrency: $("#provider-price-currency"),
  providerInputPrice: $("#provider-input-price"),
  providerOutputPrice: $("#provider-output-price"),
  providerEnabled: $("#provider-enabled"),
  providerSaveDetect: $("#provider-save-detect"),
  providerSaveOnly: $("#provider-save-only"),
  settingsStatus: $("#settings-status"),
  projectContext: $("#project-context"),
  projectContextName: $("#project-context-name"),
  projectContextMeta: $("#project-context-meta"),
  projectSettingsButton: $("#project-settings-button"),
  projectPanel: $("#project-panel"),
  projectScrim: $("#project-scrim"),
  projectClose: $("#project-close"),
  projectForm: $("#project-form"),
  projectName: $("#project-name"),
  projectInstructions: $("#project-instructions"),
  projectUseContext: $("#project-use-context"),
  projectStatus: $("#project-status"),
  projectFileInput: $("#project-file-input"),
  projectUploadCancel: $("#project-upload-cancel"),
  projectFileList: $("#project-file-list"),
  projectFilesEmpty: $("#project-files-empty"),
  modelImport: $("#model-import"),
  detectedModelCount: $("#detected-model-count"),
  detectedModels: $("#detected-models"),
  providerDetect: $("#provider-detect"),
  manualModels: $("#manual-models"),
  manualModelImport: $("#manual-model-import"),
  emptyState: $("#empty-state"),
  messageList: $("#message-list"),
  composer: $("#composer"),
  composerDropCue: $("#composer-drop-cue"),
  composerStatus: $("#composer-status"),
  input: $("#composer-input"),
  fileInput: $("#file-input"),
  attachmentList: $("#attachment-list"),
  sendButton: $("#send-button"),
  stopButton: $("#stop-button"),
  hint: $("#composer-hint"),
  quotaMonitor: $("#quota-monitor"),
  quotaRemaining: $("#quota-remaining"),
  quotaReset: $("#quota-reset"),
  latencyMonitor: $("#latency-monitor"),
  latencyValue: $("#latency-value"),
  dialog: $("#device-dialog"),
  deviceCode: $("#device-code"),
  verificationLink: $("#verification-link"),
  copyCode: $("#copy-code"),
  dialogStatus: $("#dialog-status"),
  messageTemplate: $("#message-template"),
};

const state = loadState();
let models = [];
let attachments = [];
let isSending = false;
let activeTurn = null;
let activeTurnRequest = null;
const uploadControllers = new Map();
let loginPoll = null;
let autoScrollEnabled = true;
let hasRenderedMessages = false;
let renderedConversationId = null;
let renderedMessageIds = new Set();
let pendingStreamingMessage = null;
let streamingRenderScheduled = false;
let sidebarCloseTimer = null;
let quotaLastFetched = 0;
let latencyLastFetched = 0;
let latencyProbeActive = false;
let accountConnected = false;
let historyMenuConversationId = null;
let historyDeleteConfirmTimer = null;
let renamingConversationId = null;
let composerDragDepth = 0;
let composerErrorTimer = null;
let activePickerSelect = null;
let pickerReturnFocus = null;
let pickerCloseTimer = null;
let cloudSyncReady = false;
let cloudSyncTimer = null;
let cloudSyncInFlight = false;
let cloudSyncQueued = false;
let cloudSyncFailureShown = false;
let lastCloudSignature = "";

let providers = [];
let siteUsers = [];
let codexAdminModels = [];
let providerPresets = [];
let providerFormDirty = false;
let settingsCloseTimer = null;
let settingsReturnFocus = null;
let projectCloseTimer = null;
let projectReturnFocus = null;
let activeProjectUploadController = null;
function prefersReducedMotion() {
  return matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function randomId() {
  if (crypto.randomUUID) return crypto.randomUUID().replaceAll("-", "");
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
}

function utf8ByteLength(value) {
  return utf8Encoder.encode(String(value || "")).byteLength;
}

function sumFileSizes(files) {
  return files.reduce((total, file) => total + Math.max(0, Number(file?.size) || 0), 0);
}

function longTextFileName() {
  const stamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\..+$/, "").replace("T", "-");
  return `message-${stamp}.txt`;
}

function normalizeTokenUsage(value) {
  if (!value || typeof value !== "object") return null;
  const clamp = (candidate) => Math.max(0, Math.min(1_000_000_000, Math.trunc(Number(candidate) || 0)));
  const inputTokens = clamp(value.inputTokens);
  const outputTokens = clamp(value.outputTokens);
  const totalTokens = clamp(value.totalTokens || inputTokens + outputTokens);
  if (!totalTokens) return null;
  return {
    inputTokens,
    outputTokens,
    totalTokens,
    estimated: Boolean(value.estimated),
  };
}

function normalizeCost(value) {
  if (!value || typeof value !== "object") return null;
  const kind = String(value.kind || "");
  if (kind === "subscription") {
    const amount = Number(value.amount);
    return {
      kind,
      amount: Number.isFinite(amount) && amount >= 0 ? amount : null,
      currency: "USD",
      model: String(value.model || "").slice(0, 100),
      usageEstimated: Boolean(value.usageEstimated),
    };
  }
  if (["unconfigured", "unavailable"].includes(kind)) return { kind };
  const amount = Number(value.amount);
  if (kind !== "estimated" || !Number.isFinite(amount) || amount < 0) return null;
  return {
    kind,
    amount,
    currency: value.currency === "USD" ? "USD" : "CNY",
    usageEstimated: Boolean(value.usageEstimated),
  };
}

function stripInternalAnnotations(value, { removeIncomplete = false } = {}) {
  let text = String(value || "").replace(/\uE200[^\uE200\uE201]{0,1000}\uE201/g, "");
  if (removeIncomplete) text = text.replace(/\uE200[^\uE200\uE201]{0,1000}$/g, "");
  return text;
}

function userFacingError(value, status = 0) {
  const message = String(value?.message || value || "").trim();
  const lowered = message.toLowerCase();
  if (Number(status) === 524 || /\b524\b/.test(lowered)) {
    return "请求等待超时（524）。官网可能仍在处理，请稍后检查会话后再重试";
  }
  if (/\bunauthorized\b|not authorized|authentication/.test(lowered)) {
    return "ChatGPT 网页登录已失效，请在专用浏览器重新登录后再试";
  }
  if (/image download failed/.test(lowered)) {
    return "图片已生成，但下载转发失败，请稍后重试";
  }
  if (/send not acknowledged/.test(lowered)) {
    return "官网未确认消息发送，可能页面繁忙或请求被拒绝，请稍后重试";
  }
  if (/spawn\s+openclaw\s+enoent/.test(lowered)) {
    return "图片处理组件未启动，请联系管理员重启服务";
  }
  if (/failed to fetch|networkerror|network request failed|load failed/.test(lowered)) {
    return "网络连接中断，请检查网络后重试";
  }
  return message || "请求失败，请稍后重试";
}

function normalizeConversation(item, fallbackWorkspaceId) {
  const updatedAt = Number(item?.updatedAt) || Date.now();
  const id = /^[a-f0-9]{32}$/.test(String(item?.id || "")) ? item.id : randomId();
  const workspaceId = /^[a-f0-9]{32}$/.test(String(item?.workspaceId || ""))
    ? item.workspaceId
    : fallbackWorkspaceId;
  return {
    ...item,
    id,
    workspaceId,
    title: String(item?.title || "新对话").trim().slice(0, 60) || "新对话",
    messages: Array.isArray(item?.messages) ? item.messages.map((message) => ({
      ...message,
      id: /^[a-f0-9]{32}$/.test(String(message?.id || "")) ? message.id : randomId(),
      content: message?.role === "assistant"
        ? stripInternalAnnotations(message?.content, { removeIncomplete: true })
        : String(message?.content || ""),
      createdAt: Number(message?.createdAt) || updatedAt,
      usage: normalizeTokenUsage(message?.usage),
      cost: normalizeCost(message?.cost),
    })) : [],
    updatedAt,
    pinned: Boolean(item?.pinned),
    pinnedAt: Number(item?.pinnedAt) || 0,
    projectId: /^[a-f0-9]{32}$/.test(String(item?.projectId || "")) ? item.projectId : null,
    externalConversationId: /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(String(item?.externalConversationId || ""))
      ? item.externalConversationId
      : null,
    externalContextKey: /^[a-f0-9]{64}$/.test(String(item?.externalContextKey || ""))
      ? item.externalContextKey
      : null,
    codexThreadIds: [...new Set([...(Array.isArray(item?.codexThreadIds) ? item.codexThreadIds : []),
      ...(item?.threadId ? [item.threadId] : [])])],
  };
}

function normalizeProject(item) {
  const updatedAt = Number(item?.updatedAt) || Date.now();
  return {
    id: /^[a-f0-9]{32}$/.test(String(item?.id || "")) ? item.id : randomId(),
    workspaceId: /^[a-f0-9]{32}$/.test(String(item?.workspaceId || "")) ? item.workspaceId : randomId(),
    name: String(item?.name || "新项目").trim().replace(/\s+/g, " ").slice(0, 60) || "新项目",
    instructions: String(item?.instructions || "").trim().slice(0, 12000),
    useContext: Boolean(item?.useContext),
    files: Array.isArray(item?.files) ? item.files.filter((file) => file && file.id && file.name).slice(0, 20) : [],
    createdAt: Number(item?.createdAt) || updatedAt,
    updatedAt,
  };
}

function loadState() {
  const fallback = {
    sessionId: randomId(),
    conversations: [],
    projects: [],
    activeId: null,
    activeProjectId: null,
    theme: matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light",
    model: "",
    effort: "medium",
  };
  try {
    const parsed = JSON.parse(localStorage.getItem(storageKey));
    if (!parsed || typeof parsed !== "object") return fallback;
    const sessionId = /^[a-f0-9]{32}$/.test(String(parsed.sessionId || ""))
      ? parsed.sessionId
      : fallback.sessionId;
    const conversations = Array.isArray(parsed.conversations)
      ? parsed.conversations.filter((item) => item && typeof item === "object")
        .map((item) => normalizeConversation(item, sessionId))
      : [];
    const projects = Array.isArray(parsed.projects)
      ? parsed.projects.filter((item) => item && typeof item === "object").map(normalizeProject)
      : [];
    const activeProjectId = projects.some((item) => item.id === parsed.activeProjectId)
      ? parsed.activeProjectId
      : null;
    delete parsed.preferLuna;
    return { ...fallback, ...parsed, sessionId, conversations, projects, activeProjectId };
  } catch {
    return fallback;
  }
}

function compactState({ messageLimit, contentLimit }) {
  return {...state, conversations: state.conversations.map((conversation) => ({
    ...conversation,
    messages: messageLimit > 0
      ? (conversation.messages || []).slice(-messageLimit)
        .map((message) => ({...message, content: String(message.content || "").slice(-contentLimit)}))
      : [],
  }))};
}

function persistLocalState() {
  try {
    localStorage.setItem(storageKey, JSON.stringify(state)); return true;
  } catch {
    try {
      localStorage.setItem(storageKey, JSON.stringify(compactState({ messageLimit: 10, contentLimit: 4000 })));
      showComposerError("聊天记录较多，已压缩本地历史"); return true;
    } catch {
      try {
        localStorage.setItem(storageKey, JSON.stringify(compactState({ messageLimit: 0, contentLimit: 0 })));
        showComposerError("本地聊天正文空间不足，已保留会话删除索引"); return true;
      } catch {
        showComposerError("浏览器存储不可用；当前聊天仍可继续"); return false;
      }
    }
  }
}

function saveState() {
  const saved = persistLocalState();
  scheduleCloudSync();
  return saved;
}

function cloudConversationSnapshot(conversation) {
  return {
    id: conversation.id,
    workspaceId: conversation.workspaceId || state.sessionId,
    threadId: conversation.threadId || null,
    codexThreadIds: Array.isArray(conversation.codexThreadIds) ? conversation.codexThreadIds : [],
    title: conversation.title || "新对话",
    messages: (conversation.messages || []).filter((message) => !message.streaming).map((message) => ({
      id: message.id,
      role: message.role,
      content: message.content || "",
      createdAt: Number(message.createdAt) || Number(conversation.updatedAt) || Date.now(),
      error: Boolean(message.error),
      mode: message.mode,
      attachments: message.attachments || [],
      files: message.files || [],
      usage: normalizeTokenUsage(message.usage),
      cost: normalizeCost(message.cost),
    })),
    updatedAt: Number(conversation.updatedAt) || Date.now(),
    pinned: Boolean(conversation.pinned),
    pinnedAt: Number(conversation.pinnedAt) || 0,
    backendKey: conversation.backendKey || null,
    externalConversationId: conversation.externalConversationId || null,
    externalContextKey: conversation.externalContextKey || null,
    projectId: conversation.projectId || null,
  };
}

function cloudProjectSnapshot(project) {
  return {
    id: project.id,
    workspaceId: project.workspaceId,
    name: project.name || "新项目",
    instructions: project.instructions || "",
    useContext: Boolean(project.useContext),
    files: (project.files || []).slice(0, 20).map(({ id, name, size, type }) => ({ id, name, size, type })),
    createdAt: Number(project.createdAt) || Number(project.updatedAt) || Date.now(),
    updatedAt: Number(project.updatedAt) || Date.now(),
  };
}

function cloudSnapshot() {
  return {
    conversations: [...state.conversations]
      .sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0))
      .slice(0, 200)
      .map(cloudConversationSnapshot),
    projects: [...state.projects]
      .sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0))
      .slice(0, 50)
      .map(cloudProjectSnapshot),
  };
}

function cloudSignature(snapshot = cloudSnapshot()) {
  return JSON.stringify(snapshot);
}

function setCloudSyncStatus(kind, text) {
  if (!elements.syncStatus) return;
  elements.syncStatus.textContent = text;
  elements.syncStatus.className = `sync-status is-${kind}`;
}

function scheduleCloudSync(delay = 450) {
  if (!cloudSyncReady) return;
  clearTimeout(cloudSyncTimer);
  cloudSyncTimer = setTimeout(() => syncCloudConversations(), delay);
}

async function syncCloudConversations({ force = false, initial = false } = {}) {
  if (cloudSyncInFlight) {
    cloudSyncQueued = true;
    return;
  }
  if (isSending || renamingConversationId || attachments.some((item) => item.loading)) {
    scheduleCloudSync(1200);
    return;
  }
  const outgoing = cloudSnapshot();
  const outgoingSignature = cloudSignature(outgoing);
  if (!force && outgoingSignature === lastCloudSignature) return;
  cloudSyncInFlight = true;
  if (initial) setCloudSyncStatus("syncing", "同步中");
  try {
    const response = await api("/api/conversations/sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: outgoingSignature,
    });
    const result = await response.json();
    const deleted = new Set(Array.isArray(result.deleted) ? result.deleted : []);
    const merged = new Map();
    for (const conversation of state.conversations) {
      if (!deleted.has(conversation.id)) merged.set(conversation.id, conversation);
    }
    let changed = merged.size !== state.conversations.length;
    for (const raw of Array.isArray(result.conversations) ? result.conversations : []) {
      const remote = normalizeConversation(raw, state.sessionId);
      const local = merged.get(remote.id);
      if (!local || remote.updatedAt > (Number(local.updatedAt) || 0)) {
        merged.set(remote.id, remote);
        changed = true;
      }
    }
    state.conversations = [...merged.values()];
    const projectDeleted = new Set(Array.isArray(result.projectDeleted) ? result.projectDeleted : []);
    const projectMerged = new Map();
    for (const project of state.projects) {
      if (!projectDeleted.has(project.id)) projectMerged.set(project.id, project);
    }
    if (projectMerged.size !== state.projects.length) changed = true;
    for (const raw of Array.isArray(result.projects) ? result.projects : []) {
      const remote = normalizeProject(raw);
      const local = projectMerged.get(remote.id);
      if (!local || remote.updatedAt > (Number(local.updatedAt) || 0)) {
        projectMerged.set(remote.id, remote);
        changed = true;
      }
    }
    state.projects = [...projectMerged.values()];
    if (state.activeProjectId && !state.projects.some((item) => item.id === state.activeProjectId)) {
      state.activeProjectId = null;
      changed = true;
    }
    if (!sortedConversations().some((item) => item.id === state.activeId)) {
      state.activeId = sortedConversations()[0]?.id || null;
      attachments = [];
      changed = true;
    }
    persistLocalState();
    lastCloudSignature = changed ? cloudSignature() : outgoingSignature;
    cloudSyncReady = true;
    cloudSyncFailureShown = false;
    setCloudSyncStatus("synced", "已同步");
    if (changed) {
      renderAll();
      renderAttachments();
    }
  } catch (error) {
    cloudSyncReady = true;
    setCloudSyncStatus("offline", "仅本机");
    if (!cloudSyncFailureShown) {
      showComposerError(`云同步暂不可用：${error.message}`);
      cloudSyncFailureShown = true;
    }
  } finally {
    cloudSyncInFlight = false;
    if (cloudSyncQueued) {
      cloudSyncQueued = false;
      scheduleCloudSync(80);
    }
  }
}

function activeConversation() {
  return state.conversations.find((item) => item.id === state.activeId) || null;
}

function activeProject() {
  return state.projects.find((item) => item.id === state.activeProjectId) || null;
}

function sortedConversations(projectId = state.activeProjectId) {
  return state.conversations.filter((conversation) => (
    projectId ? conversation.projectId === projectId : !conversation.projectId
  )).sort((a, b) => {
    if (Boolean(a.pinned) !== Boolean(b.pinned)) return Number(Boolean(b.pinned)) - Number(Boolean(a.pinned));
    if (a.pinned && b.pinned && a.pinnedAt !== b.pinnedAt) return (b.pinnedAt || 0) - (a.pinnedAt || 0);
    return (b.updatedAt || 0) - (a.updatedAt || 0);
  });
}

function selectProject(projectId) {
  if (isSending || attachments.some((item) => item.loading)) return;
  state.activeProjectId = projectId || null;
  const conversations = sortedConversations();
  state.activeId = conversations[0]?.id || null;
  attachments = [];
  autoScrollEnabled = true;
  saveState();
  renderAll();
  renderAttachments();
  closeHistoryMenu();
  closeSidebar();
}

function createProject() {
  if (state.projects.length >= 50) {
    showComposerError("项目数量已达到 50 个，请先整理现有项目。");
    return null;
  }
  const now = Date.now();
  const project = normalizeProject({
    id: randomId(),
    workspaceId: randomId(),
    name: "新项目",
    instructions: "",
    useContext: false,
    files: [],
    createdAt: now,
    updatedAt: now,
  });
  state.projects.unshift(project);
  state.activeProjectId = project.id;
  state.activeId = null;
  attachments = [];
  saveState();
  renderAll();
  renderAttachments();
  openProjectPanel(project);
  closeSidebar();
  projectReturnFocus = elements.projectSettingsButton;
  return project;
}

function createConversation() {
  const project = activeProject();
  const conversation = {
    id: randomId(),
    workspaceId: project?.workspaceId || randomId(),
    projectId: project?.id || null,
    threadId: null,
    externalConversationId: null,
    externalContextKey: null,
    codexThreadIds: [],
    title: "新对话",
    messages: [],
    updatedAt: Date.now(),
    pinned: false,
    pinnedAt: 0,
  };
  state.conversations.unshift(conversation);
  state.activeId = conversation.id;
  if (project) project.updatedAt = Date.now();
  autoScrollEnabled = true;
  saveState();
  renderAll();
  elements.input.focus();
  return conversation;
}

function renderAll() {
  document.documentElement.dataset.theme = state.theme;
  renderProjects();
  renderHistory();
  renderProjectContext();
  renderMessages();
  updateComposer();
}

function renderProjects() {
  const projects = [...state.projects].sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
  const conversationCounts = new Map();
  for (const conversation of state.conversations) {
    if (!conversation.projectId) continue;
    conversationCounts.set(
      conversation.projectId,
      (conversationCounts.get(conversation.projectId) || 0) + 1,
    );
  }
  const fragment = document.createDocumentFragment();
  elements.projectsEmpty.hidden = projects.length > 0;
  elements.allChats.classList.toggle("is-active", !state.activeProjectId);
  for (const project of projects) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "project-item";
    button.classList.toggle("is-active", project.id === state.activeProjectId);
    button.setAttribute("aria-current", project.id === state.activeProjectId ? "page" : "false");
    button.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7h6l2 2h10v10H3Z"/></svg>';
    const copy = document.createElement("span");
    const name = document.createElement("strong");
    name.textContent = project.name;
    const count = conversationCounts.get(project.id) || 0;
    const meta = document.createElement("small");
    meta.textContent = `${count} 个聊天 · ${project.files.length} 个文件`;
    copy.append(name, meta);
    button.append(copy);
    button.addEventListener("click", () => selectProject(project.id));
    fragment.append(button);
  }
  elements.projectList.replaceChildren(fragment);
}

function renderProjectContext() {
  const project = activeProject();
  document.body.classList.toggle("has-project", Boolean(project));
  elements.projectContext.hidden = !project;
  if (!project) {
    elements.emptyState.querySelector("h1").textContent = "有什么可以帮忙的？";
    return;
  }
  elements.projectContextName.textContent = project.name;
  const chatCount = state.conversations.filter((item) => item.projectId === project.id).length;
  elements.projectContextMeta.textContent = `${chatCount} 个聊天 · ${project.files.length} 个共享文件 · 上下文${project.useContext ? "已开启" : "未开启"}`;
  elements.emptyState.querySelector("h1").textContent = `${project.name}，想做什么？`;
}

function renderHistory() {
  const sorted = sortedConversations();
  const project = activeProject();
  const fragment = document.createDocumentFragment();
  elements.historyTitle.textContent = project ? "项目聊天" : "最近";
  elements.historyEmpty.hidden = sorted.length > 0;
  elements.historyEmpty.textContent = project
    ? "这个项目还没有聊天，点击“新对话”开始。"
    : "聊天会在已登录设备间同步。";
  for (const conversation of sorted) {
    const row = document.createElement("div");
    row.className = "history-row";
    row.classList.toggle("is-active", conversation.id === state.activeId);
    row.dataset.conversationId = conversation.id;

    if (conversation.id === renamingConversationId) {
      const form = document.createElement("form");
      form.className = "history-rename";
      const input = document.createElement("input");
      input.type = "text";
      input.value = conversation.title;
      input.maxLength = 60;
      input.setAttribute("aria-label", "聊天名称");
      form.append(input);
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        commitConversationRename(conversation.id, input.value);
      });
      input.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          renamingConversationId = null;
          renderHistory();
        }
      });
      input.addEventListener("blur", () => {
        setTimeout(() => {
          if (renamingConversationId === conversation.id) commitConversationRename(conversation.id, input.value);
        }, 0);
      });
      row.append(form);
      fragment.append(row);
      requestAnimationFrame(() => {
        input.focus();
        input.select();
      });
      continue;
    }

    const button = document.createElement("button");
    button.type = "button";
    button.className = "history-item";
    button.setAttribute("aria-current", conversation.id === state.activeId ? "page" : "false");
    button.title = conversation.title;
    const title = document.createElement("span");
    title.className = "history-title";
    title.textContent = conversation.title;
    button.append(title);
    if (conversation.pinned) {
      const pin = document.createElement("span");
      pin.className = "history-pin";
      pin.setAttribute("aria-label", "已置顶");
      pin.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m14 4 6 6-3 1-4 4-1 5-3-6-5-5 5-1 4-4Z"/></svg>';
      button.append(pin);
    }
    button.addEventListener("click", () => {
      if (isSending) return;
      if (attachments.some((item) => item.loading)) {
        showComposerError("请等待文件上传完成后再切换聊天");
        return;
      }
      state.activeId = conversation.id;
      attachments = [];
      saveState();
      renderAll();
      renderAttachments();
      closeHistoryMenu();
      closeSidebar();
    });
    button.addEventListener("keydown", (event) => {
      if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) {
        event.preventDefault();
        const rect = button.getBoundingClientRect();
        openHistoryMenu(conversation, rect.left + 18, rect.bottom - 4);
      }
    });

    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "history-menu-trigger";
    trigger.setAttribute("aria-label", `管理聊天：${conversation.title}`);
    trigger.setAttribute("aria-haspopup", "menu");
    trigger.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/></svg>';
    trigger.addEventListener("click", () => {
      const rect = trigger.getBoundingClientRect();
      openHistoryMenu(conversation, rect.right - 4, rect.bottom + 4);
    });
    row.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      openHistoryMenu(conversation, event.clientX, event.clientY);
    });
    row.append(button, trigger);
    fragment.append(row);
  }
  elements.historyList.replaceChildren(fragment);
}

function commitConversationRename(conversationId, rawTitle) {
  const conversation = state.conversations.find((item) => item.id === conversationId);
  if (!conversation) return;
  const title = rawTitle.trim().replace(/\s+/g, " ").slice(0, 60);
  if (title && title !== conversation.title) {
    conversation.title = title;
    conversation.updatedAt = Date.now();
  }
  renamingConversationId = null;
  saveState();
  renderHistory();
}

function beginConversationRename(conversationId) {
  if (isSending) return;
  renamingConversationId = conversationId;
  closeHistoryMenu();
  renderHistory();
}

function closeHistoryMenu() {
  clearTimeout(historyDeleteConfirmTimer);
  historyDeleteConfirmTimer = null;
  historyMenuConversationId = null;
  elements.historyMenu.hidden = true;
  elements.historyMenuStatus.hidden = true;
  elements.historyMenuStatus.textContent = "";
  elements.historyDeleteLabel.textContent = "删除";
  const deleteButton = elements.historyMenu.querySelector('[data-history-action="delete"]');
  deleteButton.dataset.confirming = "";
  deleteButton.classList.remove("is-confirming");
}

function openHistoryMenu(conversation, x, y) {
  closeHistoryMenu();
  historyMenuConversationId = conversation.id;
  elements.historyPinLabel.textContent = conversation.pinned ? "取消置顶" : "置顶";
  const uploadInProgress = attachments.some((item) => item.loading);
  for (const button of elements.historyMenu.querySelectorAll("button")) {
    button.disabled = isSending || (button.dataset.historyAction === "delete" && uploadInProgress);
  }
  elements.historyMenu.hidden = false;
  elements.historyMenu.style.left = "0";
  elements.historyMenu.style.top = "0";
  const rect = elements.historyMenu.getBoundingClientRect();
  const left = Math.max(8, Math.min(x, document.documentElement.clientWidth - rect.width - 8));
  const top = Math.max(8, Math.min(y, document.documentElement.clientHeight - rect.height - 8));
  elements.historyMenu.style.left = `${left}px`;
  elements.historyMenu.style.top = `${top}px`;
  elements.historyMenu.querySelector("button:not(:disabled)")?.focus();
}

async function deleteConversation(conversationId) {
  if (isSending || attachments.some((item) => item.loading)) return;
  const conversation = state.conversations.find((item) => item.id === conversationId);
  if (!conversation) return;
  const workspaceId = conversation.workspaceId || state.sessionId;
  const workspaceIsShared = state.projects.some((item) => item.workspaceId === workspaceId)
    || state.conversations.some(
    (item) => item.id !== conversation.id && (item.workspaceId || state.sessionId) === workspaceId,
  );
  const buttons = [...elements.historyMenu.querySelectorAll("button")];
  buttons.forEach((button) => { button.disabled = true; });
  elements.historyDeleteLabel.textContent = "正在删除…";
  elements.historyMenuStatus.hidden = false;
  elements.historyMenuStatus.textContent = "正在清理服务器会话数据";
  const threadIds = [...new Set([
    ...(Array.isArray(conversation.codexThreadIds) ? conversation.codexThreadIds : []),
    ...(conversation.threadId ? [conversation.threadId] : []),
  ])];
  try {
    for (let index = 0; index < threadIds.length; index += 20) {
      const chunk = threadIds.slice(index, index + 20);
      await api("/api/conversations/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace_id: workspaceId, thread_ids: chunk, delete_workspace: false }),
      });
      const remaining = new Set(threadIds.slice(index + 20));
      conversation.codexThreadIds = [...remaining];
      if (conversation.threadId && !remaining.has(conversation.threadId)) conversation.threadId = null;
      saveState();
    }
    await api("/api/conversations/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        conversation_id: conversation.id,
        workspace_id: workspaceId,
        thread_ids: [],
        delete_workspace: !workspaceIsShared,
      }),
    });
    const wasActive = state.activeId === conversation.id;
    state.conversations = state.conversations.filter((item) => item.id !== conversation.id);
    if (wasActive) {
      state.activeId = sortedConversations()[0]?.id || null;
      attachments = [];
      renderAttachments();
    }
    saveState();
    closeHistoryMenu();
    renderAll();
  } catch (error) {
    buttons.forEach((button) => { button.disabled = false; });
    elements.historyDeleteLabel.textContent = "重试删除";
    elements.historyMenuStatus.textContent = error.message;
  }
}

function renderMessages() {
  const conversation = activeConversation();
  const messages = conversation?.messages || [];
  const conversationChanged = renderedConversationId !== null && conversation?.id !== renderedConversationId;
  const hadMessages = document.body.classList.contains("has-messages");
  const willHaveMessages = messages.length > 0;
  const animateComposer = hasRenderedMessages
    && hadMessages !== willHaveMessages
    && !prefersReducedMotion();
  const composerWrap = animateComposer ? elements.composer.closest(".composer-wrap") : null;
  const previousComposerTop = composerWrap?.getBoundingClientRect().top || 0;
  const fragment = document.createDocumentFragment();
  document.body.classList.toggle("has-messages", willHaveMessages);
  elements.emptyState.hidden = messages.length > 0;
  messages.forEach((message, index) => {
    const node = buildMessage(message, conversation?.workspaceId || state.sessionId);
    if (hasRenderedMessages && (conversationChanged || !renderedMessageIds.has(message.id))) {
      node.classList.add("is-entering");
      node.style.setProperty("--enter-delay", `${conversationChanged ? Math.min(index * 24, 96) : 0}ms`);
    }
    fragment.append(node);
  });
  elements.messageList.replaceChildren(fragment);
  if (hasRenderedMessages && conversationChanged && !willHaveMessages && !prefersReducedMotion()) {
    elements.emptyState.animate(
      [{ opacity: 0.45, filter: "blur(1px)" }, { opacity: 1, filter: "blur(0)" }],
      { duration: 220, easing: "cubic-bezier(.16,1,.3,1)" },
    );
  }
  if (animateComposer && composerWrap) {
    const nextComposerTop = composerWrap.getBoundingClientRect().top;
    composerWrap.animate(
      [{ transform: `translateY(${previousComposerTop - nextComposerTop}px)` }, { transform: "translateY(0)" }],
      { duration: 320, easing: "cubic-bezier(.2,.8,.2,1)" },
    );
  }
  hasRenderedMessages = true;
  renderedConversationId = conversation?.id || null;
  renderedMessageIds = new Set(messages.map((message) => message.id));
  if (autoScrollEnabled) {
    requestAnimationFrame(() => window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "auto" }));
  }
}

async function copyText(text) {
  const value = String(text || "");
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
      return;
    }
  } catch (_error) {
    // Use the selection fallback below when clipboard permission is unavailable.
  }
  const textArea = document.createElement("textarea");
  textArea.value = value;
  textArea.setAttribute("readonly", "");
  textArea.style.position = "fixed";
  textArea.style.opacity = "0";
  document.body.append(textArea);
  textArea.select();
  const copied = document.execCommand("copy");
  textArea.remove();
  if (!copied) throw new Error("COPY_FAILED");
}

function buildMessage(message, workspaceId) {
  const node = elements.messageTemplate.content.firstElementChild.cloneNode(true);
  const isThinking = message.role === "assistant" && Boolean(message.streaming) && !String(message.content || "").trim();
  node.dataset.role = message.role;
  node.dataset.messageId = message.id;
  node.classList.toggle("is-streaming", Boolean(message.streaming));
  node.classList.toggle("is-thinking", isThinking);
  node.classList.toggle("is-error", Boolean(message.error));
  node.classList.toggle("is-image-response", message.mode === "image");
  $(".message-role", node).textContent = message.role === "user" ? "你" : "AI Chat";
  const body = $(".message-body", node);
  if (isThinking) {
    const thinking = document.createElement("span");
    thinking.className = "thinking-indicator";
    thinking.setAttribute("role", "status");
    thinking.textContent = "正在思考中......";
    body.replaceChildren(thinking);
  } else {
    body.innerHTML = message.role === "assistant" ? renderMarkdown(message.content) : renderPlain(message.content);
  }
  const actions = $(".message-actions", node);
  const copyButton = $(".copy-button", node);
  const shareButton = $(".share-button", node);
  const usageLabel = $(".message-usage", node);
  const actionStatus = $(".message-action-status", node);
  const answerText = String(message.content || "");
  actions.hidden = message.role !== "assistant" || Boolean(message.streaming) || Boolean(message.error) || !answerText.trim();
  const usage = normalizeTokenUsage(message.usage);
  const cost = normalizeCost(message.cost);
  const metadata = [];
  const details = [];
  if (usage) {
    const prefix = usage.estimated ? "约 " : "";
    metadata.push(`${prefix}${usage.totalTokens.toLocaleString("zh-CN")} tokens`);
    details.push(...[
      usage.estimated ? "估算用量" : "接口返回的实际用量",
      usage.inputTokens ? `输入 ${usage.inputTokens.toLocaleString("zh-CN")}` : "",
      usage.outputTokens ? `输出 ${usage.outputTokens.toLocaleString("zh-CN")}` : "",
    ].filter(Boolean));
  }
  if (cost?.kind === "estimated") {
    const symbol = cost.currency === "USD" ? "$" : "¥";
    const digits = cost.amount === 0 ? 2 : cost.amount < 0.0001 ? 6 : cost.amount < 0.01 ? 4 : 2;
    metadata.push(`预计 ${symbol}${cost.amount.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`);
    details.push(`按设置中的输入和输出单价估算${cost.usageEstimated ? "，Token 用量也为估算值" : ""}`);
  } else if (cost?.kind === "subscription") {
    if (cost.amount !== null) {
      const digits = cost.amount === 0 ? 2 : cost.amount < 0.0001 ? 6 : cost.amount < 0.01 ? 4 : 2;
      const amount = cost.amount.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
      metadata.push(`API 等值约 $${amount} · 订阅额度内`);
      details.push(`按 ${cost.model || "当前模型"} 的官方 API 输入与输出单价折算${cost.usageEstimated ? "，Token 用量为估算值" : ""}；未扣除缓存优惠，也不是实际扣款`);
    } else {
      metadata.push("订阅额度内 · 无公开等值价");
      details.push("当前模型没有可匹配的公开 API 单价，因此不显示金额");
    }
  } else if (cost?.kind === "unconfigured") {
    metadata.push("费用待设置");
    details.push("请在 API 设置中填写输入和输出单价");
  } else if (cost?.kind === "unavailable") {
    metadata.push("费用暂不可估算");
    details.push("接口未返回可用于计价的输入与输出用量");
  }
  if (metadata.length) {
    usageLabel.textContent = metadata.join(" · ");
    const detail = details.join(" · ");
    usageLabel.title = detail;
    usageLabel.setAttribute("aria-label", detail || metadata.join("，"));
    usageLabel.hidden = false;
  }
  let actionStatusTimer = null;
  const showActionStatus = (text) => {
    clearTimeout(actionStatusTimer);
    actionStatus.textContent = text;
    actionStatusTimer = setTimeout(() => { actionStatus.textContent = ""; }, 1800);
  };
  copyButton.addEventListener("click", async () => {
    try {
      await copyText(answerText);
      showActionStatus("已复制");
    } catch (_error) {
      showActionStatus("复制失败，请重试");
    }
  });
  shareButton.addEventListener("click", async () => {
    try {
      if (navigator.share) {
        await navigator.share({ title: "AI Chat 回答", text: answerText });
        showActionStatus("已分享");
        return;
      }
      await copyText(answerText);
      showActionStatus("内容已复制，可粘贴分享");
    } catch (error) {
      if (error?.name === "AbortError") return;
      try {
        await copyText(answerText);
        showActionStatus("无法直接分享，内容已复制");
      } catch (_copyError) {
        showActionStatus("分享失败，请重试");
      }
    }
  });
  const files = $(".message-files", node);
  for (const file of message.files || []) {
    const encodedPath = file.path.split("/").map(encodeURIComponent).join("/");
    const fileUrl = `/api/files/${workspaceId}/${encodedPath}`;
    const isImageFile = String(file.mediaType || "").startsWith("image/")
      || /\.(?:avif|gif|jpe?g|png|webp)$/i.test(String(file.name || file.path || ""));
    const isImage = isImageFile;
    if (isImage) {
      const figure = document.createElement("figure");
      figure.className = "generated-image";
      const previewLink = document.createElement("a");
      previewLink.className = "generated-image-preview";
      previewLink.href = `${fileUrl}?variant=original&inline=true`;
      previewLink.target = "_blank";
      previewLink.rel = "noopener noreferrer";
      previewLink.setAttribute("aria-label", `查看原图：${file.name}`);
      if (Number(file.width) > 0 && Number(file.height) > 0) {
        previewLink.style.aspectRatio = `${Number(file.width)} / ${Number(file.height)}`;
      }
      const image = document.createElement("img");
      image.src = `${fileUrl}?variant=preview&inline=true`;
      image.alt = "由 AI Chat 生成的图片";
      image.loading = "lazy";
      image.decoding = "async";
      image.addEventListener("load", () => previewLink.classList.add("is-loaded"), { once: true });
      image.addEventListener("error", () => {
        if (!image.dataset.originalFallback) {
          image.dataset.originalFallback = "true";
          image.src = `${fileUrl}?variant=original&inline=true`;
          return;
        }
        previewLink.classList.add("is-error");
      });
      previewLink.append(image);
      const caption = document.createElement("figcaption");
      const compressedDownload = document.createElement("a");
      compressedDownload.className = "generated-image-download is-primary";
      compressedDownload.href = `${fileUrl}?variant=compressed`;
      compressedDownload.download = `${file.name.replace(/\.[^.]+$/, "")}-高清.webp`;
      compressedDownload.textContent = file.compressedSize
        ? `下载高清版 · ${formatBytes(file.compressedSize)}`
        : "下载高清版";
      const originalDownload = document.createElement("a");
      originalDownload.className = "generated-image-download";
      originalDownload.href = `${fileUrl}?variant=original`;
      originalDownload.download = file.name;
      originalDownload.textContent = `下载原图 · ${formatBytes(file.size)}`;
      caption.append(compressedDownload, originalDownload);
      figure.append(previewLink, caption);
      files.append(figure);
      continue;
    }
    const link = document.createElement("a");
    link.href = fileUrl;
    link.textContent = `下载 ${file.name} · ${formatBytes(file.size)}`;
    files.append(link);
  }
  if (message.role === "user" && message.attachments?.length) {
    for (const attachment of message.attachments) {
      const tag = document.createElement("span");
      tag.className = "attachment-chip";
      const name = document.createElement("span");
      name.textContent = attachment.name;
      tag.append(name);
      files.append(tag);
    }
  }
  return node;
}

function renderPlain(text) {
  return `<p>${escapeHtml(text).replaceAll("\n", "<br>")}</p>`;
}

function renderMarkdown(source) {
  const escaped = escapeHtml(stripInternalAnnotations(source, { removeIncomplete: true }));
  const codeBlocks = [];
  let text = escaped.replace(/\`\`\`([\w+-]*)\n?([\s\S]*?)\`\`\`/g, (_, language, code) => {
    const key = `@@CODE${codeBlocks.length}@@`;
    codeBlocks.push(`<pre data-language="${language || "text"}"><code>${code.trimEnd()}</code></pre>`);
    return key;
  });
  text = text
    .replace(/^### (.+)$/gm, "<h3>$1</h3>")
    .replace(/^## (.+)$/gm, "<h2>$1</h2>")
    .replace(/^# (.+)$/gm, "<h1>$1</h1>")
    .replace(/^&gt; (.+)$/gm, "<blockquote>$1</blockquote>")
    .replace(/^[-*] (.+)$/gm, "<li>$1</li>")
    .replace(/((?:<li>.*<\/li>\n?)+)/g, "<ul>$1</ul>")
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\`([^\`]+)\`/g, "<code>$1</code>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  const blocks = text.split(/\n{2,}/).map((block) => {
    const trimmed = block.trim();
    if (!trimmed) return "";
    if (/^@@CODE\d+@@$/.test(trimmed) || /^<(h\d|ul|blockquote)/.test(trimmed)) return trimmed;
    return `<p>${trimmed.replaceAll("\n", "<br>")}</p>`;
  }).join("");
  return codeBlocks.reduce((result, code, index) => result.replace(`@@CODE${index}@@`, code), blocks);
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function updateComposer() {
  const ready = Boolean(state.model) && !isSending;
  const readyAttachments = attachments.filter((item) => !item.loading && !item.error);
  const attachmentsBlocked = attachments.some((item) => item.loading || item.error);
  elements.sendButton.disabled = !ready || attachmentsBlocked || (!elements.input.value.trim() && readyAttachments.length === 0);
  elements.modelSelect.disabled = models.length === 0 || isSending;
  elements.effortSelect.disabled = models.length === 0 || isSending;
  elements.fileInput.disabled = isSending || attachments.some((item) => item.loading);
  updatePickerTriggers();
}

function resizeInput() {
  elements.input.style.height = "auto";
  elements.input.style.height = `${Math.min(elements.input.scrollHeight, 180)}px`;
}

function updatePickerTriggers() {
  const pairs = [
    [elements.modelSelect, elements.modelTrigger, elements.modelTriggerLabel],
    [elements.effortSelect, elements.effortTrigger, elements.effortTriggerLabel],
  ];
  for (const [select, trigger, label] of pairs) {
    const selected = select.selectedOptions?.[0] || select.options?.[0];
    label.textContent = selected?.textContent?.trim() || "暂不可用";
    trigger.disabled = select.disabled;
  }
}

function appendPickerOption(option) {
  const button = document.createElement("button");
  const selected = option.value === activePickerSelect.value;
  button.className = "picker-option";
  button.type = "button";
  button.role = "option";
  button.setAttribute("aria-selected", String(selected));
  button.disabled = option.disabled;

  const label = document.createElement("span");
  label.textContent = option.textContent;
  const radio = document.createElement("span");
  radio.className = "picker-radio";
  radio.setAttribute("aria-hidden", "true");
  radio.append(document.createElement("i"));
  button.append(label, radio);
  button.addEventListener("click", () => {
    activePickerSelect.value = option.value;
    activePickerSelect.dispatchEvent(new Event("change", { bubbles: true }));
    updatePickerTriggers();
    closeMobilePicker();
  });
  elements.pickerOptions.append(button);
}

function renderPickerOptions() {
  elements.pickerOptions.replaceChildren();
  for (const child of activePickerSelect.children) {
    if (child.tagName === "OPTGROUP") {
      const group = document.createElement("p");
      group.className = "picker-group";
      group.textContent = child.label;
      elements.pickerOptions.append(group);
      for (const option of child.children) appendPickerOption(option);
    } else if (child.tagName === "OPTION") {
      appendPickerOption(child);
    }
  }
}

function openMobilePicker(select, title, trigger) {
  if (select.disabled) return;
  clearTimeout(pickerCloseTimer);
  activePickerSelect = select;
  pickerReturnFocus = trigger;
  elements.pickerTitle.textContent = title;
  renderPickerOptions();
  elements.picker.hidden = false;
  elements.pickerScrim.hidden = false;
  elements.picker.setAttribute("aria-hidden", "false");
  document.body.classList.add("picker-open");
  requestAnimationFrame(() => {
    elements.picker.classList.add("is-visible");
    elements.pickerScrim.classList.add("is-visible");
    elements.pickerOptions.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" });
    elements.pickerClose.focus();
  });
}

function closeMobilePicker({ restoreFocus = true } = {}) {
  if (elements.picker.hidden) return;
  elements.picker.classList.remove("is-visible");
  elements.pickerScrim.classList.remove("is-visible");
  elements.picker.setAttribute("aria-hidden", "true");
  document.body.classList.remove("picker-open");
  const finish = () => {
    elements.picker.hidden = true;
    elements.pickerScrim.hidden = true;
    activePickerSelect = null;
    if (restoreFocus) pickerReturnFocus?.focus();
    pickerReturnFocus = null;
  };
  clearTimeout(pickerCloseTimer);
  if (prefersReducedMotion()) finish();
  else pickerCloseTimer = setTimeout(finish, 180);
}

function openSidebar() {
  clearTimeout(sidebarCloseTimer);
  elements.scrim.hidden = false;
  requestAnimationFrame(() => elements.scrim.classList.add("is-visible"));
  elements.sidebar.classList.add("is-open");
  elements.menuButton.setAttribute("aria-expanded", "true");
}

function closeSidebar() {
  elements.sidebar.classList.remove("is-open");
  elements.scrim.classList.remove("is-visible");
  elements.menuButton.setAttribute("aria-expanded", "false");
  clearTimeout(sidebarCloseTimer);
  if (prefersReducedMotion()) {
    elements.scrim.hidden = true;
  } else {
    sidebarCloseTimer = setTimeout(() => { elements.scrim.hidden = true; }, 180);
  }
}

function setSettingsStatus(message = "", tone = "") {
  elements.settingsStatus.textContent = message;
  elements.settingsStatus.className = `settings-status${tone ? ` is-${tone}` : ""}`;
}

function setProviderListStatus(message = "", tone = "") {
  elements.providerListStatus.textContent = message;
  elements.providerListStatus.className = `settings-status provider-list-status${tone ? ` is-${tone}` : ""}`;
}

function setSiteUserStatus(message = "", tone = "") {
  elements.siteUserStatus.textContent = message;
  elements.siteUserStatus.className = `settings-status${tone ? ` is-${tone}` : ""}`;
}

function setCodexModelStatus(message = "", tone = "") {
  elements.codexModelStatus.textContent = message;
  elements.codexModelStatus.className = `settings-status codex-model-status${tone ? ` is-${tone}` : ""}`;
}

function renderCodexAdminModels() {
  elements.codexModelList.replaceChildren();
  elements.codexModelsEmpty.hidden = codexAdminModels.length > 0;
  if (!codexAdminModels.length && !elements.codexModelsEmpty.textContent) {
    elements.codexModelsEmpty.textContent = "暂未读取到 Codex 模型。";
  }
  for (const model of codexAdminModels) {
    const row = document.createElement("div");
    row.className = `codex-model-row${model.enabled ? "" : " is-disabled"}`;
    const copy = document.createElement("div");
    copy.className = "codex-model-copy";
    const name = document.createElement("strong");
    name.textContent = model.displayName || model.id;
    const detail = document.createElement("small");
    detail.textContent = `${model.id}${model.isDefault ? " · 默认模型" : ""}`;
    copy.append(name, detail);

    const control = document.createElement("label");
    control.className = "codex-model-toggle";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = model.enabled;
    input.setAttribute("aria-label", `${model.enabled ? "关闭" : "开启"} Codex 模型 ${model.displayName || model.id}`);
    const stateLabel = document.createElement("span");
    stateLabel.textContent = model.enabled ? "已开启" : "已关闭";
    control.append(input, stateLabel);
    input.addEventListener("change", () => toggleCodexAdminModel(model.id, input.checked, input));
    row.append(copy, control);
    elements.codexModelList.append(row);
  }
}

async function loadCodexAdminModels() {
  elements.codexModelRefresh.disabled = true;
  elements.codexModelsEmpty.hidden = true;
  setCodexModelStatus("正在读取 Codex 模型…");
  try {
    const response = await api("/api/admin/codex-models");
    const result = await response.json();
    codexAdminModels = Array.isArray(result.models) ? result.models : [];
    elements.codexModelsEmpty.textContent = "暂未读取到 Codex 模型。";
    renderCodexAdminModels();
    const disabledCount = codexAdminModels.filter((model) => !model.enabled).length;
    setCodexModelStatus(disabledCount ? `已关闭 ${disabledCount} 个 Codex 模型。` : "全部 Codex 模型均已开启。", "success");
  } catch (error) {
    codexAdminModels = [];
    elements.codexModelList.replaceChildren();
    elements.codexModelsEmpty.textContent = "Codex 模型暂时不可用，可点击刷新重试。";
    elements.codexModelsEmpty.hidden = false;
    setCodexModelStatus(error.message, "error");
  } finally {
    elements.codexModelRefresh.disabled = false;
  }
}

async function toggleCodexAdminModel(modelId, enabled, control) {
  const model = codexAdminModels.find((item) => item.id === modelId);
  const row = control.closest(".codex-model-row");
  control.disabled = true;
  row?.classList.add("is-saving");
  setCodexModelStatus(`正在${enabled ? "开启" : "关闭"}“${model?.displayName || modelId}”…`);
  try {
    const response = await api("/api/admin/codex-models", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: modelId, enabled }),
    });
    const result = await response.json();
    codexAdminModels = codexAdminModels.map((item) => item.id === modelId ? { ...item, enabled: result.enabled } : item);
    renderCodexAdminModels();
    setCodexModelStatus(`“${model?.displayName || modelId}”已${enabled ? "开启" : "关闭"}。`, "success");
    await loadModels(accountConnected);
  } catch (error) {
    control.checked = !enabled;
    control.disabled = false;
    row?.classList.remove("is-saving");
    setCodexModelStatus(`保存失败：${error.message}`, "error");
  }
}

function setSiteUserBusy(busy) {
  elements.siteUserCreate.disabled = busy;
  elements.siteUserList.querySelectorAll("button, input, select").forEach((control) => {
    control.disabled = busy || control.dataset.locked === "true";
  });
}

function updateSiteUserExpiryConstraints() {
  const usesHours = elements.siteUserExpiryUnit.value === "hours";
  elements.siteUserExpiry.max = usesHours ? "87600" : "3650";
}

function siteUserExpiryState(user) {
  if (user.expiresAt === null || user.expiresAt === undefined || user.expiresAt === "") {
    return { hasExpiry: false, expired: false, days: null, label: "" };
  }
  const expiresAt = Number(user.expiresAt);
  if (!Number.isFinite(expiresAt) || expiresAt <= 0) {
    return { hasExpiry: false, expired: false, days: null, label: "" };
  }
  const remainingMs = expiresAt * 1000 - Date.now();
  if (remainingMs <= 0) {
    return { hasExpiry: true, expired: true, label: "期限已到" };
  }
  const hours = Math.max(1, Math.ceil(remainingMs / (60 * 60 * 1000)));
  if (hours <= 48) {
    return { hasExpiry: true, expired: false, label: `剩余 ${hours} 小时停用` };
  }
  const days = Math.max(1, Math.ceil(remainingMs / (24 * 60 * 60 * 1000)));
  return { hasExpiry: true, expired: false, label: `剩余 ${days} 天停用` };
}

function renderSiteUsers() {
  elements.siteUserList.replaceChildren();
  elements.siteUsersEmpty.hidden = siteUsers.length > 0;
  siteUsers.forEach((user) => {
    const row = document.createElement("div");
    row.className = `site-user-row${user.disabled ? " is-disabled" : ""}`;

    const copy = document.createElement("div");
    copy.className = "site-user-copy";
    const name = document.createElement("strong");
    name.textContent = user.username;
    const detail = document.createElement("small");
    const hourlyLimit = Math.max(0, Number(user.hourlyMessageLimit) || 0);
    const usedLastHour = Math.max(0, Number(user.messagesUsedLastHour) || 0);
    const expiry = siteUserExpiryState(user);
    const quotaLabel = user.isAdmin || hourlyLimit === 0
      ? "消息不限"
      : `过去60分钟 ${usedLastHour}/${hourlyLimit} 条`;
    const accessLabel = expiry.expired ? "已到期停用" : (user.disabled ? "已停用" : "可登录");
    detail.textContent = `${user.isAdmin ? "管理员" : "成员"} · ${accessLabel} · ${quotaLabel}${expiry.label && !expiry.expired ? ` · ${expiry.label}` : ""}${user.id === siteAccount.id ? " · 当前账户" : ""}`;
    copy.append(name, detail);

    const actions = document.createElement("div");
    actions.className = "site-user-actions";
    const resetButton = document.createElement("button");
    resetButton.type = "button";
    resetButton.textContent = "重置密码";
    const limitButton = document.createElement("button");
    limitButton.type = "button";
    limitButton.textContent = "消息限额";
    limitButton.hidden = user.isAdmin;
    const expiryButton = document.createElement("button");
    expiryButton.type = "button";
    expiryButton.textContent = expiry.hasExpiry ? "修改期限" : "到期停用";
    expiryButton.hidden = user.isAdmin;
    const toggleButton = document.createElement("button");
    toggleButton.type = "button";
    toggleButton.textContent = user.disabled ? "启用" : "停用";
    toggleButton.className = user.disabled ? "" : "is-danger";
    if (user.id === siteAccount.id) {
      toggleButton.disabled = true;
      toggleButton.dataset.locked = "true";
      toggleButton.title = "不能停用当前登录账户";
    }
    actions.append(limitButton, expiryButton, resetButton, toggleButton);

    const limitForm = document.createElement("form");
    limitForm.className = "site-user-limit";
    limitForm.hidden = true;
    const limit = document.createElement("input");
    limit.type = "number";
    limit.min = "0";
    limit.max = "10000";
    limit.step = "1";
    limit.inputMode = "numeric";
    limit.value = String(hourlyLimit);
    limit.required = true;
    limit.setAttribute("aria-label", `设置 ${user.username} 每小时最多发送的消息数`);
    const saveLimit = document.createElement("button");
    saveLimit.type = "submit";
    saveLimit.textContent = "保存";
    const cancelLimit = document.createElement("button");
    cancelLimit.type = "button";
    cancelLimit.textContent = "取消";
    const limitHint = document.createElement("small");
    limitHint.textContent = "0 表示不限；修改后立即生效，不会清空过去60分钟的使用记录。";
    limitForm.append(limit, saveLimit, cancelLimit, limitHint);

    const expiryForm = document.createElement("form");
    expiryForm.className = "site-user-expiry";
    expiryForm.hidden = true;
    const expiryValue = document.createElement("input");
    expiryValue.type = "number";
    expiryValue.min = "1";
    expiryValue.max = "3650";
    expiryValue.step = "1";
    expiryValue.inputMode = "numeric";
    expiryValue.placeholder = "输入时长";
    expiryValue.required = true;
    expiryValue.setAttribute("aria-label", `设置 ${user.username} 多久后停用`);
    const expiryUnit = document.createElement("select");
    expiryUnit.setAttribute("aria-label", `设置 ${user.username} 的停用期限单位`);
    expiryUnit.innerHTML = '<option value="days">天后</option><option value="hours">小时后</option>';
    expiryUnit.addEventListener("change", () => {
      expiryValue.max = expiryUnit.value === "hours" ? "87600" : "3650";
    });
    const saveExpiry = document.createElement("button");
    saveExpiry.type = "submit";
    saveExpiry.textContent = "设置";
    const clearExpiry = document.createElement("button");
    clearExpiry.type = "button";
    clearExpiry.textContent = "取消期限";
    clearExpiry.hidden = !expiry.hasExpiry;
    const cancelExpiry = document.createElement("button");
    cancelExpiry.type = "button";
    cancelExpiry.textContent = "收起";
    const expiryHint = document.createElement("small");
    expiryHint.textContent = expiry.hasExpiry
      ? `当前：${expiry.label}。重新设置会从现在起重新倒计时。`
      : "可以选择几小时或几天；到期后该成员将退出所有设备且无法登录。";
    expiryForm.append(expiryValue, expiryUnit, saveExpiry, clearExpiry, cancelExpiry, expiryHint);

    const resetForm = document.createElement("form");
    resetForm.className = "site-user-reset";
    resetForm.hidden = true;
    const password = document.createElement("input");
    password.type = "password";
    password.minLength = 8;
    password.maxLength = 128;
    password.autocomplete = "new-password";
    password.placeholder = "输入至少 8 位的新密码";
    password.setAttribute("aria-label", `为 ${user.username} 设置新密码`);
    const save = document.createElement("button");
    save.type = "submit";
    save.textContent = "保存";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.textContent = "取消";
    resetForm.append(password, save, cancel);

    resetButton.addEventListener("click", () => {
      limitForm.hidden = true;
      expiryForm.hidden = true;
      resetForm.hidden = !resetForm.hidden;
      if (!resetForm.hidden) password.focus();
    });
    limitButton.addEventListener("click", () => {
      resetForm.hidden = true;
      expiryForm.hidden = true;
      limitForm.hidden = !limitForm.hidden;
      if (!limitForm.hidden) {
        limit.value = String(hourlyLimit);
        limit.focus();
        limit.select();
      }
    });
    expiryButton.addEventListener("click", () => {
      resetForm.hidden = true;
      limitForm.hidden = true;
      expiryForm.hidden = !expiryForm.hidden;
      if (!expiryForm.hidden) expiryValue.focus();
    });
    cancelLimit.addEventListener("click", () => {
      limitForm.hidden = true;
      limit.value = String(hourlyLimit);
    });
    limitForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const value = Number(limit.value);
      if (!Number.isInteger(value) || value < 0 || value > 10000) {
        setSiteUserStatus("每小时消息上限需为 0～10000 的整数。", "error");
        limit.focus();
        return;
      }
      await updateSiteUser(
        user.id,
        { hourly_message_limit: value },
        value === 0 ? `已取消“${user.username}”的消息限额。` : `“${user.username}”每小时最多可发送 ${value} 条消息。`,
      );
    });
    cancelExpiry.addEventListener("click", () => {
      expiryForm.hidden = true;
      expiryValue.value = "";
    });
    clearExpiry.addEventListener("click", async () => {
      await updateSiteUser(user.id, { expires_in_days: null }, `已取消“${user.username}”的自动停用期限。`);
    });
    expiryForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const value = Number(expiryValue.value);
      const unit = expiryUnit.value === "hours" ? "hours" : "days";
      const maximum = unit === "hours" ? 87600 : 3650;
      if (!Number.isInteger(value) || value < 1 || value > maximum) {
        setSiteUserStatus(`停用倒计时需为 1～${maximum} ${unit === "hours" ? "小时" : "天"}的整数。`, "error");
        expiryValue.focus();
        return;
      }
      await updateSiteUser(
        user.id,
        { [unit === "hours" ? "expires_in_hours" : "expires_in_days"]: value },
        `“${user.username}”将在 ${value} ${unit === "hours" ? "小时" : "天"}后自动停用。`,
      );
    });
    cancel.addEventListener("click", () => {
      resetForm.hidden = true;
      password.value = "";
    });
    resetForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (password.value.length < 8) {
        setSiteUserStatus("新密码至少需要 8 位。", "error");
        password.focus();
        return;
      }
      await updateSiteUser(user.id, { password: password.value }, "密码已重置，该账户需重新登录。");
    });
    toggleButton.addEventListener("click", async () => {
      await updateSiteUser(
        user.id,
        { disabled: !user.disabled },
        user.disabled ? "账户已启用。" : "账户已停用并退出所有设备。",
      );
    });

    row.append(copy, actions, limitForm, expiryForm, resetForm);
    elements.siteUserList.append(row);
  });
}

async function loadSiteUsers() {
  const response = await api("/api/admin/users");
  const result = await response.json();
  siteUsers = Array.isArray(result.users) ? result.users : [];
  renderSiteUsers();
}

async function updateSiteUser(userId, changes, successMessage) {
  setSiteUserBusy(true);
  setSiteUserStatus("正在保存账户设置…");
  try {
    await api(`/api/admin/users/${encodeURIComponent(userId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(changes),
    });
    await loadSiteUsers();
    setSiteUserStatus(successMessage, "success");
  } catch (error) {
    setSiteUserStatus(error.message, "error");
  } finally {
    setSiteUserBusy(false);
  }
}

async function createSiteUser() {
  const username = elements.siteUserName.value.trim();
  const password = elements.siteUserPassword.value;
  const hourlyMessageLimit = Number(elements.siteUserLimit.value);
  const expiresIn = Number(elements.siteUserExpiry.value);
  const expiryUnit = elements.siteUserExpiryUnit.value === "hours" ? "hours" : "days";
  const maximumExpiry = expiryUnit === "hours" ? 87600 : 3650;
  if (!/^[A-Za-z0-9_\-\u4e00-\u9fff]{2,32}$/.test(username)) {
    setSiteUserStatus("用户名需为 2～32 位中文、字母、数字、下划线或连字符。", "error");
    elements.siteUserName.focus();
    return;
  }
  if (password.length < 8) {
    setSiteUserStatus("初始密码至少需要 8 位。", "error");
    elements.siteUserPassword.focus();
    return;
  }
  if (!Number.isInteger(hourlyMessageLimit) || hourlyMessageLimit < 0 || hourlyMessageLimit > 10000) {
    setSiteUserStatus("每小时消息上限需为 0～10000 的整数。", "error");
    elements.siteUserLimit.focus();
    return;
  }
  if (!Number.isInteger(expiresIn) || expiresIn < 0 || expiresIn > maximumExpiry) {
    setSiteUserStatus(`自动停用期限需为 0～${maximumExpiry} ${expiryUnit === "hours" ? "小时" : "天"}的整数。`, "error");
    elements.siteUserExpiry.focus();
    return;
  }
  setSiteUserBusy(true);
  setSiteUserStatus("正在创建账户…");
  try {
    await api("/api/admin/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username,
        password,
        is_admin: elements.siteUserAdmin.checked,
        hourly_message_limit: elements.siteUserAdmin.checked ? 0 : hourlyMessageLimit,
        [expiryUnit === "hours" ? "expires_in_hours" : "expires_in_days"]:
          elements.siteUserAdmin.checked || expiresIn === 0 ? null : expiresIn,
      }),
    });
    elements.siteUserForm.reset();
    elements.siteUserLimit.disabled = false;
    elements.siteUserLimit.value = "0";
    elements.siteUserExpiry.disabled = false;
    elements.siteUserExpiry.value = "0";
    elements.siteUserExpiryUnit.disabled = false;
    elements.siteUserExpiryUnit.value = "days";
    await loadSiteUsers();
    setSiteUserStatus(`账户“${username}”已创建。`, "success");
  } catch (error) {
    setSiteUserStatus(error.message, "error");
  } finally {
    setSiteUserBusy(false);
  }
}

function presetById(id) {
  return providerPresets.find((item) => item.id === id) || null;
}

function applyProviderPreset({ keepName = false } = {}) {
  const preset = presetById(elements.providerPreset.value);
  const custom = elements.providerPreset.value === "custom";
  if (preset && !custom) {
    elements.providerBaseUrl.value = preset.baseUrl;
    elements.providerProtocol.value = preset.protocol;
    if (!keepName || !elements.providerName.value.trim()) elements.providerName.value = preset.name;
  } else if (!keepName) {
    elements.providerName.value = "";
    elements.providerBaseUrl.value = "";
    elements.providerProtocol.value = "openai";
  }
  elements.providerBaseUrl.readOnly = !custom;
  elements.providerProtocol.disabled = !custom;
  updateProviderTransportHint();
}

function updateProviderTransportHint() {
  const value = elements.providerBaseUrl.value.trim();
  const insecure = /^http:\/\//i.test(value);
  const serverLocal = /^http:\/\/(?:localhost|127(?:\.\d{1,3}){3}|\[::1\])(?::|\/|$)/i.test(value);
  elements.providerTransportHint.classList.toggle("is-warning", insecure);
  elements.providerTransportHint.textContent = serverLocal
    ? "本机 HTTP 会连接云服务器自身，不是你的手机或电脑；密钥和内容不会经过公网。"
    : insecure
      ? "当前使用 HTTP：若地址在你的电脑上，需先通过端口映射、VPN 或隧道让云服务器能够访问。"
      : "支持公网 HTTP/HTTPS，以及云服务器能够访问的电脑或私网 HTTP 地址。";
}

function setProviderFormDirty(dirty, { announce = false } = {}) {
  providerFormDirty = dirty;
  elements.providerForm.dataset.dirty = String(dirty);
  if (dirty && announce) {
    setSettingsStatus("有未保存的 API 配置；切换窗口或收起设置后仍会保留。", "");
  }
}

function renderImportedModels(provider) {
  elements.modelImport.hidden = !provider;
  elements.detectedModels.replaceChildren();
  if (!provider) return;
  const imported = provider.models || [];
  elements.detectedModelCount.textContent = imported.length
    ? `共 ${imported.length} 个；点击红叉可移除，重新检测可恢复`
    : "尚未导入模型；可重新检测恢复";
  imported.forEach((model) => {
    const chip = document.createElement("span");
    chip.className = "detected-model";
    chip.title = model;
    const name = document.createElement("span");
    name.className = "detected-model-name";
    name.textContent = model;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "detected-model-remove";
    remove.title = `移除 ${model}`;
    remove.setAttribute("aria-label", `移除模型 ${model}`);
    remove.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m7 7 10 10M17 7 7 17"/></svg>';
    remove.addEventListener("click", () => removeProviderModel(provider.id, model, remove));
    chip.append(name, remove);
    elements.detectedModels.append(chip);
  });
}

function resetProviderForm() {
  elements.providerForm.reset();
  elements.providerId.value = "";
  elements.providerPreset.value = "openai";
  elements.providerEnabled.checked = true;
  elements.providerEditorTitle.textContent = "添加 API 连接";
  elements.providerKey.placeholder = "sk-…";
  elements.providerKeyHint.textContent = "密钥只保存于服务器，不会返回浏览器。";
  elements.providerPriceCurrency.value = "CNY";
  elements.providerInputPrice.value = "";
  elements.providerOutputPrice.value = "";
  elements.manualModels.value = "";
  applyProviderPreset();
  renderImportedModels(null);
  setSettingsStatus();
  setProviderFormDirty(false);
}

function editProvider(providerId, { reveal = false } = {}) {
  const provider = providers.find((item) => item.id === providerId);
  if (!provider) return;
  elements.providerId.value = provider.id;
  elements.providerPreset.value = provider.preset || "custom";
  elements.providerName.value = provider.name;
  elements.providerBaseUrl.value = provider.baseUrl;
  elements.providerProtocol.value = provider.protocol;
  elements.providerKey.value = "";
  elements.providerKey.placeholder = provider.hasKey ? "已保存，留空保持不变" : "请输入 API 密钥";
  elements.providerKeyHint.textContent = provider.hasKey ? "服务器中已有密钥；留空不会覆盖。" : "密钥只保存于服务器，不会返回浏览器。";
  elements.providerPriceCurrency.value = provider.pricing?.currency || "CNY";
  elements.providerInputPrice.value = provider.pricing?.configured ? provider.pricing.inputPerMillion : "";
  elements.providerOutputPrice.value = provider.pricing?.configured ? provider.pricing.outputPerMillion : "";
  elements.providerEnabled.checked = provider.enabled;
  elements.providerEditorTitle.textContent = `编辑 ${provider.name}`;
  elements.manualModels.value = "";
  applyProviderPreset({ keepName: true });
  renderImportedModels(provider);
  setSettingsStatus();
  setProviderFormDirty(false);
  elements.providerList.querySelectorAll(".provider-row").forEach((row) => {
    row.classList.toggle("is-editing", row.dataset.providerId === provider.id);
  });
  if (reveal) {
    setProviderListStatus(`已打开“${provider.name}”的编辑表单。`, "success");
    const editor = elements.providerEditorTitle.closest(".provider-editor");
    requestAnimationFrame(() => {
      editor.scrollIntoView({ behavior: prefersReducedMotion() ? "auto" : "smooth", block: "start" });
    });
  }
}

function renderProviders() {
  elements.providerList.replaceChildren();
  elements.providerEmpty.hidden = providers.length > 0;
  providers.forEach((provider) => {
    const row = document.createElement("div");
    row.className = `provider-row${elements.providerId.value === provider.id ? " is-editing" : ""}`;
    row.dataset.providerId = provider.id;
    const copy = document.createElement("div");
    copy.className = "provider-copy";
    const name = document.createElement("strong");
    name.textContent = provider.name;
    const detail = document.createElement("small");
    detail.textContent = `${provider.protocol === "anthropic" ? "Anthropic 兼容" : "OpenAI 兼容"} · ${provider.models?.length || 0} 个模型 · 密钥${provider.hasKey ? "已保存" : "缺失"} · ${provider.pricing?.configured ? "费用已配置" : "费用待设置"}`;
    detail.title = provider.baseUrl;
    copy.append(name, detail);

    const actions = document.createElement("div");
    actions.className = "provider-row-actions";
    const enabled = document.createElement("label");
    enabled.className = "provider-enable";
    const enabledInput = document.createElement("input");
    enabledInput.type = "checkbox";
    enabledInput.checked = provider.enabled;
    const enabledText = document.createElement("span");
    enabledText.textContent = provider.enabled ? "已启用" : "已停用";
    enabled.append(enabledInput, enabledText);
    enabledInput.setAttribute("aria-label", `${provider.enabled ? "停用" : "启用"}${provider.name}`);
    enabledInput.addEventListener("change", () => toggleProvider(provider.id, enabledInput.checked, enabledInput));
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "provider-edit";
    edit.textContent = "编辑";
    edit.setAttribute("aria-label", `编辑${provider.name}`);
    edit.addEventListener("click", () => beginProviderEdit(provider.id));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "provider-delete";
    remove.textContent = "删除";
    remove.addEventListener("click", () => removeProvider(provider.id));
    actions.append(enabled, edit, remove);
    row.append(copy, actions);
    elements.providerList.append(row);
  });
}

function revealProviderEditor(message) {
  if (message) setProviderListStatus(message, "success");
  const editor = elements.providerEditorTitle.closest(".provider-editor");
  requestAnimationFrame(() => {
    editor.scrollIntoView({ behavior: prefersReducedMotion() ? "auto" : "smooth", block: "start" });
  });
}

function beginProviderEdit(providerId) {
  const provider = providers.find((item) => item.id === providerId);
  if (!provider) return;
  if (providerFormDirty && elements.providerId.value === providerId) {
    revealProviderEditor(`“${provider.name}”有未保存的更改。`);
    return;
  }
  if (providerFormDirty && !confirm("当前 API 配置尚未保存。确定放弃这些更改并编辑其他连接吗？")) return;
  editProvider(providerId, { reveal: true });
}

function beginNewProvider() {
  if (providerFormDirty && !confirm("当前 API 配置尚未保存。确定放弃这些更改并新建连接吗？")) return;
  resetProviderForm();
  revealProviderEditor("已打开新的 API 连接表单。");
}

async function loadProviders({ preserveDraft = false } = {}) {
  const response = await api("/api/providers");
  const result = await response.json();
  providers = result.providers || [];
  providerPresets = result.presets || [];
  renderProviders();
  if (preserveDraft && providerFormDirty) return;
  const editing = providers.find((item) => item.id === elements.providerId.value);
  if (editing) editProvider(editing.id);
  else if (!elements.providerId.value) resetProviderForm();
}

function openSettings() {
  settingsReturnFocus = document.activeElement;
  clearTimeout(settingsCloseTimer);
  elements.settingsScrim.hidden = false;
  requestAnimationFrame(() => elements.settingsScrim.classList.add("is-visible"));
  elements.settingsPanel.classList.add("is-open");
  elements.settingsPanel.setAttribute("aria-hidden", "false");
  elements.settingsButton.setAttribute("aria-expanded", "true");
  elements.appShell.inert = true;
  document.body.classList.add("settings-open");
  setProviderListStatus();
  Promise.all([loadProviders({ preserveDraft: true }), loadSiteUsers(), loadCodexAdminModels()]).catch((error) => setSettingsStatus(error.message, "error"));
  setTimeout(() => elements.settingsClose.focus(), 0);
}

function closeSettings() {
  const wasOpen = elements.settingsPanel.classList.contains("is-open");
  elements.settingsPanel.classList.remove("is-open");
  elements.settingsScrim.classList.remove("is-visible");
  elements.settingsPanel.setAttribute("aria-hidden", "true");
  elements.settingsButton.setAttribute("aria-expanded", "false");
  elements.appShell.inert = false;
  document.body.classList.remove("settings-open");
  clearTimeout(settingsCloseTimer);
  if (prefersReducedMotion()) elements.settingsScrim.hidden = true;
  else settingsCloseTimer = setTimeout(() => { elements.settingsScrim.hidden = true; }, 180);
  if (wasOpen) setTimeout(() => (settingsReturnFocus || elements.settingsButton).focus(), 0);
}

function settingsFocusable() {
  return [...elements.settingsPanel.querySelectorAll(
    'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [href], [tabindex]:not([tabindex="-1"])',
  )].filter((element) => !element.hidden && element.getClientRects().length);
}

function setProjectStatus(message = "", tone = "") {
  elements.projectStatus.textContent = message;
  elements.projectStatus.className = `settings-status${tone ? ` is-${tone}` : ""}`;
}

function renderProjectFiles(project = activeProject()) {
  elements.projectFileList.replaceChildren();
  const files = project?.files || [];
  elements.projectFilesEmpty.hidden = files.length > 0;
  for (const file of files) {
    const row = document.createElement("div");
    row.className = "project-file-row";
    row.innerHTML = file.type === "image"
      ? '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="m21 15-5-5L5 20"/></svg>'
      : '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 2h8l4 4v16H6Z"/><path d="M14 2v5h5"/></svg>';
    const copy = document.createElement("span");
    const name = document.createElement("strong");
    name.textContent = file.name;
    const size = document.createElement("small");
    size.textContent = formatBytes(file.size);
    copy.append(name, size);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "移出";
    remove.setAttribute("aria-label", `将 ${file.name} 移出项目上下文`);
    remove.addEventListener("click", () => {
      const current = activeProject();
      if (!current) return;
      current.files = current.files.filter((item) => item.id !== file.id);
      current.updatedAt = Date.now();
      saveState();
      renderProjectFiles(current);
      renderProjects();
      renderProjectContext();
      setProjectStatus("文件已移出项目上下文。", "success");
    });
    row.append(copy, remove);
    elements.projectFileList.append(row);
  }
}

function openProjectPanel(project = activeProject()) {
  if (!project) return;
  projectReturnFocus = document.activeElement;
  clearTimeout(projectCloseTimer);
  elements.projectName.value = project.name;
  elements.projectInstructions.value = project.instructions;
  elements.projectUseContext.checked = Boolean(project.useContext);
  setProjectStatus();
  renderProjectFiles(project);
  elements.projectScrim.hidden = false;
  requestAnimationFrame(() => elements.projectScrim.classList.add("is-visible"));
  elements.projectPanel.classList.add("is-open");
  elements.projectPanel.setAttribute("aria-hidden", "false");
  elements.projectSettingsButton.setAttribute("aria-expanded", "true");
  elements.appShell.inert = true;
  document.body.classList.add("project-open");
  setTimeout(() => elements.projectName.focus(), 0);
}

function closeProjectPanel() {
  const wasOpen = elements.projectPanel.classList.contains("is-open");
  elements.projectPanel.classList.remove("is-open");
  elements.projectScrim.classList.remove("is-visible");
  elements.projectPanel.setAttribute("aria-hidden", "true");
  elements.projectSettingsButton.setAttribute("aria-expanded", "false");
  elements.appShell.inert = false;
  document.body.classList.remove("project-open");
  clearTimeout(projectCloseTimer);
  if (prefersReducedMotion()) elements.projectScrim.hidden = true;
  else projectCloseTimer = setTimeout(() => { elements.projectScrim.hidden = true; }, 180);
  if (wasOpen) setTimeout(() => (projectReturnFocus || elements.projectSettingsButton).focus(), 0);
}

function projectFocusable() {
  return [...elements.projectPanel.querySelectorAll(
    'button:not(:disabled), input:not(:disabled), textarea:not(:disabled), [href], [tabindex]:not([tabindex="-1"])',
  )].filter((element) => !element.hidden && element.getClientRects().length);
}

function saveProjectDetails() {
  const project = activeProject();
  if (!project) return;
  const name = elements.projectName.value.trim().replace(/\s+/g, " ").slice(0, 60);
  if (!name) {
    setProjectStatus("请输入项目名称。", "error");
    elements.projectName.focus();
    return;
  }
  project.name = name;
  project.instructions = elements.projectInstructions.value.trim().slice(0, 12000);
  project.useContext = elements.projectUseContext.checked;
  project.updatedAt = Date.now();
  saveState();
  renderProjects();
  renderHistory();
  renderProjectContext();
  setProjectStatus("项目已保存并同步。", "success");
}

function abortError() {
  return new DOMException("操作已取消", "AbortError");
}

function waitForUploadRetry(delay, signal) {
  if (signal?.aborted) return Promise.reject(abortError());
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, delay);
    const onAbort = () => {
      clearTimeout(timer);
      reject(abortError());
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

function canRetryUpload(error) {
  const status = Number(error?.status) || 0;
  return status === 0 || status === 408 || status === 425 || status === 429 || status >= 500;
}

async function uploadStep(action, signal) {
  let lastError = null;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    if (signal?.aborted) throw abortError();
    try {
      return await action();
    } catch (error) {
      lastError = error;
      if (error?.name === "AbortError" || signal?.aborted) throw abortError();
      if (!canRetryUpload(error) || attempt === 2) throw error;
      const retryAfter = Math.max(0, Number(error?.retryAfter) || 0) * 1000;
      await waitForUploadRetry(Math.max(retryAfter, 300 * (2 ** attempt)), signal);
    }
  }
  throw lastError || new Error("上传失败");
}

async function uploadFileInChunks(file, sessionId, onProgress = () => {}, source = "file", signal) {
  if (!file || file.size > MAX_UPLOAD_BYTES) throw new Error("单个文件不能超过 30 MB");
  if (signal?.aborted) throw abortError();
  const initResponse = await uploadStep(() => api("/api/uploads/init", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, name: file.name, size: file.size, source }),
    signal,
  }), signal);
  const initialized = await initResponse.json();
  const uploadId = String(initialized.uploadId || "");
  const chunkSize = Number(initialized.chunkSize);
  let offset = Number(initialized.nextOffset);
  if (!uploadId || uploadId.length > 200) throw new Error("服务器没有返回有效的上传编号");
  if (!Number.isInteger(chunkSize) || chunkSize <= 0 || chunkSize > UPLOAD_CHUNK_BYTES) {
    throw new Error("服务器返回了无效的分片大小");
  }
  if (!Number.isInteger(offset) || offset < 0 || offset > file.size) {
    throw new Error("服务器返回了无效的上传进度");
  }
  onProgress(file.size ? offset / file.size : 1);
  while (offset < file.size) {
    const expectedOffset = offset;
    const nextExpectedOffset = Math.min(file.size, expectedOffset + chunkSize);
    const chunk = file.slice(expectedOffset, nextExpectedOffset);
    const chunkResult = await uploadStep(async () => {
      const form = new FormData();
      form.append("session_id", sessionId);
      form.append("offset", String(expectedOffset));
      form.append("chunk", chunk, file.name);
      const response = await api(`/api/uploads/${encodeURIComponent(uploadId)}/chunk`, {
        method: "POST",
        body: form,
        signal,
      });
      return response.json();
    }, signal);
    const nextOffset = Number(chunkResult.nextOffset);
    if (String(chunkResult.uploadId || uploadId) !== uploadId || nextOffset !== nextExpectedOffset) {
      throw new Error("服务器返回的分片进度不一致，请重新上传");
    }
    offset = nextOffset;
    onProgress(file.size ? offset / file.size : 1);
  }
  const completeResponse = await uploadStep(() => api(`/api/uploads/${encodeURIComponent(uploadId)}/complete`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
    signal,
  }), signal);
  const completed = await completeResponse.json();
  if (!Array.isArray(completed.files) || !completed.files.length) {
    throw new Error("上传完成，但服务器没有返回文件信息");
  }
  const expectedSource = source === "composer_text" ? "composer_text" : "file";
  return completed.files.map((uploaded) => {
    const serverSource = uploaded.source === "composer_text" ? "composer_text" : "file";
    if (serverSource !== expectedSource) throw new Error("服务器返回的文件类型不一致，请重新上传");
    if (serverSource === "composer_text" && typeof uploaded.preview !== "string") {
      throw new Error("服务器没有返回长文字预览，请重新上传");
    }
    return { ...uploaded, source: serverSource };
  });
}

async function uploadProjectFiles(fileList) {
  const project = activeProject();
  const files = [...fileList];
  if (!project || !files.length) return;
  const remaining = 20 - project.files.length;
  if (remaining <= 0) {
    setProjectStatus("每个项目最多保存 20 个共享文件。", "error");
    elements.projectFileInput.value = "";
    return;
  }
  const acceptedExtensions = new Set(
    elements.projectFileInput.accept.split(",").map((value) => value.trim().toLowerCase()),
  );
  const accepted = files.filter((file) => {
    const dot = file.name.lastIndexOf(".");
    const extension = dot >= 0 ? file.name.slice(dot).toLowerCase() : "";
    return file.size <= MAX_UPLOAD_BYTES && acceptedExtensions.has(extension);
  }).slice(0, remaining);
  if (!accepted.length) {
    setProjectStatus("文件格式不支持，或单个文件超过 30 MB。", "error");
    elements.projectFileInput.value = "";
    return;
  }
  elements.projectFileInput.disabled = true;
  const uploadController = new AbortController();
  activeProjectUploadController = uploadController;
  elements.projectUploadCancel.hidden = false;
  setProjectStatus(`正在上传 ${accepted.length} 个文件…`);
  let uploadedCount = 0;
  try {
    for (let index = 0; index < accepted.length; index += 1) {
      const file = accepted[index];
      const resultFiles = await uploadFileInChunks(file, project.workspaceId, (progress) => {
        setProjectStatus(`正在上传 ${index + 1}/${accepted.length}：${file.name}（${Math.round(progress * 100)}%）`);
      }, "file", uploadController.signal);
      project.files.push(...resultFiles);
      uploadedCount += resultFiles.length;
      project.files = project.files.slice(0, 20);
      project.updatedAt = Date.now();
      saveState();
      renderProjectFiles(project);
      renderProjects();
      renderProjectContext();
    }
    const skipped = files.length - accepted.length;
    setProjectStatus(`已加入 ${uploadedCount} 个共享文件${skipped ? `，另有 ${skipped} 个未加入` : ""}。`, "success");
  } catch (error) {
    setProjectStatus(error?.name === "AbortError" ? "已取消上传。" : userFacingError(error), error?.name === "AbortError" ? "" : "error");
  } finally {
    if (activeProjectUploadController === uploadController) activeProjectUploadController = null;
    elements.projectUploadCancel.hidden = true;
    elements.projectFileInput.disabled = false;
    elements.projectFileInput.value = "";
  }
}

function setProviderBusy(busy) {
  [elements.providerSaveDetect, elements.providerSaveOnly, elements.providerDetect, elements.manualModelImport].forEach((button) => {
    button.disabled = busy;
  });
}

function providerPayload() {
  const inputPrice = elements.providerInputPrice.value.trim();
  const outputPrice = elements.providerOutputPrice.value.trim();
  return {
    id: elements.providerId.value || null,
    preset: elements.providerPreset.value,
    name: elements.providerName.value.trim(),
    base_url: elements.providerBaseUrl.value.trim(),
    protocol: elements.providerProtocol.value,
    api_key: elements.providerKey.value,
    enabled: elements.providerEnabled.checked,
    price_currency: elements.providerPriceCurrency.value,
    input_price_per_million: inputPrice === "" ? null : Number(inputPrice),
    output_price_per_million: outputPrice === "" ? null : Number(outputPrice),
  };
}

async function saveProvider({ detect = false } = {}) {
  setProviderBusy(true);
  setSettingsStatus(detect ? "正在保存并检测模型…" : "正在保存…");
  try {
    const response = await api("/api/providers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(providerPayload()),
    });
    let provider = (await response.json()).provider;
    if (detect) {
      const detection = await api(`/api/providers/${encodeURIComponent(provider.id)}/detect`, { method: "POST" });
      const result = await detection.json();
      provider = result.provider;
      setSettingsStatus(`连接成功，已导入 ${result.count} 个模型。`, "success");
    } else {
      setSettingsStatus("连接已保存。需要时可再检测或手动导入模型。", "success");
    }
    await loadProviders();
    editProvider(provider.id);
    setSettingsStatus(detect ? `连接成功，已导入 ${provider.models.length} 个模型。` : "连接已保存。", "success");
    await loadModels(accountConnected);
  } catch (error) {
    setSettingsStatus(`${error.message}${detect ? "；也可以仅保存后手动导入模型。" : ""}`, "error");
  } finally {
    setProviderBusy(false);
  }
}

async function detectProvider(providerId = elements.providerId.value) {
  if (!providerId) {
    setSettingsStatus("请先保存连接。", "error");
    return;
  }
  setProviderBusy(true);
  setSettingsStatus("正在检测可用模型…");
  try {
    const response = await api(`/api/providers/${encodeURIComponent(providerId)}/detect`, { method: "POST" });
    const result = await response.json();
    await loadProviders();
    editProvider(providerId);
    setSettingsStatus(`检测完成，已导入 ${result.count} 个模型。`, "success");
    await loadModels(accountConnected);
  } catch (error) {
    setSettingsStatus(`${error.message}；可在下方手动导入模型 ID。`, "error");
  } finally {
    setProviderBusy(false);
  }
}

async function importManualModels() {
  const providerId = elements.providerId.value;
  const modelsToImport = elements.manualModels.value.split(/[\n,，]+/).map((item) => item.trim()).filter(Boolean);
  if (!providerId) {
    setSettingsStatus("请先保存连接。", "error");
    return;
  }
  if (!modelsToImport.length) {
    setSettingsStatus("请输入至少一个模型 ID。", "error");
    return;
  }
  setProviderBusy(true);
  setSettingsStatus("正在导入模型…");
  try {
    await api(`/api/providers/${encodeURIComponent(providerId)}/models`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ models: modelsToImport }),
    });
    elements.manualModels.value = "";
    await loadProviders();
    editProvider(providerId);
    setSettingsStatus(`已导入 ${modelsToImport.length} 个模型。`, "success");
    await loadModels(accountConnected);
  } catch (error) {
    setSettingsStatus(error.message, "error");
  } finally {
    setProviderBusy(false);
  }
}

async function removeProviderModel(providerId, modelId, control) {
  control.disabled = true;
  setSettingsStatus(`正在移除模型“${modelId}”…`);
  try {
    const response = await api(`/api/providers/${encodeURIComponent(providerId)}/models`, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: modelId }),
    });
    const result = await response.json();
    providers = providers.map((item) => item.id === providerId ? result.provider : item);
    renderProviders();
    if (elements.providerId.value === providerId) renderImportedModels(result.provider);
    setSettingsStatus(`已移除模型“${modelId}”；重新检测可恢复。`, "success");
    await loadModels(accountConnected);
  } catch (error) {
    setSettingsStatus(`移除失败：${error.message}`, "error");
    if (control.isConnected) control.disabled = false;
  }
}

async function toggleProvider(providerId, enabled, control = null) {
  const provider = providers.find((item) => item.id === providerId);
  const action = enabled ? "启用" : "停用";
  if (control) {
    control.disabled = true;
    control.closest(".provider-enable")?.classList.add("is-saving");
  }
  setProviderListStatus(`正在${action}“${provider?.name || "API 连接"}”…`);
  try {
    const response = await api(`/api/providers/${encodeURIComponent(providerId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    const result = await response.json();
    providers = providers.map((item) => item.id === providerId ? result.provider : item);
    renderProviders();
    setProviderListStatus(`“${provider?.name || "API 连接"}”已${action}。`, "success");
    await loadModels(accountConnected);
  } catch (error) {
    setProviderListStatus(`${action}失败：${error.message}`, "error");
    try {
      await loadProviders();
    } catch {}
  } finally {
    if (control?.isConnected) {
      control.disabled = false;
      control.closest(".provider-enable")?.classList.remove("is-saving");
    }
  }
}

async function removeProvider(providerId) {
  const provider = providers.find((item) => item.id === providerId);
  if (!provider || !confirm(`确定删除“${provider.name}”及其服务器端密钥吗？`)) return;
  try {
    await api(`/api/providers/${encodeURIComponent(providerId)}`, { method: "DELETE" });
    if (elements.providerId.value === providerId) resetProviderForm();
    await loadProviders();
    await loadModels(accountConnected);
    setSettingsStatus("API 连接和密钥已从服务器删除。", "success");
  } catch (error) {
    setSettingsStatus(error.message, "error");
  }
}

async function api(url, options = {}) {
  const response = await fetch(url, { ...options, headers: { ...(options.headers || {}) } });
  if (!response.ok) {
    let detail = "请求失败";
    try {
      const body = await response.json();
      detail = body.detail || body.error || detail;
    } catch {}
    if (typeof detail !== "string") detail = detail.message || "请求失败";
    if (response.status === 401 && detail === "请先登录") {
      location.href = "/login";
      throw new Error("登录已失效");
    }
    const error = new Error(userFacingError(detail, response.status));
    error.status = response.status;
    error.retryAfter = Number(response.headers.get("Retry-After")) || 0;
    throw error;
  }
  return response;
}

async function checkAccount() {
  elements.accountAction.disabled = true;
  try {
    const response = await api("/api/status");
    const status = await response.json();
    if (status.account?.type === "chatgpt") {
      accountConnected = true;
      elements.statusDot.className = "status-dot is-online";
      elements.accountLabel.textContent = "ChatGPT 已连接";
      elements.accountDetail.textContent = `${status.account.planType || "ChatGPT"} 账户`;
      elements.accountAction.textContent = "断开";
      elements.accountAction.dataset.action = "logout";
      elements.accountAction.disabled = false;
      await loadModels(true);
      await refreshQuota({ quiet: true });
      return true;
    }
    showDisconnected();
    await loadModels(false);
    return false;
  } catch (error) {
    accountConnected = false;
    elements.statusDot.className = "status-dot is-error";
    elements.accountLabel.textContent = "账户检查失败";
    elements.accountDetail.textContent = error.message;
    elements.accountAction.textContent = "重试";
    elements.accountAction.dataset.action = "login";
    elements.accountAction.disabled = false;
    await loadModels(false);
    return false;
  }
}

function showDisconnected() {
  accountConnected = false;
  models = [];
  elements.statusDot.className = "status-dot";
  elements.accountLabel.textContent = "尚未连接 ChatGPT";
  elements.accountDetail.textContent = "使用官方设备授权";
  elements.accountAction.textContent = "连接";
  elements.accountAction.dataset.action = "login";
  elements.accountAction.disabled = false;
  elements.modelSelect.innerHTML = "<option>等待连接</option>";
  elements.modelSelect.disabled = true;
  elements.effortSelect.disabled = true;
  state.model = "";
  saveState();
  updateComposer();
  setQuotaUnavailable("连接账户后显示");
}

function formatQuotaReset(timestamp) {
  if (!Number.isFinite(timestamp)) return "更新时间暂不可用";
  const date = new Date(timestamp * 1000);
  if (Number.isNaN(date.getTime())) return "更新时间暂不可用";
  const formatted = new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
  return `预计 ${formatted} 更新`;
}

function setQuotaUnavailable(detail = "稍后自动重试") {
  elements.quotaMonitor.className = "quota-monitor is-error";
  elements.quotaRemaining.textContent = "Codex 额度暂不可用";
  elements.quotaReset.textContent = detail;
}

async function refreshQuota({ quiet = false } = {}) {
  if (!accountConnected) {
    setQuotaUnavailable("连接账户后显示");
    return;
  }
  if (!quiet) elements.quotaMonitor.className = "quota-monitor is-loading";
  try {
    const response = await api("/api/rate-limits");
    const result = await response.json();
    const remaining = Math.max(0, Math.min(100, Math.round(Number(result.remainingPercent))));
    if (!Number.isFinite(remaining)) throw new Error("额度数据无效");
    elements.quotaMonitor.className = `quota-monitor${remaining <= 20 ? " is-low" : ""}`;
    elements.quotaRemaining.textContent = `Codex 剩余约 ${remaining}%`;
    const resetsAt = result.resetsAt == null ? Number.NaN : Number(result.resetsAt);
    elements.quotaReset.textContent = formatQuotaReset(resetsAt);
    quotaLastFetched = Date.now();
  } catch (error) {
    setQuotaUnavailable(error.message || "稍后自动重试");
  }
}

function setLatencyState(value, stateName, description) {
  elements.latencyMonitor.className = `latency-monitor is-${stateName}`;
  elements.latencyValue.textContent = value == null ? "-- ms" : `${value} ms`;
  elements.latencyMonitor.setAttribute("aria-label", description);
  elements.latencyMonitor.title = `${description}；浏览器到 AI Chat 服务器的往返延迟`;
}

async function refreshLatency() {
  if (latencyProbeActive || document.hidden) return;
  latencyProbeActive = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 5000);
  const startedAt = performance.now();
  try {
    const response = await fetch(`/healthz?probe=${Date.now()}`, {
      cache: "no-store",
      signal: controller.signal,
    });
    if (!response.ok) throw new Error("延迟检测失败");
    const latency = Math.max(1, Math.round(performance.now() - startedAt));
    if (latency <= 180) setLatencyState(latency, "good", `服务器延迟 ${latency} 毫秒，良好`);
    else if (latency <= 450) setLatencyState(latency, "medium", `服务器延迟 ${latency} 毫秒，一般`);
    else setLatencyState(latency, "high", `服务器延迟 ${latency} 毫秒，较高`);
    latencyLastFetched = Date.now();
  } catch {
    setLatencyState(null, "high", "服务器延迟检测失败");
  } finally {
    clearTimeout(timeout);
    latencyProbeActive = false;
  }
}

async function startDeviceLogin() {
  elements.accountAction.disabled = true;
  elements.accountAction.textContent = "准备中";
  try {
    const response = await api("/api/account/login", { method: "POST" });
    const result = await response.json();
    elements.deviceCode.textContent = result.userCode;
    elements.verificationLink.href = result.verificationUrl;
    elements.dialogStatus.textContent = "完成授权后，此窗口会自动更新。";
    elements.dialog.showModal();
    clearInterval(loginPoll);
    loginPoll = setInterval(async () => {
      const connected = await checkAccount();
      if (connected) {
        clearInterval(loginPoll);
        elements.dialogStatus.textContent = "连接成功，可以开始对话了。";
        setTimeout(() => elements.dialog.close(), 900);
      }
    }, 3000);
  } catch (error) {
    elements.accountLabel.textContent = "无法开始授权";
    elements.accountDetail.textContent = error.message;
    elements.accountAction.textContent = "重试";
    elements.accountAction.disabled = false;
  }
}

async function disconnectAccount() {
  if (!confirm("确定断开这台服务器上的 ChatGPT 登录吗？")) return;
  elements.accountAction.disabled = true;
  try {
    await api("/api/account/logout", { method: "POST" });
    showDisconnected();
    await loadModels(false);
  } catch (error) {
    elements.accountDetail.textContent = error.message;
    elements.accountAction.disabled = false;
  }
}

async function loadModels(includeCodex = accountConnected) {
  try {
    const response = await api(`/api/models?include_codex=${includeCodex ? "true" : "false"}`);
    const result = await response.json();
    models = result.data || [];
    elements.modelSelect.replaceChildren();
    const groups = new Map();
    for (const model of models) {
      const groupName = model.source === "external" ? model.providerName : "Codex";
      if (!groups.has(groupName)) {
        const group = document.createElement("optgroup");
        group.label = groupName;
        groups.set(groupName, group);
        elements.modelSelect.append(group);
      }
      const option = document.createElement("option");
      option.value = model.id;
      option.textContent = model.displayName;
      groups.get(groupName).append(option);
    }
    const storedModel = models.find((model) => model.id === state.model);
    const defaultModel = models.find((model) => model.isDefault) || models[0];
    const selectedModel = storedModel || defaultModel;
    if (selectedModel) {
      elements.modelSelect.value = selectedModel.id;
      state.model = selectedModel.id;
      updateEfforts({ persist: false });
    } else {
      const option = document.createElement("option");
      option.textContent = includeCodex ? "暂无可用模型" : "请连接账户或添加 API";
      elements.modelSelect.append(option);
      state.model = "";
      elements.effortSelect.innerHTML = '<option value="default">默认</option>';
    }
    if (result.warning && models.length === 0) elements.modelDescription.textContent = result.warning;
    saveState();
    updateComposer();
  } catch (error) {
    elements.modelDescription.textContent = `模型列表加载失败：${error.message}`;
  }
}
function updateEfforts({ persist = true } = {}) {
  const model = models.find((item) => item.id === elements.modelSelect.value);
  if (!model) return;
  state.model = model.id;
  elements.modelDescription.textContent = model.description || "";
  const allowed = model.efforts?.length
    ? model.efforts
    : [{ id: model.defaultEffort || "medium", label: effortNames[model.defaultEffort] || "中" }];
  elements.effortSelect.replaceChildren();
  for (const effort of allowed) {
    const option = document.createElement("option");
    option.value = effort.id;
    option.textContent = effortNames[effort.id] || effort.label || effort.id;
    elements.effortSelect.append(option);
  }
  const preferred = allowed.some((item) => item.id === state.effort) ? state.effort : model.defaultEffort;
  elements.effortSelect.value = preferred || allowed[0].id;
  state.effort = elements.effortSelect.value;
  if (persist) saveState();
}

async function uploadSelectedFiles(fileList) {
  const files = [...fileList];
  if (!files.length) return;
  if (isSending) {
    showComposerError("回答生成中，请等待结束后再添加文件");
    return;
  }
  if (attachments.some((item) => item.loading)) {
    showComposerError("请等待当前文件上传完成");
    return;
  }
  attachments = attachments.filter((item) => !item.error);
  if (attachments.length + files.length > MAX_TURN_FILES) {
    showComposerError("每轮最多添加 5 个文件");
    return;
  }
  const acceptedExtensions = new Set(
    elements.fileInput.accept.split(",").map((value) => value.trim().toLowerCase()).filter((value) => value.startsWith(".")),
  );
  const acceptedFiles = [];
  const rejectedFiles = [];
  for (const file of files) {
    if (file.size > MAX_UPLOAD_BYTES) {
      rejectedFiles.push({ id: randomId(), name: file.name, size: file.size, error: "超过 30 MB" });
      continue;
    }
    const dot = file.name.lastIndexOf(".");
    const extension = dot >= 0 ? file.name.slice(dot).toLowerCase() : "";
    if (!acceptedExtensions.has(extension)) {
      rejectedFiles.push({ id: randomId(), name: file.name, size: file.size, error: `暂不支持 ${extension || "无扩展名"} 文件` });
      continue;
    }
    acceptedFiles.push(file);
  }
  if (rejectedFiles.length) {
    attachments.push(...rejectedFiles);
    showComposerError(rejectedFiles[0].error);
  }
  if (!acceptedFiles.length) {
    renderAttachments();
    return;
  }
  const existingFiles = attachments.filter((item) => !item.error);
  const existingComposerText = existingFiles.some((item) => item.source === "composer_text");
  const selectedTotal = (existingComposerText ? 0 : utf8ByteLength(elements.input.value.trim()))
    + sumFileSizes(existingFiles)
    + sumFileSizes(acceptedFiles);
  if (selectedTotal > MAX_UPLOAD_BYTES) {
    showComposerError("本轮文字与附件合计不能超过 30 MB");
    elements.fileInput.value = "";
    renderAttachments();
    return;
  }
  let conversation = activeConversation();
  if (!conversation) conversation = createConversation();
  const pending = acceptedFiles.map((file) => ({
    id: randomId(), name: file.name, size: file.size, loading: true, progress: 0,
  }));
  attachments.push(...pending);
  pending.forEach((item) => uploadControllers.set(item.id, new AbortController()));
  renderAttachments();
  const sessionId = conversation.workspaceId || state.sessionId;
  let firstError = null;
  for (let index = 0; index < acceptedFiles.length; index += 1) {
    const file = acceptedFiles[index];
    const pendingItem = pending[index];
    const controller = uploadControllers.get(pendingItem.id);
    try {
      const uploadedFiles = await uploadFileInChunks(file, sessionId, (progress) => {
        pendingItem.progress = progress;
        renderAttachments();
      }, "file", controller?.signal);
      attachments = attachments.filter((item) => item !== pendingItem);
      attachments.push(...uploadedFiles);
    } catch (error) {
      if (error?.name === "AbortError") {
        attachments = attachments.filter((item) => item !== pendingItem);
      } else {
        pendingItem.loading = false;
        pendingItem.error = userFacingError(error);
        firstError ||= error;
      }
    } finally {
      uploadControllers.delete(pendingItem.id);
    }
    renderAttachments();
  }
  if (firstError) showComposerError(userFacingError(firstError, firstError.status));
  elements.fileInput.value = "";
  renderAttachments();
}

function renderAttachments() {
  elements.attachmentList.replaceChildren();
  elements.attachmentList.hidden = attachments.length === 0;
  attachments.forEach((attachment, index) => {
    const item = document.createElement("div");
    item.className = `attachment-chip${attachment.loading ? " is-loading" : ""}${attachment.error ? " is-error" : ""}`;
    item.setAttribute("role", "listitem");
    if (attachment.error) item.title = `${attachment.name}：${attachment.error}`;
    const name = document.createElement("span");
    name.textContent = attachment.loading
      ? `上传中：${attachment.name}（${Math.round((Number(attachment.progress) || 0) * 100)}%）`
      : attachment.error
        ? `${attachment.name} · ${attachment.error}`
        : `${attachment.name} · ${formatBytes(attachment.size)}`;
    item.append(name);
    const uploadController = uploadControllers.get(attachment.id);
    if (!attachment.loading || uploadController) {
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = attachment.loading ? "取消" : "移除";
      remove.setAttribute("aria-label", `${attachment.loading ? "取消上传" : "移除"} ${attachment.name}`);
      remove.addEventListener("click", () => {
        uploadController?.abort();
        attachments.splice(index, 1);
        renderAttachments();
      });
      item.append(remove);
    }
    elements.attachmentList.append(item);
  });
  updateComposer();
}

function showComposerError(message) {
  clearTimeout(composerErrorTimer);
  elements.composerStatus.textContent = message;
  elements.composerStatus.hidden = false;
  composerErrorTimer = setTimeout(() => {
    elements.composerStatus.hidden = true;
  }, 5000);
}

async function sendMessage() {
  let text = elements.input.value.trim();
  if (isSending || (!text && attachments.length === 0) || !state.model) return;
  if (attachments.some((item) => item.error)) {
    showComposerError("请先移除上传失败的文件");
    return;
  }
  let conversation = activeConversation();
  if (!conversation) conversation = createConversation();
  const previousTitle = conversation.title;
  const project = conversation.projectId
    ? state.projects.find((item) => item.id === conversation.projectId) || null
    : null;
  let outgoingFiles = attachments.filter((item) => !item.loading && !item.error);
  if (outgoingFiles.length !== attachments.length) {
    showComposerError("请等待文件上传完成");
    return;
  }
  const composerTextFiles = outgoingFiles.filter((item) => item.source === "composer_text");
  if (composerTextFiles.length > 1) {
    showComposerError("每轮最多包含一个长文字附件");
    return;
  }
  const hasComposerText = composerTextFiles.length === 1;
  if (hasComposerText && text !== String(composerTextFiles[0].preview || "")) {
    showComposerError("已有长文字附件时不能另加正文，请先移除长文字附件");
    return;
  }
  const originalTextBytes = utf8ByteLength(text);
  const currentFileBytes = sumFileSizes(outgoingFiles);
  if ((hasComposerText ? 0 : originalTextBytes) + currentFileBytes > MAX_UPLOAD_BYTES) {
    showComposerError("本轮文字与附件合计不能超过 30 MB");
    return;
  }
  const titleSource = text.slice(0, 28);
  let retryText = originalTextBytes <= INLINE_TEXT_BYTES || hasComposerText ? text : "";
  if (!hasComposerText && originalTextBytes > INLINE_TEXT_BYTES) {
    if (outgoingFiles.length >= MAX_TURN_FILES) {
      showComposerError("长文字会自动作为附件上传，请将当前附件减少到 4 个以内");
      return;
    }
    const fileName = longTextFileName();
    let textFile = new File([text], fileName, { type: "text/plain;charset=utf-8" });
    const pendingText = { id: randomId(), name: fileName, size: textFile.size, loading: true, progress: 0 };
    const textUploadController = new AbortController();
    attachments.push(pendingText);
    uploadControllers.set(pendingText.id, textUploadController);
    isSending = true;
    renderAttachments();
    try {
      const uploadedFiles = await uploadFileInChunks(
        textFile,
        conversation.workspaceId || state.sessionId,
        (progress) => {
          pendingText.progress = progress;
          renderAttachments();
        },
        "composer_text",
        textUploadController.signal,
      );
      const preview = String(uploadedFiles[0]?.preview || "");
      if (!preview || utf8ByteLength(preview) > INLINE_TEXT_BYTES) {
        throw new Error("服务器返回的长文字预览无效");
      }
      attachments = attachments.filter((item) => item !== pendingText);
      attachments.push(...uploadedFiles);
      outgoingFiles = [...outgoingFiles, ...uploadedFiles];
      textFile = null;
      elements.input.value = "";
      text = preview;
      retryText = preview;
      renderAttachments();
    } catch (error) {
      attachments = attachments.filter((item) => item !== pendingText);
      isSending = false;
      renderAttachments();
      showComposerError(error?.name === "AbortError" ? "已取消长文字上传" : `长文字上传失败：${userFacingError(error)}`);
      return;
    } finally {
      uploadControllers.delete(pendingText.id);
    }
  }
  const history = conversation.messages
    .filter((message) => !message.error && !message.streaming && ["user", "assistant"].includes(message.role) && message.content)
    .slice(-60)
    .map((message) => ({ id: message.id, role: message.role, content: message.content }));
  const selectedModel = models.find((model) => model.id === state.model);
  const backendKey = selectedModel?.source === "external" ? `external:${selectedModel.providerId}` : "codex";
  const previousBackend = conversation.backendKey || (conversation.threadId ? "codex" : null);
  if (previousBackend && previousBackend !== backendKey && conversation.threadId) {
    conversation.codexThreadIds = [...new Set([
      ...(Array.isArray(conversation.codexThreadIds) ? conversation.codexThreadIds : []),
      conversation.threadId,
    ])];
    conversation.threadId = null;
  }
  if (previousBackend && previousBackend !== backendKey) {
    conversation.externalConversationId = null;
    conversation.externalContextKey = null;
  }
  conversation.backendKey = backendKey;
  const messageCreatedAt = Date.now();
  const userMessage = {
    id: randomId(),
    role: "user",
    content: text || "请分析我上传的附件。",
    attachments: outgoingFiles.map(({ id, name, size, type, source }) => ({
      id,
      name,
      size,
      type,
      source: source === "composer_text" ? "composer_text" : "file",
    })),
    createdAt: messageCreatedAt,
  };
  const assistantMessage = {
    id: randomId(),
    role: "assistant",
    content: "",
    files: [],
    streaming: true,
    createdAt: messageCreatedAt + 1,
  };
  conversation.messages.push(userMessage, assistantMessage);
  if (conversation.title === "新对话") {
    conversation.title = (titleSource || outgoingFiles[0]?.name || "附件问答").slice(0, 28);
  }
  conversation.updatedAt = Date.now();
  if (project) project.updatedAt = conversation.updatedAt;
  elements.input.value = "";
  attachments = [];
  isSending = true;
  autoScrollEnabled = true;
  saveState();
  resizeInput();
  renderAll();
  elements.stopButton.hidden = false;
  elements.sendButton.hidden = true;

  const turnRequest = new AbortController();
  activeTurnRequest = turnRequest;
  try {
    const response = await api("/api/turn", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal: turnRequest.signal,
      body: JSON.stringify({
        session_id: conversation.workspaceId || state.sessionId,
        client_conversation_id: conversation.id,
        thread_id: conversation.threadId,
        external_conversation_id: conversation.externalConversationId,
        external_context_key: conversation.externalContextKey,
        message: userMessage.content,
        history,
        model: state.model,
        effort: state.effort,
        attachments: userMessage.attachments.map(({ id, name, source }) => ({ id, name, source })),
        project_attachments: project?.useContext
          ? project.files.map(({ id, name }) => ({ id, name }))
          : [],
        project_instructions: project?.useContext ? project.instructions : "",
      }),
    });
    await consumeStream(response, conversation, assistantMessage);
  } catch (error) {
    if (error?.name === "AbortError") {
      assistantMessage.content = stripInternalAnnotations(assistantMessage.content, { removeIncomplete: true }).trimEnd()
        || "已停止生成。";
      assistantMessage.error = false;
    } else if (error.status === 429) {
      conversation.messages = conversation.messages.filter(
        (message) => ![userMessage.id, assistantMessage.id].includes(message.id),
      );
      conversation.title = previousTitle;
      elements.input.value = retryText;
      attachments = outgoingFiles;
      showComposerError(error.message);
    } else {
      if (backendKey.startsWith("external:")) {
        conversation.externalConversationId = null;
        conversation.externalContextKey = null;
      }
      assistantMessage.error = true;
      const detail = userFacingError(error, error.status);
      const partial = stripInternalAnnotations(assistantMessage.content, { removeIncomplete: true }).trimEnd();
      assistantMessage.content = partial ? `${partial}\n\n> 回答传输中断：${detail}` : detail;
    }
  } finally {
    assistantMessage.content = stripInternalAnnotations(assistantMessage.content, { removeIncomplete: true }).trimEnd();
    assistantMessage.streaming = false;
    isSending = false;
    activeTurn = null;
    if (activeTurnRequest === turnRequest) activeTurnRequest = null;
    conversation.updatedAt = Date.now();
    saveState();
    renderAll();
    elements.stopButton.hidden = true;
    elements.sendButton.hidden = false;
    updateComposer();
    refreshQuota({ quiet: true });
  }
}

async function consumeStream(response, conversation, assistantMessage) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (value) buffer += decoder.decode(value, { stream: !done });
    if (done) buffer += decoder.decode();
    const lines = buffer.split("\n");
    buffer = done ? "" : lines.pop();
    for (const line of lines) {
      if (!line.trim()) continue;
      let event;
      try {
        event = JSON.parse(line);
      } catch {
        throw new Error("回答数据格式异常，请重试");
      }
      if (event.type === "started") {
        if (!["external", "image"].includes(event.threadId)) {
          conversation.threadId = event.threadId;
          conversation.codexThreadIds = [...new Set([
            ...(Array.isArray(conversation.codexThreadIds) ? conversation.codexThreadIds : []),
            event.threadId,
          ])];
          conversation.updatedAt = Date.now();
          saveState();
        }
        if (event.mode) assistantMessage.mode = event.mode;
        activeTurn = { threadId: event.threadId, turnId: event.turnId };
      } else if (event.type === "delta") {
        assistantMessage.content += String(event.text || "");
        updateStreamingMessage(assistantMessage);
      } else if (event.type === "replace") {
        assistantMessage.content = String(event.text || "");
        updateStreamingMessage(assistantMessage);
      } else if (event.type === "done") {
        if (event.mode) assistantMessage.mode = event.mode;
        if (event.externalConversationId && event.externalContextKey) {
          conversation.externalConversationId = event.externalConversationId;
          conversation.externalContextKey = event.externalContextKey;
        } else if (["external", "image"].includes(assistantMessage.mode)) {
          conversation.externalConversationId = null;
          conversation.externalContextKey = null;
        }
        assistantMessage.files = event.files || [];
        assistantMessage.usage = normalizeTokenUsage(event.usage);
        assistantMessage.cost = normalizeCost(event.cost);
        return;
      } else if (event.type === "error") {
        if (assistantMessage.mode === "external") {
          conversation.externalConversationId = null;
          conversation.externalContextKey = null;
        }
        assistantMessage.error = true;
        const detail = userFacingError(event.message, event.status);
        const partial = stripInternalAnnotations(assistantMessage.content, { removeIncomplete: true }).trimEnd();
        assistantMessage.content = partial ? `${partial}\n\n> 回答传输中断：${detail}` : detail;
        return;
      }
    }
    if (done) break;
  }
  throw new Error("回答连接意外中断，请重试");
}

function updateStreamingMessage(message) {
  pendingStreamingMessage = message;
  if (streamingRenderScheduled) return;
  streamingRenderScheduled = true;
  requestAnimationFrame(() => {
    streamingRenderScheduled = false;
    const latest = pendingStreamingMessage;
    pendingStreamingMessage = null;
    if (!latest) return;
    const article = elements.messageList.querySelector(
      `.message[data-message-id="${latest.id}"]`,
    );
    if (!article) return;
    article.classList.remove("is-thinking");
    article.querySelector(".message-body").innerHTML = renderMarkdown(latest.content);
    if (autoScrollEnabled) {
      window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "auto" });
    }
  });
}

async function stopCurrentTurn() {
  if (!isSending) return;
  const turn = activeTurn;
  activeTurnRequest?.abort();
  elements.stopButton.disabled = true;
  try {
    if (turn) {
      await api(`/api/turn/${encodeURIComponent(turn.threadId)}/${encodeURIComponent(turn.turnId)}/interrupt`, { method: "POST" });
    }
  } catch (error) {
    showComposerError(`停止请求未送达：${userFacingError(error, error.status)}`);
  } finally {
    elements.stopButton.disabled = false;
  }
}

elements.menuButton.addEventListener("click", openSidebar);
elements.sidebarClose.addEventListener("click", closeSidebar);
elements.scrim.addEventListener("click", closeSidebar);
elements.newChat.addEventListener("click", () => {
  if (isSending) return;
  if (attachments.some((item) => item.loading)) {
    showComposerError("请等待文件上传完成后再新建聊天");
    return;
  }
  attachments = [];
  renderAttachments();
  closeHistoryMenu();
  createConversation();
});
elements.newProject.addEventListener("click", () => {
  if (isSending || attachments.some((item) => item.loading)) return;
  closeHistoryMenu();
  createProject();
});
elements.allChats.addEventListener("click", () => selectProject(null));
elements.historyMenu.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-history-action]");
  if (!button || button.disabled || !historyMenuConversationId) return;
  const conversation = state.conversations.find((item) => item.id === historyMenuConversationId);
  if (!conversation) {
    closeHistoryMenu();
    return;
  }
  const action = button.dataset.historyAction;
  if (action === "rename") {
    beginConversationRename(conversation.id);
    return;
  }
  if (action === "pin") {
    conversation.pinned = !conversation.pinned;
    conversation.pinnedAt = conversation.pinned ? Date.now() : 0;
    conversation.updatedAt = Date.now();
    saveState();
    closeHistoryMenu();
    renderHistory();
    return;
  }
  if (action === "delete") {
    if (button.dataset.confirming !== "true") {
      button.dataset.confirming = "true";
      button.classList.add("is-confirming");
      elements.historyDeleteLabel.textContent = "再次点击，永久删除";
      elements.historyMenuStatus.hidden = false;
      elements.historyMenuStatus.textContent = "将同时清理服务器中的会话数据";
      const conversationId = conversation.id;
      historyDeleteConfirmTimer = setTimeout(() => {
        if (historyMenuConversationId !== conversationId) return;
        button.dataset.confirming = "";
        button.classList.remove("is-confirming");
        elements.historyDeleteLabel.textContent = "删除";
        elements.historyMenuStatus.hidden = true;
      }, 5000);
      return;
    }
    clearTimeout(historyDeleteConfirmTimer);
    await deleteConversation(conversation.id);
  }
});
elements.themeButton.addEventListener("click", () => {
  const applyTheme = () => {
    state.theme = state.theme === "dark" ? "light" : "dark";
    saveState();
    document.documentElement.dataset.theme = state.theme;
  };
  if (document.startViewTransition && !prefersReducedMotion()) document.startViewTransition(applyTheme);
  else applyTheme();
});
elements.settingsButton.addEventListener("click", openSettings);
elements.settingsClose.addEventListener("click", closeSettings);
elements.settingsScrim.addEventListener("click", () => {
  elements.settingsPanel.focus({ preventScroll: true });
  if (providerFormDirty) setSettingsStatus("未保存的 API 配置仍在，请保存后再关闭。", "");
});
elements.projectSettingsButton.addEventListener("click", () => openProjectPanel());
elements.projectClose.addEventListener("click", closeProjectPanel);
elements.projectScrim.addEventListener("click", closeProjectPanel);
elements.projectForm.addEventListener("submit", (event) => {
  event.preventDefault();
  saveProjectDetails();
});
elements.projectFileInput.addEventListener("change", () => uploadProjectFiles(elements.projectFileInput.files));
elements.projectUploadCancel.addEventListener("click", () => activeProjectUploadController?.abort());
elements.siteUserForm.addEventListener("submit", (event) => {
  event.preventDefault();
  createSiteUser();
});
elements.siteUserAdmin.addEventListener("change", () => {
  elements.siteUserLimit.disabled = elements.siteUserAdmin.checked;
  elements.siteUserExpiry.disabled = elements.siteUserAdmin.checked;
  elements.siteUserExpiryUnit.disabled = elements.siteUserAdmin.checked;
  if (elements.siteUserAdmin.checked) {
    elements.siteUserLimit.value = "0";
    elements.siteUserExpiry.value = "0";
  }
});
elements.siteUserExpiryUnit.addEventListener("change", updateSiteUserExpiryConstraints);
updateSiteUserExpiryConstraints();
elements.codexModelRefresh.addEventListener("click", loadCodexAdminModels);
elements.providerNew.addEventListener("click", beginNewProvider);
elements.providerPreset.addEventListener("change", () => applyProviderPreset());
elements.providerBaseUrl.addEventListener("input", updateProviderTransportHint);
elements.providerForm.addEventListener("input", () => setProviderFormDirty(true, { announce: true }));
elements.providerForm.addEventListener("change", () => setProviderFormDirty(true, { announce: true }));
elements.manualModels.addEventListener("input", () => setProviderFormDirty(true, { announce: true }));
elements.providerForm.addEventListener("submit", (event) => {
  event.preventDefault();
  saveProvider({ detect: true });
});
elements.providerSaveOnly.addEventListener("click", () => saveProvider({ detect: false }));
elements.providerDetect.addEventListener("click", () => detectProvider());
elements.manualModelImport.addEventListener("click", importManualModels);
elements.modelSelect.addEventListener("change", updateEfforts);
elements.modelTrigger.addEventListener("click", () => openMobilePicker(elements.modelSelect, "选择模型", elements.modelTrigger));
elements.effortTrigger.addEventListener("click", () => openMobilePicker(elements.effortSelect, "选择思考强度", elements.effortTrigger));
elements.pickerClose.addEventListener("click", () => closeMobilePicker());
elements.pickerScrim.addEventListener("click", () => closeMobilePicker());
elements.effortSelect.addEventListener("change", () => {
  state.effort = elements.effortSelect.value;
  saveState();
  updatePickerTriggers();
});
elements.input.addEventListener("input", () => {
  resizeInput();
  updateComposer();
});
elements.input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    sendMessage();
  }
});
elements.fileInput.addEventListener("change", () => {
  const selectedFiles = [...elements.fileInput.files];
  elements.fileInput.value = "";
  uploadSelectedFiles(selectedFiles);
});
elements.sendButton.addEventListener("click", sendMessage);
elements.stopButton.addEventListener("click", stopCurrentTurn);
elements.accountAction.addEventListener("click", () => {
  if (elements.accountAction.dataset.action === "logout") disconnectAccount();
  else startDeviceLogin();
});
elements.copyCode.addEventListener("click", async () => {
  await navigator.clipboard.writeText(elements.deviceCode.textContent);
  elements.copyCode.textContent = "已复制";
  setTimeout(() => { elements.copyCode.textContent = "复制代码"; }, 1200);
});
elements.dialog.addEventListener("close", () => clearInterval(loginPoll));
document.querySelectorAll("[data-prompt]").forEach((button) => {
  button.addEventListener("click", () => {
    elements.input.value = button.dataset.prompt;
    resizeInput();
    updateComposer();
    elements.input.focus();
  });
});
window.addEventListener("paste", (event) => {
  const images = [...(event.clipboardData?.files || [])].filter((file) => file.type.startsWith("image/"));
  if (images.length) uploadSelectedFiles(images);
});
window.addEventListener("scroll", () => {
  const distanceFromBottom = document.documentElement.scrollHeight - window.scrollY - window.innerHeight;
  autoScrollEnabled = distanceFromBottom < 180;
}, { passive: true });
document.addEventListener("pointerdown", (event) => {
  if (!elements.historyMenu.hidden && !elements.historyMenu.contains(event.target) && !event.target.closest(".history-menu-trigger")) {
    closeHistoryMenu();
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !elements.picker.hidden) {
    closeMobilePicker();
    return;
  }
  if (event.key === "Tab" && elements.settingsPanel.classList.contains("is-open")) {
    const focusable = settingsFocusable();
    if (!focusable.length) {
      event.preventDefault();
      elements.settingsPanel.focus();
      return;
    }
    const first = focusable[0];
    const last = focusable.at(-1);
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }
  if (event.key === "Tab" && elements.projectPanel.classList.contains("is-open")) {
    const focusable = projectFocusable();
    if (!focusable.length) {
      event.preventDefault();
      elements.projectPanel.focus();
      return;
    }
    const first = focusable[0];
    const last = focusable.at(-1);
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }
  if (event.key === "Escape" && !elements.historyMenu.hidden) closeHistoryMenu();
  if (event.key === "Escape" && elements.settingsPanel.classList.contains("is-open")) closeSettings();
  if (event.key === "Escape" && elements.projectPanel.classList.contains("is-open")) closeProjectPanel();
});
elements.historyList.closest(".history").addEventListener("scroll", closeHistoryMenu, { passive: true });
window.addEventListener("resize", closeHistoryMenu, { passive: true });
window.addEventListener("resize", () => {
  if (window.innerWidth > 700 && !elements.picker.hidden) closeMobilePicker({ restoreFocus: false });
}, { passive: true });
window.addEventListener("blur", closeHistoryMenu);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && Date.now() - quotaLastFetched > 60_000) refreshQuota({ quiet: true });
  if (!document.hidden && Date.now() - latencyLastFetched > 3_000) refreshLatency();
  if (!document.hidden) syncCloudConversations({ force: true });
});
window.addEventListener("beforeunload", (event) => {
  if (!providerFormDirty) return;
  event.preventDefault();
  event.returnValue = "";
});
window.addEventListener("online", () => syncCloudConversations({ force: true }));
function hasDraggedFiles(event) {
  return Array.from(event.dataTransfer?.types || []).includes("Files");
}

function setComposerDropState(active) {
  elements.composer.classList.toggle("is-file-dragover", active);
  elements.composerDropCue.setAttribute("aria-hidden", String(!active));
}

function clearComposerDropState() {
  composerDragDepth = 0;
  setComposerDropState(false);
}

elements.composer.addEventListener("dragenter", (event) => {
  if (!hasDraggedFiles(event)) return;
  event.preventDefault();
  composerDragDepth += 1;
  setComposerDropState(true);
});
elements.composer.addEventListener("dragover", (event) => {
  if (!hasDraggedFiles(event)) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = "copy";
});
elements.composer.addEventListener("dragleave", () => {
  composerDragDepth = Math.max(0, composerDragDepth - 1);
  if (composerDragDepth === 0) setComposerDropState(false);
});
elements.composer.addEventListener("drop", (event) => {
  if (!hasDraggedFiles(event)) return;
  event.preventDefault();
  const files = event.dataTransfer?.files;
  clearComposerDropState();
  if (files?.length) uploadSelectedFiles(files);
});
window.addEventListener("dragend", clearComposerDropState);
window.addEventListener("dragover", (event) => {
  if (hasDraggedFiles(event)) event.preventDefault();
});
window.addEventListener("drop", (event) => {
  if (hasDraggedFiles(event)) event.preventDefault();
  clearComposerDropState();
});

if (!sortedConversations().some((item) => item.id === state.activeId)) {
  state.activeId = sortedConversations()[0]?.id || null;
}
renderAll();
resizeInput();
syncCloudConversations({ force: true, initial: true });
checkAccount();
refreshLatency();
setInterval(() => refreshQuota({ quiet: true }), 5 * 60_000);
setInterval(refreshLatency, 3_000);
setInterval(() => {
  if (!document.hidden) syncCloudConversations({ force: true });
}, 15_000);
