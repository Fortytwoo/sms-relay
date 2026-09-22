"use strict";
const elements = Object.fromEntries(["status", "message", "receiver", "subject", "code", "copy-code", "login", "retry"]
  .map((id) => [id, document.getElementById(id)]));
const messageId = new URLSearchParams(window.location.search).get("message_id") || "";
let loading = false;
let copied = false;

function clearMessage() {
  elements.message.hidden = true;
  for (const id of ["code", "receiver", "subject"]) elements[id].textContent = "";
}

async function copyCurrentCode() {
  const code = elements.code.textContent;
  if (!/^[A-Za-z0-9]{4,32}$/.test(code)) return;
  let timer;
  try {
    if (!document.hasFocus()) throw new Error("clipboard_unavailable");
    // Preserve user activation for the manual fallback. Some clients leave the
    // modern Clipboard promise pending indefinitely, so it must not block UI.
    const previousFocus = document.activeElement;
    const buffer = document.createElement("textarea");
    buffer.className = "copy-buffer";
    buffer.value = code;
    buffer.readOnly = true;
    document.body.append(buffer);
    let immediate = false;
    try {
      buffer.select();
      immediate = document.execCommand("copy");
    } finally {
      buffer.remove();
      previousFocus?.focus();
    }
    if (!immediate) {
      if (!navigator.clipboard || !window.isSecureContext) throw new Error("clipboard_unavailable");
      await Promise.race([
        navigator.clipboard.writeText(code),
        new Promise((_, reject) => { timer = window.setTimeout(() => reject(new Error("clipboard_timeout")), 1500); }),
      ]);
    }
    copied = true;
    elements.status.textContent = "验证码已复制，可以返回使用。";
  } catch {
    elements.status.textContent = "请点击下方按钮复制验证码；也可选中验证码手动复制。";
  } finally {
    window.clearTimeout(timer);
  }
}

async function readAndCopy() {
  if (loading) return;
  if (!/^[1-9][0-9]{0,14}$/.test(messageId)) {
    elements.status.textContent = "无效的消息链接，请返回收件箱。";
    return;
  }
  loading = true;
  copied = false;
  clearMessage();
  elements.login.hidden = true;
  elements.retry.hidden = true;
  elements["copy-code"].disabled = true;
  try {
    const response = await fetch(new URL(`v1/messages/${messageId}/code`, window.location.href), { cache: "no-store" });
    if (response.status === 401) {
      elements.status.textContent = "请先使用统一认证登录，完成后返回本页。";
      elements.login.hidden = false;
      elements.retry.hidden = false;
      return;
    }
    if (!response.ok) throw new Error("read_failed");
    const payload = await response.json();
    const message = payload.message;
    if (!payload.ok || String(message?.id) !== messageId) throw new Error("wrong_message");
    const code = message.verification_code;
    if (!/^[A-Za-z0-9]{4,32}$/.test(code || "")) {
      elements.status.textContent = "这条消息未识别到唯一验证码。";
      return;
    }
    elements.code.textContent = code;
    elements.receiver.textContent = message.message_type === "email"
      ? `接收邮箱：${message.recipient}` : `接收号码：${message.sim_phone || message.sim_slot || "未提供"}`;
    elements.subject.textContent = message.subject || message.sender || "";
    elements.message.hidden = false;
    elements["copy-code"].disabled = false;
    await copyCurrentCode();
  } catch {
    clearMessage();
    elements.status.textContent = "暂时无法读取，请确认登录权限后重试。";
    elements.retry.hidden = false;
  } finally {
    loading = false;
    elements["copy-code"].disabled = false;
  }
}

elements["copy-code"].addEventListener("click", copyCurrentCode);
elements.retry.addEventListener("click", readAndCopy);
window.addEventListener("focus", () => { if (!copied) readAndCopy(); });
readAndCopy();
