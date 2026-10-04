"""画面(static/)と、画面に要る小さな口(src/web/ui_api.py)の確認(design.md §6.3・§7・§8.4・§10・§12.1 AC-02・AC-19。作業パッケージ L)。

画面は静的な HTML と素の JavaScript・CSS(ビルド工程なし)。ブラウザは動かさないので、ここでは次を確かめる。
- 配信: ページの経路(/・/interview・/me・/demo・/attack)が対応する HTML を返し、/static が静的ファイルを返す。どれも依頼者 ID を発行せず
  (発行は開始ページの GET /start だけ。§6.3)、/static はセッションを見ない。HTML が参照するファイルは、すべて実在する。
- 画面のコードの規則: ブラウザの保存領域に書かない(scripts/check_no_web_storage.sh。AC-02)、API の文字列を HTML として組み立てる道がない、
  外部の読み込み・インラインのスクリプトがない(CSP)、JS が呼ぶ API の経路は実在する("METHOD /path" の文字列を、実際の経路と照らし合わせる)、
  JS が参照する要素の id は HTML に実在する。
- 画面に要る口(web.ui_api): セッション・面談の注記・求人・デモのケース・リプレイ・SSE(活動ログ)。
- SSE は、ミドルウェアの依頼者ごとのロックを持たない(つながっている間、同じ依頼者の操作が止まらない)。
- SSE の同時本数の上限(台帳 C-65): 全体とクライアント IP ごと。超えたら 429、接続が終われば(自分で閉じた・クライアントが切った・確認の失敗・例外)、
  必ず席が戻る。メモリの中だけで数え、rate_limits には触れない。
- 本番の組み立て(create_app_from_env)は、/docs・/redoc・/openapi.json を出さない(台帳 L19-10)。開発用(create_app(docs=True))は出す。
- 画面の活動ログの購読(static/ui.js の watchNegotiation)は、node があれば、偽の EventSource で動かして確かめる(なければ、そのテストは飛ばす)。
- 画面の後半(作業パッケージ L2): 段階開示・開示台帳・FR-39 の 2 パネル・推定区間メーター・シミュレーション・二分探索の実演の区画が埋まっていること(data-status="ready"・
  ナビが本物のリンク)、画面の言葉(設計書が求める文言)、画面にある定数がサーバーの値と一致すること、デモ・攻撃の画面が本物の依頼者の API を呼ばないこと。
  node があれば、区画の描画を偽の DOM で動かして確かめる(サンプルのデータは、実際の API のモデルから作る)。
金庫は本物の vault の app を ASGI のままつなぎ、web の app へは Browser(クッキーを持つ httpx のクライアント)から入る。
"""

import asyncio
import contextlib
import datetime as dt
import functools
import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from typing import get_args

import httpx
import pytest
import web.app as web_app_module
from negotiation_core import AXES, Anchor, Policy, Verdict
from negotiation_core.estimate_interval import Interval
from sse_starlette import ServerSentEvent
from vault.api_models import EventViewItem, MoveRequest, PolicyView
from vault.fixtures import FIXTURES_DIRECTORY, load_case_fixture
from vault.models import EmployerRule, NegotiationResult
from vault.seed import seed_templates
from vault_helpers import needs_confirmation_policy, sample_package
from web import activity_api, meter_api, ui_api
from web.activity_api import ActivityEntry, ActivityLog
from web.app import create_app, create_app_from_env
from web.attack.scripted import probe_package
from web.ledger import LedgerEntry, LedgerOperator, LedgerRecipient
from web.limits import SseConnectionLimiter, SseLimitConfig
from web.panels_api import build_panels
from web.stages import DEFAULT_STAGES_CONFIG, CompanyView, EmployerDisclosure, Item, SideFlagsView, StageView
from web.session import SESSION_COOKIE_NAME, SESSION_KEY_ENV
from web.ui_api import StreamConfig, StreamNotAllowed, activity_event_stream, build_ui_router, list_cases, list_jobs
from web.vault_client import VaultUnavailableError
from web_app_helpers import dump_documents, submit_interview
from web_helpers import create_demo_negotiation

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
SCRIPT = ROOT / "scripts" / "check_no_web_storage.sh"
PAGES = {"/": "index.html", "/interview": "interview.html", "/me": "me.html", "/demo": "demo.html", "/attack": "attack.html"}
HTML_FILES = sorted(STATIC.glob("*.html"))
JS_FILES = sorted(STATIC.glob("*.js"))
ALL_CODE = [*HTML_FILES, *JS_FILES, *sorted(STATIC.glob("*.css"))]


# ----------------------------------------------------------------------
# 部品: HTML の読み取り
# ----------------------------------------------------------------------


class Page(HTMLParser):
    """HTML から、参照(link・script・a・img)・id・インラインのスクリプトとスタイルを集める。"""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.refs: list[tuple[str, str]] = []  # (タグ, 参照先)
        self.ids: list[str] = []
        self.inline_scripts = 0
        self.style_tags = 0
        self.style_attributes = 0
        self.current_pages: list[str] = []  # aria-current="page" を持つ要素の href
        self._in_script = False
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if "id" in attributes:
            self.ids.append(attributes["id"])
        if "style" in attributes:
            self.style_attributes += 1
        if attributes.get("aria-current") == "page":
            self.current_pages.append(attributes.get("href", ""))
        if tag == "style":
            self.style_tags += 1
        for name in ("src", "href"):
            if tag in ("link", "script", "a", "img") and name in attributes:
                self.refs.append((tag, attributes[name]))
        if tag == "script":
            self._in_script = True

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if self._in_script and data.strip():
            self.inline_scripts += 1


def page_of(path: Path) -> Page:
    return Page(path.read_text(encoding="utf-8"))


# ----------------------------------------------------------------------
# 部品: SSE
# ----------------------------------------------------------------------

FAST_STREAM = StreamConfig(poll_interval_seconds=0.02, max_duration_seconds=5.0, retry_milliseconds=100)


@pytest.fixture
def fast_stream(monkeypatch):
    """SSE を速く動かす(周期 0.02 秒・最長 5 秒)。web_app より先に要求すること(app を作るときに、この設定が入る)。"""
    monkeypatch.setattr("web.api.build_ui_router", functools.partial(build_ui_router, stream=FAST_STREAM))


def parse_sse(text: str) -> list[dict[str, str]]:
    """SSE の本文を、イベント(event・id・data・retry の dict)の並びにする。コメント行(`:` で始まる ping)は除く。"""
    events = []
    for block in re.split(r"\r?\n\r?\n", text):
        fields = {}
        for line in block.splitlines():
            if line and not line.startswith(":"):
                name, _, value = line.partition(":")
                fields[name] = value.removeprefix(" ")
        if fields:
            events.append(fields)
    return events


class Sse:
    """app に ASGI のまま GET して、応答(SSE)を受け取り続ける。

    httpx の ASGITransport は、応答が終わるまで返さないので、自分では終わらない(閉じない)SSE は、これで読む。
    `async with` を抜けるときに、クライアントの切断を app に知らせて、終わるのを待つ。
    """

    def __init__(self, app, target: str, *, cookie: str | None = None, headers: dict[str, str] | None = None) -> None:
        path, _, query = target.partition("?")
        raw_headers = [(b"host", b"web.test")]
        if cookie is not None:
            raw_headers.append((b"cookie", f"{SESSION_COOKIE_NAME}={cookie}".encode()))
        raw_headers += [(name.lower().encode(), value.encode()) for name, value in (headers or {}).items()]
        self._app = app
        self._scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query.encode(),
            "root_path": "",
            "headers": raw_headers,
            "client": ("127.0.0.1", 50000),
            "server": ("web.test", 443),
        }
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self._body = bytearray()
        self._changed = asyncio.Event()
        self._disconnected = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def _receive(self):
        await self._disconnected.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = {name.decode().lower(): value.decode() for name, value in message["headers"]}
        elif message["type"] == "http.response.body":
            self._body += message.get("body", b"")
        self._changed.set()

    async def _wait(self, timeout: float) -> None:
        await asyncio.wait_for(self._changed.wait(), timeout)
        self._changed.clear()

    async def __aenter__(self) -> "Sse":
        self._task = asyncio.create_task(self._app(self._scope, self._receive, self._send))
        while self.status is None:
            if self._task.done():
                self._task.result()  # 応答の前に落ちたなら、その例外を出す
                break
            await self._wait(10)
        return self

    async def __aexit__(self, *exc_info) -> None:
        self._disconnected.set()
        await asyncio.wait_for(self._task, 10)

    @property
    def body(self) -> bytes:
        return bytes(self._body)

    @property
    def events(self) -> list[dict[str, str]]:
        return parse_sse(self._body.decode("utf-8"))

    async def wait_events(self, count: int, timeout: float = 10) -> list[dict[str, str]]:
        """イベントが count 件そろうか、応答が終わるまで待って、いまのイベントを返す。"""
        while len(self.events) < count and not self._task.done():
            await self._wait(timeout)
        return self.events

    async def finished(self, timeout: float = 10) -> list[dict[str, str]]:
        """応答が自分で終わるのを待つ(終わらなければ、時間切れで失敗する)。"""
        await asyncio.wait_for(asyncio.shield(self._task), timeout)
        return self.events


def _move(store, nid: str, side: str, move: str, package=None) -> None:
    """金庫に手を 1 つ登録する。"""
    version = store.get_view(nid, side).version
    store.process_move(nid, MoveRequest(expected_version=version, side=side, move=move, package=package))


def _entry(seq: int, action: str = "check", actor: str = "self", **fields) -> ActivityEntry:
    return ActivityEntry(seq=seq, actor=actor, action=action, **fields)


def _log(entries: list[ActivityEntry], next_after_seq: int, side: str = "candidate") -> ActivityLog:
    return ActivityLog(side=side, entries=entries, next_after_seq=next_after_seq)


# ----------------------------------------------------------------------
# 配信: ページの経路と /static(依頼者 ID を発行しない。§6.3)
# ----------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(("route", "filename"), list(PAGES.items()))
async def test_a_page_route_returns_its_html_and_never_issues_a_session(web_app, route, filename):
    browser = web_app.browser()

    response = await browser.get(route)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.content == (STATIC / filename).read_bytes()
    assert 'lang="ja"' in response.text
    assert "set-cookie" not in response.headers and browser.cookie is None
    # 外部の読み込みもインラインのスクリプトも許さないヘッダ(画面のコードは、同じ配信元の静的なファイルだけを使う)
    policy = response.headers["content-security-policy"]
    for directive in ("default-src 'none'", "script-src 'self'", "style-src 'self'", "connect-src 'self'", "frame-ancestors 'none'"):
        assert directive in policy, directive
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-cache"


@pytest.mark.anyio
async def test_only_the_start_page_issues_a_session(web_app):
    # §6.3: 依頼者 ID を発行するのは、開始ページの GET(/start)だけ。画面のページ・静的ファイル・画面に要る口は、発行しない。
    browser = web_app.browser()

    for route in (*PAGES, "/static/app.css", "/v1/session", "/v1/jobs", "/v1/demo/cases", "/v1/interview/notice"):
        assert (await browser.get(route)).status_code == 200, route
    assert browser.cookie is None

    assert (await browser.get("/start")).status_code == 200
    assert browser.cookie is not None


@pytest.mark.anyio
async def test_static_files_are_served_with_their_types_and_without_the_session(web_app):
    # /static はセッションを見ない: 有効なクッキーがあっても、利用記録を更新せず、クッキーの期限も延ばさない。
    browser = web_app.browser()
    pid = await browser.register()
    meta = web_app.default_db.collection("principals_meta").document(pid)
    before = meta.get().to_dict()
    web_app.clock.advance(dt.timedelta(hours=2))  # 1 時間を過ぎているので、セッションを見る経路なら利用記録を更新する

    responses = {name: await browser.get(f"/static/{name}") for name in ("app.css", "api.js", "ui.js", "me.js")}

    for name, response in responses.items():
        assert response.status_code == 200, name
        assert "set-cookie" not in response.headers, name
        assert response.headers["cache-control"] == "no-cache", name  # 再デプロイの後に、古い JS が残らない
        assert response.content == (STATIC / name).read_bytes(), name
    assert responses["app.css"].headers["content-type"].startswith("text/css")
    assert "javascript" in responses["api.js"].headers["content-type"]  # モジュールのスクリプトは JavaScript の型が要る
    assert meta.get().to_dict() == before
    # 対照: セッションを見る経路(/v1/session)なら、同じ時刻の進みで利用記録が更新される(上の「変わらない」が、時刻の条件のせいではない)
    assert (await browser.get("/v1/session")).status_code == 200
    assert meta.get().to_dict() != before


@pytest.mark.anyio
async def test_the_static_route_does_not_serve_anything_outside_static(web_app):
    browser = web_app.browser()

    outside = [
        await browser.get("/static/%2e%2e/src/web/app.py"),
        await browser.get("/static/..%2fsrc%2fweb%2fapp.py"),
        await browser.get("/static/"),
        await browser.get("/static/no-such-file.js"),
    ]
    assert [response.status_code for response in outside] == [404, 404, 404, 404]
    assert (await browser.post("/static/app.css")).status_code in (404, 405)  # 静的ファイルへの POST は通らない


