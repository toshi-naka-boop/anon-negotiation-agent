"""開発者が手元のブラウザで、画面(static/)を確かめるためのサーバ(開発用。本番では使わない。design.md §10・tests/manual/ui_checklist.md)。

    uv run python scripts/serve_local.py                 # 台本のエージェント・面談はスタブ(Gemini は呼ばない)。http://127.0.0.1:8080
    uv run python scripts/serve_local.py --port 8081
    uv run python scripts/serve_local.py --live          # 本物の Gemini(Vertex AI)。環境変数は scripts/run_demo.py の --live と同じ

1 つのプロセスの中で、Firestore エミュレータ(scripts/run_demo.py の firestore_emulator。JDK 21 で jar を直接起動する)・金庫の app(ASGI のまま)・
web の app を動かし、web を uvicorn で 127.0.0.1 に出す(外には出さない)。データは、起動のたびに、空のエミュレータに作る(止めると消える)。
金庫には、fixtures/case*.toml のテンプレートを入れる(本番の金庫が起動時に行うのと同じ。vault.seed)。サーバは、Ctrl-C で止まる。

既定(台本のモード。ネットワークに出ない)
- 交渉エージェント: 台本(tests/scripted_negotiators.py。scripts/run_demo.py の --scripted と同じ)。LLM は呼ばない。
  攻撃の交渉の攻撃者も台本で、攻撃の指示文は読まない(年収の二分探索をする)。二分探索の実演のボタンは、本番どおり動く(もともと台本)。
- 面談の LLM: スタブ。年収の 3 問は、答えにかかわらず「額面 620 万円・賞与込み・固定残業代なし」と読み取る。自由コメント・辞めた理由は、
  何も読み取らない(条件にならない)。
- 壁 1(生のメッセージ)の送り先: agents の app(同じプロセスの中。LLM はスタブ)。拒否は本物の検査で、有効な入力のときだけ、スタブの出力が返る。
`--live` は、上の 3 つを本物にする: agents の app も同じプロセスの中で動かし(ネットワークを通さない)、本物の ADK が Gemini(Vertex AI)を呼ぶ。
Firestore だけは、どちらのモードでも、エミュレータにつなぐ(demo- で始まるプロジェクト ID)。本物の GCP に接続するのは、--live で Gemini を呼ぶときだけ。

署名の鍵(SESSION_SIGNING_KEY に当たるもの)は、起動のたびに乱数で作る(開発用)。再起動すると、ブラウザのクッキーは使えなくなる(依頼者 ID も変わる)。
クッキーは Secure なので、Chrome(Chromium 系)なら http://127.0.0.1 でも受け付ける。Safari は、http では受け付けない。

終了コード: 止められたら 0、始められなかったら 1(--live の環境変数が足りない・エミュレータが起動しない など)、引数の誤りは 2。
"""

import argparse
import asyncio
import contextlib
import json
import logging
import os
import secrets
import sys
import uuid
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# src(プロジェクトのコード)と scripts/(run_demo)を足す。tests/ は、台本・スタブを読むときだけ足す(_load_test_helpers)。
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from google.cloud import firestore  # noqa: E402

import run_demo  # noqa: E402
from vault.app import create_app as create_vault_app  # noqa: E402
from vault.clock import SystemClock  # noqa: E402
from vault.config import DEFAULT_VAULT_CONFIG  # noqa: E402
from vault.firestore_client import VAULT_DATABASE  # noqa: E402
from vault.seed import seed_templates  # noqa: E402
from vault.store import VaultStore  # noqa: E402
from web.app import bind_agents_client, create_app  # noqa: E402
from web.attack import bind_raw_sender  # noqa: E402
from web.fictional_answerer import FixtureCatalog  # noqa: E402
from web.vault_client import VaultClient  # noqa: E402

