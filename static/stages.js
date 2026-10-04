/* 段階開示の区画(design.md §6.2・§7。FR-29〜33)。段 0 → 1 → 2 の表示と、候補者の「会う」「承認」。
 *
 * - API の経路は、このモジュールに書かない。呼ぶ口(loadStage・meet・approve)は、ページのスクリプトが渡す: /me は本人の経路、/demo は
 *   架空人物のデモ用の経路(デモの画面は、本物の依頼者の API を呼ばない。tests/test_ui_static.py が確かめる)。
 * - 表示するのは、サーバが決めた段の状態(StageView)だけ。求人側を操作する口は、この画面にない(求人側の「会う」「承認」は、架空の求人の
 *   自動応答。§6.2)。本物の候補者の段 2 は模擬表示で、連絡先を集めていない(P-2)ので、「ここで連絡先が開示されます」と見せるだけ。
 * - 匿名職務要約は、書いた本人の画面にだけ出す。送ったらフォームから消し、ブラウザの保存領域には何も書かない(§1.2)。
 * - 聞くイベント(document): negotiation:selected({nid})= 表示する交渉が決まった、negotiation:ended({nid})= 交渉が終わった(最終結果が
 *   届いた。段の状態を読み直す)、negotiation:cleared = 表示していた交渉がなくなった(データの削除・デモの実行の切り替え)。
 */

import { LIKELIHOOD_LABELS, clear, counterFor, h, message, packageChips, replace, resultView, showError, showMessage, waiting, withBusy } from "./ui.js";

// 段階開示で見せるものの種類(web.stages の Item)。開示台帳(ledger.js)も同じ言葉で書く。
export const ITEM_LABELS = { likelihood: "見込み", package: "組み合わせ", job_summary: "匿名職務要約", name: "氏名", email: "連絡先(メール)" };

// 匿名職務要約の上限(文字数)。サーバの [web.stages] job_summary_max_chars と同じ(暫定。tests/test_ui_static.py が一致を確かめる)。
export const JOB_SUMMARY_MAX_CHARS = 400;

const SIMULATED_NOTE = "模擬表示です。このシステムは、あなたの氏名・連絡先を集めていないので、求人側には何も渡りません。";

const STATE_BADGES = { done: ["開示済み", "ok"], current: ["いま、ここ", "info"], locked: ["まだ", ""] };

function flagBadge(pressed) {
  return h("span", { class: `badge${pressed ? " badge-ok" : ""}` }, pressed ? "押しました" : "まだ");
}

/** 「会う」「承認」を、側ごとに押したか。 */
function flagsLine(word, pair, candidateName) {
  return h("div", { class: "small" }, `「${word}」: ${candidateName} `, flagBadge(pair.candidate), " / 求人側 ", flagBadge(pair.employer));
}

/** デモ(架空人物)で、「会う」「承認」を誰が押すか。求人側に自動応答(フィクスチャの設定)がなければ、求人側は押されない。 */
function demoNote(view) {
  return view.employer_auto_response
    ? "架空の候補者・架空の求人が、フィクスチャの設定で、自動で押します。"
    : "架空の候補者は、フィクスチャの設定で、自動で押します。求人側には自動応答がないので、押されません。";
}

function stageItem(number, title, state, content) {
  const [label, kind] = STATE_BADGES[state];
  return h(
    "li",
    { class: `stage-step stage-${state}`, "aria-current": state === "current" ? "step" : null },
    h("div", { class: "stage-head" }, h("strong", {}, `段 ${number}: ${title}`), h("span", { class: `badge${kind ? ` badge-${kind}` : ""}` }, label)),
    h("div", { class: "stage-body" }, content),
  );
}

/** 求人側に開いたものを、囲んで見せる。text は、サーバが返した文字(textContent)。 */
function disclosureBox(title, text) {
  return h("div", { class: "disclosure-box" }, h("div", { class: "small muted" }, title), h("div", { class: "disclosure-text" }, text));
}

/**
 * 段階開示の区画を動かす。body・error はページの HTML にある要素。mode は own(本人。「会う」「承認」を押せる)か demo(架空人物。
 * 自動なので押す口がない)。loadStage(nid)・meet(nid, jobSummary)・approve(nid)は、StageView を返す(ページが API につなぐ)。onChange は、
 * 段の状態を読み込む・変える(開示台帳が変わり得る)たびに呼ぶ。emptyText は、表示する交渉がないときの案内。
 */
