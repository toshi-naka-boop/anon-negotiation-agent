"""TEE スパイクのイメージとビルドのファイル(契約 §13)と、手動の確認の手順書の静的な確認。

Docker も GCP も使わない(この Mac には Docker が無い)。Cloud Build に出す前に、ファイルの中身だけで確かめられることを確かめる。

Dockerfile.vault(金庫専用のイメージ)
- ポートの公開が、config/params.toml の [vault.tee] port と同じ。launch policy のラベルは 2 つだけ。起動コマンドは固定(ENTRYPOINT)で引数なし。
  非 root のユーザーを作らない。
- COPY するのは、依存のファイル 3 つ(先)と、src/vault・src/negotiation_core・config/params.toml・fixtures だけ。依存の層(uv sync)が、
  コードの COPY より先。fixtures は、金庫が起動時にテンプレートを投入するために読む(src/vault/seed.py)ので、金庫のコードが探す位置
  (コードの 2 つ上のディレクトリの fixtures/)に置く。
- イメージに入るコード(src/vault・src/negotiation_core)が、入らない web・agents を import していない。
Dockerfile(アプリ。Cloud Run 用)
- 土台が Dockerfile.vault と同じ。src・config・scripts・deploy・fixtures を COPY する。static/ は「あれば」: 有るときだけ COPY する
  (無いディレクトリの COPY はビルドが失敗し、有るのに COPY しないと画面が入らない)。起動コマンドは web。
- どちらの Dockerfile も、COPY の元が実在し、.dockerignore に除かれていない。
cloudbuild.vault.yaml
- YAML として読め、_IMAGE・_COMMIT を使って、Dockerfile.vault をビルドして push する。
.gcloudignore・.dockerignore
- 2 つは同じ中身。設計書・テスト・秘密・キャッシュを除き、ビルドに要るもの(実行時に読む src/agents/instructions/*.md を含む)を残す。
  判定は、.dockerignore の規則(moby の patternmatcher と同じ)で行う。書き方を、どちらの道具でも同じ意味になる形にしてある。
deploy/vault-releases.json
- 契約の形({"releases": [{"digest", "commit", "built_at", "status": "active" | "revoked"}]}。revoked には revoked_at がつく)で、
  digest は重複せず、indent=2・末尾に改行。
tests/manual/tee-spike.md
- 点 1〜6 の節があり、各節に 5 つの小見出しがある。コードブロックは 1 つに 1 コマンド。
"""

import ast
import datetime as dt
import json
import re
import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pytest
import yaml  # PyYAML。google-adk が使うので uv.lock にある(テストでだけ直接使う)

from vault.fixtures import FIXTURES_DIRECTORY

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE_KEYWORDS = {
    "ADD", "ARG", "CMD", "COPY", "ENTRYPOINT", "ENV", "EXPOSE", "FROM", "HEALTHCHECK", "LABEL",
    "MAINTAINER", "ONBUILD", "RUN", "SHELL", "STOPSIGNAL", "USER", "VOLUME", "WORKDIR",
}

# ----------------------------------------------------------------------
# 部品: Dockerfile の読み取り
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Instruction:
    keyword: str
    argument: str


def parse_dockerfile(path: Path) -> list[Instruction]:
    """Dockerfile を命令ごとに分ける(行末の \\ の継続をつなぎ、コメント行と空行を除く)。"""
    instructions: list[Instruction] = []
    pending = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("#") or (not line and not pending):
            continue
        if line.endswith("\\"):
            pending += line[:-1].rstrip() + " "
            continue
        keyword, _, argument = (pending + line).partition(" ")
        instructions.append(Instruction(keyword.upper(), argument.strip()))
        pending = ""
    assert not pending, f"{path.name} の最後が継続行のまま"
    return instructions


def of_kind(instructions: list[Instruction], keyword: str) -> list[Instruction]:
    return [item for item in instructions if item.keyword == keyword]


def env_of(instructions: list[Instruction]) -> dict[str, str]:
    """ENV 命令(KEY=VALUE の並び)をまとめた辞書。"""
    pairs = (token.partition("=") for item in of_kind(instructions, "ENV") for token in shlex.split(item.argument))
    return {key: value for key, _, value in pairs}


