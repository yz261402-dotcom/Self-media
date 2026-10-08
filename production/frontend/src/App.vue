<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useStudioStore } from '@/stores/studio'
import { apiMode } from '@/api'
import StatusBadge from '@/components/StatusBadge.vue'
import WorkflowGuide from '@/components/WorkflowGuide.vue'
import DeleteConfirmation from '@/components/DeleteConfirmation.vue'
import type { Work } from '@/api/types'

const store = useStudioStore()
const route = useRoute()
const router = useRouter()

const toast = ref<{ kind: string; text: string } | null>(null)
let timer: ReturnType<typeof setTimeout> | undefined

watch(
  () => [store.notice, store.error] as const,
  ([n, e]) => {
    const next = e ? { kind: 'danger', text: e } : n ? { kind: n.kind, text: n.text } : null
    if (!next) return
    toast.value = next
    clearTimeout(timer)
    timer = setTimeout(() => {
      toast.value = null
      store.clearNotice()
    }, 3500)
  },
  { deep: true, immediate: true },
)

const STEPS = [
  { to: '/avatar', label: '人物形象' },
  { to: '/script', label: '内容创作' },
  { to: '/tts', label: '语音配音' },
  { to: '/video', label: '视频合成' },
]
const PLANS = [
  { to: '/publish', label: '发布' },
  { to: '/metrics', label: '指标监控' },
  { to: '/models', label: '模型选择' },
]
const isCreationPage = computed(() => STEPS.some(s => s.to === route.path))

const showCreate = ref(false)
const workToDelete = ref<Pick<Work, 'id' | 'name'> | null>(null)
const deletingWork = ref(false)
const deleteWorkError = ref('')

function startWorkDelete() {
  if (!store.currentWork || store.busy || store.deletionPending) return
  workToDelete.value = { id: store.currentWork.id, name: store.currentWork.name }
  deleteWorkError.value = ''
}

function cancelWorkDelete() {
  if (deletingWork.value) return
  workToDelete.value = null
  deleteWorkError.value = ''
}

async function confirmWorkDelete() {
  const target = workToDelete.value
  if (!target || deletingWork.value) return
  deletingWork.value = true
  deleteWorkError.value = ''
  try {
    if (await store.deleteWork(target.id)) workToDelete.value = null
    else deleteWorkError.value = store.error ?? '删除未完成，请稍后重试'
  } catch (cause: any) {
    deleteWorkError.value = cause?.message ?? '删除未完成，请稍后重试'
  } finally {
    deletingWork.value = false
  }
}
const form = ref({
  name: '', topic: '', content_type: 'AI 科普',
  audience: '对 AI 感兴趣的年轻受众', keywords: '', duration: '' as number | '',
})
const validDuration = computed(() => form.value.duration === '' ||
  (Number.isFinite(form.value.duration) && form.value.duration > 0 &&
    Number.isSafeInteger(Math.round(form.value.duration * 1000)) && Math.round(form.value.duration * 1000) > 0))

const mode = computed(() => {
  const m = store.capabilities?.mode ?? (apiMode === 'mock' ? 'mock' : 'unknown')
  return m === 'mock' ? 'mock' : m === 'production' ? 'live' : m === 'integration' ? 'live' : 'err'
})
const modeText = computed(() => ({
  mock: 'MOCK', live: '生产', err: '未连接', unknown: '未连接',
}[mode.value] ?? '未连接'))

/** 侧栏每一步的推进状态：完成 / 进行中 / 待办 */
const stepState = computed<Record<string, 'ok' | 'run' | 'off'>>(() => {
  const w = store.currentWork
  return {
    '/avatar': store.identityReady ? 'ok' : w?.identity_id ? 'run' : 'off',
    '/script': store.scriptReady ? 'ok' : store.busy === '写稿' ? 'run' : 'off',
    '/tts': store.confirmedAudio ? 'ok' : store.audios.length ? 'run' : 'off',
    '/video': store.deliveryReady ? 'ok' : store.task
      && ['talking_head', 'postprocess', 'publish_package'].includes(store.task.stage)
      && ['running', 'queued', 'waiting_provider'].includes(store.task.state) ? 'run' : 'off',
  }
})

