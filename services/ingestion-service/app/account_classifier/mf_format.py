from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 列名のヒント定義
COLUMN_HINTS = {
    "date": ["取引日", "日付", "発生日", "取引日付"],
    "amount": ["金額", "取引金額", "入出金額"],
    "income": ["入金", "収入"],
    "expense": ["出金", "支出"],
    "account": ["勘定科目", "科目"],
    "vendor": ["取引先", "相手先"],
    "description": ["摘要", "内容", "メモ", "備考", "摘要内容"],
    "category": ["取引区分", "区分", "収支区分"],
    "account_book": ["口座", "入出金口座", "決済口座", "口座名"],
    "tax": ["税区分", "課税区分", "税"],
}


def _find_column(header: List[str], hints: List[str]) -> Optional[str]:
    """ヘッダーからヒントに一致する列を検索"""
    for h in header:
        for key in hints:
            if key in h:
                logger.debug(f"Found column '{h}' matching hint '{key}'")
                return h
    return None


@dataclass
class AccountPrediction:
    """勘定科目の予測結果"""
    account: str
    confidence: float
    topk: Optional[List[Tuple[str, float]]] = None

    def __post_init__(self):
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"Confidence must be between 0 and 1, got {self.confidence}")


@dataclass
class MFTemplate:
    """MFクラウドのCSVテンプレート"""
    header: List[str]
    col_date: Optional[str]
    col_amount: Optional[str]
    col_income: Optional[str]
    col_expense: Optional[str]
    col_account: Optional[str]
    col_vendor: Optional[str]
    col_description: Optional[str]
    col_category: Optional[str]
    col_account_book: Optional[str]
    col_tax: Optional[str]

    @staticmethod
    def from_csv_template(path: Path, encoding: str = "cp932") -> "MFTemplate":
        """CSVテンプレートファイルから MFTemplate を作成"""
        if not path.exists():
            raise FileNotFoundError(f"Template file not found: {path}")
        
        try:
            with path.open("r", encoding=encoding, newline="") as f:
                reader = csv.reader(f)
                try:
                    header = next(reader)
                except StopIteration:
                    raise ValueError(f"Template CSV is empty: {path}")
        except UnicodeDecodeError as e:
            raise ValueError(f"Failed to decode template file with encoding {encoding}: {e}")

        # ヘッダーの正規化
        header = [h.strip() for h in header if h.strip()]
        if not header:
            raise ValueError(f"Template CSV has no valid columns: {path}")

        # 各列の自動検出
        col_date = _find_column(header, COLUMN_HINTS["date"])
        col_amount = _find_column(header, COLUMN_HINTS["amount"])
        col_income = _find_column(header, COLUMN_HINTS["income"])
        col_expense = _find_column(header, COLUMN_HINTS["expense"])
        col_account = _find_column(header, COLUMN_HINTS["account"])
        col_vendor = _find_column(header, COLUMN_HINTS["vendor"])
        col_description = _find_column(header, COLUMN_HINTS["description"])
        col_category = _find_column(header, COLUMN_HINTS["category"])
        col_account_book = _find_column(header, COLUMN_HINTS["account_book"])
        col_tax = _find_column(header, COLUMN_HINTS["tax"])

        # 必須列のチェック
        if not col_date:
            logger.warning("Date column not found in template")
        if not col_account:
            logger.warning("Account column not found in template")

        logger.info(f"Loaded template with {len(header)} columns from {path}")
        
        return MFTemplate(
            header=header,
            col_date=col_date,
            col_amount=col_amount,
            col_income=col_income,
            col_expense=col_expense,
            col_account=col_account,
            col_vendor=col_vendor,
            col_description=col_description,
            col_category=col_category,
            col_account_book=col_account_book,
            col_tax=col_tax,
        )

    def write_rows(
        self, 
        out_path: Path, 
        rows: List[Dict[str, str]], 
        encoding: str = "cp932"
    ) -> None:
        """行データをCSVファイルに書き込み"""
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            
            with out_path.open("w", encoding=encoding, newline="") as f:
                writer = csv.DictWriter(f, fieldnames=self.header, extrasaction="ignore")
                writer.writeheader()
                
                for idx, r in enumerate(rows):
                    # 全列を初期化(欠損値は空文字)
                    full = {k: "" for k in self.header}
                    full.update(r)
                    writer.writerow(full)
                    
            logger.info(f"Successfully wrote {len(rows)} rows to {out_path}")
            
        except Exception as e:
            logger.error(f"Failed to write CSV to {out_path}: {e}")
            raise


def build_mf_row(
    template: MFTemplate, 
    item,  # OCRItem
    account_pred: AccountPrediction
) -> Dict[str, str]:
    """OCRアイテムと勘定科目予測からMF CSVの1行を構築"""
    row: Dict[str, str] = {}

    # 取引日
    if template.col_date:
        row[template.col_date] = item.date

    # 取引区分(収入/支出)
    if template.col_category:
        row[template.col_category] = "収入" if item.direction == "income" else "支出"

    # 取引先
    if template.col_vendor:
        row[template.col_vendor] = item.vendor

    # 摘要
    if template.col_description:
        row[template.col_description] = item.description

    # 勘定科目
    if template.col_account:
        row[template.col_account] = account_pred.account

    # 金額処理
    amount_int = int(round(item.amount))
    
    if template.col_income or template.col_expense:
        # 入金/出金列が分かれている場合
        if item.direction == "income" and template.col_income:
            row[template.col_income] = str(amount_int)
            if template.col_expense:
                row[template.col_expense] = ""
        elif item.direction == "expense" and template.col_expense:
            row[template.col_expense] = str(amount_int)
            if template.col_income:
                row[template.col_income] = ""
    elif template.col_amount:
        # 単一の金額列(支出は負の値)
        signed = amount_int if item.direction == "income" else -amount_int
        row[template.col_amount] = str(signed)

    # 口座(デフォルトは空)
    if template.col_account_book:
        row[template.col_account_book] = ""

    # 税区分(デフォルトは空)
    if template.col_tax:
        row[template.col_tax] = ""

    logger.debug(
        f"Built row: date={item.date}, vendor={item.vendor}, "
        f"account={account_pred.account}, amount={amount_int}"
    )

    return row