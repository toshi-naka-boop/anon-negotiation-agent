#!/usr/bin/env bash
# 本番のデプロイの確認(設計書 §10 の「本番の必須設定」と TEE の照合 (a)〜(i)、§12.1 の AC-22)。
#
#   PROJECT_ID=... REGION=... ZONE=... WEB_URL=https://... AGENTS_URL=https://... VAULT_MODE=tee bash scripts/deploy_check.sh
#   bash scripts/deploy_check.sh --list                     # 確認項目の一覧(環境変数は要らない)
#   bash scripts/deploy_check.sh --only tee-a,tee-b         # 項目を絞る(繰り返し・コンマ区切り)
#   bash scripts/deploy_check.sh --show-expected            # KEK を使えるはずの主体の期待集合を表示して終わる(gcloud を呼ばない)
#   bash scripts/deploy_check.sh --reset-vault              # (h) の再起動後の自己試験も行う(金庫の VM を停止→開始する)
#
# 項目ごとに [OK]・[NG]・[SKIP] を 1 行ずつ出す(NG と SKIP には、理由・差分を、字下げした行で添える)。NG が 1 つでもあれば終了コード 1、
# なければ 0。必須の環境変数がない・引数が違うときは終了コード 2。
#   [SKIP] は「このスクリプトでは確かめられない」「VAULT_MODE の対象外」。AC-22 の合格には、確かめられない項目を人が済ませること。
#   gcloud や curl が失敗して確かめられなかった項目は [NG] にする(確かめられないものを、通ったことにしない)。
#
# 環境変数
#   PROJECT_ID  GCP のプロジェクト ID(必須)           REGION  リージョン(必須。例 asia-northeast1)
#   WEB_URL     web の URL(必須)                        AGENTS_URL  agents の URL(必須。IAM で守られているので、ID トークンつきで呼ぶ)
#   ZONE        VM のゾーン(VAULT_MODE=tee では必須。例 asia-northeast1-b)
#   VAULT_MODE  tee(既定。金庫は Confidential Space の VM) または cloudrun(金庫も Cloud Run)
#   ORG_ID      組織 ID(任意。あれば Policy Analyzer を --organization で、なければ --project で呼ぶ。P-13)
#   WEB_SA      web のサービスアカウントのメール(既定 web-run@<PROJECT_ID>.iam.gserviceaccount.com)
#   VAULT_SA    金庫の VM のサービスアカウントのメール(既定 vault-tee@<PROJECT_ID>.iam.gserviceaccount.com)
#   PROJECT_NUMBER  プロジェクト番号(任意。なければ gcloud projects describe で取る)
#   VAULT_URL   金庫(Cloud Run 版)の URL(任意。なければ gcloud run services describe の status.url)
#   WEB_SERVICE・AGENTS_SERVICE・VAULT_SERVICE  Cloud Run のサービス名(既定 web・agents・vault)
#   HEALTH_PATH  死活確認のパス(既定 /health。下の注意を読む)
#   CACHE_CONFIG_URL  Vertex AI の cacheConfig の URL(既定 https://aiplatform.googleapis.com/v1/projects/<PROJECT_ID>/cacheConfig)
#   EXPECTED_KMS_PRINCIPALS_FILE  期待する主体の正本(既定 deploy/expected-kms-principals.json)
#   RELEASES_FILE  digest の許可表(既定 deploy/vault-releases.json)
#   GCLOUD・CURL・UV・PYTHON3  外部コマンドの置き換え(既定 gcloud・curl・uv・python3。試験が偽物を差し込む)
#   RESET_POLLS・POLL_INTERVAL_SECONDS  --reset-vault の後、ログを待つ回数(既定 36)と間隔(既定 10 秒)
#
# このスクリプトがすること・しないこと
#   - gcloud・curl は、読み出し(describe・list・get・read・GET)だけ。何も作らず、変えず、消さない。ただし --reset-vault を付けたときだけ、
#     金庫の VM を停止→開始する(gcloud compute instances stop → start。数分、金庫が応えなくなる。審査期間中は付けない。
#     instances reset は使わない: 実測(2026-10-04)で、起動のたびに vTPM の DA ロックアウトのカウンタが増え、reset はそれを「不正な停止」として数える)。
#   - トークンの値・鍵・DEK・暗号文は、出力しない(ID トークン・アクセストークンは変数に持ち、curl には標準入力のヘッダで渡す。
#     プロセスの引数に出ない)。Secret Manager の値も見ない(参照かどうかだけ)。bash -x(xtrace)で動かすと、トークンが画面に出る。使わない。
#   - 期待値は、設定ファイル(config/params.toml)・deploy/vault-releases.json・deploy/expected-kms-principals.json と、手順書(research/tee-spike.md)
#     の名前から作る。現在の IAM から期待値を作らない(正本は手で書く。X-75)。
#
# 実機の出力の形は、公開の API 文書に合わせて書いてある。実機で NG が出たら、差分(字下げした行)で、スクリプトの想定が違うのか、
# GCP の設定が違うのかを見分ける。Policy Analyzer(tee-b・tee-b-pool)は Cloud Asset API(cloudasset.googleapis.com)を有効にし、
# 呼ぶ主体に cloudasset.assets.analyzeIamPolicy の権限が要る(手順 A の API の一覧にはない)。
#
# 死活確認のパスの注意: Cloud Run の *.run.app では、末尾が z のパス(/healthz など)を Google のフロントエンドが予約していて、コンテナに届く前に自前の 404 を返す
# (公式の既知の問題: 末尾が z のパスは避ける)。web・agents・金庫の経路が /health のままだと、healthz の 3 項目は NG(404)になる。
# アプリの経路を /health などに変えたら、HEALTH_PATH=/health で確認する。
#
# 書くときの注意: 変数の直後に日本語が続くときは、${VAR} と中括弧で囲む(macOS の bash 3.2 は、$VAR の直後のバイトを変数名に含めることがある)。

# check_* の関数は、run_item が名前から組み立てて呼ぶ(直接は呼ばない)ので、shellcheck の「呼ばれていない関数」の警告を止める。
# shellcheck disable=SC2317,SC2329
if [ -z "${BASH_VERSION:-}" ]; then
  echo "bash で実行してください: bash scripts/deploy_check.sh" >&2
  exit 2
fi

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ----------------------------------------------------------------------
# 確認項目の表: id|対象|題(対象: all=どちらでも、tee=TEE 版の金庫だけ、cloudrun=Cloud Run 版の金庫だけ)
# ----------------------------------------------------------------------
ITEMS='adk-capture|all|§10 ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false(web・agents の両方)
log-level|all|§10 ログは INFO 以上(web・agents に DEBUG の設定がない)
web-workers|all|§10 web は uvicorn workers=1・インスタンスは min・max とも 1・CPU 常時割り当て・Cloud Run の同時リクエスト数(containerConcurrency)200
session-key|all|§10 SESSION_SIGNING_KEY は Secret Manager の参照
service-auth|all|§10 SERVICE_AUTH_ENABLED が false でない(web)
vertex-env|all|§10 GOOGLE_GENAI_USE_VERTEXAI=TRUE・GOOGLE_CLOUD_PROJECT・GOOGLE_CLOUD_LOCATION=global(web・agents)
web-vault-env|all|§10 web の金庫の設定(VAULT_BASE_URL。TEE なら VAULT_TEE・VAULT_SERVICE_ACCOUNT・GOOGLE_CLOUD_PROJECT)
agents-url|all|§10 agents の URL(config/params.toml の [agents] public_base_url が AGENTS_URL と一致)
iam-agents|all|§10 Cloud Run IAM: agents の起動元が web のサービスアカウントだけ
iam-vault|cloudrun|§10 Cloud Run IAM: 金庫(Cloud Run 版)の起動元が web のサービスアカウントだけ
vault-command|cloudrun|§10 金庫(Cloud Run 版)の起動コマンドが vault.app:create_app_from_env --factory
ttl-default|all|§10 Firestore の TTL ポリシー((default): stages・llm_call_counters・rate_limits の ttl_at)
ttl-vault|all|§10 Firestore の TTL ポリシー(vault-db: negotiations・events・idempotency の ttl_at)
cache-config|all|§10 プロジェクトの cacheConfig.disableCache=true
vertex-quota|all|§10 Vertex AI の利用枠を申請していない(コンソールで確認)
thinking-usage|all|§10 思考の量の設定が効いている(usage_metadata の思考トークン数)
no-aiohttp|all|§10 aiohttp が入っていない(uv pip show・uv.lock)
request-log|all|§10 Log Router の _Default シンクが run.googleapis.com/requests を除外している
r7-client-ip|all|R-7 X-Forwarded-For の末尾がクライアント IP
healthz-web|all|AC-22 web の /health(既定。HEALTH_PATH で変える)が 200
healthz-agents|all|AC-22 agents の /health が 200(ID トークンつき)
healthz-vault|all|AC-22 金庫(cloudrun: /health が 200〔ID トークンつき〕。tee: web の /api/tee/attestation が verified=true)
demo-url|all|AC-22 デモ URL(WEB_URL)が開ける
submission-checklist|all|AC-22 提出物 6 点のチェックリスト
tee-a|tee|(a) WIF のプール vault-tee-pool に有効なプロバイダが attestation-verifier の 1 件だけで、発行元・audience・mapping・condition が本番と完全一致
tee-b|tee|(b) KEK を使える主体(Policy Analyzer。全階層)が deploy/expected-kms-principals.json と vault-releases.json の active から作った期待集合と完全一致
tee-b-pool|tee|(b) プールとプロバイダを変えられる主体(Policy Analyzer。プロジェクトを対象に解析)が承認済みのオーナーだけ
tee-c|tee|(c) 金庫の VM の SA と web の SA が、KEK を使える主体に現れない
tee-d|tee|(d) KEK の primary の版が ENABLED で、それ以外の版はすべて無効・破棄予定
tee-e|tee|(e) VM に外部 IP がなく、本番イメージ(confidential-space)で、tee-image-reference が active なダイジェスト
tee-f|tee|(f) ファイアウォール規則 allow-iap-to-vault が消えている
tee-g|tee|(g) Cloud KMS の Data Access 監査ログが有効で exemptedMembers が空、新しい版の後の Encrypt・Decrypt の主体が本番の VM の subject だけ
tee-h|tee|(h) _tee/dek.kek_version が KEK の primary の版と一致
tee-h-reset|tee|(h) 金庫を停止→開始しても sealing self-test ok(既存の暗号文が開く。--reset-vault のときだけ)
tee-i|tee|(i) 拒否ポリシー vault-kek-deny があり、本文が手順 G と一致'

