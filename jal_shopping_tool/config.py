"""設定管理モジュール"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "jal-shopping-tool"
CONFIG_FILE = CONFIG_DIR / "config.json"
CACHE_DIR = CONFIG_DIR / "cache"

# JALマイレージパーク LSP対象ショップPDFのURL
JAL_LSP_PDF_URL = (
    "https://www.jal.co.jp/jp/ja/jmb/lsp/service/pdf_sites/"
    "JALLifeStatus_shop_250115.pdf"
)

# JALマイレージパーク ショップ詳細ページベースURL
JAL_SHOP_BASE_URL = "https://partner.jal.co.jp/shop/?tp={shop_id}"

# LSPレート: 獲得マイル100マイルにつき1 LSP
MILES_PER_LSP = 100


@dataclass
class Config:
    rakuten_app_id: str = ""
    yahoo_app_id: str = ""
    jal_lsp_pdf_url: str = JAL_LSP_PDF_URL
    # 1 LSPあたりの価値（円）。ユーザーが設定する主観的な価値
    lsp_value_yen: float = 15.0
    # ショップ別ポイント還元率（%ではなく小数: 7.5% → 0.075）
    yahoo_paypay_rate: float = 0.075  # Yahoo!ショッピング PayPay還元率
    rakuten_point_rate: float = 0.01  # 楽天市場 ポイント還元率（SPU等で変動）

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "rakuten_app_id": self.rakuten_app_id,
                    "yahoo_app_id": self.yahoo_app_id,
                    "jal_lsp_pdf_url": self.jal_lsp_pdf_url,
                    "lsp_value_yen": self.lsp_value_yen,
                    "yahoo_paypay_rate": self.yahoo_paypay_rate,
                    "rakuten_point_rate": self.rakuten_point_rate,
                },
                f,
                indent=2,
                ensure_ascii=False,
            )

    @classmethod
    def load(cls) -> "Config":
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        return cls()
