/* デモ(design.md §8.4・§7)。架空人物どうしの交渉を、ライブで実行するか、記録(リプレイ)を流して、候補者側・求人側の 2 つのパネルに並べる。
 *
 * - ライブ: POST /v1/demo/negotiations で作り、2 つの側の活動ログを SSE で追記する(つなげないときは、両側を 1 回で返す /panels の再取得)。
 * - リプレイ: fixtures/replays/case{N}.jsonl を取り、1 手ごとに一定の間隔(ゆっくり 3 秒・ふつう 2 秒・はやい 1 秒)で流す。「記録どおり」だけは、
 *   記録した時刻の間隔のまま流す。画面の上に「リプレイ」と明示する。
 *   記録は、金庫のイベント(その側の見え方)なので、活動ログの形(web.activity_api の _SHAPE)に直して出す(SHAPE は、その写し)。
 * - 視点の切り替え: 求人側の画面(既定)・求職者の画面・並べて比べる。URL の hash(#employer・#candidate・#both)で選べて、押すと hash を更新する。
 *   2 つの側の購読は、どの視点でも両方つなぐ(出すパネルを切り替えるだけで、API の呼び出しは変えない)。
 * - 架空人物の途中確認は、フィクスチャの条件で自動で答える。画面に「架空人物の自動回答」と示す(§4.4)。
 * - 段階開示(stages.js)の区画(#slot-stages)は、ここで動かす。ライブの交渉を作ったとき(negotiation:selected)・両側の最終結果が届いたとき
 *   (negotiation:ended)・実行を切り替えたとき(negotiation:cleared)に、document へイベントを飛ばして、区画に知らせる。デモの経路(架空人物。
 *   求人側も候補者側も自動応答)だけを呼ぶ。FR-39 の 2 パネル(#slot-fr39)は、デモの架空人物の生の値を見せる口がまだないので、説明だけ(HTML)。
 */

import { call, newRequestId } from "./api.js";
import { mountStages } from "./stages.js";
import { ActivityView, clear, formatDateTime, h, mountVeil, replace, resultView, showError, showMessage, watchNegotiation, withBusy } from "./ui.js";

// リプレイの再生の速さ。interval は 1 手(記録の 1 件)ごとの間隔(秒)で、記録の時刻によらず一定。null(記録どおり)は、記録した時刻の間隔のまま。
const SPEEDS = [
  { id: "slow", label: "ゆっくり(1 手 3 秒)", interval: 3 },
  { id: "normal", label: "ふつう(1 手 2 秒)", interval: 2 },
  { id: "fast", label: "はやい(1 手 1 秒)", interval: 1 },
  { id: "recorded", label: "記録どおり", interval: null },
];
const DEFAULT_SPEED = "normal";

// 視点。panels は出すパネル。note は、その視点で見えるものの説明(1 文)。
const VIEWS = {
  employer: {
    button: "view-employer",
    panels: ["employer"],
    note: "求人担当に届くのは、候補者の提案と、自分の金庫の答えだけで、候補者の条件(本音の値)は見えません。",
  },
  candidate: {
    button: "view-candidate",
    panels: ["candidate"],
    note: "候補者に届くのは、求人側の提案と、自分の金庫の答えだけで、求人側の金庫の中身は届きません。",
  },
  both: {
    button: "view-both",
    panels: ["candidate", "employer"],
    note: "候補者と求人担当に届いた記録を、並べて比べます(同じやり取りでも、見える記録は側ごとに違い、相手の金庫の答えは、どちらにも届きません)。",
  },
};
const DEFAULT_VIEW = "employer";
const SOURCE_LABELS = { scripted: "台本のエージェントの記録", live: "本物の AI(Gemini)の記録" };

// 金庫のイベントの種類 → (主体, 画面向けの手の種類)。web.activity_api の _SHAPE と同じ(tests/test_ui_static.py が一致を確かめる)。
const SHAPE = {
  check: ["self", "check"],
  propose: ["self", "propose"],
  offer_received: ["counterparty", "propose"],
  reject: ["self", "reject"],
  offer_rejected: ["counterparty", "reject"],
  ask_principal: ["self", "ask_principal"],
  principal_answer: ["self", "principal_answer"],
  invalid: ["self", "invalid"],
  pause: ["self", "pause"],
  resume: ["self", "resume"],
  final_result: ["system", "final_result"],
};

const el = (id) => document.getElementById(id);
const views = {
  candidate: new ActivityView({
    list: el("candidate-log"),
    empty: el("candidate-empty"),
    labels: { self: "候補者側", counterparty: "求人側", autoAnswer: true },
  }),
  employer: new ActivityView({
    list: el("employer-log"),
    empty: el("employer-empty"),
    labels: { self: "求人側", counterparty: "候補者側", autoAnswer: true },
  }),
};

