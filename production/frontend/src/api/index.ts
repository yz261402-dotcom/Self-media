import { httpApi } from './http'
import { mockApi } from './mock'

// 默认走真实后端；设置 VITE_API_MODE=mock 可在无后端时演示交互（界面会显示 MOCK 标记）。
const mode = (import.meta.env.VITE_API_MODE as string) || 'http'

export const api = mode === 'mock' ? (mockApi as unknown as typeof httpApi) : httpApi
export const apiMode: 'mock' | 'http' = mode === 'mock' ? 'mock' : 'http'
export { ApiError } from './http'
