import { useState } from 'react'
import { createFileRoute } from '@tanstack/react-router'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from '@/components/ui/card'

export const Route = createFileRoute('/example')({
  head: () => ({ meta: [{ title: '组件示例 · arb' }] }),
  component: ComponentExample,
})

function ComponentExample() {
  const [count, setCount] = useState(0)

  return (
    <main className="py-16 sm:py-24">
      <p className="mb-4 text-xs font-medium tracking-[0.2em] text-muted-foreground">COMPONENT PLAYGROUND</p>
      <h1 className="text-4xl font-semibold tracking-tight">组件示例</h1>
      <p className="mt-4 max-w-xl leading-7 text-muted-foreground">一张卡片，几种按钮，一个本地计数器。用最小的交互确认组件样式与客户端交互正常。</p>
      <Card className="mt-10 max-w-xl shadow-none">
        <CardHeader>
          <Badge variant="secondary" className="mb-4">shadcn/ui</Badge>
          <CardTitle>让界面动起来</CardTitle>
          <CardDescription>点击按钮，试试 React 的本地状态更新。刷新页面后归零。</CardDescription>
        </CardHeader>
        <CardContent>
          <p role="status" aria-live="polite" aria-atomic="true" className="py-6 text-6xl font-medium tabular-nums">
            <span className="sr-only">当前计数：</span>{count}
          </p>
        </CardContent>
        <CardFooter className="flex flex-wrap gap-3">
          <Button onClick={() => setCount((value) => value + 1)}>增加计数</Button>
          <Button variant="outline" onClick={() => setCount(0)} disabled={count === 0}>重置</Button>
        </CardFooter>
      </Card>
    </main>
  )
}
