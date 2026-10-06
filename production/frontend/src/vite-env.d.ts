/// <reference types="vite/client" />

// Vite 的环境变量与客户端资源类型。缺这个文件时 `import.meta.env` 会报
// "Property 'env' does not exist on type 'ImportMeta'"，`npm run typecheck` 因此一直带噪。
interface ImportMetaEnv {
  readonly VITE_API_MODE?: 'http' | 'mock'
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
