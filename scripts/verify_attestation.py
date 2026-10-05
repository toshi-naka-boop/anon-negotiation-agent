"""金庫(TEE)の attestation を、利用者が自分で確かめるスクリプト(design.md §12.1 の AC-23、research/tee-spike-contract.md §12)。

    uv run python scripts/verify_attestation.py --web https://<web の URL> --project <プロジェクト ID> --service-account <金庫の SA のメール>
    uv run python scripts/verify_attestation.py --direct https://localhost:8443 --project ... --service-account ...   # IAP トンネルの出口

流れ
1. 乱数の nonce(32 バイトを base64url にした 43 文字)を作り、attestation トークン(JWT)を取る。
   - --web: web の GET /api/tee/attestation?nonce= を呼ぶ。web は金庫に転送して、トークンをそのまま返す。web の「検証した」という
     申告は信用せず、トークンをこのスクリプトが自分で検証する。web 経由では、TLS の証明書の結び付き(eat_nonce の 2 つ目)は確かめられない
     (スクリプトは金庫と TLS を張らないので、そのハッシュを知らない)。
   - --direct: 金庫の GET /v1/attestation?nonce= を呼ぶ。TLS は検証せずに証明書を控え(IAP のトンネルは localhost で出るため)、その 1 枚だけを信用する
     接続で呼び、トークンの eat_nonce にその証明書の SHA-256 が入っていることを確かめる(今の TLS の相手が、このトークンの金庫であること)。
2. Google の署名鍵(x509 エンドポイント。kid → PEM)でトークンの署名を検証し、契約 §10 の項目(iss・aud・exp・nonce・swname・dbgstat・hwmodel・
   STABLE・digest ほか)を確かめる。
3. digest を deploy/vault-releases.json から引き、コミットの SHA を表示する(環境変数 GITHUB_REPO_URL があれば、コミットの URL も。
   --check-commit で、GitHub の API でそのコミットが存在することも確かめる)。
4. 全部通れば終了コード 0 と claims の表。1 つでも違えば終了コード 1 と理由(reason)。

--project と --service-account は必須(第三者の検証が、web の検証より弱くならないように。web は VAULT_SERVICE_ACCOUNT・GOOGLE_CLOUD_PROJECT で
同じ照合をする)。トークンの値は、--print-token のときだけ出す(ほかの出力には、claims の値だけを出す)。--allow-debug は、debug イメージのトークン
(dbgstat・STABLE を要求しない)も通す。警告を出す。本番の確認には使わない。

この確認でできるのは、「この digest の TEE が存在し、与えた nonce に応答した」ことまで。「web が、その TEE にだけデータを送っている」ことの
証明にはならない(research/tee-spike.md 6-5)。
"""

import argparse
import json
import os
import secrets
import ssl
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

import httpx  # noqa: E402

from negotiation_core.attestation import (  # noqa: E402
    AttestationError,
    AttestationPolicy,
    SignerCerts,
    VerifiedAttestation,
    active_digests,
    certificate_sha256_of_pem,
    decode_claims_unverified,
    load_releases,
    pinned_ssl_context,
    release_for_digest,
    summarize_claims,
    verify_attestation_token,
)
from negotiation_core.tee_settings import load_tee_settings  # noqa: E402

DEFAULT_RELEASES_PATH = PROJECT_ROOT / "deploy" / "vault-releases.json"
_TIMEOUT_SECONDS = 15.0
_MAX_SHOWN_CHARS = 200

_REASON_TEXT = {
    "malformed": "トークンの形が JWT ではない",
    "signature": "署名が Google の鍵で確かめられない(改ざん・別の発行者・未知の鍵)",
    "expired": "期限(exp・iat)が合わない",
    "audience": "aud が違う",
    "issuer": "iss が違う",
    "nonce": "nonce が違う(古いトークンの使い回しの疑い)",
    "certificate": "TLS の証明書のハッシュがトークンにない(別の相手につながっている疑い)",
    "swname": "swname が CONFIDENTIAL_SPACE ではない(失効したイメージの疑い)",
    "debug": "本番イメージではない(dbgstat が disabled-since-boot ではない)",
    "support_attributes": "STABLE の印がない(失効した・debug のイメージ)",
    "hwmodel": "許可していないハードウェア",
    "image_digest": "イメージの digest が deploy/vault-releases.json にない",
    "override": "コマンドか環境変数が上書きされている",
    "project": "GCP のプロジェクトが違う",
    "service_account": "サービスアカウントが違う",
    "zone": "ゾーンが違う",
    "instance": "インスタンス名が違う",
    "unavailable": "Google の署名鍵を取得できなかった",
}


