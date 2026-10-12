/* 自分の交渉(DESIGN.md §4.3〜§4.8・§6。仕組みは design.md §3.3・§3.4・§4.4・§6.1・§6.3・§7)。
 *
 * - 本物の候補者だけが使う(面談を送信して、利用記録ができたあと)。依頼者 ID は GET /v1/session で知る(クッキーは JS から読めない)。
 * - 1 つのページの中で、画面を切り替える(URL の hash。1 画面 1 目的): ホーム(#home)・求人(#jobs)・交渉状況(#n/<nid>)・
 *   開示設定(#view-disclosure。#slot-ledger・#slot-fr39 も)・データ削除(#view-delete)。ホームは #view-home、求人は #view-jobs。ナビの「段階開示・開示台帳」(#slot-stages)は、交渉状況の段階開示へ。
 * - 一覧は金庫が返す項目だけ(交渉 ID・求人 ID・作成時刻・状態・最終結果)。求人の名前は GET /v1/jobs の公開情報から引く。
 * - 活動ログは SSE で追記し(つなげないときは 2 秒ごとの再取得)、途中確認の質問が出ていれば、答える枠を画面の一番上に出す(§4.4)。
 * - 管理(一時停止・再開・取消)は金庫の control に送る。取消の結果は「なし」になる。
 * - 段階開示(stages.js)は交渉状況の中、開示台帳(ledger.js)・並べて見る画面(panels.js)・開示設定の説明(disclosure.js)は開示設定の中。
 *   開示設定の部品は、その画面を初めて開いたときに動かす(読み込み時の要求を増やさない)。
 *   交渉を選んだとき(negotiation:selected {nid, jobId})・最終結果が届いたとき(negotiation:ended {nid})・データを消したとき
 *   (negotiation:cleared)に、document へイベントを飛ばして、区画に知らせる。本人の API の経路は、ここに書いて、各モジュールへ渡す。
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
  mountVeil,
  packageChips,
  replace,
  resultView,
  showError,
  showMessage,
  stateBox,
  watchNegotiation,
  whereBox,
  withBusy,
} from "./ui.js";

const STATE_LABELS = { active: ["進行中", "ok"], paused: ["一時停止中", "warn"], ended: ["終了", ""] };
const VIEWS = ["home", "jobs", "negotiation", "disclosure", "delete"];

// pid: 依頼者 ID、jobs: 求人の公開情報、negotiations: 本人の交渉の一覧(金庫が返す項目)、question: 答えを待っている組み合わせ、
// lastEntry: 選んだ交渉の活動ログの最後の記録(現在地の文に使う)、pending: 交渉ごとの、答えを待っている組み合わせ(ホームの「あなたの番」)
const app = {
  pid: null,
  jobs: [],
  jobsById: new Map(),
  negotiations: [],
  selected: null,
  watcher: null,
  question: null,
  lastEntry: null,
  pending: new Map(),
  activityOnce: new Map(),
  disclosureMounted: false,
  ledger: null,
};
const el = (id) => document.getElementById(id);
const pv = (route, options = {}) => call(route, { ...options, path: { pid: app.pid, ...(options.path ?? {}) } });
const view = new ActivityView({
  list: el("activity"),
  empty: el("activity-empty"),
  labels: { self: "あなたのエージェント", counterparty: "相手のエージェント" },
});

const selectedNegotiation = () => app.negotiations.find((item) => item.nid === app.selected);
const runningNegotiation = () => app.negotiations.find((item) => item.state !== "ended");

function stateBadgeClass(kind) {
  return `badge${kind ? ` badge-${kind}` : ""}`;
}

function companyText(job) {
  return job.company_name ?? "非公開求人(会うと決めた後に企業名が分かります)";
}

function jobTitle(item) {
  return app.jobsById.get(item.job_id)?.title ?? item.job_id;
}

// ---- 画面の切り替え(hash) ----

function setHash(hash) {
  try {
    if (location.hash !== hash) location.hash = hash;
  } catch {
    /* 画面のコードを偽のブラウザで動かすとき */
  }
}

