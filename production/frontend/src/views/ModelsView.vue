<script setup lang="ts">
import { computed } from 'vue'
import { useStudioStore } from '@/stores/studio'
const store = useStudioStore()
const providers = computed(() => store.capabilities?.providers ?? [])
const purpose: Record<string, string> = {
  deepseek: '口播稿', 'gpt-image-2': '人物图片（GPT Image 2.5 Sunburst）', minimax: '语音配音',
  m2_worker: '本地数字人口播', volcengine: '云端数字人口播',
}
const stateLabel: Record<string, string> = {
  usable: '已有实测记录', ready: '已有实测记录', unverified: '待验证',
  not_configured: '未配置', invalid: '配置异常', auth_failed: '认证失败',
  quota_exhausted: '额度不足', model_not_found: '模型不存在',
  capability_unsupported: '不支持此能力', network_error: '连接异常',
}
</script>

<template>
  <div class="planned-page">
    <div class="page-head"><h1>模型选择</h1><span class="tag">配置编辑开发中</span></div>
    <p class="page-sub">查看当前模型配置。暂时需要在后端配置文件中修改模型名称、地址和密钥。</p>
    <section class="section">
      <div class="section-head"><h2 class="section-title">当前配置</h2></div>
      <table><thead><tr><th>用途</th><th>模型名称</th><th>状态</th><th>说明</th></tr></thead>
        <tbody><tr v-for="provider in providers" :key="provider.provider">
          <td>{{ purpose[provider.provider] ?? provider.purpose }}</td>
          <td>{{ provider.model }}<div class="faint">{{ provider.provider }}</div></td>
          <td>{{ stateLabel[provider.validation_state] ?? '待验证' }}</td>
          <td><details><summary>查看详情</summary><p class="muted">{{ provider.detail }}</p></details></td>
        </tr></tbody>
      </table>
      <p v-if="!providers.length" class="empty">暂时没有读到模型配置，请检查后端连接。</p>
      <p class="section-footnote">实测记录反映此前的调用结果，当前可用性以本次任务返回结果为准。</p>
    </section>
    <details class="advanced"><summary>现在如何修改配置</summary>
      <p class="muted">在 production/backend/.env 中修改对应服务的配置，然后重启后端。密钥只保存在服务端，页面不会展示。</p>
    </details>
  </div>
</template>
