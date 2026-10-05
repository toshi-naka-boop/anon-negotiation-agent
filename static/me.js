/* 自分の交渉(design.md §3.3・§3.4・§4.4・§6.1・§6.3・§7)。
 *
 * - 本物の候補者だけが使う(面談を送信して、利用記録ができたあと)。依頼者 ID は GET /v1/session で知る(クッキーは JS から読めない)。
 * - 一覧は金庫が返す項目だけ(交渉 ID・求人 ID・作成時刻・状態・最終結果)。求人の名前は GET /v1/jobs の公開情報から引く。
 * - 活動ログは SSE で追記し(つなげないときは 2 秒ごとの再取得)、途中確認の質問が出ていれば、答えるカードを出す(§4.4)。
 * - 管理(一時停止・再開・取消)は金庫の control に送る。取消の結果は「なし」になる。
 * - 段階開示(stages.js)・開示台帳(ledger.js)・並べて見る画面(panels.js)の区画(#slot-stages・#slot-ledger・#slot-fr39)は、ここで動かす。
 *   交渉を選んだとき(negotiation:selected {nid, jobId})・最終結果が届いたとき(negotiation:ended {nid})・データを消したとき
 *   (negotiation:cleared)に、document へイベントを飛ばして、区画に知らせる。本人の API の経路は、ここに書いて、各モジュールへ渡す
 *   (モジュールは、経路を知らない。デモの画面が、本物の依頼者の API を呼ばないため)。
 */

import { call, newRequestId } from "./api.js";
import { mountLedger } from "./ledger.js";
import { mountPanels } from "./panels.js";
import { mountStages } from "./stages.js";
import {
  ActivityView,
  LIKELIHOOD_LABELS,
  clear,
  formatDateTime,
  h,
  message,
  packageChips,
  replace,
  resultView,
  showError,
  showMessage,
  watchNegotiation,
  withBusy,
} from "./ui.js";

const STATE_LABELS = { active: ["進行中", "ok"], paused: ["一時停止中", "warn"], ended: ["終了", ""] };

// pid: 依頼者 ID、jobs: 求人の公開情報、negotiations: 本人の交渉の一覧(金庫が返す項目)、question: 答えを待っている組み合わせ
const app = { pid: null, jobs: [], jobsById: new Map(), negotiations: [], selected: null, watcher: null, question: null, jobChoice: null };
const el = (id) => document.getElementById(id);
const pv = (route, options = {}) => call(route, { ...options, path: { pid: app.pid, ...(options.path ?? {}) } });
const view = new ActivityView({
  list: el("activity"),
  empty: el("activity-empty"),
  labels: { self: "あなたの代理人", counterparty: "相手(求人側)" },
});

const selectedNegotiation = () => app.negotiations.find((item) => item.nid === app.selected);

function stateBadgeClass(kind) {
  return `badge${kind ? ` badge-${kind}` : ""}`;
}

function companyText(job) {
  return job.company_name ?? "非公開求人(会うと決めた後に企業名を開示)";
}

// ---- 求人と交渉の開始 ----

function updateStartControls() {
  const running = app.negotiations.some((item) => item.state !== "ended");
  el("start-button").disabled = !app.jobChoice || running;
  el("start-note").hidden = !running;
  el("start-note").textContent = running ? "進行中の交渉があります。終わるか取り消してから、次の交渉を始められます。" : "";
}

function renderJobs() {
  replace(
    el("jobs"),
    app.jobs.length
      ? app.jobs.map((job) =>
          h(
            "label",
            { class: "job-option" },
            h("input", {
              type: "radio",
              name: "job",
              value: job.template_id,
              checked: job.template_id === app.jobChoice,
              onchange: () => {
                app.jobChoice = job.template_id;
                updateStartControls();
              },
            }),
            h("span", {}, h("strong", {}, job.title), h("br"), h("span", { class: "small" }, companyText(job)), h("br"), h("span", { class: "small muted" }, job.summary)),
          ),
        )
      : h("p", { class: "muted" }, "いま選べる求人はありません。"),
  );
  updateStartControls();
}

async function startNegotiation() {
  await withBusy(
    el("start-button"),
    async () => {
      showMessage(el("start-error"), null);
      const created = await pv("POST /v1/principals/{pid}/negotiations", {
        body: { request_id: newRequestId(), employer_template_id: app.jobChoice },
      });
      await loadList();
      select(created.nid);
    },
    (error) => showError(el("start-error"), error, { daily_limit_reached: "本日の交渉の上限に達しました。明日以降にもう一度お試しください。" }),
  );
  updateStartControls();
}

// ---- 交渉の一覧 ----

