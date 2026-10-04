/* 開示台帳の区画(design.md §6.2・§7。FR-38)。段の遷移(段を開いた・「会う」「承認」が押された)の記録を、交渉ごとに、時系列で並べる。
 *
 * - 台帳(GET /v1/principals/{pid}/ledger)は、見せたものの種類・相手・操作した主体・時刻だけで、生の値(職務要約の本文・氏名・連絡先)は
 *   含まない。途中確認の回答は、台帳ではなく活動ログ(金庫のイベント列の自分の側の見え方)にあるので、同じ画面に並べる(§7)。
 *   活動ログには時刻がないので、交渉ごとのまとまりの中で、回答を台帳の行より先に置く(途中確認は、交渉が終わる前にしか起きない。台帳の最初の行は、
 *   判定の後の段 0 の表示)。交渉のまとまりは、交渉を始めた順。
 * - API の経路は、このモジュールに書かない。ページのスクリプトが、loadLedger()・loadAnswers(nid)・negotiations() を渡す。
 * - 終わった交渉の回答は変わらないので、メモリの中に覚えて、読み直さない(ブラウザの保存領域には何も書かない)。
 * - 聞くイベント(document): negotiation:cleared = データの削除(区画を空にする)。読み直しは、ページが返す refresh() で行う
 *   (段の状態が変わるたびに、stages.js の onChange が呼ぶ)。
 */

import { ITEM_LABELS } from "./stages.js";
import { formatDateTime, h, packageChips, replace, showError, showMessage, waiting } from "./ui.js";

const OPERATOR_LABELS = {
  principal: "あなたの操作",
  fictional_employer: "架空の求人の自動応答",
  fictional_candidate: "架空の候補者の自動操作",
  system: "システム(自動)",
};
const RECIPIENT_LABELS = { candidate: "あなた", employer: "求人側", both: "双方" };

/** 台帳の 1 行を、1 つの文にする。 */
export function describeRow(row) {
  if (row.action === "disclose") {
    const items = row.items.map((item) => ITEM_LABELS[item] ?? item).join("・");
    const simulated = row.simulated ? "(模擬表示。連絡先は集めていないので、実際には渡っていません)" : "";
    return `段 ${row.stage} が開きました: ${items}を、${RECIPIENT_LABELS[row.to] ?? "相手"}に表示${simulated}`;
  }
  return `「${row.action === "meet" ? "会う" : "承認"}」が押されました`;
}

function rowElement(row) {
  return h(
    "li",
    { class: `ledger-item ledger-${row.action}` },
    h("time", { class: "ledger-time small muted", datetime: row.at }, formatDateTime(row.at)),
    h("div", {}, h("div", {}, describeRow(row)), h("div", { class: "small muted" }, OPERATOR_LABELS[row.operator] ?? row.operator)),
  );
}

function answerElement(entry) {
  return h(
    "li",
    { class: "ledger-item ledger-answer" },
    h("span", { class: "ledger-time small muted" }, "(時刻なし)"),
    h(
      "div",
      {},
      h("div", {}, "途中確認に答えました: ", packageChips(entry.package), ` → ${entry.answer === "accept" ? "受ける" : "受けない"}`),
      h("div", { class: "small muted" }, "活動ログの記録です(活動ログには時刻がありません)。"),
    ),
  );
}

function groupElement(group) {
  return h(
    "section",
    { class: "ledger-group" },
    h("h3", {}, group.title, " ", h("span", { class: "small muted" }, `開始 ${formatDateTime(group.createdAt)}`)),
    h("ol", { class: "ledger-list" }, [group.answers.map(answerElement), group.rows.map(rowElement)]),
  );
}

/**
 * 交渉ごとのまとまりにする。known は、いま分かっている交渉([{nid, title, createdAt}])、entries は台帳の行、answers は交渉 ID → 途中確認の回答の記録。
 * 記録のない交渉は出さない。交渉の一覧にない交渉(台帳の行だけある)も、出す。
 */
export function groupRecords(known, entries, answers) {
  const groups = new Map(
    known.map((item) => [item.nid, { nid: item.nid, title: item.title, createdAt: item.createdAt, answers: answers.get(item.nid) ?? [], rows: [] }]),
  );
  for (const row of entries) {
    if (!groups.has(row.nid)) groups.set(row.nid, { nid: row.nid, title: "(一覧にない交渉)", createdAt: row.at, answers: [], rows: [] });
    groups.get(row.nid).rows.push(row);
  }
  return [...groups.values()]
    .filter((group) => group.rows.length || group.answers.length)
    .sort((a, b) => a.createdAt.localeCompare(b.createdAt));
}

/**
 * 開示台帳の区画を動かす。body・error はページの HTML にある要素。
 * loadLedger() は台帳の行(LedgerEntry の配列)、loadAnswers(nid) はその交渉の途中確認の回答(活動ログの principal_answer の記録の配列)、
 * negotiations() はいま分かっている交渉([{nid, title, createdAt, ended}])を返す。
 */
export function mountLedger({ body, error }, { loadLedger, loadAnswers, negotiations }) {
  let sequence = 0; // 古い読み込みの結果を捨てるための札
  const endedAnswers = new Map(); // 終わった交渉の回答(メモリだけ)

  function showEmpty() {
    replace(body, h("p", { class: "empty-note" }, "まだ記録がありません。交渉が合意で終わると、段 0 の表示から記録されます。"));
  }

  async function refresh() {
    const token = (sequence += 1);
    const known = negotiations();
    let entries;
    try {
      entries = await loadLedger();
    } catch (failure) {
      if (token === sequence) showError(error, failure);
      return;
    }
    const answers = new Map();
    let failed = false;
    await Promise.all(
      known.map(async (item) => {
        if (item.ended && endedAnswers.has(item.nid)) {
          answers.set(item.nid, endedAnswers.get(item.nid));
          return;
        }
        try {
          const list = await loadAnswers(item.nid);
          answers.set(item.nid, list);
          if (item.ended) endedAnswers.set(item.nid, list);
        } catch {
          failed = true;
        }
      }),
    );
    if (token !== sequence) return;
    showMessage(error, "warn", failed ? "途中確認の回答の一部を読み込めませんでした。" : "");
    const groups = groupRecords(known, entries, answers);
    if (groups.length) replace(body, groups.map(groupElement));
    else showEmpty();
  }

  document.addEventListener("negotiation:cleared", () => {
    sequence += 1;
    endedAnswers.clear();
    showMessage(error, null);
    showEmpty();
  });
  replace(body, waiting("読み込み中…"));
  refresh();
  return { refresh };
}
