"""
MF Cloud 仕訳帳形式のCSV生成サービス
services/ingestion-service/app/account_classifier/mf_export_service.py
"""
import csv
import logging
from datetime import datetime
from io import StringIO
from typing import List, Dict

logger = logging.getLogger(__name__)


class MfExportService:
    """MF Cloud 会計 仕訳帳形式のCSV生成サービス"""
    
    # MF Cloud 会計の公式列定義（仕訳帳インポート形式）
    # 公式ドキュメント: https://biz.moneyforward.com/support/account/guide/import-books/ib01.html
    MF_COLUMNS = [
        "取引No",          # A列: 取引番号
        "取引日",          # B列: yyyy/MM/dd形式
        "借方勘定科目",    # C列: 費用科目 or 資産科目
        "借方補助科目",    # D列: (任意)
        "借方部門",        # E列: (任意)
        "借方取引先",      # F列: 取引先名
        "借方税区分",      # G列: 課税仕入10%等
        "借方インボイス",  # H列: 適格 or 80%控除
        "借方金額(円)",    # I列: 正の整数
        "借方税額",        # J列: 通常0
        "貸方勘定科目",    # K列: 収益科目 or 資産科目
        "貸方補助科目",    # L列: (任意)
        "貸方部門",        # M列: (任意)
        "貸方取引先",      # N列: 取引先名
        "貸方税区分",      # O列: 課税売上10%等
        "貸方インボイス",  # P列: 適格 or 80%控除
        "貸方金額(円)",    # Q列: 正の整数
        "貸方税額",        # R列: 通常0
        "摘要",            # S列: 取引の説明
        "仕訳メモ",        # T列: (任意)
        "タグ",            # U列: 複数可(|区切り)
        "MF仕訳タイプ",    # V列: インポート等
        "決算整理仕訳",    # W列: 決算整理の場合のみ記入
    ]
    
    def generate_csv(self, transactions: List[Dict]) -> str:
        """
        取引データからMF仕訳帳形式のCSVを生成
        
        Args:
            transactions: 取引データのリスト
                各要素は以下の形式:
                {
                    'date': '2024-01-15',
                    'vendor': '東京電力',
                    'description': '電気代',
                    'amount': 5000,
                    'direction': 'expense',  # or 'income'
                    'accountName': '水道光熱費',
                    'fileName': 'invoice.pdf'  # (任意)
                }
        
        Returns:
            str: Shift-JIS (cp932) エンコード可能なCSV文字列
        """
        csv_buffer = StringIO()
        writer = csv.DictWriter(
            csv_buffer,
            fieldnames=self.MF_COLUMNS,
            extrasaction='ignore'
        )
        
        # ヘッダー行を書き込み
        writer.writeheader()
        
        # データ行を書き込み
        for idx, tx in enumerate(transactions, start=1):
            row = self._convert_to_mf_format(tx, transaction_no=idx)
            writer.writerow(row)
        
        csv_content = csv_buffer.getvalue()
        csv_buffer.close()
        
        logger.info(f"Generated MF CSV with {len(transactions)} transactions")
        
        return csv_content
    
    def _convert_to_mf_format(self, transaction: Dict, transaction_no: int) -> Dict:
        """
        内部データ形式からMF仕訳帳形式に変換
        
        MF Cloud 会計の仕訳帳インポート形式に準拠
        - 支出: 借方=費用科目, 貸方=普通預金
        - 収入: 借方=普通預金, 貸方=収益科目
        """
        # 日付フォーマット変換 (yyyy/MM/dd形式)
        date_str = transaction.get('date', '')
        if isinstance(date_str, datetime):
            date_str = date_str.strftime('%Y/%m/%d')
        elif date_str:
            # YYYY-MM-DD → YYYY/MM/DD
            date_str = date_str.replace('-', '/')
        
        # 金額 (絶対値、整数)
        amount = int(abs(float(transaction.get('amount', 0))))
        
        # 取引情報
        direction = transaction.get('direction', 'expense')
        account_name = transaction.get('accountName', '')
        if not account_name:
            account_name = '雑費' if direction == 'expense' else '売上高'
        
        vendor = transaction.get('vendor', '')
        description = transaction.get('description', '')
        file_name = transaction.get('fileName', '')
        
        # 摘要にファイル名を追加（オプション）
        if file_name and description:
            full_description = f"{description} ({file_name})"
        elif file_name:
            full_description = file_name
        else:
            full_description = description
        
        # MF 仕訳帳形式に変換
        if direction == 'expense':
            # 支出取引: 借方=費用科目, 貸方=普通預金
            return {
                '取引No': str(transaction_no),
                '取引日': date_str,
                '借方勘定科目': account_name,
                '借方補助科目': '',
                '借方部門': '',
                '借方取引先': vendor,
                '借方税区分': '課税仕入10%',
                '借方インボイス': '適格',
                '借方金額(円)': str(amount),
                '借方税額': '0',
                '貸方勘定科目': '普通預金',
                '貸方補助科目': '',
                '貸方部門': '',
                '貸方取引先': '',
                '貸方税区分': '対象外',
                '貸方インボイス': '',
                '貸方金額(円)': str(amount),
                '貸方税額': '0',
                '摘要': full_description,
                '仕訳メモ': '',
                'タグ': 'AI自動仕訳',
                'MF仕訳タイプ': 'インポート',
                '決算整理仕訳': '',
            }
        else:
            # 収入取引: 借方=普通預金, 貸方=収益科目
            return {
                '取引No': str(transaction_no),
                '取引日': date_str,
                '借方勘定科目': '普通預金',
                '借方補助科目': '',
                '借方部門': '',
                '借方取引先': '',
                '借方税区分': '対象外',
                '借方インボイス': '',
                '借方金額(円)': str(amount),
                '借方税額': '0',
                '貸方勘定科目': account_name,
                '貸方補助科目': '',
                '貸方部門': '',
                '貸方取引先': vendor,
                '貸方税区分': '課税売上10%',
                '貸方インボイス': '適格',
                '貸方金額(円)': str(amount),
                '貸方税額': '0',
                '摘要': full_description,
                '仕訳メモ': '',
                'タグ': 'AI自動仕訳',
                'MF仕訳タイプ': 'インポート',
                '決算整理仕訳': '',
            }
    
    def validate_transactions(self, transactions: List[Dict]) -> List[str]:
        """
        MF導出前のバリデーション
        
        Returns:
            List[str]: エラーメッセージのリスト(空なら問題なし)
        """
        errors = []
        
        for i, tx in enumerate(transactions, 1):
            # 日付チェック
            if not tx.get('date'):
                errors.append(f"取引{i}: 日付が必要です")
            
            # 勘定科目チェック
            if not tx.get('accountName'):
                errors.append(f"取引{i}: 勘定科目が識別されていません")
            
            # 金額チェック
            amount = tx.get('amount')
            if amount is None or amount == 0:
                errors.append(f"取引{i}: 金額が無効です")
        
        return errors