"""web の画面 API・依頼者のセッション・削除(1d-2)のテストで共通に使う部品。

`test_` で始まらないので pytest には収集されない(tests/web_helpers.py と同じ扱い)。
本物の LLM・GCP には接続しない: 金庫は本物の vault の app を ASGI のままつなぎ、エージェントは
スタブ(IdleAgents。台本がなければ応答せず、交渉を途中のまま止めておく)、時計は注入(FixedClock)、
web の app は httpx の ASGITransport で直接呼ぶ(ブラウザの代わりの Browser がクッキーを持つ)。
テストは sleep しない(FakeSleep)。

レフェリーのタスクは、既定では動かさない(交渉を作っても、見回りが走っても、タスクを作らない)。権限・セッション・
削除のテストは、交渉の「存在」だけを使い、レフェリーが金庫を呼び続けると、テストの終わりに金庫の呼び出しが
途中で残る(別スレッドで動く呼び出しは、タスクを cancel しても止まらず、エミュレータを止めた後も再試行して、
プロセスの終了が数分止まる)。レフェリーが要るテストだけ、enable_referees() で動かす。

署名の鍵は、コードにも既定値にも持たない。テストでは、conftest のフィクスチャ(session_key)で与える。
"""

import asyncio
import json
import os
from dataclasses import dataclass, field

import httpx
from fastapi import FastAPI
from google.cloud import firestore

from vault.clock import FixedClock
from vault.templates import put_template
from vault_helpers import make_employer_template
from web.app import create_app
from web.config import DEFAULT_WEB_CONFIG, WebConfig
from web.services import WebServices
from web.session import SESSION_COOKIE_NAME
from web_helpers import AgentCall, FakeSleep, ScriptedAgents

BASE_URL = "https://web.test"  # Secure のクッキーは、https でないと返らない
REQUESTED_WITH = {"X-Requested-With": "XMLHttpRequest"}
CANARY = "CANARY-7F3A-JOB-SUMMARY"  # 段 1 の職務要約などに置く、消えているべき文字列(design.md §12.1 AC-02 と同じ形)


class IdleAgents(ScriptedAgents):
    """台本のないエージェントの呼び出しには、応答しない(交渉を途中のまま止めておく)。

    画面 API のテストは、交渉を作るだけで、手を打たせない。作った交渉のレフェリーが、勝手に進めないように。
    タスクの cancel(テストの後始末)でだけ抜ける。
    """

    async def __call__(self, role, turn_input, *, nid, timeout_s) -> dict:
        if not self._scripts[role]:
            self.calls.append(AgentCall(role=role, nid=nid, timeout_s=timeout_s, turn_input=turn_input))
            await asyncio.Event().wait()
        return await super().__call__(role, turn_input, nid=nid, timeout_s=timeout_s)


def interview_body(**overrides) -> dict:
    """面談の送信の本文(面談の結果。丸める前の生の値のアンカー・外した軸・属性帯)。"""
    body = {
        "accept_anchors": [
            {
                "salary": 620,
                "remote_days": 2,
                "night_duty": 4,
                "review_months": 12,
                "training": "*",
                "side_job": "*",
                "start": "*",
            }
        ],
        "reject_anchors": [
            {
                "salary": 410,
                "remote_days": 0,
                "night_duty": 8,
                "review_months": 12,
                "training": "*",
                "side_job": "*",
                "start": "*",
            }
        ],
        "removed_axes": [],
        "attribute_bands": {"experience_band": "3_to_5y", "region_block": "kanto", "job_category": "it_web"},
    }
    body.update(overrides)
    return body


class Browser:
    """クッキーを持つ 1 つのブラウザ。web の app に ASGI のまま(ネットワークを通さず)つなぐ。"""

    def __init__(self, env: "WebAppEnv") -> None:
        self._env = env
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url=BASE_URL)

    async def get(self, path: str, **params) -> httpx.Response:
        return await self.client.get(path, params=params or None)

    async def post(self, path: str, body: dict | None = None, *, requested_with: bool = True) -> httpx.Response:
        """POST。既定で X-Requested-With を付ける(付けない場合の確認は requested_with=False)。"""
        headers = REQUESTED_WITH if requested_with else {}
        return await self.client.post(path, json=body if body is not None else {}, headers=headers)

    @property
    def cookie(self) -> str | None:
        """今持っているセッションクッキーの値(なければ None)。"""
        return self.client.cookies.get(SESSION_COOKIE_NAME)

    @property
    def pid(self) -> str | None:
        """今持っているクッキーの依頼者 ID(期限は見ずに、署名だけを検証して取り出す)。"""
        token = self.cookie
        if token is None:
            return None
        return self._env.services.codec._serializer.loads(token)["pid"]

    def set_cookie(self, token: str) -> None:
        self.client.cookies.set(SESSION_COOKIE_NAME, token, domain="web.test")

    async def open_start_page(self) -> str:
        """開始ページを GET で開いて、依頼者 ID を持つ(すでにクッキーがあれば、そのまま)。"""
        response = await self.get("/start")
        assert response.status_code == 200, response.text
        assert self.pid is not None
        return self.pid

    async def register(self, **overrides) -> str:
        """開始ページを開き、面談を送って、利用記録と金庫のポリシーを作る。依頼者 ID を返す。"""
        pid = await self.open_start_page()
        response = await self.post(f"/v1/principals/{pid}/interview", interview_body(**overrides))
        assert response.status_code == 200, response.text
        return pid

    async def create_negotiation(self, pid: str, employer_template_id: str, request_id: str = "request-0001") -> str:
        """交渉を作って nid を返す。"""
        response = await self.post(
            f"/v1/principals/{pid}/negotiations",
            {"request_id": request_id, "employer_template_id": employer_template_id},
        )
        assert response.status_code == 200, response.text
        return response.json()["nid"]

    async def aclose(self) -> None:
        await self.client.aclose()


