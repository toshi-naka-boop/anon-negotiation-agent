"""金庫の内部 HTTP・JSON API を呼ぶ非同期クライアント(design.md §3.3・§4.1)。

レフェリー・見回りが使う口(view・events・moves・principal-answer・control・expire・見回りの一覧
(open=true))と、本人の操作・削除の流れが使う口(policy の PUT・GET、blocklist の PUT、本人の削除、
本人の交渉一覧、交渉の作成)を持つ。リクエスト・レスポンスの型は vault.api_models をそのまま使う
(同じ形を二重に書かない)。

サービス間の認証(ID トークン)はデプロイの段で足す。ここでは httpx.AsyncClient を受け取るだけ
なので、本番は base_url を金庫の URL にした AsyncClient を、テストは金庫の app をつないだ
AsyncClient(httpx.ASGITransport)を渡す。

金庫の応答は次の例外に変換する。レフェリー・見回りは、409 と一時的な失敗を区別して扱う。
"""

from typing import TypeVar

import httpx
from pydantic import BaseModel

from negotiation_core import Side

from vault.api_models import (
    ControlRequest,
    ControlResponse,
    CreateNegotiationRequest,
    CreateNegotiationResponse,
    EventViewItem,
    ExpireResponse,
    MoveRequest,
    MoveResponse,
    NegotiationViewResponse,
    OpenNegotiationsPage,
    OpenNegotiationSummary,
    PolicyView,
    PrincipalAnswerRequest,
    PrincipalAnswerResponse,
    PrincipalNegotiationSummary,
    PutBlocklistRequest,
    PutPolicyRequest,
)

_M = TypeVar("_M", bound=BaseModel)