class _FetchError(Exception):
    """トークンを取れなかった。メッセージは、そのまま表示してよい(トークンの値を含まない)。"""


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="金庫(TEE)の attestation を検証する(AC-23)。", epilog=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--web", metavar="URL", help="web の URL(GET /api/tee/attestation?nonce= を呼ぶ)")
    source.add_argument("--direct", metavar="URL", help="金庫の URL(IAP トンネルの出口。GET /v1/attestation?nonce= を呼ぶ)")
    parser.add_argument("--releases", type=Path, default=DEFAULT_RELEASES_PATH, help="digest → コミットの表(既定 deploy/vault-releases.json)")
    parser.add_argument("--project", required=True, help="GCP のプロジェクト ID(トークンの submods.gce.project_id と照合する)")
    parser.add_argument(
        "--service-account", required=True, help="金庫のサービスアカウントのメール(トークンの google_service_accounts と照合する)"
    )
    parser.add_argument("--hwmodel", action="append", help="許可するハードウェア(繰り返せる。既定は config/params.toml の [vault.tee] allowed_hwmodels)")
    parser.add_argument("--allow-debug", action="store_true", help="debug イメージのトークンも通す(警告つき。本番の確認には使わない)")
    parser.add_argument("--check-commit", action="store_true", help="GitHub の API でコミットの存在も確かめる(環境変数 GITHUB_REPO_URL が要る)")
    parser.add_argument("--print-token", action="store_true", help="トークンの値も出す(既定では出さない)")
    return parser.parse_args(argv)


def _printable(value: object) -> str:
    """表示用の文字列。制御文字(端末の escape など)を含む値が来ても、端末を壊さないように escape し、長すぎるものは切る。"""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=True)
    text = text.encode("unicode_escape").decode("ascii")
    return text if len(text) <= _MAX_SHOWN_CHARS else text[:_MAX_SHOWN_CHARS] + "..."


