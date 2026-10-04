/* 画面から API を呼ぶ薄い包み(fetch)。
 *
 * - 呼ぶ API は、"METHOD /path/{param}" の形の文字列で書く(例: "GET /v1/jobs")。tests/test_ui_static.py が、この形の文字列を
 *   集めて、実在する経路と照らし合わせる(経路の打ち間違いを、ブラウザなしで見つけるため)。
 * - 状態を変える POST には、独自ヘッダ X-Requested-With を付ける(ミドルウェアが必須にしている。§6.3)。
 * - クッキー(HttpOnly)は、同じ配信元にだけ送る(credentials: "same-origin")。JS からは読み書きしない。
 * - API のエラーは ApiError にして、describeError が画面向けの日本語にする(401・403・409・413・422・429 の 2 つの形ほか)。
 */

export const REQUESTED_WITH = "anon-negotiation";

export class ApiError extends Error {
  constructor(status, detail, body) {
    super(typeof detail === "string" ? detail : `http ${status}`);
    this.name = "ApiError";
    this.status = status; // 0 は、サーバーに届かなかった
    this.detail = detail; // 理由の名前(文字列)・429 の rate_limited(オブジェクト)・422 の検証エラー(配列)・なければ null
    this.body = body; // 応答の本文(JSON ならオブジェクト)
  }
}

/** "METHOD /path/{param}" と {path, query} から、URL を作る(EventSource にも使う)。 */
export function url(route, { path = {}, query = {} } = {}) {
  let target = route.slice(route.indexOf(" ") + 1);
  target = target.replace(/\{(\w+)\}/g, (_, name) => {
    if (!(name in path)) throw new Error(`missing path parameter: ${name}`);
    return encodeURIComponent(String(path[name]));
  });
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null) params.set(key, String(value));
  }
  const queryString = params.toString();
  return queryString ? `${target}?${queryString}` : target;
}

async function readBody(response) {
  const type = response.headers.get("content-type") || "";
  if (type.startsWith("application/json")) {
    try {
      return await response.json();
    } catch {
      return null;
    }
  }
  return response.text();
}

/**
 * API を 1 回呼ぶ。成功なら本文(JSON ならオブジェクト、それ以外は文字列)、失敗なら ApiError を投げる。
 * body は JSON にして送る。text は、そのままの文字列を JSON として送る(壁 1 の生のメッセージ)。
 */
export async function call(route, { path, query, body, text, signal } = {}) {
  const method = route.slice(0, route.indexOf(" "));
  const headers = { Accept: "application/json" };
  const init = { method, headers, credentials: "same-origin", cache: "no-store", signal };
  if (method !== "GET") headers["X-Requested-With"] = REQUESTED_WITH;
  if (text !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = text;
  } else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(url(route, { path, query }), init);
  } catch (error) {
    if (error && error.name === "AbortError") throw error;
    throw new ApiError(0, "network", null);
  }
  const parsed = await readBody(response);
  if (!response.ok) {
    const detail = parsed && typeof parsed === "object" && "detail" in parsed ? parsed.detail : null;
    throw new ApiError(response.status, detail, parsed);
  }
  return parsed;
}

/**
 * 面談の開始ページの GET(/start)で、有効なクッキーがなければ依頼者 ID を発行してもらい(あれば変わらない。§6.3)、
 * 依頼者 ID と、面談を送信済みかを返す。クッキーは JS から読めないので、依頼者 ID は /v1/session で知る。
 */
export async function ensureSession() {
  await call("GET /start");
  return call("GET /v1/session");
}

/** 交渉の作成などの冪等キー(32 桁の乱数。8〜64 文字の条件を満たす)。 */
export function newRequestId() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

/* ---- エラーの表示 ---- */

