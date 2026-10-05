/* 推定区間メーター(防御あり)と、防御なしのシミュレーション(FR-45。design.md §8.3)。
 *
 * メーター(mountMeter)
 * - 攻撃画面(attack.js)が、自分が作った交渉の ID の一覧を、一覧が変わるたびに document へ飛ばす(attack:negotiations。{nids, refresh})。
 *   一覧を覚えるのは、画面のメモリだけ(訪問者を見分ける ID は作らない。L7-3)。web は一覧を覚えず、その場で金庫を読んで区間を返す(POST /v1/demo/meter)。
 * - 計算を頼むのは、新しい提案が届いたとき(attack:offer)と、二分探索の実演が終わったとき(attack:negotiations の refresh が真)だけ。
 *   ポーリングはしない(入口の枠は、クライアントごとに 10 分で 60 回。続けて届いたときは、まとめて 1 回)。失敗したときは、利用者が押したときだけ、やり直す。
 * - 区間は (lower, upper]: 境目は lower より上、upper 以下(None は、その側の情報がない)。年収のグリッド(50 万円刻み)の上に、帯として描く。
 *   「金庫の答えをすべて見られたとしても、ここまで」(最悪の場合の攻撃者を想定した表示)。1 マスになったら、これ以上は絞れない。
 *
 * シミュレーション(mountSimulation)
 * - 防御なしの場合の計算(GET /v1/demo/meter/simulation?value=)。架空人物の生の値を 10 万円刻みで二分探索すると、7 手で特定される。金庫は使わない。
 *   「防御なしの金庫」は、このシステムのコードに存在しない。画面に「シミュレーション」と明示する。
 */

import { call } from "./api.js";
import { clear, h, message, packageText, replace, showError, showMessage, waiting, withBusy } from "./ui.js";

// 年収のグリッド(negotiation_core の語彙。50 万円刻み)と、1 回に渡せる交渉の数(web.meter_api の MAX_NEGOTIATION_IDS)。
// 画面の写しで、tests/test_ui_static.py が、サーバの値と一致することを確かめる。
export const SALARY_GRID = { low: 300, high: 1500, step: 50 };
export const MAX_NEGOTIATION_IDS = 20;
// シミュレーションの探索の範囲(web.meter_api の SIMULATION_*。10 万円刻み)。
export const SIMULATION_RANGE = { low: 300, high: 1500, step: 10 };

// グリッドの点 25 個が作る 24 マスに、両端の外側 1 マスずつを足した 26 マス(estimate_interval の Interval.cells と同じ数え方)
const CELL_COUNT = (SALARY_GRID.high - SALARY_GRID.low) / SALARY_GRID.step + 2;
const TICK_EVERY = 300; // 目盛りを付ける値の間隔(万円)
const REFRESH_DELAY_MS = 300; // 続けて届いた提案を、まとめて 1 回にするための待ち

/** 区間 (lower, upper] を、画面の言葉にする。 */
export function describeInterval(interval) {
  const { lower, upper } = interval;
  if (lower === null && upper === null) return "まだ何も分かっていません(年収のどの値もあり得ます)";
  if (lower === null) return `${upper} 万円以下`;
  if (upper === null) return `${lower} 万円より上`;
  return `${lower} 万円より上、${upper} 万円以下`;
}

/** 区間がまたぐマスの、最初と最後の番号(0〜25)。マス k(1〜24)は (grid[k-1], grid[k]]、マス 0 は grid[0] 以下、最後のマスは grid の最大より上。 */
export function cellRange(interval) {
  const gridIndex = (value) => (value - SALARY_GRID.low) / SALARY_GRID.step;
  const first = interval.lower === null ? 0 : gridIndex(interval.lower) + 1;
  const last = interval.upper === null ? CELL_COUNT - 1 : gridIndex(interval.upper);
  return [first, last];
}

/** 年収のグリッドの上の、区間の帯(マスを並べて、区間に入るマスを塗る。インラインのスタイルは使わない)。 */
function meterBar(interval) {
  const [first, last] = cellRange(interval);
  const cells = [];
  for (let index = 0; index < CELL_COUNT; index += 1) {
    const upper = SALARY_GRID.low + index * SALARY_GRID.step; // このマスの上端(最後の外側のマスは、グリッドの外)
    const open = index === 0 || index === CELL_COUNT - 1;
    const labelled = index < CELL_COUNT - 1 && upper % TICK_EVERY === 0;
    const inRange = index >= first && index <= last;
    cells.push(
      h("span", { class: `meter-cell${inRange ? " in-range" : ""}${open ? " open-end" : ""}` }, labelled ? h("span", { class: "meter-tick" }, String(upper)) : null),
    );
  }
  return h(
    "div",
    { class: "meter", role: "img", "aria-label": `候補者の年収の境目: ${describeInterval(interval)}` },
    h("div", { class: "meter-track" }, cells),
    h("div", { class: "meter-unit small muted" }, "年収(万円)。1 マスは 50 万円。両端の点線のマスは、グリッドの外まで広がる部分です。"),
  );
}

function groupsTable(groups) {
  return h(
    "div",
    { class: "table-wrap" },
    h(
      "table",
      {},
      h("thead", {}, h("tr", {}, ["固定した条件(攻撃者の探索線)", "金庫の答えの数", "区間", "マス"].map((title) => h("th", {}, title)))),
      h(
        "tbody",
        {},
        groups.map((group) =>
          h(
            "tr",
            {},
            h("td", {}, packageText(group.axes)),
            h("td", {}, String(group.observations)),
            h("td", {}, describeInterval(group.interval)),
            h("td", {}, String(group.interval.cells)),
          ),
        ),
      ),
    ),
  );
}

