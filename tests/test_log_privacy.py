"""台帳 X-40: ログとタスク名に、依頼者 ID・交渉 ID・request_id を残さない(design.md §3.8)。

§3.8 は「ログに残すのは、エンドポイント・side・手の種類・回数・判定結果だけ」と定める。ログは本人の削除の後も残り、
ID が入っていると、交渉の時刻・失敗の理由・側を、ID から継続してたどれてしまう。次を確かめる。

- src/ のすべてのログの呼び出し(静的な検査): ID を渡していない・書式の中に ID の名前がない。タスクの名前にも入れない。
- 主要な経路のログ(動かして、caplog で確かめる): レフェリーのエージェント失敗・タスクの異常終了、見回りの失敗、
  削除の流れの失敗、セッションの更新の失敗。確かめるログが実際に出ていること(空だから通るのではないこと)も確かめる。
- uvicorn のアクセスログ(URL に ID が入る): 本番の起動口(create_app_from_env)を通すと ID が伏せられる。本物の
  uvicorn のサーバで確かめる(対照として、起動口を通さなければ ID が出る)。
"""

import ast
import asyncio
import logging
import re
from pathlib import Path

import httpx
import pytest
import uvicorn
from uvicorn.logging import AccessFormatter

import web.app as web_app_module
from vault.api_models import ControlRequest
from vault_helpers import accept_all_policy, sample_package
from web.app import create_app, create_app_from_env, mask_ids_in_logs
from web.referee import NegotiationContext, RefereeManager
from web.session import SESSION_KEY_ENV
from web.vault_client import VaultClientError
from web_app_helpers import DeletionProbe
from web_helpers import create_demo_negotiation, create_live_negotiation, drive, move_dict

_SRC = Path(__file__).resolve().parents[1] / "src"
_SESSION_KEY = "oRXjuLrpBpmCPe_O9zLtE1ZhEXY7BAssKXRaJDPp1fg"

# ID を持つ変数・項目の名前(ログの引数にも、タスクの名前にも使わない)。
_ID_NAMES = frozenset({"nid", "pid", "principal_id", "candidate_principal_id", "request_id", "negotiation_id"})
_LOG_METHODS = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical", "log"})
_LOGGER_NAMES = frozenset({"_log", "log", "logger", "logging"})
_TASK_STARTERS = frozenset({"create_task", "ensure_future"})
_ID_KEY_IN_FORMAT = re.compile(r"\b(nid|pid|principal_id|request_id)\s*=")  # 書式の中の「nid=%s」のような書き方


def _names_in(node: ast.AST) -> set[str]:
    """node の中の、変数名・属性名(f"{item.nid}" のような書式の中も含む)。"""
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
    return names


def _strings_in(node: ast.AST) -> list[str]:
    return [child.value for child in ast.walk(node) if isinstance(child, ast.Constant) and isinstance(child.value, str)]


def _logging_calls(tree: ast.AST):
    """ログの呼び出し(`_log.error(...)`・`logger.info(...)` など)。"""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _LOG_METHODS
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in _LOGGER_NAMES
        ):
            yield node


def _id_uses_in_logging_call(call: ast.Call) -> list[str]:
    """ログの呼び出しが、ID を渡している(変数・属性の名前)、または書式の中に ID の名前を書いている、その一覧。"""
    arguments = [*call.args, *(keyword.value for keyword in call.keywords)]
    names = sorted(set().union(*(_names_in(argument) for argument in arguments)) & _ID_NAMES)
    formats = [text for argument in arguments for text in _strings_in(argument) if _ID_KEY_IN_FORMAT.search(text)]
    return names + formats


