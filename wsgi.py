"""Render等のクラウドサービス用エントリポイント

gunicorn（本番用Webサーバー）がこのファイルを読み込んで
Flaskアプリを起動する。

ローカルPCでの開発には影響しない（start.batは従来通り動く）。
"""

from jal_shopping_tool.app import create_app

app = create_app()
