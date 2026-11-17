// 站号合并页：合并 / 撤销只做一次提交，request_id 由页面生成，重试不会重复落决定。
const state = { busy: false, revision: null };

function notice(message, isError = false) {
  const box = document.getElementById("notice");
  if (!box) return;
  box.textContent = message;
  box.classList.toggle("is-error", Boolean(isError));
  box.hidden = false;
}

function requestId(kind) {
  return "web-" + kind + "-" + Date.now() + "-" + Math.random().toString(16).slice(2, 8);
}

async function postJSON(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error((payload.error || response.status) + " " + (payload.message || ""));
  }
  return payload;
}

async function merge(button) {
  const source = window.prompt("要并入的别名站号（source）");
  if (!source) return;
  const target = window.prompt("保留的根站号（target）");
  if (!target) return;
  state.busy = true;
  button.disabled = true;
  try {
    const payload = await postJSON("/api/stations/merges", {
      source: source.trim(),
      target: target.trim(),
      request_id: requestId("merge"),
    });
    notice("合并 " + payload.decision.source + " → " + payload.decision.target +
      " · " + payload.decision.status_label);
    window.setTimeout(() => window.location.reload(), 400);
  } catch (error) {
    notice(String(error.message || error), true);
  } finally {
    state.busy = false;
    button.disabled = false;
  }
}

async function undo(button) {
  const decisionId = button.dataset.decision;
  if (!decisionId || !window.confirm("撤销该合并？撤销后两条站号分别重算。")) return;
  state.busy = true;
  button.disabled = true;
  try {
    const payload = await postJSON(
      "/api/stations/merges/" + encodeURIComponent(decisionId) + "/undo",
      { request_id: requestId("undo") });
    notice("已撤销 · 修订 " + payload.revision);
    window.setTimeout(() => window.location.reload(), 400);
  } catch (error) {
    notice(String(error.message || error), true);
    button.disabled = false;
  } finally {
    state.busy = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const mergeButton = document.getElementById("merge-button");
  if (mergeButton && mergeButton.tagName === "BUTTON") {
    mergeButton.addEventListener("click", () => merge(mergeButton));
  }
  document.querySelectorAll(".undo-button").forEach((button) => {
    button.addEventListener("click", () => undo(button));
  });
});