HOST = "127.0.0.1"  # 外には出さない
DEFAULT_PORT = 8080
# agents の URL。A2A のクライアントが組み立てる URL の元になるだけで、通信はすべて同じプロセスの agents の app につながる(run_demo と同じ)。
AGENTS_BASE_URL = "http://agents.test"

# 面談のスタブの、年収の 3 問の読み取り(tests/test_interview_api.py の BASIS と同じ。確認の手順の例の 620 万円)。
STUB_SALARY_BASIS = {
    "amount_man_yen": 620,
    "amount_period": "annual",
    "amount_kind": "gross",
    "bonus_included": True,
    "bonus_months": 0,
    "fixed_overtime_man_yen_per_month": 0,
}


def _load_test_helpers() -> None:
    """台本のエージェント(scripted_negotiators)・スタブの LLM(agents_helpers)は tests/ にある。そこを import の道に足す(run_demo と同じ)。"""
    tests = str(PROJECT_ROOT / "tests")
    if tests not in sys.path:
        sys.path.insert(0, tests)


def interview_stub_behavior(request) -> str:
    """面談のスタブの LLM の返事。入力(JSON)の task で、年収の読み取りか、発言の構造化かを分ける(発言は、何も読み取らない)。"""
    task = json.loads(request.contents[-1].parts[0].text)["task"]
    if task == "salary_basis":
        return json.dumps(STUB_SALARY_BASIS)
    return json.dumps({"statements": []})


class ScriptedAgents:
    """レフェリーの SendTurn の形。候補者・求人は台本の ScriptedNegotiators(ケース 1 の始め方)、攻撃者は台本の攻撃者(年収の二分探索)。

    LLM は呼ばない(run_demo.scripted_sender と同じ台本)。攻撃者は、金庫の答えをすべて見られる最悪の場合の攻撃者として、交渉の候補者側の見え方を、
    デモ用の読み出し(架空の候補者の交渉だけ)から読む。
    """

    def __init__(self, vault: VaultClient) -> None:
        async def read_candidate_events(nid: str):
            return await vault.get_demo_events(nid, "candidate", 0)

        self._negotiators = run_demo.scripted_sender(1)
        self._attack = run_demo.scripted_sender(3, read_candidate_events)

    async def __call__(self, role, turn_input, *, nid, timeout_s):
        sender = self._attack if role == "attacker" else self._negotiators
        return await sender(role, turn_input, nid=nid, timeout_s=timeout_s)


@contextlib.contextmanager
def raw_messages_over_asgi(agents_app) -> Iterator[None]:
    """壁 1 の生のメッセージを、ネットワークを通さず、同じプロセスの agents の app に送る(web.attack.raw_message が HTTP クライアントを作る口を差し替える)。"""
    import web.attack.raw_message as raw_message

    transport = httpx.ASGITransport(app=agents_app)
    original = raw_message._open_http_client
    raw_message._open_http_client = lambda timeout_s: httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(timeout_s))
    try:
        yield
    finally:
        raw_message._open_http_client = original


def check_live_environment(environ: Mapping[str, str]) -> str | None:
    """--live に要る環境変数(run_demo と同じ)がそろっているか。そろっていなければ、止めるときに出す説明(そろっていれば None)。値は書かない。"""
    missing = [name for name, _ in run_demo._LIVE_ENVIRONMENT if not environ.get(name, "").strip()]
    if not missing:
        return None
    example = " ".join(f"{name}={value}" for name, value in run_demo._LIVE_ENVIRONMENT)
    return "\n".join(
        [
            f"--live を始められません。足りない環境変数: {'、'.join(missing)}。",
            "必要な環境変数は、ADK と google-genai の標準のものです(プロジェクト ID は、コードにも設定ファイルにも書きません)。",
            f"例: {example} uv run python scripts/serve_local.py --live",
            "何も呼ばずに止めました(Firestore エミュレータも起動していません)。",
        ]
    )


