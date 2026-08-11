"use strict";

const PAGE_SIZE = 50;
const REFRESH_INTERVAL_MS = 15000;

const state = {
  user: null,
  csrfToken: "",
  messages: [],
  selectedId: null,
  hasMore: false,
  loading: false,
  refreshTimer: null,
  directory: [],
  directoryRevision: 0,
  accessRevision: 0,
  selectedDepartmentId: "",
  departmentGrants: new Set(),
  userGrants: new Set(),
  members: [],
  memberOffset: 0,
  memberHasMore: false,
  adminLoaded: false,
  adminLoading: false,
  syncTimer: null,
};

const elements = {
  loginView: document.querySelector("#login-view"),
  inboxView: document.querySelector("#inbox-view"),
  permissionsView: document.querySelector("#permissions-view"),
  mainNav: document.querySelector("#main-nav"),
  inboxNav: document.querySelector("#inbox-nav"),
  permissionsNav: document.querySelector("#permissions-nav"),
  topbarActions: document.querySelector("#topbar-actions"),
  signedInUser: document.querySelector("#signed-in-user"),
  feishuLogin: document.querySelector("#feishu-login"),
  loginError: document.querySelector("#login-error"),
  logoutButton: document.querySelector("#logout-button"),
  searchInput: document.querySelector("#search-input"),
  refreshButton: document.querySelector("#refresh-button"),
  autoRefresh: document.querySelector("#auto-refresh"),
  messageList: document.querySelector("#message-list"),
  listEmpty: document.querySelector("#list-empty"),
  loadMore: document.querySelector("#load-more"),
  listFooter: document.querySelector("#list-footer"),
  detailEmpty: document.querySelector("#detail-empty"),
  detailCard: document.querySelector("#detail-card"),
  detailSender: document.querySelector("#detail-sender"),
  detailContent: document.querySelector("#detail-content"),
  verificationRow: document.querySelector("#verification-row"),
  detailCode: document.querySelector("#detail-code"),
  detailTime: document.querySelector("#detail-time"),
  detailSim: document.querySelector("#detail-sim"),
  detailDevice: document.querySelector("#detail-device"),
  detailVersion: document.querySelector("#detail-version"),
  copySender: document.querySelector("#copy-sender"),
  backButton: document.querySelector("#back-button"),
  serviceState: document.querySelector("#service-state"),
  serviceStateText: document.querySelector("#service-state-text"),
  toast: document.querySelector("#toast"),
  syncDirectory: document.querySelector("#sync-directory"),
  directoryStatus: document.querySelector("#directory-status"),
  directoryStatusText: document.querySelector("#directory-status-text"),
  directorySyncTime: document.querySelector("#directory-sync-time"),
  departmentSearch: document.querySelector("#department-search"),
  directoryTree: document.querySelector("#directory-tree"),
  directoryEmpty: document.querySelector("#directory-empty"),
  departmentGrantCount: document.querySelector("#department-grant-count"),
  memberTitle: document.querySelector("#member-title"),
  selectedDepartmentLabel: document.querySelector("#selected-department-label"),
  userGrantCount: document.querySelector("#user-grant-count"),
  memberSearch: document.querySelector("#member-search"),
  memberList: document.querySelector("#member-list"),
  memberEmpty: document.querySelector("#member-empty"),
  memberLoadMore: document.querySelector("#member-load-more"),
  accessSummary: document.querySelector("#access-summary"),
  saveAccess: document.querySelector("#save-access"),
};

function relativeUrl(path) {
  return new URL(path, window.location.href);
}

function apiUrl(beforeId) {
  const url = relativeUrl("v1/messages");
  url.searchParams.set("limit", String(PAGE_SIZE));
  if (beforeId) url.searchParams.set("before_id", String(beforeId));
  return url;
}

function createSvg(paths, viewBox = "0 0 24 24") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", viewBox);
  svg.setAttribute("aria-hidden", "true");
  paths.forEach((definition) => {
    const node = document.createElementNS("http://www.w3.org/2000/svg", definition.tag || "path");
    Object.entries(definition.attributes).forEach(([name, value]) => node.setAttribute(name, value));
    svg.append(node);
  });
  return svg;
}

