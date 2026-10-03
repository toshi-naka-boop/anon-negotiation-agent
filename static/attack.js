/* 攻撃の実演(design.md §8.1〜§8.3 の冒頭・L7-3)。API は src/web/attack/router.py のとおり(/v1/demo/attack/...)。
 *
 * - 攻撃の指示(400 文字まで)を入れて、攻撃の交渉を作る。相手は架空の候補者のコピーに決まっている(選べない)。
 * - 自分が作った交渉の ID は、このページのメモリ(変数 attack)だけに持つ。訪問者を見分ける ID は作らず、ブラウザの保存領域にも書かない。
 *   メーター(次のパッケージ L2)には、一覧が変わるたびに document へ飛ぶ attack:negotiations({nids})で渡す。
 * - 壁 1: 生の A2A メッセージ(JSON)を、そのまま候補者側の受信口へ送り、止まった場所と理由を見る。
 * - 壁 2: 候補者側のエージェントの直近の手番で、LLM に渡った入力の全文と、その機械的な検査の結果。
 * - 壁 3: 攻撃者の提案ごとの、金庫の答え(丸め済みの 3 値だけ)。
 * - 攻撃側・候補者側のパネルは、デモと同じ(候補者側の見え方が金庫の答え。相手は架空人物なので見せてよい。§3.2)。
 */

import { ApiError, call, newRequestId } from "./api.js";
import {
  ActivityView,
  VERDICT_LABELS,
  clear,
  counterFor,
  h,
  packageChips,
  replace,
  resultView,
  showError,
  showMessage,
  verdictBadge,
  watchNegotiation,
  withBusy,
} from "./ui.js";

const el = (id) => document.getElementById(id);
const views = {
  employer: new ActivityView({
    list: el("employer-log"),
    empty: el("employer-empty"),
    labels: { self: "あなたの求人側", counterparty: "候補者側", autoAnswer: true },
  }),
  candidate: new ActivityView({
    list: el("candidate-log"),
    empty: el("candidate-empty"),
    labels: { self: "候補者側", counterparty: "あなたの求人側", autoAnswer: true },
  }),
};

// この画面で作った攻撃の交渉の ID(メモリだけ)。wall3Timer は、壁 3 の更新を間引く札。
const attack = { nids: [], selected: null, watchers: [], wall3Timer: null };

const DAILY_LIMIT_TEXT = "本日の上限に達しました。明日以降にもう一度お試しください。";

function announce() {
  document.dispatchEvent(new CustomEvent("attack:negotiations", { detail: { nids: [...attack.nids] } }));
}

function stopWatching() {
  attack.watchers.forEach((watcher) => watcher.close());
  attack.watchers = [];
  clearTimeout(attack.wall3Timer);
}

// ---- 攻撃の交渉 ----

function onEntries(side, entries) {
  views[side].add(entries);
  if (side === "candidate") scheduleWall3();
  const final = entries.find((entry) => entry.action === "final_result");
  if (final) {
    replace(el("attack-result"), resultView(final.result));
    el("attack-status").textContent = "交渉は終わりました。";
    if (side === "candidate") {
      refreshWall2({ quiet: true });
      refreshWall3();
    }
  }
}

function select(nid) {
  stopWatching();
  attack.selected = nid;
  Object.values(views).forEach((view) => view.reset());
  clear(el("attack-result"));
  clear(el("wall2-result"));
  clear(el("wall3-result"));
  showMessage(el("run-error"), null);
  showMessage(el("replace-message"), null);
  showMessage(el("wall2-error"), null);
  showMessage(el("wall3-error"), null);
  el("run-card").classList.remove("hidden");
  el("attack-select").value = nid;
  el("attack-status").textContent = "交渉は進んでいます。";
  el("wall2-button").disabled = false;
  el("wall3-button").disabled = false;
  attack.watchers.push(
    watchNegotiation({
      sides: ["employer", "candidate"],
      streamRoute: "GET /v1/stream/demo/negotiations/{nid}/activity",
      path: { nid },
      // つなげないときの再取得は、両側のパネルを 1 回で返す口
      poll: ({ candidate, employer }) =>
        call("GET /v1/demo/negotiations/{nid}/panels", { path: { nid }, query: { candidate_after_seq: candidate, employer_after_seq: employer } }),
      onEntries,
      onError: (error) => showError(el("run-error"), error),
    }),
  );
}

function renderSelect() {
  replace(
    el("attack-select"),
    attack.nids.map((nid, index) => h("option", { value: nid, selected: nid === attack.selected }, `攻撃 ${index + 1}`)),
  );
}