def copies_of(instructions: list[Instruction]) -> list[tuple[list[str], str]]:
    """COPY 命令(--from つきを除く)の (元の並び, 先)。"""
    copies = []
    for item in of_kind(instructions, "COPY"):
        tokens = shlex.split(item.argument)
        if any(token.startswith("--from=") for token in tokens):
            continue
        paths = [token for token in tokens if not token.startswith("--")]
        copies.append((paths[:-1], paths[-1]))
    return copies


def copy_sources(instructions: list[Instruction]) -> list[str]:
    return [source for sources, _ in copies_of(instructions) for source in sources]


@pytest.fixture(scope="module")
def vault_dockerfile() -> list[Instruction]:
    return parse_dockerfile(ROOT / "Dockerfile.vault")


@pytest.fixture(scope="module")
def app_dockerfile() -> list[Instruction]:
    return parse_dockerfile(ROOT / "Dockerfile")


@pytest.fixture(scope="module")
def tee_config() -> dict:
    with (ROOT / "config" / "params.toml").open("rb") as f:
        return tomllib.load(f)["vault"]["tee"]


# ----------------------------------------------------------------------
# 部品: .dockerignore の判定(moby の patternmatcher と同じ規則)
# ----------------------------------------------------------------------


def compile_ignore_pattern(pattern: str) -> re.Pattern[str]:
    """.dockerignore の 1 行を正規表現にする: * は / 以外、? は / 以外の 1 文字、** は / を含む任意(`**/` は 0 個以上のディレクトリ)。"""
    regex, index = "", 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if pattern[index + 1 : index + 2] == "*":
                index += 1
                if pattern[index + 1 : index + 2] == "/":
                    index += 1
                regex += ".*" if index + 1 == len(pattern) else "(.*/)?"
            else:
                regex += "[^/]*"
        elif char == "?":
            regex += "[^/]"
        else:
            regex += re.escape(char)
        index += 1
    return re.compile(f"^{regex}$")


def parse_ignore_rules(text: str) -> list[tuple[bool, re.Pattern[str]]]:
    """.dockerignore の中身を (! の行か, 正規表現) の並びにする(空行とコメントは除く)。"""
    rules = []
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            rules.append((line.startswith("!"), compile_ignore_pattern(line.removeprefix("!").strip("/"))))
    return rules


def is_ignored(rules: list[tuple[bool, re.Pattern[str]]], path: str) -> bool:
    """上から順に見る。除外の行は、まだ除かれていないときに、! の行は、除かれているときに効く。親ディレクトリが一致しても、除かれる。"""
    parts = path.split("/")
    candidates = ["/".join(parts[: count + 1]) for count in range(len(parts))]
    ignored = False
    for negated, regex in rules:
        if negated != ignored:
            continue
        if any(regex.match(candidate) for candidate in candidates):
            ignored = not negated
    return ignored


@pytest.fixture(scope="module")
def ignore_rules() -> list[tuple[bool, re.Pattern[str]]]:
    return parse_ignore_rules((ROOT / ".dockerignore").read_text(encoding="utf-8"))


# ----------------------------------------------------------------------
# Dockerfile.vault
# ----------------------------------------------------------------------


def test_vault_dockerfile_exposes_the_port_in_the_config(vault_dockerfile, tee_config):
    assert [item.argument for item in of_kind(vault_dockerfile, "EXPOSE")] == [f"{tee_config['port']}/tcp"] == ["8443/tcp"]


def test_vault_dockerfile_has_only_the_two_launch_policy_labels(vault_dockerfile):
    labels = dict(token.split("=", 1) for item in of_kind(vault_dockerfile, "LABEL") for token in shlex.split(item.argument))

    assert labels == {
        "tee.launch_policy.log_redirect": "always",
        "tee.launch_policy.monitoring_memory_allow": "never",
    }


def test_vault_dockerfile_fixes_the_start_command(vault_dockerfile):
    (entrypoint,) = of_kind(vault_dockerfile, "ENTRYPOINT")
    (cmd,) = of_kind(vault_dockerfile, "CMD")

    assert json.loads(entrypoint.argument) == ["/opt/venv/bin/python", "-m", "vault.tee.main"]
    assert json.loads(cmd.argument) == []


def test_vault_dockerfile_does_not_switch_user(vault_dockerfile):
    assert of_kind(vault_dockerfile, "USER") == []


