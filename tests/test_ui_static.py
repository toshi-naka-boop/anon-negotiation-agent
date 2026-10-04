"""画面(static/)と、画面に要る小さな口(src/web/ui_api.py)の確認(design.md §6.3・§7・§8.4・§10・§12.1 AC-02・AC-19。作業パッケージ L)。

画面は静的な HTML と素の JavaScript・CSS(ビルド工程なし)。ブラウザは動かさないので、ここでは次を確かめる。
- 配信: ページの経路(/・/interview・/me・/demo・/attack)が対応する HTML を返し、/static が静的ファイルを返す。どれも依頼者 ID を発行せず
  (発行は開始ページの GET /start だけ。§6.3)、/static はセッションを見ない。HTML が参照するファイルは、すべて実在する。
- 画面のコードの規則: ブラウザの保存領域に書かない(scripts/check_no_web_storage.sh。AC-02)、API の文字列を HTML として組み立てる道がない、
  外部の読み込み・インラインのスクリプトがない(CSP)、JS が呼ぶ API の経路は実在する("METHOD /path" の文字列を、実際の経路と照らし合わせる)、
  JS が参照する要素の id は HTML に実在する。
- 画面に要る口(web.ui_api): セッション・面談の注記・求人・デモのケース・リプレイ・SSE(活動ログ)。
- SSE は、ミドルウェアの依頼者ごとのロックを持たない(つながっている間、同じ依頼者の操作が止まらない)。
- 画面の活動ログの購読(static/ui.js の watchNegotiation)は、node があれば、偽の EventSource で動かして確かめる(なければ、そのテストは飛ばす)。
金庫は本物の vault の app を ASGI のままつなぎ、web の app へは Browser(クッキーを持つ httpx のクライアント)から入る。
"""

import asyncio
import datetime as dt
import functools
import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from negotiation_core import Verdict
from sse_starlette import ServerSentEvent
from vault.api_models import MoveRequest
from vault.fixtures import FIXTURES_DIRECTORY, load_case_fixture
from vault.models import EmployerRule
from vault.seed import seed_templates
from vault_helpers import needs_confirmation_policy, sample_package
from web import activity_api, ui_api
from web.activity_api import ActivityEntry, ActivityLog
from web.session import SESSION_COOKIE_NAME
from web.ui_api import StreamConfig, activity_event_stream, build_ui_router, list_cases, list_jobs
from web.vault_client import VaultUnavailableError
from web_app_helpers import interview_body
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
    assert (await browser.get("/openapi.json")).status_code == 200  # スキーマの生成が、足した口で壊れていない


# ----------------------------------------------------------------------
# 画面のコードの規則
# ----------------------------------------------------------------------


def test_the_expected_files_exist():
    assert {path.name for path in HTML_FILES} == set(PAGES.values())
    for name in ("app.css", "api.js", "ui.js", "index.js", "interview.js", "me.js", "demo.js", "attack.js"):
        assert (STATIC / name).is_file(), name


@pytest.mark.parametrize("path", HTML_FILES, ids=lambda path: path.name)
def test_every_file_an_html_page_refers_to_exists(path):
    page = page_of(path)

    assert page.refs  # 読み取りが壊れて、何も確かめずに通らない
    for tag, target in page.refs:
        if target.startswith("/static/"):
            assert (STATIC / target.removeprefix("/static/")).is_file(), f"{path.name}: {tag} {target}"
        elif tag == "a":
            assert target in PAGES, f"{path.name}: the link {target} is not a page"
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

    assert len(called) >= 35  # 読み取りが壊れて、何も確かめずに通らない
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
    ):
        assert expected in called, expected


def test_only_the_pages_for_a_real_principal_call_the_routes_that_need_the_session():
    # デモ・攻撃の画面は、依頼者のセッションを要る API(/v1/principals・/v1/negotiations)を呼ばない(本物の依頼者には触れない。§6.3)。
    for (method, route), files in routes_called_by_the_javascript().items():
        if route.startswith(("/v1/principals", "/v1/negotiations", "/v1/stream/negotiations")):
            assert set(files) <= {"interview.js", "me.js"}, (method, route, files)


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
    script = (STATIC / f"{page.stem}.js").read_text(encoding="utf-8")
    used = set(re.findall(r"""\bel\("([\w-]+)"\)""", script)) | set(re.findall(r"""getElementById\("([\w-]+)"\)""", script))

    assert used  # 読み取りが空振りしていない
    assert used <= set(page_of(page).ids), sorted(used - set(page_of(page).ids))


def test_the_placeholders_for_the_next_package_are_in_place():
    # L2(段階開示・開示台帳・FR-39 の 2 パネル・メーター)が差し込む区画の id と、準備中の印。
    expected = {
        "me.html": {"slot-stages", "slot-ledger", "slot-fr39"},
        "demo.html": {"slot-fr39", "slot-stages"},
        "attack.html": {"slot-meter", "slot-simulation"},
    }
    for name, slots in expected.items():
        text = (STATIC / name).read_text(encoding="utf-8")
        for slot in slots:
            assert f'<section class="slot" id="{slot}" data-status="pending">' in text, (name, slot)
    for path in HTML_FILES:  # どのページのナビゲーションにも、準備中の置き場がある
        text = path.read_text(encoding="utf-8")
        assert 'data-slot="nav-stages"' in text and 'data-slot="nav-meter"' in text, path.name


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
    assert (await browser.post(f"/v1/principals/{pid}/interview", interview_body())).status_code == 200
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
# 画面の確認手順(AC-19)
# ----------------------------------------------------------------------

CHECKLIST = ROOT / "tests" / "manual" / "ui_checklist.md"


def test_the_manual_checklist_covers_every_page_and_says_what_the_next_package_adds():
    text = CHECKLIST.read_text(encoding="utf-8")
    sections = re.findall(r"^## (.+)$", text, flags=re.MULTILINE)

    for heading in ("共通の確認", "入口 `/`", "面談 `/interview`", "自分の交渉 `/me`", "デモ `/demo`", "攻撃の実演 `/attack`", "L2 で足す確認"):
        assert any(section.startswith(heading) for section in sections), heading
    # 段階開示・開示台帳・FR-39 の 2 パネル・メーター・シミュレーションは、L2 で足す(いまは「準備中」の区画があることだけを確かめる)
    assert text.count("L2 で足す") >= 5
    for slot in ("#slot-stages", "#slot-ledger", "#slot-fr39", "#slot-meter", "#slot-simulation"):
        assert slot in text, slot
    # 確認の表は、すべて「操作」と「合格」の 2 列
    headers = [line for line in text.splitlines() if line.startswith("| 操作")]
    assert len(headers) == 6 and set(headers) == {"| 操作 | 合格 |"}
    # 活動ログ・2 つのパネルの並列表示・一時停止・取消(AC-19)を、画面で確かめる行がある
    for required in ("活動ログ", "並んで", "一時停止", "取消"):
        assert required in text, required


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
