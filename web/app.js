"use strict";

const PAGE_SIZE = 50;
const REFRESH_INTERVAL_MS = 15000;

const state = {
  user: null,
  messages: [],
  selectedId: null,
  hasMore: false,
  loading: false,
  refreshTimer: null,
};

const elements = {
  loginView: document.querySelector("#login-view"),
  inboxView: document.querySelector("#inbox-view"),
  mailSettingsView: document.querySelector("#mail-settings-view"),
  mailSettingsNav: document.querySelector("#mail-settings-nav"),
  mailAdd: document.querySelector("#mail-add"),
  mailAccountList: document.querySelector("#mail-account-list"),
  mailEditor: document.querySelector("#mail-editor"),
  mailEditorTitle: document.querySelector("#mail-editor-title"),
  mailForm: document.querySelector("#mail-form"),
  mailSave: document.querySelector("#mail-save"),
  mailCancel: document.querySelector("#mail-cancel"),
  mailFormError: document.querySelector("#mail-form-error"),
  mailSettingsNotice: document.querySelector("#mail-settings-notice"),
  mainNav: document.querySelector("#main-nav"),
  inboxNav: document.querySelector("#inbox-nav"),
  topbarActions: document.querySelector("#topbar-actions"),
  signedInUser: document.querySelector("#signed-in-user"),
  feishuLogin: document.querySelector("#feishu-login"),
  loginError: document.querySelector("#login-error"),
  logoutButton: document.querySelector("#logout-button"),
  searchInput: document.querySelector("#search-input"),
  messageType: document.querySelector("#message-type"),
  recipientFilter: document.querySelector("#recipient-filter"),
  mailboxStatus: document.querySelector("#mailbox-status"),
  subjectRow: document.querySelector("#detail-subject-row"),
  detailSubject: document.querySelector("#detail-subject"),
  receiverLabel: document.querySelector("#receiver-label"),
  refreshButton: document.querySelector("#refresh-button"),
  autoRefresh: document.querySelector("#auto-refresh"),
  messageList: document.querySelector("#message-list"),
  listEmpty: document.querySelector("#list-empty"),
  loadMore: document.querySelector("#load-more"),
  listFooter: document.querySelector("#list-footer"),
  detailEmpty: document.querySelector("#detail-empty"),
  detailCard: document.querySelector("#detail-card"),
  detailTagRow: document.querySelector("#detail-tag-row"),
  detailTag: document.querySelector("#detail-tag"),
  detailSender: document.querySelector("#detail-sender"),
  detailContent: document.querySelector("#detail-content"),
  verificationRow: document.querySelector("#verification-row"),
  detailCode: document.querySelector("#detail-code"),
  detailTime: document.querySelector("#detail-time"),
  detailPhone: document.querySelector("#detail-phone"),
  detailSim: document.querySelector("#detail-sim"),
  detailDevice: document.querySelector("#detail-device"),
  detailVersion: document.querySelector("#detail-version"),
  copySender: document.querySelector("#copy-sender"),
  backButton: document.querySelector("#back-button"),
  serviceState: document.querySelector("#service-state"),
  serviceStateText: document.querySelector("#service-state-text"),
  toast: document.querySelector("#toast"),
};

function relativeUrl(path) {
  return new URL(path, window.location.href);
}

function apiUrl(beforeId) {
  const url = relativeUrl("v1/messages");
  url.searchParams.set("limit", String(PAGE_SIZE));
  if (beforeId) url.searchParams.set("before_id", String(beforeId));
  if (elements.messageType.value) url.searchParams.set("message_type", elements.messageType.value);
  if (elements.recipientFilter.value) url.searchParams.set("recipient", elements.recipientFilter.value);
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
  state.requestController?.abort();
  state.user = null;
  state.messages = [];
  state.selectedId = null;
  elements.messageList.replaceChildren();
  elements.detailContent.textContent = "";
  elements.detailSubject.textContent = "";
  elements.recipientFilter.replaceChildren(new Option("全部邮箱", ""));
  elements.mailboxStatus.hidden = true;
  document.body.classList.remove("detail-open");
  document.body.classList.add("is-auth-view");
  elements.inboxView.hidden = true;
  elements.mailSettingsView.hidden = true;
  elements.mainNav.hidden = true;
  elements.topbarActions.hidden = true;
  elements.loginView.hidden = false;
  setLoginLoading(false);
  setLoginError(message);
  window.setTimeout(() => elements.feishuLogin.focus(), 0);
}