# 手順書(research/tee-spike.md)と設定(config/params.toml の [vault.tee])の名前。期待値の元。
POOL="vault-tee-pool"
PROVIDER="attestation-verifier"
KEYRING="vault-tee"
KEY="vault-kek"
VM_NAME="vault-tee"
DENY_POLICY="vault-kek-deny"
IAP_FIREWALL_RULE="allow-iap-to-vault"

# ----------------------------------------------------------------------
# 引数
# ----------------------------------------------------------------------
usage() {
  sed -n '2,/^# 環境変数$/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
  echo "環境変数と、このスクリプトがすること・しないことは、ファイルの先頭のコメントに書いてある。"
}

LIST_ONLY=0
SHOW_EXPECTED=0
RESET_VAULT=0
ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --list) LIST_ONLY=1 ;;
    --show-expected) SHOW_EXPECTED=1 ;;
    --reset-vault) RESET_VAULT=1 ;;
    --only)
      if [ $# -lt 2 ]; then
        echo "--only には項目の id を渡してください(--list で一覧)" >&2
        exit 2
      fi
      ONLY="$ONLY,$2"
      shift
      ;;
    --only=*) ONLY="$ONLY,${1#--only=}" ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "不明な引数: $1(使い方は --help)" >&2
      exit 2
      ;;
  esac
  shift
done

if [ "$LIST_ONLY" -eq 1 ]; then
  printf '%-22s %-9s %s\n' "id" "対象" "題"
  while IFS='|' read -r id mode title; do
    printf '%-22s %-9s %s\n' "$id" "$mode" "$title"
  done <<EOF
$ITEMS
EOF
  exit 0
fi

item_exists() { # item_exists ID: 確認項目の表にあるか
  local id
  while IFS='|' read -r id _ <&3; do
    if [ "$id" = "$1" ]; then return 0; fi
  done 3<<EOF
$ITEMS
EOF
  return 1
}

# --only の id が表にあること
if [ -n "$ONLY" ]; then
  while IFS= read -r wanted; do
    [ -n "$wanted" ] || continue
    if ! item_exists "$wanted"; then
      echo "不明な項目: $wanted(--list で一覧)" >&2
      exit 2
    fi
  done <<EOF
$(printf '%s' "$ONLY" | tr ',' '\n')
EOF
fi

GCLOUD="${GCLOUD:-gcloud}"
CURL="${CURL:-curl}"
UV="${UV:-uv}"
PYTHON3="${PYTHON3:-python3}"
EXPECTED_FILE="${EXPECTED_KMS_PRINCIPALS_FILE:-$ROOT/deploy/expected-kms-principals.json}"
RELEASES_FILE="${RELEASES_FILE:-$ROOT/deploy/vault-releases.json}"
export ROOT EXPECTED_FILE RELEASES_FILE
export PYTHONIOENCODING=utf-8

# ----------------------------------------------------------------------
# 評価プログラム(python3。標準ライブラリだけ)。bash が $WORK に取っておいた gcloud・curl の出力を読んで、1 項目の結果を出す。
#   python3 -c "$PY_EVAL" check <項目> [<Python の確認の名前>]  → 結果の行(先頭は [OK]・[NG]・[SKIP])
#   python3 -c "$PY_EVAL" value <名前>                         → 値を 1 行で出す(取れなければ、理由を標準エラーに出して終了コード 1)
#   python3 -c "$PY_EVAL" expected                             → 期待する主体の集合
# ----------------------------------------------------------------------
IFS= read -r -d '' PY_EVAL <<'PYEOF' || true
import json
import os
import re
import sys
from urllib.parse import urlsplit

ENV = os.environ
WORK = ENV.get("WORK", "")
ROOT = ENV.get("ROOT", ".")
MODE = ENV.get("VAULT_MODE", "tee")
PROJECT_ID = ENV.get("PROJECT_ID", "")
PROJECT_NUMBER = ENV.get("PROJECT_NUMBER", "")
WEB_SA = ENV.get("WEB_SA", "")
VAULT_SA = ENV.get("VAULT_SA", "")
AGENTS_URL = ENV.get("AGENTS_URL", "")
WEB_URL = ENV.get("WEB_URL", "")
SERVICES = {
    "web": ENV.get("WEB_SERVICE", "web"),
    "agents": ENV.get("AGENTS_SERVICE", "agents"),
    "vault": ENV.get("VAULT_SERVICE", "vault"),
}
POOL = ENV.get("POOL", "vault-tee-pool")
PROVIDER = ENV.get("PROVIDER", "attestation-verifier")
KEY = ENV.get("KEY", "vault-kek")
DENY_POLICY = ENV.get("DENY_POLICY", "vault-kek-deny")


class Done(Exception):
    """1 項目の結果。ok・ng・skip が投げる。"""

    def __init__(self, status, message, details):
        Exception.__init__(self, message)
        self.status = status
        self.message = message
        self.details = details


def ok(message, *details):
    raise Done("OK", message, details)


def ng(message, *details):
    raise Done("NG", message, details)


def skip(message, *details):
    raise Done("SKIP", message, details)


def shown(value):
    return "未設定" if value is None else repr(value)


# ---- bash が取っておいた出力を読む ----


