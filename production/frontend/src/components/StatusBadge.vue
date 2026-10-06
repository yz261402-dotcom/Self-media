<script setup lang="ts">
const props = defineProps<{ text: string; kind?: 'ok' | 'warn' | 'danger' | 'info' | 'run' | 'plain' }>()

const MAP: Record<string, string> = {
  succeeded: 'ok', approved: 'ok', pass: 'ok', ready: 'ok', available: 'ok', supported: 'ok',
  verified: 'ok',
  running: 'run', waiting_provider: 'run', submitted: 'run', leased: 'run',
  queued: 'info', planned: 'info', not_run: 'info', intent: 'info',
  waiting_selection: 'warn', waiting_review: 'warn', review: 'warn', developing: 'warn',
  unknown: 'warn', unverified: 'warn', partially_verified: 'warn', not_configured: 'warn',
  blocked: 'danger', failed: 'danger', reject: 'danger', rejected: 'danger', fail: 'danger',
  invalidated: 'danger', invalid: 'danger',
  // 占位产物必须一眼可见：它不继承任何通过结论，也不得被当作真实产物
  mock: 'danger',
  cancelled: 'plain',
}

const LABEL: Record<string, string> = {
  queued: '排队中', running: '执行中', waiting_provider: '等待外部作业',
  waiting_review: '等待质量审批', waiting_selection: '等待用户选择', retry_wait: '延期重试',
  succeeded: '已完成', failed: '失败', cancelled: '已取消', blocked: '已阻断',
  planned: '规划中', developing: '开发中', available: '可用',
  pending: '待审', approved: '已定版', rejected: '已拒收',
  pass: '通过', fail: '未通过', review: '待复核', not_run: '未执行',
  ready: '已就绪', unsupported: '不支持',
  // T05：三种"看起来像但不能算"的状态必须各有各的说法
  intent: '仅登记意向', submitted: '已受理待查', leased: '占用 GPU 中',
  unknown: '结果未知', invalidated: '结论已失效',
  not_configured: '未配置', unverified: '未实测',
  partially_verified: '部分实测', verified: '已实测',
  mock: 'MOCK',
}

const kind = () => props.kind ?? (MAP[props.text] as string) ?? 'plain'
</script>

<template>
  <span class="st" :class="kind()">{{ LABEL[props.text] ?? props.text }}</span>
</template>
