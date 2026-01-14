'use client'

import { useState } from 'react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

interface Transaction {
    vendor: string
    description: string
    amount: number
    direction: 'income' | 'expense'
}

interface ClassifyResult {
    account: string
    confidence: number
    vendor: string
    description: string
}

export default function ClassifyPage() {
    const [transactions, setTransactions] = useState<Transaction[]>([
        { vendor: '', description: '', amount: 0, direction: 'expense' }
    ])
    const [predictor, setPredictor] = useState('claude')
    const [results, setResults] = useState<ClassifyResult[]>([])
    const [loading, setLoading] = useState(false)

    const addTransaction = () => {
        setTransactions([...transactions, { vendor: '', description: '', amount: 0, direction: 'expense' }])
    }

    const updateTransaction = (index: number, field: keyof Transaction, value: any) => {
        const updated = [...transactions]
        updated[index] = { ...updated[index], [field]: value }
        setTransactions(updated)
    }

    const classify = async () => {
        setLoading(true)
        try {
            const response = await fetch('/api/classify/predict', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ transactions, predictor })
            })

            if (!response.ok) throw new Error('Classification failed')

            const data = await response.json()
            setResults(data)
        } catch (error) {
            console.error('Error:', error)
            alert('分類に失敗しました')
        } finally {
            setLoading(false)
        }
    }

    return (
        <div className="container mx-auto p-6">
            <h1 className="text-3xl font-bold mb-6">勘定科目自動識別</h1>

            <Card className="mb-6">
                <CardHeader>
                    <CardTitle>取引情報入力</CardTitle>
                    <CardDescription>
                        取引情報を入力して、AIで勘定科目を自動識別します
                    </CardDescription>
                </CardHeader>
                <CardContent>
                    <div className="mb-4">
                        <label className="block mb-2">予測器選択</label>
                        <Select value={predictor} onValueChange={setPredictor}>
                            <SelectTrigger>
                                <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                                <SelectItem value="claude">Claude 3.5 Sonnet</SelectItem>
                                <SelectItem value="openai">OpenAI GPT-4o</SelectItem>
                                <SelectItem value="rule">ルールベース</SelectItem>
                            </SelectContent>
                        </Select>
                    </div>

                    {transactions.map((tx, index) => (
                        <div key={index} className="grid grid-cols-5 gap-4 mb-4 p-4 border rounded">
                            <Input
                                placeholder="取引先"
                                value={tx.vendor}
                                onChange={(e) => updateTransaction(index, 'vendor', e.target.value)}
                            />
                            <Input
                                placeholder="摘要"
                                value={tx.description}
                                onChange={(e) => updateTransaction(index, 'description', e.target.value)}
                            />
                            <Input
                                type="number"
                                placeholder="金額"
                                value={tx.amount}
                                onChange={(e) => updateTransaction(index, 'amount', parseFloat(e.target.value))}
                            />
                            <Select
                                value={tx.direction}
                                onValueChange={(value: 'income' | 'expense') => updateTransaction(index, 'direction', value)}
                            >
                                <SelectTrigger>
                                    <SelectValue />
                                </SelectTrigger>
                                <SelectContent>
                                    <SelectItem value="expense">支出</SelectItem>
                                    <SelectItem value="income">収入</SelectItem>
                                </SelectContent>
                            </Select>
                        </div>
                    ))}

                    <div className="flex gap-2">
                        <Button onClick={addTransaction} variant="outline">
                            + 取引追加
                        </Button>
                        <Button onClick={classify} disabled={loading}>
                            {loading ? '識別中...' : '勘定科目を識別'}
                        </Button>
                    </div>
                </CardContent>
            </Card>

            {results.length > 0 && (
                <Card>
                    <CardHeader>
                        <CardTitle>識別結果</CardTitle>
                    </CardHeader>
                    <CardContent>
                        <table className="w-full">
                            <thead>
                                <tr className="border-b">
                                    <th className="text-left p-2">取引先</th>
                                    <th className="text-left p-2">摘要</th>
                                    <th className="text-left p-2">勘定科目</th>
                                    <th className="text-left p-2">信頼度</th>
                                </tr>
                            </thead>
                            <tbody>
                                {results.map((result, index) => (
                                    <tr key={index} className="border-b">
                                        <td className="p-2">{result.vendor}</td>
                                        <td className="p-2">{result.description}</td>
                                        <td className="p-2 font-semibold">{result.account}</td>
                                        <td className="p-2">
                                            <span className={`px-2 py-1 rounded ${result.confidence > 0.8 ? 'bg-green-100 text-green-800' :
                                                    result.confidence > 0.5 ? 'bg-yellow-100 text-yellow-800' :
                                                        'bg-red-100 text-red-800'
                                                }`}>
                                                {(result.confidence * 100).toFixed(0)}%
                                            </span>
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </CardContent>
                </Card>
            )}
        </div>
    )
}