@pytest.mark.anyio
async def test_the_api_routes_are_still_served_next_to_the_pages(web_app):
    # ページの経路と /static を足しても、既存の API の経路(/health・/start)は変わらない。
    browser = web_app.browser()

    assert (await browser.get("/health")).json() == {"status": "ok"}
    assert (await browser.get("/start")).json() == {"status": "ok"}
    assert web_app.app.openapi()["paths"]  # スキーマの生成が、足した口で壊れていない(/openapi.json の配信は、本番では止める。次の試験。台帳 L19-10)


@pytest.mark.anyio
async def test_the_production_assembly_does_not_serve_the_api_docs_and_the_development_one_does(default_db, session_key, monkeypatch):
    # 台帳 L19-10: /docs は CDN の Swagger UI の JS を読み込み、ページに付けている CSP が掛からないので、セッションのクッキーと同じ配信元で第三者の JS が動く。
    # 本番の起動口(create_app_from_env)は /docs・/redoc・/openapi.json を出さない(404)。create_app の既定も出さない。開発用(docs=True)だけが出す。
    monkeypatch.setattr(web_app_module, "_create_default_db", lambda: default_db)
    production = create_app_from_env({SESSION_KEY_ENV: session_key, "VAULT_BASE_URL": "http://vault.test"})
    by_default = create_app(vault=object(), default_db=default_db, session_key=session_key)
    development = create_app(vault=object(), default_db=default_db, session_key=session_key, docs=True)
    paths = ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect")

    async def statuses(app) -> dict[str, int]:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://web.test") as client:
            return {path: (await client.get(path)).status_code for path in paths}

    assert await statuses(production) == {path: 404 for path in paths}
    assert await statuses(by_default) == {path: 404 for path in paths}  # 出すと決めたときだけ出す
    assert await statuses(development) == {path: 200 for path in paths}
    # 本番でも、画面と API は動く(スキーマの生成そのものは、app.openapi() で確かめている)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=production), base_url="https://web.test") as client:
        assert (await client.get("/health")).json() == {"status": "ok"}
        assert (await client.get("/")).status_code == 200


# ----------------------------------------------------------------------
# 画面のコードの規則
# ----------------------------------------------------------------------


def test_the_expected_files_exist():
    assert {path.name for path in HTML_FILES} == set(PAGES.values())
    for name in (
        "app.css", "api.js", "ui.js", "index.js", "interview.js", "me.js", "demo.js", "attack.js",
        "stages.js", "ledger.js", "panels.js", "meter.js",
    ):  # fmt: skip
        assert (STATIC / name).is_file(), name


@pytest.mark.parametrize("path", HTML_FILES, ids=lambda path: path.name)
def test_every_file_an_html_page_refers_to_exists(path):
    page = page_of(path)

    assert page.refs  # 読み取りが壊れて、何も確かめずに通らない
    for tag, target in page.refs:
        if target.startswith("/static/"):
            assert (STATIC / target.removeprefix("/static/")).is_file(), f"{path.name}: {tag} {target}"
        elif tag == "a":
            page_target, _, fragment = target.partition("#")
            assert page_target in PAGES, f"{path.name}: the link {target} is not a page"
            if fragment:  # ナビの「段階開示・開示台帳」「推定区間メーター」: 区画の id が、行き先のページにある
                assert fragment in page_of(STATIC / PAGES[page_target]).ids, f"{path.name}: the link {target} has no such section"
        else:
            assert target.startswith("data:"), f"{path.name}: {tag} {target} is neither a static file nor a data URI"
    scripts = [target for tag, target in page.refs if tag == "script"]
    assert scripts == [f"/static/{path.stem}.js"]  # ページごとに、同じ名前のスクリプト 1 つ(共通の部品は、そこから import する)
    assert "/static/app.css" in [target for tag, target in page.refs if tag == "link"]


@pytest.mark.parametrize("path", JS_FILES, ids=lambda path: path.name)
def test_every_module_a_script_imports_exists(path):
    imports = re.findall(r"""from\s+["'](\.{1,2}/[^"']+)["']""", path.read_text(encoding="utf-8"))

    for target in imports:
        assert (STATIC / target).is_file(), f"{path.name}: import {target}"
    if path.name not in ("api.js", "ui.js"):
        assert imports  # ページのスクリプトは、api.js・ui.js を使う


@pytest.mark.parametrize("path", HTML_FILES, ids=lambda path: path.name)
def test_the_html_has_no_inline_script_or_style_and_unique_ids(path):
    page = page_of(path)

    assert (page.inline_scripts, page.style_tags, page.style_attributes) == (0, 0, 0)  # CSP: script-src・style-src は 'self'
    assert len(page.ids) == len(set(page.ids)), [name for name in page.ids if page.ids.count(name) > 1]


@pytest.mark.parametrize("path", ALL_CODE, ids=lambda path: path.name)
def test_the_code_loads_nothing_from_outside(path):
    # フレームワーク・CDN・外部フォントは使わない(ネットワークに出るのは、同じ配信元の API だけ)。
    text = path.read_text(encoding="utf-8")

    assert not re.search(r"https?://|//cdn\.|@import|@font-face", text), path.name


BANNED_IN_JS = ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "srcdoc", "javascript:", "setAttribute(\"style\""]


@pytest.mark.parametrize("path", [*JS_FILES, *HTML_FILES], ids=lambda path: path.name)
def test_the_code_has_no_way_to_turn_a_string_into_html_or_script(path):
    # API の文字列は、textContent(h() の文字列の子)だけで入れる。HTML として解釈する道そのものを、画面のコードに置かない。
    text = path.read_text(encoding="utf-8")

    assert [name for name in BANNED_IN_JS if name in text] == []
    if path.suffix == ".html":
        assert not re.search(r"\son[a-z]+\s*=\s*[\"']", text)  # onclick="..." のような属性のハンドラもない


def run_check_script(*arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), *arguments], capture_output=True, text=True, timeout=60, check=False)


def test_the_web_storage_check_passes_on_the_real_static_directory():
    # AC-02: static/ のコードに、生の値をブラウザの保存領域へ書く呼び出しがない。
    result = run_check_script()

    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


@pytest.mark.parametrize(
    "line",
    [
        "window.localStorage.setItem('k', v);",
        "sessionStorage.k = v;",
        "const db = indexedDB.open('x');",
        "document.cookie = 'a=b';",
        "document.cookie='a=b';",
        "const open = openDatabase('x');",
        "cookieStore.set('a', 'b');",
        "caches.open('x');",
    ],
)
def test_the_web_storage_check_catches_each_way_of_writing_to_the_browser(tmp_path, line):
    (tmp_path / "bad.js").write_text(f"export function save(v) {{\n  {line}\n}}\n", encoding="utf-8")

    result = run_check_script(str(tmp_path))

    assert result.returncode == 1
    assert "bad.js:2" in result.stderr  # 見つけた場所を示す


def test_the_web_storage_check_also_reads_html_and_fails_closed_when_there_is_nothing_to_check(tmp_path):
    (tmp_path / "page.html").write_text("<button onclick=\"localStorage.clear()\">x</button>\n", encoding="utf-8")
    assert run_check_script(str(tmp_path)).returncode == 1
    (tmp_path / "page.html").write_text("<p>nothing here</p>\n", encoding="utf-8")
    assert run_check_script(str(tmp_path)).returncode == 0

    empty = tmp_path / "empty"
    empty.mkdir()
    assert run_check_script(str(empty)).returncode == 1  # 調べるファイルがない: 何も調べずに通らない
    assert run_check_script(str(tmp_path / "no-such-directory")).returncode == 1


# ----------------------------------------------------------------------
# 画面のコードと、実際の API・HTML の食い違い(ブラウザなしで見つける)
# ----------------------------------------------------------------------

ROUTE_LITERAL = re.compile(r"""["'`](GET|POST) (/[^"'`\s]*)["'`]""")


def routes_called_by_the_javascript() -> dict[tuple[str, str], list[str]]:
    """JS が呼ぶ API("METHOD /path/{param}" の文字列。api.js の call)→ それを書いたファイルの一覧。"""
    found: dict[tuple[str, str], list[str]] = {}
    for path in JS_FILES:
        for method, route in ROUTE_LITERAL.findall(path.read_text(encoding="utf-8")):
            found.setdefault((method, route), []).append(path.name)
    return found


@pytest.mark.anyio
async def test_every_api_the_javascript_calls_is_a_real_route(web_app):
    # 経路の打ち間違い・メソッドの違い・パスの変数名の違いを、ブラウザなしで見つける。
    paths = web_app.app.openapi()["paths"]
    called = routes_called_by_the_javascript()

    assert len(called) >= 44  # 読み取りが壊れて、何も確かめずに通らない
    missing = [
        f"{method} {route} ({', '.join(files)})"
        for (method, route), files in called.items()
        if method.lower() not in paths.get(route, {})
    ]
    assert missing == []
    # 画面は、依頼者 ID の発行を /start に、SSE を /v1/stream/ に、リプレイを /v1/demo/replays/{case} に頼る
    for expected in (
        ("GET", "/start"),
        ("GET", "/v1/session"),
        ("GET", "/v1/demo/replays/{case}"),
        ("GET", "/v1/stream/negotiations/{nid}/activity"),
        ("GET", "/v1/stream/demo/negotiations/{nid}/activity"),
        ("GET", "/v1/demo/negotiations/{nid}/panels"),  # 並べて見る画面(2 パネル)の再取得
        # 画面の後半(L2): 段階開示・開示台帳・FR-39 の 2 パネル・メーター・シミュレーション・二分探索の実演
        ("GET", "/v1/negotiations/{nid}/stage"),
        ("POST", "/v1/negotiations/{nid}/stage/meet"),
        ("POST", "/v1/negotiations/{nid}/stage/approve"),
        ("GET", "/v1/principals/{pid}/ledger"),
        ("GET", "/v1/principals/{pid}/panels"),
        ("GET", "/v1/demo/negotiations/{nid}/stage"),
        ("POST", "/v1/demo/meter"),
        ("GET", "/v1/demo/meter/simulation"),
        ("POST", "/v1/demo/attack/bisection"),
    ):
        assert expected in called, expected


def test_only_the_pages_for_a_real_principal_call_the_routes_that_need_the_session():
    # デモ・攻撃の画面は、依頼者のセッションを要る API(/v1/principals・/v1/negotiations)を呼ばない(本物の依頼者には触れない。§6.3)。
    for (method, route), files in routes_called_by_the_javascript().items():
        if route.startswith(("/v1/principals", "/v1/negotiations", "/v1/stream/negotiations")):
            assert set(files) <= {"interview.js", "me.js"}, (method, route, files)


def imported_scripts(path: Path) -> list[Path]:
    """path が import する、画面の JS(`./x.js` の形のもの)。"""
    targets = re.findall(r"""from\s+["'](\./[^"']+)["']""", path.read_text(encoding="utf-8"))
    return [STATIC / target.removeprefix("./") for target in targets]


def script_graph(entry: Path) -> list[Path]:
    """entry と、そこから import をたどって読み込まれる、画面の JS のすべて(共通のモジュールを含む)。"""
    seen: list[Path] = []
    queue = [entry]
    while queue:
        current = queue.pop()
        if current not in seen:
            seen.append(current)
            queue.extend(imported_scripts(current))
    return seen


def test_the_demo_and_attack_pages_load_no_code_that_calls_a_real_principals_api():
    # 共通のモジュール(stages.js など)を、デモ・攻撃の画面も読み込む。その全部(読み込みの連なり)に、依頼者のセッションを要る経路の文字列がない
    # (本物の依頼者の API は、me.js が渡す。モジュールは、経路を知らない。§6.3)。
    prefixes = ("/v1/principals", "/v1/negotiations", "/v1/stream/negotiations")
    for page in ("demo", "attack"):
        graph = script_graph(STATIC / f"{page}.js")
        assert {script.name for script in graph} >= {f"{page}.js", "api.js", "ui.js"}
        for script in graph:
            routes = [route for _method, route in ROUTE_LITERAL.findall(script.read_text(encoding="utf-8"))]
            assert not [route for route in routes if route.startswith(prefixes)], (page, script.name)
    assert {script.name for script in script_graph(STATIC / "demo.js")} >= {"stages.js"}
    assert {script.name for script in script_graph(STATIC / "attack.js")} >= {"meter.js"}
    assert {script.name for script in script_graph(STATIC / "me.js")} >= {"stages.js", "ledger.js", "panels.js"}


def test_the_stream_limit_matches_the_activity_api():
    assert ui_api.MAX_SEQ == activity_api._MAX_SEQ


def test_the_activity_shape_table_of_the_demo_script_matches_the_activity_api():
    # リプレイの記録(金庫のイベントの見え方)を、活動ログの形に直す表は、web.activity_api の _SHAPE と同じ(主体・手の種類)。
    text = (STATIC / "demo.js").read_text(encoding="utf-8")
    start = text.index("const SHAPE = {")
    block = text[start : text.index("};", start)]
    table = {kind: (actor, action) for kind, actor, action in re.findall(r"""(\w+): \["(\w+)", "(\w+)"\]""", block)}

    assert table == {kind: (actor, action) for kind, (actor, action, _fields) in activity_api._SHAPE.items()}


@pytest.mark.parametrize("page", HTML_FILES, ids=lambda path: path.name)
def test_every_element_id_a_page_script_uses_exists_in_the_html(page):
    # el(...)・getElementById(...) で引く id が、そのページの HTML にある(打ち間違いで、画面が動かなくなるのを防ぐ)。
    used: set[str] = set()
    for script in script_graph(STATIC / f"{page.stem}.js"):  # ページのスクリプトと、そこから読み込まれるモジュール
        source = script.read_text(encoding="utf-8")
        used |= set(re.findall(r"""\bel\("([\w-]+)"\)""", source)) | set(re.findall(r"""getElementById\("([\w-]+)"\)""", source))

    assert used  # 読み取りが空振りしていない
    assert used <= set(page_of(page).ids), sorted(used - set(page_of(page).ids))