/** 侧栏卡片第二行：当前这一步的具体状态，不只显示进度点 */
const stepHint = computed<Record<string, string>>(() => {
  const s = stepState.value
  const dur = ((store.confirmedAudio?.duration_ms ?? 0) / 1000).toFixed(1)
  return {
    '/avatar': store.currentIdentity?.name ?? '未选择人物',
    '/script': s['/script'] === 'ok' ? '已采用一稿' : store.scriptBatch
      ? store.scriptBatch.ready_to_present ? '三稿待选定' : '审核或返工中' : '尚未生成',
    '/tts': store.currentWork?.brief.content_route === 'short_video_storyboard' ? '分镜路线不需要配音'
      : store.confirmedAudio ? `已确认 ${dur}s` : store.audios.length ? '待试听确认' : '尚未合成',
    '/video': store.deliveryReady ? '成片可下载' : store.finalVideo ? '原片已确认'
      : store.hasReviewableOriginal ? '原片待审核'
      : store.unresolvedVideoRuns.length ? '等待生成结果'
      : s['/video'] === 'run' ? '合成进行中' : '尚未提交',
    '/publish': '抖音浏览器发布',
    '/metrics': '未接入平台数据',
    '/models': '配置页后续开放',
  }
})

async function createWork() {
  if (!validDuration.value) return
  const created = await store.createWork({
    name: form.value.name || form.value.topic || '未命名作品',
    topic: form.value.topic,
    content_type: form.value.content_type,
    audience: form.value.audience,
    keywords: form.value.keywords.split(/[,，\s]+/).filter(Boolean),
    target_duration_ms: form.value.duration === '' ? 0 : Math.round(form.value.duration * 1000),
  })
  if (!created) return
  showCreate.value = false
  form.value = { name: '', topic: '', content_type: 'AI 科普', audience: '对 AI 感兴趣的年轻受众', keywords: '', duration: '' }
  await router.push('/avatar')
}

onMounted(async () => {
  await router.isReady()
  if (!route.meta.standalone) await store.bootstrap()
})
watch(() => route.meta.standalone, (standalone, previous) => {
  if (!standalone && previous && !store.apiReady) void store.bootstrap()
})
</script>

