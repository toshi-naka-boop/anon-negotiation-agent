"""scripts/deploy_check.sh(AC-22。design.md §10 の本番の必須設定と TEE の照合 (a)〜(i))の試験。

GCP にも、実物の gcloud・curl にも接続しない。スクリプトは、環境変数 GCLOUD・CURL・UV で外部コマンドを差し替えられるので、試験が置く偽物
(tests の一時ディレクトリの Python スクリプト。シナリオの JSON を読んで、gcloud・curl の応答を返す)を差し込む。スクリプトのコピーは、
一時ディレクトリの偽のリポジトリ(config/params.toml・Dockerfile・uv.lock・deploy/ を置く)に置く: スクリプトはリポジトリの直下を、
自分の場所から決めるので、リポジトリの中身に依存する項目(agents の URL・workers・期待する主体の正本)を、実物に触れずに確かめられる。

確かめること
- `bash -n` で構文が通る。shellcheck があれば、警告がない(なければ skip)。
- 必須の環境変数が足りなければ、足りない名前を言って終了コード 2。VAULT_MODE が違う・URL が URL でない・--only の id が表にない、も 2。
- `--list` が、(a)〜(i) と §10 の項目名を出す(環境変数なしで動く)。`--show-expected` は gcloud を呼ばずに、期待する主体の集合を出す。
- 「良い世界」(すべて本番の設定どおり)では、VAULT_MODE=tee・cloudrun のどちらでも、SKIP にしている項目のほかが、すべて [OK]・終了コード 0。
- 項目ごとに、設定を 1 つ壊すと [NG](終了コード 1)になり、理由が出る。gcloud・curl が失敗したときも [NG](確かめられないものを通さない)。
- agents の起動元(iam-agents)は、サービスに直に付いた束縛に加えて、Policy Analyzer が列挙する run.routes.invoke を持つ主体(継承込み)が、
  web の SA と承認済みのオーナーだけであること(X-86)。web の TEE の設定(web-vault-env)は、VAULT_EXPECTED_ZONE・VAULT_EXPECTED_INSTANCE も、金庫の VM と一致すること(L20-8)。
- (i)(tee-i)は、組織の配下でなければ [SKIP](拒否ポリシーは作れない。I-37)。組織の配下(祖先に組織がある・ORG_ID がある)なら、これまでどおり確かめる。
  プール管理者の問い合わせ(tee-b-pool)は、完全な形の権限名(iam.googleapis.com/...)で行う(I-39)。
- 読み出しだけを行う(--reset-vault がなければ、gcloud に書き込みの動詞を渡さない)。--reset-vault は、停止→開始を 1 回だけ行い(reset は使わない)、再起動の後の自己試験を確かめる。
- トークンの値は、出力にも、curl の引数にも出ない(ヘッダは標準入力で渡す)。
- uv pip show aiohttp は、実物の uv でも動く(リポジトリの環境に aiohttp はない)。
"""

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy_check.sh"
BASH = shutil.which("bash") or "bash"

PROJECT_ID = "demo-project"
PROJECT_NUMBER = "123456789012"
REGION = "asia-northeast1"
ZONE = "asia-northeast1-b"
VM_NAME = "vault-tee"
WEB_URL = "https://web-abc.a.run.app"
AGENTS_URL = "https://agents-abc.a.run.app"
VAULT_RUN_URL = "https://vault-abc.a.run.app"
WEB_SA = f"web-run@{PROJECT_ID}.iam.gserviceaccount.com"
VAULT_SA = f"vault-tee@{PROJECT_ID}.iam.gserviceaccount.com"
COMPUTE_SA = f"{PROJECT_NUMBER}-compute@developer.gserviceaccount.com"  # Compute Engine の既定の SA(プロジェクトの roles/editor。run.routes.invoke を含む)
CI_SA = f"ci-runner@{PROJECT_ID}.iam.gserviceaccount.com"  # プロジェクトに roles/run.invoker を付けられた SA
OWNER = "owner@example.com"
DIGEST = "sha256:" + "ab" * 32
OLD_DIGEST = "sha256:" + "cd" * 32
INSTANCE_ID = "4242424242424242"
KEY_NAME = f"projects/{PROJECT_ID}/locations/{REGION}/keyRings/vault-tee/cryptoKeys/vault-kek"
PRIMARY = f"{KEY_NAME}/cryptoKeyVersions/2"
POOL_PATTERN = f"principalSet://iam.googleapis.com/projects/{PROJECT_NUMBER}/locations/global/workloadIdentityPools/vault-tee-pool/attribute.image_digest/{{digest}}"
SUBJECT = f"principal://iam.googleapis.com/projects/{PROJECT_NUMBER}/locations/global/workloadIdentityPools/vault-tee-pool/subject/gcpcs::{DIGEST}::{PROJECT_NUMBER}::{INSTANCE_ID}"
SECRETS = ("SECRET-ACCESS-TOKEN", "SECRET-ID-TOKEN", "SECRET-WRAPPED-DEK", "SECRET-ATTESTATION-JWT")
WEB_CONTAINER_CONCURRENCY = 200  # §10: web の Cloud Run の同時リクエスト数(SSE の同時本数の上限 20 に、通常の要求の分を足した値。台帳 C-65)

# 確かめられない・人が確認する項目(どの世界でも [SKIP])
ALWAYS_SKIP = {"vertex-quota", "thinking-usage", "r7-client-ip", "submission-checklist"}

# ----------------------------------------------------------------------
# 偽の gcloud・curl・uv(Python)。FAKE_SCENARIO(JSON)を読んで答え、FAKE_LOG に呼ばれた内容を残す。
# ----------------------------------------------------------------------
FAKE_GCLOUD = """#!/usr/bin/env python3
import json, os, sys

scenario = json.load(open(os.environ["FAKE_SCENARIO"]))
line = " ".join(sys.argv[1:])
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("gcloud " + line + "\\n")
for rule in scenario["gcloud"]:
    if all(part in line for part in rule["match"]):
        sys.stdout.write(rule.get("stdout", ""))
        sys.stderr.write(rule.get("stderr", ""))
        sys.exit(rule.get("exit", 0))
sys.stderr.write("fake gcloud: no rule for: " + line + "\\n")
sys.exit(97)
"""

FAKE_CURL = """#!/usr/bin/env python3
import json, os, sys

scenario = json.load(open(os.environ["FAKE_SCENARIO"]))
args = sys.argv[1:]
out_path = None
with_header_file = False
for index, arg in enumerate(args):
    if arg == "-o":
        out_path = args[index + 1]
    if arg == "-H" and args[index + 1] == "@-":
        with_header_file = True
url = args[-1]
header = sys.stdin.read() if with_header_file else ""
authenticated = header.startswith("Authorization: Bearer ") and len(header.strip()) > len("Authorization: Bearer ")
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("curl " + " ".join(args) + " | auth=" + ("yes" if authenticated else "no") + "\\n")
for rule in scenario["curl"]:
    if rule["url"] == url:
        if rule.get("exit"):
            sys.stderr.write("curl: (7) Failed to connect\\n")
            sys.exit(rule["exit"])
        status = rule.get("status", 200)
        if rule.get("needs_auth") and not authenticated:
            status = 401
        if out_path:
            open(out_path, "w").write(rule.get("body", ""))
        sys.stdout.write(str(status))
        sys.exit(0)
sys.stderr.write("fake curl: no rule for: " + url + "\\n")
sys.exit(97)
"""

FAKE_UV = """#!/usr/bin/env python3
import os, sys

if os.environ.get("FAKE_UV_INSTALLED") == "1":
    print("Name: aiohttp")
    sys.exit(0)
sys.stderr.write("warning: Package(s) not found for: aiohttp\\n")
sys.exit(1)
"""

DOCKERFILE = 'FROM python:3.12-slim\nCMD ["uvicorn", "web.app:create_app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]\n'


# ----------------------------------------------------------------------
# 「良い世界」: 本番の設定どおりのときの、gcloud・curl の応答
# ----------------------------------------------------------------------


def env_list(values: dict, secrets=()) -> list:
    items = [{"name": key, "value": value} for key, value in values.items()]
    items += [{"name": name, "valueFrom": {"secretKeyRef": {"name": name.lower(), "key": "latest"}}} for name in secrets]
    return items


def service_json(name, url, env, *, command=None, args=None, annotations=None, service_annotations=None, concurrency=None) -> dict:
    container = {"image": f"{REGION}-docker.pkg.dev/{PROJECT_ID}/vault/app@sha256:{'0' * 64}", "env": env}
    if command:
        container["command"] = command
    if args:
        container["args"] = args
    template_spec = {"containers": [container]}
    if concurrency is not None:
        template_spec["containerConcurrency"] = concurrency
    return {
        "apiVersion": "serving.knative.dev/v1",
        "kind": "Service",
        "metadata": {"name": name, "annotations": dict(service_annotations or {})},
        "spec": {"template": {"metadata": {"annotations": dict(annotations or {})}, "spec": template_spec}},
        "status": {"url": url},
    }


def ttl_fields(database, groups, state="ACTIVE") -> list:
    return [
        {
            "name": f"projects/{PROJECT_ID}/databases/{database}/collectionGroups/{group}/fields/ttl_at",
            "indexConfig": {},
            "ttlConfig": {"state": state},
        }
        for group in groups
    ]


def analysis_json(identities, *, fully_explored=True) -> dict:
    result = {
        "attachedResourceFullName": "//cloudresourcemanager.googleapis.com/projects/" + PROJECT_NUMBER,
        "iamBinding": {"role": "roles/owner", "members": list(identities)},
        "identityList": {"identities": [{"name": identity} for identity in identities]},
        "fullyExplored": True,
    }
    return {"fullyExplored": fully_explored, "mainAnalysis": {"analysisResults": [result], "fullyExplored": fully_explored}}


