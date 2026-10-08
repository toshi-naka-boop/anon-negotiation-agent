"""画面(static/)に要る小さな API(design.md §6.3・§7・§8.4。作業パッケージ L)。画面そのものは含まない。

既存の API(web.api・web.interview.api・web.attack.router・web.activity_api)は変えず、画面が使うのに足りない口だけをここに置く。
web.api の build_router が include する、別の APIRouter。

| メソッドとパス | 内容 | セッション |
|---|---|---|
| GET /v1/session | ブラウザが持つクッキーの依頼者 ID と、面談を送信済みか(なければ null・false)。クッキーは HttpOnly で JS から読めないが、本人の経路の URL に依頼者 ID が要るため | 見る |
| GET /v1/interview/notice | 面談の入口の注記(面談 API の begin が返す notice と同じ中身)。begin は面談の状態をメモリに作るので、入口のページの表示には使わない | 見る(使わない) |
| GET /v1/jobs | 求人の一覧(企業名込みの公開情報。confidential な求人は企業名を伏せる。§6.1・FR-32)。攻撃モード用の求人は載せない | 見る(使わない) |
| GET /v1/demo/cases | デモのケース 1〜3 の一覧(企業名・求人名・説明文・テンプレート ID・リプレイがあるか) | 見ない(デモ) |
| GET /v1/demo/replays/{case} | fixtures/replays/case{N}.jsonl をそのまま返す(N は 1〜3 だけ。それ以外は 404) | 見ない(デモ) |
| GET /v1/stream/negotiations/{nid}/activity?after_seq= | 本人の活動ログの SSE(event: activity)。権限は /v1/negotiations/{nid}/activity と同じ | 見ない(下) |
| GET /v1/stream/demo/negotiations/{nid}/activity?side=&after_seq= | デモ・攻撃の活動ログの SSE。権限は /v1/demo/negotiations/{nid}/activity と同じ | 見ない |

SSE(sse-starlette)
- サーバが 2 秒ごとに活動ログを読み、新しい記録があるときだけ `event: activity` で送る(data は ActivityLog の JSON。id は next_after_seq)。
  ブラウザの EventSource は、切れると Last-Event-ID を付けて再接続するので、それを after_seq より優先する(続きから読む)。
  最終結果(final_result)を送ったら `event: end` を送って閉じる(画面は閉じる)。30 秒で閉じる(画面がつなぎ直す)。
  読めなくなったときは `event: problem` を送って閉じる(画面は通常の GET の再取得に切り替えて、理由を表示する)。
- ミドルウェアは、同じ依頼者のリクエストを 1 つずつ処理するため、応答を送り終えるまで依頼者ごとのロックを持つ(web.session_middleware)。
  SSE が 30 秒つながっている間ずっと持つと、同じ依頼者の操作と、レフェリーの金庫への操作(web.locks の PrincipalScopedVault)が
  30 秒止まる。そのため、/v1/stream/ はミドルウェアのセッションを見ない経路にして(web.app の session_free_prefixes)、本人の経路は、
  この module が同じ確認(署名付きクッキー・削除中でないこと・交渉の当事者であること)を、始めに 1 回行う。
  接続してから本人の削除が始まると、「削除中はすべての操作を拒否」(§6.3)に反して、金庫の削除と競合しながら記録を送り続けてしまう。
  そのため、各 poll(2 秒ごと)の前に、利用記録 principals_meta を読み直す(読むだけ。依頼者のロックは取らない。台帳 X-83)。削除中・削除済み
  (利用記録がない)なら、次の記録を送らずに、`event: problem`(detail は principal_deleting・principal_deleted)を送って閉じる(画面は通常の GET の
  再取得に切り替え、その GET が 409 などで理由を示す)。読み直せないとき(Firestore の失敗)も、閉じる側に倒す(problem の stream_failed)。
  デモの経路は、/v1/demo/negotiations/{nid}/activity と同じ 2 段の確認(web の段の状態と金庫のデモ用の読み出しの口。台帳 X-38)を行う。
- 同時本数の上限(台帳 C-65): SSE は 1 本が最長 30 秒つながり、web は 1 インスタンスなので、匿名のクライアントが、同時リクエストの枠を SSE で埋められる
  (面談・画面・リプレイまで止まる)。そこで、つなぐ前に、いまつないである本数を、全体(sse_max_connections。既定 20)と、クライアント IP ごと
  (sse_max_connections_per_client。既定 2。web.client_ip)に数える(web.limits の SseConnectionLimiter。メモリの中だけ。Firestore には触れず、
  入口の枠 rate_overall_limit にも数えない)。超えたら 429(detail は {"code": "rate_limited", "entrance": "sse", "scope", "limit", ...}、
  Retry-After は 2 秒)で、画面の EventSource はつなぎ直さず、通常の GET の再取得(2 秒ごと)に切り替わる。席は、権限の確認(Firestore・金庫を読む)
  より先に取る(上限を超えた要求が、バックエンドを読まないように)。確認が失敗しても、接続が終わっても(自分で閉じた・クライアントが切った・例外)、
  必ず戻す(_SlotEventSourceResponse)。デモ・攻撃の画面は 1 交渉につき側ごとに 2 本つなぐので、既定では、クライアント IP 1 つが 1 交渉ぶんを見られる。
- 開始の回数の枠(台帳 C-72・X-89): 終わった交渉の配信は、最終結果を送るとすぐ閉じて席を返すので、同時本数の上限だけでは、開き直しの繰り返し(金庫と Firestore の読み出し)を止められない。
  そこで、SSE の開始(再接続を含む)も、ほかの GET と同じく、読み取りの枠(web.limits の AnonymousReadLimitMiddleware。クライアントごとに 1 分 120 回。メモリの中だけ)に 1 回と数える。
  ミドルウェアはルートより外にあるので、席の確認・権限の確認(Firestore・金庫を読む)より前に数え、超えたら、ここまで来ずに 429(入口 anonymous_read)になる。

ログには、例外の型名だけを書く(組み合わせの値・評価・依頼者 ID を、ここから出さない)。
"""

