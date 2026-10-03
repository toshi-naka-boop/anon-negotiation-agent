/* 面談(design.md §5 の 9 手順)。API は src/web/interview/api.py のとおり。
 *
 * - 面談の途中の答えは、サーバーのメモリにだけある。この画面は、続きから再開するために、サーバーの state.stage を見て手順を決める。
 * - 生の値(年収の回答・経験年数・自由コメント・辞めた理由)は、フォームの中にだけ置く。送信が済んだら、フォームを消す。
 *   ブラウザの保存領域には何も書かない(§1.2)。
 * - 手順 2・4・5 の AI への送信は、数秒かかるので、待ちの表示を出す。
 */

import { ApiError, call, ensureSession } from "./api.js";
import { AXIS_LABELS, clear, counterFor, h, message, replace, showError, showMessage, waiting, withBusy } from "./ui.js";

const STEPS = [
  { id: "profile", label: "プロフィール" },
  { id: "salary", label: "年収" },
  { id: "axes", label: "軸を外す" },
  { id: "choices", label: "二択" },
  { id: "reason", label: "辞めた理由" },
  { id: "confirm", label: "確認" },
  { id: "worst", label: "最悪ここまで" },
  { id: "blocklist", label: "ブロック先" },
  { id: "submit", label: "送信" },
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
const SOURCE_LABELS = { choice: "二択", comment: "自由コメント", reason: "辞めた理由" };

// pid: 依頼者 ID、texts: 設問の文面、state: 面談の進み具合(いずれも、サーバーから受け取った値。生の値は持たない)
const app = { pid: null, texts: null, state: null, current: null, furthest: 0, token: 0 };
const el = (id) => document.getElementById(id);
const stepIndex = (id) => STEPS.findIndex((step) => step.id === id);
const iv = (route, options = {}) => call(route, { ...options, path: { pid: app.pid, ...(options.path ?? {}) } });

function field(label, control, hint) {
  return h("label", { class: "field" }, h("span", { class: "label" }, label), control, hint ? h("span", { class: "hint" }, hint) : null);
}

function handleStepError(error) {
  const box = el("step-error");
  showError(box, error);
  if (error instanceof ApiError && error.detail === "interview_not_started") {
    box.appendChild(h("button", { class: "btn btn-small", type: "button", onclick: () => start(true, box) }, "面談を始め直す"));
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

/** 手順を開く。draw は、いまの手順の中身を描く関数(別の手順に移った後の遅れた応答で、上書きしないための札つき)。 */
function go(stepId) {
  app.current = stepId;
  app.furthest = Math.max(app.furthest, stepIndex(stepId));
  const token = ++app.token;
  const draw = (...nodes) => {
    if (token === app.token) replace(el("step"), ...nodes);
  };
  renderStepBar();
  showMessage(el("step-error"), null);
  draw(waiting("読み込んでいます…"));
  RENDERERS[stepId](draw).catch(handleStepError);
  el("wizard").scrollIntoView({ block: "start" });
}

async function start(restart, errorBox) {
  showMessage(errorBox, null);
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

// ---- 手順 1: プロフィール ----

function bandText(bands) {
  const profile = app.texts.profile;
  const experience = profile.experience_bands.find((band) => band.key === bands.experience_band)?.label ?? bands.experience_band;
  const region = profile.regions.find((item) => item.block === bands.region_block)?.label ?? bands.region_block;
  const job = profile.job_categories.find((item) => item.key === bands.job_category)?.label ?? bands.job_category;
  return `経験年数 ${experience} / 地域 ${region} / 職種 ${job}`;
}

async function renderProfile(draw) {
  const profile = app.texts.profile;
  const years = h("input", { type: "number", min: 0, max: 80, step: "any", required: true, autocomplete: "off" });
  const prefecture = h(
    "select",
    { required: true, autocomplete: "off" },
    h("option", { value: "" }, "選んでください"),
    profile.regions.map((region) =>
      h("optgroup", { label: region.label }, region.prefectures.map((name) => h("option", { value: name }, name))),
    ),
  );
  const job = h(
    "select",
    { required: true, autocomplete: "off" },
    h("option", { value: "" }, "選んでください"),
    profile.job_categories.map((item) => h("option", { value: item.key }, item.label)),
  );
  const submit = h("button", { class: "btn btn-primary", type: "submit" }, "次へ");
  const known = app.state.bands;
  const form = h(
    "form",
    {},
    h("h2", {}, "1. プロフィール"),
    h("p", {}, "経験年数・都道府県・職種を入力してください。入力した値は、すぐに「帯」(経験年数は区間、都道府県は地域ブロック)に変換して、正確な値は捨てます。"),
    field("経験年数(年)", years, "小数も入力できます(例: 7.5)"),
    field("お住まいの都道府県", prefecture),
    field("職種", job),
    known ? h("p", { class: "small muted" }, `登録済みの帯: ${bandText(known)}(正確な値は保存していません)`) : null,
    h("div", { class: "row" }, submit, known ? h("button", { class: "btn", type: "button", onclick: () => go("salary") }, "このまま次へ") : null),
  );
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const body = { experience_years: Number(years.value), prefecture: prefecture.value, job_category: job.value };
    withBusy(
      submit,
      async () => {
        app.state = await iv("POST /v1/principals/{pid}/interview/profile", { body });
        form.reset(); // 正確な値を、画面からも消す
        go("salary");
      },
      handleStepError,
    );
  });
  draw(form);
}

// ---- 手順 2: 年収の正規化 ----

function numberInput(value, { min, max, step = "any" } = {}) {
  return h("input", { type: "number", value: String(value), min, max, step, required: true, autocomplete: "off" });
}

function selectInput(options, selected) {
  return h(
    "select",
    { autocomplete: "off" },
    options.map(([value, label]) => h("option", { value, selected: value === selected }, label)),
  );
}

function drawSalaryAnswers(draw) {
  const questions = app.texts.salary_questions;
  const boxes = questions.map(() => h("textarea", { rows: 3, maxlength: 2000, required: true, autocomplete: "off" }));
  const status = h("div", { "aria-live": "polite" });
  const submit = h("button", { class: "btn btn-primary", type: "submit" }, "送信して読み取る");
  const confirmed = app.state.salary?.confirmed;
  const form = h(
    "form",
    {},
    h("h2", {}, "2. 年収"),
    h("p", {}, "3 つの質問に、自由に答えてください。AI が読み取って、比較基準年収(額面・賞与込み・固定残業代を除く)に換算します。"),
    confirmed ? message("info", "年収は確認済みです。変える場合は、回答を入力し直してください。") : null,
    questions.map((question, index) =>
      h("label", { class: "field" }, h("span", { class: "label" }, `${index + 1}. ${question}`), boxes[index], counterFor(boxes[index])),
    ),
    status,
    h("div", { class: "row" }, submit, confirmed ? h("button", { class: "btn", type: "button", onclick: () => go("axes") }, "このまま次へ") : null),
  );
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const answers = boxes.map((box) => box.value);
    withBusy(
      submit,
      async () => {
        replace(status, waiting("AI が回答を読み取っています…"));
        const proposal = await iv("POST /v1/principals/{pid}/interview/salary/answers", { body: { answers } });
        boxes.forEach((box) => {
          box.value = ""; // 送信した回答は、画面からも消す
        });
        drawSalaryProposal(draw, proposal, null);
      },
      (error) => {
        clear(status);
        handleStepError(error);
      },
    );
  });
  draw(form);
}

