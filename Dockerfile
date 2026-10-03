# アプリのイメージ(Cloud Run 用。web・agents・vault〔Cloud Run 版〕と、スパイクの検証クライアント scripts/tee_probe_client.py が使う。
# research/tee-spike-contract.md §13)。土台は Dockerfile.vault と同じ。TEE 版の金庫は、専用の Dockerfile.vault で作る。
# サービスごとの起動コマンドは、デプロイのときに指定する(ここに書いたのは web の既定)。
# ビルドは Cloud Build で行う(gcloud builds submit --tag)。

FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /uvx /bin/

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_PREFERENCE=only-system \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 依存の層。pyproject.toml・uv.lock・.python-version だけに依存するので、コードを変えても作り直さない。
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev

# アプリのコードと設定。deploy/ の vault-releases.json は、web とスクリプトが「許可する digest」として読む
# (金庫のイメージを作り直して表に追記したら、このイメージも作り直す)。
# 画面(static/。静的な HTML と素の JS・CSS)は、web が /static とページの経路で配る(src/web/app.py)。
COPY src ./src
COPY config ./config
COPY scripts ./scripts
COPY deploy ./deploy
COPY fixtures ./fixtures
COPY static ./static

ENV PATH=/opt/venv/bin:$PATH \
    PORT=8080

CMD ["uvicorn", "web.app:create_app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