function renderList() {
  el("list-empty").hidden = app.negotiations.length > 0;
  replace(
    el("negotiations"),
    app.negotiations.map((item) => {
      const [label, kind] = STATE_LABELS[item.state] ?? [item.state, ""];
      const job = app.jobsById.get(item.job_id);
      return h(
        "li",
        { class: "neg-item", "aria-current": item.nid === app.selected ? "true" : null },
        h(
          "button",
          { type: "button", onclick: () => select(item.nid) },
          h("strong", {}, job ? job.title : item.job_id),
          " ",
          h("span", { class: stateBadgeClass(kind) }, label),
          h("div", { class: "small muted" }, formatDateTime(item.created_at)),
          item.result ? h("div", { class: "small" }, `結果: 見込み ${LIKELIHOOD_LABELS[item.result.likelihood] ?? item.result.likelihood}`) : null,
        ),
      );
    }),
  );
}

async function loadList() {
  try {
    const list = await pv("GET /v1/principals/{pid}/negotiations");
    app.negotiations = [...list].sort((a, b) => b.created_at.localeCompare(a.created_at));
    showMessage(el("list-error"), null);
  } catch (error) {
    showError(el("list-error"), error);
    return;
  }
  renderList();
  updateStartControls();
  renderDetailHeader();
}

// ---- 選んだ交渉 ----

function renderDetailHeader() {
  const item = selectedNegotiation();
  if (!item) return;
  const job = app.jobsById.get(item.job_id);
  const [label, kind] = STATE_LABELS[item.state] ?? [item.state, ""];
  el("detail-title").textContent = job ? job.title : item.job_id;
  el("detail-state").className = stateBadgeClass(kind);
  el("detail-state").textContent = label;
  el("detail-summary").textContent = `${job ? `${companyText(job)} / ` : ""}開始: ${formatDateTime(item.created_at)}`;
  replace(el("detail-result"), item.result ? resultView(item.result) : null);
  el("controls").hidden = item.state === "ended";
  el("pause-button").hidden = item.state !== "active";
  el("resume-button").hidden = item.state !== "paused";
  el("cancel-button").hidden = item.state === "ended";
  renderQuestion();
}

function answerButton(choice, label) {
  const button = h("button", { class: choice === "accept" ? "btn btn-primary" : "btn", type: "button" }, label);
  button.addEventListener("click", () =>
    withBusy(
      button,
      async () => {
        await call("POST /v1/negotiations/{nid}/principal-answer", {
          path: { nid: app.selected },
          body: { package: app.question, answer: choice },
        });
        app.question = null;
        renderQuestion();
      },
      (error) => showError(el("detail-error"), error),
    ),
  );
  return button;
}

/** 途中確認(§4.4): 質問(組み合わせ)に、受ける・受けないの 2 つで答える。 */
function renderQuestion() {
  const item = selectedNegotiation();
  if (!app.question || !item || item.state === "ended") {
    clear(el("question"));
    return;
  }
  replace(
    el("question"),
    h(
      "div",
      { class: "msg msg-warn", role: "alert" },
      h("strong", {}, "この組み合わせなら受けますか?"),
      h("div", {}, packageChips(app.question)),
      h(
        "div",
        { class: "small" },
        "年収は 50 万円単位のグリッドの値です。「受ける」と答えると、それより良い組み合わせも受けられるものとして扱われます。「受けない」と答えると、それより悪い組み合わせは受けないものとして扱われます。",
      ),
      h("div", { class: "row" }, answerButton("accept", "受ける"), answerButton("reject", "受けない")),
    ),
  );
}

function onEntries(entries) {
  view.add(entries);
  for (const entry of entries) {
    if (entry.action === "ask_principal") app.question = entry.package;
    if (entry.action === "principal_answer" || entry.action === "final_result") app.question = null;
  }
  renderQuestion();
  if (entries.some((entry) => ["pause", "resume", "final_result"].includes(entry.action))) loadList();
  if (entries.some((entry) => entry.action === "final_result")) {
    document.dispatchEvent(new CustomEvent("negotiation:ended", { detail: { nid: app.selected } }));
  }
}

function select(nid) {
  if (app.watcher) app.watcher.close();
  app.selected = nid;
  app.question = null;
  view.reset();
  showMessage(el("detail-error"), null);
  el("detail-empty").hidden = true;
  el("detail-body").classList.remove("hidden");
  renderList();
  renderDetailHeader();
  app.watcher = watchNegotiation({
    sides: ["candidate"],
    streamRoute: "GET /v1/stream/negotiations/{nid}/activity",
    path: { nid },
    poll: async ({ candidate }) => ({
      candidate: await call("GET /v1/negotiations/{nid}/activity", { path: { nid }, query: { after_seq: candidate } }),
    }),
    onEntries: (_side, entries) => onEntries(entries),
    onEnd: loadList,
    onError: (error) => showError(el("detail-error"), error),
  });
  document.dispatchEvent(new CustomEvent("negotiation:selected", { detail: { nid, jobId: selectedNegotiation()?.job_id ?? null } }));
}