async function createAttack() {
  const instruction = el("instruction").value;
  if (!instruction.trim()) {
    showMessage(el("create-error"), "warn", "攻撃の指示を入力してください。");
    return;
  }
  await withBusy(
    el("create-button"),
    async () => {
      showMessage(el("create-error"), null);
      const created = await call("POST /v1/demo/attack/negotiations", { body: { request_id: newRequestId(), instruction } });
      attack.nids.push(created.nid);
      renderSelect();
      announce();
      select(created.nid);
    },
    (error) => showError(el("create-error"), error, { daily_limit_reached: DAILY_LIMIT_TEXT }),
  );
}

async function replaceInstruction() {
  const instruction = el("new-instruction").value;
  if (!instruction.trim()) {
    showMessage(el("replace-message"), "warn", "新しい指示を入力してください。");
    return;
  }
  await withBusy(
    el("replace-button"),
    async () => {
      await call("POST /v1/demo/attack/negotiations/{nid}/instruction", { path: { nid: attack.selected }, body: { instruction } });
      showMessage(el("replace-message"), "ok", "指示を置き換えました。次の手番から効きます。");
    },
    (error) => showError(el("replace-message"), error),
  );
}

// ---- 壁 1: 生のメッセージを送る ----

const OUTCOMES = {
  rejected: ["拒否されました", "ok"],
  accepted: ["有効な入力として受け付けられました", "info"],
  failed: ["エージェントとの通信に失敗しました", "danger"],
};
const STOPPED_AT = {
  web: "web の受信口(agents へ送る前)",
  agents_endpoint: "agents の受信口(/a2a/candidate。LLM が動く前)",
};
const REASONS = {
  body_too_large: "本文が上限を超えています",
  invalid_json: "JSON として読めません",
  not_a_json_object: "JSON のオブジェクトではありません",
  agent_timeout: "エージェントが時間内に応答しませんでした",
  agent_unreachable: "エージェントに届きませんでした",
  unexpected_http_response: "想定外の HTTP の応答でした",
  unexpected_response: "想定外の形の応答でした",
  agent_error: "エージェントがエラーを返しました",
};

const wall1 = { limit: null };

function updateWall1Size() {
  const bytes = new TextEncoder().encode(el("wall1-text").value).length;
  el("wall1-size").textContent = wall1.limit ? `${bytes} バイト(上限 ${wall1.limit} バイト)` : `${bytes} バイト`;
}

async function loadExample() {
  try {
    const example = await call("GET /v1/demo/attack/walls/1/example");
    wall1.limit = example.limit_bytes;
    el("wall1-text").value = JSON.stringify(example.message, null, 2);
    showMessage(el("wall1-error"), null);
    updateWall1Size();
  } catch (error) {
    showError(el("wall1-error"), error);
  }
}

function factsTable(rows) {
  return h(
    "div",
    { class: "table-wrap" },
    h("table", {}, h("tbody", {}, rows.map(([label, value]) => h("tr", {}, h("th", { scope: "row" }, label), h("td", {}, value))))),
  );
}

function pretty(value) {
  return JSON.stringify(value, null, 2);
}

/** 壁 1 の応答(web.attack.raw_message の「応答の形」)を、何を送ったら・どこで・どう止まったかの表にする。 */
function renderWall1(result) {
  const [label, kind] = OUTCOMES[result.outcome] ?? [result.outcome, "info"];
  const rows = [
    ["結果", h("span", { class: `badge badge-${kind}` }, label)],
    ["止まった場所", result.stopped_at ? (STOPPED_AT[result.stopped_at] ?? result.stopped_at) : "止まりませんでした"],
    ["LLM", result.llm_called === true ? "動きました" : result.llm_called === false ? "動いていません" : "動いたかどうか分かりません"],
    ["金庫への登録", result.registered_in_vault ? "登録されました" : "登録されていません(どの交渉にも影響しません)"],
    ["送った大きさ", `${result.sent_bytes} バイト`],
  ];
  const rejection = result.rejection;
  if (rejection) {
    const reason = rejection.reason ? (REASONS[rejection.reason] ?? rejection.reason) : rejection.message;
    rows.push(["拒否の理由", reason ?? ""]);
  }
  return h(
    "div",
    {},
    factsTable(rows),
    rejection ? h("details", {}, h("summary", {}, "拒否の詳細(JSON)"), h("pre", {}, pretty(rejection))) : null,
    result.result ? h("details", { open: true }, h("summary", {}, "LLM が返した出力(計画または手)"), h("pre", {}, pretty(result.result))) : null,
    result.usage ? h("details", {}, h("summary", {}, "使用量"), h("pre", {}, pretty(result.usage))) : null,
  );
}