function go(name) {
  VIEWS.forEach((other) => {
    el(`view-${other}`).hidden = other !== name;
  });
  for (const tab of ["home", "jobs", "disclosure"]) {
    const link = el(`tab-${tab}`);
    if (tab === name) link.setAttribute("aria-current", "true");
    else link.removeAttribute("aria-current");
  }
  window.scrollTo?.(0, 0);
}

function route() {
  const hash = String(location.hash ?? "");
  if (hash.startsWith("#n/")) {
    const nid = hash.slice(3);
    if (nid && nid !== app.selected) select(nid);
    go("negotiation");
  } else if (hash === "#view-jobs" || hash.startsWith("#job/")) {
    go("jobs");
  } else if (hash === "#view-disclosure" || hash === "#slot-ledger" || hash === "#slot-fr39") {
    ensureDisclosure();
    go("disclosure");
    if (hash !== "#view-disclosure") el(hash.slice(1)).scrollIntoView?.();
  } else if (hash === "#slot-stages") {
    if (!app.selected && app.negotiations.length) select(app.negotiations[0].nid);
    go("negotiation");
    el("slot-stages").scrollIntoView?.();
  } else if (hash === "#view-delete") {
    go("delete");
  } else {
    go("home");
  }
}

// ---- 活動ログの 1 回だけの読み取り(交渉ごと。ホームの「あなたの番」と、台帳の途中確認の回答が共有する) ----

function activityOnce(nid) {
  if (!app.activityOnce.has(nid)) {
    app.activityOnce.set(nid, call("GET /v1/negotiations/{nid}/activity", { path: { nid } }));
  }
  return app.activityOnce.get(nid);
}

/** entries(記録の列)から、まだ答えていない途中確認の組み合わせを返す(なければ null)。 */
function pendingQuestion(entries) {
  let pending = null;
  for (const entry of entries) {
    if (entry.action === "ask_principal") pending = entry.package;
    if (entry.action === "principal_answer" || entry.action === "final_result") pending = null;
  }
  return pending;
}

// ---- ホーム(§4.3) ----

function renderHome() {
  const running = runningNegotiation();
  const turn = el("your-turn");
  const state = el("home-state");
  const agreed = app.negotiations.find((item) => item.state === "ended" && item.result && item.result.likelihood !== "none");

  if (running && app.pending.get(running.nid)) {
    replace(
      turn,
      h(
        "div",
        { class: "your-turn" },
        h("strong", {}, "あなたの番です。"),
        h("p", {}, `${jobTitle(running)}: この組み合わせなら受けますか。答えると交渉が続きます。`),
        h("a", { class: "btn btn-primary", href: `/me#n/${running.nid}` }, "確認して答える"),
      ),
    );
  } else if (agreed && !running) {
    replace(
      turn,
      h(
        "div",
        { class: "your-turn" },
        h("strong", {}, "判定が出ました。"),
        h("p", {}, `${jobTitle(agreed)}: 見込み ${LIKELIHOOD_LABELS[agreed.result.likelihood]}。「会う」を押すかどうかを決められます。`),
        h("a", { class: "btn btn-primary", href: `/me#n/${agreed.nid}` }, "交渉を見る"),
      ),
    );
  } else {
    clear(turn);
  }

  if (!app.negotiations.length) {
    replace(state, stateBox("empty", "条件を預けました。", `求人を選ぶと、交渉を始められます。いま選べる求人は ${app.jobs.length} 件です。`));
  } else if (running && !app.pending.get(running.nid)) {
    replace(
      state,
      stateBox(
        running.state === "paused" ? "empty" : "waiting",
        running.state === "paused" ? "一時停止中です。" : "交渉が進んでいます。",
        running.state === "paused" ? "再開すると、続きから進みます。" : "あなたは何もしなくて大丈夫。次に開いたとき、動きがあればここに出ます。",
      ),
    );
  } else {
    clear(state);
  }

  el("home-jobs-text").textContent = running
    ? `いま選べる求人は ${app.jobs.length} 件です。進行中の交渉が終わるか取り消すと、次を始められます。`
    : `いま選べる求人は ${app.jobs.length} 件です。相手に渡るのは「受けられるか」の答えだけです。`;
}