function showInbox(user) {
  state.user = user;
  document.body.classList.remove("is-auth-view");
  elements.signedInUser.textContent = user.name || "统一认证用户";
  elements.loginView.hidden = true;
  elements.mainNav.hidden = false;
  elements.topbarActions.hidden = false;
  elements.inboxView.hidden = false;
  elements.mailSettingsView.hidden = true;
  elements.inboxNav.classList.add("is-active");
  elements.mailSettingsNav.classList.remove("is-active");
  startAutoRefresh();
  if (!state.messages.length) fetchMessages();
}

const mailState = { accounts: [], editingId: null };

function mailNotice(message = "") {
  elements.mailSettingsNotice.textContent = message;
  elements.mailSettingsNotice.hidden = !message;
}

function mailError(message = "") {
  elements.mailFormError.textContent = message;
  elements.mailFormError.hidden = !message;
}

function showMailSettings() {
  stopAutoRefresh();
  elements.inboxView.hidden = true;
  elements.mailSettingsView.hidden = false;
  elements.inboxNav.classList.remove("is-active");
  elements.mailSettingsNav.classList.add("is-active");
  loadMailAccounts();
}

async function mailRequest(path, options = {}) {
  const response = await fetch(relativeUrl(path), { cache: "no-store", ...options });
  let body = {};
  try { body = await response.json(); } catch { /* Keep a generic error below. */ }
  if (response.status === 401) {
    showLogin("登录已过期，请重新通过统一认证登录");
    throw new Error("unauthorized");
  }
  if (!response.ok) throw new Error(body.error || `http_${response.status}`);
  return body;
}

function renderMailAccounts() {
  elements.mailAccountList.replaceChildren();
  if (!mailState.accounts.length) {
    const empty = document.createElement("p");
    empty.className = "mail-empty";
    empty.textContent = "尚未配置邮箱。点击“添加邮箱”开始。";
    elements.mailAccountList.append(empty);
    return;
  }
  for (const account of mailState.accounts) {
    const card = document.createElement("article");
    card.className = "mail-account-card";
    const heading = document.createElement("div");
    heading.className = "mail-account-heading";
    const title = document.createElement("strong");
    title.textContent = account.address;
    const id = document.createElement("span");
    id.textContent = account.id;
    heading.append(title, id);
    const detail = document.createElement("p");
    detail.textContent = `IMAP ${account.host}:${account.port} · SMTP ${account.smtp_host ? `${account.smtp_host}:${account.smtp_port}` : "未配置"}`;
    const sync = document.createElement("p");
    sync.className = "mail-account-status";
    if (account.last_error === "mailbox_auth_failed") {
      sync.textContent = "收信登录被拒绝：请检查客户端访问权限和客户端专用密码";
      sync.classList.add("is-error");
    } else if (account.last_error === "uidvalidity_changed") {
      sync.textContent = "邮箱文件夹标识已变化，请核对同步配置";
      sync.classList.add("is-error");
    } else if (account.last_error) {
      sync.textContent = "收信同步异常，请测试收信连接";
      sync.classList.add("is-error");
    } else {
      sync.textContent = account.last_success_at ? `上次收信同步：${fullDate(account.last_success_at)}` : "等待首次收信同步";
    }
    if (account.last_error && account.next_retry_at * 1000 > Date.now()) {
      sync.textContent += ` · 将于 ${new Date(account.next_retry_at * 1000).toLocaleTimeString("zh-CN")} 自动重试`;
    }
    const actions = document.createElement("div");
    actions.className = "mail-account-actions";
    for (const [label, action] of [["编辑", "edit"], ["测试收信", "receive"], ["测试发信", "send"], ["删除", "delete"]]) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "secondary-button";
      button.textContent = label;
      button.addEventListener("click", () => mailAction(account, action, button));
      actions.append(button);
    }
    card.append(heading, detail, sync, actions);
    elements.mailAccountList.append(card);
  }
}

