<script setup lang="ts">
const metrics = ['播放量', '完播率', '点赞量', '收藏量']
</script>

<template>
  <div class="planned-page">
    <div class="page-head"><h1>指标监控</h1><span class="tag">开发中</span></div>
    <p class="page-sub">账号数据尚未接入。接入后，可按作品查看发布效果。</p>
    <section class="section">
      <div class="section-head"><h2 class="section-title">计划支持的指标</h2></div>
      <table><thead><tr><th>指标</th><th>当前数据</th></tr></thead>
        <tbody><tr v-for="metric in metrics" :key="metric"><td>{{ metric }}</td><td class="muted">尚未获取</td></tr></tbody>
      </table>
      <p class="section-footnote">各项数据以平台实际开放范围为准。</p>
    </section>
  </div>
</template>
