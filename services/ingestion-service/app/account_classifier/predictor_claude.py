"""
Claude 3.5 Sonnetを使った勘定科目予測器
services/ingestion-service/app/account_classifier/predictor_claude.py
"""
import json
import logging
import os
from dataclasses import dataclass
from typing import Optional, List, Tuple

logger = logging.getLogger(__name__)


@dataclass
class AccountPrediction:
    """勘定科目の予測結果"""
    account: str
    confidence: float
    reasoning: Optional[str] = None


@dataclass
class ClaudePredictor:
    """Claude 3.5 Sonnetを使った勘定科目予測器"""
    
    api_key: Optional[str] = None
    model: str = "claude-3-5-sonnet-20241022"
    max_tokens: int = 500
    temperature: float = 0.0
    
    def __post_init__(self):
        # API キーの取得
        if self.api_key is None:
            self.api_key = os.getenv("ANTHROPIC_API_KEY")
        
        if not self.api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY is required. "
                "Set it in environment variables or pass as parameter."
            )
        
        try:
            from anthropic import Anthropic
        except ImportError as e:
            raise RuntimeError(
                "Anthropic library is required. "
                "Install with: pip install anthropic"
            ) from e
        
        self.client = Anthropic(api_key=self.api_key)
        logger.info(f"Claude predictor initialized with model: {self.model}")
    
    def predict(
        self,
        vendor: str,
        description: str,
        amount: float,
        direction: str
    ) -> AccountPrediction:
        """
        勘定科目を予測
        
        Args:
            vendor: 取引先名
            description: 摘要・内容
            amount: 金額
            direction: 取引方向 ("income" or "expense")
        
        Returns:
            AccountPrediction: 予測結果
        """
        system_prompt = self._build_system_prompt()
        user_prompt = self._build_user_prompt(vendor, description, amount, direction)
        
        try:
            logger.debug(f"Predicting account for: {vendor} - {description}")
            
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}]
            )
            
            content = response.content[0].text.strip()
            
            # JSON部分を抽出
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
            
            result = json.loads(content)
            
            account = result.get("account", "")
            confidence = float(result.get("confidence", 0.5))
            reasoning = result.get("reasoning", "")
            
            # 勘定科目の検証（許可リストに含まれているか）
            if not self._is_valid_account(account):
                logger.warning(f"Invalid account returned: {account}")
                account = self._get_fallback_account(direction)
                confidence = 0.3
            
            logger.info(
                f"Predicted: {account} (conf: {confidence:.2f}) - {reasoning}"
            )
            
            return AccountPrediction(
                account=account,
                confidence=confidence,
                reasoning=reasoning
            )
            
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Claude response: {e}")
            return self._fallback_prediction(direction)
            
        except Exception as e:
            logger.error(f"Prediction failed: {e}")
            return self._fallback_prediction(direction)
    
    def _build_system_prompt(self) -> str:
        """システムプロンプトを構築"""
        return """あなたは日本の会計実務に精通した経理AIアシスタントです。
取引情報から適切な勘定科目を正確に識別することがあなたの役割です。

# 利用可能な勘定科目（必ずこのリストから選択）
- 水道光熱費
- 通信費
- 地代家賃
- 旅費交通費
- 広告宣伝費
- 消耗品費
- 会議費
- 接待交際費
- 給料賃金
- 福利厚生費
- 支払手数料
- 租税公課
- 減価償却費
- 修繕費
- 保険料
- 荷造運賃
- 外注費
- 研修費
- 採用費
- 雑費
- 売上高
- 受取利息
- 受取配当金
- 雑収入

# 判定基準
- 取引先名、摘要、金額、取引方向を総合的に判断
- 日本の会計基準と一般的な商習慣に従う
- 判断根拠を簡潔に説明
- 信頼度(0.0-1.0)は慎重に評価

# 特別な考慮事項
- 水道・ガス・電気 → 水道光熱費
- 携帯電話・インターネット → 通信費
- タクシー・電車・新幹線 → 旅費交通費
- Google/Meta等の広告 → 広告宣伝費
- 文房具・PC周辺機器 → 消耗品費
- カフェでの打ち合わせ → 会議費
- 高額な取引先接待 → 接待交際費

# 回答形式（必ずこの形式で）
{
  "account": "勘定科目名",
  "confidence": 0.95,
  "reasoning": "判断理由(1-2文で簡潔に)"
}"""
    
    def _build_user_prompt(
        self,
        vendor: str,
        description: str,
        amount: float,
        direction: str
    ) -> str:
        """ユーザープロンプトを構築"""
        direction_ja = "収入" if direction == "income" else "支出"
        
        return f"""以下の取引の勘定科目を判定してください。

【取引情報】
取引先: {vendor if vendor else "（記載なし）"}
摘要: {description if description else "（記載なし）"}
金額: ¥{amount:,.0f}
取引方向: {direction_ja}

上記の情報から最も適切な勘定科目を判定し、JSON形式で回答してください。"""
    
    def _is_valid_account(self, account: str) -> bool:
        """勘定科目が許可リストに含まれているか検証"""
        valid_accounts = {
            "水道光熱費", "通信費", "地代家賃", "旅費交通費", "広告宣伝費",
            "消耗品費", "会議費", "接待交際費", "給料賃金", "福利厚生費",
            "支払手数料", "租税公課", "減価償却費", "修繕費", "保険料",
            "荷造運賃", "外注費", "研修費", "採用費", "雑費",
            "売上高", "受取利息", "受取配当金", "雑収入"
        }
        return account in valid_accounts
    
    def _get_fallback_account(self, direction: str) -> str:
        """フォールバック勘定科目を取得"""
        return "売上高" if direction == "income" else "雑費"
    
    def _fallback_prediction(self, direction: str) -> AccountPrediction:
        """フォールバック予測（エラー時）"""
        account = self._get_fallback_account(direction)
        logger.warning(f"Using fallback prediction: {account}")
        return AccountPrediction(
            account=account,
            confidence=0.3,
            reasoning="自動判定に失敗したため、デフォルト科目を使用"
        )