function drawSalaryProposal(draw, proposal, confirmed) {
  const basis = proposal.salary_basis;
  const amount = numberInput(basis.amount_man_yen, { min: 0.01, max: 100000 });
  const period = selectInput([["annual", "年額(1 年分)"], ["monthly", "月額(1 か月分)"]], basis.amount_period);
  const kind = selectInput([["gross", "額面(税引き前)"], ["net", "手取り"]], basis.amount_kind);
  const bonusMonths = numberInput(basis.bonus_months, { min: 0, max: 24, step: "0.5" });
  const overtime = numberInput(basis.fixed_overtime_man_yen_per_month, { min: 0, max: 1000 });
  const bonusIncluded = h("input", { type: "checkbox", checked: basis.bonus_included });
  const confirmButton = h("button", { class: "btn btn-primary", type: "submit" }, "この内容で確定する");
  const form = h(
    "form",
    {},
    h("h2", {}, "2. 年収: 換算の確認"),
    h("p", {}, "AI が次のように読み取りました。違っていれば、直してから確定してください。"),
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
    h("h3", {}, "換算の式と前提"),
    h("pre", {}, proposal.formula),
    h("ul", {}, proposal.assumptions.map((text) => h("li", {}, text))),
    confirmed ? message("ok", `確定しました: 比較基準年収 ${confirmed.normalized_man_yen} 万円`) : null,
    h(
      "div",
      { class: "row" },
      confirmButton,
      confirmed ? h("button", { class: "btn btn-primary", type: "button", onclick: () => go("axes") }, "次へ(軸を外す)") : null,
      h("button", { class: "btn btn-quiet", type: "button", onclick: () => drawSalaryAnswers(draw) }, "回答からやり直す"),
    ),
  );
  form.addEventListener("submit", (event) => {
    event.preventDefault();
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
        drawSalaryProposal(draw, result, result);
      },
      handleStepError,
    );
  });
  draw(form);
}

