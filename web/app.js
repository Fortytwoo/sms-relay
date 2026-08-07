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
  state.user = null;
  state.messages = [];
  state.selectedId = null;
  document.body.classList.remove("detail-open");
  elements.inboxView.hidden = true;
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
  elements.inboxView.hidden = false;
  elements.topbarActions.hidden = false;
  startAutoRefresh();
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
    return payload.user || null;
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

window.addEventListener("pageshow", async () => {
  const query = new URLSearchParams(window.location.search);
  const loginError = query.get("login_error") ? "飞书授权未完成，请重试" : "";
  if (query.has("login_error")) window.history.replaceState({}, "", window.location.pathname);
  const user = await checkSession();
  if (!user) {
    showLogin(loginError);
    return;
  }
  showInbox(user);
  await fetchMessages();
}, { once: true });