async function control(action, button) {
  const nid = app.selected;
  if (action === "cancel" && !window.confirm("この交渉を取り消しますか?結果は「なし」になります。")) return;
  await withBusy(
    button,
    async () => {
      await call("POST /v1/negotiations/{nid}/control", { path: { nid }, body: { action } });
      showMessage(el("detail-error"), null);
      await loadList();
    },
    (error) => showError(el("detail-error"), error),
  );
}

// ---- データを消す(§6.3 の削除の流れ) ----

async function deleteData(button) {
  if (!window.confirm("保存したデータを、すべて消します。元には戻せません。よろしいですか?")) return;
  await withBusy(
    button,
    async () => {
      const result = await pv("POST /v1/principals/{pid}/delete");
      if (app.watcher) app.watcher.close();
      app.pid = null;
      app.selected = null;
      el("content").classList.add("hidden");
      el("gate").classList.remove("hidden");
      replace(
        el("gate"),
        message("ok", result.status === "deleted" ? "データを消しました。" : "削除を受け付けました。途中で止まった分は、自動で最後までやり直します。"),
        h("a", { class: "btn", href: "/" }, "入口へ"),
      );
      document.dispatchEvent(new CustomEvent("negotiation:cleared"));
    },
    (error) => showError(el("delete-message"), error),
  );
}

// ---- 段階開示・開示台帳・並べて見る画面(区画は、それぞれのモジュール) ----

function mountSections() {
  const ledger = mountLedger(
    { body: el("ledger-body"), error: el("ledger-error") },
    {
      loadLedger: () => pv("GET /v1/principals/{pid}/ledger"),
      // 途中確認の回答は、活動ログ(金庫のイベント列の自分の側の見え方)にある
      loadAnswers: async (nid) =>
        (await call("GET /v1/negotiations/{nid}/activity", { path: { nid } })).entries.filter((entry) => entry.action === "principal_answer"),
      negotiations: () =>
        app.negotiations.map((item) => ({
          nid: item.nid,
          title: app.jobsById.get(item.job_id)?.title ?? item.job_id,
          createdAt: item.created_at,
          ended: item.state === "ended",
        })),
    },
  );
  mountStages(
    { body: el("stages-body"), error: el("stages-error") },
    {
      mode: "own",
      loadStage: (nid) => call("GET /v1/negotiations/{nid}/stage", { path: { nid } }),
      meet: (nid, jobSummary) => call("POST /v1/negotiations/{nid}/stage/meet", { path: { nid }, body: { job_summary: jobSummary } }),
      approve: (nid) => call("POST /v1/negotiations/{nid}/stage/approve", { path: { nid } }),
      onChange: ledger.refresh, // 段の状態を読む・変えるたびに、台帳が変わり得る
      emptyText: "左の一覧から、交渉を選んでください。",
    },
  );
  mountPanels({ body: el("fr39-body"), error: el("fr39-error") }, { load: () => pv("GET /v1/principals/{pid}/panels", { path: { pid: "me" } }) });
}

// ---- 起動と配線 ----

async function init() {
  let session;
  try {
    session = await call("GET /v1/session");
  } catch (error) {
    showError(el("gate"), error);
    return;
  }
  if (!session.principal_id || !session.registered) {
    replace(
      el("gate"),
      message(
        "info",
        session.principal_id
          ? "面談がまだ送信されていません。面談を送信すると、交渉を始められます。"
          : "まだ面談を受けていません。面談を受けると、交渉を始められます。",
      ),
      h("a", { class: "btn btn-primary", href: "/interview" }, "面談へ"),
    );
    return;
  }
  app.pid = session.principal_id;
  try {
    const { jobs } = await call("GET /v1/jobs");
    app.jobs = jobs;
    app.jobsById = new Map(jobs.map((job) => [job.job_id, job]));
  } catch (error) {
    showError(el("start-error"), error);
  }
  el("gate").classList.add("hidden");
  el("content").classList.remove("hidden");
  renderJobs();
  await loadList();
  mountSections();
}

el("start-button").addEventListener("click", startNegotiation);
el("pause-button").addEventListener("click", (event) => control("pause", event.currentTarget));
el("resume-button").addEventListener("click", (event) => control("resume", event.currentTarget));
el("cancel-button").addEventListener("click", (event) => control("cancel", event.currentTarget));
el("delete-ack").addEventListener("change", (event) => {
  el("delete-button").disabled = !event.currentTarget.checked;
});
el("delete-button").addEventListener("click", (event) => deleteData(event.currentTarget));

init();
