"""金庫の app は、どちらの版(Cloud Run 版・TEE 版)でも /docs・/redoc・/openapi.json を出さない(内部 API の全容を見せない)。

Firestore に触れない(VaultStore は、この経路では使われない)ので、エミュレータなしで動く。
"""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from vault.app import create_app


def _verifier():
    return None


@pytest.mark.parametrize("caller_verifier", [None, _verifier], ids=["cloud_run", "tee"])
@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_vault_app_does_not_serve_api_docs(caller_verifier, path):
    app = create_app(MagicMock(), caller_verifier=caller_verifier)

    response = TestClient(app).get(path)

    assert response.status_code in {401, 403, 404}
    assert "openapi" not in response.text.lower() or response.status_code != 200
