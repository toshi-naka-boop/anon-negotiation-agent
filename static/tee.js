/* 入口のページの「金庫の確認(TEE)」(design.md §9 の表の 6 行目・AC-23)。
 *
 * - web が金庫(vault)の attestation を検証した結果(GET /api/tee/attestation。nonce なし)を、そのまま描く。公開情報だけで、セッションは要らない。
 *   応答には、検証に使ったトークン(JWT)も入っているが、この画面には出さない(自分で確かめたい人は、下に出すスクリプトで取り直して検証する)。
 * - verified が偽でも、取れた範囲の claims は返る。署名を確かめていない値なので、その旨を添える。
 * - 説明文は、設計書 §9 の「TEE で言えること」の 3 段と「TEE でも言えないこと」に合わせる。L0 の間は、コミットとの対応は運営者の記録で、
 *   公開コードで動いているとは書かない(設計書の規則)。
 * - API の経路は、このモジュールに書かない(ページのスクリプトが load() を渡す)。
 */

import { ApiError } from "./api.js";
import { h, replace, showError } from "./ui.js";

// TEE モードでない環境(開発用のサーバなど)では、経路そのものがなく、404 が返る。
const NOT_TEE_NOTE = "この環境では、金庫は TEE(Confidential Space)で動いていません(開発用の構成)。";

// 証明(claims)の項目: [応答のキー, 呼び名]。この順に並べる。
const CLAIMS = [
  ["image_digest", "金庫のイメージの digest"],
  ["hwmodel", "ハードウェア"],
  ["swname", "実行環境"],
  ["swversion", "実行環境のバージョン"],
  ["dbgstat", "デバッグの状態"],
  ["support_attributes", "サポートの状態"],
  ["project_id", "プロジェクト ID"],
  ["zone", "ゾーン"],
  ["instance_name", "インスタンス名"],
];

const RELEASE_STATUS = { active: "有効(active)", revoked: "失効(revoked)" };

// 設計書 §9「TEE で言えること」の 3 段(誰が何で確かめるかを添える)と、「TEE でも言えないこと」。
const EXPLANATION = [
  {
    head: "動いているもの",
    by: "第三者が、Google の証明で確かめられます",
    text: "「検証済み」のとき、この金庫では、Confidential Space の本番イメージで、上の digest のコンテナが、コマンドも環境変数も上書きされずに動いています。",
  },
  {
    head: "鍵の排他性",
    by: "運営者の設定に依存します",
    text: "保存データの鍵を使えるのは、この digest の金庫と、プロジェクトのオーナーだけです。この設定は、デプロイの確認(scripts/deploy_check.sh)で照合しています。オーナーは技術的には復号できますが、Cloud KMS の監査ログに主体と時刻が残ります。防ぐのではなく、記録による抑止です。",
  },
  {
    head: "ソースの由来",
    by: "いまは、運営者の申告です",
    text: "許可リストにある digest が動いていることまでが言えます。コミットとの対応は運営者の記録で、第三者が確かめられるのは、GitHub の証明つきビルドか再現ビルドを入れてからです。",
  },
  {
    head: "確かめられるのはここまで",
    text: "この digest の金庫が存在し、与えた nonce に応答したことまでです。web がその金庫にだけデータを送っていることの証明にはなりません。検証は、確認した時刻のものです。",
  },
  {
    head: "TEE の外にあるもの",
    text: "面談中の生の値、段階開示の状態、開示台帳は、TEE の外(web 側)にあります。運営側のコードは、本人向けの読み出し口を通せば、丸め済みの条件と交渉の記録を読めます。",
  },
];

// 検証のコマンドに差し込む値の形。応答の値は、この形に合うものだけを使う(コマンドは、そのまま端末に貼られ得るため)。合わなければ、< > の置き換え用の文字を出す。
const PROJECT_ID_PATTERN = /^[a-z][a-z0-9-]{4,28}[a-z0-9]$/; // GCP のプロジェクト ID(6〜30 文字)
const ORIGIN_PATTERN = /^https?:\/\/[A-Za-z0-9.-]+(:\d+)?$/;

function pick(value, pattern, placeholder) {
  return typeof value === "string" && pattern.test(value) ? value : placeholder;
}

/** コミットのリンク先。https の URL だけを使う(応答の文字列を、そのまま href に入れない)。なければ null。 */
function commitLink(url) {
  try {
    return new URL(url).protocol === "https:" ? url : null;
  } catch {
    return null;
  }
}

