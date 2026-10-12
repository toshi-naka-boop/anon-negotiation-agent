/* 面談(design.md §5 の 9 手順を、DESIGN.md §3・§4.2 の「1 問 1 画面」で見せる)。API は src/web/interview/api.py のとおり
 * (呼ぶ API と手順の順番は、作り替える前と同じ。年収の 3 問は 1 問ずつ聞いて、3 問目のあとにまとめて送る。二択は 1 組ずつ)。
 *
 * - 面談の途中の答えは、サーバーのメモリにだけある。この画面は、続きから再開するために、サーバーの state.stage を見て手順を決める。
 * - 生の値(年収の回答・経験年数・言い足りない条件・辞めた理由)は、入力欄と、送るまでの間の変数にだけ置く。送ったら消す。
 *   ブラウザの保存領域には何も書かない(§1.2)。
 * - AI に読み取らせる 3 か所(年収の 3 問・言い足りない条件・辞めた理由)は数秒〜最長 60 秒かかるので、待ちの箱を出す
 *   (「読み取っています。入力はそのまま残ります」。30 秒を超えたら「もう少しかかります」)。
 * - 画面の言葉(DESIGN.md §7・§8): 技術用語(グリッド・アンカー・ポリシー)は出さず、マス目・境目・あなたの条件、と言う。
 *   断定・評価・煽りの語は書かない。必須の印は付けず、例文を薄く(placeholder)出す。
 */

import { ApiError, call, ensureSession } from "./api.js";
import { SALARY_GRID, describeInterval } from "./meter.js";
import { AXIS_LABELS, clear, counterFor, h, message, mountVeil, replace, showError, showMessage, stateBox, withBusy } from "./ui.js";

const STEPS = [
  { id: "profile", label: "プロフィール" },
  { id: "salary", label: "年収" },
  { id: "axes", label: "飛ばす条件" },
  { id: "choices", label: "二択" },
  { id: "reason", label: "辞めた理由" },
  { id: "confirm", label: "確認" },
  { id: "worst", label: "最悪ここまで" },
  { id: "blocklist", label: "ブロック先" },
  { id: "submit", label: "預ける" },
];
// 面談 API の state.stage → 再開する手順
const STAGE_TO_STEP = {
  profile: "profile",
  salary: "salary",
  axes: "axes",
  choices: "choices",
  confirm: "confirm",
  worst_case: "worst",
  ready: "blocklist",
};
const SOURCE_LABELS = { choice: "二択", comment: "言い足りない条件", reason: "辞めた理由" };

// 例文(薄く出す。DESIGN.md §4.2)。年収の 3 問の順(fixtures/interview_templates.toml の salary_questions)に合わせる。
const SALARY_EXAMPLES = ["例: 額面で年 620 万くらい", "例: 固定残業代が月 3 万円入っています", "例: 賞与は年 2 か月分。上の金額には入っていません"];

// セッション切れ(DESIGN.md §4.2 のエラーの表)。api.js の MESSAGES より、この画面の文を優先する。
const SESSION_ERRORS = {
  interview_not_started: "1 時間使わなかったので、最初からになります。前回の答えは残っていません。",
};
// AI の読み取りが失敗したとき(DESIGN.md §8)。飛ばせる問いでは「そのまま進むか、言い直すか」、年収の 3 問では「言い直す」だけ。
function readErrors(canSkip) {
  const text = canSkip
    ? "AI の読み取りが一時的にできませんでした。そのまま進むか、言い直すか選べます。"
    : "AI の読み取りが一時的にできませんでした。言い直して、もう一度送れます。";
  return { ...SESSION_ERRORS, llm_unavailable: text, llm_failed: text, output_invalid: text, temporarily_unavailable: text };
}

// pid: 依頼者 ID、texts: 設問の文面、state: 面談の進み具合(いずれも、サーバーから受け取った値。生の値は持たない)
const app = { pid: null, texts: null, state: null, current: null, furthest: 0, token: 0 };
const el = (id) => document.getElementById(id);
const stepIndex = (id) => STEPS.findIndex((step) => step.id === id);
const iv = (route, options = {}) => call(route, { ...options, path: { pid: app.pid, ...(options.path ?? {}) } });

// ---- 1 画面の部品 ----

/**
 * 1 画面の骨(DESIGN.md §4.2・§9): 小さな前置き(eyebrow)・問い(18px・600)・補足・本文・状態の置き場・画面下部のアクション
 * (主要アクション 1 つ・飛ばすなどの副・戻る)。
 */