// API の detail(理由の名前)→ 画面に出す文。入力の値は、サーバーが返さないので、ここにも出ない。
const MESSAGES = {
  no_session: "この操作には、面談のページで始めたセッションが要ります。「面談を始める」から開き直してください。",
  forbidden: "この操作は許可されていません(他の人の交渉、または存在しない交渉です)。",
  missing_requested_with_header: "リクエストの形が正しくありません。ページを読み込み直してください。",
  principal_deleting: "データの削除中です。削除が終わるまで、この操作はできません。",
  interview_not_submitted: "面談がまだ送信されていません。先に面談を完了してください。",
  interview_not_started: "面談がまだ始まっていません(途中の状態は、一定時間たつと消えます)。「面談を始める」からやり直してください。",
  profile_missing: "先にプロフィールを入力してください。",
  salary_proposal_missing: "先に年収の 3 問に答えてください。",
  salary_not_confirmed: "先に年収の換算を確かめてください。",
  axes_not_chosen: "先に、外す軸を決めてください。",
  choices_incomplete: "二択に、必要な組数まで答えてください。",
  not_confirmed: "先に、条件の平文を確認してください。",
  worst_case_not_approved: "先に、「最悪ここまで」を承認してください。",
  contradiction: "条件に矛盾があります。どちらかを消してください。",
  no_accept_anchors: "受けられる条件が 0 件です。外すのをやめるか、そのまま進むかを選んでください。",
  too_many_anchors: "条件が多すぎます。いくつかを消してください。",
  already_active: "進行中の交渉があります。終わるか取り消してから、次の交渉を始めてください。",
  budget_exhausted: "今日の交渉の予算を使い切りました。明日以降にもう一度お試しください。",
  blocked: "この求人の企業は、ブロック先に入っているため、交渉を始められません。",
  attribute_bands_missing: "面談の結果(属性帯)が保存されていません。面談をやり直してください。",
  no_matching_question: "この質問には、もう答える必要がありません(すでに答えたか、交渉が進みました)。",
  conflict: "状態が変わりました。画面を更新して、もう一度試してください。",
  negotiation_ended: "この交渉は、もう終わっています。",
  unknown_entry: "その項目は見つかりません。",
  not_found: "見つかりません。",
  unknown_attack_negotiation: "この攻撃の交渉は、サーバーに残っていません(再起動などで消えました)。新しく作ってください。",
  no_llm_context: "まだ候補者側の手番が来ていないか、記録が残っていません。",
  payload_too_large: "送った内容が大きすぎます(32 KB まで)。",
  body_too_large: "送った内容が大きすぎます(32 KB まで)。",
  policy_invalid: "条件に矛盾があり、保存できません。確認の画面で見直してください。",
  unknown_region: "都道府県の名前を読み取れません。一覧から選んでください。",
  unknown_job_category: "職種を選び直してください。",
  salary_basis_invalid: "この年収の定義では、比較基準年収を計算できません(固定残業代が大きすぎるなど)。値を見直してください。",
  unknown_pair: "その質問は見つかりません。画面を更新してください。",
  unknown_company: "一覧にない企業は選べません。",
  too_many_companies: "選べる企業の数を超えています。",
  invalid_json: "JSON として読めません。",
  temporarily_unavailable: "一時的に使えません。しばらくしてから、もう一度試してください。",
  starting_up: "起動したところです。1 分ほど待ってから、もう一度試してください。",
  too_many_interviews: "いま面談の席が埋まっています。しばらくしてから、もう一度試してください。",
  vault_error: "金庫との通信に失敗しました。しばらくしてから、もう一度試してください。",
  raw_message_unavailable: "生のメッセージを送る口が、いま使えません。",
  llm_unavailable: "AI が一時的に使えません。しばらくしてから、もう一度試してください。",
  llm_failed: "AI の呼び出しに失敗しました。もう一度試してください。",
  output_truncated: "AI の出力が長すぎて切れました。回答を短くして、もう一度試してください。",
  output_invalid: "AI の出力を読み取れませんでした。もう一度試してください。",
  not_judged: "この交渉は、まだ終わっていません。終わってから、もう一度試してください。",
  not_agreed: "合意できなかった交渉では、この操作はできません。",
  stage_not_open: "「承認」は、双方が「会う」を押して、段 1 が開いてから押せます。",
  job_summary_invalid: "職務要約は、1 文字以上 400 文字以下で書いてください。",
  invalid_body: "入力の形が正しくありません。",
  request_too_large: "送った内容が大きすぎます。",
  value_not_on_the_grid: "年収は、300〜1500 万円の、10 万円刻みの値で入れてください。",
  inconsistent_answers: "渡した交渉の答えが食い違っています(別々の候補者の交渉が混ざっています)。",
  bisection_timeout: "二分探索の実演が、時間内に終わりませんでした。もう一度試してください。",
};