/**
 * メーターの区画を動かす。body・error はページの HTML にある要素。attack:negotiations で交渉 ID の一覧を受け取り、
 * attack:offer(新しい提案が届いた)と、一覧に refresh が付いたとき(二分探索の実演が終わった)に、区間を計算してもらう。
 */
export function mountMeter({ body, error }) {
  const state = { nids: [], timer: null, running: false, queued: false };

  function showEmpty() {
    replace(
      body,
      h("p", { class: "empty-note" }, "まだ、金庫の答えがありません。攻撃の交渉を作るか、「台本の攻撃者で実演」を押すと、攻撃者の提案が届くたびに、ここが更新されます。"),
    );
  }

  function render(response) {
    if (response.narrowest === null) {
      showEmpty();
      return;
    }
    const { interval } = response.narrowest;
    replace(
      body,
      h("p", { class: "meter-claim" }, h("strong", {}, response.note)),
      meterBar(interval),
      h("p", {}, "候補者の年収の境目は、", h("strong", {}, describeInterval(interval)), `の中にあります(${interval.cells} マス)。`),
      interval.cells === 1
        ? message("ok", "これ以上は絞れません。提案もグリッドの値に限られ、金庫も丸めた値でしか答えないので、境目がマスの中のどこかは、何度尋ねても分かりません。")
        : null,
      h("p", { class: "small muted" }, "区間は、候補者側の金庫の「受けられる」「受けられない」から計算しています。「本人確認が必要」は、情報なしとして数えます。候補者側のエージェントが実際に受けたかどうかは、使っていません。"),
      groupsTable(response.groups),
    );
  }

  async function refresh() {
    if (state.nids.length === 0) return;
    if (state.running) {
      state.queued = true; // 計算している間に、また届いた: 終わってから、もう 1 回
      return;
    }
    state.running = true;
    try {
      const response = await call("POST /v1/demo/meter", { body: { negotiation_ids: state.nids.slice(-MAX_NEGOTIATION_IDS) } });
      showMessage(error, null);
      render(response);
    } catch (failure) {
      showError(error, failure, {
        forbidden: "この画面で作った交渉が、サーバーに残っていません(再起動などで消えました)。攻撃の交渉を作り直してください。",
      });
      error.appendChild(h("button", { class: "btn btn-small", type: "button", onclick: () => refresh() }, "もう一度計算する"));
    } finally {
      state.running = false;
      if (state.queued) {
        state.queued = false;
        schedule();
      }
    }
  }

  function schedule() {
    clearTimeout(state.timer);
    state.timer = setTimeout(refresh, REFRESH_DELAY_MS);
  }

  document.addEventListener("attack:negotiations", (event) => {
    state.nids = [...event.detail.nids];
    if (event.detail.refresh) schedule();
  });
  document.addEventListener("attack:offer", schedule);
  showEmpty();
  return { refresh };
}

function stepsTable(steps) {
  return h(
    "div",
    { class: "table-wrap" },
    h(
      "table",
      {},
      h("thead", {}, h("tr", {}, ["手", "尋ねたこと", "答え", "残りの候補(万円)"].map((title) => h("th", {}, title)))),
      h(
        "tbody",
        {},
        steps.map((step, index) =>
          h(
            "tr",
            {},
            h("td", {}, String(index + 1)),
            h("td", {}, `${step.ask} 万円以上ですか?`),
            h("td", {}, step.at_least ? "はい" : "いいえ"),
            h("td", {}, step.low === step.high ? `${step.low}(特定)` : `${step.low}〜${step.high}`),
          ),
        ),
      ),
    ),
  );
}

/**
 * シミュレーションの区画を動かす。value(数の入力)・button・error・result はページの HTML にある要素。
 * 値は SIMULATION_RANGE の範囲の、10 万円刻み。サーバが二分探索の手の列と手数を計算して返す(防御がないので、尋ねるたびに正確に答えてもらえる)。
 */
export function mountSimulation({ value, button, error, result }) {
  const { low, high, step } = SIMULATION_RANGE;

  function render(simulation) {
    replace(
      result,
      h("div", { class: "banner banner-simulation", role: "note" }, "シミュレーション(防御なしの場合の計算。金庫は使っていません)"),
      h(
        "p",
        {},
        `架空人物の生の値が ${simulation.value} 万円のとき、防御がなければ、`,
        h("strong", {}, `${simulation.count} 手で、${simulation.found} 万円と特定されます`),
        "。",
      ),
      stepsTable(simulation.steps),
      h(
        "p",
        { class: "small muted" },
        "防御ありの金庫では、同じ探索をしても、上のメーターのとおり、50 万円幅の 1 マスまでしか絞れません。生の値(例: 620 万円)は、マスの中のどこかとしか分かりません。",
      ),
    );
  }

  async function simulate() {
    const amount = Number(value.value);
    if (value.value.trim() === "" || !Number.isInteger(amount) || amount < low || amount > high || amount % step !== 0) {
      showMessage(error, "warn", `年収は、${low}〜${high} 万円の、${step} 万円刻みの値で入れてください。`);
      clear(result);
      return;
    }
    await withBusy(
      button,
      async () => {
        showMessage(error, null);
        replace(result, waiting("計算しています…"));
        render(await call("GET /v1/demo/meter/simulation", { query: { value: amount } }));
      },
      (failure) => {
        clear(result);
        showError(error, failure);
      },
    );
  }

  button.addEventListener("click", simulate);
}