// ---- 求人(§4.4)と交渉の開始 ----

function renderJobs() {
  const running = runningNegotiation();
  el("start-note").hidden = !running;
  el("start-note").textContent = running ? "進行中の交渉があります。終わるか取り消してから、次の交渉を始められます。" : "";
  replace(
    el("jobs"),
    app.jobs.length
      ? app.jobs.map((job) => {
          const button = h("button", { class: "btn btn-primary", type: "button", disabled: Boolean(running) }, "この求人と交渉を始める");
          button.addEventListener("click", () => startNegotiation(job.template_id, button));
          return h(
            "section",
            { class: "card job-card", "aria-label": job.title },
            h("h3", {}, job.title),
            h("p", { class: "job-meta" }, companyText(job), job.job_category ? ` / ${job.job_category}` : ""),
            h("p", {}, job.summary),
            h("div", { class: "row" }, button),
          );
        })
      : stateBox("empty", "いま選べる求人はありません。", "しばらくしてから、もう一度開いてください。"),
  );
}

async function startNegotiation(templateId, button) {
  await withBusy(
    button,
    async () => {
      showMessage(el("start-error"), null);
      const created = await pv("POST /v1/principals/{pid}/negotiations", {
        body: { request_id: newRequestId(), employer_template_id: templateId },
      });
      await loadList();
      select(created.nid);
      go("negotiation");
      setHash(`#n/${created.nid}`);
    },
    (error) => showError(el("start-error"), error, { daily_limit_reached: "本日の交渉の上限に達しました。明日以降にもう一度お試しください。" }),
  );
  renderJobs();
}

// ---- 交渉の一覧(ホームの中) ----

