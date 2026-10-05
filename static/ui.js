/* 画面の部品(DOM の組み立て・組み合わせの表示・活動ログの表示と購読)。
 *
 * - API から来た文字列は、必ず textContent(h() の文字列の子)で入れる。HTML として解釈する経路は、この画面のコードに存在しない
 *   (tests/test_ui_static.py が、HTML を文字列から組み立てる API の名前が使われていないことを確かめる)。
 * - ブラウザの保存領域には何も書かない(生の値を残さない。design.md §1.2・scripts/check_no_web_storage.sh)。
 */

import { ApiError, call, describeError, url } from "./api.js";

// ---- DOM ----

// プロパティとして代入するもの(それ以外は属性にする)。
const PROPERTIES = new Set(["value", "checked", "disabled", "hidden", "selected", "htmlFor", "textContent"]);

function append(parent, child) {
  if (child === null || child === undefined || child === false) return;
  if (Array.isArray(child)) {
    child.forEach((item) => append(parent, item));
  } else if (child instanceof Node) {
    parent.appendChild(child);
  } else {
    parent.appendChild(document.createTextNode(String(child)));
  }
}

/** 要素を作る。props: class・text・on* (イベント)・dataset・value などのプロパティ・その他は属性。children の文字列は、文字として入る。 */
export function h(tag, props = {}, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") element.className = value;
    else if (key === "text") element.textContent = value;
    else if (key === "dataset") Object.assign(element.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") element.addEventListener(key.slice(2).toLowerCase(), value);
    else if (PROPERTIES.has(key)) element[key] = value;
    else element.setAttribute(key, value === true ? "" : String(value));
  }
  append(element, children);
  return element;
}

/** 子をすべて消す(入れ替えるなら replace)。 */
export function clear(element) {
  element.replaceChildren();
  return element;
}

export function replace(element, ...children) {
  element.replaceChildren();
  append(element, children);
  return element;
}

/** メッセージの枠。kind は error・warn・ok・info。 */
export function message(kind, text) {
  return h("div", { class: `msg msg-${kind}`, role: kind === "error" ? "alert" : "status" }, text);
}

/** container に、メッセージを 1 つだけ出す(text が空なら消す)。 */
export function showMessage(container, kind, text) {
  clear(container);
  if (text) container.appendChild(message(kind, text));
}

/** container に、API のエラーを日本語で出す。 */
export function showError(container, error, overrides) {
  showMessage(container, "error", describeError(error, overrides));
}

/** 待ちの表示(LLM を呼ぶ手順など)。 */
export function waiting(text) {
  return h("div", { class: "msg msg-info", role: "status" }, h("span", { class: "spinner", "aria-hidden": "true" }), text);
}

/**
 * ボタンの処理を実行する間、ボタンを押せなくする(二度押しを防ぐ)。処理が投げた例外は、onError に渡す
 * (なければ、そのまま投げ直す)。
 */
export async function withBusy(button, action, onError) {
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  try {
    return await action();
  } catch (error) {
    if (!onError) throw error;
    onError(error);
    return undefined;
  } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }
}

// ---- 文字数の表示 ----

/** textarea の下に文字数を出す要素を返す(上限は maxlength 属性)。 */
export function counterFor(textarea) {
  const max = Number(textarea.getAttribute("maxlength"));
  const counter = h("div", { class: "counter", "aria-hidden": "true" });
  const update = () => {
    counter.textContent = max ? `${textarea.value.length} / ${max} 文字` : `${textarea.value.length} 文字`;
  };
  textarea.addEventListener("input", update);
  update();
  return counter;
}

// ---- 組み合わせ・評価・結果の言葉 ----

export const AXIS_LABELS = {
  salary: "年収",
  remote_days: "リモート",
  night_duty: "当直",
  review_months: "昇給見直し",
  training: "研修",
  side_job: "副業",
  start: "入職時期",
};

const CATEGORICAL_VALUES = {
  training: { none: "なし", available: "あり" },
  side_job: { not_allowed: "不可", allowed: "可" },
  start: { within_1_month: "1 か月以内", within_3_months: "3 か月以内", within_6_months: "6 か月以内" },
};

/** 軸の値を、画面の言葉にする(年収は 50 万円刻みのグリッドの値)。 */
export function formatAxisValue(axis, value) {
  switch (axis) {
    case "salary":
      return `${value} 万円`;
    case "remote_days":
      return value === 0 ? "週 0 日(フル出社)" : value >= 5 ? "週 5 日(フルリモート)" : `週 ${value} 日`;
    case "night_duty":
      return value === 0 ? "なし" : `月 ${value} 回`;
    case "review_months":
      return `${value} か月`;
    default:
      return CATEGORICAL_VALUES[axis]?.[value] ?? String(value);
  }
}

/** 組み合わせ(7 軸)を、1 行の文字にする。 */
export function packageText(pkg) {
  return Object.keys(AXIS_LABELS)
    .filter((axis) => axis in pkg)
    .map((axis) => `${AXIS_LABELS[axis]} ${formatAxisValue(axis, pkg[axis])}`)
    .join(" / ");
}

