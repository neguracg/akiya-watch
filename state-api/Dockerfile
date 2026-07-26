# akiya-watch state-api 本番用Dockerfile。
# 純PostgreSQLのみで動作するステートレスAPI(ローカルファイルへの永続化は無いため
# data/ボリュームは持たない。12_kintai/18_SeitaiYoyaku とは異なりSQLiteフォールバックも無い)。

FROM python:3.12-slim

# psycopg2-binaryはwheel同梱のためビルドツール不要。追加のシステムパッケージは不要。
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/

# 非rootユーザーで実行する。
RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# BASE_PATHが未設定(空文字)ならヘルスチェックは /healthz、
# 設定時は {BASE_PATH}/healthz になる(docker-compose側で環境変数を渡す前提)。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request,sys; \
    base=os.environ.get('BASE_PATH',''); \
    sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:8000{base}/healthz', timeout=3).status==200 else 1)"

# --proxy-headers は付けない。誤解防止のため明記する: このAPIはクライアントIPに
# 依存"しない"わけではない――レート制限(app/main.pyの_client_ip()参照)は
# X-Forwarded-Forヘッダを自前でパースして送信元IP単位の濫用抑止に使っており、
# クライアントIPに明確に依存している。
# ここで付けないのは、Uvicornの--proxy-headers機構(XFFを信頼してrequest.client.host/
# scope["scheme"]を書き換える仕組み)をこのアプリが使っていないため。
# _client_ip()のrequest.headers.get("x-forwarded-for")は--proxy-headers設定と
# 無関係に常に生ヘッダを読めるので、有効化しても濫用抑止の挙動は変わらない。
# request.client.hostはXFF不在時のフォールバックにしか使わずCookie/リダイレクトも
# 扱わないため、書き換えを有効にする意味も無い。付けると信頼IP範囲
# (--forwarded-allow-ips)の設定・運用が増えるだけで得るものが無い。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