function screen({ eyebrow, question, hint, body = [], status, primary, secondary = [], back }) {
  return h(
    "div",
    { class: "screen" },
    eyebrow ? h("p", { class: "eyebrow" }, eyebrow) : null,
    h("p", { class: "question" }, question),
    hint ? h("p", { class: "small muted" }, hint) : null,
    body,
    status ?? null,
    h(
      "div",
      { class: "screen-actions" },
      primary,
      secondary,
      back ? h("button", { class: "btn btn-quiet", type: "button", onclick: back }, "戻る") : null,
    ),
  );
}

function primaryButton(label, onclick, props = {}) {
  return h("button", { class: "btn btn-primary btn-wide", type: "button", onclick, ...props }, label);
}

function quietButton(label, onclick) {
  return h("button", { class: "btn btn-quiet", type: "button", onclick }, label);
}

/** 入力欄 1 つ(問いは画面に出ているので、名前は読み上げ用に隠す)。 */
function inputField(name, control, ...extra) {
  return h("label", { class: "field" }, h("span", { class: "visually-hidden" }, name), control, extra);
}

/** 待ちの箱(DESIGN.md §4.2): 入力はそのまま残す。30 秒を超えたら「もう少しかかります」。返す関数で時計を止める(箱は、次の表示が置き換える)。 */
function startWaiting(container, title = "読み取っています。入力はそのまま残ります") {
  const box = stateBox("waiting", title);
  replace(container, box);
  const timer = setTimeout(() => box.appendChild(h("div", {}, "もう少しかかります")), 30000);
  return () => clearTimeout(timer);
}

function handleStepError(error) {
  const box = el("step-error");
  showError(box, error, SESSION_ERRORS);
  if (error instanceof ApiError && error.detail === "interview_not_started") {
    box.appendChild(h("button", { class: "btn", type: "button", onclick: () => start(true, box) }, "最初から始める"));
  }
}

function renderStepBar() {
  replace(
    el("steps"),
    STEPS.map((step, index) => {
      const reachable = index <= app.furthest;
      const current = step.id === app.current;
      return h(
        "li",
        {},
        h(
          "button",
          {
            type: "button",
            class: reachable && !current ? "done" : "",
            disabled: !reachable,
            "aria-current": current ? "step" : null,
            onclick: () => go(step.id),
          },
          step.label,
        ),
      );
    }),
  );
}

/** 手順を開く。draw は、いまの手順の画面を描く関数(別の手順に移った後の遅れた応答で、上書きしないための札つき。描くたびに、画面の上へ戻す)。 */
function go(stepId) {
  app.current = stepId;
  app.furthest = Math.max(app.furthest, stepIndex(stepId));
  const token = ++app.token;
  const draw = (...nodes) => {
    if (token !== app.token) return;
    replace(el("step"), ...nodes);
    el("wizard").scrollIntoView({ block: "start" });
  };
  renderStepBar();
  showMessage(el("step-error"), null);
  draw(stateBox("waiting", "読み込んでいます"));
  RENDERERS[stepId](draw).catch(handleStepError);
}

/** 最初の問いから「戻る」: 入口の注記に戻す(途中の答えはサーバーに残り、「面談を始める(続ける)」で同じ問いから再開する)。 */
function toIntro() {
  el("wizard").classList.add("hidden");
  el("intro").classList.remove("hidden");
  showMessage(el("intro-error"), null);
  el("intro").scrollIntoView({ block: "start" });
}

async function start(restart, errorBox) {
  replace(errorBox, stateBox("waiting", "準備しています"));
  try {
    const session = await ensureSession();
    if (!session.principal_id) {
      showMessage(errorBox, "error", "セッションを始められませんでした。ブラウザのクッキーが無効になっていないか、確かめてください。");
      return;
    }
    app.pid = session.principal_id;
    const begun = await iv("POST /v1/principals/{pid}/interview/begin", { body: { restart } });
    app.texts = begun.texts;
    app.state = begun.state;
    showMessage(errorBox, null);
    el("intro").classList.add("hidden");
    el("wizard").classList.remove("hidden");
    el("provisional-note").hidden = !app.texts.provisional;
    const step = STAGE_TO_STEP[app.state.stage];
    app.furthest = stepIndex(step); // 再開: ここまでの手順は、戻って見直せる
    go(step);
  } catch (error) {
    showError(errorBox, error);
  }
}

// ---- 手順 1: プロフィール(3 問。3 問目のあとに送る) ----

function bandText(bands) {
  const profile = app.texts.profile;
  const experience = profile.experience_bands.find((band) => band.key === bands.experience_band)?.label ?? bands.experience_band;
  const region = profile.regions.find((item) => item.block === bands.region_block)?.label ?? bands.region_block;
  const job = profile.job_categories.find((item) => item.key === bands.job_category)?.label ?? bands.job_category;
  return `経験年数 ${experience} / 地域 ${region} / 職種 ${job}`;
}