import asyncio
import json
import logging
import time
import tomllib
from collections.abc import AsyncIterator, Awaitable, Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from sse_starlette import EventSourceResponse, ServerSentEvent
from starlette.types import Receive, Scope, Send

from negotiation_core import Side

from vault.api_models import EventViewItem

from web.activity_api import ActivityLog, to_activity_log
from web.interview.templates import FIXTURES_DIRECTORY
from web.limits import SseSlot
from web.principals_meta import DELETION_DELETING
from web.services import WebServices
from web.session import SESSION_COOKIE_NAME
from web.vault_client import VaultNotFoundError, VaultUnavailableError

_log = logging.getLogger(__name__)

# ミドルウェアのセッションを見ない経路(web.app の session_free_prefixes に入れる)。理由は、モジュールの docstring の SSE の節。
STREAM_PATH_PREFIX = "/v1/stream/"

# デモのケース(§8.4)。ケース 3 は攻撃のケース。リプレイは fixtures/replays/case{N}.jsonl。
CASE_NUMBERS = (1, 2, 3)
ATTACK_CASE = 3
REPLAYS_DIRECTORY = FIXTURES_DIRECTORY / "replays"

# 1 つの側のイベント番号の上限(web.activity_api の _MAX_SEQ と同じ。テストで一致を確かめる)。
MAX_SEQ = 2**31 - 1

ACTIVITY_EVENT = "activity"
END_EVENT = "end"
PROBLEM_EVENT = "problem"

# ケースの見出しと説明文(§8.4 の「各ケースに持たせる性質」)。フィクスチャ(fixtures/case{N}.toml)には説明の項目がない
# (読み込みは知らない項目を拒否する)ので、ここに置く。企業名・求人名・テンプレート ID は、フィクスチャから読む。
CASE_TEXTS: dict[int, tuple[str, str]] = {
    1: (
        "年収だけでは合意できないケース",
        "リモート 0 日では、年収だけで合意できる組み合わせがありません。リモートや当直を動かせば合意できます。",
    ),
    2: (
        "合意できる組み合わせがないケース",
        "両者が受けられる組み合わせがありません。交渉は上限まで続き、双方に「なし」だけが返ります。",
    ),
    3: (
        "攻撃のケース",
        "求人担当の立場から、何でも受ける求人で候補者の条件を探ります。金庫は丸めた値でしか答えないので、"
        "探っても年収の境目はグリッド 1 マスより狭くなりません。攻撃の実演は攻撃画面で行います。",
    ),
}