def deny_policy_json() -> dict:
    return {
        "name": f"policies/cloudresourcemanager.googleapis.com%2Fprojects%2F{PROJECT_NUMBER}/denypolicies/vault-kek-deny",
        "kind": "DenyPolicy",
        "displayName": "vault KEK: only the TEE workload pool may use the key",
        "rules": [
            {
                "denyRule": {
                    "deniedPrincipals": ["principalSet://goog/public:all"],
                    "exceptionPrincipals": [f"principalSet://iam.googleapis.com/projects/{PROJECT_NUMBER}/locations/global/workloadIdentityPools/vault-tee-pool/*"],
                    "deniedPermissions": [
                        "cloudkms.googleapis.com/cryptoKeyVersions.useToDecrypt",
                        "cloudkms.googleapis.com/cryptoKeyVersions.useToEncrypt",
                    ],
                }
            }
        ],
    }


def provider_json() -> dict:
    return {
        "name": f"projects/{PROJECT_NUMBER}/locations/global/workloadIdentityPools/vault-tee-pool/providers/attestation-verifier",
        "state": "ACTIVE",
        "oidc": {"issuerUri": "https://confidentialcomputing.googleapis.com/", "allowedAudiences": ["https://sts.googleapis.com"]},
        "attributeMapping": {
            "google.subject": '"gcpcs::"+assertion.submods.container.image_digest+"::"+assertion.submods.gce.project_number+"::"+assertion.submods.gce.instance_id',
            "attribute.image_digest": "assertion.submods.container.image_digest",
        },
        "attributeCondition": (
            "assertion.swname == 'CONFIDENTIAL_SPACE' && 'STABLE' in assertion.submods.confidential_space.support_attributes && "
            "assertion.dbgstat == 'disabled-since-boot' && assertion.hwmodel in ['GCP_AMD_SEV','GCP_INTEL_TDX'] && "
            f"assertion.submods.gce.project_id == '{PROJECT_ID}' && '{VAULT_SA}' in assertion.google_service_accounts"
        ),
    }


def make_world(mode: str = "tee") -> dict:
    """本番の設定どおりの世界。試験は、これを写して、1 か所ずつ壊す。"""
    vertex = {"GOOGLE_GENAI_USE_VERTEXAI": "TRUE", "GOOGLE_CLOUD_PROJECT": PROJECT_ID, "GOOGLE_CLOUD_LOCATION": "global"}
    adk = {"ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS": "false"}
    web_env = {**adk, **vertex, "VAULT_BASE_URL": "https://10.10.0.10:8443" if mode == "tee" else VAULT_RUN_URL}
    if mode == "tee":
        web_env.update({"VAULT_TEE": "true", "VAULT_SERVICE_ACCOUNT": VAULT_SA, "VAULT_EXPECTED_ZONE": ZONE, "VAULT_EXPECTED_INSTANCE": VM_NAME})
    scale = {
        "autoscaling.knative.dev/minScale": "1",
        "autoscaling.knative.dev/maxScale": "1",
        "run.googleapis.com/cpu-throttling": "false",
    }
    world = {
        "mode": mode,
        "svc": {
            "web": service_json("web", WEB_URL, env_list(web_env, ["SESSION_SIGNING_KEY"]), annotations=scale, concurrency=WEB_CONTAINER_CONCURRENCY),
            "agents": service_json("agents", AGENTS_URL, env_list({**adk, **vertex})),
            "vault": service_json(
                "vault",
                VAULT_RUN_URL,
                [],
                command=["uvicorn", "vault.app:create_app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8080"],
            ),
        },
        "iam": {
            "agents": {"bindings": [{"role": "roles/run.invoker", "members": [f"serviceAccount:{WEB_SA}"]}], "etag": "x"},
            "vault": {"bindings": [{"role": "roles/run.invoker", "members": [f"serviceAccount:{WEB_SA}"]}], "etag": "x"},
        },
        "ttl_default": ttl_fields("(default)", ["stages", "llm_call_counters", "rate_limits"]),
        "ttl_vault": ttl_fields("vault-db", ["negotiations", "events", "idempotency"]),
        "sink": {
            "name": "_Default",
            "exclusions": [
                {"name": "exclude-run-requests", "filter": 'logName:"run.googleapis.com%2Frequests" AND resource.type="cloud_run_revision"'}
            ],
        },
        "cache": {"name": f"projects/{PROJECT_ID}/cacheConfig", "disableCache": True},
        "providers": [provider_json()],
        "ancestors": [{"id": PROJECT_ID, "type": "project"}],  # 組織の配下ではない(組織の配下の世界は、試験が organization を足す)
        "kms_analysis": analysis_json([f"user:{OWNER}", POOL_PATTERN.format(digest=DIGEST)]),
        "pool_analysis": analysis_json([f"user:{OWNER}"]),
        # run.routes.invoke を持つ主体(agents を呼べる主体): web の SA(サービスの束縛)と、承認済みのオーナー(基本ロール)だけ
        "agents_analysis": analysis_json([f"serviceAccount:{WEB_SA}", f"user:{OWNER}"]),
        "key": {"name": KEY_NAME, "primary": {"name": PRIMARY, "state": "ENABLED", "createTime": "2026-10-03T12:00:00.123456Z"}},
        "versions": [
            {"name": f"{KEY_NAME}/cryptoKeyVersions/1", "state": "DISABLED"},
            {"name": PRIMARY, "state": "ENABLED"},
        ],
        "vm": {
            "id": INSTANCE_ID,
            "name": "vault-tee",
            "status": "RUNNING",
            "networkInterfaces": [{"networkIP": "10.10.0.10"}],
            "metadata": {
                "items": [
                    {"key": "tee-image-reference", "value": f"{REGION}-docker.pkg.dev/{PROJECT_ID}/vault/vault@{DIGEST}"},
                    {"key": "tee-container-log-redirect", "value": "cloud_logging"},
                ]
            },
            "disks": [{"source": f"https://www.googleapis.com/compute/v1/projects/{PROJECT_ID}/zones/{ZONE}/disks/vault-tee"}],
        },
        "disk": {
            "name": "vault-tee",
            "sourceImage": "https://www.googleapis.com/compute/v1/projects/confidential-space-images/global/images/confidential-space-260100",
        },
        "firewall": [],
        "project_iam": {
            "auditConfigs": [
                {"service": "cloudkms.googleapis.com", "auditLogConfigs": [{"logType": "DATA_READ"}, {"logType": "DATA_WRITE"}]}
            ],
            "bindings": [],
        },
        "audit_others": [],
        "audit_expected": [{"timestamp": "2026-10-03T13:00:00Z", "protoPayload": {"methodName": "Decrypt", "authenticationInfo": {"principalSubject": SUBJECT}}}],
        "deny": deny_policy_json(),
        "dek": {"name": "x/_tee/dek", "fields": {"kek_version": {"stringValue": PRIMARY}, "wrapped_dek": {"bytesValue": "SECRET-WRAPPED-DEK"}}},
        "selftest_status": 200,
        "launcher_logs": [{"timestamp": "2026-10-04T01:00:00Z", "textPayload": "INFO vault.tee.main: sealing self-test ok"}],
        "attestation": {
            "verified": True,
            "reason": None,
            "claims": {"image_digest": DIGEST, "hwmodel": "GCP_AMD_SEV"},
            "release": {"commit": "a" * 40},
            "token": "SECRET-ATTESTATION-JWT",
        },
        "http": {"web": 200, "agents": 200, "vault": 200, "demo": 200},
        "files": {
            "params": f'[agents]\nmodel = "x"\npublic_base_url = "{AGENTS_URL}"\n\n[agents.cost_targets]\nmax = 1\n',
            "dockerfile": DOCKERFILE,
            "uv_lock": 'version = 1\n\n[[package]]\nname = "httpx"\nversion = "0.28.1"\n',
            "releases": {
                "releases": [
                    {"digest": DIGEST, "commit": "a" * 40, "built_at": "2026-10-03T12:00:00Z", "status": "active"},
                    {"digest": OLD_DIGEST, "commit": "b" * 40, "built_at": "2026-10-02T12:00:00Z", "status": "revoked", "revoked_at": "2026-10-03T00:00:00Z"},
                ]
            },
            "expected": {"owners": [OWNER], "principal_set_pattern": POOL_PATTERN},
        },
        "fail": {},  # 名前 → (gcloud の終了コード, 標準エラー): その gcloud の呼び出しを失敗させる
    }
    return world