// 状態の理由が分からないとき(detail がない・知らない)の文。
const STATUS_MESSAGES = {
  401: "セッションがありません。「面談を始める」から開き直してください。",
  403: "この操作は許可されていません。",
  404: "見つかりません。",
  409: "いまの状態では、この操作はできません。画面を更新して、もう一度試してください。",
  413: "送った内容が大きすぎます(32 KB まで)。",
  422: "入力の形が正しくありません。",
};

// 429 の 2 つの形のうち、{code: "rate_limited", entrance, ...} の入口の名前(web.limits の ENTRANCES)。
const ENTRANCE_LABELS = {
  interview_llm: "面談の AI への問い合わせ",
  demo_run: "デモの実行",
  live_negotiation_create: "交渉の開始",
  attack_create: "攻撃の交渉の作成",
  attack_instruction: "攻撃の指示",
  raw_message: "壁 1 の生のメッセージの送信",
  meter: "推定区間メーターの計算",
};

// 422 の検証エラーで、場所(項目名)を画面の言葉にする。入力の値は、サーバーが返さない。
const FIELD_LABELS = {
  experience_years: "経験年数",
  prefecture: "都道府県",
  job_category: "職種",
  answers: "回答",
  text: "文章",
  instruction: "指示(400 文字まで)",
  amount_man_yen: "金額",
  amount_period: "年額か月額か",
  amount_kind: "額面か手取りか",
  bonus_included: "賞与を含むか",
  bonus_months: "賞与の月数",
  fixed_overtime_man_yen_per_month: "固定残業代(月額)",
  job_summary: "匿名職務要約",
  negotiation_ids: "交渉の一覧",
  value: "年収",
};

function describeValidation(detail) {
  if (!Array.isArray(detail)) return STATUS_MESSAGES[422];
  const names = [];
  for (const item of detail) {
    const parts = Array.isArray(item && item.loc) ? item.loc.filter((part) => typeof part === "string" && part !== "body") : [];
    const name = parts.length ? parts[parts.length - 1] : null;
    if (name && !names.includes(name)) names.push(FIELD_LABELS[name] ?? name);
  }
  return names.length
    ? `入力の形が正しくありません(${names.join("、")})。値を見直してください。`
    : STATUS_MESSAGES[422];
}

// 429 には 2 つの形がある(台帳 I-26): 入口ごとの回数の制限 {code: "rate_limited", ...} と、文字列の daily_limit_reached(1 日の上限)・rate_limited。
function describeRateLimit(error, overrides) {
  const detail = error.detail;
  if (detail && typeof detail === "object" && detail.code === "rate_limited") {
    const entrance = ENTRANCE_LABELS[detail.entrance] ?? "この操作";
    const scope = detail.scope === "overall" ? "全体の上限" : "あなたの上限";
    const minutes = Math.max(1, Math.round(Number(detail.window_seconds) / 60));
    return `${entrance}の回数が、${scope}(${minutes} 分あたり ${detail.limit} 回)に達しました。${detail.retry_after_seconds} 秒ほど待ってから、もう一度試してください。`;
  }
  if (detail === "daily_limit_reached") {
    return overrides.daily_limit_reached ?? "本日の上限に達しました。明日以降にもう一度お試しください。";
  }
  if (detail === "rate_limited") return "短い間に操作が多すぎます。少し待ってから、もう一度試してください。";
  return "混み合っています。少し待ってから、もう一度試してください。";
}

/**
 * エラーを、画面に出す日本語にする。overrides は、detail の名前 → 文(場面に合わせて文を変えたいとき。
 * 例: 交渉の作成の daily_limit_reached は「本日の交渉の上限に達しました」)。
 */
export function describeError(error, overrides = {}) {
  if (!(error instanceof ApiError)) return "予期しないエラーが起きました。ページを読み込み直してください。";
  if (error.status === 0) return "サーバーに接続できません。通信の状態を確かめて、もう一度試してください。";
  if (error.status === 429) return describeRateLimit(error, overrides);
  const code = typeof error.detail === "string" ? error.detail : null;
  if (code && overrides[code]) return overrides[code];
  if (code && MESSAGES[code]) return MESSAGES[code];
  if (error.status === 422) return describeValidation(error.detail);
  if (STATUS_MESSAGES[error.status]) return STATUS_MESSAGES[error.status];
  if (error.status >= 500) return "サーバーで問題が起きました。しばらくしてから、もう一度試してください。";
  return `リクエストを処理できませんでした(${error.status})。`;
}