async function renderSalary(draw) {
  drawSalaryAnswers(draw);
}

// ---- 手順 3: 軸を外すかどうか ----

async function renderAxes(draw) {
  const data = await iv("GET /v1/principals/{pid}/interview/axes");
  const checks = data.axes.map((axis) => h("input", { type: "checkbox", checked: axis.removed }));
  const submit = h("button", { class: "btn btn-primary", type: "submit" }, "次へ");
  const form = h(
    "form",
    {},
    h("h2", {}, "3. 軸を外すかどうか"),
    h("p", {}, "年収以外の軸ごとに、条件を事前に答えるかを選びます。外した軸は、事前には答えず、交渉中に聞かれたときに答えます(途中確認)。"),
    data.axes.map((axis, index) =>
      h(
        "div",
        { class: "card" },
        h("h3", {}, axis.label),
        h("p", { class: "small muted" }, data.notice),
        h("label", { class: "check" }, checks[index], data.remove_label),
      ),
    ),
    h("div", { class: "row" }, submit),
  );
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const removed = data.axes.filter((_, index) => checks[index].checked).map((axis) => axis.axis);
    withBusy(
      submit,
      async () => {
        const result = await iv("POST /v1/principals/{pid}/interview/axes", { body: { removed_axes: removed } });
        app.state = result.state;
        go("choices");
        if (result.choices_reset) {
          showMessage(el("step-error"), "info", "外す軸を変えたので、二択の回答をやり直してください。");
        }
      },
      handleStepError,
    );
  });
  draw(form);
}

// ---- 手順 4: パッケージの二択・自由コメント ----

function optionBlock(pair, name, answers, update) {
  const option = pair.options[name];
  const feedback = h("div", { class: "small muted", "aria-live": "polite" });
  const choices = Object.entries(answers).map(([key, label]) => {
    const input = h("input", { type: "radio", name: `${pair.id}-${name}`, value: key, checked: option.answer === key });
    input.addEventListener("change", async () => {
      try {
        const result = await iv("POST /v1/principals/{pid}/interview/choices/answer", {
          body: { pair_id: pair.id, option: name, response: key },
        });
        update(result.answered, result.required);
        feedback.textContent = result.message ?? (key === "undecided" ? "「迷う」は、条件にしません。" : result.anchor_saved ? "条件として保存しました。" : "");
      } catch (error) {
        showError(feedback, error);
      }
    });
    return h("label", {}, input, label);
  });
  return h(
    "div",
    { class: "option" },
    h("strong", {}, name === "a" ? "A" : "B"),
    ` ${option.text}`,
    h(
      "div",
      {},
      h(
        "span",
        { class: "pkg" },
        option.axes.map((axis) =>
          h("span", { class: "chip" }, h("span", { class: "chip-label" }, AXIS_LABELS[axis.axis] ?? axis.label), axis.value),
        ),
      ),
    ),
    h("div", { class: "answers" }, choices),
    feedback,
  );
}

function readingResults(result) {
  const badge = (statement) =>
    statement.saved
      ? h("span", { class: "badge badge-ok" }, "条件にしました")
      : h("span", { class: "badge badge-warn" }, statement.message ?? "どの軸にも触れていないので、条件にしません");
  return h(
    "div",
    {},
    h("p", { class: "small" }, result.statements.length ? "次のように読み取りました。" : "条件として読み取れた発言はありませんでした。"),
    h("ul", {}, result.statements.map((statement) => h("li", {}, statement.sentence, " ", badge(statement)))),
    result.dropped ? h("p", { class: "small muted" }, `読み取れなかった発言が ${result.dropped} 件ありました。`) : null,
  );
}