/** 項目名と値の 1 行。値がない(null・空の配列)ときは「—」。 */
function fact(label, value) {
  const missing = value === null || value === undefined || (Array.isArray(value) && value.length === 0);
  return h(
    "div",
    { class: "tee-fact" },
    h("dt", {}, label),
    h("dd", {}, missing ? h("span", { class: "muted" }, "—") : h("code", {}, Array.isArray(value) ? value.join(", ") : String(value))),
  );
}

function statusLine(data) {
  if (data.verified) {
    return h(
      "p",
      { class: "row" },
      h("span", { class: "badge badge-ok" }, "検証済み"),
      h("span", {}, "web が、Google の署名つきの証明(attestation)を検証しました。この表示を信用せず、下の手順で自分でも確かめられます。"),
    );
  }
  return h(
    "p",
    { class: "row" },
    h("span", { class: "badge badge-danger" }, "検証できていません"),
    h("span", {}, data.reason ? ["理由: ", h("code", {}, data.reason)] : "理由は返っていません。"),
  );
}

function commitRow(release) {
  const link = commitLink(release.url);
  return h(
    "div",
    { class: "tee-fact" },
    h("dt", {}, "コミット(commit)"),
    h(
      "dd",
      {},
      link
        ? h("a", { href: link, target: "_blank", rel: "noopener noreferrer" }, h("code", {}, release.commit))
        : [h("code", {}, release.commit), h("div", { class: "small muted" }, "GitHub のコミットへのリンクは、この環境では設定されていません。")],
    ),
  );
}

function releaseSection(release) {
  if (!release) {
    return h(
      "p",
      {},
      "この digest は、運営者のリリースの表(deploy/vault-releases.json)にありません(検証が通っていないときは、表と照合していません)。コミットとの対応は示せません。",
    );
  }
  return h(
    "dl",
    { class: "tee-facts" },
    commitRow(release),
    fact("ビルドした時刻(built_at)", release.built_at),
    fact("状態(status)", RELEASE_STATUS[release.status] ?? release.status),
  );
}

function explanation() {
  return h(
    "ul",
    {},
    EXPLANATION.map(({ head, by, text }) => h("li", {}, h("strong", {}, head), by ? `(${by})` : null, `: ${text}`)),
  );
}

function verifyCommand(claims, origin) {
  const web = pick(origin, ORIGIN_PATTERN, "<この URL>");
  const project = pick(claims.project_id, PROJECT_ID_PATTERN, "<プロジェクト ID>");
  return `uv run python scripts/verify_attestation.py --web ${web} --project ${project} --service-account <金庫の SA>`;
}

function render(data, origin) {
  const claims = data.claims ?? {};
  return [
    statusLine(data),
    data.verified
      ? null
      : h("p", { class: "small muted" }, "検証が通らなかったので、下の値は、確かめられていないトークンに書かれていたものです(取れなかった項目は「—」)。信用しないでください。"),
    h("h3", {}, "証明の内容"),
    h(
      "dl",
      { class: "tee-facts" },
      CLAIMS.map(([key, label]) => fact(`${label}(${key})`, claims[key])),
      fact("確認した時刻(checked_at)", data.checked_at),
      fact("金庫の TLS 証明書のハッシュ(certificate_sha256)", data.certificate_sha256),
    ),
    h("h3", {}, "GitHub のコミットとの対応"),
    releaseSection(data.release),
    h("h3", {}, "この確認で言えること・言えないこと"),
    explanation(),
    h("h3", {}, "自分で確かめる"),
    h("p", {}, "リポジトリのルートで、次を実行します。web の「検証済み」を信用せず、Google の署名つきのトークンを、スクリプトが自分で検証します。"),
    h("pre", {}, h("code", {}, verifyCommand(claims, origin))),
    h("p", { class: "small muted" }, "< > の部分は、実際の値に置き換えます(<金庫の SA> は、金庫のサービスアカウントのメールアドレスです)。"),
  ];
}

/**
 * 区画を描く。body はページの HTML にある要素。load() は、GET /api/tee/attestation の応答(JSON)を返す。origin は、このページの配信元
 * (検証のコマンドの --web に入れる)。404 は TEE モードでない環境(注記を出す)、それ以外の失敗は、ほかの区画と同じ形でエラーを出す。
 */
export async function showTee(body, { load, origin }) {
  try {
    replace(body, render(await load(), origin));
  } catch (failure) {
    if (failure instanceof ApiError && failure.status === 404) replace(body, h("p", { class: "muted" }, NOT_TEE_NOTE));
    else showError(body, failure);
  }
}