function senderIcon() {
  return createSvg([
    { tag: "circle", attributes: { cx: "12", cy: "8", r: "3.5" } },
    { attributes: { d: "M5 21v-2a7 7 0 0 1 14 0v2" } },
  ]);
}

function chevronIcon() {
  return createSvg([{ attributes: { d: "m9 18 6-6-6-6" } }]);
}

function setServiceState(ok) {
  elements.serviceState.classList.toggle("is-error", !ok);
  elements.serviceStateText.textContent = ok ? "服务正常" : "连接异常";
}

function setLoginError(message = "") {
  elements.loginError.textContent = message;
  elements.loginError.hidden = !message;
}

function setLoginLoading(loading) {
  elements.feishuLogin.disabled = loading;
  elements.feishuLogin.classList.toggle("is-loading", loading);
}

function showToast(message) {
  elements.toast.textContent = message;
  elements.toast.hidden = false;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => { elements.toast.hidden = true; }, 2200);
}

function showLogin(message = "") {
  stopAutoRefresh();
  stopDirectoryPolling();
  state.user = null;
  state.csrfToken = "";
  state.messages = [];
  state.selectedId = null;
  document.body.classList.remove("detail-open");
  elements.inboxView.hidden = true;
  elements.permissionsView.hidden = true;
  elements.mainNav.hidden = true;
  elements.topbarActions.hidden = true;
  elements.loginView.hidden = false;
  setLoginLoading(false);
  setLoginError(message);
  window.setTimeout(() => elements.feishuLogin.focus(), 0);
}

function showInbox(user) {
  state.user = user;
  elements.signedInUser.textContent = user.name || "飞书用户";
  elements.loginView.hidden = true;
  elements.mainNav.hidden = false;
  elements.permissionsNav.hidden = user.role !== "admin";
  elements.topbarActions.hidden = false;
  switchView(window.location.hash === "#permissions" ? "permissions" : "inbox");
}

function switchView(view) {
  const permissionsAllowed = view === "permissions" && state.user?.role === "admin";
  elements.inboxView.hidden = permissionsAllowed;
  elements.permissionsView.hidden = !permissionsAllowed;
  elements.inboxNav.classList.toggle("is-active", !permissionsAllowed);
  elements.permissionsNav.classList.toggle("is-active", permissionsAllowed);
  if (permissionsAllowed) {
    stopAutoRefresh();
    if (window.location.hash !== "#permissions") window.history.replaceState({}, "", "#permissions");
    loadAdminData();
  } else {
    stopDirectoryPolling();
    if (window.location.hash) window.history.replaceState({}, "", window.location.pathname);
    startAutoRefresh();
    if (!state.messages.length) fetchMessages();
  }
}

function normalizeDate(value) {
  if (!value) return null;
  const normalized = value.includes("T") ? value : value.replace(" ", "T");
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? null : date;
}

function fullDate(value) {
  const date = normalizeDate(value);
  if (!date) return value || "未提供";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(date).replaceAll("/", "-");
}

function shortDate(value) {
  const date = normalizeDate(value);
  if (!date) return value || "";
  const today = new Date();
  const sameDay = date.getFullYear() === today.getFullYear()
    && date.getMonth() === today.getMonth()
    && date.getDate() === today.getDate();
  if (sameDay) {
    return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
  }
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit" }).format(date);
}

function messageTime(message) {
  return message.source_received_at || message.received_at;
}

function simLabel(message) {
  if (message.sim_slot && message.sim_phone) return `${message.sim_slot} · ${message.sim_phone}`;
  return message.sim_phone || message.sim_slot || message.sim_info || "未提供";
}

function filteredMessages() {
  const query = elements.searchInput.value.trim().toLocaleLowerCase("zh-CN");
  if (!query) return state.messages;
  return state.messages.filter((message) =>
    `${message.sender} ${message.content} ${message.verification_code || ""} ${simLabel(message)}`
      .toLocaleLowerCase("zh-CN").includes(query));
}

