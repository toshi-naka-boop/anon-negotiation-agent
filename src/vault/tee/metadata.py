"""Compute Engine のメタデータサーバの読み取り(research/tee-spike-contract.md §4 の 2)。

TEE 版の金庫は、プロジェクト ID・番号・ゾーン・インスタンス名を、設定にもコードにも持たず、起動時にここから取る
(公開リポジトリに ID を書かずに済む)。ヘッダ `Metadata-Flavor: Google` が必須。
リージョンは、ゾーン(`projects/<番号>/zones/<ゾーン>` の最後の要素)の末尾 `-x` を除いたもの(例: asia-northeast1-b → asia-northeast1)。

取った値は URL(KMS の鍵の名前)に入るので、形を確かめる。外部への通信はメタデータサーバだけ。transport は差し込める(テストは httpx.MockTransport)。
"""

import re
from dataclasses import dataclass

import httpx

METADATA_BASE_URL = "http://metadata.google.internal/computeMetadata/v1/"
_HEADERS = {"Metadata-Flavor": "Google"}
_TIMEOUT_SECONDS = 5.0

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")  # プロジェクト ID・インスタンス名
_NUMBER = re.compile(r"[0-9]+")  # プロジェクト番号
_ZONE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*-[a-z]")  # 例: asia-northeast1-b


class MetadataError(Exception):
    """メタデータサーバから、必要な値を取れなかった(届かない・2xx 以外・空・形が違う)。"""


@dataclass(frozen=True)
class InstanceMetadata:
    project_id: str
    project_number: str
    zone: str
    region: str
    instance_name: str


def _read(http: httpx.Client, path: str) -> str:
    try:
        response = http.get(path)
    except httpx.HTTPError as exc:
        raise MetadataError(f"could not reach the metadata server for {path} ({type(exc).__name__})") from exc
    if response.status_code != 200:
        raise MetadataError(f"the metadata server returned {response.status_code} for {path}")
    return response.text.strip()


def read_instance_metadata(*, transport: httpx.BaseTransport | None = None) -> InstanceMetadata:
    """project/project-id・project/numeric-project-id・instance/zone・instance/name を読む。"""
    # trust_env=False: 環境変数のプロキシ設定が、メタデータサーバへの呼び出しを横取りしないように。
    with httpx.Client(
        base_url=METADATA_BASE_URL, headers=_HEADERS, transport=transport, timeout=_TIMEOUT_SECONDS, trust_env=False
    ) as http:
        project_id = _read(http, "project/project-id")
        project_number = _read(http, "project/numeric-project-id")
        zone = _read(http, "instance/zone").rsplit("/", 1)[-1]
        instance_name = _read(http, "instance/name")
    well_formed = (
        _NAME.fullmatch(project_id)
        and _NUMBER.fullmatch(project_number)
        and _ZONE.fullmatch(zone)
        and _NAME.fullmatch(instance_name)
    )
    if not well_formed:
        raise MetadataError("the metadata server returned a value in an unexpected form")
    return InstanceMetadata(
        project_id=project_id,
        project_number=project_number,
        zone=zone,
        region=zone.rsplit("-", 1)[0],
        instance_name=instance_name,
    )
