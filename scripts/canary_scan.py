"""生の値のカナリアが、保存・ログ・メモリ・スパンのどこにも残らないことを確かめるスクリプト(design.md §12.1 の AC-02・AC-17・AC-18)。

    uv run python scripts/canary_scan.py
    uv run python scripts/canary_scan.py --plant log --plant vault-db   # 動作確認: カナリアを故意に置く。検出されて、終了コード 1 になるはず

流れ
1. Firestore エミュレータを使う。環境変数 FIRESTORE_EMULATOR_HOST がループバック(127.0.0.1・localhost)を指していれば、その起動済みのものを、
   なければ起動する(tests/conftest.py と同じく、gcloud を使わず、JDK 21 で jar を直接起動する。scripts/run_demo.py の firestore_emulator)。
   プロジェクト ID は demo- で始まる一時のもの。本物の GCP には接続しない。
2. 金庫(vault-db。本物の vault の app)と web((default))を、tests/web_app_helpers.py と同じ組み立てで、同じプロセスの中につなぐ(ASGI)。
   LLM はスタブ(tests/agents_helpers.py の StubLlm)で、本物の Gemini は呼ばない。
3. 面談の API に、カナリアを流して送信まで進める。年収の回答(CANARY-7F3A-SALARY・623.45 万円)・自由コメント(CANARY-7F3A-COMMENT・617.77)・
   辞めた理由(CANARY-7F3A-REASON・411.11。AC-17)・プロフィールの正確な値(経験 7.3141 年・神奈川県)。送信で、丸めたポリシー(650・400)だけが金庫に残る。
4. 調べる(どれも、カナリアが 1 つでも出れば NG)。「空だから通る」ことのないよう、調べた量も出し、0 のところは NG にする。
   - vault-db と (default) の全文書(パスと内容。サブコレクションの下も。bytes の項目は除く)
   - ログ(logging のハンドラで集めた全行。ルートは INFO、web は DEBUG。本番は INFO 以上。§10)
   - web のメモリ(面談の状態の store): 送信の前は、原文とプロフィールの正確な値を持たない。送信のあとは、状態そのものが残らない
   - スパン(OpenTelemetry。ADK が作るスパンの名前・属性・イベント。ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false の本番の設定のまま)
   あわせて、検査が空振りでないことの確認: 原文が LLM には届いていること、金庫に依頼者のポリシーが書かれたこと。

--plant は動作確認用(vault-db・default-db・log・memory・span を繰り返せる)。その場所にカナリアを 1 つ置いて、検出できる(終了コード 1)ことを確かめる。

終了コード: 0 = カナリアはどこにも出ず、どの場所も空でなかった。1 = カナリアが出た・検査が空振りだった・流れが送信まで進まなかった・検査できなかった。
クラウドの確認(gcloud logging read でカナリアを検索する。AC-18)は、このスクリプトの範囲外(エミュレータのログとスパンまで)。
"""

import argparse
import asyncio
import contextlib
import dataclasses
import datetime as dt
import json
import logging
import os
import secrets
import sys
import traceback
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# run_demo(scripts/)と、テストの組み立て(tests/)と、プロジェクトのコード(src/)を import できるようにする。
for _directory in (PROJECT_ROOT / "scripts", PROJECT_ROOT / "tests", PROJECT_ROOT / "src"):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

import httpx  # noqa: E402
from google.cloud import firestore  # noqa: E402
from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

from agents_helpers import StubLlm  # noqa: E402
from negotiation_core import AXIS_KEYS  # noqa: E402
from run_demo import firestore_emulator  # noqa: E402
from vault.app import create_app as create_vault_app  # noqa: E402
from vault.clock import FixedClock  # noqa: E402
from vault.config import DEFAULT_VAULT_CONFIG  # noqa: E402
from vault.firestore_client import VAULT_DATABASE  # noqa: E402
from vault.store import VaultStore  # noqa: E402
from web.vault_client import VaultClient  # noqa: E402
from web_app_helpers import build_web_env, dump_documents  # noqa: E402