function selectMessage(id, openMobile = false) {
  state.selectedId = id;
  renderList();
  renderDetail();
  if (openMobile && window.matchMedia("(max-width: 760px)").matches) {
    document.body.classList.add("detail-open");
    elements.backButton.focus();
  }
}

function buildMessageItem(message) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "message-item";
  button.setAttribute("role", "option");
  button.setAttribute("aria-selected", String(message.id === state.selectedId));
  if (message.id === state.selectedId) button.classList.add("is-selected");

  const avatar = document.createElement("span");
  avatar.className = "message-avatar";
  avatar.append(senderIcon());

  const main = document.createElement("span");
  main.className = "message-main";
  const head = document.createElement("span");
  head.className = "message-head";
  const sender = document.createElement("span");
  sender.className = "message-sender";
  sender.textContent = message.sender || "未知号码";
  const time = document.createElement("time");
  time.className = "message-time";
  time.textContent = shortDate(messageTime(message));
  head.append(sender, time);
  const preview = document.createElement("span");
  preview.className = "message-preview";
  preview.textContent = message.verification_code
    ? `验证码 ${message.verification_code} · ${message.content}`
    : message.content;
  main.append(head, preview);

  const chevron = document.createElement("span");
  chevron.className = "message-chevron";
  chevron.append(chevronIcon());
  button.append(avatar, main, chevron);
  button.addEventListener("click", () => selectMessage(message.id, true));
  return button;
}

function renderList() {
  const messages = filteredMessages();
  elements.messageList.replaceChildren(...messages.map(buildMessageItem));
  elements.messageList.hidden = messages.length === 0;
  elements.listEmpty.hidden = messages.length !== 0;
  const queryActive = Boolean(elements.searchInput.value.trim());
  elements.listEmpty.querySelector("strong").textContent = queryActive ? "没有匹配结果" : "暂无短信";
  elements.listEmpty.querySelector("span").textContent = queryActive ? "请尝试其他号码或关键词" : "收到新短信后会自动出现在这里";
  elements.listFooter.textContent = queryActive ? `找到 ${messages.length} 条` : `共 ${state.messages.length} 条`;
  elements.loadMore.hidden = !state.hasMore || queryActive;
}

function renderDetail() {
  const selected = state.messages.find((message) => message.id === state.selectedId);
  elements.detailEmpty.hidden = Boolean(selected);
  elements.detailCard.hidden = !selected;
  if (!selected) return;
  elements.detailSender.textContent = selected.sender || "未知号码";
  elements.detailContent.textContent = selected.content;
  elements.verificationRow.hidden = !selected.verification_code;
  elements.detailCode.textContent = selected.verification_code || "";
  elements.detailTime.textContent = fullDate(messageTime(selected));
  elements.detailSim.textContent = simLabel(selected);
  elements.detailDevice.textContent = selected.device_name || "未提供";
  elements.detailVersion.textContent = selected.app_version || "未提供";
}

function render() {
  if (state.selectedId === null && state.messages.length) state.selectedId = state.messages[0].id;
  renderList();
  renderDetail();
}

function stopDirectoryPolling() {
  if (state.syncTimer) window.clearTimeout(state.syncTimer);
  state.syncTimer = null;
}

function departmentById(id) {
  return state.directory.find((item) => item.department_id === id) || null;
}

function isDepartmentCovered(id, includeSelf = true) {
  let current = includeSelf ? id : departmentById(id)?.parent_department_id;
  const visited = new Set();
  while (current && !visited.has(current)) {
    if (state.departmentGrants.has(current)) return true;
    visited.add(current);
    current = departmentById(current)?.parent_department_id || "";
  }
  return false;
}

function isDescendantOf(id, parentId) {
  let current = departmentById(id)?.parent_department_id || "";
  const visited = new Set();
  while (current && !visited.has(current)) {
    if (current === parentId) return true;
    visited.add(current);
    current = departmentById(current)?.parent_department_id || "";
  }
  return false;
}