def read(name, suffix, default=""):
    try:
        with open("%s/%s.%s" % (WORK, name, suffix), encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return default


def data(name):
    """取っておいた出力を JSON として読む。コマンドが失敗していた・HTTP が 2xx でない・JSON でないなら、NG にする。"""
    label = read(name, "cmd", name)
    rc = read(name, "rc", "none")
    if rc != "0":
        err = read(name, "err")
        ng("取得できない: %s(終了コード %s%s)" % (label, rc, ": " + err if err else ""))
    http = read(name, "http")
    if http and not http.startswith("2"):
        ng("取得できない: %s(HTTP %s)" % (label, http))
    try:
        with open("%s/%s.out" % (WORK, name), encoding="utf-8", errors="replace") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        ng("取得した出力が JSON として読めない: %s" % label)


# ---- Cloud Run のサービスの説明(gcloud run services describe --format json。Knative の v1 の形) ----


def containers(service):
    try:
        found = service["spec"]["template"]["spec"]["containers"]
    except (KeyError, TypeError):
        found = None
    if not found:
        ng("Cloud Run のサービスの説明が想定の形でない(spec.template.spec.containers がない)")
    return found


def env_of(service):
    """環境変数 → ("value", 値) か ("secret", "")。Secret Manager の参照(valueFrom)の中身は、見ない・出さない。"""
    values = {}
    for container in containers(service):
        for item in container.get("env") or []:
            if "valueFrom" in item:
                values[item["name"]] = ("secret", "")
            else:
                values[item["name"]] = ("value", str(item.get("value", "")))
    return values


def plain(env, key):
    kind, value = env.get(key, (None, None))
    return value if kind == "value" else None


def command_tokens(container):
    return list(container.get("command") or []) + list(container.get("args") or [])


def option_value(tokens, option):
    found = None
    for index, token in enumerate(tokens):
        if token == option and index + 1 < len(tokens):
            found = tokens[index + 1]
        elif token.startswith(option + "="):
            found = token.split("=", 1)[1]
    return found


def annotations_of(service):
    merged = dict((service.get("metadata") or {}).get("annotations") or {})
    template = ((service.get("spec") or {}).get("template") or {}).get("metadata") or {}
    merged.update(template.get("annotations") or {})
    return merged


def service_urls(service):
    urls = set()
    status_url = (service.get("status") or {}).get("url")
    if status_url:
        urls.add(status_url.rstrip("/"))
    try:
        listed = json.loads(annotations_of(service).get("run.googleapis.com/urls") or "[]")
        urls.update(str(url).rstrip("/") for url in listed)
    except (ValueError, TypeError, AttributeError):
        pass
    return urls


# ---- §10 の本番の必須設定 ----

LOW_LEVELS = {"debug", "trace", "notset", "0", "5", "10"}


def check_adk_capture():
    problems = []
    for name in ("web", "agents"):
        got = env_of(data("svc-" + name)).get("ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS")
        if got is None:
            problems.append("%s: 未設定(ADK の既定は、メッセージの内容をスパンに載せる)" % name)
        elif got[0] != "value" or got[1].strip().lower() != "false":
            problems.append("%s: false でない(値: %s)" % (name, got[1] if got[0] == "value" else "秘密の参照"))
    if problems:
        ng("スパンにメッセージの内容が載る設定になっている", *problems)
    ok("web・agents とも false")


def check_log_level():
    problems = []
    for name in ("web", "agents"):
        service = data("svc-" + name)
        for container in containers(service):
            level = option_value(command_tokens(container), "--log-level")
            if level is not None and level.strip().lower() in LOW_LEVELS:
                problems.append("%s: --log-level %s" % (name, level))
        for key, (kind, value) in env_of(service).items():
            if "LOG_LEVEL" in key.upper() and kind == "value" and value.strip().lower() in LOW_LEVELS:
                problems.append("%s: %s=%s" % (name, key, value))
    if problems:
        ng("ログの水準が DEBUG 以下になっている(a2a-sdk と ADK は DEBUG でリクエストの本文を出す)", *problems)
    ok("INFO 以上(--log-level・LOG_LEVEL 系の環境変数に debug・trace がない)")


def dockerfile_workers():
    try:
        with open(ROOT + "/Dockerfile", encoding="utf-8") as handle:
            found = re.search(r'"--workers",\s*"(\d+)"', handle.read())
    except OSError:
        return None
    return found.group(1) if found else None


# §10: web の Cloud Run の同時リクエスト数(containerConcurrency)。SSE の同時本数の上限(20)に通常の要求の分を足した値(台帳 C-65)。
WEB_CONTAINER_CONCURRENCY = 200


def check_web_workers():
    service = data("svc-web")
    tokens = command_tokens(containers(service)[0])
    problems = []
    if tokens:
        workers = option_value(tokens, "--workers")
        if workers not in (None, "1"):
            problems.append("起動コマンドの --workers が %s(1 にする。依頼者ごとのロックがプロセスの中にある)" % workers)
        source = "起動コマンドの指定"
    else:
        if dockerfile_workers() != "1":
            problems.append("起動コマンドの指定がなく、イメージの CMD(Dockerfile)にも --workers 1 がない")
        source = "イメージの CMD(Dockerfile の --workers 1)"
    concurrency = plain(env_of(service), "WEB_CONCURRENCY")
    if concurrency not in (None, "", "1"):
        problems.append("環境変数 WEB_CONCURRENCY=%s(uvicorn の workers の既定になる)" % concurrency)
    annotations = annotations_of(service)
    for key, want in (
        ("autoscaling.knative.dev/minScale", "1"),
        ("autoscaling.knative.dev/maxScale", "1"),
        ("run.googleapis.com/cpu-throttling", "false"),
    ):
        got = annotations.get(key)
        if got is None or str(got).strip().lower() != want:
            problems.append("%s が %s(期待: %s)" % (key, shown(got), want))
    template_spec = ((service.get("spec") or {}).get("template") or {}).get("spec") or {}
    limit = template_spec.get("containerConcurrency")
    if limit is None or str(limit).strip() != str(WEB_CONTAINER_CONCURRENCY):
        problems.append(
            "spec.template.spec.containerConcurrency が %s(期待: %d。SSE の同時本数の上限〔20〕に通常の要求の分を足した値。既定の 80 のままだと SSE だけで埋まる)"
            % (shown(limit), WEB_CONTAINER_CONCURRENCY)
        )
    if problems:
        ng("web の起動・スケール・同時リクエスト数の設定が、前提(プロセスの中の状態〔依頼者ごとのロック・面談の状態〕、SSE の同時本数の枠)と合わない", *problems)
    ok("workers=1(%s)・min・max とも 1・CPU 常時割り当て・同時リクエスト数 %d" % (source, WEB_CONTAINER_CONCURRENCY))


def check_session_key():
    got = env_of(data("svc-web")).get("SESSION_SIGNING_KEY")
    if got is None:
        ng("web に SESSION_SIGNING_KEY がない(web は起動できない)")
    if got[0] != "secret":
        ng("SESSION_SIGNING_KEY が平文の値で設定されている(Secret Manager の参照にする。値は表示しない)")
    ok("Secret Manager の参照で設定されている(値は見ていない)")


def check_service_auth():
    got = env_of(data("svc-web")).get("SERVICE_AUTH_ENABLED")
    if got is None:
        ok("未設定(= 有効。既定)")
    if got[0] == "value" and got[1].strip().lower() == "true":
        ok("true")
    ng("SERVICE_AUTH_ENABLED が true でない(値: %s)。本番では有効にする" % (got[1] if got[0] == "value" else "秘密の参照"))


def check_vertex_env():
    wanted = (
        ("GOOGLE_GENAI_USE_VERTEXAI", "TRUE", True),
        ("GOOGLE_CLOUD_PROJECT", PROJECT_ID, False),
        ("GOOGLE_CLOUD_LOCATION", "global", False),
    )
    problems = []
    for name in ("web", "agents"):
        env = env_of(data("svc-" + name))
        for key, want, ignore_case in wanted:
            got = plain(env, key)
            if got is None:
                problems.append("%s: %s が未設定(または秘密の参照)" % (name, key))
            elif (got.strip().lower() != want.lower()) if ignore_case else (got.strip() != want):
                problems.append("%s: %s が %s(期待: %s)" % (name, key, shown(got), want))
    if problems:
        ng("Vertex AI の設定が足りない・違う", *problems)
    ok("web・agents とも GOOGLE_GENAI_USE_VERTEXAI=TRUE・GOOGLE_CLOUD_PROJECT・GOOGLE_CLOUD_LOCATION=global")


def check_web_vault_env():
    env = env_of(data("svc-web"))
    base = plain(env, "VAULT_BASE_URL")
    tee = (plain(env, "VAULT_TEE") or "false").strip().lower()
    if not base:
        ng("VAULT_BASE_URL が未設定(web は起動できない)")
    problems = []
    if MODE == "cloudrun":
        if tee == "true":
            problems.append("VAULT_TEE=true だが、VAULT_MODE=cloudrun(金庫は Cloud Run 版のはず)")
        urls = service_urls(data("svc-vault"))
        if base.rstrip("/") not in urls:
            problems.append("VAULT_BASE_URL(%s)が、金庫の Cloud Run サービスの URL(%s)にない" % (base, ", ".join(sorted(urls)) or "取れない"))
        if problems:
            ng("web の金庫の設定が合わない", *problems)
        ok("VAULT_BASE_URL が金庫の Cloud Run サービスの URL・VAULT_TEE は有効でない")
    if tee != "true":
        problems.append("VAULT_TEE が true でない(値: %s)" % shown(plain(env, "VAULT_TEE")))
    if not base.startswith("https://"):
        problems.append("VAULT_BASE_URL(%s)が https でない(TEE の金庫は、証明書をピン留めした TLS でつなぐ)" % base)
    for key, want in (("VAULT_SERVICE_ACCOUNT", VAULT_SA), ("GOOGLE_CLOUD_PROJECT", PROJECT_ID)):
        got = plain(env, key)
        if got is None:
            problems.append("%s が未設定(TEE モードでは必須)" % key)
        elif got.strip() != want:
            problems.append("%s が %s(期待: %s)" % (key, shown(got), want))
    if problems:
        ng("web の TEE モードの設定が合わない", *problems)
    releases = plain(env, "VAULT_RELEASES_FILE")
    ok(
        "VAULT_TEE=true・VAULT_BASE_URL=%s・VAULT_SERVICE_ACCOUNT・GOOGLE_CLOUD_PROJECT(VAULT_RELEASES_FILE: %s)"
        % (base, releases or "未設定 = イメージ内の deploy/vault-releases.json")
    )


def check_agents_url():
    try:
        with open(ROOT + "/config/params.toml", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        ng("config/params.toml を読めない")
    section = re.search(r"^\[agents\]\s*$(.*?)(?=^\[|\Z)", text, re.S | re.M)
    found = re.search(r'^public_base_url\s*=\s*"([^"]*)"', section.group(1), re.M) if section else None
    if not found:
        ng("config/params.toml の [agents] public_base_url を読めない")
    configured, expected = found.group(1).rstrip("/"), AGENTS_URL.rstrip("/")
    if configured != expected:
        ng(
            "public_base_url が AGENTS_URL と違う(web は public_base_url を、agents を呼ぶ宛先と ID トークンの audience に使う。デプロイ時に差し替える)",
            "config/params.toml: %s" % configured,
            "AGENTS_URL: %s" % expected,
        )
    ok("config/params.toml の public_base_url が AGENTS_URL と一致(%s)" % expected)


def check_iam(name):
    service = data("svc-" + name)
    policy = data("iam-" + name)
    web_member = "serviceAccount:" + WEB_SA
    invokers = set()
    problems = []
    for binding in policy.get("bindings") or []:
        members = set(binding.get("members") or [])
        if binding.get("role") == "roles/run.invoker":
            invokers |= members
        public = members & {"allUsers", "allAuthenticatedUsers"}
        if public:
            problems.append("%s が %s に付いている(公開されている)" % (binding.get("role"), ", ".join(sorted(public))))
    if invokers != {web_member}:
        problems.append("roles/run.invoker の主体: %s(web の SA %s だけのはず)" % (", ".join(sorted(invokers)) or "なし", WEB_SA))
    if str(annotations_of(service).get("run.googleapis.com/invoker-iam-disabled", "")).strip().lower() == "true":
        problems.append("起動元の IAM の確認が無効(run.googleapis.com/invoker-iam-disabled=true)")
    if problems:
        ng("%s の起動元が web のサービスアカウントだけになっていない" % name, *problems)
    ok("%s の roles/run.invoker は web の SA だけ(公開の設定なし)" % name)


def check_iam_agents():
    check_iam("agents")


def check_iam_vault():
    check_iam("vault")


def check_vault_command():
    tokens = command_tokens(containers(data("svc-vault"))[0])
    if "vault.app:create_app_from_env" in " ".join(tokens) and "--factory" in tokens:
        ok("起動コマンドは vault.app:create_app_from_env --factory")
    ng(
        "金庫の起動コマンドが uvicorn vault.app:create_app_from_env --factory になっていない(既定の CMD は web の起動になる)",
        "command・args: %s" % (" ".join(tokens) or "指定なし"),
    )


def check_ttl(name, label, groups):
    states = {}
    for field in data(name):
        found = re.search(r"/collectionGroups/([^/]+)/fields/([^/]+)$", str(field.get("name", "")))
        if found:
            states[(found.group(1), found.group(2))] = (field.get("ttlConfig") or {}).get("state")
    problems = []
    for group in groups:
        state = states.get((group, "ttl_at"))
        if state is None:
            problems.append("%s の ttl_at に TTL ポリシーがない" % group)
        elif state != "ACTIVE":
            problems.append("%s の ttl_at の TTL ポリシーが %s(ACTIVE でない。作成中なら、少し待って再実行)" % (group, state))
    if problems:
        ng("%s の Firestore の TTL ポリシーが足りない" % label, *problems)
    ok("%s: %s の ttl_at がすべて ACTIVE" % (label, "・".join(groups)))


def check_ttl_default():
    check_ttl("ttl-default", "(default)", ("stages", "llm_call_counters", "rate_limits"))


def check_ttl_vault():
    check_ttl("ttl-vault", "vault-db", ("negotiations", "events", "idempotency"))


def check_cache_config():
    body = data("cache-config")
    if isinstance(body, dict) and body.get("disableCache") is True:
        ok("disableCache=true")
    ng("disableCache が true でない(キャッシュが有効のとき、GET の応答に disableCache は出ない。Vertex AI の暗黙のキャッシュが、面談の入出力を最長 24 時間メモリに持つ)")


def check_no_aiohttp():
    rc = read("uv-show", "rc", "none")
    err = read("uv-show", "err").lower()
    problems = []
    if rc == "0":
        problems.append("uv pip show aiohttp: インストールされている")
    elif not (rc == "1" and "not found" in err):
        problems.append("uv pip show aiohttp の結果を解釈できない(終了コード %s: %s)。リポジトリの直下で uv sync してから再実行" % (rc, read("uv-show", "err")))
    try:
        with open(ROOT + "/uv.lock", encoding="utf-8") as handle:
            if re.search(r'^name = "aiohttp"$', handle.read(), re.M):
                problems.append("uv.lock に aiohttp がある(イメージは uv sync --frozen で作るので、入ってしまう)")
    except OSError:
        problems.append("uv.lock を読めない")
    if problems:
        ng("aiohttp が入ると、google-genai が接続エラーを内部で 1 回再試行し、「1 計上 = 要求 1 回」(§4.1)が崩れる", *problems)
    ok("uv pip show で未インストール・uv.lock にもない(イメージの中は、uv.lock から作るので同じ)")


def check_request_log():
    sink = data("sink-default")
    wanted = [SERVICES["web"], SERVICES["agents"]] + ([SERVICES["vault"]] if MODE == "cloudrun" else [])
    listed = []
    for exclusion in sink.get("exclusions") or []:
        text = str(exclusion.get("filter", ""))
        if exclusion.get("disabled") or ("run.googleapis.com%2Frequests" not in text and "run.googleapis.com/requests" not in text):
            continue
        listed.append(str(exclusion.get("name", "")))
        if "service_name" in text and any(service not in text for service in wanted):
            continue  # サービスを絞った除外で、必要なサービスが足りない
        ok("除外フィルタ %s がある(Cloud Run のリクエストログを _Default に入れない)" % exclusion.get("name"))
    ng(
        "_Default シンクに、Cloud Run のリクエストログ(run.googleapis.com/requests)を除く、有効な除外フィルタがない"
        "(URL の依頼者 ID・交渉 ID がログに残る。I-9。対象のサービス: %s)" % "・".join(wanted),
        "関係しそうな除外: %s" % (", ".join(listed) or "なし"),
    )


def check_vault_attestation_via_web():
    body = data("attestation")
    if not isinstance(body, dict):
        ng("GET /api/tee/attestation の応答が JSON のオブジェクトでない")
    claims = body.get("claims") or {}
    release = body.get("release") or {}
    if body.get("verified") is not True:
        ng("web が金庫の attestation を検証できていない(verified が true でない)", "reason: %s" % body.get("reason"), "image_digest: %s" % claims.get("image_digest"))
    ok(
        "verified=true(digest %s・hwmodel %s・コミット %s)"
        % (claims.get("image_digest"), claims.get("hwmodel"), release.get("commit") or "対応表になし")
    )


def check_vertex_quota():
    skip("gcloud では確かめられない。コンソールの「割り当て」に gemini-3.5-flash の行がないこと(I-20: 動的共有クォータなので、申請しない)")


def check_thinking_usage():
    skip(
        "本物の Gemini を呼ぶので、ここでは確かめない。デプロイした agents を 1 回呼び(ID トークンつきの A2A。scripts/tee_probe_client.py の流儀)、"
        "応答の usage.thoughts_tokens が、config/params.toml の [agents.cost_targets] thinking_tokens_baseline の半分以下であることを見る。"
        "手元で同じ基準を判定するのは uv run python scripts/run_demo.py --case 1 --live --runs 2 --judge(DV-15)"
    )


def check_r7_client_ip():
    skip(
        "web に診断用の口がない(X-Forwarded-For の末尾がクライアント IP であることは、実測できない)。"
        "前提は Cloud Run の直接公開: web の前に外部ロードバランサを置かない(置くと、末尾が LB の IP になり、IP ごとのレート制限が全員で 1 枠になる。I-21)"
    )


def check_submission_checklist():
    skip("人が確認する。提出物 6 点: GitHub・デプロイ URL・説明文・信頼境界図・3 分デモ動画・Zenn 記事(チェックリストのファイルはまだない)")


# ---- TEE の照合 (a)〜(i) ----

ATTESTATION_ISSUER = "https://confidentialcomputing.googleapis.com/"
ATTESTATION_AUDIENCES = ["https://sts.googleapis.com"]
ATTRIBUTE_MAPPING = {
    "google.subject": '"gcpcs::"+assertion.submods.container.image_digest+"::"+assertion.submods.gce.project_number+"::"+assertion.submods.gce.instance_id',
    "attribute.image_digest": "assertion.submods.container.image_digest",
}


def attribute_condition():
    return (
        "assertion.swname == 'CONFIDENTIAL_SPACE' && 'STABLE' in assertion.submods.confidential_space.support_attributes && "
        "assertion.dbgstat == 'disabled-since-boot' && assertion.hwmodel in ['GCP_AMD_SEV','GCP_INTEL_TDX'] && "
        "assertion.submods.gce.project_id == '%s' && '%s' in assertion.google_service_accounts" % (PROJECT_ID, VAULT_SA)
    )


def check_tee_a():
    providers = [p for p in data("wif-providers") if p.get("state", "ACTIVE") != "DELETED"]
    names = sorted(str(p.get("name", "")).rsplit("/", 1)[-1] for p in providers)
    if names != [PROVIDER]:
        ng("プール %s のプロバイダが %s の 1 件だけでない(別のプロバイダを足すと、TEE を通らずに同じ principalSet になれる。C-62)" % (POOL, PROVIDER), "いまのプロバイダ: %s" % (", ".join(names) or "なし"))
    provider = providers[0]
    oidc = provider.get("oidc") or {}
    problems = []
    if provider.get("disabled"):
        problems.append("プロバイダが無効になっている")
    if oidc.get("issuerUri") != ATTESTATION_ISSUER:
        problems.append("発行元(issuerUri): %s(期待: %s)" % (shown(oidc.get("issuerUri")), ATTESTATION_ISSUER))
    if list(oidc.get("allowedAudiences") or []) != ATTESTATION_AUDIENCES:
        problems.append("許す audience: %s(期待: %s)" % (oidc.get("allowedAudiences"), ATTESTATION_AUDIENCES))
    if provider.get("attributeMapping") != ATTRIBUTE_MAPPING:
        problems.append("attribute-mapping が本番の文字列と違う: %s" % json.dumps(provider.get("attributeMapping"), ensure_ascii=False))
    if provider.get("attributeCondition") != attribute_condition():
        problems.append("attribute-condition が本番の文字列と違う")
        problems.append("  実際: %s" % provider.get("attributeCondition"))
        problems.append("  期待: %s" % attribute_condition())
    if problems:
        ng("WIF のプロバイダの設定が本番の文字列と完全一致しない", *problems)
    ok("%s/%s の 1 件だけ。発行元・audience・mapping・condition が本番の文字列と完全一致" % (POOL, PROVIDER))


def norm(identity):
    """主体の表記をそろえる(メールは、型の接頭辞 user: serviceAccount: group: を除いて小文字。principalSet はそのまま)。"""
    identity = str(identity).strip()
    if identity.startswith(("principalSet://", "principal://")):
        return identity
    if ":" in identity:
        identity = identity.split(":", 1)[1].strip()
    return identity.lower() if "@" in identity else identity


def active_digests():
    path = ENV.get("RELEASES_FILE", ROOT + "/deploy/vault-releases.json")
    try:
        with open(path, encoding="utf-8") as handle:
            releases = json.load(handle)["releases"]
        return sorted(
            {
                release["digest"]
                for release in releases
                if isinstance(release, dict) and release.get("status", "active") == "active" and isinstance(release.get("digest"), str)
            }
        )
    except (OSError, ValueError, KeyError, TypeError):
        ng("%s を読めない(deploy/vault-releases.json の形ではない)" % path)


def expected_principals():
    """(承認済みのオーナー〔正規化済み〕, active なダイジェストの principalSet の一覧)。deploy/expected-kms-principals.json は、手で書く正本。"""
    path = ENV.get("EXPECTED_FILE", ROOT + "/deploy/expected-kms-principals.json")
    try:
        with open(path, encoding="utf-8") as handle:
            spec = json.load(handle)
    except (OSError, ValueError):
        ng("%s を読めない(JSON として)" % path)
    owners = spec.get("owners") if isinstance(spec, dict) else None
    pattern = spec.get("principal_set_pattern") if isinstance(spec, dict) else None
    if not (
        isinstance(owners, list)
        and owners
        and all(isinstance(owner, str) and owner.strip() for owner in owners)
        and isinstance(pattern, str)
        and "{digest}" in pattern
    ):
        ng("deploy/expected-kms-principals.json の形が違う", "owners: 承認済みのオーナーのメールの一覧(1 件以上)、principal_set_pattern: {digest} を含む文字列")
    if any(("<" in owner or ">" in owner) for owner in owners) or "<" in pattern or ">" in pattern:
        ng("deploy/expected-kms-principals.json が雛形のまま(< > の部分、オーナーのメールとプロジェクト番号を埋める。README の「期待する主体の正本」)")
    return sorted({norm(owner) for owner in owners}), [pattern.replace("{digest}", digest) for digest in active_digests()]


def analysis_scope():
    """Policy Analyzer の範囲の説明。プロジェクトが組織の配下なのに ORG_ID がなければ NG(--project の範囲では、組織・フォルダに付いた IAM が
    解析に入らず、全階層の列挙にならない。気づかずに、弱い確認を通さないため)。"""
    organization = ENV.get("ORG_ID", "")
    if organization:
        return "組織 %s" % organization
    if read("ancestors", "rc", "none") != "0":
        return "プロジェクトのみ(祖先を確かめられなかった。組織の配下なら ORG_ID を設定する)"
    try:
        with open("%s/ancestors.out" % WORK, encoding="utf-8", errors="replace") as handle:
            found = [item.get("id") for item in json.load(handle) if item.get("type") == "organization"]
    except (OSError, ValueError, AttributeError):
        return "プロジェクトのみ(祖先の出力を読めなかった。組織の配下なら ORG_ID を設定する)"
    if found:
        ng(
            "プロジェクトは組織 %s の配下だが、ORG_ID が未設定(--project の範囲では、組織・フォルダに付いた IAM が解析に入らず、全階層の列挙にならない)" % found[0],
            "ORG_ID=%s を設定して再実行する(P-13)" % found[0],
        )
    return "プロジェクトのみ(組織の配下ではない)"


def analysis_identities(name):
    """Policy Analyzer(--show-response)の応答から、権限を持つ主体の集合を取る。解析が未完了なら NG。範囲の説明は analysis_scope。"""
    analysis_scope()
    body = data(name)
    if not isinstance(body, dict) or not isinstance(body.get("mainAnalysis"), dict):
        ng("Policy Analyzer の出力が想定の形でない(--show-response の mainAnalysis がない)")
    main = body["mainAnalysis"]
    results = main.get("analysisResults") or []
    incomplete = []
    if body.get("fullyExplored") is not True:
        incomplete.append("fullyExplored=%s" % body.get("fullyExplored"))
    if main.get("fullyExplored") is False:
        incomplete.append("mainAnalysis.fullyExplored=false")
    incomplete += ["analysisResults[%d].fullyExplored=false" % index for index, result in enumerate(results) if result.get("fullyExplored") is False]
    if incomplete:
        ng(
            "Policy Analyzer の解析が未完了(全階層・custom role まで調べ切れていない。X-71・X-75)",
            *(incomplete + ["nonCriticalErrors: %d 件" % len(main.get("nonCriticalErrors") or [])])
        )
    found = set()
    for result in results:
        listed = (result.get("identityList") or {}).get("identities") or []
        names = [item.get("name") for item in listed if item.get("name")]
        found.update(names or (result.get("iamBinding") or {}).get("members") or [])
    return found


def check_tee_b():
    owners, principal_sets = expected_principals()
    actual = {norm(identity) for identity in analysis_identities("kms-analysis")}
    expected = set(owners) | set(principal_sets)
    if actual != expected:
        details = ["想定外(実際にあって、正本にない): %s" % name for name in sorted(actual - expected)]
        details += ["足りない(正本にあって、実際にない): %s" % name for name in sorted(expected - actual)]
        ng("KEK を使える主体が、期待集合(承認済みのオーナー + active なダイジェストの principalSet)と一致しない", *details)
    ok(
        "KEK を使える主体は %d 件(オーナー %d・principalSet %d)で、期待集合と完全一致。解析は完了(fullyExplored)。範囲: %s"
        % (len(actual), len(owners), len(principal_sets), analysis_scope())
    )


def check_tee_b_pool():
    owners, _ = expected_principals()
    actual = analysis_identities("pool-analysis")
    extra = sorted(identity for identity in actual if norm(identity) not in owners)
    if extra:
        ng("プール・プロバイダを変えられる主体に、承認済みのオーナー以外がいる(C-62)", *["想定外: %s" % identity for identity in extra])
    ok("プール・プロバイダを変えられる主体は %d 件で、承認済みのオーナーだけ。解析は完了。範囲: %s" % (len(actual), analysis_scope()))


def check_tee_c():
    actual = {norm(identity) for identity in analysis_identities("kms-analysis")}
    bad = [email for email in (VAULT_SA, WEB_SA) if norm("serviceAccount:" + email) in actual]
    if bad:
        ng("KEK を使える主体に、金庫の VM の SA か web の SA が入っている(運営者が同じ SA で別の VM を作って復号できる)", *bad)
    ok("金庫の VM の SA(%s)と web の SA(%s)は、KEK を使える主体にない" % (VAULT_SA, WEB_SA))


def check_tee_d():
    key = data("kms-key")
    primary = key.get("primary") or {}
    name = primary.get("name")
    if not name:
        ng("KEK に primary の版がない")
    problems = []
    if primary.get("state") != "ENABLED":
        problems.append("primary の版の状態が ENABLED でない(%s)" % primary.get("state"))
    live = [
        "%s(%s)" % (version.get("name", "").rsplit("/", 1)[-1], version.get("state"))
        for version in data("kms-versions")
        if version.get("name") != name and version.get("state") not in ("DISABLED", "DESTROY_SCHEDULED", "DESTROYED")
    ]
    if live:
        problems.append("primary 以外に、無効化されていない版がある: %s(debug の間の DEK を包んだ版は、無効化する。手順 D)" % ", ".join(live))
    if problems:
        ng("KEK の版の状態が、本番切り替え後のものでない", *problems)
    ok("primary は %s(ENABLED)。ほかの版はすべて DISABLED・DESTROY_SCHEDULED・DESTROYED" % name.rsplit("/", 1)[-1])


def tee_metadata(vm):
    return {item.get("key"): item.get("value", "") for item in ((vm.get("metadata") or {}).get("items") or [])}


def vm_digest(vm):
    found = re.search(r"@(sha256:[0-9a-f]{64})$", tee_metadata(vm).get("tee-image-reference", ""))
    return found.group(1) if found else None


def check_tee_e():
    vm = data("vm")
    disk = data("vm-disk")
    problems = []
    interfaces = vm.get("networkInterfaces") or []
    if not interfaces:
        problems.append("ネットワークインターフェースがない")
    for interface in interfaces:
        if interface.get("accessConfigs") or interface.get("ipv6AccessConfigs"):
            problems.append("外部 IP がある(accessConfigs)")
    image = str(disk.get("sourceImage", ""))
    if "/confidential-space-images/" not in image or "debug" in image.rsplit("/", 1)[-1]:
        problems.append("ブートディスクの元イメージが、本番の Confidential Space(confidential-space)でない: %s" % (image.rsplit("/", 1)[-1] or "取れない"))
    reference = tee_metadata(vm).get("tee-image-reference", "")
    digest = vm_digest(vm)
    if digest is None:
        problems.append("tee-image-reference が digest(@sha256:...)の参照でない: %s" % (reference or "未設定"))
    elif digest not in active_digests():
        problems.append("tee-image-reference の digest(%s)が deploy/vault-releases.json の active にない" % digest)
    web_base = plain(env_of(data("svc-web")), "VAULT_BASE_URL") or ""
    internal_ip = (interfaces[0].get("networkIP") if interfaces else None) or ""
    if urlsplit(web_base).hostname != internal_ip:
        problems.append("web の VAULT_BASE_URL の宛先(%s)が、この VM の内部 IP(%s)と違う" % (urlsplit(web_base).hostname, internal_ip or "取れない"))
    if problems:
        ng("金庫の VM が、本番の構成でない", *problems)
    ok("外部 IP なし・本番イメージ(%s)・digest %s は active・web の宛先 %s と一致(VM の状態: %s)" % (image.rsplit("/", 1)[-1], digest, internal_ip, vm.get("status")))


def check_tee_f():
    rules = data("firewall")
    if rules:
        ng("ファイアウォール規則 %s が残っている(金庫に手元から届く経路。検証が終わったら消す。手順 F)" % ENV.get("IAP_FIREWALL_RULE", "allow-iap-to-vault"))
    ok("%s は存在しない" % ENV.get("IAP_FIREWALL_RULE", "allow-iap-to-vault"))


def principal_of(entry):
    info = (entry.get("protoPayload") or {}).get("authenticationInfo") or {}
    return info.get("principalSubject") or info.get("principalEmail") or "(不明)"


def check_tee_g():
    policy = data("project-iam")
    problems = []
    seen = set()
    for config in policy.get("auditConfigs") or []:
        if config.get("service") not in ("cloudkms.googleapis.com", "allServices"):
            continue
        for log_config in config.get("auditLogConfigs") or []:
            seen.add(log_config.get("logType"))
            if log_config.get("exemptedMembers"):
                problems.append("%s の %s に exemptedMembers がある(%d 件)" % (config.get("service"), log_config.get("logType"), len(log_config["exemptedMembers"])))
    for log_type in ("DATA_READ", "DATA_WRITE"):
        if log_type not in seen:
            problems.append("%s が有効でない(cloudkms.googleapis.com か allServices の auditConfigs にない)" % log_type)
    others = data("audit-others")
    mine = data("audit-expected")
    if not mine:
        problems.append("本番の VM の subject による Encrypt・Decrypt の記録が、新しい版の後に 1 件もない(監査ログが出ていない・VM がまだ鍵を使っていない)")
    if others:
        principals = sorted({principal_of(entry) for entry in others})
        problems.append("新しい版の後に Encrypt・Decrypt を成功させた、本番の VM 以外の主体がいる(%d 件): %s" % (len(others), ", ".join(principals[:10])))
    if problems:
        ng("KMS の Data Access 監査ログの設定か、鍵を使った主体が、期待と違う(置かれた DEK・オーナーの復号の検出。C-63・X-78)", *problems)
    ok("DATA_READ・DATA_WRITE が有効で exemptedMembers なし。新しい版の後の Encrypt・Decrypt は、本番の VM の subject だけ(%d 件以上)" % len(mine))


def primary_name():
    primary = (data("kms-key").get("primary") or {})
    if not primary.get("name"):
        ng("KEK に primary の版がない")
    return primary["name"]


def check_tee_h():
    stored = (((data("dek-doc").get("fields") or {}).get("kek_version")) or {}).get("stringValue")
    if not stored:
        ng("_tee/dek に kek_version がない(金庫は起動できない)")
    if stored != primary_name():
        ng("_tee/dek.kek_version が KEK の primary の版と違う(古い版の DEK を書き戻された・版を切り替えた後に DEK を作り直していない)", "kek_version: %s" % stored, "primary: %s" % primary_name())
    ok("_tee/dek.kek_version が primary の版(%s)と一致" % stored.rsplit("/", 1)[-1])


def check_tee_h_selftest_exists():
    if read("selftest-before", "http") != "200":
        ng("再起動の前に _tee/selftest がない(HTTP %s)。金庫が一度も起動していないか、自己試験の文書が消えている" % read("selftest-before", "http", "取れない"))
    ok("_tee/selftest がある")


def check_tee_h_reset():
    text = read("selftest-log", "out")
    if "sealing self-test ok" not in text:
        ng("再起動の後に sealing self-test ok が出ていない(金庫が起動し直せない・既存の暗号文が開かない。launcher のログを読む)")
    if "sealing self-test: created the probe" in text:
        ng("再起動の後に、自己試験の文書を作り直した(sealing self-test: created the probe)。再起動の前の暗号文が開くことの確認になっていない")
    ok("再起動の後に sealing self-test ok(再起動の前にあった _tee/selftest が開いた)")


def canonical_rule(rule):
    deny = (rule or {}).get("denyRule") or {}
    return {"denyRule": {key: (sorted(value) if isinstance(value, list) else value) for key, value in deny.items()}}


def check_tee_i():
    # gcloud iam policies list は、ポリシーのメタデータだけを返し、rules は含まない(IAM v2 の API の仕様)。本文は get で取る。
    if read("deny-policy", "rc", "none") != "0":
        ng(
            "拒否ポリシー %s を取得できない(ない・権限がない。プロジェクトのオーナーが KEK を復号できてしまう。手順 G。P-13)" % DENY_POLICY,
            read("deny-policy", "err") or "gcloud の終了コード %s" % read("deny-policy", "rc", "none"),
        )
    policy = data("deny-policy")
    if not isinstance(policy, dict) or str(policy.get("name", "")).rsplit("/", 1)[-1] != DENY_POLICY:
        ng("取得した拒否ポリシーの name が %s でない" % DENY_POLICY)
    mine = [policy]
    expected = {
        "denyRule": {
            "deniedPrincipals": ["principalSet://goog/public:all"],
            "exceptionPrincipals": ["principalSet://iam.googleapis.com/projects/%s/locations/global/workloadIdentityPools/%s/*" % (PROJECT_NUMBER, POOL)],
            "deniedPermissions": [
                "cloudkms.googleapis.com/cryptoKeyVersions.useToDecrypt",
                "cloudkms.googleapis.com/cryptoKeyVersions.useToEncrypt",
            ],
        }
    }
    rules = [canonical_rule(rule) for rule in (mine[0].get("rules") or [])]
    if rules != [canonical_rule(expected)]:
        ng("拒否ポリシーの本文が手順 G と違う", "実際の rules: %s" % json.dumps(mine[0].get("rules"), ensure_ascii=False), "期待: %s" % json.dumps(expected, ensure_ascii=False))
    ok("%s があり、本文(拒否: 全主体。例外: プール %s のワークロード。権限: useToDecrypt・useToEncrypt)が手順 G と一致" % (DENY_POLICY, POOL))


# ---- 値を取り出す(bash が、次の gcloud の引数を作るために使う) ----


def value_service_url(name):
    urls = sorted(service_urls(data("svc-" + name)))
    if not urls:
        ng("%s の Cloud Run サービスの URL(status.url)を取れない" % name)
    return urls[0]


def value_primary_create_time():
    created = (data("kms-key").get("primary") or {}).get("createTime")
    if not created:
        ng("KEK の primary の版の createTime を取れない")
    return created


def value_vm_subject():
    vm = data("vm")
    digest = vm_digest(vm)
    instance_id = str(vm.get("id", ""))
    if not (digest and instance_id and PROJECT_NUMBER):
        ng("本番の VM の subject を作れない(tee-image-reference の digest・インスタンス ID・プロジェクト番号のどれかがない)")
    return "principal://iam.googleapis.com/projects/%s/locations/global/workloadIdentityPools/%s/subject/gcpcs::%s::%s::%s" % (
        PROJECT_NUMBER,
        POOL,
        digest,
        PROJECT_NUMBER,
        instance_id,
    )


def value_boot_disk_name():
    disks = data("vm").get("disks") or []
    source = str(disks[0].get("source", "")) if disks else ""
    if not source:
        ng("VM のブートディスクを取れない")
    return source.rsplit("/", 1)[-1]


VALUES = {
    "service-url": value_service_url,
    "primary-create-time": value_primary_create_time,
    "vm-subject": value_vm_subject,
    "boot-disk-name": value_boot_disk_name,
}


def main(argv):
    kind = argv[1]
    if kind == "expected":
        try:
            owners, principal_sets = expected_principals()
        except Done as done:
            sys.stdout.write("%s\n" % done.message)
            for line in done.details:
                sys.stdout.write("    %s\n" % line)
            return 1
        for owner in owners:
            print("owner: %s" % owner)
        for principal_set in principal_sets:
            print("principalSet: %s" % principal_set)
        return 0
    if kind == "value":
        try:
            print(VALUES[argv[2]](*argv[3:]))
        except Done as done:
            sys.stderr.write("%s\n" % done.message)
            return 1
        return 0
    item = argv[2]
    function = globals().get("check_" + (argv[3] if len(argv) > 3 else item).replace("-", "_"))
    try:
        if function is None:
            raise RuntimeError("no such check")
        function()
        raise RuntimeError("the check returned no result")
    except Done as done:
        status, message, details = done.status, done.message, done.details
    except Exception as exc:  # 出力の形が想定と違う(KeyError など)。値は出さず、型名だけ
        status, message, details = "NG", "出力の形が想定と違う、または内部エラー(%s)" % type(exc).__name__, ()
    print("[%s] %s: %s" % (status, item, message))
    for line in details:
        print("    %s" % line)
    return 0


sys.exit(main(sys.argv))
PYEOF

# ----------------------------------------------------------------------
# --show-expected(gcloud を呼ばない)
# ----------------------------------------------------------------------
if [ "$SHOW_EXPECTED" -eq 1 ]; then
  exec "$PYTHON3" -c "$PY_EVAL" expected
fi

# ----------------------------------------------------------------------
# 環境変数の確認(足りなければ、全部まとめて言って、終了コード 2)
# ----------------------------------------------------------------------
VAULT_MODE="${VAULT_MODE:-tee}"
case "$VAULT_MODE" in
  tee | cloudrun) ;;
  *)
    echo "VAULT_MODE は tee か cloudrun にしてください(値: $VAULT_MODE)" >&2
    exit 2
    ;;
