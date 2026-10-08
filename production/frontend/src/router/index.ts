import { createRouter, createWebHashHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'

// 七入口顺序与 PRD 第 8 章一致，前四项为制作闭环，后三项展示真实规划/开发状态。
// 显式标注类型：不标注时 TS 会把数组推成联合类型，重定向记录（只有 path+redirect）
// 与命名路由记录对不上，`vue-tsc`（package.json 的 typecheck 脚本）会直接报错。
const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/avatar' },
  { path: '/avatar', name: 'avatar', component: () => import('@/views/AvatarView.vue'), meta: { index: 1, title: '人物形象' } },
  { path: '/script', name: 'script', component: () => import('@/views/ScriptView.vue'), meta: { index: 2, title: '口播稿生成' } },
  { path: '/human-review/:workId/:scriptId', name: 'human-review', component: () => import('@/views/HumanReviewView.vue'), meta: { title: '分镜剧本人工审核', standalone: true } },
  { path: '/tts', name: 'tts', component: () => import('@/views/TtsView.vue'), meta: { index: 3, title: 'TTS 语音' } },
  { path: '/video', name: 'video', component: () => import('@/views/VideoView.vue'), meta: { index: 4, title: '视频合成' } },
  { path: '/publish', name: 'publish', component: () => import('@/views/PublishView.vue'), meta: { index: 5, title: '发布' } },
  { path: '/metrics', name: 'metrics', component: () => import('@/views/MetricsView.vue'), meta: { index: 6, title: '指标监控' } },
  { path: '/models', name: 'models', component: () => import('@/views/ModelsView.vue'), meta: { index: 7, title: '领域模型选择' } },
]

export default createRouter({
  history: createWebHashHistory(),
  routes,
})