function renderAccessSummary(effectiveCount = null) {
  elements.departmentGrantCount.textContent = `${state.departmentGrants.size} 个部门`;
  elements.userGrantCount.textContent = `${state.userGrants.size} 人单独授权`;
  const grants = `${state.departmentGrants.size} 个部门 + ${state.userGrants.size} 名成员`;
  elements.accessSummary.textContent = effectiveCount === null
    ? `待保存：${grants}`
    : `已授权 ${effectiveCount} 名普通用户（${grants}）`;
}

function buildDepartmentRow(department, depth) {
  const row = document.createElement("div");
  row.className = "directory-row";
  row.setAttribute("role", "treeitem");
  row.setAttribute("aria-level", String(depth + 1));
  if (department.department_id === state.selectedDepartmentId) row.classList.add("is-selected");
  row.style.setProperty("--tree-depth", String(depth));

  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.className = "permission-checkbox";
  const inherited = isDepartmentCovered(department.department_id, false);
  checkbox.checked = inherited || state.departmentGrants.has(department.department_id);
  checkbox.disabled = inherited;
  checkbox.setAttribute("aria-label", `授权部门 ${department.name}`);
  checkbox.addEventListener("change", () => {
    if (checkbox.checked) {
      state.departmentGrants.add(department.department_id);
      for (const existing of [...state.departmentGrants]) {
        if (existing !== department.department_id && isDescendantOf(existing, department.department_id)) {
          state.departmentGrants.delete(existing);
        }
      }
    } else {
      state.departmentGrants.delete(department.department_id);
    }
    renderDirectoryTree();
    renderMembers();
    renderAccessSummary();
  });

  const select = document.createElement("button");
  select.type = "button";
  select.className = "directory-name";
  select.textContent = department.name;
  select.addEventListener("click", () => selectDepartment(department.department_id));

  const count = document.createElement("span");
  count.className = "directory-member-count";
  count.textContent = String(department.member_count || 0);
  if (inherited) {
    const inheritedBadge = document.createElement("span");
    inheritedBadge.className = "inherited-badge";
    inheritedBadge.textContent = "已继承";
    row.append(checkbox, select, inheritedBadge, count);
  } else {
    row.append(checkbox, select, count);
  }
  return row;
}

function renderDirectoryTree() {
  const query = elements.departmentSearch.value.trim().toLocaleLowerCase("zh-CN");
  const children = new Map();
  for (const item of state.directory) {
    const parent = item.parent_department_id || "";
    if (!children.has(parent)) children.set(parent, []);
    children.get(parent).push(item);
  }
  for (const items of children.values()) {
    items.sort((a, b) => (a.sort_order - b.sort_order) || a.name.localeCompare(b.name, "zh-CN"));
  }
  const nodes = [];
  if (query) {
    for (const item of state.directory.filter((entry) => entry.name.toLocaleLowerCase("zh-CN").includes(query))) {
      nodes.push(buildDepartmentRow(item, 0));
    }
  } else {
    const visit = (item, depth, seen) => {
      if (seen.has(item.department_id)) return;
      const nextSeen = new Set(seen);
      nextSeen.add(item.department_id);
      nodes.push(buildDepartmentRow(item, depth));
      for (const child of children.get(item.department_id) || []) visit(child, depth + 1, nextSeen);
    };
    const roots = children.get("") || state.directory.filter((item) => !departmentById(item.parent_department_id));
    for (const root of roots) visit(root, 0, new Set());
  }
  elements.directoryTree.replaceChildren(...nodes);
  elements.directoryTree.hidden = nodes.length === 0;
  elements.directoryEmpty.hidden = nodes.length !== 0;
}