async function renderProfile(draw) {
  const profile = app.texts.profile;
  const known = app.state.bands;
  const answers = { years: "", prefecture: "", job: "" }; // 送るまでの間だけ持つ。送ったら消す

  function askYears() {
    const years = h("input", { type: "number", min: 0, max: 80, step: "any", autocomplete: "off", placeholder: "例: 7.5", value: answers.years });
    const status = h("div", { "aria-live": "polite" });
    draw(
      screen({
        eyebrow: "プロフィール 1 / 3",
        question: "いまの仕事の経験は、何年くらいですか",
        hint: "小数も書けます。すぐに「5〜10 年」のような幅に変えて、正確な年数は捨てます。",
        body: [
          inputField("経験年数(年)", years),
          known ? h("p", { class: "small muted" }, `前に答えた分: ${bandText(known)}(正確な値は保存していません)`) : null,
        ],
        status,
        primary: primaryButton("次へ", () => {
          if (years.value === "" || Number.isNaN(Number(years.value))) {
            showMessage(status, "warn", "年数を書くと進めます。ざっくりで大丈夫です。");
            return;
          }
          answers.years = years.value;
          askPrefecture();
        }),
        secondary: known ? [quietButton("このまま次へ", () => go("salary"))] : [],
        back: toIntro,
      }),
    );
  }

  function askPrefecture() {
    const prefecture = h(
      "select",
      { autocomplete: "off" },
      h("option", { value: "" }, "選ぶ"),
      profile.regions.map((region) =>
        h("optgroup", { label: region.label }, region.prefectures.map((name) => h("option", { value: name, selected: name === answers.prefecture }, name))),
      ),
    );
    const status = h("div", { "aria-live": "polite" });
    draw(
      screen({
        eyebrow: "プロフィール 2 / 3",
        question: "お住まいの都道府県はどこですか",
        hint: "地域の区分(関東など)だけに変えて、都道府県は捨てます。",
        body: [inputField("都道府県", prefecture)],
        status,
        primary: primaryButton("次へ", () => {
          if (!prefecture.value) {
            showMessage(status, "warn", "都道府県を選ぶと進めます。");
            return;
          }
          answers.prefecture = prefecture.value;
          askJob();
        }),
        back: askYears,
      }),
    );
  }

  function askJob() {
    const job = h(
      "select",
      { autocomplete: "off" },
      h("option", { value: "" }, "選ぶ"),
      profile.job_categories.map((item) => h("option", { value: item.key, selected: item.key === answers.job }, item.label)),
    );
    const status = h("div", { "aria-live": "polite" });
    const next = primaryButton("次へ", () => {
      if (!job.value) {
        showMessage(status, "warn", "職種を選ぶと進めます。");
        return;
      }
      answers.job = job.value;
      const body = { experience_years: Number(answers.years), prefecture: answers.prefecture, job_category: answers.job };
      withBusy(
        next,
        async () => {
          app.state = await iv("POST /v1/principals/{pid}/interview/profile", { body });
          answers.years = ""; // 正確な値を、画面の変数からも消す
          answers.prefecture = "";
          answers.job = "";
          go("salary");
        },
        handleStepError,
      );
    });
    draw(
      screen({
        eyebrow: "プロフィール 3 / 3",
        question: "職種は、どれに近いですか",
        hint: "大まかな区分だけを覚えます。",
        body: [inputField("職種", job)],
        status,
        primary: next,
        back: askPrefecture,
      }),
    );
  }

  askYears();
}

// ---- 手順 2: 年収の 3 問(1 問ずつ) → 読み取りの確認 → 価値 1「相手に伝わり得るのはここまで」 ----

function numberInput(value, { min, max, step = "any" } = {}) {
  return h("input", { type: "number", value: String(value), min, max, step, autocomplete: "off" });
}

function selectInput(options, selected) {
  return h(
    "select",
    { autocomplete: "off" },
    options.map(([value, label]) => h("option", { value, selected: value === selected }, label)),
  );
}

/**
 * 価値 1(DESIGN.md §3): 比べるための年収を 50 万円刻みのマス目に丸めたときの、相手に伝わり得るマス。
 * 受ける側の丸め(上のマス目へ。anchors.py の _salary_cell と同じ区間 (下の点, 上の点])を、画面で写した SALARY_GRID で計算する。
 */
function salaryCellText(manYen) {
  const { low, high, step } = SALARY_GRID;
  const upper = Math.min(high, Math.max(low, Math.ceil(manYen / step) * step));
  return describeInterval({ lower: upper > low ? upper - step : null, upper });
}

