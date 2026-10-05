"""冪等キーの口(金庫の `GET /v1/negotiations/by-request/{request_id}`)の URL が、アクセスログで伏せられること。

design.md §3.3(by-request)と §3.8・台帳 X-40(ログに ID を残さない)。冪等キーは依頼者 ID を含み得るので、
16 桁の 16 進数の形をしていなくても、`by-request/` の後ろをまとめて伏せる(台帳 X-57 の実装で見つかった衝突)。
"""

import logging

import pytest

from negotiation_core.log_privacy import mask_ids_in_logs

_PID = "0123456789abcdef"


@pytest.mark.parametrize(
    "path",
    [
        f"/v1/negotiations/by-request/{_PID}:req-7f3a",  # web の冪等キー(依頼者 ID + 画面の乱数)
        f"/v1/negotiations/by-request/{_PID}%3Areq%2F7f3a",  # パーセントエンコード
        "/v1/negotiations/by-request/plain-key",  # ID の形をしていないキー
    ],
)
def test_request_key_in_access_log_is_masked(path: str, caplog: pytest.LogCaptureFixture) -> None:
    mask_ids_in_logs()
    logger = logging.getLogger("uvicorn.access")
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        logger.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "GET", path, "1.1", 200)
    text = caplog.text
    assert "by-request/<key>" in text
    assert "req" not in text.split("by-request/")[1].split()[0]
    assert _PID not in text
    assert "plain-key" not in text


def test_other_paths_keep_the_endpoint(caplog: pytest.LogCaptureFixture) -> None:
    mask_ids_in_logs()
    logger = logging.getLogger("uvicorn.access")
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        logger.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "GET", f"/v1/negotiations/{_PID}/view", "1.1", 200)
    assert "/v1/negotiations/<id>/view" in caplog.text