def test_vault_dockerfile_copies_only_the_dependency_files_then_the_vault_code(vault_dockerfile):
    assert copies_of(vault_dockerfile) == [
        (["pyproject.toml", "uv.lock", ".python-version"], "./"),
        (["src/vault"], "./src/vault"),
        (["src/negotiation_core"], "./src/negotiation_core"),
        (["config/params.toml"], "./config/params.toml"),
        (["fixtures"], "./fixtures"),
    ]
    assert of_kind(vault_dockerfile, "ADD") == []


def test_vault_dockerfile_puts_the_fixtures_where_the_vault_code_looks_for_them(vault_dockerfile):
    """金庫は、fixtures.py の 2 つ上のディレクトリの fixtures/ を読む(vault.fixtures.FIXTURES_DIRECTORY)。イメージでも同じ相対位置に置く。"""
    (workdir,) = [item.argument for item in of_kind(vault_dockerfile, "WORKDIR")]
    destinations = {tuple(sources): destination for sources, destination in copies_of(vault_dockerfile)}

    def in_image(destination: str) -> PurePosixPath:
        return PurePosixPath(workdir) / destination

    code_in_image = in_image(destinations[("src/vault",)]) / "fixtures.py"

    assert code_in_image.parents[2] / "fixtures" == in_image(destinations[("fixtures",)]) == PurePosixPath("/app/fixtures")
    assert FIXTURES_DIRECTORY == ROOT / "fixtures"  # リポジトリでも同じ関係(コードの側の計算が、この前提から外れていない)
    assert sorted(path.name for path in FIXTURES_DIRECTORY.glob("case*.toml"))  # 投入するフィクスチャが、1 つはある


def test_vault_dockerfile_installs_the_locked_dependencies_before_copying_the_code(vault_dockerfile):
    keywords_and_arguments = [(item.keyword, item.argument) for item in vault_dockerfile]
    sync = keywords_and_arguments.index(("RUN", "uv sync --frozen --no-dev"))
    first_code_copy = next(
        index for index, item in enumerate(vault_dockerfile) if item.keyword == "COPY" and "src/" in item.argument
    )
    dependency_copy = next(
        index for index, item in enumerate(vault_dockerfile) if item.keyword == "COPY" and "uv.lock" in item.argument
    )

    assert dependency_copy < sync < first_code_copy


def test_vault_dockerfile_is_built_on_the_python_in_python_version_with_the_pinned_uv(vault_dockerfile):
    python_version = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    (base,) = of_kind(vault_dockerfile, "FROM")

    assert base.argument == f"python:{python_version}-slim" == "python:3.12-slim"
    assert vault_dockerfile[0] == base
    assert "COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /uvx /bin/" in [
        f"{item.keyword} {item.argument}" for item in vault_dockerfile
    ]


def test_vault_dockerfile_environment(vault_dockerfile):
    assert env_of(vault_dockerfile) == {
        "UV_PROJECT_ENVIRONMENT": "/opt/venv",
        "UV_PYTHON_PREFERENCE": "only-system",
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_LINK_MODE": "copy",
        "UV_COMPILE_BYTECODE": "1",
        "PYTHONPATH": "/app/src",
        "PYTHONUNBUFFERED": "1",
    }
    assert [item.argument for item in of_kind(vault_dockerfile, "WORKDIR")] == ["/app"]