function buildMemberRow(user) {
  const row = document.createElement("label");
  row.className = "member-row";
  const inherited = user.is_admin || user.department_ids.some((id) => isDepartmentCovered(id));
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.className = "permission-checkbox";
  checkbox.checked = inherited || state.userGrants.has(user.open_id);
  checkbox.disabled = inherited;
  checkbox.addEventListener("change", () => {
    if (checkbox.checked) state.userGrants.add(user.open_id);
    else state.userGrants.delete(user.open_id);
    renderAccessSummary();
  });
  const avatar = document.createElement("span");
  avatar.className = "member-avatar";
  avatar.textContent = (user.name || "飞").slice(0, 1);
  const identity = document.createElement("span");
  identity.className = "member-identity";
  const name = document.createElement("strong");
  name.textContent = user.name || "飞书用户";
  const hint = document.createElement("span");
  hint.textContent = user.is_admin ? "管理员 · 服务端保护" : inherited ? "由部门授权" : "可单独授权";
  identity.append(name, hint);
  row.append(checkbox, avatar, identity);
  return row;
}

function renderMembers() {
  const nodes = state.members.map(buildMemberRow);
  elements.memberList.replaceChildren(...nodes);
  elements.memberList.hidden = nodes.length === 0;
  elements.memberEmpty.hidden = nodes.length !== 0;
  if (!nodes.length) {
    elements.memberEmpty.textContent = state.selectedDepartmentId ? "当前部门没有匹配成员" : "请选择一个部门";
  }
  elements.memberLoadMore.hidden = !state.memberHasMore;
}

async function fetchMembers({ append = false } = {}) {
  if (!state.selectedDepartmentId) {
    state.members = [];
    renderMembers();
    return;
  }
  const offset = append ? state.memberOffset : 0;
  const url = relativeUrl("v1/admin/directory/users");
  url.searchParams.set("department_id", state.selectedDepartmentId);
  url.searchParams.set("query", elements.memberSearch.value.trim());
  url.searchParams.set("limit", "100");
  url.searchParams.set("offset", String(offset));
  try {
    const response = await fetch(url, { cache: "no-store" });
    if (response.status === 401) throw new Error("unauthorized");
    if (response.status === 403) throw new Error("forbidden");
    if (!response.ok) throw new Error(`http_${response.status}`);
    const payload = await response.json();
    state.members = append ? [...state.members, ...(payload.users || [])] : (payload.users || []);
    state.memberOffset = offset + (payload.users || []).length;
    state.memberHasMore = Boolean(payload.has_more);
    renderMembers();
  } catch (error) {
    if (error.message === "unauthorized") showLogin("登录已过期，请重新使用飞书登录");
    else if (error.message === "forbidden") switchView("inbox");
    else showToast("读取成员失败，请稍后重试");
  }
}

function selectDepartment(id) {
  state.selectedDepartmentId = id;
  const department = departmentById(id);
  elements.memberTitle.textContent = department?.name || "成员";
  elements.selectedDepartmentLabel.textContent = "直属成员；部门授权同时覆盖全部子部门";
  elements.memberSearch.value = "";
  state.members = [];
  state.memberOffset = 0;
  renderDirectoryTree();
  fetchMembers();
}

function renderSyncStatus(sync) {
  const status = sync?.status || "idle";
  elements.directoryStatus.classList.toggle("is-running", status === "running");
  elements.directoryStatus.classList.toggle("is-error", status === "failed");
  elements.syncDirectory.disabled = status === "running";
  elements.syncDirectory.classList.toggle("is-loading", status === "running");
  if (status === "running") elements.directoryStatusText.textContent = "正在从飞书同步企业架构，部门较多时可能需要数分钟";
  else if (status === "failed") elements.directoryStatusText.textContent = `同步失败：${sync.error || "未知错误"}`;
  else if (status === "success") elements.directoryStatusText.textContent = "企业架构同步完成";
  else elements.directoryStatusText.textContent = "尚未同步企业架构";
  elements.directorySyncTime.textContent = sync?.finished_at ? `最近完成 ${fullDate(sync.finished_at)}` : "";
  stopDirectoryPolling();
  if (status === "running" && !elements.permissionsView.hidden) {
    state.syncTimer = window.setTimeout(() => refreshDirectory(), 5000);
  }
}