def _token_from_json(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        raise _FetchError("the response is not JSON") from None
    token = body.get("token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise _FetchError("the response has no token")
    return token


def _fetch_via_web(url: str, nonce: str, transport: httpx.BaseTransport | None) -> str:
    try:
        with httpx.Client(timeout=_TIMEOUT_SECONDS, transport=transport) as client:
            response = client.get(f"{url.rstrip('/')}/api/tee/attestation", params={"nonce": nonce})
    except httpx.HTTPError as exc:
        raise _FetchError(f"could not call the web app ({type(exc).__name__})") from exc
    if response.status_code == 429:
        raise _FetchError("the web app returned 429 (nonce つきの検証は、同じ送信元から 10 秒に 1 回、全体で 2 秒に 1 回まで。少し待ってから、もう一度)")
    if response.status_code != 200:
        raise _FetchError(f"the web app returned {response.status_code}")
    return _token_from_json(response)


def _fetch_direct(url: str, nonce: str) -> tuple[str, str]:
    """金庫に直接。(トークン, 控えた証明書の SHA-256)を返す。"""
    target = httpx.URL(url)
    try:
        pem = ssl.get_server_certificate((target.host, target.port or 443), timeout=_TIMEOUT_SECONDS)
        # trust_env=False: IAP のトンネル(localhost)の呼び出しを、環境のプロキシに横取りさせない
        with httpx.Client(verify=pinned_ssl_context(pem), timeout=_TIMEOUT_SECONDS, trust_env=False) as client:
            response = client.get(str(target.copy_with(path="/v1/attestation", params={"nonce": nonce})))
    except (OSError, httpx.HTTPError) as exc:
        raise _FetchError(f"could not call the vault ({type(exc).__name__})") from exc
    if response.status_code != 200:
        raise _FetchError(f"the vault returned {response.status_code}")
    return _token_from_json(response), certificate_sha256_of_pem(pem)


def _print_claims(claims: Mapping[str, object]) -> None:
    for name, value in summarize_claims(claims).items():
        print(f"  {name:<20} {_printable(value)}")


def _commit_url(commit: str, environ: Mapping[str, str]) -> str | None:
    repo = environ.get("GITHUB_REPO_URL", "").strip().rstrip("/")
    return f"{repo}/commit/{commit}" if repo else None


def _commit_exists(commit: str, environ: Mapping[str, str], transport: httpx.BaseTransport | None) -> bool:
    """GitHub の API で、GITHUB_REPO_URL のリポジトリにそのコミットがあるか(認証なし。公開リポジトリが前提)。"""
    path = urlsplit(environ["GITHUB_REPO_URL"]).path.strip("/").removesuffix(".git")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "verify-attestation-script"}
    with httpx.Client(timeout=_TIMEOUT_SECONDS, transport=transport, headers=headers) as client:
        response = client.get(f"https://api.github.com/repos/{path}/commits/{commit}")
    return response.status_code == 200


def main(
    argv: Sequence[str] | None = None,
    *,
    signer_certs: Mapping[str, str] | SignerCerts | None = None,
    environ: Mapping[str, str] | None = None,
    http_transport: httpx.BaseTransport | None = None,
) -> int:
    """検証して、終了コードを返す(0: 全部通った、1: 1 つでも違う・取れない)。

    signer_certs(署名鍵。省略すると Google の x509 エンドポイント)・environ・http_transport(--web と GitHub の API の通信路)は、テストで差し込む。
    """
    args = _parse_args(argv)
    environ = os.environ if environ is None else environ
    settings = load_tee_settings()
    try:
        releases = load_releases(args.releases)
    except (OSError, ValueError) as exc:
        print(f"エラー: digest の表を読めません: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    policy = AttestationPolicy(
        audience=settings.attestation_audience,
        issuer=settings.attestation_issuer,
        allowed_hwmodels=frozenset(args.hwmodel or settings.allowed_hwmodels),
        allowed_digests=active_digests(releases),
        project_id=args.project,
        service_account=args.service_account,
        require_production=not args.allow_debug,
    )
    if args.allow_debug:
        print("警告: --allow-debug: debug イメージのトークン(dbgstat・STABLE を見ない)も通します。本番の確認には使わないでください。", file=sys.stderr)
    if args.check_commit and not environ.get("GITHUB_REPO_URL", "").strip():
        print("エラー: --check-commit には、環境変数 GITHUB_REPO_URL(例 https://github.com/<owner>/<repo>)が要ります。", file=sys.stderr)
        return 1

    if args.direct and httpx.URL(args.direct).scheme != "https":
        print("エラー: --direct の URL は https:// で始めてください(金庫は TLS で話します)。", file=sys.stderr)
        return 1

    nonce = secrets.token_urlsafe(32)
    try:
        if args.web:
            token, certificate_sha256 = _fetch_via_web(args.web, nonce, http_transport), None
        else:
            token, certificate_sha256 = _fetch_direct(args.direct, nonce)
    except _FetchError as exc:
        print(f"エラー: トークンを取れませんでした: {exc}", file=sys.stderr)
        return 1

    certs = signer_certs if signer_certs is not None else SignerCerts(settings.attestation_signer_certs_url)
    try:
        verified = verify_attestation_token(
            token, certs=certs, policy=policy, nonce=nonce, certificate_sha256=certificate_sha256
        )
    except AttestationError as exc:
        print(f"NG: 検証に失敗しました。reason={exc.reason}({_REASON_TEXT.get(exc.reason, '')})")
        try:
            print("取れた範囲の claims(署名を確かめていないので、信用しないでください):")
            _print_claims(decode_claims_unverified(token))
        except AttestationError:
            pass
        if args.print_token:
            print(token)
        return 1

    return _report_success(args, verified, token, certificate_sha256, releases, environ, http_transport)


def _report_success(
    args: argparse.Namespace,
    verified: VerifiedAttestation,
    token: str,
    certificate_sha256: str | None,
    releases: list[dict],
    environ: Mapping[str, str],
    http_transport: httpx.BaseTransport | None,
) -> int:
    print("OK: attestation トークンの署名と、すべての項目を確かめました。")
    _print_claims(verified.claims)
    print(f"  {'service_accounts':<20} {_printable(list(verified.service_accounts))}")
    if certificate_sha256 is not None:
        print(f"  {'certificate_sha256':<20} {certificate_sha256}(トークンの eat_nonce と一致。今の TLS の相手)")
    else:
        print("  (web 経由なので、TLS の証明書の結び付きは、このスクリプトからは確かめていません)")
    if verified.dbgstat != "disabled-since-boot" or "STABLE" not in verified.support_attributes:
        print(f"警告: このトークンは本番イメージのものではありません(dbgstat={_printable(verified.dbgstat)})。", file=sys.stderr)

    release = release_for_digest(releases, verified.image_digest)
    code = 0
    if release is None:  # 許可リストに入れた digest で検証が通ったので、ここには来ないはず
        print("NG: digest が表にありません。")
        code = 1
    else:
        print("リリースの記録(運営者が、このコミットから作ったと記録したもの):")
        print(f"  commit    {release['commit']}")
        print(f"  built_at  {_printable(release.get('built_at'))}")
        url = _commit_url(release["commit"], environ)
        if url is not None:
            print(f"  url       {url}")
        if args.check_commit:
            try:
                exists = _commit_exists(release["commit"], environ, http_transport)
            except httpx.HTTPError as exc:
                print(f"NG: GitHub に問い合わせられませんでした({type(exc).__name__})。")
                code = 1
            else:
                print("  GitHub にそのコミットがあることを確かめました。" if exists else "NG: GitHub に、そのコミットが見つかりません。")
                code = code if exists else 1
    if args.print_token:
        print(token)
    return code


if __name__ == "__main__":
    sys.exit(main())
