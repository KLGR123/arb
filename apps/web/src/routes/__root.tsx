import { createRootRoute, HeadContent, Outlet, Scripts } from '@tanstack/react-router'
import styles from '../styles.css?url'

export const Route = createRootRoute({
  head: () => ({
    meta: [
      { charSet: 'utf-8' },
      { name: 'viewport', content: 'width=device-width, initial-scale=1' },
      { title: 'arb · Research foundation' },
      { name: 'description', content: 'arb research foundation with an offline Vela chart.' },
    ],
    links: [{ rel: 'stylesheet', href: styles }],
  }),
  component: Root,
  notFoundComponent: () => <main className="p-10">页面不存在。<a href="/" className="underline">返回示例</a></main>,
})

function Root() {
  return (
    <html lang="zh-CN" className="dark">
      <head><HeadContent /></head>
      <body><Outlet /><Scripts /></body>
    </html>
  )
}