function reachBox(manYen) {
  return h(
    "div",
    { class: "card reach" },
    h("p", { class: "small muted" }, "相手に伝わり得るのは、ここまでです"),
    h("p", { class: "figure" }, salaryCellText(manYen)),
    h("p", { class: "small" }, "書いた金額そのものは渡りません。相手に分かるのは、最大でもこのマス目 1 つぶんです。"),
  );
}

async function renderSalary(draw) {
  const questions = app.texts.salary_questions;
  const answers = questions.map(() => ""); // 3 問目のあとにまとめて送る。送ったら消す
  const confirmed = app.state.salary?.confirmed;

  function ask(index) {
    const box = h("textarea", { rows: 3, maxlength: 2000, autocomplete: "off", placeholder: SALARY_EXAMPLES[index] ?? "", value: answers[index] });
    const status = h("div", { "aria-live": "polite" });
    const last = index === questions.length - 1;
    const next = primaryButton(last ? "読み取ってもらう" : "次へ", () => {
      answers[index] = box.value;
      if (!box.value.trim()) {
        showMessage(status, "warn", "ざっくりで大丈夫です。書くと進めます。");
        return;
      }
      if (!last) {
        ask(index + 1);
        return;
      }
      withBusy(next, async () => {
        const stop = startWaiting(status);
        try {
          const proposal = await iv("POST /v1/principals/{pid}/interview/salary/answers", { body: { answers: [...answers] } });
          answers.fill(""); // 送った回答は、画面の変数からも消す(画面は、次の表示が置き換える)
          showProposal(proposal, null);
        } catch (error) {
          showError(status, error, readErrors(false));
        } finally {
          stop();
        }
      });
    });
    draw(
      screen({
        eyebrow: `年収 ${index + 1} / ${questions.length}`,
        question: questions[index],
        hint: index === 0 ? "3 問に答えると、AI が読み取って、比べるための年収に直します。書いた文章そのものは保存しません。" : null,
        body: [
          inputField(questions[index], box, counterFor(box)),
          confirmed && index === 0 ? h("p", { class: "small muted" }, "年収は確かめ済みです。変えないなら、このまま次へ進めます。") : null,
        ],
        status,
        primary: next,
        secondary: confirmed && index === 0 ? [quietButton("このまま次へ", () => go("axes"))] : [],
        back: () => (index === 0 ? go("profile") : ask(index - 1)),
      }),
    );
  }

  /** 読み取りの結果。confirmedResult があれば、確かめたあとの画面(価値 1 と「次へ」)。なければ、直せる表と「これで合っています」。 */
  function showProposal(proposal, confirmedResult) {
    const status = h("div", { "aria-live": "polite" });
    if (confirmedResult) {
      draw(
        screen({
          eyebrow: "年収",
          question: `確かめました。比べるための年収は ${confirmedResult.normalized_man_yen} 万円です`,
          hint: "額面・賞与込み・固定残業代を除いた年額です。この年収を土台に、次の問いに進みます。",
          body: [reachBox(confirmedResult.normalized_man_yen)],
          status,
          primary: primaryButton("次へ", () => go("axes")),
          secondary: [quietButton("直す", () => showProposal(confirmedResult, null))],
          back: () => ask(questions.length - 1),
        }),
      );
      return;
    }
    const basis = proposal.salary_basis;
    const amount = numberInput(basis.amount_man_yen, { min: 0.01, max: 100000 });
    const period = selectInput([["annual", "年額(1 年分)"], ["monthly", "月額(1 か月分)"]], basis.amount_period);
    const kind = selectInput([["gross", "額面(税引き前)"], ["net", "手取り"]], basis.amount_kind);
    const bonusMonths = numberInput(basis.bonus_months, { min: 0, max: 24, step: "0.5" });
    const overtime = numberInput(basis.fixed_overtime_man_yen_per_month, { min: 0, max: 1000 });
    const bonusIncluded = h("input", { type: "checkbox", checked: basis.bonus_included });
    const field = (label, control) => h("label", { class: "field" }, h("span", { class: "label" }, label), control);
    const confirmButton = primaryButton("これで合っています", () => {
      const salaryBasis = {
        amount_man_yen: Number(amount.value),
        amount_period: period.value,
        amount_kind: kind.value,
        bonus_included: bonusIncluded.checked,
        bonus_months: Number(bonusMonths.value),
        fixed_overtime_man_yen_per_month: Number(overtime.value),
      };
      withBusy(
        confirmButton,
        async () => {
          const result = await iv("POST /v1/principals/{pid}/interview/salary/confirm", { body: { salary_basis: salaryBasis } });
          app.state = result.state;
          showProposal(result, result);
        },
        (error) => showError(status, error, SESSION_ERRORS),
      );
    });
    draw(
      screen({
        eyebrow: "年収",
        question: "次のように読み取りました。これで合っていますか",
        hint: "違っていれば、ここで直せます。",
        body: [
          h(
            "div",
            { class: "grid cols-2" },
            field("金額(万円)", amount),
            field("年額か月額か", period),
            field("額面か手取りか", kind),
            field("賞与の月数(月給の何か月分か)", bonusMonths),
            field("固定残業代(月額、万円)", overtime),
            h("label", { class: "check" }, bonusIncluded, "この金額には、賞与がすでに含まれている"),
          ),
          h(
            "details",
            {},
            h("summary", {}, `比べるための年収: ${proposal.normalized_man_yen} 万円(計算の式と前提を見る)`),
            h("pre", {}, proposal.formula),
            h("ul", {}, proposal.assumptions.map((text) => h("li", {}, text))),
          ),
          reachBox(proposal.normalized_man_yen),
        ],
        status,
        primary: confirmButton,
        secondary: [quietButton("言い直す", () => ask(0))],
        back: () => ask(questions.length - 1),
      }),
    );
  }

  ask(0);
}