esac
missing=""
for name in PROJECT_ID REGION WEB_URL AGENTS_URL; do
  [ -n "${!name:-}" ] || missing="$missing $name"
done
if [ "$VAULT_MODE" = tee ] && [ -z "${ZONE:-}" ]; then
  missing="$missing ZONE(VAULT_MODE=tee では必須)"
fi
if [ -n "$missing" ]; then
  echo "必須の環境変数が足りません:$missing" >&2
  echo "例: PROJECT_ID=<プロジェクト ID> REGION=asia-northeast1 ZONE=asia-northeast1-b WEB_URL=https://<web の URL> AGENTS_URL=https://<agents の URL> VAULT_MODE=tee bash scripts/deploy_check.sh" >&2
  exit 2
fi
for name in WEB_URL AGENTS_URL; do
  case "${!name}" in
    http://* | https://*) ;;
    *)
      echo "$name は http:// か https:// で始まる URL にしてください(値: ${!name})" >&2
      exit 2
      ;;
  esac
done

WEB_URL="${WEB_URL%/}"
AGENTS_URL="${AGENTS_URL%/}"
ZONE="${ZONE:-}"
WEB_SA="${WEB_SA:-web-run@${PROJECT_ID}.iam.gserviceaccount.com}"
VAULT_SA="${VAULT_SA:-vault-tee@${PROJECT_ID}.iam.gserviceaccount.com}"
WEB_SERVICE="${WEB_SERVICE:-web}"
AGENTS_SERVICE="${AGENTS_SERVICE:-agents}"
VAULT_SERVICE="${VAULT_SERVICE:-vault}"
CACHE_CONFIG_URL="${CACHE_CONFIG_URL:-https://aiplatform.googleapis.com/v1/projects/${PROJECT_ID}/cacheConfig}"
FIRESTORE_DOCS="https://firestore.googleapis.com/v1/projects/${PROJECT_ID}/databases/vault-db/documents"
HTTP_TIMEOUT="${HTTP_TIMEOUT:-60}"
HEALTH_PATH="${HEALTH_PATH:-/health}"
RESET_POLLS="${RESET_POLLS:-36}"
POLL_INTERVAL_SECONDS="${POLL_INTERVAL_SECONDS:-10}"
PROJECT_NUMBER="${PROJECT_NUMBER:-}"
ORG_ID="${ORG_ID:-}"
VAULT_URL="${VAULT_URL:-}"
export PROJECT_ID REGION ZONE VAULT_MODE WEB_URL AGENTS_URL WEB_SA VAULT_SA WEB_SERVICE AGENTS_SERVICE VAULT_SERVICE
export POOL PROVIDER KEY DENY_POLICY IAP_FIREWALL_RULE PROJECT_NUMBER ORG_ID

