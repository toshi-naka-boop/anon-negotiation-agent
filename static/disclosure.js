/* 開示設定の上部の説明(DESIGN.md §4.6・§5)。「渡していないもの」「渡し得るもの」「渡したもの」「後から取り消せないもの」の 4 つの区分を、
 * 本人の丸め済みの条件(GET /v1/principals/me/panels の応答。panels.js と同じ形)から描く。
 *
 * - 値を持つのは「外した条件」の軸の名前だけ。生の値は、サーバが保存していないので、ここにも出ない。
 * - API の経路は、このモジュールに書かない(ページのスクリプトが loadPanels() を渡す)。ブラウザの保存領域には何も書かない。
 * - 4 状態(空・待ち・エラー)は、ui.js の stateBox で出す。成功は、4 つの区分そのもの。
 * - 聞くイベント(document): negotiation:cleared = データの削除(空の表示に戻す)。読み直しは、ページが返す refresh() で行う。
 */

import { ApiError, describeError } from "./api.js";
import { AXIS_LABELS, h, replace, stateBox } from "./ui.js";

// 段ごとに開くもの(DESIGN.md §5 の表)。段 1・段 2 は、押したら戻せない。
const STAGES = [
  ["段 0", "見込み(高・中・なし)と、組み合わせ 1 つ", "判定が出たら自動で開きます。操作はありません。"],
  ["段 1", "匿名の職務要約(あなたが書くもの)", "あなたと相手の双方が「会う」を押したときだけ開きます。戻せません。"],
  ["段 2", "氏名と連絡先", "双方が「承認」を押したときだけ開きます。戻せません。"],
];

function group(title, ...body) {
  return h("section", { class: "disclosure-group" }, h("h3", {}, title), body);
}

/** 渡していないもの: 生の値、面談の記録、外した条件(panels の still_hidden で removed の軸)。 */
function notGiven(panels) {
  const removed = panels.still_hidden.filter((item) => item.removed).map((item) => AXIS_LABELS[item.axis] ?? item.axis);
  return group(
    "渡していないもの",
    h(
      "ul",
      {},
      h("li", {}, "生の値(あなたが書いた本音の数字)。預けた後に捨てました。"),
      h("li", {}, "面談の記録(辞めた理由など)。面談の最後に捨てました。"),
      h(
        "li",
        {},
        removed.length ? `外した条件: ${removed.join("・")}。` : "外した条件: ありません。",
        h("div", { class: "small muted" }, "外した条件は、事前には預けません。交渉中に聞かれたときだけ、あなたが答えます。戻すには、面談をやり直します。"),
      ),
    ),
  );
}

/** 渡し得るもの: マス目の粗さと、段ごとに開くもの。 */
function mayBeGiven() {
  return group(
    "渡し得るもの",
    h("p", {}, "相手に分かるのは、最大でもマス目 1 つぶんです(年収なら 50 万円の幅)。マス目の中のどこかは、分かりません。"),
    h("p", { class: "small muted" }, "軸ごとのマス目は、下の「最悪漏れてもここまで」にあります。"),
    h(
      "ol",
      { class: "disclosure-stages" },
      STAGES.map(([stage, what, how]) => h("li", {}, h("strong", {}, `${stage}: `), what, h("div", { class: "small muted" }, how))),
    ),
  );
}

function given() {
  return group("渡したもの", h("p", {}, "下の開示台帳に、いつ・何を・どの交渉で・誰の操作で、が残ります。生の値は書きません。"));
}

function irreversible() {
  return group(
    "後から取り消せないもの",
    h("p", {}, "段 1(職務要約)と段 2(氏名と連絡先)の開示。相手に見えたものは戻せません。押す前に「戻せません」が出ます。"),
    h("p", { class: "small muted" }, "取り消せるもの: 交渉の取消(判定の前)、一時停止、データの削除(全部)。"),
  );
}

function emptyBox() {
  return stateBox("empty", "まだ何も渡していません", "面談を終えて預けると、ここに、渡していないもの・渡し得るものが出ます。");
}

/**
 * 開示設定の上部の説明を動かす。body・error はページの HTML にある要素。loadPanels() は、サーバが作った 2 つのパネル({worst_case, still_hidden})を返す。
 * 返り値の refresh() で読み直す(条件を預け直したとき・データを消したとき)。
 */
export function mountDisclosure({ body, error }, { loadPanels }) {
  let sequence = 0; // 古い読み込みの結果を捨てるための札

  async function refresh() {
    const token = (sequence += 1);
    replace(error, null);
    replace(body, stateBox("waiting", "読み込んでいます", "あなたは何もしなくて大丈夫。"));
    let panels;
    try {
      panels = await loadPanels();
    } catch (failure) {
      if (token !== sequence) return;
      if (failure instanceof ApiError && failure.status === 404) {
        replace(body, emptyBox()); // まだ条件を預けていない
        return;
      }
      replace(body, null);
      replace(error, stateBox("error", "読み込めませんでした", describeError(failure)));
      return;
    }
    if (token !== sequence) return;
    if (!panels || !Array.isArray(panels.still_hidden) || panels.still_hidden.length === 0) {
      replace(body, emptyBox());
      return;
    }
    replace(body, h("div", { class: "disclosure" }, notGiven(panels), mayBeGiven(), given(), irreversible()));
  }

  document.addEventListener("negotiation:cleared", () => {
    sequence += 1;
    replace(error, null);
    replace(body, emptyBox());
  });
  refresh();
  return { refresh };
}