// ---- 手順 3: 飛ばす条件(仕組みは「軸を外す」そのもの。design.md §2.4・§5 の 3) ----

async function renderAxes(draw) {
  const data = await iv("GET /v1/principals/{pid}/interview/axes");
  const checks = data.axes.map((axis) => h("input", { type: "checkbox", checked: axis.removed }));
  const next = primaryButton("次へ", () => {
    const removed = data.axes.filter((_, index) => checks[index].checked).map((axis) => axis.axis);
    withBusy(
      next,
      async () => {
        const result = await iv("POST /v1/principals/{pid}/interview/axes", { body: { removed_axes: removed } });
        app.state = result.state;
        go("choices");
        if (result.choices_reset) {
          showMessage(el("step-error"), "info", "飛ばす条件を変えたので、二択を答え直します。");
        }
      },
      handleStepError,
    );
  });
  draw(
    screen({
      eyebrow: "飛ばす条件",
      question: "答えたくない条件はありますか。飛ばす条件に印を付けてください",
      hint: "印を付けなくても進めます。年収は飛ばせません。",
      body: [
        data.axes.map((axis, index) =>
          h("label", { class: "check" }, checks[index], h("span", {}, `${AXIS_LABELS[axis.axis] ?? axis.label} を飛ばす`)),
        ),
        h("p", { class: "small" }, "飛ばした条件は相手に先に知られません。代わりに交渉中に聞かれることがあります。"),
      ],
      primary: next,
      back: () => go("salary"),
    }),
  );
}

// ---- 手順 4: 二択(1 組ずつ) → 言い足りない条件(任意) ----

/** 二択の片方(A か B)。行く・行かない・迷う は同じ大きさ・同じ色の重みで並べ、押した答えだけを塗る。答えは、押したその場で送る。 */
function optionCard(pair, name, labels, onAnswered) {
  const option = pair.options[name];
  const title = name === "a" ? "A" : "B";
  const feedback = h("div", { class: "small muted", "aria-live": "polite" });
  const buttons = Object.entries(labels).map(([key, label]) => {
    const button = h("button", { class: "btn", type: "button", "aria-pressed": option.answer === key ? "true" : "false" }, label);
    button.addEventListener("click", () =>
      withBusy(
        button,
        async () => {
          const result = await iv("POST /v1/principals/{pid}/interview/choices/answer", {
            body: { pair_id: pair.id, option: name, response: key },
          });
          option.answer = key;
          buttons.forEach((other) => other.setAttribute("aria-pressed", other === button ? "true" : "false"));
          feedback.textContent = result.message ?? (key === "undecided" ? "「迷う」は、条件にしません。" : result.anchor_saved ? "条件にしました。" : "");
          onAnswered(result.answered, result.required);
        },
        (error) => showError(feedback, error, SESSION_ERRORS),
      ),
    );
    return button;
  });
  return h(
    "div",
    { class: "option" },
    h("p", {}, h("strong", {}, title), ` ${option.text}`),
    h(
      "div",
      {},
      h(
        "span",
        { class: "pkg" },
        option.axes.map((axis) => h("span", { class: "chip" }, h("span", { class: "chip-label" }, AXIS_LABELS[axis.axis] ?? axis.label), axis.value)),
      ),
    ),
    h("div", { class: "answers-3", role: "group", "aria-label": `${title} への答え` }, buttons),
    feedback,
  );
}