// ---- 視点の切り替え ----

const panelElements = { candidate: el("candidate-panel"), employer: el("employer-panel") };

function showView(name) {
  const view = VIEWS[name];
  for (const [key, info] of Object.entries(VIEWS)) el(info.button).setAttribute("aria-pressed", String(key === name));
  for (const [side, panel] of Object.entries(panelElements)) panel.hidden = !view.panels.includes(side);
  el("panels").className = view.panels.length === 1 ? "panels panels-single" : "panels";
  el("view-note").textContent = view.note;
}

/** URL の hash(#employer・#candidate・#both)が指す視点。視点の名前でない hash(ほかの区画への移動など)は、null。 */
function viewFromHash() {
  const name = location.hash.slice(1);
  return Object.keys(VIEWS).includes(name) ? name : null;
}

// いま流しているもの(ライブの購読・リプレイのタイマー)。止めるときに、まとめて止める。
const running = { watchers: [], timer: null };

function stopRunning() {
  running.watchers.forEach((watcher) => watcher.close());
  running.watchers = [];
  clearTimeout(running.timer);
  running.timer = null;
}

function toEntry(event) {
  const [actor, action] = SHAPE[event.kind];
  return {
    seq: event.seq,
    actor,
    action,
    package: event.package ?? null,
    own_evaluation: event.own_evaluation ?? null,
    answer: event.answer ?? null,
    reason: event.reason ?? null,
    attempted_move: event.attempted_move ?? null,
    result: event.result ?? null,
  };
}

/** 1 つの実行(ライブまたはリプレイ)を始める前に、画面を空にして、見出しと帯を出す。 */
function beginRun({ kind, title, bannerText }) {
  stopRunning();
  document.dispatchEvent(new CustomEvent("negotiation:cleared")); // 表示していた交渉の段階開示の状態を、消す
  Object.values(views).forEach((view) => view.reset());
  clear(el("run-result"));
  showMessage(el("run-error"), null);
  el("run").classList.remove("hidden");
  el("run-title").textContent = title;
  const banner = el("banner");
  banner.className = `banner banner-${kind}`;
  banner.textContent = bannerText;
  el("run-status").textContent = "";
  el("stop-button").hidden = false;
  el("view-switch").scrollIntoView({ block: "start" }); // 視点のボタンから見せる(実行を見ながら、視点を切り替えられる)
}

function addEntries(side, entries) {
  views[side].add(entries);
  const final = entries.find((entry) => entry.action === "final_result");
  if (final) {
    replace(el("run-result"), resultView(final.result));
    el("run-status").textContent = "交渉は終わりました。";
    el("stop-button").hidden = true;
  }
}

// ---- ライブ ----

async function runLive(info) {
  beginRun({
    kind: "live",
    title: `ケース ${info.case}: ${info.title}`,
    bannerText: "ライブ実行: 架空人物どうしの交渉を、いま実際に動かしています。",
  });
  el("run-status").textContent = "交渉を作っています…";
  try {
    const created = await call("POST /v1/demo/negotiations", {
      body: {
        request_id: newRequestId(),
        candidate_template_id: info.candidate_template_id,
        employer_template_id: info.employer_template_id,
      },
    });
    el("run-status").textContent = "交渉は進んでいます。あなたは何もしなくて大丈夫。記録が届くたびに、ここに出ます。";
    const path = { nid: created.nid };
    document.dispatchEvent(new CustomEvent("negotiation:selected", { detail: { nid: created.nid, jobId: null } }));
    running.watchers.push(
      watchNegotiation({
        sides: ["candidate", "employer"],
        streamRoute: "GET /v1/stream/demo/negotiations/{nid}/activity",
        path,
        // つなげないときの再取得は、両側のパネルを 1 回で返す口(並べて見る画面。§7)
        poll: ({ candidate, employer }) =>
          call("GET /v1/demo/negotiations/{nid}/panels", { path, query: { candidate_after_seq: candidate, employer_after_seq: employer } }),
        onEntries: addEntries,
        onEnd: () => document.dispatchEvent(new CustomEvent("negotiation:ended", { detail: { nid: created.nid } })),
        onError: (error) => showError(el("run-error"), error),
      }),
    );
  } catch (error) {
    el("run-status").textContent = "";
    el("stop-button").hidden = true;
    showError(el("run-error"), error, { daily_limit_reached: "本日の上限に達しました。リプレイで、同じ交渉を見られます。" });
    if (error.status === 429 && info.replay_available) {
      el("run-error").appendChild(h("button", { class: "btn btn-small", type: "button", onclick: () => playReplay(info) }, "リプレイで見る"));
    }
  }
}

// ---- リプレイ ----