def _task_names_with_an_id(tree: ast.AST):
    """name に ID を入れたタスクの作成(`asyncio.create_task(..., name=f"x-{nid}")` など)の、行番号。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) in _TASK_STARTERS:
            for keyword in node.keywords:
                if keyword.arg == "name" and _names_in(keyword.value) & _ID_NAMES:
                    yield node.lineno


def test_no_logging_call_in_src_takes_an_id_and_no_task_name_contains_one():
    # X-40(静的な検査): src/ のすべてのログの呼び出し(_log.error など)の引数と書式に、ID の変数・項目の名前がない。
    # create_task の name にも、ID を入れない(タスクの名前は、例外のときの記録に出る)。検査が空振りしていないことも確かめる。
    checked_logging_calls = 0
    offenders = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for call in _logging_calls(tree):
            checked_logging_calls += 1
            if uses := _id_uses_in_logging_call(call):
                offenders.append((path.relative_to(_SRC).as_posix(), call.lineno, uses))
        offenders += [(path.relative_to(_SRC).as_posix(), line, "task name") for line in _task_names_with_an_id(tree)]

    assert checked_logging_calls >= 10  # src/ に、ログの呼び出しが十分にあり、それを検査している
    assert offenders == []


def test_the_static_check_catches_a_logging_call_with_an_id():
    # 静的な検査の対照: ID を渡すログの呼び出しと、ID を入れたタスクの名前を、見つける(何も見つけない検査ではないこと)。
    tree = ast.parse(
        "_log.error('failed nid=%s side=%s', nid, side)\n"  # 1: 書式に nid=、引数に nid
        "_log.info('ended %s', item.nid)\n"  # 2: 属性 nid
        "logger.warning(f'principal {principal_id} deleted')\n"  # 3: f 文字列の中の principal_id
        "asyncio.create_task(run(), name=f'referee-{context.nid}')\n"  # 4: タスクの名前
        "_log.info('fine side=%s reason=%s', side, reason)\n"  # 5: ID を使わない
        "asyncio.create_task(run(), name='referee')\n"  # 6: ID を使わない
    )

    flagged = [call.lineno for call in _logging_calls(tree) if _id_uses_in_logging_call(call)]

    assert flagged == [1, 2, 3]
    assert list(_task_names_with_an_id(tree)) == [4]


# --- 主要な経路(動かして、caplog で確かめる) ---


@pytest.fixture
def web_logs(caplog):
    """web・agents の logger のログを DEBUG から集める。root は動かさない(httpx など外のライブラリのログは入れない)。"""
    caplog.set_level(logging.DEBUG, logger="web")
    caplog.set_level(logging.DEBUG, logger="agents")
    return caplog


def assert_ids_are_absent(caplog, *ids: str) -> None:
    for identifier in ids:
        assert identifier not in caplog.text, f"{identifier} is in the logs:\n{caplog.text}"


@pytest.mark.anyio
async def test_the_referee_logs_no_ids_when_the_agent_fails(store, web_env, web_logs):
    # X-40: エージェントが拒否した(ValueError)・約束にない例外を投げた、どちらの経路のログにも、交渉 ID・依頼者 ID がない。
    # 側・理由(列挙値)・例外の型名は残る。
    env = web_env
    nid, pid = create_live_negotiation(store)
    env.agents.script("candidate", ValueError("rejected"), RuntimeError("bug"), move_dict("end"))
    referee = env.referee(nid, mode="live", candidate_principal_id=pid)

    await referee.step()  # 拒否 → schema_invalid の登録
    await referee.step()  # 約束にない例外 → agent_timeout の登録

    assert "registering invalid move side=candidate reason=schema_invalid" in web_logs.text
    assert "registering invalid move side=candidate reason=agent_timeout" in web_logs.text
    assert "agent call failed side=candidate error=RuntimeError" in web_logs.text
    assert_ids_are_absent(web_logs, nid, pid)


@pytest.mark.anyio
async def test_a_referee_task_that_ends_with_an_error_is_logged_and_named_without_the_id(store, web_env, web_logs):
    # X-40: レフェリーのタスクが異常終了したときの記録(タスクの名前を含む)にも、交渉 ID がない。
    env = web_env
    nid = create_demo_negotiation(store)

    class BrokenVault:
        def __getattr__(self, name):
            async def fail(*args, **kwargs):
                raise RuntimeError("the vault client is broken")

            return fail

    await env.restart(vault=BrokenVault())
    manager = RefereeManager(env.deps)
    assert manager.start(NegotiationContext(nid=nid, mode="demo", candidate_principal_id=None))
    task = manager.task(nid)
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)  # 終了時のコールバックが動くのを待つ

    assert "referee task ended with an error error=RuntimeError" in web_logs.text
    assert nid not in task.get_name()
    assert_ids_are_absent(web_logs, nid)


@pytest.mark.anyio
async def test_the_sweeper_logs_no_ids_when_a_step_fails(store, web_env, web_logs):
    # X-40: 見回りの 1 件の処理が失敗したときのログに、交渉 ID がない(段階の名前と、例外の型名は残る)。
    env = web_env
    nid = create_demo_negotiation(store)
    real_vault = env.vault

    class FailingExpire:
        def __getattr__(self, name):
            return getattr(real_vault, name)

        async def expire(self, nid):
            raise VaultClientError("vault returned 500: " + nid, 500)  # 例外の文には ID が入っても、ログには書かない

    await env.restart(vault=FailingExpire())
    await real_vault.control(nid, ControlRequest(side="candidate", action="pause"))  # 一時停止中は、毎回 expire を呼ぶ

    report = await env.sweeper.sweep_once()

    assert report.errors == 1
    assert "sweep step failed step=_expire_if_due error=VaultClientError" in web_logs.text
    assert_ids_are_absent(web_logs, nid)


@pytest.mark.anyio
async def test_the_referee_logs_a_vault_error_by_status_only(store, web_env, web_logs):
    # X-40・L9-1: 金庫が 404・409・503 以外を返したとき、レフェリーはステータスだけを書く(金庫の detail には、ID が入り得る)。
    env = web_env
    nid = create_demo_negotiation(store)
    real_vault = env.vault

    class ServerErrorVault:
        def __getattr__(self, name):
            return getattr(real_vault, name)

        async def get_view(self, nid, side):
            raise VaultClientError(f"vault returned 500: {nid}", 500)

    await env.restart(vault=ServerErrorVault())

    await env.referee(nid).step()

    assert "vault call failed status=500" in web_logs.text
    assert_ids_are_absent(web_logs, nid)


@pytest.mark.anyio
async def test_the_deletion_flow_and_the_session_middleware_log_no_ids(web_app, web_logs, monkeypatch):
    # X-40: 削除の流れが途中で止まったとき・セッションの更新に失敗したときのログに、依頼者 ID も、クッキーもない。
    browser = web_app.browser()
    pid = await browser.register()
    probe = DeletionProbe(web_app)
    probe.fail_once_at("vault")

    response = await browser.post(f"/v1/principals/{pid}/delete")
    assert response.status_code == 202  # 途中で止まった。削除中の印が残る
    assert "principal deletion stopped step=vault error=RuntimeError" in web_logs.text

    async def broken_touch(principal_id):
        raise RuntimeError(f"cannot read the record of {principal_id}")

    monkeypatch.setattr(web_app.services.meta, "touch", broken_touch)
    other = web_app.browser()
    other_pid = await other.open_start_page()
    refused = await other.get(f"/v1/principals/{other_pid}/negotiations")
    assert refused.status_code == 503
    assert "session touch failed error=RuntimeError" in web_logs.text

    assert_ids_are_absent(web_logs, pid, other_pid, browser.cookie or "-", other.cookie or "-")


@pytest.mark.anyio
async def test_a_real_candidates_negotiation_leaves_no_ids_in_the_logs_of_the_production_configuration(
    store, web_env, caplog
):
    # X-40: 本番の設定(mask_ids_in_logs)で、ライブラリを含むすべてのログを DEBUG から集めても、交渉の最初から終わりまでに、
    # 交渉 ID も依頼者 ID も出ない(httpx が URL を INFO で書くログも、本番の設定では出ない)。
    mask_ids_in_logs()
    caplog.set_level(logging.DEBUG)
    env = web_env
    nid, pid = create_live_negotiation(store, candidate_policy=accept_all_policy("candidate"))
    env.agents.script("candidate", ValueError("rejected"), move_dict("propose", sample_package()))
    env.agents.script("employer", move_dict("accept"))
    referee = env.referee(nid, mode="live", candidate_principal_id=pid)

    await drive(referee)

    assert store.get_view(nid, "candidate").status == "judged"
    assert caplog.records  # ログは出ている(空だから通るのではない)
    assert_ids_are_absent(caplog, nid, pid)


@pytest.mark.anyio
async def test_httpx_would_log_the_url_with_the_id_unless_the_production_configuration_is_applied(
    store, web_env, caplog
):
    # 対照(X-40): 本番の設定(mask_ids_in_logs)がなければ、httpx は金庫への呼び出しの URL(交渉 ID つき)を INFO で書く。
    # 設定を適用すると、書かない(httpx の水準が WARNING になる)。
    caplog.set_level(logging.DEBUG)
    env = web_env
    nid = create_demo_negotiation(store)

    await env.vault.get_view(nid, "candidate")
    assert nid in caplog.text  # 設定がなければ、ID が出る

    caplog.clear()
    mask_ids_in_logs()
    await env.vault.get_view(nid, "candidate")
    assert nid not in caplog.text


# --- uvicorn のアクセスログ ---

_PID = "0123456789abcdef"
_NID = "fedcba9876543210"


def _access_record(path: str, status: int = 200) -> logging.LogRecord:
    """uvicorn が書くアクセスログの記録と同じ形(引数は、クライアント・メソッド・URL・HTTP の版・ステータス)。"""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:50000", "GET", path, "1.1", status),
        exc_info=None,
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (f"/v1/principals/{_PID}/policy", "/v1/principals/<id>/policy"),
        (f"/v1/negotiations/{_NID}/events?side=candidate&after_seq=3", "/v1/negotiations/<id>/events?side=candidate&after_seq=3"),
        (f"/v1/demo/negotiations/{_NID}/events?side=employer", "/v1/demo/negotiations/<id>/events?side=employer"),
        (f"/v1/principals/{_PID}/negotiations", "/v1/principals/<id>/negotiations"),
        ("/start", "/start"),  # ID のない URL は、そのまま
        ("/v1/negotiations/0123456789abcde/view", "/v1/negotiations/0123456789abcde/view"),  # 15 桁は ID ではない
        ("/v1/negotiations/0123456789abcdef0/view", "/v1/negotiations/0123456789abcdef0/view"),  # 17 桁も ID ではない
        ("/v1/negotiations/0123456789ABCDEF/view", "/v1/negotiations/0123456789ABCDEF/view"),  # 大文字は ID の形ではない
    ],
)
def test_the_access_log_filter_replaces_only_ids_and_keeps_the_record_shape(path, expected):
    # X-40: uvicorn のアクセスログの URL にある ID(16 桁の 16 進数)だけを <id> にする。エンドポイント・クエリ・
    # ステータスは残す。引数の並びを保つので、uvicorn の書式(AccessFormatter)でそのまま整形できる。
    mask_ids_in_logs()
    access_logger = logging.getLogger("uvicorn.access")
    record = _access_record(path)

    assert access_logger.filter(record)
    formatter = AccessFormatter(fmt="%(request_line)s -> %(status_code)s", use_colors=False)
    assert formatter.format(record) == f"GET {expected} HTTP/1.1 -> 200 OK"


def test_applying_the_log_configuration_twice_keeps_one_filter():
    mask_ids_in_logs()
    mask_ids_in_logs()
    access_logger = logging.getLogger("uvicorn.access")
    assert len([f for f in access_logger.filters if type(f).__name__ == "_MaskIdsFilter"]) == 1
    assert logging.getLogger("httpx").level == logging.WARNING


class _Collect(logging.Handler):
    """uvicorn のアクセスログの、整形済みの 1 行を集める。"""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


async def _serve_and_request(app, paths: list[str]) -> list[str]:
    """app を本物の uvicorn のサーバで動かし、paths を GET して、アクセスログの行を返す。"""
    handler = _Collect()
    handler.setFormatter(AccessFormatter(fmt="%(request_line)s -> %(status_code)s", use_colors=False))
    access_logger = logging.getLogger("uvicorn.access")
    previous_level, previous_propagate = access_logger.level, access_logger.propagate
    access_logger.addHandler(handler)
    access_logger.setLevel(logging.INFO)
    access_logger.propagate = False
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None, lifespan="off"))
    serving = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(30):
            while not server.started:
                await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            for path in paths:
                await client.get(path)
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 30)
        access_logger.removeHandler(handler)
        access_logger.setLevel(previous_level)
        access_logger.propagate = previous_propagate
    return handler.lines


def _production_app(default_db, monkeypatch):
    monkeypatch.setattr(web_app_module, "_create_default_db", lambda: default_db)
    return create_app_from_env(
        {SESSION_KEY_ENV: _SESSION_KEY, "VAULT_BASE_URL": "http://vault.test", "SERVICE_AUTH_ENABLED": "false"}
    )


@pytest.mark.anyio
async def test_a_real_uvicorn_server_started_through_the_production_entry_point_logs_no_ids(default_db, monkeypatch):
    # X-40: 本番の起動口(create_app_from_env)で作った app を、本物の uvicorn のサーバで動かすと、アクセスログの URL に、
    # 依頼者 ID・交渉 ID が出ない(エンドポイント・クエリ・ステータスは残る)。
    app = _production_app(default_db, monkeypatch)

    lines = await _serve_and_request(
        app,
        [f"/v1/principals/{_PID}/negotiations", f"/v1/negotiations/{_NID}/events?side=candidate", "/start"],
    )

    assert lines == [
        "GET /v1/principals/<id>/negotiations HTTP/1.1 -> 401 Unauthorized",
        "GET /v1/negotiations/<id>/events?side=candidate HTTP/1.1 -> 401 Unauthorized",
        "GET /start HTTP/1.1 -> 200 OK",
    ]
    assert all(_PID not in line and _NID not in line for line in lines)


@pytest.mark.anyio
async def test_a_real_uvicorn_server_logs_the_ids_unless_started_through_the_production_entry_point(default_db):
    # 対照(X-40): 起動口を通さずに(create_app で直接)作った app は、アクセスログの URL に ID をそのまま書く。
    # 上の確認が、何も書かないサーバで通っているのではないことを示す。
    app = create_app(vault=object(), default_db=default_db, session_key=_SESSION_KEY)

    lines = await _serve_and_request(app, [f"/v1/principals/{_PID}/negotiations"])

    assert lines == [f"GET /v1/principals/{_PID}/negotiations HTTP/1.1 -> 401 Unauthorized"]