class VaultClientError(Exception):
    """金庫の呼び出しが失敗した(基底クラス)。

    メッセージには、HTTP ステータスと金庫の detail だけを入れる(組み合わせの値は含まれない)。
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class VaultNotFoundError(VaultClientError):
    """404。交渉が(削除されるなどして)存在しない。"""


class VaultConflictError(VaultClientError):
    """409。手の前提が崩れた(expected_version の不一致・手番違い・一時停止中など)か、
    依頼者が削除中。レフェリーは状態を読み直してから進める(§4.1 の 3)。
    """


class VaultUnavailableError(VaultClientError):
    """503 または通信の失敗。冪等な操作は、あとで呼び直してよい(§3.3)。"""


class VaultClient:
    """金庫の API の薄い非同期ラッパー。状態は持たない。"""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        body: BaseModel | None = None,
    ):
        """リクエストを送り、失敗(4xx・5xx・通信エラー)は例外にして、成功した JSON を返す。

        本文のない成功(204。PUT・DELETE)は None を返す。
        """
        json_body = body.model_dump(mode="json", exclude_none=True) if body is not None else None
        try:
            response = await self._http.request(method, path, params=params, json=json_body)
        except httpx.TransportError as exc:
            raise VaultUnavailableError(f"vault is unreachable: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise self._error_for(response)
        if response.status_code == 204:
            return None
        return response.json()

    @staticmethod
    def _error_for(response: httpx.Response) -> VaultClientError:
        try:
            detail = str(response.json().get("detail", ""))
        except (ValueError, AttributeError):
            detail = ""
        message = f"vault returned {response.status_code}: {detail}"
        status = response.status_code
        if status == 404:
            return VaultNotFoundError(message, status)
        if status == 409:
            return VaultConflictError(message, status)
        if status == 503:
            return VaultUnavailableError(message, status)
        return VaultClientError(message, status)

    async def _request(
        self,
        method: str,
        path: str,
        response_model: type[_M],
        *,
        params: dict | None = None,
        body: BaseModel | None = None,
    ) -> _M:
        data = await self._send(method, path, params=params, body=body)
        # 金庫が返した JSON を読み戻す。negotiation_core のモデルは strict=True なので、
        # Enum(Verdict)を文字列から読み戻せるよう strict=False にする(vault.serialization と同じ扱い)。
        return response_model.model_validate(data, strict=False)

    async def get_view(self, nid: str, side: Side) -> NegotiationViewResponse:
        """GET /v1/negotiations/{nid}/view?side=。その側から見た状態。"""
        return await self._request(
            "GET", f"/v1/negotiations/{nid}/view", NegotiationViewResponse, params={"side": side}
        )

    async def get_events(self, nid: str, side: Side, after_seq: int = 0) -> list[EventViewItem]:
        """GET /v1/negotiations/{nid}/events?side=&after_seq=。イベント列のその側の見え方。"""
        data = await self._send(
            "GET", f"/v1/negotiations/{nid}/events", params={"side": side, "after_seq": after_seq}
        )
        return [EventViewItem.model_validate(item, strict=False) for item in data]

    async def post_move(self, nid: str, request: MoveRequest) -> MoveResponse:
        """POST /v1/negotiations/{nid}/moves。expected_version の不一致は VaultConflictError。"""
        return await self._request("POST", f"/v1/negotiations/{nid}/moves", MoveResponse, body=request)

    async def post_principal_answer(
        self, nid: str, request: PrincipalAnswerRequest
    ) -> PrincipalAnswerResponse:
        """POST /v1/negotiations/{nid}/principal-answer。"""
        return await self._request(
            "POST", f"/v1/negotiations/{nid}/principal-answer", PrincipalAnswerResponse, body=request
        )

    async def control(self, nid: str, request: ControlRequest) -> ControlResponse:
        """POST /v1/negotiations/{nid}/control(一時停止・再開・取消。冪等)。"""
        return await self._request("POST", f"/v1/negotiations/{nid}/control", ControlResponse, body=request)

    async def expire(self, nid: str) -> ExpireResponse:
        """POST /v1/negotiations/{nid}/expire(期限を過ぎていれば終了処理。冪等)。"""
        return await self._request("POST", f"/v1/negotiations/{nid}/expire", ExpireResponse)

    async def list_open_negotiations(self) -> list[OpenNegotiationSummary]:
        """GET /v1/negotiations?open=true&cursor=。judged でない交渉を、全ページぶん集める。"""
        items: list[OpenNegotiationSummary] = []
        cursor: str | None = None
        while True:
            params: dict = {"open": "true"}
            if cursor is not None:
                params["cursor"] = cursor
            page = await self._request("GET", "/v1/negotiations", OpenNegotiationsPage, params=params)
            items.extend(page.items)
            if page.next_cursor is None:
                return items
            cursor = page.next_cursor

    # ------------------------------------------------------------------
    # 本人の操作・削除の流れが使う口(1d-2)
    # ------------------------------------------------------------------

    async def put_policy(self, principal_id: str, request: PutPolicyRequest) -> None:
        """PUT /v1/principals/{pid}/policy(丸め済みポリシー・外した軸・属性帯を置き換える。§3.3)。"""
        await self._send("PUT", f"/v1/principals/{principal_id}/policy", body=request)

    async def get_policy(self, principal_id: str) -> PolicyView:
        """GET /v1/principals/{pid}/policy(本人向けの表示。web は保存しない。§3.3)。"""
        return await self._request("GET", f"/v1/principals/{principal_id}/policy", PolicyView)

    async def put_blocklist(self, principal_id: str, request: PutBlocklistRequest) -> None:
        """PUT /v1/principals/{pid}/blocklist(ブロック先を置き換える。§3.3)。"""
        await self._send("PUT", f"/v1/principals/{principal_id}/blocklist", body=request)

    async def delete_principal(self, principal_id: str) -> None:
        """DELETE /v1/principals/{pid}(本人のデータを消す。冪等。すでに消えていても成功。§3.8)。"""
        await self._send("DELETE", f"/v1/principals/{principal_id}")

    async def list_principal_negotiations(self, principal_id: str) -> list[PrincipalNegotiationSummary]:
        """GET /v1/principals/{pid}/negotiations(本人が当事者の交渉の一覧。終わったものを含む。§3.3)。"""
        data = await self._send("GET", f"/v1/principals/{principal_id}/negotiations")
        return [PrincipalNegotiationSummary.model_validate(item, strict=False) for item in data]

    async def create_negotiation(self, request: CreateNegotiationRequest) -> CreateNegotiationResponse:
        """POST /v1/negotiations(request_id で冪等。断られたときは status=refused と reason。§3.5)。"""
        return await self._request("POST", "/v1/negotiations", CreateNegotiationResponse, body=request)