# gcloud が対話の質問で止まらないようにする(質問は、既定の答え = 何もしない、になる)
export CLOUDSDK_CORE_DISABLE_PROMPTS=1

WORK="$(mktemp -d "${TMPDIR:-/tmp}/deploy-check.XXXXXX")"
chmod 700 "$WORK"
export WORK
trap 'rm -rf "$WORK"' EXIT

OK_COUNT=0
NG_COUNT=0
SKIP_COUNT=0
ACCESS_TOKEN=""

# ----------------------------------------------------------------------
# 部品
# ----------------------------------------------------------------------
rf() { # rf FILE [既定値]: ファイルの中身(なければ既定値)
  if [ -f "$1" ]; then cat "$1"; else printf '%s' "${2:-}"; fi
}

first_line() { # first_line FILE: 標準エラーの 1 行目(ERROR の行があればそれ)。200 文字まで
  if [ -f "$1" ]; then
    { grep -m1 '^ERROR' "$1" || head -n 1 "$1"; } 2>/dev/null | cut -c1-200
  fi
  return 0
}

emit() { # emit STATUS ID MESSAGE [詳細の行...]
  local status="$1" id="$2" message="$3" line
  shift 3
  case "$status" in
    OK) OK_COUNT=$((OK_COUNT + 1)) ;;
    SKIP) SKIP_COUNT=$((SKIP_COUNT + 1)) ;;
    *) NG_COUNT=$((NG_COUNT + 1)) ;;
  esac
  printf '[%s] %s: %s\n' "$status" "$id" "$message"
  for line in "$@"; do
    printf '    %s\n' "$line"
  done
  return 0
}