async function loadMailAccounts() {
  try {
    const body = await mailRequest("v1/mailboxes/config");
    mailState.accounts = body.mailboxes || [];
    renderMailAccounts();
    mailNotice();
  } catch (error) {
    if (error.message !== "unauthorized") mailNotice("邮箱配置读取失败，请稍后重试");
  }
}

function openMailEditor(account = null) {
  mailState.editingId = account?.id || null;
  elements.mailForm.reset();
  elements.mailForm.elements.id.disabled = !!account;
  if (account) {
    for (const [name, value] of Object.entries(account)) {
      if (elements.mailForm.elements[name] && name !== "password" && name !== "smtp_password") {
        elements.mailForm.elements[name].value = value;
      }
    }
  }
  elements.mailEditorTitle.textContent = account ? `编辑 ${account.address}` : "添加邮箱";
  elements.mailEditor.hidden = false;
  mailError();
  elements.mailEditor.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function mailAction(account, action, button) {
  if (action === "edit") { openMailEditor(account); return; }
  if (action === "delete" && !window.confirm(`确定删除 ${account.address} 的邮箱配置？已接收邮件仍会保留。`)) return;
  button.disabled = true;
  mailNotice(action === "receive" ? "正在测试收信…" : action === "send" ? "正在发送测试邮件…" : "正在删除…");
  try {
    const base = `v1/mailboxes/config/${encodeURIComponent(account.id)}`;
    if (action === "delete") {
      await mailRequest(base, { method: "DELETE" });
      if (mailState.editingId === account.id) elements.mailEditor.hidden = true;
      await loadMailAccounts();
      mailNotice("邮箱配置已删除");
    } else {
      await mailRequest(`${base}/test/${action}`, { method: "POST" });
      mailNotice(action === "receive" ? "收信测试成功：已只读检查收件箱" : "发信测试成功：测试邮件已发往本邮箱");
    }
  } catch (error) {
    if (error.message !== "unauthorized") {
      const reason = {
        smtp_not_configured: "请先填写 SMTP 主机",
        mailbox_auth_failed: "IMAP 登录被拒绝，请核对客户端访问权限和客户端专用密码",
        imap_test_failed: "IMAP 连接或收件箱读取失败",
        smtp_test_failed: "SMTP 连接、登录或发送失败",
      }[error.message] || error.message;
      mailNotice(`${action === "delete" ? "删除" : "连通性测试"}失败：${reason}`);
    }
  } finally { button.disabled = false; }
}

elements.mailSettingsNav.addEventListener("click", showMailSettings);
elements.inboxNav.addEventListener("click", () => {
  elements.mailSettingsView.hidden = true;
  elements.inboxView.hidden = false;
  elements.mailSettingsNav.classList.remove("is-active");
  elements.inboxNav.classList.add("is-active");
  startAutoRefresh();
  fetchMessages({ quiet: true });
});
elements.mailAdd.addEventListener("click", () => openMailEditor());
elements.mailCancel.addEventListener("click", () => { elements.mailEditor.hidden = true; mailError(); });
elements.mailForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  mailError();
  const data = Object.fromEntries(new FormData(elements.mailForm));
  data.id = mailState.editingId || data.id;
  data.port = Number(data.port);
  data.smtp_port = Number(data.smtp_port);
  elements.mailSave.disabled = true;
  try {
    const path = mailState.editingId ? `v1/mailboxes/config/${encodeURIComponent(mailState.editingId)}` : "v1/mailboxes/config";
    await mailRequest(path, { method: mailState.editingId ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
    elements.mailEditor.hidden = true;
    await loadMailAccounts();
    mailNotice("邮箱配置已保存，收信线程已更新");
  } catch (error) {
    if (error.message !== "unauthorized") mailError(`保存失败：${error.message}`);
  } finally { elements.mailSave.disabled = false; }
});

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
  if (message.message_type === "email") return message.recipient || "未提供";
  if (message.sim_slot && message.sim_phone) return `${message.sim_slot} · ${message.sim_phone}`;
  return message.sim_phone || message.sim_slot || message.sim_info || "未提供";
}

