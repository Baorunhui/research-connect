const token = document.body.dataset.token;
const labels = { queued: "排队中", running: "执行中", completed: "已完成", failed: "失败", cancelled: "已取消" };
let snapshot = null;

function render(data) {
  snapshot = data;
  const job = data.job;
  document.title = `${job.title} · Research Connect`;
  document.getElementById("title").textContent = job.title;
  document.getElementById("meta").textContent = `${job.module_name} · ${job.job_id}`;
  const status = document.getElementById("status");
  status.textContent = labels[job.status] || job.status;
  status.className = `status ${job.status}`;
  const events = data.events || [];
  document.getElementById("events").replaceChildren(...events.map(eventNode));
  if (events.length) updateProgress(events[events.length - 1]);
  if (job.report_ready || data.report_url) showReport(data.report_url || `/reports/${token}/index.html`);
}

function eventNode(event) {
  const li = document.createElement("li");
  const time = document.createElement("time");
  time.textContent = new Date(event.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const text = document.createElement("span");
  text.textContent = `${event.stage ? event.stage + " · " : ""}${event.message}`;
  li.append(time, text);
  return li;
}

function updateProgress(event) {
  document.getElementById("current-message").textContent = event.message;
  let percent = event.total_value ? Math.max(2, Math.min(100, event.current_value / event.total_value * 100)) : null;
  if (event.event_type === "job.completed") percent = 100;
  if (percent !== null) document.getElementById("progress-bar").style.width = `${percent}%`;
}

function showReport(url) {
  const section = document.getElementById("report-section");
  section.classList.remove("hidden");
  document.getElementById("open-report").href = url;
  const frame = document.getElementById("report");
  if (!frame.src) frame.src = url;
}

async function loadSnapshot() {
  const response = await fetch(`/api/v1/public/jobs/${token}`);
  if (!response.ok) throw new Error("任务不存在");
  render(await response.json());
}

function connect() {
  const indicator = document.getElementById("connection");
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${scheme}://${location.host}/ws/public/jobs/${token}`);
  socket.onopen = () => { indicator.textContent = "实时更新中"; indicator.classList.add("online"); };
  socket.onmessage = ({ data }) => {
    const message = JSON.parse(data);
    if (message.type === "snapshot") { render(message.snapshot); return; }
    if (message.type === "report_ready") { loadSnapshot(); return; }
    if (message.type === "event" && snapshot) {
      snapshot.events.push(message.event);
      snapshot.job.status = ({ "job.completed": "completed", "job.failed": "failed", "job.cancelled": "cancelled" })[message.event.event_type] || "running";
      render(snapshot);
    }
  };
  socket.onclose = () => { indicator.textContent = "已断开，正在重连"; indicator.classList.remove("online"); setTimeout(connect, 2000); };
}

loadSnapshot().then(connect).catch(error => { document.getElementById("current-message").textContent = error.message; });