def test_the_sections_of_the_second_package_are_filled_in_and_the_navigation_links_to_them():
    # L2(段階開示・開示台帳・FR-39 の 2 パネル・メーター・シミュレーション)の区画の id と、「ready」の印。準備中の印・文は、もう残っていない。
    expected = {
        "me.html": {"slot-stages", "slot-ledger", "slot-fr39"},
        "demo.html": {"slot-fr39", "slot-stages"},
        "attack.html": {"slot-meter", "slot-simulation"},
    }
    for name, slots in expected.items():
        source = (STATIC / name).read_text(encoding="utf-8")
        for slot in slots:
            assert re.search(rf'<section[^>]*\bid="{slot}"[^>]*\bdata-status="ready"', source), (name, slot)
    for path in [*HTML_FILES, *JS_FILES, STATIC / "app.css"]:
        source = path.read_text(encoding="utf-8")
        assert 'data-status="pending"' not in source and "準備中" not in source, path.name
    for path in HTML_FILES:  # どのページのナビゲーションにも、「段階開示・開示台帳」「推定区間メーター」の本物のリンクがある
        source = path.read_text(encoding="utf-8")
        assert '<a href="/me#slot-stages" data-slot="nav-stages">段階開示・開示台帳</a>' in source, path.name
        assert '<a href="/attack#slot-meter" data-slot="nav-meter">推定区間メーター</a>' in source, path.name


# 設計書が求める、画面の言葉(§6.2・§7・§8.3)。どれかが消えたら、確かめ直す。
SCREEN_TEXT = [
    ("attack.html", meter_api.NOTE),  # 「金庫の答えをすべて見られたとしても、ここまで」
    ("attack.html", "シミュレーション(防御なしの場合の計算。金庫は使っていません)"),
    ("meter.js", "これ以上は絞れません"),
    ("stages.js", "架空の求人(自動応答)"),
    ("stages.js", "ここで連絡先が開示されます"),
    ("stages.js", "氏名・勤務先・連絡先など、個人が特定できることは書かない"),
    ("panels.js", "最悪漏れてもここまで"),
    ("panels.js", "まだ隠しているもの"),
    ("panels.js", "辞めた理由(面談時に破棄済み)"),
    ("panels.js", "軸どうしの組み合わせ"),  # 軸ごとに分けて見せるので、組み合わせの情報は落ちている、の 1 文
]


@pytest.mark.parametrize(("name", "phrase"), SCREEN_TEXT)
def test_the_screens_say_what_the_design_asks_them_to_say(name, phrase):
    assert phrase in (STATIC / name).read_text(encoding="utf-8")


def js_numbers(name: str, constant: str) -> dict[str, int]:
    """JS の `export const CONSTANT = { low: 300, high: 1500, step: 50 };` の中身を、dict にする。"""
    source = (STATIC / name).read_text(encoding="utf-8")
    body = re.search(rf"export const {constant} = \{{([^}}]*)\}}", source)
    assert body is not None, (name, constant)
    return {key: int(value) for key, value in re.findall(r"(\w+):\s*(\d+)", body.group(1))}


def js_keys(name: str, constant: str) -> set[str]:
    """JS の `const CONSTANT = { key: "...", ... };` の、キーの集合。"""
    source = (STATIC / name).read_text(encoding="utf-8")
    body = re.search(rf"const {constant} = \{{(.*?)\n?\}};", source, flags=re.DOTALL)
    assert body is not None, (name, constant)
    return set(re.findall(r"""(?:^|[\s,{])(\w+):\s*["']""", body.group(1)))


def test_the_constants_the_screens_copy_are_the_servers_values():
    grid = AXES["salary"].grid
    assert js_numbers("meter.js", "SALARY_GRID") == {"low": grid[0], "high": grid[-1], "step": grid[1] - grid[0]}
    assert len({b - a for a, b in zip(grid, grid[1:])}) == 1  # グリッドは等間隔(画面は、位置を値から計算する)
    assert js_numbers("meter.js", "SIMULATION_RANGE") == {
        "low": meter_api.SIMULATION_LOW,
        "high": meter_api.SIMULATION_HIGH,
        "step": meter_api.SIMULATION_STEP,
    }
    assert f"export const MAX_NEGOTIATION_IDS = {meter_api.MAX_NEGOTIATION_IDS};" in (STATIC / "meter.js").read_text(encoding="utf-8")
    assert f"export const JOB_SUMMARY_MAX_CHARS = {DEFAULT_STAGES_CONFIG.job_summary_max_chars};" in (STATIC / "stages.js").read_text(encoding="utf-8")
    assert js_keys("stages.js", "ITEM_LABELS") == set(get_args(Item))  # 段階開示で見せるものの種類
    assert js_keys("ledger.js", "OPERATOR_LABELS") == set(get_args(LedgerOperator))  # 台帳の、操作した主体
    assert js_keys("ledger.js", "RECIPIENT_LABELS") == set(get_args(LedgerRecipient))  # 台帳の、見せた相手


@pytest.mark.parametrize(("route", "filename"), list(PAGES.items()))
def test_every_page_has_the_same_navigation_and_marks_itself(route, filename):
    page = page_of(STATIC / filename)
    links = [target for tag, target in page.refs if tag == "a" and target in ("/interview", "/me", "/demo", "/attack")]

    assert links[:4] == ["/interview", "/me", "/demo", "/attack"]
    # 自分のページだけに aria-current="page" がある(入口は、ブランドのリンク)
    assert page.current_pages == [route]


# ----------------------------------------------------------------------
# 画面に要る口(web.ui_api): セッション・面談の注記・求人・デモのケース・リプレイ
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_session_info_shows_the_cookies_principal_and_never_issues_one(web_app):
    # クッキーは HttpOnly で JS から読めないが、本人の経路の URL には依頼者 ID が要る。ID の発行は、開始ページの GET(/start)だけ。
    browser = web_app.browser()

    anonymous = await browser.get("/v1/session")
    assert anonymous.json() == {"principal_id": None, "registered": False}
    assert "set-cookie" not in anonymous.headers and anonymous.headers["cache-control"] == "no-store"

    pid = await browser.open_start_page()
    assert (await browser.get("/v1/session")).json() == {"principal_id": pid, "registered": False}  # 面談は、まだ送信していない
    await submit_interview(web_app.services, pid)  # 面談の送信(内部の関数。公開面には、直接の送信の口はない。台帳 X-81)
    assert (await browser.get("/v1/session")).json() == {"principal_id": pid, "registered": True}


@pytest.mark.anyio
async def test_the_session_info_ignores_a_cookie_with_a_wrong_signature(web_app):
    forged = web_app.browser()
    forged.set_cookie("0123456789abcdef.forged-signature")

    response = await forged.get("/v1/session")

    assert response.json() == {"principal_id": None, "registered": False}
    assert "set-cookie" not in response.headers


@pytest.mark.anyio
async def test_the_interview_notice_is_the_one_begin_returns_and_reading_it_starts_no_interview(web_app):
    # 入口のページは begin を呼ばない(begin は、面談の状態をサーバーのメモリに作る。上限があり、通りがかりの訪問者で埋まってしまう)。
    browser = web_app.browser()
    pid = await browser.open_start_page()

    notice = (await browser.get("/v1/interview/notice")).json()

    assert web_app.services.interview.store.get(pid) is None
    assert [item["id"] for item in notice["items"]] == ["vertex_ai", "global_endpoint", "server_memory", "auto_delete"]
    begun = (await browser.post(f"/v1/principals/{pid}/interview/begin")).json()
    assert begun["notice"] == notice
    assert "{days}" not in json.dumps(notice, ensure_ascii=False)  # 日数は、差し込み済み


@pytest.mark.anyio
async def test_the_job_list_is_public_information_from_the_fixtures_without_the_attack_job(web_app):
    response = await web_app.browser().get("/v1/jobs")

    jobs = response.json()["jobs"]
    assert [job["template_id"] for job in jobs] == ["case1-employer", "case2-employer"]  # 攻撃用の求人(case3)は載せない
    for job in jobs:
        assert set(job) == {"job_id", "template_id", "company_name", "title", "summary", "confidential", "job_category"}
    for secret in ("raw_conditions", "max_salary", "min_salary", "contact", "example.com", "job_summary"):
        assert secret not in response.text, secret


def test_a_confidential_job_hides_its_company_name(tmp_path):
    # §6.1・FR-32: confidential な求人は、段 1 まで企業名を伏せる(求人名・要約は公開情報)。
    source = (FIXTURES_DIRECTORY / "case1.toml").read_text(encoding="utf-8")
    (tmp_path / "case1.toml").write_text(source.replace("confidential = false", "confidential = true"), encoding="utf-8")

    (job,) = list_jobs(tmp_path)

    assert (job["confidential"], job["company_name"]) == (True, None)
    assert "サンプルシステムズ" not in json.dumps(job, ensure_ascii=False)
    assert job["title"] and job["summary"]
    assert list_jobs(tmp_path, exclude_template_ids={job["template_id"]}) == []


@pytest.mark.anyio
async def test_the_demo_cases_come_from_the_fixtures_and_show_nothing_private(web_app):
    response = await web_app.browser().get("/v1/demo/cases")

    cases = response.json()["cases"]
    assert [case["case"] for case in cases] == [1, 2, 3]
    assert [case["attack"] for case in cases] == [False, False, True]
    for case in cases:
        assert set(case) == {
            "case", "title", "description", "attack", "candidate_template_id", "employer_template_id",
            "company_name", "job_title", "job_summary", "replay_available",
        }  # fmt: skip
        fixture = load_case_fixture(case["case"])
        assert case["candidate_template_id"] == fixture.candidate.template_id
        assert case["employer_template_id"] == fixture.employer.template_id
        assert (case["company_name"], case["job_title"]) == (fixture.employer.company_name, fixture.employer.public_job.title)
        assert case["replay_available"] is True
        # 候補者の職務要約・連絡先・生の条件は、段階開示や並べて見る画面の中身。ケースの一覧には出さない
        for private in (fixture.candidate.job_summary, fixture.candidate.contact.name, fixture.candidate.contact.email):
            assert private not in response.text
    assert "raw_conditions" not in response.text
    assert list_cases(FIXTURES_DIRECTORY) == cases


@pytest.mark.anyio
async def test_the_template_ids_the_screens_send_are_accepted_by_the_creation_apis(web_app):
    # デモの画面は、/v1/demo/cases の ID で POST /v1/demo/negotiations を、交渉の画面は、/v1/jobs の ID で交渉を作る。
    seed_templates(web_app.store._db)  # 金庫が起動時に行う、フィクスチャの投入(§3.7)
    browser = web_app.browser()
    for case in (await browser.get("/v1/demo/cases")).json()["cases"]:
        created = await browser.post(
            "/v1/demo/negotiations",
            {
                "request_id": f"ui-test-case-{case['case']}-0001",
                "candidate_template_id": case["candidate_template_id"],
                "employer_template_id": case["employer_template_id"],
            },
        )
        assert created.status_code == 200, (case["case"], created.text)
        assert re.fullmatch(r"[0-9a-f]{16}", created.json()["nid"])

    owner = web_app.browser()
    pid = await owner.register()
    (job, *_) = (await owner.get("/v1/jobs")).json()["jobs"]
    nid = await owner.create_negotiation(pid, job["template_id"])
    listed = (await owner.get(f"/v1/principals/{pid}/negotiations")).json()
    assert [(item["nid"], item["job_id"]) for item in listed] == [(nid, job["job_id"])]  # 一覧の job_id は、求人の一覧の job_id で引ける