function readingResults(result) {
  const badge = (statement) =>
    statement.saved
      ? h("span", { class: "badge badge-ok" }, "条件にしました")
      : h("span", { class: "badge badge-warn" }, statement.message ?? "どの条件にも触れていないので、条件にしません");
  return h(
    "div",
    {},
    h("p", { class: "small" }, result.statements.length ? "次のように読み取りました。" : "条件として読み取れた文はありませんでした。"),
    h("ul", {}, result.statements.map((statement) => h("li", {}, statement.sentence, " ", badge(statement)))),
    result.dropped ? h("p", { class: "small muted" }, `読み取れなかった文が ${result.dropped} 件ありました。`) : null,
  );
}

/**
 * 文章を AI に読み取らせる画面(言い足りない条件・辞めた理由。どちらも飛ばせる)。読み取れたら文章を消して結果を並べ、主要アクションを「次へ」にする。
 * 失敗したときは、文章を残したまま「そのまま進むか、言い直すか」。
 */
function statementScreen(draw, { eyebrow, question, hint, placeholder, route, next, back }) {
  const box = h("textarea", { rows: 4, maxlength: 6000, autocomplete: "off", placeholder });
  const counter = counterFor(box);
  const status = h("div", { "aria-live": "polite" });
  const results = h("div", { "aria-live": "polite" });
  const read = h("button", { type: "button" }, "読み取ってもらう");
  let readings = 0;

  function render() {
    read.className = readings ? "btn btn-wide" : "btn btn-primary btn-wide";
    read.textContent = readings ? "この文章も読み取ってもらう" : "読み取ってもらう";
    draw(
      screen({
        eyebrow,
        question,
        hint,
        body: [inputField(question, box, counter), results],
        status,
        primary: readings ? primaryButton("次へ", next) : read,
        secondary: readings ? [read] : [quietButton("飛ばす", next)],
        back,
      }),
    );
  }

  read.addEventListener("click", () => {
    const text = box.value;
    if (!text.trim()) {
      showMessage(status, "warn", "書くと読み取れます。書かないなら、飛ばせます。");
      return;
    }
    withBusy(read, async () => {
      const stop = startWaiting(status);
      try {
        const result = await iv(route, { body: { text } });
        box.value = ""; // 送った文章は、画面からも消す
        box.dispatchEvent(new Event("input")); // 文字数の表示も 0 に戻す
        readings += 1;
        clear(status);
        replace(results, readingResults(result));
        render();
      } catch (error) {
        showError(status, error, readErrors(true));
      } finally {
        stop();
      }
    });
  });

  render();
}

async function renderChoices(draw) {
  const data = await iv("GET /v1/principals/{pid}/interview/choices");
  let answered = data.answered;
  const required = data.required;
  const complete = (pair) => Boolean(pair.options.a.answer && pair.options.b.answer);

  function showPair(index, note) {
    const pair = data.pairs[index];
    const progress = h("p", { class: "small muted", "aria-live": "polite" });
    const update = () => {
      progress.textContent = `A と B の両方に答えた組: ${answered} / ${required}`;
    };
    update();
    const next = primaryButton("次へ", () => {
      if (index + 1 < data.pairs.length) {
        showPair(index + 1);
        return;
      }
      if (answered < required) {
        const first = data.pairs.findIndex((item) => !complete(item));
        showPair(first < 0 ? index : first, `A と B の両方に答えた組が ${required} 組になると、次に進めます。あと ${required - answered} 組です。`);
        return;
      }
      showComment();
    });
    draw(
      screen({
        eyebrow: `二択 ${index + 1} / ${data.pairs.length}`,
        question: pair.question,
        hint: index === 0 ? data.intro : null,
        body: [
          note ? message("info", note) : null,
          optionCard(pair, "a", data.answers, (count) => {
            answered = count;
            update();
          }),
          optionCard(pair, "b", data.answers, (count) => {
            answered = count;
            update();
          }),
          progress,
        ],
        primary: next,
        back: () => (index === 0 ? go("axes") : showPair(index - 1)),
      }),
    );
  }

  function showComment() {
    statementScreen(draw, {
      eyebrow: "二択のあとに",
      question: "言い足りない条件はありますか(任意)",
      hint: app.texts.free_comment_prompt,
      placeholder: "例: 当直が月 4 回以上なら行かない",
      route: "POST /v1/principals/{pid}/interview/comment",
      next: () => go("reason"),
      back: () => showPair(data.pairs.length - 1),
    });
  }

  if (!data.pairs.length) {
    draw(screen({ question: "二択を出せませんでした", body: [stateBox("empty", "年収を確かめると、二択が出ます")], primary: primaryButton("年収へ", () => go("salary")), back: () => go("axes") }));
    return;
  }
  showPair(0);
}

// ---- 手順 5: 辞めた理由(任意) ----