/** 文章を AI に読み取らせる箱(自由コメント・辞めた理由)。送れたら、文章を画面から消す(失敗したときは、やり直せるよう残す)。 */
function statementBox({ title, prompt, route, submitLabel }) {
  const box = h("textarea", { rows: 4, maxlength: 6000, autocomplete: "off" });
  const results = h("div", { "aria-live": "polite" });
  const send = h("button", { class: "btn", type: "button" }, submitLabel);
  send.addEventListener("click", () => {
    const text = box.value;
    if (!text.trim()) {
      showMessage(results, "warn", "文章を入力してください。");
      return;
    }
    withBusy(
      send,
      async () => {
        replace(results, waiting("AI が読み取っています…"));
        const result = await iv(route, { body: { text } });
        box.value = ""; // 送った文章は、画面からも消す
        box.dispatchEvent(new Event("input")); // 文字数の表示も 0 に戻す
        replace(results, readingResults(result));
      },
      (error) => showError(results, error),
    );
  });
  return h(
    "div",
    { class: "stack" },
    h("h3", {}, title),
    h("p", {}, prompt),
    h("label", { class: "field" }, h("span", { class: "visually-hidden" }, title), box, counterFor(box)),
    h("div", { class: "row" }, send),
    results,
  );
}

async function renderChoices(draw) {
  const data = await iv("GET /v1/principals/{pid}/interview/choices");
  const progress = h("p", { class: "small", "aria-live": "polite" });
  const next = h("button", { class: "btn btn-primary", type: "button", onclick: () => go("reason") }, "次へ(辞めた理由)");
  const update = (answered, required) => {
    progress.textContent = `答えた組: ${answered} / 必要な組: ${required}(A と B の両方に答えた組を数えます)`;
    next.disabled = answered < required;
  };
  update(data.answered, data.required);
  draw(
    h("h2", {}, "4. パッケージの二択"),
    h("p", {}, data.intro),
    data.pairs.map((pair, index) =>
      h(
        "div",
        { class: "pair" },
        h("h3", {}, `質問 ${index + 1}`),
        h("p", {}, pair.question),
        optionBlock(pair, "a", data.answers, update),
        optionBlock(pair, "b", data.answers, update),
      ),
    ),
    progress,
    h("div", { class: "pair" }, statementBox({
      title: "自由コメント(任意)",
      prompt: app.texts.free_comment_prompt,
      route: "POST /v1/principals/{pid}/interview/comment",
      submitLabel: "コメントを送る",
    })),
    h("div", { class: "row" }, next),
  );
}

// ---- 手順 5: 辞めた理由 ----

async function renderReason(draw) {
  draw(
    h("h2", {}, "5. 辞めた理由(任意)"),
    statementBox({
      title: "辞めた理由",
      prompt: app.texts.reason_prompt,
      route: "POST /v1/principals/{pid}/interview/reason",
      submitLabel: "理由を送る",
    }),
    h("div", { class: "row" }, h("button", { class: "btn btn-primary", type: "button", onclick: () => go("confirm") }, "次へ(確認)")),
  );
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
      showError(errors, error);
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
      (error) => showError(errors, error),
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
        h("button", { class: "btn btn-small", type: "button", onclick: () => toggle(entry) }, entry.active ? "消す" : "付け直す"),
      ),
    );
    const proceed = h("button", { class: "btn btn-primary", type: "button", disabled: conflicted }, warning ? "そのまま進む" : "この内容で確認する");
    proceed.addEventListener("click", () => confirm(Boolean(warning), proceed));
    draw(
      h("h2", {}, "6. 平文での確認"),
      h("p", {}, "あなたの条件を、文章にして示します。違うものは「消す」、消したものは「付け直す」ことができます。"),
      data.removed_axes.length ? h("p", { class: "small" }, `外した軸: ${data.removed_axes.map((axis) => axis.label).join("、")}(交渉中に確認します)`) : null,
      rows.length ? h("div", {}, rows) : h("p", { class: "muted" }, "いまは、条件がありません。"),
      data.not_saved.length ? message("info", `${data.not_saved.length} 件は、外した軸に触れているので保存しません。${data.not_saved[0].message}`) : null,
      data.ignored_statements ? h("p", { class: "small muted" }, `どの軸にも触れていない発言が ${data.ignored_statements} 件あり、条件にしませんでした。`) : null,
      conflicted
        ? message("error", `条件に矛盾があります。どちらかを消してください: ${data.conflicts.map((c) => `「${c.accept_sentence}」と「${c.reject_sentence}」`).join("、")}`)
        : null,
      warning ? message("warn", warning.message) : null,
      errors,
      h(
        "div",
        { class: "row" },
        proceed,
        warning ? h("button", { class: "btn", type: "button", onclick: () => go("axes") }, "外すのをやめて、二択からやり直す") : null,
      ),
    );
  }
  render();
}