def scenario_of(world: dict) -> dict:
    """世界から、偽の gcloud・curl が引く規則を作る(上から順に、最初に合ったものを使う)。"""
    rules = []

    def add(name, match, document=None, *, text=None):
        fail = world["fail"].get(name)
        if fail is not None:
            rules.append({"match": match, "exit": fail[0], "stderr": fail[1]})
        else:
            rules.append({"match": match, "stdout": text if text is not None else json.dumps(document)})

    for name in ("web", "agents", "vault"):
        add(f"svc-{name}", [f"run services describe {name} "], world["svc"][name])
    for name in ("agents", "vault"):
        add(f"iam-{name}", [f"run services get-iam-policy {name} "], world["iam"][name])
    add("ttl-default", ["firestore fields ttls list", "--database=(default)"], world["ttl_default"])
    add("ttl-vault", ["firestore fields ttls list", "--database=vault-db"], world["ttl_vault"])
    add("sink-default", ["logging sinks describe _Default"], world["sink"])
    add("wif-providers", ["workload-identity-pools providers list"], world["providers"])
    add("ancestors", ["projects get-ancestors"], world["ancestors"])
    add("kms-analysis", ["asset analyze-iam-policy", "//cloudkms.googleapis.com/"], world["kms_analysis"])
    add("pool-analysis", ["asset analyze-iam-policy", "//cloudresourcemanager.googleapis.com/projects/"], world["pool_analysis"])
    add("agents-analysis", ["asset analyze-iam-policy", "//run.googleapis.com/"], world["agents_analysis"])
    add("kms-key", ["kms keys describe vault-kek"], world["key"])
    add("kms-versions", ["kms keys versions list"], world["versions"])
    add("vm", ["compute instances describe vault-tee"], world["vm"])
    add("vm-disk", ["compute disks describe vault-tee"], world["disk"])
    add("firewall", ["compute firewall-rules list"], world["firewall"])
    add("project-iam", ["projects get-iam-policy"], world["project_iam"])
    add("project-number", ["projects describe"], text=PROJECT_NUMBER + "\n")
    add("deny-policy", ["iam policies get vault-kek-deny"], world["deny"])
    add("stop", ["compute instances stop vault-tee"], text="")
    add("start", ["compute instances start vault-tee"], text="")
    add("launcher-log", ["logging read", "confidential-space-launcher"], world["launcher_logs"])
    add("audit-others", ["logging read", "NOT protoPayload.authenticationInfo.principalSubject"], world["audit_others"])
    add("audit-expected", ["logging read", "protoPayload.authenticationInfo.principalSubject="], world["audit_expected"])
    add("access-token", ["auth print-access-token"], text="SECRET-ACCESS-TOKEN\n")
    add("id-token", ["auth print-identity-token"], text="SECRET-ID-TOKEN\n")

    http = world["http"]
    health = world.get("health_path", "/health")
    curl = [
        {"url": f"{WEB_URL}{health}", "status": http["web"], "body": '{"status":"ok"}'},
        {"url": f"{AGENTS_URL}{health}", "status": http["agents"], "body": '{"status":"ok"}', "needs_auth": True},
        {"url": f"{VAULT_RUN_URL}{health}", "status": http["vault"], "body": '{"status":"ok"}', "needs_auth": True},
        {"url": f"{WEB_URL}/", "status": http["demo"], "body": "<html></html>"},
        {"url": f"{WEB_URL}/api/tee/attestation", "status": 200, "body": json.dumps(world["attestation"])},
        {"url": f"https://aiplatform.googleapis.com/v1/projects/{PROJECT_ID}/cacheConfig", "status": world.get("cache_status", 200), "body": json.dumps(world["cache"]), "needs_auth": True},
        {
            "url": f"https://firestore.googleapis.com/v1/projects/{PROJECT_ID}/databases/vault-db/documents/_tee/dek",
            "status": 200,
            "body": json.dumps(world["dek"]),
            "needs_auth": True,
        },
        {
            "url": f"https://firestore.googleapis.com/v1/projects/{PROJECT_ID}/databases/vault-db/documents/_tee/selftest",
            "status": world["selftest_status"],
            "body": "{}",
            "needs_auth": True,
        },
    ]
    for override in world.get("curl_fail", []):
        curl.insert(0, override)
    return {"gcloud": rules, "curl": curl}


# ----------------------------------------------------------------------
# 実行の部品
# ----------------------------------------------------------------------


class Run:
    """スクリプトを 1 回動かした結果。項目ごとの [OK]・[NG]・[SKIP] の行と、字下げした詳細の行を読む。"""

    LINE = re.compile(r"^\[(OK|NG|SKIP)\] (\S+): (.*)$")

    def __init__(self, completed: subprocess.CompletedProcess, log: Path) -> None:
        self.completed = completed
        self.code = completed.returncode
        self.stdout, self.stderr = completed.stdout, completed.stderr
        self.commands = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        self.items: dict[str, tuple[str, str, list[str]]] = {}
        current = None
        for line in self.stdout.splitlines():
            found = self.LINE.match(line)
            if found:
                assert found.group(2) not in self.items, f"{found.group(2)} が 2 行ある"
                current = found.group(2)
                self.items[current] = (found.group(1), found.group(3), [])
            elif line.startswith("    ") and current is not None:
                self.items[current][2].append(line.strip())

    def status(self, item: str) -> str:
        return self.items[item][0]

    def text(self, item: str) -> str:
        """項目の行と詳細の行をつないだもの。"""
        status, message, details = self.items[item]
        return " ".join([message, *details])


def make_repo(path: Path) -> Path:
    """偽のリポジトリ(スクリプトのコピーと、スクリプトが読むファイル)と、偽の外部コマンドを置いたディレクトリ。"""
    (path / "scripts").mkdir()
    shutil.copy(SCRIPT, path / "scripts" / "deploy_check.sh")
    for name, body in (("gcloud", FAKE_GCLOUD), ("curl", FAKE_CURL), ("uv", FAKE_UV)):
        fake = path / "bin" / name
        fake.parent.mkdir(exist_ok=True)
        fake.write_text(body, encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path)


def write_repo_files(repo: Path, world: dict) -> None:
    files = world["files"]
    (repo / "config").mkdir(exist_ok=True)
    (repo / "config" / "params.toml").write_text(files["params"], encoding="utf-8")
    (repo / "Dockerfile").write_text(files["dockerfile"], encoding="utf-8")
    (repo / "uv.lock").write_text(files["uv_lock"], encoding="utf-8")
    (repo / "deploy").mkdir(exist_ok=True)
    (repo / "deploy" / "vault-releases.json").write_text(json.dumps(files["releases"]), encoding="utf-8")
    (repo / "deploy" / "expected-kms-principals.json").write_text(json.dumps(files["expected"]), encoding="utf-8")


def base_env(repo: Path, mode: str) -> dict:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", str(repo)),
        "TMPDIR": str(repo),
        "LANG": "C.UTF-8",
        "PROJECT_ID": PROJECT_ID,
        "REGION": REGION,
        "WEB_URL": WEB_URL,
        "AGENTS_URL": AGENTS_URL,
        "VAULT_MODE": mode,
        "PROJECT_NUMBER": PROJECT_NUMBER,
        "GCLOUD": str(repo / "bin" / "gcloud"),
        "CURL": str(repo / "bin" / "curl"),
        "UV": str(repo / "bin" / "uv"),
        "FAKE_SCENARIO": str(repo / "scenario.json"),
        "FAKE_LOG": str(repo / "calls.log"),
        "RESET_POLLS": "1",
        "POLL_INTERVAL_SECONDS": "0",
    }
    if mode == "tee":
        env["ZONE"] = ZONE
    return env