def imports_of(names: set[str], directory: Path) -> list[str]:
    """directory の下の .py が import している、最上位の名前が names にあるモジュール(「ファイル: モジュール」の並び)。"""
    found = []
    for path in sorted(directory.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            else:
                continue
            found += [f"{path.name}: {module}" for module in modules if module.split(".")[0] in names]
    return found


OUTSIDE_THE_VAULT_IMAGE = {"web", "agents", "scripts", "tests"}


def test_vault_image_code_does_not_import_what_the_image_does_not_contain():
    """イメージに入るのは src/vault と src/negotiation_core だけ。web・agents を import していたら、起動で ImportError になる。"""
    for package in ("vault", "negotiation_core"):
        assert imports_of(OUTSIDE_THE_VAULT_IMAGE, ROOT / "src" / package) == []


def test_the_import_check_does_detect_an_import_from_outside_the_image(tmp_path):
    (tmp_path / "bad.py").write_text("import web.app\nfrom agents import client\nimport vault.store\nfrom . import sibling\n")

    assert imports_of(OUTSIDE_THE_VAULT_IMAGE, tmp_path) == ["bad.py: web.app", "bad.py: agents"]


# ----------------------------------------------------------------------
# Dockerfile(アプリ)
# ----------------------------------------------------------------------


def test_app_dockerfile_has_the_same_base_as_the_vault_dockerfile(app_dockerfile, vault_dockerfile):
    def base(instructions):
        pieces = [f"{item.keyword} {item.argument}" for item in instructions]
        return [piece for piece in pieces if piece.startswith(("FROM ", "COPY --from=", "WORKDIR ", "RUN uv sync"))]

    assert base(app_dockerfile) == base(vault_dockerfile)
    assert env_of(vault_dockerfile).items() <= env_of(app_dockerfile).items()


def test_app_dockerfile_copies_the_runtime_directories(app_dockerfile):
    sources = copy_sources(app_dockerfile)

    required = ["src", "config", "scripts", "deploy", "fixtures"]
    assert sources[:3] == ["pyproject.toml", "uv.lock", ".python-version"]
    assert all(directory in sources for directory in required)
    assert not any(directory in sources for directory in ("tests", "design", ".git", ".venv", "tmp"))


@pytest.mark.parametrize("directory", ["static", "fixtures"])
def test_app_dockerfile_copies_an_optional_directory_exactly_when_it_exists(app_dockerfile, directory):
    exists = (ROOT / directory).is_dir()

    assert (directory in copy_sources(app_dockerfile)) == exists, (
        f"{directory}/ が{'有る' if exists else '無い'}のに、Dockerfile の COPY が{'無い' if exists else '有る'}"
        "(無いディレクトリの COPY はビルドが失敗し、有るのに COPY しないとイメージに入らない)"
    )


def test_app_dockerfile_starts_the_web_app_on_the_cloud_run_port(app_dockerfile):
    (cmd,) = of_kind(app_dockerfile, "CMD")
    command = json.loads(cmd.argument)
    environment = env_of(app_dockerfile)

    assert command == ["uvicorn", "web.app:create_app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
    assert command[command.index("--port") + 1] == environment["PORT"] == "8080"
    assert environment["PATH"] == "/opt/venv/bin:$PATH"
    assert re.search(r"^def create_app_from_env\(", (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8"), re.MULTILINE)


@pytest.mark.parametrize("dockerfile", ["Dockerfile", "Dockerfile.vault"])
def test_dockerfiles_use_real_instructions_and_every_copy_source_exists_and_is_not_ignored(dockerfile, ignore_rules):
    instructions = parse_dockerfile(ROOT / dockerfile)

    assert {item.keyword for item in instructions} <= DOCKERFILE_KEYWORDS
    assert instructions[0].keyword == "FROM"
    for source in copy_sources(instructions):
        assert (ROOT / source).exists(), f"{dockerfile}: COPY の元 {source} が無い(ビルドが失敗する)"
        assert not is_ignored(ignore_rules, source), f"{dockerfile}: COPY の元 {source} が .dockerignore に除かれている"


# ----------------------------------------------------------------------
# cloudbuild.vault.yaml
# ----------------------------------------------------------------------


def strings_in(node) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [text for value in node.values() for text in strings_in(value)]
    if isinstance(node, list):
        return [text for value in node for text in strings_in(value)]
    return []


def test_cloud_build_config_builds_the_vault_image_and_pushes_it():
    config = yaml.safe_load((ROOT / "cloudbuild.vault.yaml").read_text(encoding="utf-8"))
    build, push = config["steps"]

    assert build["name"] == push["name"] == "gcr.io/cloud-builders/docker"
    assert build["args"] == [
        "build", "-f", "Dockerfile.vault", "-t", "${_IMAGE}", "--label", "org.opencontainers.image.revision=${_COMMIT}", ".",
    ]
    assert push["args"] == ["push", "${_IMAGE}"]
    assert config["images"] == ["${_IMAGE}"]
    assert config["options"] == {"logging": "CLOUD_LOGGING_ONLY"}
    assert (ROOT / "Dockerfile.vault").is_file()


def test_cloud_build_config_uses_exactly_the_two_substitutions_image_and_commit():
    config = yaml.safe_load((ROOT / "cloudbuild.vault.yaml").read_text(encoding="utf-8"))

    used = set(re.findall(r"\$\{?(\w+)\}?", " ".join(strings_in(config))))

    assert used == {"_IMAGE", "_COMMIT"}


# ----------------------------------------------------------------------
# .gcloudignore・.dockerignore
# ----------------------------------------------------------------------


def test_gcloudignore_and_dockerignore_are_identical():
    assert (ROOT / ".gcloudignore").read_bytes() == (ROOT / ".dockerignore").read_bytes()


def test_the_ignore_matcher_follows_the_docker_rules():
    """判定の部品の確かめ: `*` は `/` を越えない。`**/` はどの深さにも効く。`!` は、除かれたものを戻す。"""
    rules = parse_ignore_rules("*.md\n**/*.pyc\ntmp\n!tmp/keep.txt\n")

    assert is_ignored(rules, "README.md") and not is_ignored(rules, "docs/README.md")
    assert is_ignored(rules, "c.pyc") and is_ignored(rules, "a/b/c.pyc")
    assert is_ignored(rules, "tmp/x") and not is_ignored(rules, "tmp/keep.txt")
    assert not is_ignored(rules, "src/app.py")


@pytest.mark.parametrize(
    "path",
    [
        ".git", ".git/config", ".venv/bin/python", "tmp/tee_spike/p1.json", "temp/x",
        "design/anon-negotiation-agent/design.md", "tests/test_x.py", "tests/manual/tee-spike.md",
        ".pytest_cache/README.md", ".claude/settings.json", ".claude/worktrees/a/b",
        ".env", ".env.local", "src/web/.env", "credentials.json", "gcp-credentials.json",
        "service-account.json", "config/my-service-account-key.json",
        "src/vault/__pycache__/app.cpython-312.pyc", "scripts/__pycache__/x.cpython-312.pyc", "src/vault/stale.pyc",
        "README.md", "CLAUDE.md", "docs/guide.md", ".DS_Store", "src/vault/.DS_Store",
        # deploy/ と src/ の中の .md は残すが、その中にあっても、秘密とキャッシュは除く(「残す」行で戻されない)
        "deploy/.env", "deploy/prod-credentials.json", "deploy/my-service-account.json", "deploy/__pycache__/x.pyc",
        "src/agents/instructions/.env", "src/agents/instructions/__pycache__/x.pyc",
    ],
)
def test_ignore_files_keep_these_out_of_the_build(ignore_rules, path):
    assert is_ignored(ignore_rules, path)


@pytest.mark.parametrize(
    "path",
    [
        "Dockerfile", "Dockerfile.vault", "cloudbuild.vault.yaml", "pyproject.toml", "uv.lock", ".python-version",
        "config/params.toml", "deploy/vault-releases.json", "deploy/NOTES.md", "src/vault/app.py",
        "src/negotiation_core/__init__.py", "src/agents/instructions/candidate.md", "scripts/tee_probe_client.py",
        "fixtures/case1.toml",
    ],
)
def test_ignore_files_keep_what_the_build_needs(ignore_rules, path):
    assert not is_ignored(ignore_rules, path)


def test_ignore_files_keep_every_file_the_images_copy(ignore_rules):
    """src・config・scripts・deploy・fixtures の実在のファイルが、1 つも除かれていない(実行時に読む .md などを巻き込まない)。"""
    ignored = []
    for directory in ("src", "config", "scripts", "deploy", "fixtures", "static"):
        for path in sorted((ROOT / directory).rglob("*")) if (ROOT / directory).is_dir() else []:
            relative = path.relative_to(ROOT).as_posix()
            if path.is_file() and "__pycache__" not in relative and not relative.endswith((".pyc", ".DS_Store")):
                if is_ignored(ignore_rules, relative):
                    ignored.append(relative)

    assert ignored == []


def test_the_agent_instruction_files_are_in_the_files_the_images_copy():
    """.md を除く規則の例外(src/**/*.md)が要る理由: エージェントの指示文が .md で、実行時に読まれる。"""
    assert sorted(path.name for path in (ROOT / "src" / "agents" / "instructions").glob("*.md"))


# ----------------------------------------------------------------------
# deploy/vault-releases.json
# ----------------------------------------------------------------------


def assert_release_table_shape(raw: str) -> None:
    """表のファイルの中身が、契約の形どおりか(外れていれば AssertionError)。"""
    table = json.loads(raw)

    assert list(table) == ["releases"] and isinstance(table["releases"], list)
    for release in table["releases"]:
        assert release.get("status") in ("active", "revoked")
        revoked = release["status"] == "revoked"
        assert list(release) == ["digest", "commit", "built_at", "status", *(["revoked_at"] if revoked else [])]
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", release["digest"])
        assert re.fullmatch(r"[0-9a-f]{40}", release["commit"])
        for key in ("built_at", *(["revoked_at"] if revoked else [])):
            assert dt.datetime.fromisoformat(release[key]).tzinfo is not None
    digests = [release["digest"] for release in table["releases"]]
    assert len(digests) == len(set(digests))
    assert raw == json.dumps(table, indent=2) + "\n"  # scripts/tee_record_release.py が書くのと同じ形


def test_release_table_has_the_agreed_shape():
    assert_release_table_shape((ROOT / "deploy" / "vault-releases.json").read_text(encoding="utf-8"))


ACTIVE_RELEASE = {"digest": "sha256:" + "ab" * 32, "commit": "c" * 40, "built_at": "2026-10-03T12:00:00Z", "status": "active"}
REVOKED_RELEASE = {**ACTIVE_RELEASE, "digest": "sha256:" + "cd" * 32, "status": "revoked", "revoked_at": "2026-10-05T00:00:00Z"}


def table_text(*releases: dict) -> str:
    return json.dumps({"releases": list(releases)}, indent=2) + "\n"


def test_the_release_table_check_accepts_active_and_revoked_entries():
    assert_release_table_shape(table_text())
    assert_release_table_shape(table_text(ACTIVE_RELEASE, REVOKED_RELEASE))


@pytest.mark.parametrize(
    "release",
    [
        {key: value for key, value in ACTIVE_RELEASE.items() if key != "status"},
        {**ACTIVE_RELEASE, "status": "pending"},
        {**ACTIVE_RELEASE, "revoked_at": "2026-10-05T00:00:00Z"},
        {key: value for key, value in REVOKED_RELEASE.items() if key != "revoked_at"},
        {**REVOKED_RELEASE, "revoked_at": "2026-10-05"},
        {**ACTIVE_RELEASE, "commit": "c" * 7},
        {**ACTIVE_RELEASE, "digest": "sha256:" + "AB" * 32},
        {**ACTIVE_RELEASE, "built_at": "2026-10-03T12:00:00"},
        {**ACTIVE_RELEASE, "note": "extra"},
    ],
    ids=["no-status", "unknown-status", "active-with-revoked-at", "revoked-without-revoked-at", "revoked-at-without-timezone",
         "short-commit", "uppercase-digest", "built-at-without-timezone", "extra-key"],
)
def test_the_release_table_check_rejects_a_table_that_leaves_the_agreed_shape(release):
    with pytest.raises(AssertionError):
        assert_release_table_shape(table_text(release))


def test_the_release_table_check_rejects_a_duplicate_digest_and_a_file_in_another_format():
    with pytest.raises(AssertionError):
        assert_release_table_shape(table_text(ACTIVE_RELEASE, {**ACTIVE_RELEASE, "commit": "d" * 40}))
    with pytest.raises(AssertionError):
        assert_release_table_shape(json.dumps({"releases": []}))  # 末尾の改行・indent=2 でない


# ----------------------------------------------------------------------
# tests/manual/tee-spike.md
# ----------------------------------------------------------------------

MANUAL = ROOT / "tests" / "manual" / "tee-spike.md"
MANUAL_PARTS = ("どの手順の段で", "実行するコマンド", "合格の出力", "貼ってもらう出力", "不合格のときの代替")


def test_manual_has_a_section_for_each_of_the_six_points_with_the_five_parts():
    text = MANUAL.read_text(encoding="utf-8")

    chapters = re.split(r"^(?=## )", text, flags=re.MULTILINE)  # 「## 」の見出しごと(「### 」では分けない)
    sections = [chapter for chapter in chapters if re.match(r"## 点 \d", chapter)]

    assert [section.split(":")[0] for section in sections] == [f"## 点 {number}" for number in range(1, 7)]
    for section in sections:
        headings = re.findall(r"^### (.+?)\s*$", section, flags=re.MULTILINE)
        assert headings == list(MANUAL_PARTS), section.splitlines()[0]


def test_manual_code_blocks_hold_one_command_each():
    text = MANUAL.read_text(encoding="utf-8")

    blocks = re.findall(r"^```[^\n]*\n(.*?)^```$", text, flags=re.MULTILINE | re.DOTALL)

    assert blocks
    for block in blocks:
        commands = [line for line in re.sub(r"\\\n", " ", block).splitlines() if line.strip() and not line.lstrip().startswith("#")]
        assert len(commands) == 1, block