/** 組み合わせ(7 軸)を、軸ごとの小さな札で並べる。 */
export function packageChips(pkg) {
  const chips = Object.keys(AXIS_LABELS)
    .filter((axis) => axis in pkg)
    .map((axis) =>
      h("span", { class: "chip" }, h("span", { class: "chip-label" }, AXIS_LABELS[axis]), formatAxisValue(axis, pkg[axis])),
    );
  return h("span", { class: "pkg" }, chips);
}

export const VERDICT_LABELS = {
  acceptable: "受けられる",
  not_acceptable: "受けられない",
  needs_confirmation: "本人確認が必要",
};
const VERDICT_KINDS = { acceptable: "ok", not_acceptable: "danger", needs_confirmation: "warn" };

export function verdictBadge(verdict) {
  return h("span", { class: `badge badge-${VERDICT_KINDS[verdict] ?? "info"}` }, VERDICT_LABELS[verdict] ?? String(verdict));
}

export const LIKELIHOOD_LABELS = { high: "高", medium: "中", none: "なし" };

/** 最終結果 {likelihood, package}。理由は含まれない(AC-08)。 */
export function resultView(result, { title = "最終結果" } = {}) {
  const none = result.likelihood === "none";
  return h(
    "div",
    { class: `result-box${none ? " none" : ""}` },
    h("strong", {}, `${title}: 見込み ${LIKELIHOOD_LABELS[result.likelihood] ?? result.likelihood}`),
    none ? h("div", { class: "small" }, "合意できる組み合わせは見つかりませんでした。") : null,
    result.package ? h("div", {}, packageChips(result.package)) : null,
  );
}

export function formatDateTime(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  return date.toLocaleString("ja-JP", { dateStyle: "medium", timeStyle: "short" });
}

// ---- 活動ログ ----

const ACTION_LABELS = {
  "self:check": "確かめ",
  "self:propose": "提案",
  "counterparty:propose": "相手の提案",
  "self:reject": "相手の提案を断る",
  "counterparty:reject": "相手に断られた",
  "self:ask_principal": "本人に確認",
  "self:principal_answer": "本人の回答",
  "self:invalid": "無効な手",
  "self:pause": "一時停止",
  "self:resume": "再開",
  "system:final_result": "最終結果",
};

// 無効な手の理由(negotiation_core.LastErrorReason)
const REASON_LABELS = {
  not_acceptable_to_own_principal: "本人が受けられない組み合わせでした",
  no_pending_offer: "受ける相手の提案がありませんでした",
  question_not_applicable: "途中確認の対象にならない組み合わせでした",
  question_budget_exhausted: "途中確認の回数の上限に達していました",
  evaluation_budget_exhausted: "評価の回数の上限に達していました",
  schema_invalid: "形式が正しくない手でした",
  agent_timeout: "エージェントが時間内に応答しませんでした",
  output_truncated: "出力が長すぎて切れました",
};

const MOVE_LABELS = {
  propose: "提案",
  accept: "受け入れ",
  reject: "断り",
  check: "確かめ",
  ask_principal: "本人への確認",
  end: "終了",
};

/**
 * 活動ログの 1 件を、表示用の要素にする。labels は {self, counterparty}(その画面での呼び名)と autoAnswer
 * (真なら、途中確認の質問と回答が架空人物の自動回答であることを添える。§4.4)。
 */
export function entryElement(entry, labels) {
  const actorName = { self: labels.self, counterparty: labels.counterparty, system: "システム" }[entry.actor] ?? entry.actor;
  const actionLabel = ACTION_LABELS[`${entry.actor}:${entry.action}`] ?? entry.action;
  const body = [];
  if (entry.package) body.push(h("div", {}, packageChips(entry.package)));
  if (entry.own_evaluation) {
    body.push(h("div", { class: "small" }, "自分側の評価: ", verdictBadge(entry.own_evaluation)));
  }
  if (entry.action === "ask_principal") {
    body.push(
      h("div", { class: "small muted" }, labels.autoAnswer ? "架空人物の自動回答で答えます。" : "あなたに「この組み合わせなら受けますか?」と確認します。"),
    );
  }
  if (entry.action === "principal_answer" && entry.answer) {
    body.push(
      h(
        "div",
        { class: "small" },
        `回答: ${entry.answer === "accept" ? "受ける" : "受けない"}`,
        labels.autoAnswer ? "(架空人物の自動回答)" : "",
      ),
    );
  }
  if (entry.action === "invalid") {
    const attempted = entry.attempted_move ? `(試した手: ${MOVE_LABELS[entry.attempted_move] ?? entry.attempted_move})` : "";
    body.push(h("div", { class: "small" }, `${REASON_LABELS[entry.reason] ?? entry.reason ?? "理由は記録されていません"}${attempted}`));
  }
  if (entry.result) body.push(resultView(entry.result));
  return h(
    "li",
    { class: `entry actor-${entry.actor} action-${entry.action}`, dataset: { seq: entry.seq } },
    h("span", { class: "entry-seq" }, `#${entry.seq}`),
    h("div", { class: "entry-head" }, h("span", { class: "entry-action" }, actionLabel), h("span", { class: "entry-actor" }, actorName)),
    h("div", { class: "entry-body" }, body),
  );
}