<template>
  <router-view v-if="route.meta.standalone" />
  <div v-else class="shell">
    <!-- 侧栏 -->
    <aside class="sidebar" aria-label="功能导航">
      <div class="sidebar-head">
        <router-link to="/avatar" class="sidebar-title">口播工作台</router-link>
        <span class="mobile-nav-hint">左右滑动切换功能</span>
      </div>

      <div class="sidebar-body">
        <div class="nav-group">
          <div class="nav-group-label">制作</div>
          <router-link
            v-for="s in STEPS"
            :key="s.to"
            :to="s.to"
            class="nav-item"
            :class="{ active: route.path === s.to }"
          >
            <span class="nav-text">
              <span class="nav-label">{{ s.label }}</span>
              <span class="nav-hint">{{ stepHint[s.to] }}</span>
            </span>
          </router-link>
        </div>

        <div class="nav-group">
          <div class="nav-group-label">其他功能</div>
          <router-link
            v-for="s in PLANS"
            :key="s.to"
            :to="s.to"
            class="nav-item"
            :class="{ active: route.path === s.to }"
          >
            <span class="nav-text">
              <span class="nav-label">{{ s.label }}</span>
            </span>
            <small v-if="s.to !== '/publish'" class="nav-planned">开发中</small>
          </router-link>
        </div>
      </div>

      <div class="sidebar-foot" :title="`运行模式 ${modeText} · 单条额度 ¥${((store.capabilities?.budget.per_task_limit_fen ?? 0) / 100).toFixed(2)}`">
        <span class="mode-tag" :class="mode">{{ modeText }}</span>
        <span class="num faint">额度 ¥{{ ((store.capabilities?.budget.per_task_limit_fen ?? 0) / 100).toFixed(2) }}</span>
      </div>
    </aside>

    <!-- 主区 -->
    <div class="main">
      <header class="topbar">
        <div class="work-select">
          <span class="work-select-label">当前作品</span>
          <select
            aria-label="当前作品"
            :value="store.currentWorkId ?? ''"
            :disabled="!!store.busy"
            @change="store.selectWork(($event.target as HTMLSelectElement).value)"
          >
            <option v-if="!store.works.length" value="">暂无作品</option>
            <option v-for="w in store.works" :key="w.id" :value="w.id">{{ w.name }} · #{{ w.id.slice(-4) }}</option>
          </select>
        </div>
        <button class="sm" :disabled="!!store.busy || !store.apiReady" @click="showCreate = true">新建作品</button>
        <button class="sm danger" :disabled="!store.currentWork || !!store.busy || store.deletionPending" @click="startWorkDelete">删除作品</button>

        <div class="topbar-spacer"></div>

        <span v-if="store.busy" class="st run">{{ store.busy }}…</span>
        <StatusBadge v-if="store.task" :text="store.task.state" />
      </header>

      <div class="content">
        <div class="page-wrap">
          <div v-if="store.error" class="persistent-error" role="alert">
            <strong>操作未完成</strong><p>{{ store.error }}</p>
            <button v-if="!store.apiReady" :disabled="!!store.busy" @click="store.bootstrap()">重新连接</button>
            <button v-else class="sm" @click="store.error = null">知道了</button>
          </div>
          <div v-if="!store.apiReady && !store.error" class="empty">正在连接工作台…</div>
          <template v-if="store.apiReady">
            <WorkflowGuide v-if="isCreationPage" @create="showCreate = true" />
            <router-view v-if="store.currentWork || ['/avatar', '/publish', '/metrics', '/models'].includes(route.path)" />
          </template>
        </div>
      </div>
    </div>

    <DeleteConfirmation
      v-if="workToDelete"
      title="删除作品"
      :name="workToDelete.name"
      :pending="deletingWork"
      :error="deleteWorkError"
      impact="删除后作品将从列表中移除，页面没有恢复入口。已有媒体、生成记录和费用历史会保留。正在执行或结果未知的任务须先处理。"
      @cancel="cancelWorkDelete"
      @confirm="confirmWorkDelete"
    />

    <!-- 新建作品 -->
    <div v-if="showCreate" class="modal-mask" @click.self="showCreate = false">
      <div class="modal" role="dialog" aria-modal="true" aria-label="新建作品">
        <div class="modal-head">新建作品</div>
        <div class="modal-body">
          <p class="muted">先告诉我们这一期讲什么。创建后，你会从选人物开始，逐步完成成片。</p>
          <p v-if="store.error" class="form-error" role="alert">{{ store.error }}</p>
          <label class="field">
            <span>作品名称</span>
            <input v-model="form.name" type="text" placeholder="例如：RAG 科普 · 第 1 期" />
          </label>
          <label class="field">
            <span>选题 / 要求</span>
            <input v-model="form.topic" type="text" placeholder="例如：用三分钟讲清楚 RAG" />
          </label>
          <div class="grid-2">
            <label class="field"><span>文案类型</span><input v-model="form.content_type" type="text" /></label>
            <label class="field"><span>目标时长（秒，选填）</span><input v-model.number="form.duration" type="number" step="any" placeholder="留空，按内容自然生成" /></label>
          </div>
          <p class="muted">不限制成片总时长。填写时长用于指导写稿，最终长度以确认的配音为准。</p>
          <label class="field">
            <span>目标受众</span>
            <input v-model="form.audience" type="text" />
          </label>
          <label class="field" style="margin-bottom: 0">
            <span>关键词（空格或逗号分隔）</span>
            <input v-model="form.keywords" type="text" placeholder="RAG 检索 科普" />
          </label>
        </div>
        <div class="modal-foot">
          <button @click="showCreate = false">取消</button>
          <button class="primary" :disabled="!form.topic.trim() || !validDuration || store.busy !== ''" @click="createWork">创建并选择人物</button>
        </div>
      </div>
    </div>

    <!-- 浮层提示：不打断操作，3.5 秒自动消失 -->
    <div v-if="toast" class="toast" :class="toast.kind">
      <span class="st" :class="toast.kind">{{ toast.text }}</span>
    </div>
  </div>
</template>