CANARY = "CANARY-7F3A"
PLANTED = f"{CANARY}-PLANTED"  # --plant で置く、動作確認用のカナリア
SALARY_ANSWERS = [f"{CANARY}-SALARY 年収は 623.45 万円です", "固定残業代なし", "賞与込み"]
FREE_COMMENT = f"{CANARY}-COMMENT 年収 617.77 万円以上なら行く"
REASON_FOR_LEAVING = f"{CANARY}-REASON 年収 411.11 万円以下の仕事だったので辞めた"
PROFILE = {"experience_years": 7.3141, "prefecture": "神奈川県", "job_category": "it_web"}
SALARY_BASIS = {
    "amount_man_yen": 623.45,
    "amount_period": "annual",
    "amount_kind": "gross",
    "bonus_included": True,
    "bonus_months": 0,
    "fixed_overtime_man_yen_per_month": 0,
}
# 送信のあと、どこにも残らないはずの値(原文のカナリア・年収の生の数字・経験年数の正確な値・都道府県)。数字は、乱数の ID や時刻と
# 偶然に重ならないよう、小数点以下まで書く(丸めたあとの 650・400 は、残ってよい)。
NEEDLES = (CANARY, "623.45", "617.77", "411.11", "7.3141", "神奈川")
# 送信の前でも、メモリに持たないもの(原文と、プロフィールの正確な値。年収の数字は、面談の間は、取り出した値として持つ)。
ORIGINAL_NEEDLES = (CANARY, "7.3141", "神奈川")
SENT_TO_LLM = (f"{CANARY}-SALARY", f"{CANARY}-COMMENT", f"{CANARY}-REASON")  # LLM には届いているはずの原文(届いていなければ、検査は空振り)
PLANT_CHOICES = ("vault-db", "default-db", "log", "memory", "span")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class ScanError(Exception):
    """面談を送信まで進められなかった(検査の前提が崩れた)。"""


@dataclasses.dataclass
class Place:
    """調べた 1 か所。scanned は調べた量(0 なら、空だから通っただけ)、findings は見つかった場所とカナリアの名前(前後の内容は出さない)。"""

    name: str
    scanned: int
    unit: str
    findings: list[str]
    note: str = ""

    @property
    def clean(self) -> bool:
        return self.scanned > 0 and not self.findings

    def render(self) -> str:
        note = f"({self.note})" if self.note else ""
        if self.scanned == 0:
            return f"[NG] {self.name}: 調べた対象が 0 {self.unit}だった(空のものを調べても、通ったことにならない){note}"
        if self.findings:
            shown = " / ".join(self.findings[:5]) + (f" ほか {len(self.findings) - 5} 件" if len(self.findings) > 5 else "")
            return f"[NG] {self.name}: {self.scanned} {self.unit}のうち {len(self.findings)} 件にカナリアが出た: {shown}{note}"
        return f"[OK] {self.name}: {self.scanned} {self.unit}を調べた。カナリアは出なかった{note}"


# ----------------------------------------------------------------------
# 探す部品
# ----------------------------------------------------------------------


def needles_in(text: str, needles: Sequence[str] = NEEDLES) -> list[str]:
    """text に現れる、探している文字列(順番は needles のとおり)。"""
    return [needle for needle in needles if needle in text]