def run_script(repo: Path, world: dict, *args: str, env_extra: dict | None = None, drop: tuple[str, ...] = ()) -> Run:
    write_repo_files(repo, world)
    (repo / "scenario.json").write_text(json.dumps(scenario_of(world)), encoding="utf-8")
    (repo / "calls.log").unlink(missing_ok=True)  # 1 回の実行ごとに、呼ばれた内容を記録し直す
    env = base_env(repo, world["mode"])
    env.update(env_extra or {})
    for name in drop:
        env.pop(name, None)
    completed = subprocess.run(
        [BASH, str(repo / "scripts" / "deploy_check.sh"), *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    return Run(completed, repo / "calls.log")


def mutated(mode: str, change) -> dict:
    world = make_world(mode)
    change(world)
    return world


# ----------------------------------------------------------------------
# 構文・引数・一覧
# ----------------------------------------------------------------------


def test_the_script_passes_bash_n():
    result = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_the_script_passes_shellcheck_when_it_is_installed():
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck がない")
    result = subprocess.run([shellcheck, "-x", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout


def test_the_script_runs_with_the_bash_that_macos_ships():
    # macOS の標準の bash は 3.2。配列の空展開・連想配列・mapfile などの bash 4 の機能を使っていない(--list は何も呼ばずに通る)
    result = subprocess.run(["/bin/bash", str(SCRIPT), "--list"], capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
    assert result.returncode == 0, result.stderr


def test_list_prints_every_item_without_any_environment_variable():
    result = subprocess.run([BASH, str(SCRIPT), "--list"], capture_output=True, text=True, env={"PATH": os.environ["PATH"]})

    assert result.returncode == 0, result.stderr
    ids = [line.split()[0] for line in result.stdout.splitlines()[1:]]
    assert len(ids) == len(set(ids))
    for letter in "abcdefghi":
        assert f"({letter})" in result.stdout, letter
    # §10 の項目名(題に、設計書の言葉が入っている)
    for phrase in (
        "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS", "INFO", "workers=1", "containerConcurrency", "SESSION_SIGNING_KEY", "SERVICE_AUTH_ENABLED",
        "GOOGLE_GENAI_USE_VERTEXAI", "VAULT_BASE_URL", "public_base_url", "起動元", "TTL", "cacheConfig.disableCache",
        "aiohttp", "run.googleapis.com/requests", "R-7", "/health", "/api/tee/attestation", "vault.app:create_app_from_env",
        "デモ URL", "提出物 6 点", "vault-kek-deny", "allow-iap-to-vault", "Policy Analyzer", "exemptedMembers",
    ):
        assert phrase in result.stdout, phrase
    assert {"tee-a", "tee-b", "tee-c", "tee-d", "tee-e", "tee-f", "tee-g", "tee-h", "tee-i"} <= set(ids)


def test_help_prints_the_usage_and_exits_0():
    result = subprocess.run([BASH, str(SCRIPT), "--help"], capture_output=True, text=True, env={"PATH": os.environ["PATH"]})

    assert result.returncode == 0
    for phrase in ("--list", "--only", "--show-expected", "--reset-vault", "終了コード"):
        assert phrase in result.stdout, phrase


def test_missing_environment_variables_are_named_and_exit_2(repo):
    result = subprocess.run([BASH, str(repo / "scripts" / "deploy_check.sh")], capture_output=True, text=True, env={"PATH": os.environ["PATH"]})

    assert result.returncode == 2
    for name in ("PROJECT_ID", "REGION", "WEB_URL", "AGENTS_URL", "ZONE"):
        assert name in result.stderr, name
    assert "[OK]" not in result.stdout and "[NG]" not in result.stdout


@pytest.mark.parametrize(
    "extra, drop, fragment",
    [
        ({"VAULT_MODE": "docker"}, (), "VAULT_MODE"),
        ({"WEB_URL": "web.example"}, (), "WEB_URL"),
        ({}, ("ZONE",), "ZONE"),  # VAULT_MODE=tee では ZONE が要る
        ({}, ("PROJECT_ID",), "PROJECT_ID"),
    ],
)
def test_bad_configuration_exits_2(repo, extra, drop, fragment):
    result = run_script(repo, make_world("tee"), env_extra=extra, drop=drop)

    assert result.code == 2 and fragment in result.stderr
    assert result.commands == []  # 何も呼ばずに止まる


def test_zone_is_not_required_for_cloudrun_mode(repo):
    result = run_script(repo, make_world("cloudrun"), "--only", "healthz-web", drop=("ZONE",))

    assert result.code == 0 and result.status("healthz-web") == "OK"


def test_unknown_arguments_and_unknown_items_exit_2(repo):
    assert run_script(repo, make_world(), "--frobnicate").code == 2
    unknown = run_script(repo, make_world(), "--only", "tee-z")
    assert unknown.code == 2 and "tee-z" in unknown.stderr and unknown.commands == []
    assert run_script(repo, make_world(), "--only").code == 2


def test_only_runs_just_the_named_items_and_accepts_commas_and_repeats(repo):
    result = run_script(repo, make_world("tee"), "--only", "healthz-web,demo-url", "--only", "tee-f")

    assert set(result.items) == {"healthz-web", "demo-url", "tee-f"} and result.code == 0


# ----------------------------------------------------------------------
# 良い世界: すべて [OK](確かめない項目だけ [SKIP])
# ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def good_tee(tmp_path_factory) -> Run:
    return run_script(make_repo(tmp_path_factory.mktemp("good-tee")), make_world("tee"))


@pytest.fixture(scope="module")
def good_cloudrun(tmp_path_factory) -> Run:
    return run_script(make_repo(tmp_path_factory.mktemp("good-cloudrun")), make_world("cloudrun"))


ITEMS_FOR_TEE = [
    "adk-capture", "log-level", "web-workers", "session-key", "service-auth", "vertex-env", "web-vault-env", "agents-url",
    "iam-agents", "ttl-default", "ttl-vault", "cache-config", "no-aiohttp", "request-log", "healthz-web", "healthz-agents",
    "healthz-vault", "demo-url", "tee-a", "tee-b", "tee-b-pool", "tee-c", "tee-d", "tee-e", "tee-f", "tee-g", "tee-h",
]
ITEMS_FOR_CLOUDRUN = [
    "adk-capture", "log-level", "web-workers", "session-key", "service-auth", "vertex-env", "web-vault-env", "agents-url",
    "iam-agents", "iam-vault", "vault-command", "ttl-default", "ttl-vault", "cache-config", "no-aiohttp", "request-log",
    "healthz-web", "healthz-agents", "healthz-vault", "demo-url",
]


def test_a_good_tee_world_passes_and_every_item_has_exactly_one_line(good_tee):
    assert good_tee.code == 0, good_tee.stdout
    listed = subprocess.run([BASH, str(SCRIPT), "--list"], capture_output=True, text=True).stdout.splitlines()[1:]
    assert set(good_tee.items) == {line.split()[0] for line in listed}
    assert not [item for item, (status, _, _) in good_tee.items.items() if status == "NG"]
    # SKIP は、確かめられない項目・Cloud Run 版だけの項目・--reset-vault なしの再起動・組織の配下でない世界の (i)(拒否ポリシーは作れない。I-37)だけ
    skipped = {item for item, (status, _, _) in good_tee.items.items() if status == "SKIP"}
    assert skipped == ALWAYS_SKIP | {"iam-vault", "vault-command", "tee-h-reset", "tee-i"}
    assert re.search(r"結果: OK \d+・NG 0・SKIP \d+", good_tee.stdout)


def test_a_good_cloudrun_world_passes_and_the_tee_items_are_not_applicable(good_cloudrun):
    assert good_cloudrun.code == 0, good_cloudrun.stdout
    skipped = {item for item, (status, _, _) in good_cloudrun.items.items() if status == "SKIP"}
    assert skipped == ALWAYS_SKIP | {f"tee-{letter}" for letter in "abcdefghi"} | {"tee-b-pool", "tee-h-reset"}
    assert "VAULT_MODE=cloudrun では対象外" in good_cloudrun.items["tee-a"][1]


@pytest.mark.parametrize("item", ITEMS_FOR_TEE)
def test_each_item_is_ok_in_a_good_tee_world(good_tee, item):
    assert good_tee.status(item) == "OK", good_tee.text(item)


@pytest.mark.parametrize("item", ITEMS_FOR_CLOUDRUN)
def test_each_item_is_ok_in_a_good_cloudrun_world(good_cloudrun, item):
    assert good_cloudrun.status(item) == "OK", good_cloudrun.text(item)


def test_the_unverifiable_items_are_skipped_with_a_reason(good_tee):
    for item in ALWAYS_SKIP:
        assert good_tee.status(item) == "SKIP" and len(good_tee.items[item][1]) > 20
    assert "診断用の口" in good_tee.items["r7-client-ip"][1] and "外部ロードバランサ" in good_tee.items["r7-client-ip"][1]
    assert "--reset-vault" in good_tee.items["tee-h-reset"][1]


def test_the_check_only_reads_and_never_prints_a_token(repo):
    result = run_script(repo, make_world("tee"))

    assert result.code == 0
    forbidden = ("create", "delete", "update", "reset", "stop", "start", "add-iam", "remove-iam", "set-iam", "enable", "disable", "patch", "import")
    gcloud_calls = [call for call in result.commands if call.startswith("gcloud ")]
    assert gcloud_calls
    for call in gcloud_calls:
        assert not any(word in call.split() for word in forbidden), call
    for secret in SECRETS:
        assert secret not in result.stdout and secret not in result.stderr
        assert not any(secret in call for call in result.commands)
    # トークンは、curl の引数ではなく標準入力のヘッダで渡る(認証が要る呼び出し: agents の /health・cacheConfig・_tee/dek は、auth=yes で通っている)
    authenticated = [call for call in result.commands if call.startswith("curl ") and "auth=yes" in call]
    assert len(authenticated) == 3 and all("-H @-" in call for call in authenticated)
    assert not any("Authorization" in call for call in result.commands)
    assert "wrapped_dek" not in result.stdout


def test_the_script_cleans_up_its_temporary_directory(repo):
    run_script(repo, make_world("tee"), "--only", "healthz-web")

    assert not list(repo.glob("deploy-check.*"))


# ----------------------------------------------------------------------
# §10 の項目ごとに、1 つ壊すと [NG]
# ----------------------------------------------------------------------


def edit_env(world, service, name, value):
    """service の環境変数 name を value にする(None なら消す)。"""
    container = world["svc"][service]["spec"]["template"]["spec"]["containers"][0]
    container["env"] = [item for item in container["env"] if item["name"] != name]
    if value is not None:
        container["env"].append({"name": name, "value": value})


def edit_container(world, service, **changes):
    world["svc"][service]["spec"]["template"]["spec"]["containers"][0].update(changes)


def edit_annotations(world, service, key, value):
    world["svc"][service]["spec"]["template"]["metadata"]["annotations"][key] = value


def remove_command(world, service):
    world["svc"][service]["spec"]["template"]["spec"]["containers"][0].pop("command", None)


def edit_concurrency(world, service, value):
    """service の containerConcurrency(同時リクエスト数)を value にする(None なら項目を消す)。"""
    template_spec = world["svc"][service]["spec"]["template"]["spec"]
    if value is None:
        template_spec.pop("containerConcurrency", None)
    else:
        template_spec["containerConcurrency"] = value


NG_CASES = [
    # (項目, 世界を壊す関数, NG の行に入る語, 世界の VAULT_MODE)
    ("adk-capture", lambda w: edit_env(w, "web", "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS", "true"), "web: false でない", "tee"),
    ("adk-capture", lambda w: edit_env(w, "agents", "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS", None), "agents: 未設定", "tee"),
    ("log-level", lambda w: edit_container(w, "agents", args=["--log-level", "debug"]), "agents: --log-level debug", "tee"),
    ("log-level", lambda w: edit_env(w, "web", "LOG_LEVEL", "DEBUG"), "web: LOG_LEVEL=DEBUG", "tee"),
    ("web-workers", lambda w: edit_container(w, "web", command=["uvicorn", "web.app:create_app_from_env", "--factory", "--workers", "2"]), "--workers が 2", "tee"),
    ("web-workers", lambda w: edit_env(w, "web", "WEB_CONCURRENCY", "4"), "WEB_CONCURRENCY=4", "tee"),
    ("web-workers", lambda w: edit_annotations(w, "web", "autoscaling.knative.dev/maxScale", "3"), "maxScale", "tee"),
    ("web-workers", lambda w: edit_annotations(w, "web", "run.googleapis.com/cpu-throttling", "true"), "cpu-throttling", "tee"),
    ("web-workers", lambda w: edit_concurrency(w, "web", 80), "containerConcurrency が 80(期待: 200", "tee"),  # Cloud Run の既定。SSE だけで埋まる(C-65)
    ("web-workers", lambda w: edit_concurrency(w, "web", None), "containerConcurrency が 未設定(期待: 200", "tee"),
    ("web-workers", lambda w: w["files"].update(dockerfile='FROM python:3.12-slim\nCMD ["uvicorn", "web.app:create_app_from_env"]\n'), "Dockerfile", "tee"),
    ("session-key", lambda w: edit_env(w, "web", "SESSION_SIGNING_KEY", "plain-text-value"), "平文の値", "tee"),
    ("session-key", lambda w: edit_container(w, "web", env=[]), "SESSION_SIGNING_KEY がない", "tee"),
    ("service-auth", lambda w: edit_env(w, "web", "SERVICE_AUTH_ENABLED", "false"), "値: false", "tee"),
    ("vertex-env", lambda w: edit_env(w, "agents", "GOOGLE_CLOUD_LOCATION", "us-central1"), "GOOGLE_CLOUD_LOCATION", "tee"),
    ("vertex-env", lambda w: edit_env(w, "web", "GOOGLE_CLOUD_PROJECT", "another-project"), "GOOGLE_CLOUD_PROJECT", "tee"),
    ("vertex-env", lambda w: edit_env(w, "web", "GOOGLE_GENAI_USE_VERTEXAI", None), "GOOGLE_GENAI_USE_VERTEXAI", "tee"),
    ("web-vault-env", lambda w: edit_env(w, "web", "VAULT_TEE", "false"), "VAULT_TEE", "tee"),
    ("web-vault-env", lambda w: edit_env(w, "web", "VAULT_BASE_URL", "http://10.10.0.10:8443"), "https", "tee"),
    ("web-vault-env", lambda w: edit_env(w, "web", "VAULT_SERVICE_ACCOUNT", "someone@example.com"), "VAULT_SERVICE_ACCOUNT", "tee"),
    ("web-vault-env", lambda w: edit_env(w, "web", "VAULT_BASE_URL", None), "VAULT_BASE_URL が未設定", "tee"),
    # L20-8: web は、あれば金庫の VM のゾーン・名前を照合し、なければ照合しない。必須にして、金庫の VM(ZONE・VM_NAME)と一致させる
    ("web-vault-env", lambda w: edit_env(w, "web", "VAULT_EXPECTED_ZONE", None), "VAULT_EXPECTED_ZONE が未設定", "tee"),
    ("web-vault-env", lambda w: edit_env(w, "web", "VAULT_EXPECTED_INSTANCE", None), "VAULT_EXPECTED_INSTANCE が未設定", "tee"),
    ("web-vault-env", lambda w: edit_env(w, "web", "VAULT_EXPECTED_ZONE", "asia-northeast1-a"), "VAULT_EXPECTED_ZONE が 'asia-northeast1-a'(期待: asia-northeast1-b)", "tee"),
    ("web-vault-env", lambda w: edit_env(w, "web", "VAULT_EXPECTED_INSTANCE", "vault-tee-2"), "VAULT_EXPECTED_INSTANCE が 'vault-tee-2'(期待: vault-tee)", "tee"),
    ("web-vault-env", lambda w: edit_env(w, "web", "VAULT_BASE_URL", "https://other.a.run.app"), "金庫の Cloud Run サービスの URL", "cloudrun"),
    ("web-vault-env", lambda w: edit_env(w, "web", "VAULT_TEE", "true"), "VAULT_MODE=cloudrun", "cloudrun"),
    ("agents-url", lambda w: w["files"].update(params='[agents]\npublic_base_url = "http://localhost:8080"\n'), "localhost:8080", "tee"),
    ("agents-url", lambda w: w["files"].update(params="[vault]\nport = 1\n"), "読めない", "tee"),
    ("iam-agents", lambda w: w["iam"]["agents"]["bindings"].append({"role": "roles/run.invoker", "members": ["allUsers"]}), "allUsers", "tee"),
    ("iam-agents", lambda w: w["iam"]["agents"]["bindings"][0]["members"].append("user:friend@example.com"), "friend@example.com", "tee"),
    ("iam-agents", lambda w: w["iam"]["agents"].update(bindings=[]), "なし", "tee"),
    ("iam-agents", lambda w: w["svc"]["agents"]["metadata"]["annotations"].update({"run.googleapis.com/invoker-iam-disabled": "true"}), "invoker-iam-disabled", "tee"),
    # X-86: サービスの束縛が web の SA だけでも、プロジェクトなど上位の階層の束縛から agents を呼べる主体(Policy Analyzer の列挙)がいれば NG
    ("iam-agents", lambda w: w.update(agents_analysis=analysis_json([f"serviceAccount:{WEB_SA}", f"user:{OWNER}", f"serviceAccount:{COMPUTE_SA}"])), COMPUTE_SA, "tee"),  # roles/editor
    ("iam-agents", lambda w: w.update(agents_analysis=analysis_json([f"serviceAccount:{WEB_SA}", f"user:{OWNER}", f"serviceAccount:{CI_SA}"])), CI_SA, "tee"),  # プロジェクトの roles/run.invoker
    ("iam-agents", lambda w: w.update(agents_analysis=analysis_json([f"serviceAccount:{WEB_SA}", f"user:{OWNER}", "allAuthenticatedUsers"])), "allAuthenticatedUsers", "tee"),
    ("iam-agents", lambda w: w.update(agents_analysis=analysis_json([f"serviceAccount:{WEB_SA}", f"user:{OWNER}", f"serviceAccount:{COMPUTE_SA}"])), COMPUTE_SA, "cloudrun"),
    ("iam-agents", lambda w: w.update(agents_analysis=analysis_json([f"serviceAccount:{WEB_SA}", f"user:{OWNER}"], fully_explored=False)), "未完了", "tee"),
    ("iam-agents", lambda w: w.update(agents_analysis=[]), "想定の形でない", "tee"),
    # 結果に web の SA・承認済みのオーナーがいないのも NG(権限名・資源名の形が違うと、エラーにならずに 0 件が返ることがある。I-39)
    ("iam-agents", lambda w: w.update(agents_analysis=analysis_json([f"user:{OWNER}"])), f"足りない(期待にあって、解析の結果にない): {WEB_SA}", "tee"),
    ("iam-agents", lambda w: w.update(agents_analysis=analysis_json([f"serviceAccount:{WEB_SA}"])), f"足りない(期待にあって、解析の結果にない): {OWNER}", "tee"),
    ("iam-agents", lambda w: w.update(agents_analysis=analysis_json([])), "結果が 0 件", "tee"),
    # Google 管理のサービスエージェントは、このプロジェクトの Vertex AI・Cloud Run の 2 つだけを許す(ほかのサービスエージェントは想定外)
    ("iam-agents", lambda w: w.update(agents_analysis=analysis_json([f"serviceAccount:{WEB_SA}", f"user:{OWNER}", f"serviceAccount:service-{PROJECT_NUMBER}@gcp-sa-cloudbuild.iam.gserviceaccount.com"])), "gcp-sa-cloudbuild", "tee"),
    ("iam-agents", lambda w: w.update(agents_analysis=analysis_json([f"serviceAccount:{WEB_SA}", f"user:{OWNER}", "serviceAccount:service-999999999999@gcp-sa-aiplatform.iam.gserviceaccount.com"])), "service-999999999999", "tee"),
    ("iam-vault", lambda w: w["iam"]["vault"]["bindings"][0]["members"].append("allAuthenticatedUsers"), "allAuthenticatedUsers", "cloudrun"),
    ("vault-command", lambda w: remove_command(w, "vault"), "起動コマンド", "cloudrun"),
    ("vault-command", lambda w: edit_container(w, "vault", command=["uvicorn", "web.app:create_app_from_env", "--factory"]), "web.app", "cloudrun"),
    ("ttl-default", lambda w: w["ttl_default"].pop(), "rate_limits", "tee"),
    ("ttl-default", lambda w: w.update(ttl_default=ttl_fields("(default)", ["stages", "llm_call_counters", "rate_limits"], "CREATING")), "CREATING", "tee"),
    ("ttl-vault", lambda w: w["ttl_vault"].pop(), "idempotency", "tee"),
    ("ttl-vault", lambda w: w["ttl_vault"].pop(1), "events", "tee"),
    ("cache-config", lambda w: w["cache"].pop("disableCache"), "disableCache", "tee"),
    ("cache-config", lambda w: w.update(cache_status=403), "HTTP 403", "tee"),
    ("request-log", lambda w: w["sink"].update(exclusions=[]), "除外フィルタがない", "tee"),
    ("request-log", lambda w: w["sink"]["exclusions"][0].update(disabled=True), "除外フィルタがない", "tee"),
    ("request-log", lambda w: w["sink"]["exclusions"][0].update(filter='logName:"run.googleapis.com%2Frequests" AND resource.labels.service_name="web"'), "agents", "tee"),
    ("healthz-web", lambda w: w["http"].update(web=503), "503", "tee"),
    ("healthz-agents", lambda w: w["http"].update(agents=500), "500", "tee"),
    ("healthz-agents", lambda w: w["fail"].update({"id-token": (1, "ERROR: (gcloud.auth.print-identity-token) permission denied")}), "ID トークンを取れない", "tee"),
    ("healthz-vault", lambda w: w["attestation"].update(verified=False, reason="image_digest"), "image_digest", "tee"),
    ("healthz-vault", lambda w: w["http"].update(vault=502), "502", "cloudrun"),
    ("demo-url", lambda w: w["http"].update(demo=404), "404", "tee"),
    ("demo-url", lambda w: w.update(curl_fail=[{"url": f"{WEB_URL}/", "exit": 7}]), "届かない", "tee"),
]


@pytest.mark.parametrize("item, change, fragment, mode", NG_CASES)
def test_breaking_one_setting_makes_the_item_ng(repo, item, change, fragment, mode):
    result = run_script(repo, mutated(mode, change), "--only", item)

    assert result.status(item) == "NG", result.stdout
    assert fragment in result.text(item), result.stdout
    assert result.code == 1 and "NG 1" in result.stdout


@pytest.mark.parametrize(
    "item, name",
    [
        ("adk-capture", "svc-web"),
        ("iam-agents", "iam-agents"),
        ("iam-agents", "agents-analysis"),
        ("ttl-default", "ttl-default"),
        ("ttl-vault", "ttl-vault"),
        ("request-log", "sink-default"),
        ("tee-a", "wif-providers"),
        ("tee-b", "kms-analysis"),
        ("tee-d", "kms-key"),
        ("tee-e", "vm"),
        ("tee-f", "firewall"),
        ("tee-i", "deny-policy"),
    ],
)
def test_a_failing_gcloud_call_is_ng_not_a_pass(repo, item, name):
    world = make_world("tee")
    if item == "tee-i":
        world["ancestors"] = IN_ORGANIZATION  # (i) は、組織の配下のときだけ確かめる(組織の配下でなければ SKIP。I-37)
    world["fail"][name] = (1, "ERROR: (gcloud.x) PERMISSION_DENIED: no permission")

    result = run_script(repo, world, "--only", item)

    assert result.status(item) == "NG" and "取得できない" in result.text(item) and "PERMISSION_DENIED" in result.text(item)
    assert result.code == 1


def test_a_404_on_a_health_path_ending_in_z_explains_that_cloud_run_reserves_it(repo):
    # Cloud Run の run.app では、末尾が z のパス(/health)を Google のフロントエンドが予約していて、コンテナに届かない(公式の既知の問題)
    result = run_script(
        repo,
        mutated("tee", lambda w: (w.update(health_path="/healthz"), w["http"].update(web=404))),
        "--only",
        "healthz-web",
        env_extra={"HEALTH_PATH": "/healthz"},
    )

    assert result.status("healthz-web") == "NG" and "予約" in result.text("healthz-web") and "HEALTH_PATH=/health" in result.text("healthz-web")


def test_the_health_path_can_be_changed_with_health_path(repo):
    world = mutated("tee", lambda w: w.update(health_path="/livecheck"))

    on_default = run_script(repo, world, "--only", "healthz-web,healthz-agents")
    renamed = run_script(repo, world, "--only", "healthz-web,healthz-agents", env_extra={"HEALTH_PATH": "/livecheck"})

    assert on_default.status("healthz-web") == "NG"  # 既定の /health は、この世界では届かない
    assert renamed.status("healthz-web") == "OK" and renamed.status("healthz-agents") == "OK" and renamed.code == 0
    assert "/livecheck が 200" in renamed.items["healthz-web"][1]


def test_service_auth_may_be_unset_or_true(repo):
    unset = run_script(repo, mutated("tee", lambda w: edit_env(w, "web", "SERVICE_AUTH_ENABLED", None)), "--only", "service-auth")
    enabled = run_script(repo, mutated("tee", lambda w: edit_env(w, "web", "SERVICE_AUTH_ENABLED", "TRUE")), "--only", "service-auth")

    assert unset.status("service-auth") == "OK" and enabled.status("service-auth") == "OK"


def test_web_may_take_its_workers_from_the_start_command_and_a_single_worker_is_fine(repo):
    world = mutated("tee", lambda w: edit_container(w, "web", args=["--workers=1", "--log-level", "info"]))

    result = run_script(repo, world, "--only", "web-workers,log-level")

    assert result.status("web-workers") == "OK" and result.status("log-level") == "OK"


# ---- aiohttp(実物の uv も使う) ----


def test_aiohttp_must_not_be_installed_or_locked(repo):
    clean = run_script(repo, make_world("tee"), "--only", "no-aiohttp")
    installed = run_script(repo, make_world("tee"), "--only", "no-aiohttp", env_extra={"FAKE_UV_INSTALLED": "1"})
    locked = run_script(repo, mutated("tee", lambda w: w["files"].update(uv_lock='[[package]]\nname = "aiohttp"\nversion = "3.9"\n')), "--only", "no-aiohttp")
    no_uv = run_script(repo, make_world("tee"), "--only", "no-aiohttp", env_extra={"UV": str(repo / "bin" / "no-such-uv")})

    assert clean.status("no-aiohttp") == "OK"
    assert installed.status("no-aiohttp") == "NG" and "インストールされている" in installed.text("no-aiohttp")
    assert locked.status("no-aiohttp") == "NG" and "uv.lock" in locked.text("no-aiohttp")
    assert no_uv.status("no-aiohttp") == "NG" and "uv が見つからない" in no_uv.text("no-aiohttp")


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv がない")
def test_the_real_uv_confirms_that_this_repository_has_no_aiohttp():
    environment = {key: value for key, value in os.environ.items() if key in ("PATH", "HOME", "TMPDIR", "UV_CACHE_DIR")}
    environment.update(PROJECT_ID="p", REGION="r", ZONE="z", WEB_URL="https://w.example", AGENTS_URL="https://a.example")

    result = subprocess.run([BASH, str(SCRIPT), "--only", "no-aiohttp"], cwd=ROOT, env=environment, capture_output=True, text=True)

    assert "[OK] no-aiohttp" in result.stdout, result.stdout + result.stderr
    assert result.returncode == 0


# ----------------------------------------------------------------------
# 期待する主体の正本(deploy/expected-kms-principals.json)
# ----------------------------------------------------------------------


def test_the_shipped_file_has_the_agreed_shape_whether_it_is_still_a_template_or_filled_in():
    # 契約 §18 の形。運営者が埋めたあとも通る(雛形のままであることは、確かめない)
    spec = json.loads((ROOT / "deploy" / "expected-kms-principals.json").read_text(encoding="utf-8"))

    assert set(spec) == {"owners", "principal_set_pattern"}
    assert isinstance(spec["owners"], list) and spec["owners"] and all(isinstance(owner, str) and owner for owner in spec["owners"])
    assert spec["principal_set_pattern"].startswith("principalSet://iam.googleapis.com/projects/")
    assert spec["principal_set_pattern"].endswith("/workloadIdentityPools/vault-tee-pool/attribute.image_digest/{digest}")


def test_show_expected_refuses_a_template_that_is_not_filled_in(repo):
    world = make_world("tee")
    world["files"]["expected"] = {"owners": ["<オーナーのメール>"], "principal_set_pattern": POOL_PATTERN.replace(PROJECT_NUMBER, "<PROJECT_NUMBER>")}

    result = run_script(repo, world, "--show-expected")

    assert result.code == 1 and "雛形のまま" in result.stdout and result.commands == []


def test_show_expected_builds_the_set_from_the_owners_and_the_active_digests_without_calling_gcloud(repo):
    world = make_world("tee")
    world["files"]["expected"]["owners"] = ["Owner@Example.com", f"user:{OWNER}", "other@example.com"]

    result = run_script(repo, world, "--show-expected")

    assert result.code == 0 and result.commands == []
    lines = result.stdout.splitlines()
    assert lines.count(f"owner: {OWNER}") == 1  # 大文字小文字と user: の接頭辞は、そろえて 1 件にする
    assert "owner: other@example.com" in lines
    assert lines.count(f"principalSet: {POOL_PATTERN.format(digest=DIGEST)}") == 1  # active のダイジェストだけ
    assert not any(OLD_DIGEST in line for line in lines)  # revoked は期待集合に入れない
    assert len(lines) == 3


def test_show_expected_refuses_a_malformed_file(repo):
    world = make_world("tee")
    world["files"]["expected"] = {"owners": [], "principal_set_pattern": "no-placeholder"}

    result = run_script(repo, world, "--show-expected")

    assert result.code == 1 and "形が違う" in result.stdout


# ----------------------------------------------------------------------
# TEE の (a)〜(i)
# ----------------------------------------------------------------------


IN_ORGANIZATION = [{"id": PROJECT_ID, "type": "project"}, {"id": "987654321", "type": "organization"}]


def in_org(change):
    """組織の配下の世界で change を行う(拒否ポリシーの照合 (i) は、組織の配下のときだけ。組織の配下でなければ SKIP。I-37)。"""
    return lambda world: (world.update(ancestors=IN_ORGANIZATION), change(world))


def edit_provider(world, **changes):
    world["providers"][0].update(changes)


def add_provider(world):
    other = provider_json()
    other["name"] = other["name"].replace("attestation-verifier", "second-provider")
    world["providers"].append(other)


TEE_NG_CASES = [
    ("tee-a", add_provider, "second-provider"),
    ("tee-a", lambda w: w["providers"].clear(), "1 件だけでない"),
    ("tee-a", lambda w: edit_provider(w, attributeCondition="assertion.swname == 'CONFIDENTIAL_SPACE'"), "attribute-condition"),
    ("tee-a", lambda w: w["providers"][0]["oidc"].update(issuerUri="https://accounts.google.com"), "issuerUri"),
    ("tee-a", lambda w: w["providers"][0]["oidc"].update(allowedAudiences=["https://sts.googleapis.com", "https://evil.example"]), "audience"),
    ("tee-a", lambda w: w["providers"][0]["attributeMapping"].update({"attribute.image_digest": "assertion.sub"}), "attribute-mapping"),
    ("tee-a", lambda w: edit_provider(w, disabled=True), "無効"),
    ("tee-b", lambda w: w.update(kms_analysis=analysis_json([f"user:{OWNER}", "user:evil@example.com", POOL_PATTERN.format(digest=DIGEST)])), "evil@example.com"),
    ("tee-b", lambda w: w.update(kms_analysis=analysis_json([f"user:{OWNER}"])), "足りない"),
    ("tee-b", lambda w: w.update(kms_analysis=analysis_json([f"user:{OWNER}", POOL_PATTERN.format(digest=OLD_DIGEST), POOL_PATTERN.format(digest=DIGEST)])), OLD_DIGEST),
    ("tee-b", lambda w: w.update(kms_analysis=analysis_json([f"user:{OWNER}", POOL_PATTERN.format(digest=DIGEST)], fully_explored=False)), "未完了"),
    ("tee-b", lambda w: w.update(kms_analysis={"mainAnalysis": {"analysisResults": []}}), "未完了"),
    ("tee-b", lambda w: w.update(kms_analysis=[]), "想定の形でない"),
    ("tee-b", lambda w: w["files"].update(expected={"owners": ["<オーナーのメール>"], "principal_set_pattern": POOL_PATTERN}), "雛形のまま"),
    ("tee-b", lambda w: w.update(kms_analysis=analysis_json([f"user:{OWNER}", f"serviceAccount:{VAULT_SA}", POOL_PATTERN.format(digest=DIGEST)])), VAULT_SA),
    ("tee-b", lambda w: w.update(ancestors=IN_ORGANIZATION), "ORG_ID=987654321 を設定"),
    ("tee-b-pool", lambda w: w.update(ancestors=IN_ORGANIZATION), "ORG_ID=987654321 を設定"),
    ("iam-agents", lambda w: w.update(ancestors=IN_ORGANIZATION), "ORG_ID=987654321 を設定"),
    ("tee-c", lambda w: w.update(ancestors=IN_ORGANIZATION), "ORG_ID=987654321 を設定"),
    ("tee-b-pool", lambda w: w.update(pool_analysis=analysis_json([f"user:{OWNER}", "user:evil@example.com"])), "evil@example.com"),
    ("tee-b-pool", lambda w: w.update(pool_analysis=analysis_json([f"user:{OWNER}"], fully_explored=False)), "未完了"),
    # Policy Analyzer の結果に加えて、プロジェクトの IAM の owner・pool admin の束縛も見る(二重の守り。I-39)
    ("tee-b-pool", lambda w: (w.update(pool_analysis=analysis_json([])), w["project_iam"]["bindings"].append({"role": "roles/owner", "members": ["user:evil@example.com"]})), "evil@example.com"),
    ("tee-b-pool", lambda w: (w.update(pool_analysis=analysis_json([])), w["project_iam"]["bindings"].append({"role": "roles/iam.workloadIdentityPoolAdmin", "members": ["serviceAccount:ci@example.iam.gserviceaccount.com"]})), "ci@example.iam.gserviceaccount.com"),
    ("tee-b-pool", lambda w: w.update(pool_analysis=analysis_json([])), "1 件も見つからない"),
    ("tee-c", lambda w: w.update(kms_analysis=analysis_json([f"user:{OWNER}", f"serviceAccount:{VAULT_SA}"])), VAULT_SA),
    ("tee-c", lambda w: w.update(kms_analysis=analysis_json([f"user:{OWNER}", f"serviceAccount:{WEB_SA}"])), WEB_SA),
    ("tee-d", lambda w: w["versions"][0].update(state="ENABLED"), "無効化されていない版"),
    ("tee-d", lambda w: w["key"]["primary"].update(state="DISABLED"), "ENABLED でない"),
    ("tee-d", lambda w: w["key"].pop("primary"), "primary の版がない"),
    ("tee-e", lambda w: w["vm"]["networkInterfaces"][0].update(accessConfigs=[{"natIP": "203.0.113.5"}]), "外部 IP"),
    ("tee-e", lambda w: w["disk"].update(sourceImage="https://www.googleapis.com/compute/v1/projects/confidential-space-images/global/images/confidential-space-debug-260100"), "confidential-space-debug"),
    ("tee-e", lambda w: w["disk"].update(sourceImage="https://www.googleapis.com/compute/v1/projects/debian-cloud/global/images/debian-12"), "debian-12"),
    ("tee-e", lambda w: w["vm"]["metadata"]["items"][0].update(value=f"{REGION}-docker.pkg.dev/{PROJECT_ID}/vault/vault:spike"), "digest(@sha256"),
    ("tee-e", lambda w: w["vm"]["metadata"]["items"][0].update(value=f"{REGION}-docker.pkg.dev/{PROJECT_ID}/vault/vault@{OLD_DIGEST}"), "active にない"),
    ("tee-e", lambda w: w["vm"]["networkInterfaces"][0].update(networkIP="10.10.0.99"), "10.10.0.99"),
    ("tee-f", lambda w: w.update(firewall=[{"name": "allow-iap-to-vault"}]), "残っている"),
    ("tee-g", lambda w: w["project_iam"].update(auditConfigs=[]), "DATA_READ が有効でない"),
    ("tee-g", lambda w: w["project_iam"]["auditConfigs"][0]["auditLogConfigs"].pop(), "DATA_WRITE が有効でない"),
    ("tee-g", lambda w: w["project_iam"]["auditConfigs"][0]["auditLogConfigs"][0].update(exemptedMembers=["user:someone@example.com"]), "exemptedMembers"),
    ("tee-g", lambda w: w.update(audit_others=[{"protoPayload": {"authenticationInfo": {"principalEmail": "owner@example.com"}}}]), "owner@example.com"),
    ("tee-g", lambda w: w.update(audit_expected=[]), "1 件もない"),
    ("tee-h", lambda w: w["dek"]["fields"]["kek_version"].update(stringValue=f"{KEY_NAME}/cryptoKeyVersions/1"), "primary の版と違う"),
    ("tee-h", lambda w: w["dek"]["fields"].pop("kek_version"), "kek_version がない"),
    ("tee-i", in_org(lambda w: w["fail"].update({"deny-policy": (1, "ERROR: (gcloud.iam.policies.get) NOT_FOUND: policy does not exist")})), "NOT_FOUND"),
    ("tee-i", in_org(lambda w: w["deny"].update(name="policies/x/denypolicies/another-policy")), "name が vault-kek-deny でない"),
    ("tee-i", in_org(lambda w: w["deny"]["rules"][0]["denyRule"].update(exceptionPrincipals=["principalSet://iam.googleapis.com/projects/1/locations/global/workloadIdentityPools/other/*"])), "手順 G と違う"),
    ("tee-i", in_org(lambda w: w["deny"]["rules"][0]["denyRule"].update(deniedPermissions=["cloudkms.googleapis.com/cryptoKeyVersions.useToDecrypt"])), "手順 G と違う"),
    ("tee-i", in_org(lambda w: w["deny"]["rules"].append(w["deny"]["rules"][0])), "手順 G と違う"),
    ("tee-i", in_org(lambda w: w["deny"].pop("rules")), "手順 G と違う"),
]


@pytest.mark.parametrize("item, change, fragment", TEE_NG_CASES)
def test_breaking_one_tee_setting_makes_the_item_ng(repo, item, change, fragment):
    result = run_script(repo, mutated("tee", change), "--only", item)

    assert result.status(item) == "NG", result.stdout
    assert fragment in result.text(item), result.stdout
    assert result.code == 1


def test_pool_admins_are_taken_from_the_project_iam_when_the_analyzer_returns_nothing(repo):
    # 二重の守り(I-39): Policy Analyzer が 0 件・欠けを返しても(権限名の形が違うと、エラーにならずに 0 件が返る)、プロジェクトの IAM の owner の束縛で補う
    def change(w):
        w.update(pool_analysis=analysis_json([]))
        w["project_iam"]["bindings"].append({"role": "roles/owner", "members": [f"user:{OWNER}"]})

    result = run_script(repo, mutated("tee", change), "--only", "tee-b-pool")

    assert result.status("tee-b-pool") == "OK", result.stdout
    assert "Policy Analyzer 0" in result.text("tee-b-pool") and "owner/pool admin 1" in result.text("tee-b-pool")
    assert any("projects get-iam-policy" in call for call in result.commands)


def test_agents_invokers_allow_this_projects_vertex_ai_and_cloud_run_service_agents(repo):
    # 実測(2026-10-05): Vertex AI と Cloud Run のサービスエージェントは、役割に run.routes.invoke を含む。名前で許し、OK の文に数を出す
    def change(w):
        w.update(agents_analysis=analysis_json([
            f"serviceAccount:{WEB_SA}", f"user:{OWNER}",
            f"serviceAccount:service-{PROJECT_NUMBER}@gcp-sa-aiplatform.iam.gserviceaccount.com",
            f"serviceAccount:service-{PROJECT_NUMBER}@serverless-robot-prod.iam.gserviceaccount.com",
        ]))

    result = run_script(repo, mutated("tee", change), "--only", "iam-agents")

    assert result.status("iam-agents") == "OK", result.stdout
    assert "サービスエージェント 2" in result.text("iam-agents")


def test_policy_analyzer_uses_the_organization_when_org_id_is_given_and_the_project_otherwise(repo):
    project_scope = run_script(repo, make_world("tee"), "--only", "tee-b,tee-b-pool")
    organization_scope = run_script(repo, make_world("tee"), "--only", "tee-b,tee-b-pool", env_extra={"ORG_ID": "987654321"})

    analysis_calls = [call for call in project_scope.commands if "analyze-iam-policy" in call]
    assert len(analysis_calls) == 2 and all(f"--project={PROJECT_ID}" in call and "--organization" not in call for call in analysis_calls)
    assert "--full-resource-name=//cloudkms.googleapis.com/" + KEY_NAME in analysis_calls[0]
    assert "--permissions=cloudkms.cryptoKeyVersions.useToDecrypt,cloudkms.cryptoKeyVersions.useToEncrypt" in analysis_calls[0]
    # Policy Analyzer は WIF のプールを対象にできない(実測 INVALID_ARGUMENT)ので、プロジェクトを対象に解析する
    assert "--full-resource-name=//cloudresourcemanager.googleapis.com/projects/" + PROJECT_ID in analysis_calls[1]
    # 権限名は完全な形(iam.googleapis.com/...)。iam.workloadIdentityPoolProviders.create の形では、Policy Analyzer は常に 0 件を返す(実測 2026-10-05。I-39)
    assert (
        "--permissions=iam.googleapis.com/workloadIdentityPoolProviders.create,iam.googleapis.com/workloadIdentityPoolProviders.update,"
        "iam.googleapis.com/workloadIdentityPoolProviders.delete,iam.googleapis.com/workloadIdentityPoolProviders.undelete,"
        "iam.googleapis.com/workloadIdentityPools.update,iam.googleapis.com/workloadIdentityPools.undelete"
    ) in analysis_calls[1].split()
    organization_calls = [call for call in organization_scope.commands if "analyze-iam-policy" in call]
    assert len(organization_calls) == 2 and all("--organization=987654321" in call and "--project" not in call for call in organization_calls)
    assert "--show-response" in analysis_calls[0]  # 解析が未完了かの判定(fullyExplored)に要る
    assert any("projects get-ancestors" in call for call in project_scope.commands)  # プロジェクトの範囲のときだけ、組織の配下でないかを確かめる
    assert not any("get-ancestors" in call for call in organization_scope.commands)
    assert "範囲: プロジェクトのみ(組織の配下ではない)" in project_scope.items["tee-b"][1]
    assert "範囲: 組織 987654321" in organization_scope.items["tee-b"][1] and organization_scope.status("tee-b-pool") == "OK"


def test_the_organization_scope_is_what_lets_a_project_in_an_organization_pass(repo):
    in_organization = mutated("tee", lambda w: w.update(ancestors=IN_ORGANIZATION))

    with_org = run_script(repo, in_organization, "--only", "tee-b,tee-b-pool,tee-c,iam-agents", env_extra={"ORG_ID": "987654321"})
    unknown_ancestors = make_world("tee")
    unknown_ancestors["fail"]["ancestors"] = (1, "ERROR: (gcloud.projects.get-ancestors) denied")
    without_ancestors = run_script(repo, unknown_ancestors, "--only", "tee-b,iam-agents")

    assert [with_org.status(item) for item in ("tee-b", "tee-b-pool", "tee-c", "iam-agents")] == ["OK", "OK", "OK", "OK"]
    # 祖先を取れなかったときは、止めずに、弱い範囲であることを書く
    assert without_ancestors.status("tee-b") == "OK" and "祖先を確かめられなかった" in without_ancestors.items["tee-b"][1]
    assert without_ancestors.status("iam-agents") == "OK" and "祖先を確かめられなかった" in without_ancestors.items["iam-agents"][1]


# ---- agents を呼べる主体(iam-agents。X-86) ----


def test_the_agents_invokers_are_listed_with_policy_analyzer_for_run_routes_invoke_on_the_service(repo):
    project_scope = run_script(repo, make_world("tee"), "--only", "iam-agents")
    organization_scope = run_script(repo, make_world("tee"), "--only", "iam-agents", env_extra={"ORG_ID": "987654321"})

    assert project_scope.status("iam-agents") == "OK", project_scope.stdout
    calls = [call for call in project_scope.commands if "analyze-iam-policy" in call]
    assert len(calls) == 1
    arguments = calls[0].split()
    # Cloud Run のサービス(Cloud Asset の run.googleapis.com/Service)の完全な名前。範囲は --project(ORG_ID があれば --organization)
    assert f"--full-resource-name=//run.googleapis.com/projects/{PROJECT_ID}/locations/{REGION}/services/agents" in arguments
    assert "--permissions=run.routes.invoke" in arguments
    assert f"--project={PROJECT_ID}" in arguments and not any(argument.startswith("--organization") for argument in arguments)
    assert "--show-response" in arguments  # 解析が未完了かの判定(fullyExplored)に要る
    assert "範囲: プロジェクトのみ(組織の配下ではない)" in project_scope.items["iam-agents"][1]
    # サービスに直に付いた束縛の確認(これまでの確認)も行う
    assert any("run services get-iam-policy agents" in call for call in project_scope.commands)
    organization_calls = [call for call in organization_scope.commands if "analyze-iam-policy" in call]
    assert len(organization_calls) == 1 and "--organization=987654321" in organization_calls[0].split() and "--project" not in organization_calls[0]
    assert organization_scope.status("iam-agents") == "OK" and "範囲: 組織 987654321" in organization_scope.items["iam-agents"][1]


def test_the_agents_invokers_are_compared_with_the_web_sa_and_the_approved_owners_after_normalizing_them(repo):
    world = make_world("tee")
    world["files"]["expected"]["owners"] = [OWNER, "user:Second.Owner@Example.com"]  # 大文字小文字・user: の接頭辞は、そろえて比べる
    world["agents_analysis"] = analysis_json([f"serviceAccount:{WEB_SA.upper()}", f"user:{OWNER}", "user:second.owner@example.com"])

    result = run_script(repo, world, "--only", "iam-agents")

    assert result.status("iam-agents") == "OK", result.stdout
    assert "3 件(web の SA 1・承認済みのオーナー 2・Google 管理のサービスエージェント 0)" in result.text("iam-agents")


# ---- (i) 拒否ポリシー(組織の配下のときだけ確かめる。I-37) ----


def test_i_is_skipped_without_asking_for_more_when_the_project_is_not_in_an_organization(repo):
    # 拒否ポリシーを作る役割(Deny Admin)は組織にだけ付与できる。祖先に組織がなければ、拒否ポリシーは作れないので、NG でなく SKIP
    world = make_world("tee")
    world["fail"]["project-number"] = (1, "ERROR: (gcloud.projects.describe) denied")  # SKIP にプロジェクト番号は要らない

    result = run_script(repo, world, "--only", "tee-i", drop=("PROJECT_NUMBER",))

    assert result.status("tee-i") == "SKIP" and result.code == 0 and "NG 0" in result.stdout
    for phrase in ("組織の配下でないので拒否ポリシーは作れない", "Deny Admin は組織にだけ付与できる", "I-37", "オーナーの抑止は監査ログの記録のみ"):
        assert phrase in result.text("tee-i"), phrase
    assert any("projects get-ancestors" in call for call in result.commands)
    assert not any("iam policies get" in call or "projects describe" in call for call in result.commands)  # 作れないものを、取りに行かない


def test_i_is_checked_as_before_when_an_organization_is_an_ancestor_or_org_id_is_given(repo):
    by_ancestor = run_script(repo, mutated("tee", lambda w: w.update(ancestors=IN_ORGANIZATION)), "--only", "tee-i")
    by_org_id = run_script(repo, make_world("tee"), "--only", "tee-i", env_extra={"ORG_ID": "987654321"})

    assert by_ancestor.status("tee-i") == "OK" and by_org_id.status("tee-i") == "OK"
    for result in (by_ancestor, by_org_id):
        assert any("iam policies get vault-kek-deny" in call for call in result.commands)
    assert not any("get-ancestors" in call for call in by_org_id.commands)  # ORG_ID があれば、祖先は見ない


def test_i_is_not_skipped_when_the_ancestors_cannot_be_read(repo):
    # 組織の配下でないと確かめられないときは、SKIP にしない(確かめられないものを、通ったことにしない)。これまでどおり、拒否ポリシーを確かめる
    unknown = make_world("tee")
    unknown["fail"]["ancestors"] = (1, "ERROR: (gcloud.projects.get-ancestors) denied")
    refused = make_world("tee")
    refused["fail"].update({"ancestors": (1, "ERROR: (gcloud.projects.get-ancestors) denied"), "deny-policy": (1, "ERROR: (gcloud.iam.policies.get) NOT_FOUND: policy does not exist")})

    assert run_script(repo, unknown, "--only", "tee-i").status("tee-i") == "OK"
    for unreadable_ancestors in ({"not": "a list"}, [], None):  # 形が違う・空(プロジェクト自身も返らない)も、確かめられていない
        unreadable = make_world("tee")
        unreadable["ancestors"] = unreadable_ancestors
        assert run_script(repo, unreadable, "--only", "tee-i").status("tee-i") == "OK", unreadable_ancestors
    result = run_script(repo, refused, "--only", "tee-i")
    assert result.status("tee-i") == "NG" and "NOT_FOUND" in result.text("tee-i") and "SKIP にしない" in result.text("tee-i")


def test_the_project_number_is_fetched_when_it_is_not_given_and_a_failure_is_ng(repo):
    in_organization = mutated("tee", lambda w: w.update(ancestors=IN_ORGANIZATION))
    fetched = run_script(repo, in_organization, "--only", "tee-i", drop=("PROJECT_NUMBER",))
    world = mutated("tee", lambda w: w.update(ancestors=IN_ORGANIZATION))
    world["fail"]["project-number"] = (1, "ERROR: (gcloud.projects.describe) denied")
    refused = run_script(repo, world, "--only", "tee-i", drop=("PROJECT_NUMBER",))

    assert fetched.status("tee-i") == "OK" and any("projects describe" in call for call in fetched.commands)
    assert refused.status("tee-i") == "NG" and "プロジェクト番号" in refused.text("tee-i")


def test_the_audit_log_queries_look_for_other_principals_and_for_the_vm_after_the_new_key_version(repo):
    result = run_script(repo, make_world("tee"), "--only", "tee-g")

    reads = [call for call in result.commands if "logging read" in call]
    others = next(call for call in reads if "NOT protoPayload.authenticationInfo.principalSubject" in call)
    mine = next(call for call in reads if "NOT protoPayload.authenticationInfo.principalSubject" not in call)
    for call in (others, mine):
        assert 'timestamp>="2026-10-03T12:00:00.123456Z"' in call  # 新しい版(primary)を作った時刻の後
        assert 'protoPayload.serviceName="cloudkms.googleapis.com"' in call and "Encrypt" in call and "Decrypt" in call
        assert "cloudaudit.googleapis.com%2Fdata_access" in call and "NOT protoPayload.status.code>0" in call
    assert SUBJECT in others and SUBJECT in mine  # 本番の VM の subject(ダイジェスト・プロジェクト番号・インスタンス ID)


# ---- (h) の再起動 ----


def test_h_reset_is_skipped_without_the_flag_and_never_restarts_the_vm(repo):
    result = run_script(repo, make_world("tee"), "--only", "tee-h,tee-h-reset")

    assert result.status("tee-h") == "OK" and result.status("tee-h-reset") == "SKIP"
    assert not any(verb in call for call in result.commands for verb in ("instances reset", "instances stop", "instances start"))


def test_h_reset_restarts_once_and_checks_that_the_old_ciphertext_opens(repo):
    result = run_script(repo, make_world("tee"), "--only", "tee-h-reset", "--reset-vault")

    assert result.status("tee-h-reset") == "OK" and result.code == 0
    stops = [call for call in result.commands if "instances stop" in call]
    starts = [call for call in result.commands if "instances start" in call]
    assert len(stops) == 1 and len(starts) == 1 and "vault-tee" in stops[0] and "vault-tee" in starts[0]
    assert not any("instances reset" in call for call in result.commands)  # reset は vTPM の DA ロックアウトのカウンタを増やすので使わない
    # 再起動の前に _tee/selftest があることを確かめてから、停止→開始の順に行う
    selftest_read = next(index for index, call in enumerate(result.commands) if call.startswith("curl ") and "_tee/selftest" in call)
    assert selftest_read < result.commands.index(stops[0]) < result.commands.index(starts[0])


@pytest.mark.parametrize(
    "change, fragment",
    [
        (lambda w: w.update(launcher_logs=[]), "出ていない"),
        (lambda w: w.update(launcher_logs=[{"textPayload": "sealing self-test: created the probe"}, {"textPayload": "sealing self-test ok"}]), "作り直した"),
        (lambda w: w.update(selftest_status=404), "_tee/selftest がない"),
        (lambda w: w["fail"].update(stop=(1, "ERROR: (gcloud.compute.instances.stop) denied")), "停止できない"),
    ],
)
def test_h_reset_is_ng_when_the_self_test_does_not_come_back(repo, change, fragment):
    result = run_script(repo, mutated("tee", change), "--only", "tee-h-reset", "--reset-vault")

    assert result.status("tee-h-reset") == "NG" and fragment in result.text("tee-h-reset"), result.stdout
    assert result.code == 1


def test_h_reset_does_not_restart_when_there_is_no_self_test_to_check_afterwards(repo):
    result = run_script(repo, mutated("tee", lambda w: w.update(selftest_status=404)), "--only", "tee-h-reset", "--reset-vault")

    assert not any(verb in call for call in result.commands for verb in ("instances reset", "instances stop", "instances start"))  # 確かめる材料がないまま、再起動しない
