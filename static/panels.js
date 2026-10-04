/* 並べて見る画面(FR-39。design.md §7)の 2 つのパネル「最悪漏れてもここまで」「まだ隠しているもの」。
 *
 * - 本人の丸め済みポリシーから、表示のたびにサーバが作る(GET /v1/principals/me/panels。web には保存しない)。この画面も、何も保存しない
 *   (ブラウザの保存領域には、何も書かない。§1.2)。生の値(例: 620 万円)は、どこにも保存していないので、ここに出るのは、丸めた後のグリッドの値だけ。
 * - 「最悪漏れてもここまで」: 金庫の答えをすべて見られたとしても、知られるのは、軸ごとの、丸めた後のマスまで。条件が付いていない軸(cells が 0)は出さない。
 *   サーバは、多次元のポリシーを軸ごとに分けて見せる。軸どうしの組み合わせの情報は、ここには載っていない(画面の 1 文で、誤読させない)。
 * - 「まだ隠しているもの」: 値を持たず、種類と、どのマスの中かだけ。外した軸(交渉中に聞かれたときだけ答える)と、面談の入力(辞めた理由)も。
 * - API の経路は、このモジュールに書かない(ページのスクリプトが load() を渡す)。
 */

import { AXIS_LABELS, formatAxisValue, h, replace, showError, showMessage, waiting } from "./ui.js";

// 年収のように、正確な値(境目)が隠れている軸の、画面での呼び名。
const RANGE_LABELS = { salary: "最低年収" };

/** マス(丸めた後の値)を、画面の言葉にする。年収は隣り合う 2 点の間、離散の軸はその値そのもの。 */
export function cellText(axis, cell) {
  if ("value" in cell) return formatAxisValue(axis, cell.value); // 区分軸: ポリシーが限っている値
  if (cell.low === cell.high) return formatAxisValue(axis, cell.low); // とびとびの数値軸: 値そのもの
  return `${cell.low}〜${cell.high} 万円`; // 年収: 隣り合うグリッドの 2 点の間のマス
}

function worstCasePanel(worstCase) {
  const rows = worstCase
    .filter((item) => item.cells.length > 0)
    .map((item) =>
      h(
        "li",
        { class: "axis-row" },
        h("span", { class: "axis-name" }, AXIS_LABELS[item.axis] ?? item.axis),
        h("span", { class: "axis-cells" }, item.cells.map((cell) => h("span", { class: "chip" }, cellText(item.axis, cell)))),
      ),
    );
  return h(
    "section",
    { class: "card", "aria-labelledby": "panel-worst-title" },
    h("h3", { id: "panel-worst-title" }, "最悪漏れてもここまで"),
    h("p", { class: "small muted" }, "金庫の答えをすべて見られたとしても、知られるのは、次のマス(丸めた後の値)までです。"),
    rows.length ? h("ul", { class: "axis-list" }, rows) : h("p", { class: "empty-note" }, "条件が付いている軸がありません。"),
    h(
      "ul",
      { class: "small muted" },
      h("li", {}, "年収は、50 万円刻みのマスに丸めて預けています。マスの中のどこかは、金庫の答えからは分かりません。"),
      h("li", {}, "リモート・当直などの、とびとびの値の軸は、条件そのものが外から知られ得ます。"),
      h("li", {}, "この表は、軸ごとに分けて見せています。軸どうしの組み合わせ(どの条件がどの条件と組になっているか)の情報は、この表には載っていません。"),
    ),
  );
}

function stillHiddenPanel(panels) {
  const cellsOf = (axis) => panels.worst_case.find((item) => item.axis === axis)?.cells ?? [];
  const items = [];
  for (const item of panels.still_hidden) {
    if (item.cells === 0) continue; // 隠れているものがない軸は、出さない
    const label = AXIS_LABELS[item.axis] ?? item.axis;
    if (item.removed) {
      items.push(
        h("li", {}, `${label}の条件(外しています)`, h("div", { class: "small muted" }, "事前には預けていません。交渉中に聞かれたときだけ答えます。")),
      );
    } else if (item.kind === "range") {
      const where = cellsOf(item.axis).map((cell) => cellText(item.axis, cell)).join("・");
      items.push(h("li", {}, `正確な${RANGE_LABELS[item.axis] ?? label}(${where}のマスの中のどこか)`));
    } else {
      items.push(h("li", {}, label));
    }
  }
  items.push(h("li", {}, "辞めた理由(面談時に破棄済み)"));
  return h(
    "section",
    { class: "card", "aria-labelledby": "panel-hidden-title" },
    h("h3", { id: "panel-hidden-title" }, "まだ隠しているもの"),
    h("p", { class: "small muted" }, "値は持たず、種類と、どのマスの中かだけを示します。生の値は、保存していません。"),
    h("ul", { class: "hidden-list" }, items),
  );
}

/** 2 つのパネルの区画を動かす。body・error はページの HTML にある要素。load() は、サーバが作った 2 つのパネル({worst_case, still_hidden})を返す。 */
export function mountPanels({ body, error }, { load }) {
  async function refresh() {
    replace(body, waiting("読み込み中…"));
    try {
      const panels = await load();
      showMessage(error, null);
      replace(body, [worstCasePanel(panels.worst_case), stillHiddenPanel(panels)]);
    } catch (failure) {
      replace(body, null);
      showError(error, failure, { not_found: "まだ、条件が保存されていません。面談を送信すると、ここに出ます。" });
    }
  }

  document.addEventListener("negotiation:cleared", () => {
    showMessage(error, null);
    replace(body, null);
  });
  refresh();
  return { refresh };
}