def without_bytes(value, removed: list[int]):
    """bytes の項目を None に置き換えた写しを返す(封印した項目。中身は乱数なので、文字列として探さない)。除いた数を removed[0] に足す。"""
    if isinstance(value, bytes):
        removed[0] += 1
        return None
    if isinstance(value, dict):
        return {key: without_bytes(item, removed) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [without_bytes(item, removed) for item in value]
    return value


def scan_documents(name: str, db: firestore.Client) -> Place:
    """db の全文書(パスと内容)から、カナリアを探す。"""
    documents = dump_documents(db)
    removed = [0]
    findings = []
    for path, data in documents.items():
        text = path + json.dumps(without_bytes(data, removed), default=str, ensure_ascii=False)
        findings += [f"{path} に {needle}" for needle in needles_in(text)]
    return Place(name, len(documents), "文書", findings, note=f"bytes の項目 {removed[0]} 件は除く")


def scan_lines(name: str, lines: Sequence[str]) -> Place:
    return Place(name, len(lines), "行", [f"{number} 行目に {needle}" for number, line in enumerate(lines, 1) for needle in needles_in(line)])


def scan_spans(spans: Sequence) -> Place:
    """スパンの名前・属性・イベント・状態の説明から、カナリアを探す。"""
    findings = []
    for span in spans:
        text = json.dumps(
            {
                "name": span.name,
                "attributes": dict(span.attributes or {}),
                "events": [{"name": event.name, "attributes": dict(event.attributes or {})} for event in span.events],
                "status": str(span.status.description),
            },
            default=str,
            ensure_ascii=False,
        )
        findings += [f"スパン {span.name} に {needle}" for needle in needles_in(text)]
    return Place("スパン", len(spans), "件", findings, note=f"ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS={os.environ.get('ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS', '未設定')}")


def scan_memory(before_submit: str | None, store) -> Place:
    """面談の状態(web のメモリ)。送信の前は原文・プロフィールの正確な値を持たず、送信のあとは、状態そのものが残らない。"""
    findings = [f"送信の前の状態に {needle}" for needle in needles_in(before_submit or "", ORIGINAL_NEEDLES)]
    after = repr(dict(store._states))  # 読むだけ(store.get は最後に使った時刻を更新する)
    findings += [f"送信のあとのメモリに {needle}" for needle in needles_in(after)]
    if len(store) > 0:
        findings.append(f"送信のあとも面談の状態が {len(store)} 件残っている")
    note = f"送信の前の状態 {0 if before_submit is None else 1} 件と、送信のあとの状態(0 件のはず){len(store)} 件"
    return Place("メモリ(面談の状態)", 0 if before_submit is None else 1, "件", findings, note=note)


class LogCollector(logging.Handler):
    """logging のログを、1 行ずつ集める(例外の文も含む)。"""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.extend(self.format(record).splitlines() or [""])
        except Exception:  # 集める側の失敗で、検査を止めない(整形できないログは数えない)
            pass


# ----------------------------------------------------------------------
# 面談を流す
# ----------------------------------------------------------------------


def interview_llm() -> StubLlm:
    """面談の LLM(スタブ)。入力の task で、年収の読み取りか、発言の構造化かを分けて返す(本物の Gemini は呼ばない)。"""

    def behavior(request) -> str:
        task = json.loads(request.contents[-1].parts[0].text)["task"]
        if task == "salary_basis":
            return json.dumps(SALARY_BASIS)
        polarity, salary = ("accept", 617.77) if task == "free_comment" else ("reject", 411.11)
        return json.dumps({"statements": [{"polarity": polarity, **{axis: None for axis in AXIS_KEYS}, "salary": salary}]})

    return StubLlm(behavior=behavior)


async def drive_interview(env, browser, principal_id: str) -> str | None:
    """面談の API で、送信まで進める。送信の前(確認の直前)の、面談の状態(メモリ)の写しを返す(状態がなければ None)。"""
    base = f"/v1/principals/{principal_id}/interview"

    async def step(method: str, suffix: str, body: dict | None = None) -> dict:
        response = await (browser.get(f"{base}/{suffix}") if method == "GET" else browser.post(f"{base}/{suffix}", body))
        if response.status_code != 200:
            detail = response.json().get("detail") if response.headers.get("content-type", "").startswith("application/json") else ""
            raise ScanError(f"面談を送信まで進められなかった: {method} {suffix} が {response.status_code}({detail if isinstance(detail, str) else '検証エラー'})")
        return response.json()

    await step("POST", "begin", {})
    await step("POST", "profile", PROFILE)
    proposal = await step("POST", "salary/answers", {"answers": SALARY_ANSWERS})
    await step("POST", "salary/confirm", {"salary_basis": proposal["salary_basis"]})
    await step("POST", "axes", {"removed_axes": []})
    for pair in (await step("GET", "choices"))["pairs"][:5]:
        await step("POST", "choices/answer", {"pair_id": pair["id"], "option": "a", "response": "go"})
        await step("POST", "choices/answer", {"pair_id": pair["id"], "option": "b", "response": "no_go"})
    await step("POST", "comment", {"text": FREE_COMMENT})
    await step("POST", "reason", {"text": REASON_FOR_LEAVING})
    await step("POST", "blocklist", {"company_ids": ["case1-company"]})
    state = env.services.interview.store._states.get(principal_id)  # 送信の前の状態(読むだけ。store.get のように、使った時刻を更新しない)
    before_submit = None if state is None else repr(state)
    await step("POST", "confirm", {})
    await step("POST", "worst-case/approve")
    await step("POST", "submit")
    return before_submit


# ----------------------------------------------------------------------
# 検査
# ----------------------------------------------------------------------


@dataclasses.dataclass
class Report:
    places: list[Place]
    problems: list[str]  # 検査の前提が崩れていること(カナリアが LLM に届いていない・金庫にポリシーがない)

    @property
    def passed(self) -> bool:
        return all(place.clean for place in self.places) and not self.problems

    def render(self) -> str:
        lines = [place.render() for place in self.places]
        lines += [f"[NG] 検査の前提: {problem}" for problem in self.problems]
        if self.passed:
            lines.append("\nカナリアは、どこにも現れなかった → 終了コード 0")
        else:
            lines.append("\nカナリアが残っている、または検査が空振りだった → 終了コード 1")
        return "\n".join(lines)


def install_span_collector() -> InMemorySpanExporter:
    """OpenTelemetry のスパンを、メモリに集める(プロセスで 1 回だけ設定できる TracerProvider。このスクリプトは、実行ごとに別のプロセス)。"""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return exporter


def plant_in_documents(env, plants: Sequence[str]) -> None:
    if "vault-db" in plants:
        env.store._db.collection("control").document("planted").set({"note": PLANTED})
    if "default-db" in plants:
        env.default_db.collection("control").document("planted").set({"note": PLANTED})


async def scan(plants: Sequence[str]) -> Report:
    exporter = install_span_collector()
    collector = LogCollector()
    root = logging.getLogger()
    previous_levels = (root.level, logging.getLogger("web").level)
    root.addHandler(collector)
    root.setLevel(logging.INFO)  # 本番と同じ水準(ADK と a2a-sdk は DEBUG で本文を出す。本番は INFO 以上。§10)
    logging.getLogger("web").setLevel(logging.DEBUG)  # 自分たちのコードは、DEBUG まで調べる

    project = f"demo-canary-{uuid.uuid4().hex[:12]}"
    vault_db = firestore.Client(project=project, database=VAULT_DATABASE)
    default_db = firestore.Client(project=project, database="(default)")
    clock = FixedClock(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
    store = VaultStore(db=vault_db, clock=clock, config=DEFAULT_VAULT_CONFIG)
    vault_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_vault_app(store)), base_url="http://vault")
    env = build_web_env(
        store=store, clock=clock, vault=VaultClient(vault_http), default_db=default_db, session_key=secrets.token_urlsafe(32)
    )
    llm = interview_llm()
    try:
        env.services.interview.agent.use_model(llm)
        browser = env.browser()
        principal_id = await browser.open_start_page()
        before_submit = await drive_interview(env, browser, principal_id)

        problems = []
        sent = "".join(request.dump for request in llm.requests)
        missing = [name for name in SENT_TO_LLM if name not in sent]
        if missing:
            problems.append(f"原文が LLM に届いていない({', '.join(missing)})。流した値が、検査の対象まで来ていない")
        if not store._principal_ref(principal_id).get().exists:
            problems.append("金庫に、依頼者のポリシーが書かれていない(送信が通っていない)")

        plant_in_documents(env, plants)
        if "log" in plants:
            logging.getLogger("web.interview.service").info("control %s", PLANTED)
        if "memory" in plants:
            env.services.interview.store.create("planted-principal").inactive.add(PLANTED)
        if "span" in plants:
            with trace.get_tracer("canary_scan").start_as_current_span("planted", attributes={"note": PLANTED}):
                pass

        places = [
            scan_documents("vault-db", vault_db),
            scan_documents("(default)", default_db),
            scan_lines("ログ", collector.lines),
            scan_memory(before_submit, env.services.interview.store),
            scan_spans(exporter.get_finished_spans()),
        ]
        return Report(places, problems)
    finally:
        await env.aclose()
        await vault_http.aclose()
        vault_db.close()
        default_db.close()
        root.removeHandler(collector)
        root.setLevel(previous_levels[0])
        logging.getLogger("web").setLevel(previous_levels[1])