fetch() { # fetch NAME ラベル コマンド [引数...]: 実行して、出力・標準エラーの先頭行・終了コードを $WORK に取る(同じ NAME は 1 回だけ)
  local name="$1" label="$2" rc=0
  shift 2
  if [ -f "$WORK/$name.rc" ]; then return 0; fi
  printf '%s' "$label" >"$WORK/$name.cmd"
  "$@" >"$WORK/$name.out" 2>"$WORK/$name.errfull" </dev/null || rc=$?
  first_line "$WORK/$name.errfull" >"$WORK/$name.err"
  rm -f "$WORK/$name.errfull"
  printf '%s' "$rc" >"$WORK/$name.rc"
  return 0
}

refetch() { # refetch NAME ...: 取り直す
  rm -f "$WORK/$1".rc "$WORK/$1".out "$WORK/$1".err "$WORK/$1".http
  fetch "$@"
}

fetch_http() { # fetch_http NAME URL TOKEN [curl の引数...]: GET して、本文・HTTP ステータス・終了コードを取る。TOKEN は標準入力のヘッダで渡す
  local name="$1" url="$2" token="$3" rc=0 code=""
  shift 3
  if [ -f "$WORK/$name.rc" ]; then return 0; fi
  printf 'GET %s' "$url" >"$WORK/$name.cmd"
  if [ -n "$token" ]; then
    code="$(printf 'Authorization: Bearer %s\n' "$token" | "$CURL" -sS --max-time "$HTTP_TIMEOUT" -o "$WORK/$name.out" -w '%{http_code}' -H @- "$@" "$url" 2>"$WORK/$name.errfull")" || rc=$?
  else
    code="$("$CURL" -sS --max-time "$HTTP_TIMEOUT" -o "$WORK/$name.out" -w '%{http_code}' "$@" "$url" 2>"$WORK/$name.errfull" </dev/null)" || rc=$?
  fi
  first_line "$WORK/$name.errfull" >"$WORK/$name.err"
  rm -f "$WORK/$name.errfull"
  printf '%s' "$code" >"$WORK/$name.http"
  printf '%s' "$rc" >"$WORK/$name.rc"
  return 0
}