@dataclass
class WebAppEnv:
    """テスト用の web 一式。金庫(store)・(default)・エージェントのスタブ・時計と、ブラウザを作る口を持つ。"""

    app: FastAPI
    services: WebServices
    store: object  # VaultStore(金庫の中身を直接確かめる)
    clock: FixedClock
    vault: object  # web に渡した金庫のクライアント(VaultClient か、それを包んだもの)
    default_db: firestore.Client
    agents: IdleAgents
    sleep: FakeSleep
    session_key: str
    _browsers: list[Browser] = field(default_factory=list)

    def disable_referees(self) -> None:
        """レフェリーのタスクを作らない(交渉の作成・見回りが start を呼んでも、何もしない)。"""
        self.services.referees.start = lambda context: False

    def enable_referees(self) -> None:
        """レフェリーのタスクを動かす(disable_referees で差し替えた start を、元に戻す)。"""
        self.services.referees.__dict__.pop("start", None)

    def browser(self) -> Browser:
        browser = Browser(self)
        self._browsers.append(browser)
        return browser

    def put_employer_template(self, **kwargs) -> str:
        """金庫に、求人(フィクスチャ)のテンプレートを置いて、その ID を返す。"""
        template = make_employer_template(**kwargs)
        put_template(self.store._db, template)
        return template.template_id

    async def aclose(self) -> None:
        for browser in self._browsers:
            await browser.aclose()
        await self.services.referees.stop_all()


def ensure_databases_are_empty(*databases: firestore.Client) -> None:
    """各データベース(vault-db と (default))が空であることを確かめる。空でなければ、エミュレータの REST で消し直す。

    tests/conftest.py の消去は、エミュレータの応答を確かめない。消えていない文書が次のテストに残ると、見回りが
    別のテストの交渉を拾う・「〜件だけ」の確認が崩れる(全体を流したときに、まれに起きた)。ここで確かめ、
    消し直しても空にならなければ、失敗にする(原因の分からないまま、別の確認が落ちないように)。
    """
    host = os.environ["FIRESTORE_EMULATOR_HOST"]
    for database in databases:
        url = (
            f"http://{host}/emulator/v1/projects/{database.project}"
            f"/databases/{database._database}/documents"
        )
        for _ in range(5):
            if next(iter(database.collections()), None) is None:
                break
            httpx.delete(url, timeout=10)
        else:
            raise AssertionError(f"database {database._database!r} could not be emptied before the test")


def build_web_env(
    *,
    store,
    clock: FixedClock,
    vault,
    default_db: firestore.Client,
    session_key: str,
    config: WebConfig = DEFAULT_WEB_CONFIG,
    agents_base_url: str | None = None,
    use_stub_agents: bool = True,
    run_referees: bool = False,
) -> WebAppEnv:
    """web の app を組み立てる。vault には、金庫のクライアントを包んだもの(止める仕掛けなど)も渡せる。

    use_stub_agents=False にすると、send_turn を差し込まず、agents_base_url を束ねた本物の
    agents.client.send_turn を使う(結合のテスト)。run_referees の既定は False(モジュールの docstring を参照)。
    """
    ensure_databases_are_empty(store._db, default_db)
    agents = IdleAgents()
    sleep = FakeSleep(clock)
    kwargs = {}
    if agents_base_url is not None:
        kwargs["agents_base_url"] = agents_base_url
    if use_stub_agents:
        kwargs["send_turn"] = agents
    app = create_app(
        vault=vault,
        default_db=default_db,
        session_key=session_key,
        clock=clock,
        sleep=sleep,
        config=config,
        **kwargs,
    )
    env = WebAppEnv(
        app=app,
        services=app.state.services,
        store=store,
        clock=clock,
        vault=vault,
        default_db=default_db,
        agents=agents,
        sleep=sleep,
        session_key=session_key,
    )
    if not run_referees:
        env.disable_referees()
    return env


# --- Firestore の中身の確認(「残らない」の確認用) ---