function filteredMessages() {
  const query = elements.searchInput.value.trim().toLocaleLowerCase("zh-CN");
  if (!query) return state.messages;
  return state.messages.filter((message) =>
    `${message.sender} ${message.tag || ""} ${message.subject || ""} ${message.content} ${message.verification_code || ""} ${simLabel(message)}`
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
  const identity = document.createElement("span");
  identity.className = "message-identity";
  const sender = document.createElement("span");
  sender.className = "message-sender";
  sender.textContent = message.sender || "未知发送方";
  identity.append(sender);
  if (message.tag) {
    const tag = document.createElement("span");
    tag.className = "message-platform-tag";
    tag.textContent = message.tag;
    identity.append(tag);
  }
  const time = document.createElement("time");
  time.className = "message-time";
  time.textContent = shortDate(messageTime(message));
  head.append(identity, time);
  const preview = document.createElement("span");
  preview.className = "message-preview";
  preview.textContent = message.verification_code
    ? `验证码 ${message.verification_code} · ${message.content}`
    : message.content;
  main.append(head);
  if (message.message_type === "email") {
    const recipient = document.createElement("span");
    recipient.className = "message-preview";
    recipient.textContent = `邮件 · ${message.recipient} · ${message.subject || "无主题"}`;
    main.append(recipient);
  }
  main.append(preview);

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
  elements.listEmpty.querySelector("strong").textContent = queryActive ? "没有匹配结果" : "暂无消息";
  elements.listEmpty.querySelector("span").textContent = queryActive ? "请尝试其他邮箱、号码或关键词" : "收到新短信或邮件后会自动出现在这里";
  elements.listFooter.textContent = queryActive ? `找到 ${messages.length} 条` : `共 ${state.messages.length} 条`;
  elements.loadMore.hidden = !state.hasMore || queryActive;
}

function renderDetail() {
  const selected = state.messages.find((message) => message.id === state.selectedId);
  elements.detailEmpty.hidden = Boolean(selected);
  elements.detailCard.hidden = !selected;
  if (!selected) return;
  elements.detailTagRow.hidden = !selected.tag;
  elements.detailTag.textContent = selected.tag || "";
  elements.detailSender.textContent = selected.sender || "未知发送方";
  const isEmail = selected.message_type === "email";
  elements.subjectRow.hidden = !isEmail;
  elements.detailSubject.textContent = selected.subject || "无主题";
  elements.receiverLabel.textContent = isEmail ? "接收邮箱" : "接收手机号";
  for (const element of [elements.detailSim, elements.detailDevice, elements.detailVersion]) {
    element.closest(".detail-row").hidden = isEmail;
  }
  elements.detailContent.textContent = selected.content;
  elements.verificationRow.hidden = !selected.verification_code;
  elements.detailCode.textContent = selected.verification_code || "";
  elements.detailTime.textContent = fullDate(messageTime(selected));
  elements.detailPhone.textContent = (isEmail ? selected.recipient : selected.sim_phone) || "未提供";
  elements.detailSim.textContent = selected.sim_slot
    || (selected.sim_phone ? "未提供" : selected.sim_info)
    || "未提供";
  elements.detailDevice.textContent = selected.device_name || "未提供";
  elements.detailVersion.textContent = selected.app_version || "未提供";
}

function render() {
  if (state.selectedId === null && state.messages.length) state.selectedId = state.messages[0].id;
  renderList();
  renderDetail();
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
  if (state.loading && (append || quiet)) return false;
  state.requestController?.abort();
  const controller = new AbortController();
  state.requestController = controller;
  state.loading = true;
  if (!quiet) elements.refreshButton.classList.add("is-loading");
  const beforeId = append && state.messages.length ? state.messages[state.messages.length - 1].id : null;
  try {
    const response = await fetch(apiUrl(beforeId), { cache: "no-store", signal: controller.signal });
    if (response.status === 401) throw new Error("unauthorized");
    if (!response.ok) throw new Error(`http_${response.status}`);
    const payload = await response.json();
    if (controller !== state.requestController || !state.user) return false;
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
    if (!append) fetchMailboxStatus();
    return true;
  } catch (error) {
    if (error.name === "AbortError" || controller !== state.requestController) return false;
    if (error.message === "unauthorized") {
      showLogin("登录已过期，请重新通过统一认证登录");
    } else {
      setServiceState(false);
      if (!quiet) showToast("连接服务失败，请稍后重试");
    }
    return false;
  } finally {
    if (controller === state.requestController) {
      state.loading = false;
      elements.refreshButton.classList.remove("is-loading");
    }
  }
}

async function fetchMailboxStatus() {
  try {
    const response = await fetch(relativeUrl("v1/mailboxes"), { cache: "no-store" });
    if (!response.ok) throw new Error("mailbox_status_failed");
    const payload = await response.json();
    if (!state.user) return;
    const accounts = payload.mailboxes || [];
    const selected = elements.recipientFilter.value;
    const addresses = new Set(accounts.map((account) => account.address));
    state.messages.filter((message) => message.recipient).forEach((message) => addresses.add(message.recipient));
    if (selected) addresses.add(selected);
    elements.recipientFilter.replaceChildren(new Option("全部邮箱", ""),
      ...[...addresses].sort().map((address) => new Option(address, address)));
    elements.recipientFilter.value = selected;
    elements.mailboxStatus.hidden = !accounts.length;
    const failed = accounts.filter((account) => account.last_error).length;
    const pending = accounts.filter((account) => !account.last_success_at && !account.last_error).length;
    const skipped = accounts.reduce((sum, account) => sum + account.skipped_count, 0);
    elements.mailboxStatus.textContent = `${accounts.length} 个邮箱 · ${failed ? `${failed} 个同步异常` : pending ? `${pending} 个等待首次同步` : "同步正常"}${skipped ? ` · 已跳过 ${skipped} 封超大邮件` : ""}`;
  } catch {
    if (!state.user) return;
    elements.mailboxStatus.hidden = false;
    elements.mailboxStatus.textContent = "邮箱同步状态暂时不可用";
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
    const response = await fetch(relativeUrl("auth/logout"), { method: "POST" });
    if (!response.ok) throw new Error(`http_${response.status}`);
    showLogin();
  } catch {
    showToast("退出失败，授权服务暂时不可用，请稍后重试");
  }
});
elements.refreshButton.addEventListener("click", () => fetchMessages());
elements.autoRefresh.addEventListener("change", startAutoRefresh);
elements.searchInput.addEventListener("input", render);
function reloadFilteredMessages() {
  state.messages = [];
  state.selectedId = null;
  state.hasMore = false;
  render();
  fetchMessages();
}
elements.messageType.addEventListener("change", () => {
  if (elements.messageType.value === "sms") elements.recipientFilter.value = "";
  reloadFilteredMessages();
});
elements.recipientFilter.addEventListener("change", () => {
  if (elements.recipientFilter.value) elements.messageType.value = "email";
  reloadFilteredMessages();
});
elements.loadMore.addEventListener("click", () => fetchMessages({ append: true }));
elements.backButton.addEventListener("click", () => {
  document.body.classList.remove("detail-open");
  const selected = elements.messageList.querySelector(".is-selected");
  if (selected) selected.focus();
});
elements.copySender.addEventListener("click", () =>
  copyText(elements.detailSender.textContent, "发送方已复制"));
elements.detailCode.addEventListener("click", () =>
  copyText(elements.detailCode.textContent, "验证码已复制"));

window.addEventListener("pageshow", async () => {
  const query = new URLSearchParams(window.location.search);
  const loginError = query.get("login_error") ? "当前账号未获得本应用访问权限" : "";
  if (query.has("login_error")) window.history.replaceState({}, "", window.location.pathname);
  const session = await checkSession();
  if (!session) {
    showLogin(loginError);
    return;
  }
  showInbox(session.user);
}, { once: true });