@contextlib.contextmanager
def emulator() -> Iterator[None]:
    """Firestore エミュレータ(起動済みのループバックのものがあればそれ、なければ起動する)。"""
    host = os.environ.get("FIRESTORE_EMULATOR_HOST", "")
    if host and host.rsplit(":", 1)[0].strip("[]") in LOOPBACK_HOSTS:
        print("Firestore エミュレータ: 起動済みのものを使う")
        yield
        return
    with firestore_emulator():
        print("Firestore エミュレータ: 起動した")
        yield


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生の値のカナリアが、保存・ログ・メモリ・スパンのどこにも残らないことを確かめる(AC-02・AC-17・AC-18)。")
    parser.add_argument("--plant", action="append", choices=PLANT_CHOICES, default=[], help="動作確認用: その場所にカナリアを置く(検出されて、終了コード 1 になる)")
    args = parser.parse_args(argv)
    print("カナリア検査(AC-02・AC-17・AC-18)")
    try:
        with emulator():
            report = asyncio.run(scan(args.plant))
    except ScanError as exc:
        print(f"検査できなかった: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"検査できなかった: {type(exc).__name__}", file=sys.stderr)
        traceback.print_exc()
        return 1
    print("面談を、年収の回答・自由コメント・辞めた理由・プロフィールにカナリアを入れて、送信まで進めた(LLM はスタブ)\n")
    print(report.render())
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