async def serve(*, live: bool, port: int, vault_db: firestore.Client, default_db: firestore.Client) -> None:
    """金庫・web(・live なら agents)を同じプロセスの中で組んで、web を uvicorn で出す。止められるまで戻らない。"""
    clock = SystemClock()
    store = VaultStore(db=vault_db, clock=clock, config=DEFAULT_VAULT_CONFIG)
    seeded = seed_templates(vault_db)
    print(f"金庫: フィクスチャのテンプレートを入れた(書いた {seeded.written} 件)", flush=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_vault_app(store)), base_url="http://vault") as vault_http:
        vault = VaultClient(vault_http)
        with contextlib.ExitStack() as stack:
            if live:
                agents_app = run_demo.build_agents_app()  # 本物の ADK。モデルへの接続は、最初の呼び出しのときに行われる
                stack.enter_context(run_demo.agents_over_asgi(agents_app))  # 交渉の手番を、同じプロセスの agents の app へ
                send_turn = bind_agents_client(AGENTS_BASE_URL)
            else:
                _load_test_helpers()
                from agents.app import create_app as create_agents_app
                from agents_helpers import StubLlm

                agents_app = create_agents_app(model=StubLlm())  # 壁 1 の送り先だけ。LLM はスタブ
                send_turn = ScriptedAgents(vault)
            stack.enter_context(raw_messages_over_asgi(agents_app))
            app = create_app(
                vault=vault,
                default_db=default_db,
                session_key=secrets.token_urlsafe(32),  # 開発用。起動のたびに乱数で作る(本番の鍵には使わない)
                clock=clock,
                send_turn=send_turn,
                send_raw=bind_raw_sender(AGENTS_BASE_URL),
                fixtures=FixtureCatalog.load(),  # 架空人物の自動応答(途中確認の回答・段階開示の「会う」「承認」)の元
            )
            if not live:
                _load_test_helpers()
                from agents_helpers import StubLlm

                app.state.services.interview.agent.use_model(StubLlm(behavior=interview_stub_behavior))
            print(f"\nブラウザで開く: http://{HOST}:{port}/   (止めるには Ctrl-C)\n", flush=True)
            await uvicorn.Server(uvicorn.Config(app, host=HOST, port=port, log_level="info")).serve()


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="serve_local.py",
        description="画面(static/)を、手元のブラウザで確かめるための開発用サーバ。既定は台本のエージェント・面談はスタブ(Gemini は呼ばない)。"
        f" http://{HOST}:{DEFAULT_PORT} で動く。",
    )
    parser.add_argument("--live", action="store_true", help="交渉エージェントと面談の LLM を、本物の Gemini(Vertex AI)にする(環境変数は run_demo.py と同じ)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"待ち受けるポート(既定 {DEFAULT_PORT})。ホストは {HOST} に固定")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.live:
        problem = check_live_environment(os.environ)
        if problem is not None:
            print(problem, file=sys.stderr)
            return 1
    print("serve_local: 開発用のサーバ(画面の確認用。本番では使わない)")
    print(f"モード: {'live(本物の Gemini)' if args.live else '台本(Gemini は呼ばない。面談の LLM はスタブ)'}")
    print("署名の鍵: 起動のたびに乱数で作る(開発用)。再起動すると、ブラウザのクッキーは使えなくなります。", flush=True)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        with run_demo.firestore_emulator() as host:
            print(f"Firestore エミュレータ: 起動した({host})", flush=True)
            project = f"demo-local-{uuid.uuid4().hex[:12]}"  # エミュレータ専用の名前(demo- で始まる)。起動のたびに別のデータベース
            vault_db = firestore.Client(project=project, database=VAULT_DATABASE)
            default_db = firestore.Client(project=project, database=run_demo.DEFAULT_DATABASE)
            try:
                asyncio.run(serve(live=args.live, port=args.port, vault_db=vault_db, default_db=default_db))
            finally:
                vault_db.close()
                default_db.close()
        print("Firestore エミュレータ: 止めた")
    except KeyboardInterrupt:
        print("止めました。")
    except Exception as exc:
        print(f"エラー: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