def plant_canaries(env: "WebAppEnv", principal_id: str, nids: list[str], canary: str = CANARY) -> None:
    """段の状態(段 1 の職務要約の項目)と開示台帳に、消えているべきカナリアを直接置く。

    面談と段 1 の画面は後の段なので、そこで入るはずの値を、Firestore に直接書いて確かめる。
    """
    for nid in nids:
        env.default_db.collection("stages").document(nid).update({"job_summary": canary})
    ledger = env.default_db.collection("principals").document(principal_id).collection("ledger")
    ledger.document("row-1").set({"principal_id": principal_id, "stage": 1, "note": canary, "at": env.clock.now()})
    ledger.document("row-2").set({"principal_id": principal_id, "stage": 2, "note": "meet", "at": env.clock.now()})


def dump_documents(db: firestore.Client, parent=None) -> dict[str, dict]:
    """db の全文書を、パス → 内容の dict で返す(サブコレクションの下、親の文書がない下の階層も含む)。"""
    documents: dict[str, dict] = {}
    collections = db.collections() if parent is None else parent.collections()
    for collection in collections:
        for reference in collection.list_documents():
            snapshot = reference.get()
            if snapshot.exists:
                documents[reference.path] = snapshot.to_dict()
            documents.update(dump_documents(db, reference))
    return documents


def documents_mentioning(db: firestore.Client, *needles: str) -> dict[str, dict]:
    """パスまたは内容に、needles のどれかが現れる文書(あれば、消えていない証拠)。"""
    found = {}
    for path, data in dump_documents(db).items():
        text = path + json.dumps(data, default=str, ensure_ascii=False)
        if any(needle in text for needle in needles):
            found[path] = data
    return found


# --- 止める・失敗させる仕掛け(タイミングに頼らずに、競合や途中の失敗を確かめるため) ---


class GatedVault:
    """金庫のクライアントを包み、呼び出しを記録する。gated に指定した呼び出しは、release が set されるまで止める。

    止めた呼び出しが始まったこと(entered)を、テストが待てる。呼び出しの前後は events に
    "名前:start"・"名前:end" で残る(同じ依頼者の操作が 1 つずつ順に処理されたことの確認用)。
    """

    def __init__(self, inner, *, gated: tuple[str, ...] = ()) -> None:
        self._inner = inner
        self._gated = set(gated)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.events: list[str] = []

    def __getattr__(self, name: str):
        attribute = getattr(self._inner, name)
        if not callable(attribute):
            return attribute

        async def call(*args, **kwargs):
            self.events.append(f"{name}:start")
            if name in self._gated:
                self.entered.set()
                await self.release.wait()
            try:
                return await attribute(*args, **kwargs)
            finally:
                self.events.append(f"{name}:end")

        return call


class DeletionProbe:
    """削除の流れの各段(金庫・開示台帳・段の状態・利用記録)の直前に、利用記録の状態を記録する。

    fail_once_at(段) で、その段に入ったところで 1 回だけ失敗させる(その段の処理は行わない)。
    「各段の間で失敗させても、依頼者の見回りが最後まで進める」「利用記録は最後に消え、それより先には
    消えない」の確認に使う。段の名前は STEPS(流れの順)。
    """

    STEPS = ("vault", "ledger", "stages", "meta")

    def __init__(self, env: WebAppEnv) -> None:
        self._env = env
        self.steps: list[tuple[str, str | None]] = []  # (段, その直前の利用記録の状態。なければ None)
        self._fail_once: set[str] = set()
        services = env.services
        self._wrap(services.vault, "delete_principal", "vault")
        self._wrap(services.ledger, "delete_all", "ledger")
        self._wrap(services.stages, "delete_for_principal", "stages")
        self._wrap(services.meta, "delete", "meta")

    def fail_once_at(self, step: str) -> None:
        assert step in self.STEPS
        self._fail_once.add(step)

    def meta_state(self, principal_id: str) -> str | None:
        snapshot = self._env.default_db.collection("principals_meta").document(principal_id).get()
        return snapshot.to_dict()["deletion_state"] if snapshot.exists else None

    def _wrap(self, target, method_name: str, step: str) -> None:
        original = getattr(target, method_name)

        async def wrapper(principal_id, *args, **kwargs):
            self.steps.append((step, self.meta_state(principal_id)))
            if step in self._fail_once:
                self._fail_once.discard(step)
                raise RuntimeError(f"injected failure at the {step} step")
            return await original(principal_id, *args, **kwargs)

        setattr(target, method_name, wrapper)


async def wait_until(condition, *, timeout: float = 10) -> None:
    """condition() が True になるまで、他のタスクに譲りながら待つ(sleep しない。時間切れは失敗)。"""

    async def _poll() -> None:
        while not condition():
            await asyncio.sleep(0)

    await asyncio.wait_for(_poll(), timeout=timeout)


def lock_users(env: WebAppEnv, principal_id: str) -> int:
    """依頼者のロックを、持っている数と待っている数の合計(ロックの順番待ちに入ったことを確かめるため)。"""
    entry = env.services.locks._entries.get(principal_id)
    return 0 if entry is None else entry.users