@dataclass(frozen=True)
class StreamConfig:
    """SSE の暫定値(秒・ミリ秒)。決まったら、設定ファイルへ移す。"""

    poll_interval_seconds: float = 2.0
    max_duration_seconds: float = 30.0
    retry_milliseconds: int = 2000


DEFAULT_STREAM_CONFIG = StreamConfig()

ReadActivity = Callable[[int], Awaitable[ActivityLog]]
Sleep = Callable[[float], Awaitable[None]]
Monotonic = Callable[[], float]


class StreamNotAllowed(Exception):
    """読む前の確かめ直しで、この接続をもう続けられないと分かった(本人の削除が始まった・終わった。台帳 X-83)。

    read が投げる。activity_event_stream は、次の記録を送らずに、detail(列挙値。入力の値は入れない)を `problem` で送って閉じる。
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class _SlotEventSourceResponse(EventSourceResponse):
    """応答が終わったとき(自分で閉じた・クライアントが切った・例外)に、取っておいた席(SseSlot)を必ず戻す EventSourceResponse(台帳 C-65)。

    戻すのは、ストリームの generator の finally ではなく、応答の呼び出し全体の finally で行う: generator が 1 度も動き出さないうちに応答が終わっても
    (動き出さない generator の finally は動かない)、席が残らないように。SseSlot.release は何度呼んでも 1 回しか戻さない。
    """

    def __init__(self, content: AsyncIterator[ServerSentEvent], *, slot: SseSlot) -> None:
        super().__init__(content)
        self._slot = slot

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._slot.release()


# ----------------------------------------------------------------------
# フィクスチャから作る一覧(公開情報だけ。生の条件・職務要約・連絡先は読まない)
# ----------------------------------------------------------------------


def _read_employer(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))["employer"]


def list_jobs(
    directory: Path = FIXTURES_DIRECTORY, *, exclude_template_ids: Collection[str] = ()
) -> list[dict[str, Any]]:
    """求人の一覧(§6.1・FR-32)。confidential な求人は、企業名を null にする(段 1 まで伏せる)。exclude_template_ids の求人は載せない。"""
    jobs = []
    for path in sorted(directory.glob("case*.toml")):
        employer = _read_employer(path)
        if employer["template_id"] in exclude_template_ids:
            continue
        job = employer["public_job"]
        jobs.append(
            {
                "job_id": employer["job_id"],
                "template_id": employer["template_id"],
                "company_name": None if job["confidential"] else employer["company_name"],
                "title": job["title"],
                "summary": job["summary"],
                "confidential": job["confidential"],
                "job_category": job["job_category"],
            }
        )
    return jobs


def list_cases(directory: Path = FIXTURES_DIRECTORY) -> list[dict[str, Any]]:
    """デモのケース 1〜3 の一覧(§8.4)。フィクスチャの公開情報(企業名・求人名・テンプレート ID)と、説明文(CASE_TEXTS)。"""
    cases = []
    for number in CASE_NUMBERS:
        raw = tomllib.loads((directory / f"case{number}.toml").read_text(encoding="utf-8"))
        employer = raw["employer"]
        title, description = CASE_TEXTS[number]
        cases.append(
            {
                "case": number,
                "title": title,
                "description": description,
                "attack": number == ATTACK_CASE,
                "candidate_template_id": raw["candidate"]["template_id"],
                "employer_template_id": employer["template_id"],
                "company_name": employer["company_name"],
                "job_title": employer["public_job"]["title"],
                "job_summary": employer["public_job"]["summary"],
                "replay_available": (directory / "replays" / f"case{number}.jsonl").is_file(),
            }
        )
    return cases


# ----------------------------------------------------------------------
# SSE
# ----------------------------------------------------------------------


async def activity_event_stream(
    read: ReadActivity,
    after_seq: int,
    *,
    config: StreamConfig = DEFAULT_STREAM_CONFIG,
    sleep: Sleep = asyncio.sleep,
    monotonic: Monotonic = time.monotonic,
) -> AsyncIterator[ServerSentEvent]:
    """read(after_seq) で活動ログを周期的に読み、新しい記録があるときだけ activity を送る。

    最終結果を送ったら end で閉じる。config.max_duration_seconds たったら(何も送らず)閉じる。一時的な金庫の失敗(503)は、
    次の周期で読み直す。read が StreamNotAllowed を投げたら(読む前の確かめ直しで、続けられないと分かった。台帳 X-83)、何も読まずに、その理由を
    problem で送って閉じる。それ以外の失敗は problem(stream_failed)を送って閉じる(応答の始まりを送った後なので、HTTP の状態では伝えられない)。
    """
    deadline = monotonic() + config.max_duration_seconds
    while True:
        log: ActivityLog | None
        try:
            log = await read(after_seq)
        except VaultUnavailableError:
            log = None
        except StreamNotAllowed as stopped:
            yield ServerSentEvent(event=PROBLEM_EVENT, data=json.dumps({"detail": stopped.detail}))
            return
        except Exception as exc:  # noqa: BLE001  応答の始まりを送った後なので、problem で伝える
            _log.warning("activity stream stopped error=%s", type(exc).__name__)
            yield ServerSentEvent(event=PROBLEM_EVENT, data=json.dumps({"detail": "stream_failed"}))
            return
        if log is not None and log.entries:
            after_seq = log.next_after_seq
            yield ServerSentEvent(
                data=log.model_dump_json(), event=ACTIVITY_EVENT, id=str(after_seq), retry=config.retry_milliseconds
            )
            if any(entry.action == "final_result" for entry in log.entries):
                yield ServerSentEvent(event=END_EVENT, data="{}")
                return
        if monotonic() >= deadline:
            return
        await sleep(config.poll_interval_seconds)


def resume_position(request: Request, after_seq: int) -> int:
    """読み始める位置。EventSource の再接続は Last-Event-ID を付けるので、after_seq より進んでいればそちらを使う。"""
    last_event_id = request.headers.get("last-event-id", "")
    if last_event_id.isascii() and last_event_id.isdigit() and int(last_event_id) <= MAX_SEQ:
        return max(after_seq, int(last_event_id))
    return after_seq


# ----------------------------------------------------------------------
# ルート
# ----------------------------------------------------------------------


def build_ui_router(services: WebServices, stream: StreamConfig = DEFAULT_STREAM_CONFIG) -> APIRouter:
    """画面に要る口のルートを作る。web.api の build_router が include する。"""
    router = APIRouter()
    vault = services.vault
    cases = list_cases()
    jobs = list_jobs(exclude_template_ids={services.attack.config.employer_template_id})

    @router.get("/v1/session")
    async def session_info(request: Request, response: Response) -> dict[str, Any]:
        """クッキーの依頼者 ID(なければ null)と、面談を送信済みか。ID を発行しない(発行は開始ページの GET /start だけ。§6.3)。"""
        response.headers["Cache-Control"] = "no-store"
        session = getattr(request.state, "principal_session", None)
        if session is None:
            return {"principal_id": None, "registered": False}
        return {"principal_id": session.principal_id, "registered": session.registered}

    @router.get("/v1/interview/notice")
    async def interview_notice() -> dict[str, Any]:
        return services.interview.notice()  # テストが差し替えられるよう、リクエストごとに取り出す

    @router.get("/v1/jobs")
    async def job_list() -> dict[str, Any]:
        return {"jobs": jobs}

    @router.get("/v1/demo/cases")
    async def demo_cases() -> dict[str, Any]:
        return {"cases": cases}

    @router.get("/v1/demo/replays/{case}")
    async def demo_replay(case: str) -> FileResponse:
        """リプレイの記録(JSONL)をそのまま返す。case は 1〜3 の数字だけ(ほかは、存在しないものと同じ 404)。"""
        if case not in {str(number) for number in CASE_NUMBERS}:
            raise HTTPException(status_code=404, detail="not_found")
        path = REPLAYS_DIRECTORY / f"case{case}.jsonl"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="not_found")
        return FileResponse(path, media_type="application/x-ndjson", headers={"Cache-Control": "no-cache"})

    async def authorize_own_stream(request: Request, nid: str) -> str:
        """本人の経路の確認: web.api の require_session・require_own_negotiation と同じ(ミドルウェアを通らないので、ここで行う)。依頼者 ID を返す。"""
        principal_id = services.codec.read(request.cookies.get(SESSION_COOKIE_NAME), services.clock.now())
        if principal_id is None:
            raise HTTPException(status_code=401, detail="no_session")
        meta = await services.meta.get(principal_id)
        if meta is not None and meta.deletion_state == DELETION_DELETING:
            raise HTTPException(status_code=409, detail="principal_deleting")
        summaries = await vault.list_principal_negotiations(principal_id)
        if all(summary.nid != nid for summary in summaries):
            raise HTTPException(status_code=403, detail="forbidden")  # 他人の交渉も、存在しない交渉も、同じ 403
        return principal_id

    async def ensure_principal_still_active(principal_id: str) -> None:
        """各 poll の前の確かめ直し(台帳 X-83): 利用記録を読み直す。読むだけで、依頼者のロックは取らない(接続の間、ほかの操作を止めない)。

        削除中(deletion_state=deleting)・削除済み(利用記録がない)なら StreamNotAllowed。利用記録を読めなければ、その例外のまま伝える
        (閉じる側に倒す。activity_event_stream が stream_failed で閉じる)。
        """
        meta = await services.meta.get(principal_id)
        if meta is None:
            raise StreamNotAllowed("principal_deleted")
        if meta.deletion_state == DELETION_DELETING:
            raise StreamNotAllowed("principal_deleting")

    async def read_demo_events(nid: str, side: Side, after_seq: int) -> list[EventViewItem]:
        """web.activity_api の read_demo_events と同じ 2 段の確認(web の段の状態〔補助〕と、金庫のデモ用の読み出しの口〔正本〕。台帳 X-38)。"""
        if not await services.stages.is_fictional_negotiation(nid):
            raise HTTPException(status_code=403, detail="forbidden")
        try:
            return await vault.get_demo_events(nid, side, after_seq)
        except VaultNotFoundError:
            raise HTTPException(status_code=403, detail="forbidden") from None

    @router.get(f"{STREAM_PATH_PREFIX}negotiations/{{nid}}/activity")
    async def stream_own_activity(
        nid: str, request: Request, after_seq: int = Query(default=0, ge=0, le=MAX_SEQ)
    ) -> EventSourceResponse:
        """本人の活動ログの SSE(本人の側 = 候補者側だけ)。権限は /v1/negotiations/{nid}/activity と同じ(401・409・403)。

        つながった後は、各 poll の前に、本人の削除が始まっていないかを読み直す(台帳 X-83)。
        同時本数の上限(全体・クライアント IP ごと)を超えていれば、権限の確認の前に 429(台帳 C-65)。
        """
        slot = services.stream_limiter.acquire_for_request(request)  # テストが差し替えられるよう、リクエストごとに取り出す
        try:
            principal_id = await authorize_own_stream(request, nid)

            async def read(position: int) -> ActivityLog:
                await ensure_principal_still_active(principal_id)  # 削除中・削除済みなら、金庫を読まずに閉じる
                return to_activity_log("candidate", await vault.get_events(nid, "candidate", position), position)

            return _SlotEventSourceResponse(
                activity_event_stream(read, resume_position(request, after_seq), config=stream), slot=slot
            )
        except BaseException:
            slot.release()  # 確認の失敗(401・409・403 など)でも、席を残さない。つないだ後は、応答が終わるときに戻す
            raise

    @router.get(f"{STREAM_PATH_PREFIX}demo/negotiations/{{nid}}/activity")
    async def stream_demo_activity(
        nid: str, side: Side, request: Request, after_seq: int = Query(default=0, ge=0, le=MAX_SEQ)
    ) -> EventSourceResponse:
        """デモ・攻撃の活動ログの SSE(指定した側)。権限は /v1/demo/negotiations/{nid}/activity と同じ(403)。

        同時本数の上限(全体・クライアント IP ごと)を超えていれば、確認の前に 429(台帳 C-65)。
        """
        slot = services.stream_limiter.acquire_for_request(request)
        try:
            await read_demo_events(nid, side, MAX_SEQ)  # 始めに 1 回、確認だけ行う(拒否は HTTP の状態で返す)

            async def read(position: int) -> ActivityLog:
                return to_activity_log(side, await read_demo_events(nid, side, position), position)

            return _SlotEventSourceResponse(
                activity_event_stream(read, resume_position(request, after_seq), config=stream), slot=slot
            )
        except BaseException:
            slot.release()  # 確認の失敗(403 など)でも、席を残さない
            raise

    return router