/** JSONL(replay/v1。scripts/replay_check.py)を読む。1 行目がヘッダ、2 行目以降が {side, seq, observed_at, event}。 */
function parseReplay(text) {
  const lines = text.split("\n").filter((line) => line.trim() !== "");
  const header = JSON.parse(lines[0]);
  if (header.header !== true || header.schema !== "replay/v1") throw new Error("not a replay record");
  const events = lines.slice(1).map((line) => JSON.parse(line));
  const valid = events.every(
    (item) => (item.side === "candidate" || item.side === "employer") && typeof item.observed_at === "number" && item.event && item.event.kind in SHAPE,
  );
  if (!events.length || !valid) throw new Error("not a replay record");
  return { header, events };
}

async function playReplay(info) {
  beginRun({
    kind: "replay",
    title: `ケース ${info.case}: ${info.title}`,
    bannerText: "リプレイ: 実行ではありません。記録した交渉を流します。",
  });
  let replay;
  try {
    replay = parseReplay(await call("GET /v1/demo/replays/{case}", { path: { case: info.case } }));
  } catch (error) {
    el("stop-button").hidden = true;
    if (error.name === "ApiError") showError(el("run-error"), error);
    else showMessage(el("run-error"), "error", "リプレイの記録を読み取れませんでした。しばらくしてから、もう一度試せます。");
    return;
  }
  const { header, events } = replay;
  const speed = SPEEDS.find((item) => item.id === el("speed").value) ?? SPEEDS.find((item) => item.id === DEFAULT_SPEED);
  const source = `${SOURCE_LABELS[header.source] ?? header.source}(${formatDateTime(header.recorded_at)})`;
  if (speed.interval === null) {
    const span = events[events.length - 1].observed_at - events[0].observed_at;
    el("banner").textContent = `リプレイ: 実行ではありません。${source}を、記録した間隔のまま流しています。`;
    el("run-status").textContent = `全 ${events.length} 手。記録の長さ: ${span.toFixed(1)} 秒(この長さで終わります)。`;
  } else {
    el("banner").textContent = `リプレイ: 実行ではありません。${source}を、1 手ごとに ${speed.interval} 秒の間隔で流しています。`;
    el("run-status").textContent = `全 ${events.length} 手。再生にかかる時間: 約 ${(events.length - 1) * speed.interval} 秒。`;
  }
  let index = 0;
  const step = () => {
    const current = events[index];
    addEntries(current.side, [toEntry(current.event)]);
    index += 1;
    if (index >= events.length) {
      el("stop-button").hidden = true;
      return;
    }
    const wait = speed.interval === null ? Math.max(0, events[index].observed_at - current.observed_at) : speed.interval;
    running.timer = setTimeout(step, wait * 1000);
  };
  step();
}

// ---- ケースの一覧 ----

function caseCard(info) {
  const live = h("button", { class: "btn btn-primary", type: "button" }, "ライブで実行");
  live.addEventListener("click", () => withBusy(live, () => runLive(info)));
  const replayButton = h("button", { class: "btn", type: "button", disabled: !info.replay_available }, "リプレイを見る");
  replayButton.addEventListener("click", () => playReplay(info));
  return h(
    "section",
    { class: "card stack", "aria-labelledby": `case-${info.case}` },
    h("h3", { id: `case-${info.case}` }, `ケース ${info.case}: ${info.title}`),
    h("p", { class: "small" }, info.description),
    h("p", { class: "small muted" }, `${info.company_name} / ${info.job_title}`),
    h("div", { class: "row" }, live, replayButton, info.attack ? h("a", { class: "btn btn-quiet", href: "/attack" }, "攻撃画面へ") : null),
  );
}

async function init() {
  replace(
    el("speed"),
    SPEEDS.map((item) => h("option", { value: item.id, selected: item.id === DEFAULT_SPEED }, item.label)),
  );
  for (const [name, info] of Object.entries(VIEWS)) {
    el(info.button).addEventListener("click", () => {
      showView(name);
      location.hash = name;
    });
  }
  window.addEventListener("hashchange", () => {
    const name = viewFromHash();
    if (name) showView(name);
  });
  showView(viewFromHash() ?? DEFAULT_VIEW);
  mountStages(
    { body: el("stages-body"), error: el("stages-error") },
    {
      mode: "demo",
      loadStage: (nid) => call("GET /v1/demo/negotiations/{nid}/stage", { path: { nid } }),
      emptyText: "ケースを「ライブで実行」すると、その交渉の段階開示の状態が、ここに出ます。リプレイには、段階開示の状態はありません。",
    },
  );
  el("stop-button").addEventListener("click", () => {
    stopRunning();
    el("run-status").textContent = "止めました。";
    el("stop-button").hidden = true;
  });
  try {
    const { cases } = await call("GET /v1/demo/cases");
    replace(el("cases"), cases.map(caseCard));
  } catch (error) {
    clear(el("cases"));
    showError(el("cases-error"), error);
  }
}

init();

mountVeil();