async function renderReason(draw) {
  statementScreen(draw, {
    eyebrow: "辞めた理由",
    question: "いまの仕事を辞めたい(辞めた)理由はありますか(任意)",
    hint: app.texts.reason_prompt,
    placeholder: "例: 当直が月 6 回あって、体がもたなかった",
    route: "POST /v1/principals/{pid}/interview/reason",
    next: () => go("confirm"),
    back: () => go("choices"),
  });
}

// ---- 手順 6: 平文での確認 ----

function polarityBadge(polarity) {
  return polarity === "accept"
    ? h("span", { class: "badge badge-ok" }, "受ける条件")
    : h("span", { class: "badge badge-danger" }, "受けない条件");
}

async function renderConfirm(draw) {
  let data = await iv("GET /v1/principals/{pid}/interview/confirmation");
  const errors = h("div", { "aria-live": "polite" });

  async function toggle(entry) {
    clear(errors);
    try {
      data = await iv("POST /v1/principals/{pid}/interview/anchors/{key}/active", {
        path: { key: entry.key },
        body: { active: !entry.active },
      });
      render();
    } catch (error) {
      showError(errors, error, SESSION_ERRORS);
    }
  }

  function confirm(proceed, button) {
    clear(errors);
    withBusy(
      button,
      async () => {
        app.state = await iv("POST /v1/principals/{pid}/interview/confirm", { body: { proceed_without_accept_anchors: proceed } });
        go("worst");
      },
      (error) => showError(errors, error, SESSION_ERRORS),
    );
  }

  function render() {
    const warning = data.warnings.find((item) => item.code === "no_accept_anchors");
    const conflicted = data.conflicts.length > 0;
    const rows = data.entries.map((entry) =>
      h(
        "div",
        { class: `entry-row${entry.active ? "" : " inactive"}` },
        polarityBadge(entry.polarity),
        h("span", { class: "sentence" }, entry.sentence),
        h("span", { class: "badge" }, SOURCE_LABELS[entry.source] ?? entry.source),
        h("button", { class: "btn", type: "button", onclick: () => toggle(entry) }, entry.active ? "消す" : "付け直す"),
      ),
    );
    const proceed = primaryButton(warning ? "そのまま進む" : "この内容で進む", () => confirm(Boolean(warning), proceed), { disabled: conflicted });
    const retry = warning
      ? warning.caused_by_removed_axes
        ? quietButton("飛ばすのをやめて、二択からやり直す", () => go("axes"))
        : quietButton("二択に戻る", () => go("choices"))
      : null;
    draw(
      screen({
        eyebrow: "確認",
        question: "あなたの条件は、この文章のとおりですか",
        hint: "違うものは「消す」、消したものは「付け直す」ことができます。",
        body: [
          data.removed_axes.length
            ? h("p", { class: "small" }, `飛ばした条件: ${data.removed_axes.map((axis) => AXIS_LABELS[axis.axis] ?? axis.label).join("、")}(交渉中に聞かれたら答えます)`)
            : null,
          rows.length ? h("div", {}, rows) : stateBox("empty", "まだ条件がありません", "二択で「行く」「行かない」を選ぶか、言い足りない条件を書くと、ここに並びます。"),
          data.not_saved.length ? message("info", `${data.not_saved.length} 件は、飛ばした条件に触れているので保存しません。${data.not_saved[0].message}`) : null,
          data.ignored_statements ? h("p", { class: "small muted" }, `どの条件にも触れていない文が ${data.ignored_statements} 件あり、条件にしませんでした。`) : null,
          conflicted
            ? message("error", `条件がぶつかっています。どちらかを消すと進めます: ${data.conflicts.map((c) => `「${c.accept_sentence}」と「${c.reject_sentence}」`).join("、")}`)
            : null,
          warning ? message("warn", warning.message) : null,
        ],
        status: errors,
        primary: proceed,
        secondary: retry ? [retry] : [],
        back: () => go("reason"),
      }),
    );
  }
  render();
}

// ---- 手順 7: 最悪ここまで ----

function worstCell(axis) {
  if (axis.removed) return h("span", {}, axis.text);
  if (!axis.cells.length) return h("span", { class: "muted" }, "何も分かりません(この条件には触れていません)");
  return h(
    "div",
    {},
    axis.cells.map((cell) =>
      h(
        "div",
        {},
        h("span", { class: `badge badge-${cell.kind === "accept" ? "ok" : "danger"}` }, cell.kind_label),
        ` ${cell.text}`,
      ),
    ),
    h("div", { class: "small muted" }, axis.note ?? ""),
  );
}