function renderList() {
  el("list-empty").hidden = app.negotiations.length > 0;
  replace(
    el("negotiations"),
    app.negotiations.map((item) => {
      const [label, kind] = STATE_LABELS[item.state] ?? [item.state, ""];
      return h(
        "li",
        { class: "neg-item", "aria-current": item.nid === app.selected ? "true" : null },
        h(
          "button",
          {
            type: "button",
            onclick: () => {
              select(item.nid);
              go("negotiation");
              setHash(`#n/${item.nid}`);
            },
          },
          h("strong", {}, jobTitle(item)),
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
  renderJobs();
  renderDetailHeader();
  renderHome();
  // 進行中の交渉の、答えを待っている途中確認(ホームの「あなたの番」)。活動ログは交渉ごとに 1 回だけ読む
  const running = runningNegotiation();
  if (running && !app.pending.has(running.nid)) {
    app.pending.set(running.nid, null);
    try {
      const log = await activityOnce(running.nid);
      app.pending.set(running.nid, pendingQuestion(log.entries));
      renderHome();
    } catch {
      /* ホームの「あなたの番」が出ないだけ。交渉状況を開けば分かる */
    }
  }
}

// ---- 選んだ交渉(§4.5・§6) ----

/** 現在地の 3 行(いま・次に・あなたが決めること)。状態・途中確認・最後の記録から決める。期限は時刻で書く(ここでは期限を持たないので書かない)。 */
function renderWhere(item) {
  let where;
  if (item.state === "ended" && item.result && item.result.likelihood !== "none") {
    where = {
      now: `判定が出ました。見込みは「${LIKELIHOOD_LABELS[item.result.likelihood]}」です。`,
      next: "双方が「会う」を押すと、職務要約が相手に開きます。",
      yours: "「会う」を押すかどうか(下の段階開示)。押さなくても構いません。",
    };
  } else if (item.state === "ended") {
    where = {
      now: "この交渉は終わりました。合意できる組み合わせは見つかりませんでした。",
      next: "あなたの条件は残っています。別の求人で始められます。",
      yours: "新しく始めるかどうか。",
    };
  } else if (item.state === "paused") {
    where = { now: "一時停止中です。期限は進みません。", next: "再開すると、続きから進みます。", yours: "再開するか、取り消すか。" };
  } else if (app.question) {
    where = {
      now: "あなたの確認を待っています。",
      next: "答えると、交渉が続きます。",
      yours: "上の組み合わせを、受けるか受けないか。",
    };
  } else {
    const last = app.lastEntry;
    const now =
      last?.actor === "counterparty"
        ? "相手が提案しました。あなたのエージェントが、金庫に「受けられるか」を聞いて確かめています。"
        : last
          ? "あなたのエージェントが提案しました。相手の返事を待っています。"
          : "あなたのエージェントが、最初の提案を考えています。";
    where = { now, next: "相手の返事か、あなたへの確認か、判定のどれかです。動きがあると、下の活動ログに出ます。", yours: "今は何もしなくて大丈夫。" };
  }
  replace(el("where"), whereBox(where));
}

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
  renderWhere(item);
}

function answerButton(choice, label) {
  const button = h("button", { class: "btn", type: "button" }, label);
  button.addEventListener("click", () =>
    withBusy(
      button,
      async () => {
        await call("POST /v1/negotiations/{nid}/principal-answer", {
          path: { nid: app.selected },
          body: { package: app.question, answer: choice },
        });
        app.question = null;
        app.pending.set(app.selected, null);
        renderQuestion();
        renderHome();
        const item = selectedNegotiation();
        if (item) renderWhere(item);
      },
      (error) => showError(el("detail-error"), error),
    ),
  );
  return button;
}

/** 途中確認(§4.4): 質問(組み合わせ)に、受ける・受けないの 2 つで答える。2 つは同じ大きさ・同じ重み(どちらかを推さない)。 */
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
      { class: "your-turn", role: "alert" },
      h("strong", {}, "この組み合わせなら受けますか?"),
      h("div", {}, packageChips(app.question)),
      h(
        "div",
        { class: "small" },
        "年収は 50 万円の幅のマス目の値です。「受ける」と答えると、それより良い組み合わせも受けられるものとして扱われます。「受けない」と答えると、それより悪い組み合わせは受けないものとして扱われます。",
      ),
      h("div", { class: "answers-2" }, answerButton("accept", "受ける"), answerButton("reject", "受けない")),
    ),
  );
}

function onEntries(entries) {
  view.add(entries);
  for (const entry of entries) {
    if (entry.action === "ask_principal") app.question = entry.package;
    if (entry.action === "principal_answer" || entry.action === "final_result") app.question = null;
    app.lastEntry = entry;
  }
  if (app.selected) app.pending.set(app.selected, app.question);
  renderQuestion();
  const item = selectedNegotiation();
  if (item) renderWhere(item);
  renderHome();
  if (entries.some((entry) => ["pause", "resume", "final_result"].includes(entry.action))) loadList();
  if (entries.some((entry) => entry.action === "final_result")) {
    document.dispatchEvent(new CustomEvent("negotiation:ended", { detail: { nid: app.selected } }));
  }
}

function select(nid) {
  if (app.watcher) app.watcher.close();
  app.selected = nid;
  app.question = null;
  app.lastEntry = null;
  view.reset();
  showMessage(el("detail-error"), null);
  el("detail-empty").hidden = true;
  el("detail-body").classList.remove("hidden");
  el("detail-body").hidden = false;
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
  if (action === "cancel" && !window.confirm("この交渉を取り消しますか?結果は「なし」になります。あなたの条件は残ります。")) return;
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

// ---- データを消す(§4.8。仕組みは §6.3 の削除の流れ) ----

async function deleteData(button) {
  if (!window.confirm("預けたデータを、すべて消します。戻せません。よろしいですか?")) return;
  await withBusy(
    button,
    async () => {
      const result = await pv("POST /v1/principals/{pid}/delete");
      if (app.watcher) app.watcher.close();
      app.pid = null;
      app.selected = null;
      el("content").classList.add("hidden");
      el("content").hidden = true;
      el("gate").classList.remove("hidden");
      el("gate").hidden = false;
      replace(
        el("gate"),
        stateBox(
          "ok",
          result.status === "deleted" ? "消しました。" : "削除を受け付けました。",
          result.status === "deleted" ? "預けた条件と記録は、もうありません。" : "途中で止まった分は、自動で最後まで消します。押し直す必要はありません。",
        ),
        h("a", { class: "btn", href: "/" }, "入口へ"),
      );
      document.dispatchEvent(new CustomEvent("negotiation:cleared"));
    },
    (error) => showError(el("delete-message"), error),
  );
}

// ---- 段階開示(交渉状況の中)・開示設定(台帳・並べて見る画面・説明) ----

function mountStagesSection() {
  mountStages(
    { body: el("stages-body"), error: el("stages-error") },
    {
      mode: "own",
      loadStage: (nid) => call("GET /v1/negotiations/{nid}/stage", { path: { nid } }),
      meet: (nid, jobSummary) => call("POST /v1/negotiations/{nid}/stage/meet", { path: { nid }, body: { job_summary: jobSummary } }),
      approve: (nid) => call("POST /v1/negotiations/{nid}/stage/approve", { path: { nid } }),
      onChange: () => app.ledger?.refresh(), // 段の状態を読む・変えるたびに、台帳が変わり得る
      emptyText: "交渉を選ぶと、ここに段の状態が出ます。",
    },
  );
}

/** 開示設定の画面を初めて開いたときに、台帳・並べて見る画面・説明を動かす(読み込み時の要求を増やさない)。 */
function ensureDisclosure() {
  if (app.disclosureMounted || !app.pid) return;
  app.disclosureMounted = true;
  app.ledger = mountLedger(
    { body: el("ledger-body"), error: el("ledger-error") },
    {
      loadLedger: () => pv("GET /v1/principals/{pid}/ledger"),
      // 途中確認の回答は、活動ログ(金庫のイベント列の自分の側の見え方)にある。交渉ごとに 1 回だけ読む
      loadAnswers: async (nid) => (await activityOnce(nid)).entries.filter((entry) => entry.action === "principal_answer"),
      negotiations: () =>
        app.negotiations.map((item) => ({
          nid: item.nid,
          title: jobTitle(item),
          createdAt: item.created_at,
          ended: item.state === "ended",
        })),
    },
  );
  mountPanels({ body: el("fr39-body"), error: el("fr39-error") }, { load: () => pv("GET /v1/principals/{pid}/panels", { path: { pid: "me" } }) });
  if (app.selected) document.dispatchEvent(new CustomEvent("negotiation:selected", { detail: { nid: app.selected, jobId: selectedNegotiation()?.job_id ?? null } }));
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
      stateBox(
        "empty",
        session.principal_id ? "面談がまだ途中です。" : "まだ面談を受けていません。",
        "面談で条件を預けると、ここで交渉を始められます。",
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
  el("gate").hidden = true;
  el("content").classList.remove("hidden");
  el("content").hidden = false;
  mountStagesSection();
  renderJobs();
  await loadList();
  route();
}

el("pause-button").addEventListener("click", (event) => control("pause", event.currentTarget));
el("resume-button").addEventListener("click", (event) => control("resume", event.currentTarget));
el("cancel-button").addEventListener("click", (event) => control("cancel", event.currentTarget));
el("delete-ack").addEventListener("change", (event) => {
  el("delete-button").disabled = !event.currentTarget.checked;
});
el("delete-button").addEventListener("click", (event) => deleteData(event.currentTarget));
window.addEventListener("hashchange", route);

init();

mountVeil();