py_check() { # py_check ID [Python の確認の名前]: 評価プログラムで、結果の行を出す
  local out
  if ! out="$("$PYTHON3" -c "$PY_EVAL" check "$@" 2>"$WORK/py.err")"; then
    emit NG "$1" "評価プログラム(python3)を実行できなかった: $(first_line "$WORK/py.err")"
    return 0
  fi
  case "$out" in
    "[OK]"*) OK_COUNT=$((OK_COUNT + 1)) ;;
    "[SKIP]"*) SKIP_COUNT=$((SKIP_COUNT + 1)) ;;
    *) NG_COUNT=$((NG_COUNT + 1)) ;;
  esac
  printf '%s\n' "$out"
  return 0
}

py_value() { # py_value 名前 [引数...]: 値を 1 行で出す。取れなければ、理由を $WORK/value.err に書いて 1
  "$PYTHON3" -c "$PY_EVAL" value "$@" 2>"$WORK/value.err"
}

http_ok() { # http_ok ID NAME ラベル [死活確認のパス]: 取った HTTP の応答が 200 なら OK、そうでなければ NG(パスが z で終わる 404 には、予約パスの注意を添える)
  local id="$1" name="$2" label="$3" path="${4:-}" rc code
  rc="$(rf "$WORK/$name.rc" none)"
  code="$(rf "$WORK/$name.http" "")"
  if [ "$rc" != 0 ]; then
    emit NG "$id" "$label に届かない(curl の終了コード ${rc}: $(first_line "$WORK/$name.err"))"
  elif [ "$code" = 200 ]; then
    emit OK "$id" "$label が 200"
  elif [ "$code" = 404 ] && [ "${path%z}" != "$path" ]; then
    emit NG "$id" "$label が 404(200 が期待)" \
      "Cloud Run の run.app では、末尾が z のパス($path)は Google のフロントエンドが予約していて、コンテナに届かない(公式: 末尾が z のパスは避ける)。" \
      "アプリの経路を /health などに変えて、HEALTH_PATH=/health で再実行する"
  else
    emit NG "$id" "$label が ${code}(200 が期待)"
  fi
  return 0
}

access_token() { # アクセストークン(ユーザーのもの)を ACCESS_TOKEN に持つ(出力しない)。取れなければ 1
  if [ -z "$ACCESS_TOKEN" ]; then
    ACCESS_TOKEN="$("$GCLOUD" auth print-access-token 2>"$WORK/access.err" </dev/null)" || ACCESS_TOKEN=""
  fi
  [ -n "$ACCESS_TOKEN" ]
}

identity_token() { # identity_token AUDIENCE: web の SA の ID トークン(impersonation)を標準出力に出す。呼び出し側が変数で受ける(表示しない)
  "$GCLOUD" auth print-identity-token "--impersonate-service-account=$WEB_SA" "--audiences=$1" --include-email 2>"$WORK/idtoken.err" </dev/null
}

need_project_number() { # プロジェクト番号を PROJECT_NUMBER に持つ。取れなければ 1
  if [ -z "$PROJECT_NUMBER" ]; then
    fetch project-number "gcloud projects describe" "$GCLOUD" projects describe "$PROJECT_ID" --format 'value(projectNumber)'
    if [ "$(rf "$WORK/project-number.rc" none)" = 0 ]; then
      PROJECT_NUMBER="$(tr -d '[:space:]' <"$WORK/project-number.out")"
    fi
  fi
  case "$PROJECT_NUMBER" in
    '' | *[!0-9]*) return 1 ;;
  esac
  export PROJECT_NUMBER
  return 0
}

service_name() {
  case "$1" in
    web) printf '%s' "$WEB_SERVICE" ;;
    agents) printf '%s' "$AGENTS_SERVICE" ;;
    *) printf '%s' "$VAULT_SERVICE" ;;
  esac
}

fetch_service() { # fetch_service web|agents|vault
  fetch "svc-$1" "gcloud run services describe $(service_name "$1")" \
    "$GCLOUD" run services describe "$(service_name "$1")" --region "$REGION" --project "$PROJECT_ID" --format json
}

fetch_iam() { # fetch_iam agents|vault
  fetch "iam-$1" "gcloud run services get-iam-policy $(service_name "$1")" \
    "$GCLOUD" run services get-iam-policy "$(service_name "$1")" --region "$REGION" --project "$PROJECT_ID" --format json
}

fetch_key() {
  fetch kms-key "gcloud kms keys describe $KEY" \
    "$GCLOUD" kms keys describe "$KEY" --location "$REGION" --keyring "$KEYRING" --project "$PROJECT_ID" --format json
}

fetch_vm() {
  fetch vm "gcloud compute instances describe $VM_NAME" \
    "$GCLOUD" compute instances describe "$VM_NAME" --zone "$ZONE" --project "$PROJECT_ID" --format json
}

analyze() { # analyze NAME 資源の完全な名前 権限(コンマ区切り): Policy Analyzer。ORG_ID があれば組織、なければプロジェクトの範囲
  local scope="--project=$PROJECT_ID"
  if [ -n "$ORG_ID" ]; then
    scope="--organization=$ORG_ID"
  else
    # 組織の配下なのに ORG_ID がないことに気づけるよう、祖先を取っておく(評価プログラムが、組織があれば NG にする)
    fetch ancestors "gcloud projects get-ancestors" "$GCLOUD" projects get-ancestors "$PROJECT_ID" --format json
  fi
  fetch "$1" "gcloud asset analyze-iam-policy($2)" \
    "$GCLOUD" asset analyze-iam-policy "$scope" "--full-resource-name=$2" "--permissions=$3" --show-response --format=json
}

# ----------------------------------------------------------------------
# 項目(関数名は check_ + id のハイフンをアンダースコアにしたもの)
# ----------------------------------------------------------------------
check_adk_capture() { fetch_service web; fetch_service agents; py_check adk-capture; }
check_log_level() { fetch_service web; fetch_service agents; py_check log-level; }
check_web_workers() { fetch_service web; py_check web-workers; }
check_session_key() { fetch_service web; py_check session-key; }
check_service_auth() { fetch_service web; py_check service-auth; }
check_vertex_env() { fetch_service web; fetch_service agents; py_check vertex-env; }

check_web_vault_env() {
  fetch_service web
  if [ "$VAULT_MODE" = cloudrun ]; then fetch_service vault; fi
  py_check web-vault-env
}

check_agents_url() { py_check agents-url; }
check_iam_agents() { fetch_service agents; fetch_iam agents; py_check iam-agents; }
check_iam_vault() { fetch_service vault; fetch_iam vault; py_check iam-vault; }
check_vault_command() { fetch_service vault; py_check vault-command; }

check_ttl_default() {
  fetch ttl-default "gcloud firestore fields ttls list" \
    "$GCLOUD" firestore fields ttls list --database='(default)' --project "$PROJECT_ID" --format json
  py_check ttl-default
}

check_ttl_vault() {
  fetch ttl-vault "gcloud firestore fields ttls list" \
    "$GCLOUD" firestore fields ttls list --database=vault-db --project "$PROJECT_ID" --format json
  py_check ttl-vault
}

check_cache_config() {
  if ! access_token; then
    emit NG cache-config "アクセストークンを取れない(gcloud auth print-access-token: $(first_line "$WORK/access.err"))"
    return 0
  fi
  fetch_http cache-config "$CACHE_CONFIG_URL" "$ACCESS_TOKEN"
  py_check cache-config
}

check_vertex_quota() { py_check vertex-quota; }
check_thinking_usage() { py_check thinking-usage; }

check_no_aiohttp() {
  if ! command -v "$UV" >/dev/null 2>&1; then
    emit NG no-aiohttp "uv が見つからない(uv pip show aiohttp を実行できない)"
    return 0
  fi
  (cd "$ROOT" && fetch uv-show "uv pip show aiohttp" "$UV" pip show aiohttp)
  py_check no-aiohttp
}

check_request_log() {
  fetch sink-default "gcloud logging sinks describe _Default" \
    "$GCLOUD" logging sinks describe _Default --project "$PROJECT_ID" --format json
  py_check request-log
}

check_r7_client_ip() { py_check r7-client-ip; }

check_healthz_web() {
  fetch_http healthz-web "${WEB_URL}${HEALTH_PATH}" ""
  http_ok healthz-web healthz-web "web の ${HEALTH_PATH}" "$HEALTH_PATH"
}

check_healthz_agents() {
  local token=""
  token="$(identity_token "$AGENTS_URL")" || token=""
  if [ -z "$token" ]; then
    emit NG healthz-agents "agents 用の ID トークンを取れない(web の SA $WEB_SA を impersonate できない。roles/iam.serviceAccountOpenIdTokenCreator が要る: $(first_line "$WORK/idtoken.err"))"
    return 0
  fi
  fetch_http healthz-agents "${AGENTS_URL}${HEALTH_PATH}" "$token"
  http_ok healthz-agents healthz-agents "agents の ${HEALTH_PATH}(ID トークンつき)" "$HEALTH_PATH"
}

check_healthz_vault() {
  local token="" url="$VAULT_URL"
  if [ "$VAULT_MODE" = tee ]; then
    fetch_http attestation "$WEB_URL/api/tee/attestation" ""
    py_check healthz-vault vault_attestation_via_web
    return 0
  fi
  if [ -z "$url" ]; then
    fetch_service vault
    url="$(py_value service-url vault)" || {
      emit NG healthz-vault "$(first_line "$WORK/value.err")"
      return 0
    }
  fi
  token="$(identity_token "$url")" || token=""
  if [ -z "$token" ]; then
    emit NG healthz-vault "金庫用の ID トークンを取れない(web の SA $WEB_SA を impersonate できない: $(first_line "$WORK/idtoken.err"))"
    return 0
  fi
  fetch_http healthz-vault "${url}${HEALTH_PATH}" "$token"
  http_ok healthz-vault healthz-vault "金庫の ${HEALTH_PATH}(ID トークンつき)" "$HEALTH_PATH"
}

