from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from mf233.mf_format import MFTemplate, build_mf_row
from mf233.predictor_rule import RuleAccountPredictor

logger = logging.getLogger(__name__)


@dataclass
class OCRItem:
    """OCRで読み取った取引データ"""
    date: str
    amount: float
    direction: str  # "expense" | "income"
    vendor: str
    description: str

    def __post_init__(self):
        if self.direction not in ("expense", "income"):
            raise ValueError(f"Invalid direction: {self.direction}. Must be 'expense' or 'income'")
        if self.amount < 0:
            raise ValueError(f"Amount must be non-negative, got {self.amount}")


def _guess_direction(raw: Dict[str, Any]) -> str:
    """取引方向を推測(明示的な指定 > 金額の符号)"""
    direction = (raw.get("direction") or "").strip().lower()
    if direction in ("expense", "income"):
        return direction
    
    amt = raw.get("amount", 0)
    try:
        amt_f = float(amt)
        return "expense" if amt_f < 0 else "income"
    except (ValueError, TypeError):
        logger.warning(f"Could not parse amount '{amt}', defaulting to expense")
        return "expense"


def load_ocr_jsonl(path: Path) -> List[OCRItem]:
    """JSONL形式のOCRデータを読み込み"""
    if not path.exists():
        raise FileNotFoundError(f"OCR JSONL file not found: {path}")

    items: List[OCRItem] = []
    errors: List[str] = []
    
    try:
        with path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as e:
                    error_msg = f"Line {line_no}: JSON parse error - {e}"
                    errors.append(error_msg)
                    logger.error(error_msg)
                    continue

                # 必須フィールドのチェック
                date = str(raw.get("date", "")).strip()
                if not date:
                    error_msg = f"Line {line_no}: Missing 'date' field"
                    errors.append(error_msg)
                    logger.error(error_msg)
                    continue

                # 金額の解析
                amt = raw.get("amount", 0)
                try:
                    amount = abs(float(amt))  # 絶対値に正規化
                except (ValueError, TypeError) as e:
                    error_msg = f"Line {line_no}: Invalid amount '{amt}' - {e}"
                    errors.append(error_msg)
                    logger.error(error_msg)
                    continue

                direction = _guess_direction(raw)
                vendor = str(raw.get("vendor", "")).strip()
                description = str(raw.get("description", "")).strip()

                try:
                    item = OCRItem(
                        date=date,
                        amount=amount,
                        direction=direction,
                        vendor=vendor,
                        description=description
                    )
                    items.append(item)
                except ValueError as e:
                    error_msg = f"Line {line_no}: Validation error - {e}"
                    errors.append(error_msg)
                    logger.error(error_msg)
                    continue

    except Exception as e:
        raise RuntimeError(f"Failed to read OCR JSONL file: {e}") from e

    if errors:
        logger.warning(f"Loaded {len(items)} items with {len(errors)} errors")
    else:
        logger.info(f"Successfully loaded {len(items)} OCR items from {path}")

    if not items and errors:
        raise ValueError(f"No valid OCR items loaded. Total errors: {len(errors)}")

    return items


def run_pipeline(
    ocr_jsonl_path: Path,
    mf_template_path: Path,
    out_csv_path: Path,
    predictor: str = "rule",
    classifier_dir: Optional[Path] = None,
    openai_config: Optional[Path] = None,
    template_encoding: str = "cp932",
    out_encoding: str = "cp932",
) -> None:
    """メインパイプライン実行"""
    logger.info("=" * 60)
    logger.info("Starting MF CSV generation pipeline")
    logger.info(f"OCR input: {ocr_jsonl_path}")
    logger.info(f"Template: {mf_template_path}")
    logger.info(f"Output: {out_csv_path}")
    logger.info(f"Predictor: {predictor}")
    logger.info("=" * 60)

    # 1. テンプレート読み込み
    try:
        template = MFTemplate.from_csv_template(mf_template_path, encoding=template_encoding)
    except Exception as e:
        logger.error(f"Failed to load MF template: {e}")
        raise

    # 2. OCRデータ読み込み
    try:
        ocr_items = load_ocr_jsonl(ocr_jsonl_path)
    except Exception as e:
        logger.error(f"Failed to load OCR data: {e}")
        raise

    # 3. 勘定科目予測器の初期化
    try:
        if predictor == "rule":
            account_predictor = RuleAccountPredictor()
            logger.info("Using rule-based predictor")
            
        elif predictor == "hf":
            if not classifier_dir:
                raise ValueError("--classifier_dir is required when --predictor hf")
            from mf233.predictor_hf import HFPredictor
            account_predictor = HFPredictor(classifier_dir)
            logger.info(f"Using HuggingFace predictor from {classifier_dir}")
            
        elif predictor == "openai":
            from mf233.predictor_openai import OpenAIPredictor
            
            if openai_config and openai_config.exists():
                account_predictor = OpenAIPredictor.from_config_file(openai_config)
                logger.info(f"Using OpenAI predictor with config from {openai_config}")
            else:
                account_predictor = OpenAIPredictor()
                logger.info("Using OpenAI predictor with default config")
        
        elif predictor == "claude":
            from mf233.predictor_claude import ClaudePredictor
            
            if openai_config and openai_config.exists():
                # claude_config パラメータとして使用
                account_predictor = ClaudePredictor.from_config_file(openai_config)
                logger.info(f"Using Claude predictor with config from {openai_config}")
            else:
                account_predictor = ClaudePredictor()
                logger.info("Using Claude predictor with default config")
                
        else:
            raise ValueError(
                f"Unknown predictor: {predictor}. "
                f"Choose from: 'rule', 'hf', 'openai', 'claude'"
            )
    except Exception as e:
        logger.error(f"Failed to initialize predictor: {e}")
        raise

    # 4. 各OCRアイテムを処理
    rows: List[Dict[str, str]] = []
    failed_items: List[tuple] = []

    for idx, item in enumerate(ocr_items, start=1):
        try:
            # 勘定科目予測
            pred = account_predictor.predict(
                item.vendor, 
                item.description, 
                item.amount, 
                item.direction
            )
            
            # MF行を構築
            row = build_mf_row(template, item=item, account_pred=pred)
            rows.append(row)
            
            logger.debug(
                f"[{idx}/{len(ocr_items)}] Processed: {item.vendor} -> {pred.account} "
                f"(confidence: {pred.confidence:.2f})"
            )
            
        except Exception as e:
            error_info = (idx, item, str(e))
            failed_items.append(error_info)
            logger.error(f"[{idx}/{len(ocr_items)}] Failed to process item: {e}")
            continue

    # 5. CSV出力
    if rows:
        try:
            template.write_rows(out_csv_path, rows, encoding=out_encoding)
            logger.info("=" * 60)
            logger.info(f"✓ Successfully wrote {len(rows)} rows to {out_csv_path}")
            if failed_items:
                logger.warning(f"✗ Failed to process {len(failed_items)} items")
            logger.info("=" * 60)
        except Exception as e:
            logger.error(f"Failed to write output CSV: {e}")
            raise
    else:
        raise RuntimeError("No valid rows generated. Check input data and logs.")

    # 失敗したアイテムのサマリー
    if failed_items:
        logger.warning("Failed items summary:")
        for idx, item, error in failed_items[:5]:  # 最初の5件のみ表示
            logger.warning(f"  [{idx}] {item.vendor}: {error}")
        if len(failed_items) > 5:
            logger.warning(f"  ... and {len(failed_items) - 5} more")