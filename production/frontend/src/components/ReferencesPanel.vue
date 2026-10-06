<script setup lang="ts">
import { computed } from 'vue'
import { useContentStore } from '@/stores/content'
import type { ReferenceSearchItem } from '@/api/types'

const content = useContentStore()

const fmt = (n: number | null | undefined) => (n === null || n === undefined ? '未提供' : n >= 10000 ? `${(n / 10000).toFixed(1)}万` : String(n))
const importedIds = computed(() => new Set((content.state?.references ?? []).map((r) => r.platform_work_id)))
const selectedPlatformId = computed(() => content.selectedReference?.platform_work_id ?? null)
const sortNote = computed(() => {
  if (!content.searchItems.length) return ''
  if (content.sort === 'recommended') return '按平台返回顺序展示。'
  return '红狐搜索接口不支持服务端排序：以下只在已取回的这一批内排序，翻页后不会重排前面的结果。'
})
const busy = computed(() => content.busy)

function search(more = false) { void content.search(more) }
function open(item: ReferenceSearchItem) { window.open(item.source_url, '_blank', 'noopener') }
</script>

<template>
  <section class="section refs" aria-label="搜索参考视频">
    <div class="section-head">
      <h2 class="section-title">找同题材的参考</h2>
      <span class="faint">只借鉴写法，不照搬内容；参考视频默认是灵感，不当作已核实事实。</span>
    </div>
    <div class="panel panel-pad">
      <form class="search-row" @submit.prevent="search(false)">
        <label class="field grow"><span>搜索主题</span>
          <input v-model="content.query" type="text" placeholder="例如：AI 助手怎么用" aria-label="搜索主题" />
        </label>
        <label class="field"><span>时间范围</span>
          <select v-model.number="content.periodDays" aria-label="时间范围">
            <option :value="7">近 7 天</option><option :value="30">近 30 天</option>
          </select>
        </label>
        <label class="field"><span>排序</span>
          <select v-model="content.sort" aria-label="排序">
            <option value="likes">点赞最多</option><option value="recent">最新发布</option><option value="recommended">平台推荐</option>
          </select>
        </label>
        <button class="primary" type="submit" :disabled="busy || !content.query.trim()">搜索参考</button>
      </form>
      <p class="cost-note">{{ content.costNote }} 导入时会尝试提取口播转写；纯音乐或舞蹈视频没有口播是正常结果，可在导入后另行分析画面。</p>
      <p v-if="content.searchedAt" class="faint">数据获取时间：{{ content.searchedAt }}。{{ sortNote }}</p>

      <ul v-if="content.searchItems.length" class="result-list">
        <li v-for="item in content.searchItems" :key="item.platform_work_id" class="result" :class="{ selected: selectedPlatformId === item.platform_work_id }">
          <div class="cover" aria-hidden="true">
            <img v-if="item.cover_url" :src="item.cover_url" alt="" />
            <span v-else>{{ item.media_kind === 'image_set' ? '图集' : '视频' }}</span>
          </div>
          <div class="info">
            <p class="caption">{{ item.caption || '（无发布简介）' }}</p>
            <p class="meta">{{ item.author || '未知作者' }} · {{ item.published_at || '发布时间未提供' }}</p>
            <p class="metrics">
              <span>点赞 {{ fmt(item.metrics.likes) }}</span><span>评论 {{ fmt(item.metrics.comments) }}</span>
              <span>分享 {{ fmt(item.metrics.shares) }}</span><span>收藏 {{ fmt(item.metrics.collects) }}</span>
            </p>
          </div>
          <div class="actions">
            <button class="ghost sm" type="button" @click="open(item)">看原视频</button>
            <button v-if="item.transcribable" class="sm" type="button" :disabled="busy || importedIds.has(item.platform_work_id)"
                    @click="content.importReference(item)">
              {{ importedIds.has(item.platform_work_id) ? '已导入' : '导入参考' }}
            </button>
            <span v-else class="faint">图集没有口播，不能导入</span>
          </div>
        </li>
      </ul>
      <p v-else-if="content.searchJob" class="faint empty">这次没有搜到结果，可以换个说法或放宽时间范围。</p>
      <button v-if="content.nextCursor" class="ghost sm more" type="button" :disabled="busy" @click="search(true)">再加载一页</button>
    </div>
  </section>
</template>

<style scoped>
.search-row { display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-end; }
.grow { flex: 1; min-width: 220px; }
.cost-note { font-size: 12px; color: var(--fg-3); margin: 10px 0 0; }
.result-list { list-style: none; padding: 0; margin: 16px 0 0; border-top: 1px solid var(--line); }
.result { display: grid; grid-template-columns: 72px 1fr auto; gap: 14px; padding: 14px 0; border-bottom: 1px solid var(--line); align-items: center; }
.result.selected { box-shadow: inset 3px 0 0 var(--fg); padding-left: 10px; }
.cover { width: 72px; height: 96px; background: var(--line); display: grid; place-items: center; font-size: 12px; color: var(--fg-3); overflow: hidden; }
.cover img { width: 100%; height: 100%; object-fit: cover; }
.caption { font-size: 14px; line-height: 1.7; margin: 0 0 4px; overflow-wrap: anywhere; }
.meta, .metrics { font-size: 12px; color: var(--fg-3); margin: 0; }
.metrics span + span { margin-left: 12px; }
.actions { display: flex; flex-direction: column; gap: 6px; align-items: flex-end; }
.empty { margin-top: 16px; }
.more { margin-top: 12px; }
@media (max-width: 700px) { .result { grid-template-columns: 56px 1fr; } .actions { grid-column: 1 / -1; flex-direction: row; } }
</style>