export function mountStages({ body, error }, { mode, loadStage, meet, approve, onChange, emptyText }) {
  const own = mode === "own";
  const candidateName = own ? "あなた" : "架空の候補者";
  const shown = { nid: null, token: 0 }; // token: 古い読み込みの結果を捨てるための札

  const changed = () => {
    if (onChange) onChange();
  };

  function showEmpty() {
    replace(body, h("p", { class: "empty-note" }, emptyText));
  }

  // ---- 表示 ----

  function identityLine(view) {
    const company = view.company ? (view.company.name ?? "非公開求人(会うと決めた後に企業名を開示)") : null;
    return h(
      "p",
      { class: "stage-identity" },
      company !== null ? [h("strong", {}, "求人: "), company, " "] : null,
      h("span", { class: "badge badge-info" }, view.employer_auto_response ? "架空の求人(自動応答)" : "架空の求人"),
      " ",
      h(
        "span",
        { class: "small muted" },
        view.employer_auto_response
          ? `求人側の「会う」「承認」は、フィクスチャの設定どおり、自動で押されます。${own ? "あなたが操作できるのは、候補者側(あなたの側)だけです。" : "架空の候補者の操作も、自動です。"}`
          : "この求人には自動応答がなく、求人側を操作する口も、この画面にはありません。段 1 以降は、求人側が「会う」を押すまで進みません。",
      ),
    );
  }

  function stage0(view) {
    return stageItem(0, "見込みと組み合わせ", "done", [
      h("div", {}, `見込み: ${LIKELIHOOD_LABELS[view.result.likelihood] ?? view.result.likelihood}`),
      view.result.package ? h("div", {}, packageChips(view.result.package)) : null,
      h("p", { class: "small muted" }, "交渉が合意で終わると、双方に自動で表示されます。求人側にも、同じものが見えています。"),
    ]);
  }

  function meetForm(view) {
    const summary = h("textarea", {
      rows: 4,
      maxlength: String(JOB_SUMMARY_MAX_CHARS),
      autocomplete: "off",
      "aria-describedby": "stages-summary-hint",
    });
    const button = h("button", { class: "btn btn-primary", type: "button", disabled: true }, "会う");
    summary.addEventListener("input", () => {
      button.disabled = summary.value.trim() === "";
    });
    button.addEventListener("click", () =>
      operate(button, async () => {
        const next = await meet(view.nid, summary.value.trim());
        summary.value = ""; // 送ったら、フォームから消す
        return next;
      }),
    );
    return h(
      "div",
      { class: "meet-form" },
      h(
        "label",
        { class: "field" },
        h("span", { class: "label" }, `匿名職務要約(${JOB_SUMMARY_MAX_CHARS} 文字まで)`),
        summary,
        counterFor(summary),
        h("span", { class: "hint", id: "stages-summary-hint" }, "氏名・勤務先・連絡先など、個人が特定できることは書かない(職務の内容・経験・得意なことを、一般的な言葉で)。"),
      ),
      h(
        "ul",
        { class: "small muted" },
        h("li", {}, "この要約は、この交渉の求人側の画面にだけ出ます(求人は架空なので、実際には誰にも表示されません)。AI(LLM)には渡しません。"),
        h("li", {}, "「会う」を押すと、書き直せません(最初に書いたものが残ります)。"),
        h("li", {}, "データを消したとき(または、最後に使ってから一定の期間がたったとき)に、段の状態ごと消えます。"),
      ),
      h("div", { class: "row" }, button),
    );
  }

  function stage1(view) {
    const open = view.stage >= 1;
    const content = [
      h("p", { class: "small" }, "双方が「会う」を押すと、あなたの匿名職務要約が、求人側に開きます。AI(LLM)には渡しません。"),
      flagsLine("会う", view.meet, candidateName),
    ];
    if (open) {
      content.push(disclosureBox("求人側に見えている職務要約", view.disclosed_to_employer.job_summary ?? "(要約は記録されていません)"));
    } else if (own && !view.meet.candidate) {
      content.push(meetForm(view));
    } else if (own) {
      content.push(h("p", { class: "small muted" }, "あなたは「会う」を押しました。求人側が押すのを待っています。"));
    } else {
      content.push(h("p", { class: "small muted" }, demoNote(view)));
    }
    return stageItem(1, "匿名職務要約(会う)", open ? "done" : "current", content);
  }

  function contactBox(disclosed) {
    if (disclosed.simulated) {
      return h("div", { class: "disclosure-box simulated" }, h("strong", {}, "ここで連絡先が開示されます"), h("p", { class: "small" }, SIMULATED_NOTE));
    }
    return h(
      "div",
      { class: "disclosure-box" },
      h("div", { class: "small muted" }, "求人側に見えている氏名と連絡先(フィクスチャの架空のもの)"),
      h("div", { class: "disclosure-text" }, `氏名: ${disclosed.name ?? "-"}`),
      h("div", { class: "disclosure-text" }, `連絡先(メール): ${disclosed.email ?? "-"}`),
    );
  }

  function approveBlock(view) {
    const button = h("button", { class: "btn btn-primary", type: "button" }, "承認する");
    button.addEventListener("click", () => {
      if (!window.confirm("承認すると、段 2 で、氏名と連絡先が求人側に開示されます(このシステムは連絡先を集めていないので、実際には模擬表示です)。承認しますか?")) return;
      operate(button, () => approve(view.nid));
    });
    return h(
      "div",
      {},
      h("p", { class: "small muted" }, `承認すると、段 2 で、氏名と連絡先が求人側に開示されます。${SIMULATED_NOTE}`),
      h("div", { class: "row" }, button),
    );
  }

  function stage2(view) {
    const done = view.stage === 2;
    const current = view.stage === 1;
    const content = [
      h("p", { class: "small" }, "双方が「承認」を押すと、氏名と連絡先が、求人側に開きます。"),
      flagsLine("承認", view.approve, candidateName),
    ];
    if (done) content.push(contactBox(view.disclosed_to_employer));
    else if (!current) content.push(h("p", { class: "small muted" }, "段 1 が開いてから、承認できます。"));
    else if (own && !view.approve.candidate) content.push(approveBlock(view));
    else if (own) content.push(h("p", { class: "small muted" }, "あなたは「承認」を押しました。求人側が押すのを待っています。"));
    else content.push(h("p", { class: "small muted" }, demoNote(view)));
    return stageItem(2, "氏名と連絡先(承認)", done ? "done" : current ? "current" : "locked", content);
  }

  function render(view) {
    const nodes = [identityLine(view)];
    if (!view.judged) {
      nodes.push(
        message("info", "この交渉は、まだ終わっていません。見込みは、交渉が終わったときに 1 回だけ出ます。それまでは、あなたにも求人側にも、見えていません。"),
      );
    } else {
      nodes.push(resultView(view.result));
      if (view.agreed) nodes.push(h("ol", { class: "stage-list" }, [stage0(view), stage1(view), stage2(view)]));
      else nodes.push(message("info", "合意できる組み合わせがなかったので、段 1 以降には進めません。求人側に見えているのも、「見込み: なし」だけです。"));
      nodes.push(h("p", { class: "small muted" }, `いま求人側に見えているもの: ${view.disclosed_to_employer.visible.map((item) => ITEM_LABELS[item] ?? item).join("・")}`));
    }
    replace(body, nodes);
  }

  // ---- 読み込みと操作 ----

  async function load(nid, token) {
    try {
      const view = await loadStage(nid);
      if (token !== shown.token) return;
      render(view);
    } catch (failure) {
      if (token !== shown.token) return;
      clear(body);
      showError(error, failure);
    }
    changed();
  }

  async function show(nid) {
    shown.nid = nid;
    shown.token += 1;
    showMessage(error, null);
    replace(body, waiting("段階開示の状態を読み込んでいます…"));
    await load(nid, shown.token);
  }

  async function reload(nid) {
    if (nid !== shown.nid) return;
    shown.token += 1;
    await load(nid, shown.token);
  }

  /** 「会う」「承認」の操作。成功したら、返ってきた状態を表示する。失敗したら理由を出し、状態が変わっていたときのために読み直す。 */
  async function operate(button, action) {
    const nid = shown.nid;
    await withBusy(
      button,
      async () => {
        showMessage(error, null);
        const view = await action();
        if (nid !== shown.nid) return;
        shown.token += 1;
        render(view);
        changed();
      },
      (failure) => {
        showError(error, failure);
        if (failure.status === 409) reload(nid);
      },
    );
  }

  document.addEventListener("negotiation:selected", (event) => show(event.detail.nid));
  document.addEventListener("negotiation:ended", (event) => reload(event.detail.nid));
  document.addEventListener("negotiation:cleared", () => {
    shown.nid = null;
    shown.token += 1;
    showMessage(error, null);
    showEmpty();
  });
  showEmpty();
  return { show, reload };
}