check_demo_url() {
  fetch_http demo-url "$WEB_URL/" "" -L --max-redirs 3
  http_ok demo-url demo-url "デモ URL($WEB_URL/)"
}

check_submission_checklist() { py_check submission-checklist; }

check_tee_a() {
  fetch wif-providers "gcloud iam workload-identity-pools providers list" \
    "$GCLOUD" iam workload-identity-pools providers list "--workload-identity-pool=$POOL" --location=global --project "$PROJECT_ID" --format json
  py_check tee-a
}

check_tee_b() {
  analyze kms-analysis "//cloudkms.googleapis.com/projects/${PROJECT_ID}/locations/${REGION}/keyRings/${KEYRING}/cryptoKeys/${KEY}" \
    cloudkms.cryptoKeyVersions.useToDecrypt,cloudkms.cryptoKeyVersions.useToEncrypt
  py_check tee-b
}

check_tee_b_pool() {
  # Policy Analyzer は Workload Identity Pool の full resource name を受け付けない(実測 2026-10-04: INVALID_ARGUMENT)ので、
  # プロジェクトを対象に「プール・プロバイダを変えられる権限」を持つ主体を列挙する(プールの IAM は、プロジェクト〔と上位〕の束縛で決まる)。
  analyze pool-analysis "//cloudresourcemanager.googleapis.com/projects/${PROJECT_ID}" \
    iam.workloadIdentityPoolProviders.create,iam.workloadIdentityPoolProviders.update,iam.workloadIdentityPoolProviders.delete,iam.workloadIdentityPools.update
  py_check tee-b-pool
}

check_tee_c() {
  analyze kms-analysis "//cloudkms.googleapis.com/projects/${PROJECT_ID}/locations/${REGION}/keyRings/${KEYRING}/cryptoKeys/${KEY}" \
    cloudkms.cryptoKeyVersions.useToDecrypt,cloudkms.cryptoKeyVersions.useToEncrypt
  py_check tee-c
}

check_tee_d() {
  fetch_key
  fetch kms-versions "gcloud kms keys versions list" \
    "$GCLOUD" kms keys versions list --location "$REGION" --keyring "$KEYRING" --key "$KEY" --project "$PROJECT_ID" --format json
  py_check tee-d
}

check_tee_e() {
  local disk
  fetch_vm
  fetch_service web
  disk="$(py_value boot-disk-name)" || {
    emit NG tee-e "$(first_line "$WORK/value.err")"
    return 0
  }
  fetch vm-disk "gcloud compute disks describe $disk" \
    "$GCLOUD" compute disks describe "$disk" --zone "$ZONE" --project "$PROJECT_ID" --format json
  py_check tee-e
}

check_tee_f() {
  fetch firewall "gcloud compute firewall-rules list" \
    "$GCLOUD" compute firewall-rules list "--filter=name=$IAP_FIREWALL_RULE" --project "$PROJECT_ID" --format json
  py_check tee-f
}

check_tee_g() {
  local created subject base
  if ! need_project_number; then
    emit NG tee-g "プロジェクト番号を取れない(PROJECT_NUMBER を設定するか、gcloud projects describe の権限を確かめる)"
    return 0
  fi
  fetch_key
  fetch_vm
  fetch project-iam "gcloud projects get-iam-policy" "$GCLOUD" projects get-iam-policy "$PROJECT_ID" --format json
  created="$(py_value primary-create-time)" || {
    emit NG tee-g "$(first_line "$WORK/value.err")"
    return 0
  }
  subject="$(py_value vm-subject)" || {
    emit NG tee-g "$(first_line "$WORK/value.err")"
    return 0
  }
  base="logName=\"projects/${PROJECT_ID}/logs/cloudaudit.googleapis.com%2Fdata_access\""
  base="$base AND protoPayload.serviceName=\"cloudkms.googleapis.com\" AND protoPayload.methodName:(\"Encrypt\" OR \"Decrypt\")"
  base="$base AND protoPayload.resourceName:\"/cryptoKeys/${KEY}\" AND timestamp>=\"${created}\" AND NOT protoPayload.status.code>0"
  fetch audit-others "gcloud logging read(本番の VM 以外の主体)" \
    "$GCLOUD" logging read "$base AND NOT protoPayload.authenticationInfo.principalSubject=\"${subject}\"" --project "$PROJECT_ID" --limit 50 --format json
  fetch audit-expected "gcloud logging read(本番の VM の subject)" \
    "$GCLOUD" logging read "$base AND protoPayload.authenticationInfo.principalSubject=\"${subject}\"" --project "$PROJECT_ID" --limit 1 --format json
  py_check tee-g
}

check_tee_h() {
  fetch_key
  if ! access_token; then
    emit NG tee-h "アクセストークンを取れない(gcloud auth print-access-token: $(first_line "$WORK/access.err"))"
    return 0
  fi
  fetch_http dek-doc "$FIRESTORE_DOCS/_tee/dek" "$ACCESS_TOKEN"
  py_check tee-h
}

check_tee_h_reset() {
  local started filter poll=0
  if [ "$RESET_VAULT" -ne 1 ]; then
    emit SKIP tee-h-reset "金庫の VM を再起動するので、--reset-vault を付けたときだけ行う(AC-22 の合格には、再起動の後の自己試験も要る。審査期間中は付けない)"
    return 0
  fi
  if ! access_token; then
    emit NG tee-h-reset "アクセストークンを取れない(gcloud auth print-access-token: $(first_line "$WORK/access.err"))"
    return 0
  fi
  fetch_http selftest-before "$FIRESTORE_DOCS/_tee/selftest" "$ACCESS_TOKEN"
  if [ "$(rf "$WORK/selftest-before.http" "")" != 200 ]; then
    py_check tee-h-reset tee_h_selftest_exists
    return 0
  fi
  started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "    金庫の VM($VM_NAME)を停止→開始する(--reset-vault。数分、金庫が応えない。reset は使わない: vTPM の DA ロックアウトのカウンタが増える)"
  fetch stop "gcloud compute instances stop $VM_NAME" \
    "$GCLOUD" compute instances stop "$VM_NAME" --zone "$ZONE" --project "$PROJECT_ID"
  if [ "$(rf "$WORK/stop.rc" none)" != 0 ]; then
    emit NG tee-h-reset "金庫の VM を停止できない(gcloud compute instances stop: $(first_line "$WORK/stop.err"))"
    return 0
  fi
  fetch start "gcloud compute instances start $VM_NAME" \
    "$GCLOUD" compute instances start "$VM_NAME" --zone "$ZONE" --project "$PROJECT_ID"
  if [ "$(rf "$WORK/start.rc" none)" != 0 ]; then
    emit NG tee-h-reset "金庫の VM を開始できない(gcloud compute instances start: $(first_line "$WORK/start.err"))"
    return 0
  fi
  filter="logName=\"projects/${PROJECT_ID}/logs/confidential-space-launcher\" AND timestamp>=\"${started}\""
  filter="$filter AND (\"sealing self-test ok\" OR \"sealing self-test: created the probe\")"
  while :; do
    refetch selftest-log "gcloud logging read(launcher のログ)" \
      "$GCLOUD" logging read "$filter" --project "$PROJECT_ID" --limit 50 --format json
    if grep -q 'sealing self-test ok' "$WORK/selftest-log.out" 2>/dev/null; then break; fi
    poll=$((poll + 1))
    if [ "$poll" -ge "$RESET_POLLS" ]; then break; fi
    sleep "$POLL_INTERVAL_SECONDS"
  done
  py_check tee-h-reset
}

check_tee_i() {
  if ! need_project_number; then
    emit NG tee-i "プロジェクト番号を取れない(PROJECT_NUMBER を設定するか、gcloud projects describe の権限を確かめる)"
    return 0
  fi
  fetch deny-policy "gcloud iam policies get $DENY_POLICY" \
    "$GCLOUD" iam policies get "$DENY_POLICY" "--attachment-point=cloudresourcemanager.googleapis.com/projects/${PROJECT_ID}" --kind=denypolicies --format json
  py_check tee-i
}

# ----------------------------------------------------------------------
# 実行
# ----------------------------------------------------------------------
only_has() { # only_has ID: --only で選ばれているか(指定がなければ全部)
  if [ -z "$ONLY" ]; then return 0; fi
  case "$ONLY," in
    *",$1,"*) return 0 ;;
  esac
  return 1
}

run_item() { # run_item ID 対象
  local id="$1" mode="$2"
  if ! only_has "$id"; then return 0; fi
  if [ "$mode" = tee ] && [ "$VAULT_MODE" != tee ]; then
    emit SKIP "$id" "VAULT_MODE=$VAULT_MODE では対象外(TEE 版の金庫だけの確認)"
    return 0
  fi
  if [ "$mode" = cloudrun ] && [ "$VAULT_MODE" != cloudrun ]; then
    emit SKIP "$id" "VAULT_MODE=$VAULT_MODE では対象外(Cloud Run 版の金庫だけの確認。TEE 版の金庫は、アプリの中で ID トークンを検証し、起動コマンドはイメージの ENTRYPOINT)"
    return 0
  fi
  "check_${id//-/_}" || emit NG "$id" "確認の処理が途中で失敗した(スクリプトの内部エラー)"
  return 0
}

echo "デプロイの確認: プロジェクト ${PROJECT_ID}・リージョン ${REGION}・VAULT_MODE=${VAULT_MODE}(web: ${WEB_URL})"
# 表は、ファイル記述子 3 から読む(項目の中の gcloud・curl が、標準入力から表を食べないように)
while IFS='|' read -r id mode _ <&3; do
  [ -n "$id" ] || continue
  run_item "$id" "$mode"
done 3<<EOF
$ITEMS
EOF

echo
echo "結果: OK ${OK_COUNT}・NG ${NG_COUNT}・SKIP ${SKIP_COUNT}"
if [ "$SKIP_COUNT" -gt 0 ]; then
  echo "SKIP は、このスクリプトでは確かめていない(対象外か、人が確認する項目)。AC-22 の合格には、対象の項目を確かめ終えること"
fi
if [ "$NG_COUNT" -gt 0 ]; then
  exit 1
fi
exit 0