// ---- 手順 7: 最悪ここまで ----

function worstCell(axis) {
  if (axis.removed) return h("span", {}, axis.text);
  if (!axis.cells.length) return h("span", { class: "muted" }, "制約なし(何も分かりません)");
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
  const approve = h("button", { class: "btn btn-primary", type: "button" }, data.approved ? "承認済み。次へ" : "承認して次へ");
  approve.addEventListener("click", () =>
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
    h("h2", {}, "7. 最悪ここまで"),
    h("p", {}, "保存した条件から、最悪の場合に外から分かる範囲です(丸めた後のマス)。この範囲を承認すると、送信できます。"),
    h(
      "div",
      { class: "table-wrap" },
      h(
        "table",
        {},
        h("thead", {}, h("tr", {}, h("th", {}, "軸"), h("th", {}, "最悪の場合に、外から分かること"))),
        h("tbody", {}, data.axes.map((axis) => h("tr", {}, h("th", { scope: "row" }, axis.label), h("td", {}, worstCell(axis))))),
      ),
    ),
    h("div", { class: "row" }, approve),
  );
}

// ---- 手順 8: ブロック先 ----

async function renderBlocklist(draw) {
  const data = await iv("GET /v1/principals/{pid}/interview/companies");
  const chosen = new Set(app.state.blocklist ?? []);
  const checks = data.companies.map((company) => h("input", { type: "checkbox", value: company.company_id, checked: chosen.has(company.company_id) }));
  const submit = h("button", { class: "btn btn-primary", type: "submit" }, "次へ");
  const form = h(
    "form",
    {},
    h("h2", {}, "8. ブロック先"),
    h("p", {}, data.prompt),
    h("p", { class: "small muted" }, data.note),
    data.companies.map((company, index) => h("label", { class: "check" }, checks[index], company.company_name)),
    h("div", { class: "row" }, submit),
  );
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const companyIds = data.companies.filter((_, index) => checks[index].checked).map((company) => company.company_id);
    withBusy(
      submit,
      async () => {
        app.state = await iv("POST /v1/principals/{pid}/interview/blocklist", { body: { company_ids: companyIds } });
        go("submit");
      },
      handleStepError,
    );
  });
  draw(form);
}

// ---- 手順 9: 送信 ----

function finish() {
  app.state = null;
  app.texts = null; // 画面の変数からも消す
  clear(el("step"));
  clear(el("steps"));
  el("wizard").classList.add("hidden");
  el("done").classList.remove("hidden");
}

async function renderSubmit(draw) {
  const send = h("button", { class: "btn btn-primary", type: "button" }, "送信する");
  send.addEventListener("click", () =>
    withBusy(
      send,
      async () => {
        await iv("POST /v1/principals/{pid}/interview/submit");
        finish();
      },
      handleStepError,
    ),
  );
  draw(
    h("h2", {}, "9. 送信"),
    h("p", {}, "送信すると、条件をグリッドの一番近いマスに丸めて保存し、面談の途中の答えをサーバーのメモリから消します。このページの入力も消えます。"),
    h("p", { class: "small muted" }, "送信したあとも、「自分の交渉」から、保存したデータをすべて消せます。"),
    h("div", { class: "row" }, send),
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
  if (window.confirm("入力した内容を捨てて、最初からやり直しますか?")) start(true, el("step-error"));
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
    showMessage(el("intro-error"), "ok", "面談を破棄しました。");
  } catch (error) {
    handleStepError(error);
  }
});

showNotice();