async function refreshDirectory() {
  try {
    const response = await fetch(relativeUrl("v1/admin/directory"), { cache: "no-store" });
    if (response.status === 401) throw new Error("unauthorized");
    if (response.status === 403) throw new Error("forbidden");
    if (!response.ok) throw new Error(`http_${response.status}`);
    const payload = await response.json();
    const changed = state.directoryRevision !== Number(payload.directory_revision || 0);
    state.directory = Array.isArray(payload.departments) ? payload.departments : [];
    state.directoryRevision = Number(payload.directory_revision || 0);
    renderSyncStatus(payload.sync);
    renderDirectoryTree();
    if (!state.selectedDepartmentId && state.directory.length) selectDepartment(state.directory[0].department_id);
    else if (changed && state.selectedDepartmentId) fetchMembers();
    return payload;
  } catch (error) {
    if (error.message === "unauthorized") showLogin("登录已过期，请重新使用飞书登录");
    else if (error.message === "forbidden") switchView("inbox");
    else showToast("读取企业架构失败");
    return null;
  }
}

async function loadAdminData() {
  if (state.adminLoading) return;
  state.adminLoading = true;
  try {
    const directory = await refreshDirectory();
    if (!directory) return;
    const response = await fetch(relativeUrl("v1/admin/access"), { cache: "no-store" });
    if (!response.ok) throw new Error(`http_${response.status}`);
    const access = await response.json();
    state.accessRevision = Number(access.revision || 0);
    state.departmentGrants = new Set(access.department_ids || []);
    state.userGrants = new Set(access.user_open_ids || []);
    state.adminLoaded = true;
    renderDirectoryTree();
    renderMembers();
    renderAccessSummary(Number(access.effective_user_count || 0));
  } catch {
    showToast("读取权限配置失败");
  } finally {
    state.adminLoading = false;
  }
}

async function startDirectorySync() {
  elements.syncDirectory.disabled = true;
  try {
    const response = await fetch(relativeUrl("v1/admin/directory/sync"), {
      method: "POST",
      headers: { "X-CSRF-Token": state.csrfToken },
    });
    if (response.status !== 202 && response.status !== 409) throw new Error(`http_${response.status}`);
    await refreshDirectory();
  } catch {
    elements.syncDirectory.disabled = false;
    showToast("启动同步失败，请稍后重试");
  }
}

async function saveAccess() {
  elements.saveAccess.disabled = true;
  elements.saveAccess.classList.add("is-loading");
  try {
    const response = await fetch(relativeUrl("v1/admin/access"), {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": state.csrfToken,
      },
      body: JSON.stringify({
        revision: state.accessRevision,
        department_ids: [...state.departmentGrants],
        user_open_ids: [...state.userGrants],
      }),
    });
    if (response.status === 409) {
      showToast("权限已被其他页面更新，正在重新加载");
      state.adminLoaded = false;
      await loadAdminData();
      return;
    }
    if (!response.ok) throw new Error(`http_${response.status}`);
    const payload = await response.json();
    state.accessRevision = Number(payload.revision || 0);
    state.departmentGrants = new Set(payload.department_ids || []);
    state.userGrants = new Set(payload.user_open_ids || []);
    renderDirectoryTree();
    renderMembers();
    renderAccessSummary(Number(payload.effective_user_count || 0));
    showToast("权限已保存");
  } catch {
    showToast("保存权限失败，请稍后重试");
  } finally {
    elements.saveAccess.disabled = false;
    elements.saveAccess.classList.remove("is-loading");
  }
}

async function copyText(value, successMessage) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
    } else {
      const textarea = document.createElement("textarea");
      textarea.value = value;
      textarea.setAttribute("readonly", "");
      textarea.style.position = "fixed";
      textarea.style.opacity = "0";
      document.body.append(textarea);
      textarea.select();
      if (!document.execCommand("copy")) throw new Error("copy_failed");
      textarea.remove();
    }
    showToast(successMessage);
  } catch {
    showToast("复制失败，请手动复制");
  }
}