/** 活動ログの一覧(<ol class="activity">)と、まだ何もないときの案内を管理する。 */
export class ActivityView {
  constructor({ list, empty, labels }) {
    this.list = list;
    this.empty = empty;
    this.labels = labels;
  }

  reset() {
    clear(this.list);
    this.empty.hidden = false;
  }

  add(entries) {
    if (!entries.length) return;
    this.empty.hidden = true;
    const atBottom = this.list.scrollHeight - this.list.scrollTop - this.list.clientHeight < 48;
    entries.forEach((entry) => this.list.appendChild(entryElement(entry, this.labels)));
    if (atBottom) this.list.scrollTop = this.list.scrollHeight;
  }
}

/**
 * 交渉の活動ログを購読する(sides は、読む側の並び。本人は ["candidate"]、デモ・攻撃の 2 パネルは ["candidate", "employer"])。
 *
 * まず、側ごとに SSE(EventSource)でつなぐ。つなげない・読めない・サーバーが problem を送ったときは、通常の GET の再取得(2 秒ごと)に
 * 切り替える(エラーは onError に渡すので、権限の拒否などの理由が画面に出る)。再取得の口は poll が決める: positions(側ごとの、
 * 次に読む位置)を受け取って、側ごとの活動ログ({candidate: ActivityLog, employer: ActivityLog})を返す(2 パネルは、1 回で両側を返す
 * /panels を使う)。サーバーは 30 秒で SSE を閉じるが、EventSource が自動でつなぎ直し(Last-Event-ID で続きから)、重複は seq で除く。
 * 全部の側が最終結果を受け取ったら止めて onEnd を呼ぶ。onEntries(side, entries)は、新しい記録だけを渡す。
 *
 * streamRoute は、api.js の call と同じ「メソッドと経路」の文字列(側は side の引数で渡す)。path は SSE と同じ交渉の指定。close() で止める。
 */
export function watchNegotiation({ sides, streamRoute, path, poll, onEntries, onEnd, onError, pollInterval = 2000 }) {
  const states = Object.fromEntries(sides.map((side) => [side, { position: 0, seen: new Set(), ended: false }]));
  const sources = new Map();
  let timer = null;
  let stopped = false;
  let polling = false;

  function closeSource(side) {
    const source = sources.get(side);
    if (source) source.close();
    sources.delete(side);
  }

  function stop() {
    stopped = true;
    sides.forEach(closeSource);
    clearTimeout(timer);
  }

  function deliver(side, log) {
    const state = states[side];
    state.position = Math.max(state.position, log.next_after_seq);
    const fresh = log.entries.filter((entry) => !state.seen.has(entry.seq));
    fresh.forEach((entry) => state.seen.add(entry.seq));
    if (fresh.length) onEntries(side, fresh);
    if (fresh.some((entry) => entry.action === "final_result")) {
      state.ended = true;
      closeSource(side); // サーバーは end で閉じる。つなぎ直さないよう、こちらからも閉じる
    }
    if (!stopped && sides.every((name) => states[name].ended)) {
      stop();
      if (onEnd) onEnd();
    }
  }

  async function pollOnce(failures) {
    if (stopped) return;
    let nextFailures = 0;
    try {
      const logs = await poll(Object.fromEntries(sides.map((side) => [side, states[side].position])));
      sides.forEach((side) => {
        if (logs[side]) deliver(side, logs[side]);
      });
    } catch (error) {
      const transient = error instanceof ApiError && (error.status === 0 || error.status >= 500);
      if (!transient || failures >= 4) {
        stop();
        if (onError) onError(error);
        return;
      }
      nextFailures = failures + 1;
    }
    if (!stopped) timer = setTimeout(() => pollOnce(nextFailures), pollInterval);
  }

  function fallToPolling() {
    if (stopped || polling) return;
    polling = true;
    sides.forEach(closeSource);
    pollOnce(0);
  }

  function openStream(side) {
    const source = new EventSource(url(streamRoute, { path, query: { side, after_seq: 0 } }));
    sources.set(side, source);
    source.addEventListener("activity", (event) => {
      try {
        deliver(side, JSON.parse(event.data));
      } catch {
        fallToPolling();
      }
    });
    source.addEventListener("end", () => closeSource(side));
    source.addEventListener("problem", fallToPolling);
    source.addEventListener("error", () => {
      if (sources.get(side) === source && source.readyState === EventSource.CLOSED) fallToPolling();
    });
  }

  if (typeof EventSource === "undefined") fallToPolling();
  else sides.forEach(openStream);
  return { close: stop };
}
