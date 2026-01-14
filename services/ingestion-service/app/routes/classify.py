from fastapi import APIRouter, HTTPException, UploadFile, File
from typing import List, Optional
from pydantic import BaseModel
import json
import tempfile
from pathlib import Path

from app.account_classifier.predictor_claude import ClaudePredictor
from app.account_classifier.mf_format import AccountPrediction

router = APIRouter(prefix="/api/classify", tags=["勘定科目識別"])

# グローバル予測器インスタンス(起動時に初期化)
predictors = {}

@router.on_event("startup")
async def load_predictors():
    """アプリ起動時に予測器を初期化"""
    try:
        predictors["claude"] = ClaudePredictor()
    except Exception as e:
        print(f"Warning: Failed to load some predictors: {e}")


class TransactionData(BaseModel):
    """取引データ"""
    vendor: str
    description: str
    amount: float
    direction: str  # "income" or "expense"


class ClassifyRequest(BaseModel):
    """勘定科目識別リクエスト"""
    transactions: List[TransactionData]
    predictor: str = "claude"  # "claude"


class ClassifyResponse(BaseModel):
    """勘定科目識別レスポンス"""
    account: str
    confidence: float
    vendor: str
    description: str


@router.post("/predict", response_model=List[ClassifyResponse])
async def classify_transactions(request: ClassifyRequest):
    """
    取引データから勘定科目を識別
    
    Example:
```json
    {
      "transactions": [
        {
          "vendor": "東京電力",
          "description": "電気代",
          "amount": 5000,
          "direction": "expense"
        }
      ],
      "predictor": "claude"
    }
```
    """
    if request.predictor not in predictors:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid predictor: {request.predictor}"
        )
    
    predictor = predictors[request.predictor]
    results = []
    
    for tx in request.transactions:
        try:
            pred = predictor.predict(
                vendor=tx.vendor,
                description=tx.description,
                amount=tx.amount,
                direction=tx.direction
            )
            
            results.append(ClassifyResponse(
                account=pred.account,
                confidence=pred.confidence,
                vendor=tx.vendor,
                description=tx.description
            ))
        except Exception as e:
            # エラー時はフォールバック
            results.append(ClassifyResponse(
                account="雑費" if tx.direction == "expense" else "売上高",
                confidence=0.0,
                vendor=tx.vendor,
                description=tx.description
            ))
    
    return results


@router.post("/batch-process")
async def batch_process_file(
    file: UploadFile = File(...),
    predictor: str = "claude"
):
    """
    JSONLファイルをアップロードしてバッチ処理
    
    Returns: MFフォーマットのCSVファイル
    """
    from app.account_classifier.pipeline import run_pipeline
    from app.account_classifier.mf_format import MFTemplate
    
    # 一時ファイルに保存
    with tempfile.NamedTemporaryFile(
        delete=False, 
        suffix=".jsonl"
    ) as tmp_input:
        content = await file.read()
        tmp_input.write(content)
        tmp_input_path = Path(tmp_input.name)
    
    # 出力ファイル
    tmp_output_path = tmp_input_path.with_suffix(".csv")
    
    # MFテンプレート(デフォルト)
    tmp_template_path = Path("config/mf_template_default.csv")
    
    try:
        # パイプライン実行
        run_pipeline(
            ocr_jsonl_path=tmp_input_path,
            mf_template_path=tmp_template_path,
            out_csv_path=tmp_output_path,
            predictor=predictor
        )
        
        # 結果を返す
        with tmp_output_path.open("r", encoding="cp932") as f:
            csv_content = f.read()
        
        return {
            "success": True,
            "csv_content": csv_content,
            "message": f"Processed {file.filename} successfully"
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
        
    finally:
        # クリーンアップ
        tmp_input_path.unlink(missing_ok=True)
        tmp_output_path.unlink(missing_ok=True)