async function fetchMessages({ append = false, quiet = false } = {}) {
  if (state.loading) return false;
  state.loading = true;
  if (!quiet) elements.refreshButton.classList.add("is-loading");
  const beforeId = append && state.messages.length ? state.messages[state.messages.length - 1].id : null;
  try {
    const response = await fetch(apiUrl(beforeId), { cache: "no-store" });
    if (response.status === 401) throw new Error("unauthorized");
    if (!response.ok) throw new Error(`http_${response.status}`);
    const payload = await response.json();
    const incoming = Array.isArray(payload.messages) ? payload.messages : [];
    if (append) {
      const existing = new Set(state.messages.map((message) => message.id));
      state.messages.push(...incoming.filter((message) => !existing.has(message.id)));
    } else {
      state.messages = incoming;
      if (!state.messages.some((message) => message.id === state.selectedId)) {
        state.selectedId = state.messages.length ? state.messages[0].id : null;
      }
    }
    state.hasMore = incoming.length === PAGE_SIZE;
    setServiceState(true);
    render();
    return true;
  } catch (error) {
    if (error.message === "unauthorized") {
      showLogin("登录已过期，请重新使用飞书登录");
    } else {
      setServiceState(false);
      if (!quiet) showToast("连接服务失败，请稍后重试");
    }
    return false;
  } finally {
    state.loading = false;
    elements.refreshButton.classList.remove("is-loading");
  }
}

async function checkSession() {
  try {
    const response = await fetch(relativeUrl("auth/session"), { cache: "no-store" });
    if (!response.ok) return null;
    const payload = await response.json();
    return payload.user ? payload : null;
  } catch {
    return null;
  }
}

function startAutoRefresh() {
  stopAutoRefresh();
  if (!elements.autoRefresh.checked) return;
  state.refreshTimer = window.setInterval(() => fetchMessages({ quiet: true }), REFRESH_INTERVAL_MS);
}

function stopAutoRefresh() {
  if (state.refreshTimer) window.clearInterval(state.refreshTimer);
  state.refreshTimer = null;
}

elements.feishuLogin.addEventListener("click", () => {
  setLoginError();
  setLoginLoading(true);
  window.location.assign(relativeUrl("auth/login"));
});

elements.logoutButton.addEventListener("click", async () => {
  try {
    await fetch(relativeUrl("auth/logout"), { method: "POST" });
  } finally {
    showLogin();
  }
});
elements.inboxNav.addEventListener("click", () => switchView("inbox"));
elements.permissionsNav.addEventListener("click", () => switchView("permissions"));
elements.refreshButton.addEventListener("click", () => fetchMessages());
elements.autoRefresh.addEventListener("change", startAutoRefresh);
elements.searchInput.addEventListener("input", render);
elements.loadMore.addEventListener("click", () => fetchMessages({ append: true }));
elements.backButton.addEventListener("click", () => {
  document.body.classList.remove("detail-open");
  const selected = elements.messageList.querySelector(".is-selected");
  if (selected) selected.focus();
});
elements.copySender.addEventListener("click", () =>
  copyText(elements.detailSender.textContent, "号码已复制"));
elements.detailCode.addEventListener("click", () =>
  copyText(elements.detailCode.textContent, "验证码已复制"));
elements.syncDirectory.addEventListener("click", startDirectorySync);
elements.saveAccess.addEventListener("click", saveAccess);
elements.departmentSearch.addEventListener("input", renderDirectoryTree);
elements.memberLoadMore.addEventListener("click", () => fetchMembers({ append: true }));
elements.memberSearch.addEventListener("input", () => {
  window.clearTimeout(elements.memberSearch.timer);
  elements.memberSearch.timer = window.setTimeout(() => fetchMembers(), 320);
});
window.addEventListener("hashchange", () => {
  if (!state.user) return;
  switchView(window.location.hash === "#permissions" ? "permissions" : "inbox");
});

window.addEventListener("pageshow", async () => {
  const query = new URLSearchParams(window.location.search);
  const loginError = query.get("login_error") ? "飞书授权未完成，请重试" : "";
  if (query.has("login_error")) window.history.replaceState({}, "", window.location.pathname);
  const session = await checkSession();
  if (!session) {
    showLogin(loginError);
    return;
  }
  state.csrfToken = session.csrf_token || "";
  showInbox(session.user);
}, { once: true });