async function renderWorst(draw) {
  const data = await iv("GET /v1/principals/{pid}/interview/worst-case");
  const approve = primaryButton(data.approved ? "次へ" : "この範囲で進む", () =>
    withBusy(
      approve,
      async () => {
        if (!data.approved) app.state = await iv("POST /v1/principals/{pid}/interview/worst-case/approve");
        go("blocklist");
      },
      handleStepError,
    ),
  );
  draw(
    screen({
      eyebrow: "最悪ここまで",
      question: "最悪の場合でも、相手に分かるのはここまでです",
      hint: "あなたの条件を、マス目に丸めた後の形です。マス目の中のどこかは、相手には分かりません。飛ばした条件は、交渉中に聞かれたら答えます。",
      body: [h("dl", { class: "worst-list" }, data.axes.map((axis) => [h("dt", {}, AXIS_LABELS[axis.axis] ?? axis.label), h("dd", {}, worstCell(axis))]))],
      primary: approve,
      back: () => go("confirm"),
    }),
  );
}

// ---- 手順 8: ブロック先(任意) ----

async function renderBlocklist(draw) {
  const data = await iv("GET /v1/principals/{pid}/interview/companies");
  const chosen = new Set(app.state.blocklist ?? []);
  const checks = data.companies.map((company) => h("input", { type: "checkbox", value: company.company_id, checked: chosen.has(company.company_id) }));
  const save = async (companyIds) => {
    app.state = await iv("POST /v1/principals/{pid}/interview/blocklist", { body: { company_ids: companyIds } });
    go("submit");
  };
  const next = primaryButton("次へ", () =>
    withBusy(next, () => save(data.companies.filter((_, index) => checks[index].checked).map((company) => company.company_id)), handleStepError),
  );
  const skip = quietButton("飛ばす(選ばずに進む)", () => withBusy(skip, () => save([]), handleStepError));
  draw(
    screen({
      eyebrow: "ブロック先",
      question: "いまの勤め先を、相手から外しますか",
      hint: data.prompt,
      body: [
        data.companies.map((company, index) => h("label", { class: "check" }, checks[index], company.company_name)),
        h("p", { class: "small muted" }, data.note),
      ],
      primary: next,
      secondary: [skip],
      back: () => go("worst"),
    }),
  );
}

// ---- 手順 9: 預ける ----

function finish() {
  app.state = null;
  app.texts = null; // 画面の変数からも消す
  clear(el("step"));
  clear(el("steps"));
  el("wizard").classList.add("hidden");
  el("done").classList.remove("hidden");
  el("done").scrollIntoView({ block: "start" });
}

async function renderSubmit(draw) {
  const status = h("div", { "aria-live": "polite" });
  const send = primaryButton("預ける", () =>
    withBusy(send, async () => {
      const stop = startWaiting(status, "預けています");
      try {
        await iv("POST /v1/principals/{pid}/interview/submit");
        finish();
      } catch (error) {
        clear(status);
        handleStepError(error);
      } finally {
        stop();
      }
    }),
  );
  draw(
    screen({
      eyebrow: "預ける",
      question: "この条件を預けますか",
      body: [
        h("p", {}, "預けると、条件をマス目に丸めた形で金庫に保存します。書いた本音と面談の記録は捨てます。相手に分かるのはマス目までです。"),
        h("p", { class: "small muted" }, "預けたあとも、「自分の交渉」から、保存したデータをすべて消せます。"),
      ],
      status,
      primary: send,
      back: () => go("blocklist"),
    }),
  );
}

// ---- 配線 ----

const RENDERERS = {
  profile: renderProfile,
  salary: renderSalary,
  axes: renderAxes,
  choices: renderChoices,
  reason: renderReason,
  confirm: renderConfirm,
  worst: renderWorst,
  blocklist: renderBlocklist,
  submit: renderSubmit,
};

async function showNotice() {
  try {
    const notice = await call("GET /v1/interview/notice");
    el("notice-title").textContent = notice.title;
    replace(el("notice-body"), h("ul", {}, notice.items.map((item) => h("li", { dataset: { id: item.id } }, item.text))));
  } catch (error) {
    showError(el("notice-body"), error);
  }
}

el("start-button").addEventListener("click", (event) => withBusy(event.currentTarget, () => start(false, el("intro-error"))));

el("restart-button").addEventListener("click", () => {
  if (window.confirm("ここまでの答えを捨てて、最初からやり直しますか?")) start(true, el("step-error"));
});

el("discard-button").addEventListener("click", async () => {
  if (!window.confirm("面談を破棄しますか?(途中の答えは、サーバーのメモリからも消えます)")) return;
  try {
    await iv("POST /v1/principals/{pid}/interview/discard");
    app.state = null;
    app.texts = null;
    clear(el("step"));
    el("wizard").classList.add("hidden");
    el("intro").classList.remove("hidden");
    showMessage(el("intro-error"), "ok", "面談を破棄しました。途中の答えは消えました。");
  } catch (error) {
    handleStepError(error);
  }
});

showNotice();

mountVeil();
