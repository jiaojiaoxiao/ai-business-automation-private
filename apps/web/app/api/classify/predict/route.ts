import { NextResponse } from 'next/server'

export async function POST(request: Request) {
    try {
        const body = await request.json()

        // バックエンドのFastAPIにプロキシ
        const response = await fetch(
            `${process.env.NEXT_PUBLIC_API_BASE_URL}/api/classify/predict`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body)
            }
        )

        if (!response.ok) {
            throw new Error('Classification failed')
        }

        const data = await response.json()
        return NextResponse.json(data)

    } catch (error) {
        return NextResponse.json(
            { error: 'Classification failed' },
            { status: 500 }
        )
    }
}