@pytest.mark.anyio
@pytest.mark.parametrize("case", [1, 2, 3])
async def test_a_replay_is_the_recorded_file_as_it_is(web_app, case):
    response = await web_app.browser().get(f"/v1/demo/replays/{case}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert response.content == (FIXTURES_DIRECTORY / "replays" / f"case{case}.jsonl").read_bytes()
    header = json.loads(response.text.splitlines()[0])
    assert (header["header"], header["schema"], header["case"]) == (True, "replay/v1", case)
    # 画面は、最初の行(ヘッダ)を除いた各行を {side, seq, observed_at, event} として読む
    events = [json.loads(line) for line in response.text.splitlines()[1:]]
    assert events and all({"side", "seq", "observed_at", "event"} == set(item) for item in events)


@pytest.mark.anyio
@pytest.mark.parametrize("case", ["0", "4", "10", "01", "-1", "abc", "1.jsonl", "%2e%2e"])
async def test_only_the_replays_of_cases_1_to_3_exist(web_app, case):
    response = await web_app.browser().get(f"/v1/demo/replays/{case}")

    assert response.status_code == 404
    assert response.json() == {"detail": "not_found"}


@pytest.mark.anyio
async def test_the_demo_routes_of_the_screens_do_not_use_or_extend_the_principal_session(web_app):
    # /v1/demo/ の下(ケース・リプレイ)は、有効なクッキーがあっても、利用記録を更新しない(本物の依頼者に触れない。§6.3)。
    browser = web_app.browser()
    pid = await browser.register()
    meta = web_app.default_db.collection("principals_meta").document(pid)
    before = meta.get().to_dict()
    web_app.clock.advance(dt.timedelta(hours=2))

    responses = [await browser.get("/v1/demo/cases"), await browser.get("/v1/demo/replays/1")]

    assert [response.status_code for response in responses] == [200, 200]
    assert all("set-cookie" not in response.headers for response in responses)
    assert meta.get().to_dict() == before


# ----------------------------------------------------------------------
# SSE(活動ログ)の流れ: 時計と読み出しを偽物にして、周期・位置・終わり方を確かめる
# ----------------------------------------------------------------------


class FakeClock:
    """sleep せずに進む時計。sleep の呼び出しを記録する。"""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


UNIT_STREAM = StreamConfig(poll_interval_seconds=2.0, max_duration_seconds=10.0, retry_milliseconds=2000)


async def run_stream(results: list, *, after_seq: int = 0) -> tuple[list[ServerSentEvent], list[int], FakeClock]:
    """read が順に返す(例外なら投げる)結果で、ストリームを最後まで動かす。(イベント、read が受けた位置、時計)。台本を使い切ったら、空の読み出し。"""
    clock, positions, script = FakeClock(), [], list(results)

    async def read(position: int) -> ActivityLog:
        positions.append(position)
        item = script.pop(0) if script else _log([], position)
        if isinstance(item, Exception):
            raise item
        return item

    events = [
        event
        async for event in activity_event_stream(read, after_seq, config=UNIT_STREAM, sleep=clock.sleep, monotonic=clock.monotonic)
    ]
    return events, positions, clock


@pytest.mark.anyio
async def test_the_stream_sends_only_new_entries_and_reads_on_from_the_last_position():
    events, positions, clock = await run_stream([_log([], 0), _log([_entry(1), _entry(2)], 2), _log([], 2), _log([_entry(3)], 3)])

    assert [(event.event, event.id, event.retry) for event in events] == [("activity", "2", 2000), ("activity", "3", 2000)]
    assert [[entry["seq"] for entry in json.loads(event.data)["entries"]] for event in events] == [[1, 2], [3]]
    assert json.loads(events[0].data)["next_after_seq"] == 2 and json.loads(events[0].data)["side"] == "candidate"
    assert positions == [0, 0, 2, 2, 3, 3]  # 新しい記録があったときだけ、読む位置が進む
    assert clock.sleeps == [2.0] * 5  # 周期は 2 秒


@pytest.mark.anyio
async def test_the_stream_closes_by_itself_after_the_maximum_duration_when_nothing_happens():
    events, positions, clock = await run_stream([], after_seq=5)

    assert events == []  # 何も送らずに閉じる(画面の EventSource が、つなぎ直す)
    assert set(positions) == {5}
    assert clock.now == 10.0


@pytest.mark.anyio
async def test_the_stream_ends_with_an_end_event_after_the_final_result():
    result = {"likelihood": "none", "package": None}

    events, positions, _ = await run_stream([_log([_entry(1), _entry(2, "final_result", "system", result=result)], 2)])

    assert [event.event for event in events] == ["activity", "end"]
    assert json.loads(events[0].data)["entries"][-1]["result"] == result
    assert json.loads(events[1].data) == {}
    assert positions == [0]  # 最終結果の後は、読まない


@pytest.mark.anyio
async def test_a_temporary_vault_failure_is_retried_and_any_other_failure_ends_the_stream_with_a_problem():
    events, positions, _ = await run_stream(
        [VaultUnavailableError("503"), _log([_entry(1)], 1), RuntimeError("secret detail"), _log([_entry(2)], 2)]
    )

    assert [event.event for event in events] == ["activity", "problem"]
    assert positions == [0, 0, 1]  # 一時的な失敗は、次の周期で読み直す。それ以外では、読むのをやめる
    assert "secret detail" not in events[1].data  # 失敗の中身は、画面に送らない
    assert json.loads(events[1].data) == {"detail": "stream_failed"}


@pytest.mark.anyio
async def test_the_stream_stops_before_reading_when_the_recheck_says_it_may_not_go_on():
    # 台帳 X-83: read(各 poll の前の確かめ直しを含む)が StreamNotAllowed を投げたら、次の記録を送らずに、その理由を problem で送って閉じる。
    # それより前に送った記録は、そのまま届いている。閉じた後は、読まない・待たない。
    events, positions, clock = await run_stream([_log([_entry(1)], 1), StreamNotAllowed("principal_deleting"), _log([_entry(2)], 2)])

    assert [event.event for event in events] == ["activity", "problem"]
    assert json.loads(events[1].data) == {"detail": "principal_deleting"}
    assert positions == [0, 1]  # 2 回目の読みで打ち切られ、3 回目はない
    assert clock.sleeps == [2.0]


@pytest.mark.parametrize(
    ("last_event_id", "after_seq", "expected"),
    [("7", 0, 7), ("7", 9, 9), ("0", 3, 3), ("abc", 4, 4), ("-1", 4, 4), ("٣", 4, 4), (str(2**31), 4, 4), ("", 4, 4)],
)
def test_the_resume_position_prefers_a_later_last_event_id_and_ignores_a_bad_one(last_event_id, after_seq, expected):
    from starlette.requests import Request

    request = Request({"type": "http", "headers": [(b"last-event-id", last_event_id.encode())]})

    assert ui_api.resume_position(request, after_seq) == expected


# ----------------------------------------------------------------------
# SSE(本人の活動ログ): /v1/stream/negotiations/{nid}/activity
# ----------------------------------------------------------------------


async def live_negotiation(web_app, **overrides):
    """本物の候補者(面談を送信済み)と、その交渉を作る。(ブラウザ、依頼者 ID、交渉 ID)。"""
    browser = web_app.browser()
    pid = await browser.register(**overrides)
    nid = await browser.create_negotiation(pid, web_app.put_employer_template())
    return browser, pid, nid


@pytest.mark.anyio
async def test_the_own_stream_sends_the_activity_log_and_then_what_is_added(fast_stream, web_app):
    browser, _, nid = await live_negotiation(web_app)
    store, package = web_app.store, sample_package()
    _move(store, nid, "candidate", "check", package)
    _move(store, nid, "candidate", "check", sample_package(salary=750))
    log = (await browser.get(f"/v1/negotiations/{nid}/activity")).json()

    async with Sse(web_app.app, f"/v1/stream/negotiations/{nid}/activity", cookie=browser.cookie) as sse:
        assert sse.status == 200
        assert sse.headers["content-type"].startswith("text/event-stream")
        assert sse.headers["cache-control"] == "no-store"
        assert "set-cookie" not in sse.headers
        (first,) = await sse.wait_events(1)
        assert (first["event"], first["id"], first["retry"]) == ("activity", str(log["next_after_seq"]), "100")
        assert json.loads(first["data"]) == log  # /activity と同じ中身(同じ形・同じ記録)
        _move(store, nid, "candidate", "check", sample_package(salary=800))  # つながっている間に増えた記録が、続けて届く
        _first, second = await sse.wait_events(2)
        assert [entry["seq"] for entry in json.loads(second["data"])["entries"]] == [3]
        assert second["id"] == "3"


@pytest.mark.anyio
async def test_the_own_stream_reads_on_from_after_seq_and_from_last_event_id(fast_stream, web_app):
    # EventSource は、切れると Last-Event-ID を付けてつなぎ直す(URL の after_seq は、最初のまま)。進んでいる方から読む。
    browser, _, nid = await live_negotiation(web_app)
    for salary in (700, 750, 800):
        _move(web_app.store, nid, "candidate", "check", sample_package(salary=salary))

    async def first_seqs(target: str, headers=None) -> list[int]:
        async with Sse(web_app.app, target, cookie=browser.cookie, headers=headers) as sse:
            (event,) = await sse.wait_events(1)
            return [entry["seq"] for entry in json.loads(event["data"])["entries"]]

    base = f"/v1/stream/negotiations/{nid}/activity"
    assert await first_seqs(base) == [1, 2, 3]
    assert await first_seqs(f"{base}?after_seq=1") == [2, 3]
    assert await first_seqs(base, {"Last-Event-ID": "2"}) == [3]
    assert await first_seqs(f"{base}?after_seq=1", {"Last-Event-ID": "2"}) == [3]
    assert await first_seqs(f"{base}?after_seq=2", {"Last-Event-ID": "1"}) == [3]


@pytest.mark.anyio
async def test_the_own_stream_ends_with_an_end_event_after_the_final_result(fast_stream, web_app):
    # 取消 → 最終結果(「なし」)。ストリームは、最終結果を送ったら end で閉じる(画面は、つなぎ直さない)。
    browser, _, nid = await live_negotiation(web_app)
    assert (await browser.post(f"/v1/negotiations/{nid}/control", {"action": "cancel"})).status_code == 200

    response = await browser.get(f"/v1/stream/negotiations/{nid}/activity")  # 自分で終わるので、httpx の ASGITransport でも読める

    assert response.status_code == 200
    assert "set-cookie" not in response.headers
    events = parse_sse(response.text)
    assert [event["event"] for event in events] == ["activity", "end"]
    entries = json.loads(events[0]["data"])["entries"]
    assert entries[-1]["action"] == "final_result" and entries[-1]["result"] == {"likelihood": "none", "package": None}
    for hidden in ("cancelled", "end_reason", "version", "needs_confirmation"):
        assert hidden not in response.text


@pytest.mark.anyio
async def test_an_open_stream_does_not_hold_the_principals_lock(fast_stream, web_app):
    # ミドルウェアは、依頼者ごとのロックを、応答を送り終えるまで持つ。SSE がこれを通ると、つながっている間、同じ依頼者の操作と、
    # レフェリーの金庫への操作(依頼者のロックを取る)が止まる。/v1/stream/ はセッションを見ない経路にして、持たせない。
    browser, pid, nid = await live_negotiation(web_app)

    async with Sse(web_app.app, f"/v1/stream/negotiations/{nid}/activity", cookie=browser.cookie) as sse:
        assert sse.status == 200
        paused = await asyncio.wait_for(browser.post(f"/v1/negotiations/{nid}/control", {"action": "pause"}), timeout=5)
        listed = await asyncio.wait_for(browser.get(f"/v1/principals/{pid}/negotiations"), timeout=5)
        assert paused.status_code == 200 and listed.json()[0]["state"] == "paused"
        (event,) = await sse.wait_events(1)
        assert [entry["action"] for entry in json.loads(event["data"])["entries"]] == ["pause"]  # 他の操作の結果が、ストリームに届く


@pytest.mark.anyio
async def test_the_own_stream_has_the_same_authorization_as_the_activity_route(web_app):
    # §6.3・DV-01: 本人の経路は、ミドルウェアを通らないので、同じ確認(クッキー・削除中でないこと・当事者であること)を、ストリームの前に行う。
    mine, others, stranger, forged = web_app.browser(), web_app.browser(), web_app.browser(), web_app.browser()
    pid, other_pid = await mine.register(), await others.register()
    template_id = web_app.put_employer_template()
    my_nid = await mine.create_negotiation(pid, template_id)
    other_nid = await others.create_negotiation(other_pid, template_id)
    demo_nid = create_demo_negotiation(web_app.store)
    forged.set_cookie("0123456789abcdef.forged-signature")

    async with Sse(web_app.app, f"/v1/stream/negotiations/{my_nid}/activity?after_seq=99", cookie=mine.cookie) as sse:
        assert sse.status == 200  # 本人は読める(何でも断っているのではない)
    for nid in (other_nid, "0123456789abcdef", demo_nid, "not-a-negotiation-id"):
        streamed = await mine.get(f"/v1/stream/negotiations/{nid}/activity")
        plain = await mine.get(f"/v1/negotiations/{nid}/activity")
        assert (streamed.status_code, streamed.json()) == (403, {"detail": "forbidden"}) == (plain.status_code, plain.json()), nid
    for visitor in (stranger, forged):  # クッキーがない・署名が合わない: 401 で、ID を発行しない
        response = await visitor.get(f"/v1/stream/negotiations/{my_nid}/activity")
        assert (response.status_code, response.json()) == (401, {"detail": "no_session"})
        assert "set-cookie" not in response.headers
    for bad in ("-1", str(2**31), "x"):  # 範囲外・数でない after_seq は、金庫に届かず 422
        assert (await mine.get(f"/v1/stream/negotiations/{my_nid}/activity?after_seq={bad}")).status_code == 422


@pytest.mark.anyio
async def test_the_own_stream_is_refused_while_the_principals_data_is_being_deleted(web_app):
    # 削除中の依頼者は、すべての操作を拒否する(§6.3)。ミドルウェアを通らない経路でも、同じく 409。
    browser, pid, nid = await live_negotiation(web_app)
    web_app.default_db.collection("principals_meta").document(pid).update({"deletion_state": "deleting"})

    streamed = await browser.get(f"/v1/stream/negotiations/{nid}/activity")
    plain = await browser.get(f"/v1/negotiations/{nid}/activity")

    assert (streamed.status_code, streamed.json()) == (409, {"detail": "principal_deleting"})
    assert (plain.status_code, plain.json()) == (409, {"detail": "principal_deleting"})


@pytest.mark.anyio
async def test_the_own_stream_closes_without_sending_the_next_record_when_the_deletion_starts_after_it_was_opened(
    fast_stream, web_app, monkeypatch
):
    # 台帳 X-83・§6.3: つながった後に本人の削除が始まる(deletion_state=deleting)と、次の poll の前の確かめ直しで気づき、金庫を読まずに、
    # 次の記録を送らずに閉じる(problem の detail は principal_deleting。画面は GET の再取得に切り替えて、409 で理由を表示する)。
    browser, pid, nid = await live_negotiation(web_app)
    store = web_app.store
    _move(store, nid, "candidate", "check", sample_package())
    order = []
    original_meta_get, original_get_events = web_app.services.meta.get, web_app.vault.get_events

    async def recording_meta_get(principal_id):
        order.append("meta")
        return await original_meta_get(principal_id)

    async def recording_get_events(*args, **kwargs):
        order.append("events")
        return await original_get_events(*args, **kwargs)

    monkeypatch.setattr(web_app.services.meta, "get", recording_meta_get)
    monkeypatch.setattr(web_app.vault, "get_events", recording_get_events)

    async with Sse(web_app.app, f"/v1/stream/negotiations/{nid}/activity", cookie=browser.cookie) as sse:
        (first,) = await sse.wait_events(1)
        assert [entry["seq"] for entry in json.loads(first["data"])["entries"]] == [1]  # 始めの記録は届く(何でも閉じているのではない)
        web_app.default_db.collection("principals_meta").document(pid).update({"deletion_state": "deleting"})  # 削除が始まった
        _move(store, nid, "candidate", "check", sample_package(salary=750))  # 削除の途中で増えた記録
        events = await sse.finished()

    assert [event["event"] for event in events] == ["activity", "problem"]
    assert json.loads(events[1]["data"]) == {"detail": "principal_deleting"}
    assert '"seq":2' not in sse.body.decode() and "750" not in sse.body.decode()  # 増えた記録は、送っていない
    assert order[0] == "meta"  # 始めの確認(authorize_own_stream)
    assert all(order[index - 1] == "meta" for index, call in enumerate(order) if call == "events")  # 金庫を読む前には、毎回、利用記録を読み直す
    assert order[-1] == "meta" and order.count("events") >= 1  # 最後は、確かめ直して閉じた(金庫は読んでいない)


@pytest.mark.anyio
async def test_the_own_stream_closes_when_the_usage_record_is_gone_and_when_it_cannot_be_read(fast_stream, web_app, monkeypatch):
    # 削除が終わって利用記録がない(principal_deleted)ときも、利用記録を読み直せないとき(Firestore の失敗)も、閉じる側に倒す
    # (読めないまま送り続けない)。後者は、読めない理由を画面に送らない(stream_failed)。
    gone_browser, gone_pid, gone_nid = await live_negotiation(web_app)
    unreadable_browser, _, unreadable_nid = await live_negotiation(web_app)
    original_meta_get = web_app.services.meta.get

    async def failing_meta_get(principal_id):
        raise RuntimeError("secret detail: principals_meta is down")

    async with Sse(web_app.app, f"/v1/stream/negotiations/{gone_nid}/activity", cookie=gone_browser.cookie) as sse:
        web_app.default_db.collection("principals_meta").document(gone_pid).delete()  # 削除が最後の段まで終わった
        gone = await sse.finished()
    assert [event["event"] for event in gone] == ["problem"]
    assert json.loads(gone[0]["data"]) == {"detail": "principal_deleted"}

    async with Sse(web_app.app, f"/v1/stream/negotiations/{unreadable_nid}/activity", cookie=unreadable_browser.cookie) as sse:
        monkeypatch.setattr(web_app.services.meta, "get", failing_meta_get)  # 最初の確認の後で、読めなくなる
        unreadable = await sse.finished()
    monkeypatch.setattr(web_app.services.meta, "get", original_meta_get)
    assert [event["event"] for event in unreadable] == ["problem"]
    assert json.loads(unreadable[0]["data"]) == {"detail": "stream_failed"}
    assert "secret detail" not in sse.body.decode()


@pytest.mark.anyio
async def test_the_own_stream_shows_only_the_principals_side(fast_stream, web_app):
    # 求人側だけが持つ評価(カナリア)は、ストリームにも出ない(§3.2・DV-10)。求人側は、どの組み合わせも「本人確認が必要」と評価する。
    browser = web_app.browser()
    pid = await browser.register()
    template_id = web_app.put_employer_template(rules=[EmployerRule(when={}, policy=needs_confirmation_policy("employer"))])
    nid = await browser.create_negotiation(pid, template_id)
    package = sample_package()
    _move(web_app.store, nid, "candidate", "check", package)
    _move(web_app.store, nid, "candidate", "propose", package)
    _move(web_app.store, nid, "employer", "reject")
    employer_side = web_app.store.get_events(nid, "employer")
    assert [(event.kind, event.own_evaluation) for event in employer_side] == [("offer_received", "needs_confirmation"), ("reject", None)]  # 対照

    async with Sse(web_app.app, f"/v1/stream/negotiations/{nid}/activity", cookie=browser.cookie) as sse:
        (event,) = await sse.wait_events(1)

    entries = json.loads(event["data"])["entries"]
    assert [(entry["actor"], entry["action"]) for entry in entries] == [("self", "check"), ("self", "propose"), ("counterparty", "reject")]
    for canary in ("offer_received", "offer_rejected", Verdict.NEEDS_CONFIRMATION.value):
        assert canary not in event["data"], canary


# ----------------------------------------------------------------------
# SSE(デモ・攻撃の活動ログ): /v1/stream/demo/negotiations/{nid}/activity?side=
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_demo_stream_sends_either_sides_log_without_a_session(fast_stream, web_app):
    nid = create_demo_negotiation(web_app.store)
    await web_app.services.sweeper.sweep_once()  # 架空人物の交渉にも、段の状態を作る(§6.2)
    _move(web_app.store, nid, "candidate", "propose", sample_package())
    visitor = web_app.browser()  # クッキーのない訪問者

    shown = {}
    for side in ("candidate", "employer"):
        log = (await visitor.get(f"/v1/demo/negotiations/{nid}/activity", side=side)).json()
        async with Sse(web_app.app, f"/v1/stream/demo/negotiations/{nid}/activity?side={side}") as sse:
            assert sse.status == 200 and sse.headers["content-type"].startswith("text/event-stream")
            assert "set-cookie" not in sse.headers
            (event,) = await sse.wait_events(1)
        assert json.loads(event["data"]) == log  # /v1/demo/negotiations/{nid}/activity と同じ中身
        shown[side] = [(entry["actor"], entry["action"], entry["own_evaluation"]) for entry in log["entries"]]

    # どちらのパネルも、その側自身の見え方(相手の評価は入らない。DV-10)
    assert shown == {"candidate": [("self", "propose", None)], "employer": [("counterparty", "propose", "acceptable")]}


@pytest.mark.anyio
async def test_the_demo_stream_refuses_what_the_demo_activity_route_refuses(fast_stream, web_app):
    # 本物の利用者の交渉・存在しない交渉・段の状態がまだない交渉は、どれも 403(台帳 X-38)。side は必須で、列挙の値だけ。
    owner = web_app.browser()
    pid = await owner.register()
    live_nid = await owner.create_negotiation(pid, web_app.put_employer_template())
    unswept_nid = create_demo_negotiation(web_app.store)  # 段の状態を作る前
    visitor = web_app.browser()

    for nid in (live_nid, "0123456789abcdef", "not-a-negotiation-id", unswept_nid):
        streamed = await visitor.get(f"/v1/stream/demo/negotiations/{nid}/activity", side="candidate")
        plain = await visitor.get(f"/v1/demo/negotiations/{nid}/activity", side="candidate")
        assert (streamed.status_code, streamed.json()) == (403, {"detail": "forbidden"}) == (plain.status_code, plain.json()), nid
    assert (await visitor.get(f"/v1/stream/demo/negotiations/{unswept_nid}/activity")).status_code == 422  # side は必須
    assert (await visitor.get(f"/v1/stream/demo/negotiations/{unswept_nid}/activity", side="somebody")).status_code == 422
    # 本物の利用者の交渉は、ログインしていても、デモの経路からは読めない(自分の交渉でも)
    assert (await owner.get(f"/v1/stream/demo/negotiations/{live_nid}/activity", side="candidate")).status_code == 403


@pytest.mark.anyio
async def test_the_demo_stream_does_not_use_or_extend_the_principal_session(fast_stream, web_app):
    nid = create_demo_negotiation(web_app.store)
    await web_app.services.sweeper.sweep_once()
    _move(web_app.store, nid, "candidate", "check", sample_package())
    browser = web_app.browser()
    pid = await browser.register()
    meta = web_app.default_db.collection("principals_meta").document(pid)
    before = meta.get().to_dict()
    web_app.clock.advance(dt.timedelta(hours=2))  # 1 時間を過ぎているので、セッションを見る経路なら利用記録を更新する

    async with Sse(web_app.app, f"/v1/stream/demo/negotiations/{nid}/activity?side=candidate", cookie=browser.cookie) as sse:
        (event,) = await sse.wait_events(1)

    assert "set-cookie" not in sse.headers and meta.get().to_dict() == before
    assert [entry["action"] for entry in json.loads(event["data"])["entries"]] == ["check"]


# ----------------------------------------------------------------------
# SSE の同時本数の上限(台帳 C-65): 全体・クライアント IP ごと。超えたら 429、接続が終われば、どの終わり方でも席が戻る
# ----------------------------------------------------------------------


def refused_body(scope: str, limit: int) -> dict:
    """SSE の同時本数の上限で断るときの 429 の本文(入口は sse。時間窓がないので window_seconds は null。Retry-After は 2 秒)。"""
    return {
        "detail": {
            "code": "rate_limited",
            "entrance": "sse",
            "scope": scope,
            "limit": limit,
            "window_seconds": None,
            "retry_after_seconds": 2,
        }
    }


def demo_stream_path(nid: str, side: str = "candidate") -> str:
    return f"/v1/stream/demo/negotiations/{nid}/activity?side={side}"


async def demo_negotiation_to_watch(web_app) -> str:
    """デモの交渉(段の状態まで作ってある。見回りが作る。§6.2)。"""
    nid = create_demo_negotiation(web_app.store)
    await web_app.services.sweeper.sweep_once()
    return nid


@pytest.mark.anyio
async def test_the_own_stream_refuses_a_third_connection_from_one_client_and_a_closed_one_gives_its_place_back(fast_stream, web_app):
    # 既定(設定ファイル)は、クライアントごと 2 本。上限ちょうどまで通り、3 本目は 429(画面の EventSource はつなぎ直さず、GET の再取得に切り替える)。
    browser, _, nid = await live_negotiation(web_app)
    path, limiter = f"/v1/stream/negotiations/{nid}/activity", web_app.services.stream_limiter

    async with Sse(web_app.app, path, cookie=browser.cookie) as first:
        async with Sse(web_app.app, path, cookie=browser.cookie) as second:
            assert (first.status, second.status) == (200, 200)
            assert (len(limiter), limiter.open_for("127.0.0.1")) == (2, 2)

            refused = await browser.get(path)

            assert refused.status_code == 429
            assert refused.headers["Retry-After"] == "2" and refused.json() == refused_body("client", 2)
            assert len(limiter) == 2  # 断った分は、数えていない
            assert "set-cookie" not in refused.headers
            # SSE をあきらめた画面が使う、通常の GET の再取得は、この上限の影響を受けない
            assert (await browser.get(f"/v1/negotiations/{nid}/activity")).status_code == 200
        assert len(limiter) == 1  # 1 本閉じると、1 本ぶん空く
        async with Sse(web_app.app, path, cookie=browser.cookie) as third:
            assert third.status == 200
            assert len(limiter) == 2
    assert len(limiter) == 0


@pytest.mark.anyio
async def test_the_demo_stream_has_the_same_limits_and_the_two_panels_of_one_negotiation_fit_in_the_default(fast_stream, web_app):
    # デモ・攻撃の画面は、1 交渉につき側ごとに 2 本つなぐ(static/ui.js の sides)。既定の 2 本で、ちょうど 1 交渉ぶん。
    nid = await demo_negotiation_to_watch(web_app)
    visitor, limiter = web_app.browser(), web_app.services.stream_limiter

    async with Sse(web_app.app, demo_stream_path(nid, "candidate")) as candidate:
        async with Sse(web_app.app, demo_stream_path(nid, "employer")) as employer:
            assert (candidate.status, employer.status) == (200, 200)
            refused = await visitor.get(f"/v1/stream/demo/negotiations/{nid}/activity", side="candidate")
            assert refused.status_code == 429 and refused.json() == refused_body("client", 2)
            assert refused.headers["Retry-After"] == "2"
            assert (await visitor.get(f"/v1/demo/negotiations/{nid}/panels")).status_code == 200  # 再取得の口は使える
    assert len(limiter) == 0


@pytest.mark.anyio
async def test_each_client_is_counted_on_its_own_and_the_overall_limit_comes_after_the_clients_one(fast_stream, web_app):
    nid = await demo_negotiation_to_watch(web_app)
    web_app.services.stream_limiter = SseConnectionLimiter(SseLimitConfig(max_connections=3, max_connections_per_client=2))
    limiter, visitor = web_app.services.stream_limiter, web_app.browser()

    def client(ip: str) -> dict[str, str]:
        return {"X-Forwarded-For": f"10.0.0.1, {ip}"}  # クライアントは末尾(web.client_ip)。先頭側は、利用者が書ける値

    async with contextlib.AsyncExitStack() as stack:
        held = [
            await stack.enter_async_context(Sse(web_app.app, demo_stream_path(nid), headers=client(ip)))
            for ip in ("198.51.100.1", "198.51.100.1", "198.51.100.2")  # 別のクライアントは、別に数える
        ]
        assert [sse.status for sse in held] == [200, 200, 200] and len(limiter) == 3

        over_all = await visitor.client.get(demo_stream_path(nid), headers=client("198.51.100.3"))  # このクライアントの枠は空いている
        over_client = await visitor.client.get(demo_stream_path(nid), headers=client("198.51.100.1"))  # 全体も埋まっているが、具体的な方を理由にする

        assert (over_all.status_code, over_all.json()) == (429, refused_body("overall", 3))
        assert (over_client.status_code, over_client.json()) == (429, refused_body("client", 2))
        assert len(limiter) == 3
    assert len(limiter) == 0


@pytest.mark.anyio
async def test_the_default_limits_are_twenty_in_all_and_two_per_client(web_app):
    # 設定ファイルの値(sse_max_connections = 20・sse_max_connections_per_client = 2)が、本番の組み立てのルートに効いている。20 本を同時につなぐ
    # (周期は既定の 2 秒のまま。fast_stream だと、20 本が金庫と Firestore を 50 回/秒ずつ読んでしまう)。
    nid = await demo_negotiation_to_watch(web_app)
    limiter, visitor = web_app.services.stream_limiter, web_app.browser()

    async def refused(ip: str):
        return await visitor.client.get(demo_stream_path(nid), headers={"X-Forwarded-For": ip})

    async with contextlib.AsyncExitStack() as stack:
        for index in range(10):  # 10 のクライアントが 2 本ずつ
            for _ in range(2):
                headers = {"X-Forwarded-For": f"198.51.100.{index + 1}"}
                sse = await stack.enter_async_context(Sse(web_app.app, demo_stream_path(nid), headers=headers))
                assert sse.status == 200
        assert len(limiter) == 20

        assert (await refused("203.0.113.1")).json() == refused_body("overall", 20)  # 21 本目(11 番目のクライアント)は、全体の上限
        assert (await refused("198.51.100.1")).json() == refused_body("client", 2)  # 同じクライアントの 3 本目は、クライアントの上限
    assert len(limiter) == 0


@pytest.mark.anyio
async def test_a_connection_over_the_limit_is_refused_before_anything_is_read(fast_stream, web_app, monkeypatch):
    # 上限を超えた要求は、権限の確認(Firestore と金庫を読む)の前に断る。上限の外の要求が、バックエンドを読ませないため。
    nid = await demo_negotiation_to_watch(web_app)
    visitor, other_nid = web_app.browser(), "0123456789abcdef"
    checked: list[str] = []
    original = web_app.services.stages.is_fictional_negotiation

    async def recording(negotiation_id):
        checked.append(negotiation_id)
        return await original(negotiation_id)

    monkeypatch.setattr(web_app.services.stages, "is_fictional_negotiation", recording)

    async with Sse(web_app.app, demo_stream_path(nid)) as first:
        async with Sse(web_app.app, demo_stream_path(nid)):  # クライアントの枠(2 本)を使い切る
            assert first.status == 200
            over = await visitor.get(f"/v1/stream/demo/negotiations/{other_nid}/activity", side="candidate")
            assert over.status_code == 429 and other_nid not in checked  # 確認の読み出しに、届いていない
        free = await visitor.get(f"/v1/stream/demo/negotiations/{other_nid}/activity", side="candidate")  # 1 本空けば、確認まで進む
        assert (free.status_code, free.json()) == (403, {"detail": "forbidden"}) and other_nid in checked


@pytest.mark.anyio
async def test_a_stream_that_fails_the_authorization_does_not_keep_its_place(fast_stream, web_app):
    # 確認の失敗(401・403・409)は、席を取ったまま終わらない: 上限(2 本)より多く繰り返しても、429 にならず、席は 0 に戻る。
    mine, stranger, deleting = web_app.browser(), web_app.browser(), web_app.browser()
    pid = await mine.register()
    my_nid = await mine.create_negotiation(pid, web_app.put_employer_template())
    deleting_pid = await deleting.register()
    deleting_nid = await deleting.create_negotiation(deleting_pid, web_app.put_employer_template())
    web_app.default_db.collection("principals_meta").document(deleting_pid).update({"deletion_state": "deleting"})
    limiter = web_app.services.stream_limiter

    for _ in range(5):
        statuses = [
            (await stranger.get(f"/v1/stream/negotiations/{my_nid}/activity")).status_code,  # クッキーがない
            (await mine.get("/v1/stream/negotiations/0123456789abcdef/activity")).status_code,  # 存在しない交渉
            (await mine.get(f"/v1/stream/demo/negotiations/{my_nid}/activity", side="candidate")).status_code,  # 本物の交渉は、デモの口から読めない
            (await deleting.get(f"/v1/stream/negotiations/{deleting_nid}/activity")).status_code,  # 削除中
        ]
        assert statuses == [401, 403, 403, 409]
        assert len(limiter) == 0


@pytest.mark.anyio
async def test_every_way_a_stream_can_end_gives_its_place_back(fast_stream, web_app, monkeypatch):
    # 自分で閉じる(最終結果・problem)・クライアントが切る・応答が例外で落ちる、のどれでも、席は戻る(上限を超えて繰り返しても 429 にならない)。
    browser, _, nid = await live_negotiation(web_app)
    path, limiter = f"/v1/stream/negotiations/{nid}/activity", web_app.services.stream_limiter
    _move(web_app.store, nid, "candidate", "check", sample_package())  # 送る記録がある

    # クライアントが切る(async with を抜けるときに、切断を app に知らせる)
    for _ in range(3):
        async with Sse(web_app.app, path, cookie=browser.cookie) as sse:
            await sse.wait_events(1)
        assert len(limiter) == 0

    # problem で閉じる(利用記録を読めない)。続けて 3 回(上限は 2 本)
    original_meta_get = web_app.services.meta.get

    async def failing_meta_get(principal_id):
        raise RuntimeError("principals_meta is down")

    for _ in range(3):
        async with Sse(web_app.app, path, cookie=browser.cookie) as sse:
            monkeypatch.setattr(web_app.services.meta, "get", failing_meta_get)  # 最初の確認の後で、読めなくなる
            events = await sse.finished()
        monkeypatch.setattr(web_app.services.meta, "get", original_meta_get)
        assert events[-1]["event"] == "problem" and len(limiter) == 0

    # 応答が例外で落ちる(本物のサーバで、書き込めなくなったとき): 3 回続けても、席は戻る
    class BrokenSse(Sse):
        async def _send(self, message) -> None:
            await super()._send(message)
            if message["type"] == "http.response.body":
                raise ConnectionResetError("the client went away")

    for _ in range(3):
        with pytest.raises(ConnectionResetError):
            async with BrokenSse(web_app.app, path, cookie=browser.cookie) as sse:
                await sse.finished()
        assert len(limiter) == 0

    # 最終結果で閉じる(end)。自分で終わるので、httpx の ASGITransport でも読める
    assert (await browser.post(f"/v1/negotiations/{nid}/control", {"action": "cancel"})).status_code == 200
    for _ in range(3):
        response = await browser.get(path)
        assert response.status_code == 200 and parse_sse(response.text)[-1]["event"] == "end"
        assert len(limiter) == 0


@pytest.mark.anyio
async def test_the_stream_limit_is_counted_in_memory_and_leaves_the_rate_limit_counters_alone(fast_stream, web_app):
    # 同時本数は、時間窓の回数ではない: メモリの中だけで数え、(default) の rate_limits にも、全体の枠(300)にも触れない。
    nid = await demo_negotiation_to_watch(web_app)
    visitor = web_app.browser()

    async with Sse(web_app.app, demo_stream_path(nid)), Sse(web_app.app, demo_stream_path(nid)):
        assert (await visitor.get(f"/v1/stream/demo/negotiations/{nid}/activity", side="candidate")).status_code == 429

    counters = [path for path in dump_documents(web_app.default_db) if path.startswith("rate_limits/")]
    assert counters == []


# ----------------------------------------------------------------------
# 画面の確認手順(AC-19)
# ----------------------------------------------------------------------

CHECKLIST = ROOT / "tests" / "manual" / "ui_checklist.md"


def test_the_manual_checklist_covers_every_page_and_every_part_of_the_second_package():
    source = CHECKLIST.read_text(encoding="utf-8")
    sections = re.findall(r"^## (.+)$", source, flags=re.MULTILINE)

    for heading in (
        "準備", "共通の確認", "入口 `/`", "面談 `/interview`", "自分の交渉 `/me`", "デモ `/demo`", "攻撃の実演 `/attack`",
        "既知の限界", "自動で確かめていること",
    ):  # fmt: skip
        assert any(section.startswith(heading) for section in sections), heading
    # 確認の行に、「あとで足す」の印を残さない。段階開示・開示台帳・FR-39 の 2 パネル・メーター・シミュレーションの区画が、どれも書いてある
    assert "L2 で足す" not in source and "準備中" not in source
    for slot in ("#slot-stages", "#slot-ledger", "#slot-fr39", "#slot-meter", "#slot-simulation"):
        assert slot in source, slot
    # 確認の表は、すべて「操作」と「合格」の 2 列
    headers = [line for line in source.splitlines() if line.startswith("| 操作")]
    assert len(headers) == 6 and set(headers) == {"| 操作 | 合格 |"}
    # AC-19(活動ログ・開示台帳の閲覧、2 つのパネルの並列表示、一時停止・取消)と、後半の要点を、画面で確かめる行がある
    for required in (
        "活動ログ", "並んで", "一時停止", "取消", "開示台帳", "段階開示", "ここで連絡先が開示されます", "架空の求人(自動応答)",
        "金庫の答えをすべて見られたとしても、ここまで", "シミュレーション(防御なしの場合の計算。金庫は使っていません)", "台本の攻撃者で実演",
        "最悪漏れてもここまで", "まだ隠しているもの", "scripts/serve_local.py",
    ):  # fmt: skip
        assert required in source, required


# ----------------------------------------------------------------------
# 活動ログの購読(static/ui.js の watchNegotiation): node があれば、偽の EventSource で動かして確かめる
# ----------------------------------------------------------------------

NODE = shutil.which("node")

WATCHER_SCRIPT = r"""
import assert from "node:assert/strict";
import { ApiError } from "__API__";
import { watchNegotiation } from "__UI__";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

class FakeEventSource {
  static CLOSED = 2;
  static instances = [];
  constructor(url) { this.url = url; this.readyState = 1; this.listeners = {}; this.closed = false; FakeEventSource.instances.push(this); }
  addEventListener(name, fn) { (this.listeners[name] ??= []).push(fn); }
  emit(name, data) { (this.listeners[name] ?? []).forEach((fn) => fn({ data: JSON.stringify(data) })); }
  close() { this.closed = true; this.readyState = 2; }
}
globalThis.EventSource = FakeEventSource;

const entry = (seq, action = "check") => ({ seq, actor: "self", action, package: null, own_evaluation: null, answer: null, reason: null,
  attempted_move: null, result: action === "final_result" ? { likelihood: "none", package: null } : null });
const log = (side, entries) => ({ side, entries, next_after_seq: entries.length ? entries[entries.length - 1].seq : 0 });

{ // SSE: 側ごとに 1 本。重複は除き、全部の側が最終結果を受け取ったときだけ終わる。終わった側は、つなぎ直さないよう閉じる
  const got = []; let ended = 0;
  const watcher = watchNegotiation({ sides: ["candidate", "employer"], streamRoute: "GET /v1/stream/demo/negotiations/{nid}/activity",
    path: { nid: "abc" }, poll: async () => { throw new Error("must not poll"); },
    onEntries: (side, entries) => got.push([side, entries.map((e) => e.seq)]), onEnd: () => { ended += 1; } });
  const [candidate, employer] = FakeEventSource.instances;
  assert.equal(candidate.url, "/v1/stream/demo/negotiations/abc/activity?side=candidate&after_seq=0");
  assert.equal(employer.url, "/v1/stream/demo/negotiations/abc/activity?side=employer&after_seq=0");
  candidate.emit("activity", log("candidate", [entry(1), entry(2)]));
  candidate.emit("activity", log("candidate", [entry(2), entry(3)]));
  employer.emit("activity", log("employer", [entry(1)]));
  assert.deepEqual(got, [["candidate", [1, 2]], ["candidate", [3]], ["employer", [1]]]);
  candidate.emit("activity", log("candidate", [entry(4, "final_result")]));
  assert.deepEqual([candidate.closed, employer.closed, ended], [true, false, 0]);
  employer.emit("activity", log("employer", [entry(2, "final_result")]));
  assert.deepEqual([employer.closed, ended], [true, 1]);
  watcher.close();
  console.log("ok sse");
}

{ // problem: 再取得に切り替える。1 回の再取得で両側を読み、位置は側ごとに続きから
  FakeEventSource.instances.length = 0;
  const positions = []; let ended = 0; let round = 0;
  const watcher = watchNegotiation({ sides: ["candidate", "employer"], streamRoute: "x", path: {}, pollInterval: 5,
    poll: async (p) => { positions.push({ ...p }); round += 1;
      return round === 1 ? { candidate: log("candidate", [entry(3), entry(4, "final_result")]), employer: log("employer", [entry(2)]) }
                         : { candidate: log("candidate", []), employer: log("employer", [entry(3, "final_result")]) }; },
    onEntries: () => {}, onEnd: () => { ended += 1; } });
  const [candidate] = FakeEventSource.instances;
  candidate.emit("activity", log("candidate", [entry(1), entry(2)]));
  candidate.emit("problem", {});
  await sleep(80);
  assert.deepEqual(positions, [{ candidate: 2, employer: 0 }, { candidate: 4, employer: 2 }]);
  assert.equal(ended, 1);
  assert.ok(FakeEventSource.instances.every((source) => source.closed));
  watcher.close();
  console.log("ok problem");
}

{ // EventSource がない: 最初から再取得。一時的な失敗(503)は続け、確定した拒否(403)は止めて onError に渡す
  globalThis.EventSource = undefined;
  let calls = 0; const errors = []; const got = [];
  watchNegotiation({ sides: ["candidate"], streamRoute: "x", path: {}, pollInterval: 5,
    poll: async () => { calls += 1;
      if (calls <= 2) throw new ApiError(503, "temporarily_unavailable", null);
      if (calls === 3) return { candidate: log("candidate", [entry(1)]) };
      throw new ApiError(403, "forbidden", null); },
    onEntries: (side, entries) => got.push(entries.length), onError: (error) => errors.push(error.status) });
  await sleep(120);
  assert.deepEqual([got, errors, calls], [[1], [403], 4]);
  await sleep(40);
  assert.equal(calls, 4);
  console.log("ok polling");
}
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_activity_watcher_of_the_screens_streams_falls_back_to_polling_and_stops_as_designed():
    # api.js・ui.js を、そのまま node で読み込む(DOM は触らない部分だけ)。SSE → problem → 両側を 1 回で返す再取得 → 終わり方。
    script = WATCHER_SCRIPT.replace("__API__", (STATIC / "api.js").as_uri()).replace("__UI__", (STATIC / "ui.js").as_uri())

    result = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60, check=False)

    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.split() == ["ok", "sse", "ok", "problem", "ok", "polling"]


# ----------------------------------------------------------------------
# 区画の描画と振る舞い(L2): node があれば、偽の DOM で動かして確かめる。サンプルのデータは、実際の API のモデルから作る
# ----------------------------------------------------------------------


def screen_fixtures() -> dict:
    """描画のテストに渡す、サンプルのデータ(StageView・LedgerEntry・build_panels・build_meter・simulate_bisection が返す形)。"""
    package = sample_package(salary=700, remote_days=2, night_duty=2, review_months=6)
    result = NegotiationResult(likelihood="high", package=package)
    summary = "業務システムの開発と運用に約 7 年従事。顧客との調整を担当した(確認用)。"
    both, only_employer, neither = (
        SideFlagsView(candidate=True, employer=True),
        SideFlagsView(candidate=False, employer=True),
        SideFlagsView(candidate=False, employer=False),
    )

    def stage_view(visible=("likelihood", "package"), **changes) -> dict:
        values = dict(
            nid="0123456789abcdef",
            judged=True,
            agreed=True,
            stage=0,
            result=result,
            meet=only_employer,
            approve=neither,
            employer_fictional=True,
            employer_auto_response=True,
            company=CompanyView(confidential=False, name="株式会社サンプルシステムズ(架空)"),
        )
        disclosure = dict(visible=list(visible), likelihood="high", package=package)
        disclosure.update(changes.pop("disclosure", {}))
        values.update(changes)
        return StageView(**values, disclosed_to_employer=EmployerDisclosure(**disclosure)).model_dump(mode="json")

    with_summary = ("likelihood", "package", "job_summary")
    with_contact = (*with_summary, "name", "email")
    views = {
        "running": stage_view(visible=(), judged=False, agreed=False, result=None, meet=neither, disclosure=dict(likelihood=None, package=None)),
        "none": stage_view(
            visible=("likelihood",),
            agreed=False,
            result=NegotiationResult(likelihood="none", package=None),
            meet=neither,
            disclosure=dict(likelihood="none", package=None),
        ),
        "open": stage_view(),
        "confidential": stage_view(company=CompanyView(confidential=True, name=None)),
        "no_auto_response": stage_view(employer_auto_response=False, meet=neither),
        "stage1": stage_view(
            visible=with_summary, stage=1, meet=both, approve=only_employer, disclosure=dict(job_summary=summary)
        ),
        "stage2_simulated": stage_view(
            visible=with_contact, stage=2, meet=both, approve=both, disclosure=dict(job_summary=summary, simulated=True)
        ),
        "stage2_demo": stage_view(
            visible=with_contact,
            stage=2,
            meet=both,
            approve=both,
            disclosure=dict(job_summary=summary, name="架空 花子", email="hanako.kako@example.com"),
        ),
    }

    def at(minute: int) -> dt.datetime:
        return dt.datetime(2026, 10, 4, 3, minute, tzinfo=dt.timezone.utc)

    def row(action, stage, operator, minute, **fields) -> dict:
        return LedgerEntry(nid="n1", action=action, stage=stage, operator=operator, at=at(minute), **fields).model_dump(mode="json")

    ledger = [
        row("disclose", 0, "system", 1, items=["likelihood", "package"], to="both"),
        row("meet", 0, "fictional_employer", 1),
        row("meet", 0, "principal", 2),
        row("disclose", 1, "principal", 2, items=["job_summary"], to="employer"),
        row("approve", 1, "fictional_employer", 2),
        row("approve", 1, "principal", 3),
        row("disclose", 2, "principal", 3, items=["name", "email"], to="employer", simulated=True),
    ]
    answers = [
        ActivityEntry(seq=3, actor="self", action="principal_answer", package=package, own_evaluation="acceptable", answer="accept").model_dump(mode="json")
    ]

    def anchor(**values) -> Anchor:
        base = dict(salary=650, remote_days=2, night_duty=8, review_months=12, training="*", side_job="*", start="*")
        return Anchor(**{**base, **values})

    policy = Policy(
        side="candidate",
        accept_anchors=[anchor()],
        reject_anchors=[anchor(salary=400, remote_days=0, night_duty=0)],
    )  # 当直の軸を外した本人(受ける・受けないの当直の値は、軸について中立)
    panels = build_panels(PolicyView(policy=policy, removed_axes=["night_duty"])).model_dump(mode="json")

    def offer(seq: int, salary: int, verdict: str) -> EventViewItem:
        return EventViewItem(seq=seq, kind="offer_received", package=probe_package(salary), own_evaluation=verdict)

    probes = [offer(1, 900, "acceptable"), offer(2, 550, "not_acceptable"), offer(3, 700, "acceptable"), offer(4, 600, "not_acceptable"), offer(5, 650, "acceptable")]
    steps = meter_api.simulate_bisection(620)
    grid = AXES["salary"].grid
    return {
        "views": views,
        "summary": summary,
        "ledger": ledger,
        "answers": answers,
        "panels": panels,
        "meter_one": meter_api.build_meter([probes]).model_dump(mode="json"),
        "meter_wide": meter_api.build_meter([probes[:1]]).model_dump(mode="json"),
        "meter_empty": meter_api.build_meter([]).model_dump(mode="json"),
        "simulation": meter_api.SimulationResponse(
            simulation=True,
            value=620,
            candidates={"low": meter_api.SIMULATION_LOW, "high": meter_api.SIMULATION_HIGH, "step": meter_api.SIMULATION_STEP},
            steps=steps,
            count=len(steps),
            found=steps[-1].low,
            note=meter_api.SIMULATION_NOTE,
        ).model_dump(mode="json"),
        # 区間 (lower, upper] と、そのマスの数(Interval.cells)のすべての組(画面の帯の描き方が、API の数え方と合うこと)
        "intervals": [
            [lower, upper, Interval(lower, upper).cells]
            for lower in (None, *grid)
            for upper in (None, *grid)
            if lower is None or upper is None or lower < upper
        ],
    }


SCREENS_SCRIPT = r"""
import assert from "node:assert/strict";
import { ApiError } from "__API__";
import { mountStages } from "__STAGES__";
import { groupRecords, mountLedger } from "__LEDGER__";
import { cellText, mountPanels } from "__PANELS__";
import { cellRange, describeInterval, mountMeter, mountSimulation } from "__METER__";

const DATA = __DATA__;
const NID = DATA.views.open.nid; // 段階開示のサンプルの交渉 ID(サーバーが返す StageView の nid)
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ---- 偽の DOM(画面の部品 h() が使う分だけ) ----
class FakeNode {}
class FakeText extends FakeNode {
  constructor(text) { super(); this.data = String(text); }
  get textContent() { return this.data; }
}
class FakeElement extends FakeNode {
  constructor(tag) {
    super();
    Object.assign(this, { tag, children: [], attributes: {}, className: "", listeners: {}, dataset: {}, value: "", disabled: false, hidden: false });
  }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren() { this.children = []; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(type, listener) { (this.listeners[type] ??= []).push(listener); }
  after() {}
  emit(type) { for (const listener of this.listeners[type] ?? []) listener({ currentTarget: this }); }
  click() { this.emit("click"); }
  get textContent() { return this.children.map((child) => child.textContent).join(""); }
  set textContent(value) { this.children = [new FakeText(value)]; }
}
const documentListeners = {};
globalThis.Node = FakeNode;
globalThis.CustomEvent = class { constructor(type, init = {}) { this.type = type; this.detail = init.detail; } };
globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (value) => new FakeText(value),
  addEventListener: (type, listener) => (documentListeners[type] ??= []).push(listener),
  dispatchEvent: (event) => (documentListeners[event.type] ?? []).forEach((listener) => listener(event)),
};
const confirms = [];
let confirmAnswer = true;
globalThis.window = { confirm: (message) => { confirms.push(message); return confirmAnswer; } };

const fire = (type, detail) => document.dispatchEvent(new CustomEvent(type, { detail }));
const resetListeners = () => Object.keys(documentListeners).forEach((type) => delete documentListeners[type]);
const walk = (node, visit) => { visit(node); (node.children ?? []).forEach((child) => walk(child, visit)); };
const find = (root, predicate) => { const found = []; walk(root, (node) => { if (predicate(node)) found.push(node); }); return found; };
const byTag = (root, tag) => find(root, (node) => node.tag === tag);
const byClass = (root, name) => find(root, (node) => (node.className || "").split(/\s+/).includes(name));
const buttonOf = (root, label) => find(root, (node) => node.tag === "button" && node.textContent === label)[0];
const text = (root) => root.textContent;
const mounted = () => ({ body: new FakeElement("div"), error: new FakeElement("div") });
const includesAll = (shown, phrases) => phrases.forEach((phrase) => assert.ok(shown.includes(phrase), `missing: ${phrase}`));

{ // 段階開示(本人): 段 0 → 「会う」(職務要約。送ったらフォームが消える)→ 「承認」(確認のダイアログ。段 2 は模擬表示)
  resetListeners();
  const { body, error } = mounted();
  const calls = [];
  let view = DATA.views.open;
  mountStages({ body, error }, {
    mode: "own",
    emptyText: "左の一覧から、交渉を選んでください。",
    loadStage: async (nid) => { calls.push(["load", nid]); return view; },
    meet: async (nid, summary) => { calls.push(["meet", nid, summary]); view = DATA.views.stage1; return view; },
    approve: async (nid) => { calls.push(["approve", nid]); view = DATA.views.stage2_simulated; return view; },
    onChange: () => calls.push(["changed"]),
  });
  assert.ok(text(body).includes("左の一覧から、交渉を選んでください。"));
  fire("negotiation:selected", { nid: NID });
  await wait(30);
  includesAll(text(body), ["株式会社サンプルシステムズ(架空)", "架空の求人(自動応答)", "段 0: 見込みと組み合わせ", "段 1: 匿名職務要約(会う)",
    "段 2: 氏名と連絡先(承認)", "氏名・勤務先・連絡先など、個人が特定できることは書かない", "段 1 が開いてから、承認できます。"]);
  const [summaryField] = byTag(body, "textarea");
  assert.equal(summaryField.getAttribute("maxlength"), "400");
  const meet = buttonOf(body, "会う");
  assert.equal(meet.disabled, true); // 空のままでは押せない
  summaryField.value = "  業務システムの開発(確認用)  ";
  summaryField.emit("input");
  assert.equal(meet.disabled, false);
  meet.click();
  await wait(30);
  assert.deepEqual(calls.filter((call) => call[0] === "meet"), [["meet", NID, "業務システムの開発(確認用)"]]); // 前後の空白を除いて送る
  assert.equal(byTag(body, "textarea").length, 0); // 送ったら、フォームは消える
  includesAll(text(body), ["求人側に見えている職務要約", DATA.summary]);
  confirmAnswer = false;
  buttonOf(body, "承認する").click();
  await wait(30);
  assert.equal(calls.filter((call) => call[0] === "approve").length, 0); // 確認で断れば、押さない
  assert.ok(confirms.length === 1 && confirms[0].includes("模擬表示"));
  confirmAnswer = true;
  buttonOf(body, "承認する").click();
  await wait(30);
  assert.deepEqual(calls.filter((call) => call[0] === "approve"), [["approve", NID]]);
  includesAll(text(body), ["ここで連絡先が開示されます", "模擬表示です"]);
  assert.ok(!text(body).includes("hanako"));
  assert.ok(calls.filter((call) => call[0] === "changed").length >= 3); // 読み込む・会う・承認するたびに、台帳へ知らせる
  console.log("ok stages-own");
}

{ // 段階開示(デモ): 架空の候補者・求人は自動。押す口はなく、フィクスチャの架空の連絡先が出る
  resetListeners();
  const { body, error } = mounted();
  mountStages({ body, error }, { mode: "demo", emptyText: "ライブで実行してください", loadStage: async () => DATA.views.stage2_demo });
  fire("negotiation:selected", { nid: "n1" });
  await wait(30);
  includesAll(text(body), ["架空の候補者", "氏名: 架空 花子", "hanako.kako@example.com", "フィクスチャの架空のもの"]);
  assert.equal(byTag(body, "button").length + byTag(body, "textarea").length, 0);
  // 段 0 の状態で、「会う」を誰が押すか: 求人側にも自動応答がある(ケース 1・2)・求人側に自動応答がない(ケース 3。求人側は押されない)
  for (const [view, phrase, other] of [
    [DATA.views.open, "架空の候補者・架空の求人が、フィクスチャの設定で、自動で押します", "求人側には自動応答がないので、押されません"],
    [DATA.views.no_auto_response, "求人側には自動応答がないので、押されません", "架空の候補者・架空の求人が、フィクスチャの設定で、自動で押します"],
  ]) {
    resetListeners();
    const demo = mounted();
    mountStages(demo, { mode: "demo", emptyText: "-", loadStage: async () => view });
    fire("negotiation:selected", { nid: "n1" });
    await wait(30);
    assert.ok(text(demo.body).includes(phrase) && !text(demo.body).includes(other), phrase);
    assert.equal(byTag(demo.body, "button").length + byTag(demo.body, "textarea").length, 0);
  }
  console.log("ok stages-demo");
}

{ // 段階開示の状態ごとの表示・失敗・古い読み込みの捨て方・交渉が終わったときの読み直し
  const cases = [
    ["running", ["まだ終わっていません"], 0],
    ["none", ["合意できる組み合わせがなかったので", "段 1 以降には進めません"], 0],
    ["confidential", ["非公開求人(会うと決めた後に企業名を開示)"], 1],
    ["no_auto_response", ["自動応答がなく"], 1],
  ];
  for (const [name, phrases, lists] of cases) {
    resetListeners();
    const { body, error } = mounted();
    mountStages({ body, error }, { mode: "own", emptyText: "-", loadStage: async () => DATA.views[name] });
    fire("negotiation:selected", { nid: "n1" });
    await wait(30);
    includesAll(text(body), phrases);
    assert.equal(byClass(body, "stage-list").length, lists, name);
  }
  resetListeners();
  let { body, error } = mounted();
  mountStages({ body, error }, { mode: "own", emptyText: "-", loadStage: async () => { throw new ApiError(403, "forbidden", null); } });
  fire("negotiation:selected", { nid: "n1" });
  await wait(30);
  assert.ok(text(error).includes("許可されていません") && text(body) === "");

  resetListeners();
  ({ body, error } = mounted());
  let release;
  const slow = new Promise((resolve) => { release = resolve; });
  const loads = [];
  mountStages({ body, error }, { mode: "own", emptyText: "空です", loadStage: async (nid) => { loads.push(nid); await slow; return DATA.views.open; } });
  fire("negotiation:selected", { nid: "n1" });
  fire("negotiation:cleared");
  release();
  await wait(30);
  assert.equal(text(body), "空です"); // 消した後に届いた古い結果は、表示しない
  fire("negotiation:selected", { nid: "n2" });
  await wait(30);
  fire("negotiation:ended", { nid: "other" });
  fire("negotiation:ended", { nid: "n2" });
  await wait(30);
  assert.deepEqual(loads, ["n1", "n2", "n2"]); // 選んでいる交渉が終わったときだけ、読み直す
  console.log("ok stages-states");
}

{ // 開示台帳: 交渉ごと(始めた順)に、途中確認の回答(時刻なし)を台帳の行より先に。終わった交渉の回答は、読み直さない
  resetListeners();
  const { body, error } = mounted();
  const known = [
    { nid: "n2", title: "進行中の交渉", createdAt: "2026-10-04T04:00:00Z", ended: false },
    { nid: "n1", title: "インフラエンジニア", createdAt: "2026-10-04T03:00:00Z", ended: true },
    { nid: "n0", title: "記録のない交渉", createdAt: "2026-10-03T03:00:00Z", ended: true },
  ];
  const loaded = [];
  const ledger = mountLedger({ body, error }, {
    loadLedger: async () => DATA.ledger,
    loadAnswers: async (nid) => { loaded.push(nid); return nid === "n1" ? DATA.answers : []; },
    negotiations: () => known,
  });
  await wait(30);
  const shown = text(body);
  includesAll(shown, ["インフラエンジニア", "段 0 が開きました: 見込み・組み合わせを、双方に表示", "段 1 が開きました: 匿名職務要約を、求人側に表示",
    "段 2 が開きました: 氏名・連絡先(メール)を、求人側に表示(模擬表示。連絡先は集めていないので、実際には渡っていません)",
    "「会う」が押されました", "「承認」が押されました", "架空の求人の自動応答", "あなたの操作", "システム(自動)", "途中確認に答えました", "→ 受ける"]);
  assert.ok(shown.indexOf("途中確認に答えました") < shown.indexOf("段 0 が開きました")); // 回答は、その交渉の台帳の行より先
  assert.ok(!shown.includes("記録のない交渉") && !shown.includes("進行中の交渉")); // 記録のない交渉は出さない
  assert.ok(!shown.includes(DATA.summary)); // 職務要約の本文は、台帳に出ない
  await ledger.refresh();
  assert.equal(loaded.filter((nid) => nid === "n1").length, 1); // 終わった交渉の回答は、覚えている
  assert.equal(loaded.filter((nid) => nid === "n2").length, 2); // 終わっていない交渉は、読み直す
  assert.equal(groupRecords([], [{ ...DATA.ledger[0], nid: "zz" }], new Map())[0].title, "(一覧にない交渉)");
  fire("negotiation:cleared");
  assert.ok(text(body).includes("まだ記録がありません"));
  console.log("ok ledger");
}

{ // 並べて見る画面(FR-39): cells が 0 の軸は出さない。外した軸は右のパネルに。生の値はどこにもない
  resetListeners();
  let { body, error } = mounted();
  mountPanels({ body, error }, { load: async () => DATA.panels });
  await wait(30);
  includesAll(text(body), ["最悪漏れてもここまで", "まだ隠しているもの", "400〜450 万円", "600〜650 万円", "週 2 日", "当直の条件(外しています)",
    "正確な最低年収(400〜450 万円・600〜650 万円のマスの中のどこか)", "辞めた理由(面談時に破棄済み)", "軸どうしの組み合わせ"]);
  assert.deepEqual(byClass(body, "axis-name").map(text), ["年収", "リモート", "昇給見直し"]);
  assert.ok(!text(body).includes("620") && !text(body).includes("410"));
  assert.equal(cellText("salary", { low: 600, high: 650 }), "600〜650 万円");
  assert.equal(cellText("remote_days", { low: 2, high: 2 }), "週 2 日");
  assert.equal(cellText("training", { value: "available" }), "あり");
  resetListeners();
  ({ body, error } = mounted());
  mountPanels({ body, error }, { load: async () => { throw new ApiError(404, "not_found", null); } });
  await wait(30);
  assert.ok(text(error).includes("まだ、条件が保存されていません"));
  console.log("ok panels");
}

{ // メーターの帯: 区間 (lower, upper] の描き方が、API のマスの数(Interval.cells)と合う
  for (const [lower, upper, cells] of DATA.intervals) {
    const [first, last] = cellRange({ lower, upper });
    assert.equal(last - first + 1, cells, `${lower} ${upper}`);
    assert.ok(first >= 0 && last <= 25);
  }
  assert.equal(describeInterval({ lower: 600, upper: 650 }), "600 万円より上、650 万円以下");
  assert.equal(describeInterval({ lower: null, upper: 900 }), "900 万円以下");
  assert.equal(describeInterval({ lower: 1450, upper: null }), "1450 万円より上");
  console.log("ok meter-cells");
}

{ // 推定区間メーター: 計算を頼むのは、新しい提案が届いたとき(続けて届いたら 1 回)と、実演が終わったときだけ。失敗したら、押したときだけやり直す
  resetListeners();
  const { body, error } = mounted();
  const requests = [];
  let answer = { status: 200, data: DATA.meter_one };
  globalThis.fetch = async (url, init) => {
    requests.push({ url, method: init.method, body: init.body ?? null });
    return { ok: answer.status < 400, status: answer.status, headers: { get: () => "application/json" }, json: async () => answer.data };
  };
  mountMeter({ body, error });
  assert.ok(text(body).includes("まだ、金庫の答えがありません"));
  fire("attack:negotiations", { nids: ["a", "b"], refresh: false });
  await wait(450);
  assert.equal(requests.length, 0); // 交渉の一覧が変わっただけでは、頼まない
  for (let index = 0; index < 3; index += 1) fire("attack:offer");
  await wait(450);
  assert.equal(requests.length, 1); // 続けて届いた提案は、まとめて 1 回
  assert.deepEqual([requests[0].method, requests[0].url, JSON.parse(requests[0].body)], ["POST", "/v1/demo/meter", { negotiation_ids: ["a", "b"] }]);
  includesAll(text(body), ["金庫の答えをすべて見られたとしても、ここまで", "600 万円より上、650 万円以下", "これ以上は絞れません", "固定した条件"]);
  const cells = byClass(body, "meter-cell");
  const inRange = cells.filter((cell) => cell.className.split(/\s+/).includes("in-range"));
  assert.equal(cells.length, 26);
  assert.deepEqual([inRange.length, cells.indexOf(inRange[0])], [1, 7]); // 600 万円より上、650 万円以下の 1 マス
  assert.deepEqual(byClass(body, "meter-tick").map(text), ["300", "600", "900", "1200", "1500"]);
  fire("attack:negotiations", { nids: ["a", "b", "c"], refresh: true });
  await wait(450);
  assert.equal(requests.length, 2); // 二分探索の実演が終わったとき
  const many = Array.from({ length: 25 }, (_, index) => `n${index}`);
  fire("attack:negotiations", { nids: many, refresh: true });
  await wait(450);
  assert.deepEqual(JSON.parse(requests[2].body).negotiation_ids, many.slice(5)); // 21 件以上は、新しい方の 20 件
  answer = { status: 200, data: DATA.meter_wide };
  fire("attack:offer");
  await wait(450);
  assert.ok(text(body).includes("900 万円以下") && !text(body).includes("これ以上は絞れません")); // 1 つの提案だけなら、広い帯
  assert.equal(byClass(body, "meter-cell").filter((cell) => cell.className.includes("in-range")).length, 13);
  answer = { status: 429, data: { detail: { code: "rate_limited", entrance: "meter", scope: "client", limit: 60, window_seconds: 600, retry_after_seconds: 30 } } };
  fire("attack:offer");
  await wait(450);
  assert.ok(text(error).includes("推定区間メーターの計算の回数が"));
  const retry = buttonOf(error, "もう一度計算する");
  assert.ok(retry);
  const before = requests.length;
  await wait(450);
  assert.equal(requests.length, before); // 勝手には読み直さない
  answer = { status: 200, data: DATA.meter_empty };
  retry.click();
  await wait(60);
  assert.equal(requests.length, before + 1);
  assert.ok(text(error) === "" && text(body).includes("まだ、金庫の答えがありません"));
  console.log("ok meter");
}

{ // 防御なしのシミュレーション: 範囲外・10 万円刻みでない値は、通信せずに断る。表には 7 手
  const value = new FakeElement("input");
  const button = new FakeElement("button");
  const error = new FakeElement("div");
  const result = new FakeElement("div");
  const requests = [];
  globalThis.fetch = async (url, init) => {
    requests.push([url, init.method]);
    return { ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => DATA.simulation };
  };
  mountSimulation({ value, button, error, result });
  value.value = "620";
  button.click();
  await wait(40);
  assert.deepEqual(requests, [["/v1/demo/meter/simulation?value=620", "GET"]]);
  includesAll(text(result), ["シミュレーション(防御なしの場合の計算。金庫は使っていません)", "7 手で、620 万円と特定されます", "900 万円以上ですか?", "620(特定)"]);
  assert.equal(byTag(result, "tr").length, 1 + 7); // 見出しと 7 手
  for (const bad of ["625", "1600", "290", "", "abc"]) { value.value = bad; button.click(); await wait(10); }
  assert.equal(requests.length, 1);
  assert.ok(text(error).includes("10 万円刻み"));
  console.log("ok simulation");
}
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_sections_render_the_apis_data_and_behave_as_designed():
    # stages.js・ledger.js・panels.js・meter.js を、そのまま node で読み込み、偽の DOM に、実際の API のモデルから作ったデータを描く。
    script = (
        SCREENS_SCRIPT.replace("__API__", (STATIC / "api.js").as_uri())
        .replace("__STAGES__", (STATIC / "stages.js").as_uri())
        .replace("__LEDGER__", (STATIC / "ledger.js").as_uri())
        .replace("__PANELS__", (STATIC / "panels.js").as_uri())
        .replace("__METER__", (STATIC / "meter.js").as_uri())
        .replace("__DATA__", json.dumps(screen_fixtures(), ensure_ascii=False))
    )

    result = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=120, check=False)

    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.split() == [
        "ok", "stages-own", "ok", "stages-demo", "ok", "stages-states", "ok", "ledger", "ok", "panels", "ok", "meter-cells", "ok", "meter", "ok", "simulation",
    ]  # fmt: skip