async function sendWall1() {
  await withBusy(
    el("wall1-send"),
    async () => {
      showMessage(el("wall1-error"), null);
      clear(el("wall1-result"));
      try {
        replace(el("wall1-result"), renderWall1(await call("POST /v1/demo/attack/walls/1", { text: el("wall1-text").value })));
      } catch (error) {
        // 413・422・502・504 は、止まった場所と理由を持つ本文(壁の実演の結果)で返る
        if (error instanceof ApiError && error.body && error.body.wall === 1) replace(el("wall1-result"), renderWall1(error.body));
        else throw error;
      }
    },
    (error) => showError(el("wall1-error"), error, { daily_limit_reached: DAILY_LIMIT_TEXT }),
  );
}

// ---- 壁 2: LLM に渡った入力 ----

const PHASE_LABELS = { plan: "計画", decide: "決定" };

function prettyJsonText(text) {
  try {
    return pretty(JSON.parse(text));
  } catch {
    return text;
  }
}

function renderWall2(report) {
  const checked = (phase) => {
    const found = report.inspection[phase];
    if (!found) return null;
    return h("p", { class: "small" }, `検査: 自由文 ${found.free_text.length} 件・ID ${found.ids.length} 件・グリッド外の数値 ${found.off_grid_numbers.length} 件`);
  };
  return h(
    "div",
    {},
    report.clean
      ? h("div", { class: "msg msg-ok" }, "検査の結果: 自由文・ID・グリッド外の数値(生の値)は、入っていません。")
      : h("div", { class: "msg msg-error", role: "alert" }, "検査で、入ってはならないものが見つかりました。"),
    h("details", {}, h("summary", {}, "固定の前文(指示文)"), h("pre", {}, report.preamble)),
    ["plan", "decide"].map((phase) =>
      h(
        "details",
        { open: true },
        h("summary", {}, `${PHASE_LABELS[phase]}の入力(TurnInput)`),
        report.turn_inputs[phase] ? h("pre", {}, prettyJsonText(report.turn_inputs[phase])) : h("p", { class: "muted small" }, "この手番では、この呼び出しはありませんでした。"),
        checked(phase),
      ),
    ),
  );
}

async function refreshWall2({ quiet = false } = {}) {
  const nid = attack.selected;
  if (!nid) return;
  try {
    const report = await call("GET /v1/demo/attack/walls/2/{nid}", { path: { nid } });
    if (nid !== attack.selected) return;
    showMessage(el("wall2-error"), null);
    replace(el("wall2-result"), renderWall2(report));
  } catch (error) {
    if (nid === attack.selected && !(quiet && error instanceof ApiError && error.status === 404)) showError(el("wall2-error"), error);
  }
}

// ---- 壁 3: 金庫の答え ----

function renderWall3(report) {
  if (!report.answers.length) return h("p", { class: "empty-note" }, "まだ、攻撃者の提案がありません。");
  return h(
    "div",
    {},
    h("p", { class: "small" }, `答えの種類は「${report.answer_values.map((value) => VERDICT_LABELS[value] ?? value).join("」「")}」の 3 つだけです(提案 ${report.answers.length} 件)。`),
    h(
      "div",
      { class: "table-wrap" },
      h(
        "table",
        {},
        h("thead", {}, h("tr", {}, h("th", {}, "記録"), h("th", {}, "攻撃者の提案"), h("th", {}, "金庫の答え"))),
        h(
          "tbody",
          {},
          report.answers.map((answer) =>
            h("tr", {}, h("td", {}, `#${answer.seq}`), h("td", {}, packageChips(answer.package)), h("td", {}, verdictBadge(answer.vault_answer))),
          ),
        ),
      ),
    ),
  );
}

async function refreshWall3() {
  const nid = attack.selected;
  if (!nid) return;
  try {
    const report = await call("GET /v1/demo/attack/walls/3/{nid}", { path: { nid } });
    if (nid !== attack.selected) return;
    showMessage(el("wall3-error"), null);
    replace(el("wall3-result"), renderWall3(report));
  } catch (error) {
    if (nid === attack.selected) showError(el("wall3-error"), error);
  }
}

// 候補者側のパネルに記録が届くたびに、壁 3 を読み直す(続けて届くときは、まとめて 1 回)。
function scheduleWall3() {
  clearTimeout(attack.wall3Timer);
  attack.wall3Timer = setTimeout(refreshWall3, 700);
}

// ---- 配線 ----

el("create-button").addEventListener("click", createAttack);
el("replace-button").addEventListener("click", replaceInstruction);
el("attack-select").addEventListener("change", (event) => select(event.currentTarget.value));
el("wall1-send").addEventListener("click", sendWall1);
el("wall1-example").addEventListener("click", loadExample);
el("wall1-text").addEventListener("input", updateWall1Size);
el("wall2-button").addEventListener("click", () => refreshWall2());
el("wall3-button").addEventListener("click", () => refreshWall3());
el("instruction").after(counterFor(el("instruction")));
el("new-instruction").after(counterFor(el("new-instruction")));

loadExample();
