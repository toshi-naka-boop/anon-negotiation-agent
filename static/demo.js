/* デモ(design.md §8.4・§7)。架空人物どうしの交渉を、ライブで実行するか、記録(リプレイ)を流して、候補者側・求人側の 2 つのパネルに並べる。
 *
 * - ライブ: POST /v1/demo/negotiations で作り、2 つの側の活動ログを SSE で追記する(つなげないときは、両側を 1 回で返す /panels の再取得)。
 * - リプレイ: fixtures/replays/case{N}.jsonl を取り、記録した時刻の間隔のとおり(倍率で縮める)に流す。画面の上に「リプレイ」と明示する。
 *   記録は、金庫のイベント(その側の見え方)なので、活動ログの形(web.activity_api の _SHAPE)に直して出す(SHAPE は、その写し)。
 * - 架空人物の途中確認は、フィクスチャの条件で自動で答える。画面に「架空人物の自動回答」と示す(§4.4)。
 * - 「最悪漏れてもここまで」「まだ隠しているもの」(FR-39)・段階開示は、次のパッケージ(L2)の区画(#slot-fr39・#slot-stages)。
 */

import { call, newRequestId } from "./api.js";
import { ActivityView, clear, formatDateTime, h, replace, resultView, showError, showMessage, watchNegotiation, withBusy } from "./ui.js";

const SPEEDS = [0.1, 0.25, 0.5, 1, 2, 4, 10];
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
  el("run").scrollIntoView({ block: "start" });
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
    el("run-status").textContent = "交渉は進んでいます。記録が届くたびに追記します。";
    const path = { nid: created.nid };
    running.watchers.push(
      watchNegotiation({
        sides: ["candidate", "employer"],
        streamRoute: "GET /v1/stream/demo/negotiations/{nid}/activity",
        path,
        // つなげないときの再取得は、両側のパネルを 1 回で返す口(並べて見る画面。§7)
        poll: ({ candidate, employer }) =>
          call("GET /v1/demo/negotiations/{nid}/panels", { path, query: { candidate_after_seq: candidate, employer_after_seq: employer } }),
        onEntries: addEntries,
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
    bannerText: "リプレイ: 実行ではありません。記録した交渉を、記録どおりの間隔で流します。",
  });
  let replay;
  try {
    replay = parseReplay(await call("GET /v1/demo/replays/{case}", { path: { case: info.case } }));
  } catch (error) {
    el("stop-button").hidden = true;
    if (error.name === "ApiError") showError(el("run-error"), error);
    else showMessage(el("run-error"), "error", "リプレイの記録を読み取れません。");
    return;
  }
  const { header, events } = replay;
  const speed = Number(el("speed").value);
  const span = events[events.length - 1].observed_at - events[0].observed_at;
  el("banner").textContent = `リプレイ: 実行ではありません。${SOURCE_LABELS[header.source] ?? header.source}(${formatDateTime(header.recorded_at)})を、記録した間隔のまま(×${speed})流しています。`;
  el("run-status").textContent = `記録の長さ: ${span.toFixed(1)} 秒。この倍率での再生時間: ${(span / speed).toFixed(1)} 秒。`;
  let index = 0;
  const step = () => {
    const current = events[index];
    addEntries(current.side, [toEntry(current.event)]);
    index += 1;
    if (index >= events.length) {
      el("stop-button").hidden = true;
      return;
    }
    running.timer = setTimeout(step, (Math.max(0, events[index].observed_at - current.observed_at) * 1000) / speed);
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
    SPEEDS.map((value) => h("option", { value: String(value), selected: value === 1 }, `×${value}${value === 1 ? "(記録どおり)" : ""}`)),
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
