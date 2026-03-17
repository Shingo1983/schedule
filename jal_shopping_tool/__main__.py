"""python -m jal_shopping_tool で実行するためのエントリポイント

Usage:
  python -m jal_shopping_tool search "商品名"   # CLI検索
  python -m jal_shopping_tool web               # Webアプリ起動
  python -m jal_shopping_tool setup             # 初期設定
  python -m jal_shopping_tool shops             # ショップ一覧
"""

import sys

if len(sys.argv) > 1 and sys.argv[1] == "web":
    # "web" サブコマンドの場合はFlaskアプリを起動
    sys.argv.pop(1)  # "web" を除去
    from .app import main
    main()
else:
    from .cli import main
    main()
