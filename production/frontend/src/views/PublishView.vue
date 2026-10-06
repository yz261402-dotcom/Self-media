<script setup lang="ts">
import { computed, onMounted, onScopeDispose, ref, watch } from 'vue'
import { api } from '@/api'
import type { PublishTaskView, PublishTaskState, PublishSessionView } from '@/api/types'
import { useStudioStore } from '@/stores/studio'

const store = useStudioStore()

const platforms = [
  { key: 'douyin', label: '抖音', state: '浏览器发布', detail: '扫码登录，准备后由你确认发布' },
  { key: 'bilibili', label: 'B 站', state: '开发中', detail: '尚未接入' },
  { key: 'kuaishou', label: '快手', state: '开发中', detail: '尚未接入' },
  { key: 'xiaohongshu', label: '小红书', state: '开发中', detail: '尚未接入' },
  { key: 'channels', label: '微信视频号', state: '开发中', detail: '尚未接入' },
] as const

const creatorCenterUrl = 'https://creator.douyin.com/'
const selectedPlatform = ref('douyin')
const publishTask = ref<PublishTaskView | null>(null)
const publishAcknowledged = ref(false)
const loginSession = ref<PublishSessionView | null>(null)
const loginBusy = ref(false)
const loginError = ref('')
const action = ref('')
const publishError = ref('')
const copyNotice = ref('')
const publishTitle = ref('')
const publishDescription = ref('')
const publishTags = ref('')
let pollTimer: ReturnType<typeof setInterval> | undefined
let requestVersion = 0
let polling = false
let mounted = false

const packageReady = computed(() => store.deliveryReady && !!store.pkg)
const publishState = computed<PublishTaskState>(() => currentTask.value?.state ?? 'draft')
const currentTask = computed(() => publishTask.value?.work_id === store.currentWorkId ? publishTask.value : null)
const titleLength = computed(() => Array.from(publishTitle.value.trim()).length)
const titleValid = computed(() => titleLength.value >= 1 && titleLength.value <= 30)
const parsedTags = computed(() => [...new Set(publishTags.value.split(/[#，,、\s]+/u).map(tag => tag.trim()).filter(Boolean))])
const metadataLocked = computed(() => !!action.value || (!!currentTask.value?.id && !['draft', 'cancelled'].includes(publishState.value)))
const canStart = computed(() => packageReady.value && !action.value && titleValid.value && ['draft', 'failed', 'cancelled', 'awaiting_login'].includes(publishState.value))
const canConfirm = computed(() => packageReady.value && !action.value && publishAcknowledged.value
  && publishState.value === 'ready_to_confirm' && !!currentTask.value?.id && currentTask.value.requires_user_confirmation === true
  && !!currentTask.value.package_digest && !!currentTask.value.confirmation_digest
  && (!currentTask.value.package_id || currentTask.value.package_id === store.pkg?.id))
const canCancel = computed(() => !!currentTask.value?.id && !action.value
  && ['draft', 'awaiting_login', 'ready_to_confirm', 'failed'].includes(publishState.value))
const statusLabel = computed(() => ({
  draft: '待开始', awaiting_login: '等待登录', uploading: '正在上传', ready_to_confirm: '待确认',
  submitting: '正在提交', submitted: '已提交，待平台审核', needs_verification: '待核验结果', failed: '操作未完成', cancelled: '已取消',
}[publishState.value]))
const statusDetail = computed(() => ({
  draft: '先打开专用发布浏览器并扫码登录。开始准备会上传视频和填写文案，停在发布前。',
  awaiting_login: '请在专用发布浏览器扫码登录或完成页面验证，再点击“登录后继续准备”。',
  uploading: '正在上传并准备投稿，请保持专用发布浏览器打开。准备完成后可确认发布或取消；上传期间不能取消。',
  ready_to_confirm: '请核对专用浏览器内的账号、视频、文案、封面和 AI 内容声明。确认后将执行发布。',
  submitting: '正在向抖音提交作品，请等待结果，不要再次点击发布。',
  submitted: '已经提交给抖音。提交成功不代表审核通过，实际展示状态以创作者中心为准。',
  needs_verification: '发布结果不明确，先到创作者中心核对是否已提交，请勿重复发布。',
  failed: '本次操作未完成，请根据错误信息处理。',
  cancelled: '发布任务已取消，可以重新准备。',
}[publishState.value]))
const visibleError = computed(() => publishError.value || currentTask.value?.error?.message || '')
const accountLabel = computed(() => currentTask.value?.account_label || loginSession.value?.account_label || '')
const loginStateLabel = computed(() => ({
  awaiting_login: '等待扫码登录', logged_in: '已登录', unavailable: '浏览器不可用',
  needs_verification: '需要完成页面验证', busy: '浏览器正在执行任务',
}[loginSession.value?.state ?? 'unavailable']))

function contextMatches(workId: string, version: number) {
  return store.currentWorkId === workId && requestVersion === version
}

async function runAction(label: string, call: (workId: string) => Promise<PublishTaskView>) {
  const workId = store.currentWorkId
  if (!workId || action.value) return
  const version = ++requestVersion
  action.value = label
  publishError.value = ''
  try {
    const result = await call(workId)
    if (contextMatches(workId, version) && result.work_id === workId) publishTask.value = result
  } catch (error: any) {
    if (contextMatches(workId, version)) publishError.value = error?.message ?? String(error)
  } finally {
    if (contextMatches(workId, version)) action.value = ''
  }
}

async function beginPrepare() {
  if (!canStart.value) return
  publishAcknowledged.value = false
  if (currentTask.value?.id && ['awaiting_login', 'failed'].includes(publishState.value)) {
    const taskId = currentTask.value.id
    await runAction('继续准备', workId => api.resumePublish(workId, taskId))
    return
  }
  await runAction('准备投稿', workId => api.startPublish(workId, {
    platform: 'douyin', auto_submit: false, title: publishTitle.value.trim(),
    description: publishDescription.value.trim(), tags: parsedTags.value,
  }))
}

async function openLogin() {
  if (loginBusy.value || action.value) return
  loginBusy.value = true
  loginError.value = ''
  try { loginSession.value = await api.openPublishLogin() }
  catch (error: any) { loginError.value = error?.message ?? String(error) }
  finally { loginBusy.value = false }
}

async function refreshSession() {
  if (loginBusy.value || action.value) return
  loginBusy.value = true
  loginError.value = ''
  try { loginSession.value = await api.getPublishSession() }
  catch (error: any) { loginError.value = error?.message ?? String(error) }
  finally { loginBusy.value = false }
}

async function confirmAndPublish() {
  if (!canConfirm.value || !currentTask.value?.id || !currentTask.value.package_digest || !currentTask.value.confirmation_digest) return
  const taskId = currentTask.value.id
  const packageDigest = currentTask.value.package_digest
  const confirmationDigest = currentTask.value.confirmation_digest
  publishAcknowledged.value = false
  await runAction('正在提交发布', workId => api.confirmPublish(workId, taskId, packageDigest, confirmationDigest))
}

async function cancelPrepare() {
  if (!canCancel.value || !currentTask.value?.id) return
  const taskId = currentTask.value.id
  publishAcknowledged.value = false
  await runAction('取消准备', workId => api.cancelPublish(workId, taskId))
}

async function refreshStatus() {
  const workId = store.currentWorkId
  if (!workId || polling || action.value) return
  const version = requestVersion
  polling = true
  try {
    const result = await api.getPublishStatus(workId)
    if (contextMatches(workId, version) && result.work_id === workId) publishTask.value = result
  } catch (error: any) {
    if (contextMatches(workId, version)) publishError.value = error?.message ?? String(error)
  } finally { polling = false }
}

async function refreshPackage() {
  if (action.value) return
  publishError.value = ''
  if (store.currentWorkId) await store.loadWork(store.currentWorkId)
  await refreshStatus()
}

async function copyValue(value: string, label: string) {
  if (!value) return
  try {
    if (typeof navigator === 'undefined' || !navigator.clipboard) throw new Error('clipboard unavailable')
    await navigator.clipboard.writeText(value)
    copyNotice.value = `${label}已复制`
  } catch {
    copyNotice.value = `无法自动复制${label}，请手动选择文本`
  }
}

watch(() => [store.currentWorkId, store.workLoadVersion] as const, () => {
  requestVersion++
  publishTask.value = null
  publishAcknowledged.value = false
  action.value = ''
  publishError.value = ''
  if (mounted) void refreshStatus()
})
watch(() => [store.pkg?.id, store.pkg?.work_id] as const, () => {
  publishTitle.value = store.pkg?.title ?? ''
  publishDescription.value = store.pkg?.description ?? ''
  publishTags.value = store.pkg?.tags?.join('、') ?? ''
}, { immediate: true })
watch(() => JSON.stringify([currentTask.value?.id, currentTask.value?.title, currentTask.value?.description, currentTask.value?.tags]), () => {
  const task = currentTask.value
  if (!task?.id) return
  if (task.title !== undefined) publishTitle.value = task.title
  if (task.description !== undefined) publishDescription.value = task.description
  if (task.tags !== undefined) publishTags.value = task.tags.join('、')
})
watch(() => currentTask.value?.confirmation_digest, () => { publishAcknowledged.value = false }, { flush: 'sync' })
onMounted(() => {
  mounted = true
  void refreshStatus()
  void refreshSession()
  pollTimer = setInterval(() => { void refreshStatus() }, 3000)
})
onScopeDispose(() => {
  mounted = false
  requestVersion++
  if (pollTimer) clearInterval(pollTimer)
})
</script>

<template>
  <div class="planned-page publish-page">
    <div class="page-head"><h1>发布</h1><span class="tag">抖音 · 浏览器发布</span></div>
    <p class="page-sub">先登录账号，再上传已确认的成片。准备完成后，由你核对并确认发布。</p>

    <section class="section publish-login">
      <div class="section-head"><h2 class="section-title">抖音账号</h2><span :class="['st', loginSession?.state === 'logged_in' ? 'ok' : 'plain']">{{ loginSession ? loginStateLabel : '尚未检查' }}</span><span v-if="loginSession?.adapter === 'mock'" class="tag">MOCK</span></div>
      <p v-if="accountLabel" class="publish-account">当前账号：<strong>{{ accountLabel }}</strong><span v-if="loginSession?.account_id" class="faint"> · {{ loginSession.account_id }}</span></p>
      <p class="muted">使用专用发布浏览器扫码登录，普通浏览器中的登录不会自动共享。</p>
      <p v-if="loginSession?.message" class="muted">{{ loginSession.message }}</p>
      <p v-if="loginError" class="publish-error" role="alert">{{ loginError }}</p>
      <div class="btn-row">
        <button :disabled="loginBusy || !!action" @click="openLogin">{{ loginBusy ? '连接发布浏览器…' : '打开专用浏览器登录' }}</button>
        <button class="sm" :disabled="loginBusy || !!action" @click="refreshSession">检查登录状态</button>
        <a class="action-link" :href="creatorCenterUrl" target="_blank" rel="noopener noreferrer">打开抖音创作者中心</a>
      </div>
    </section>

    <section class="section">
      <div class="section-head">
        <h2 class="section-title">当前作品</h2>
        <span class="section-note">{{ store.currentWork?.name ?? '未选择作品' }}</span>
        <span class="spacer"></span>
        <button class="sm" :disabled="!!store.busy || !!action" @click="refreshPackage">刷新</button>
      </div>

      <div v-if="!packageReady" class="panel panel-pad publish-blocker">
        <strong>{{ store.currentWork ? '尚未有可发布的交付包' : '请先选择或创建作品' }}</strong>
        <p class="muted">必须先在视频页完成后期、预览并确认成片，才能进入发布准备。</p>
        <router-link v-if="store.currentWork" to="/video" class="action-link">去制作视频</router-link>
      </div>

      <template v-else>
        <div class="publish-package-head">
          <div>
            <strong>{{ store.pkg?.title }}</strong>
            <p class="muted">交付包已确认，可开始准备投稿。</p>
          </div>
          <a v-if="store.pkg?.download_url" :href="store.pkg.download_url" class="action-link" download>下载完整作品包</a>
        </div>

        <div class="publish-preview-grid">
          <div class="publish-preview panel">
            <div class="preview-label">视频预览</div>
            <video v-if="store.pkg?.video_url" :src="store.pkg.video_url" controls preload="metadata" aria-label="视频预览"></video>
            <div v-else class="preview-empty">暂无视频预览地址</div>
          </div>
          <div class="publish-preview panel">
            <div class="preview-label">封面预览</div>
            <img v-if="store.pkg?.cover_url" :src="store.pkg.cover_url" alt="封面预览" />
            <div v-else class="preview-empty">暂无封面预览地址</div>
          </div>
        </div>

        <div class="publish-meta panel">
          <div class="panel-head"><strong>投稿资料</strong><span class="spacer"></span><span v-if="copyNotice" class="st ok" role="status">{{ copyNotice }}</span></div>
          <div class="publish-meta-body">
            <div class="publish-meta-row">
              <label class="meta-label" for="publish-title">标题</label>
              <div><input id="publish-title" v-model="publishTitle" type="text" :readonly="metadataLocked" aria-describedby="publish-title-help" /><small id="publish-title-help" :class="{ 'form-error': !titleValid }">{{ titleLength }} / 30 字；标题需要 1–30 字，过长请手动缩短。</small></div>
              <button class="sm" :disabled="!publishTitle.trim()" @click="copyValue(publishTitle.trim(), '标题')">复制标题</button>
            </div>
            <div class="publish-meta-row"><label class="meta-label" for="publish-description">简介</label><textarea id="publish-description" v-model="publishDescription" :readonly="metadataLocked" rows="3" /><button class="sm" :disabled="!publishDescription" @click="copyValue(publishDescription, '简介')">复制简介</button></div>
            <div class="publish-meta-row"><label class="meta-label" for="publish-tags">话题</label><div><input id="publish-tags" v-model="publishTags" type="text" :readonly="metadataLocked" placeholder="例如：AI、RAG、知识库" /><small>用空格、逗号或顿号分隔，可带 #。</small></div><button class="sm" :disabled="!parsedTags.length" @click="copyValue(parsedTags.map(tag => '#' + tag).join(' '), '话题')">复制话题</button></div>
            <p class="section-footnote">{{ metadataLocked ? '已开始准备，以上是本次投稿文案。需要修改时先取消准备，再重新开始。' : '修改仅用于本次投稿，不会改写原交付包。' }}</p>
          </div>
        </div>
      </template>
    </section>

    <section class="section">
      <div class="section-head"><h2 class="section-title">发布平台</h2><span class="section-note">单账号、单作品</span></div>
      <table class="platform-table">
        <thead><tr><th>平台</th><th>状态</th><th>说明</th><th></th></tr></thead>
        <tbody>
          <tr v-for="platform in platforms" :key="platform.key" :class="{ selected: selectedPlatform === platform.key }">
            <td><strong>{{ platform.label }}</strong></td>
            <td><span :class="['st', platform.key === 'douyin' ? 'info' : 'plain']">{{ platform.state }}</span></td>
            <td class="muted">{{ platform.detail }}</td>
            <td><button v-if="platform.key === 'douyin'" class="sm" :disabled="!packageReady" @click="selectedPlatform = platform.key">选择</button><span v-else class="faint">—</span></td>
          </tr>
        </tbody>
      </table>
    </section>

    <section v-if="packageReady && selectedPlatform === 'douyin'" class="section publish-action-section">
      <div class="section-head"><h2 class="section-title">抖音自动发布</h2><span :class="['st', publishState === 'failed' ? 'danger' : publishState === 'draft' ? 'plain' : ['ready_to_confirm', 'needs_verification'].includes(publishState) ? 'warn' : 'info']">{{ statusLabel }}</span><span v-if="currentTask?.is_mock" class="tag">MOCK · 未真实发布</span></div>
      <div class="panel panel-pad">
        <p class="publish-status-detail">{{ statusDetail }}</p>
        <ol class="publish-steps" aria-label="抖音准备状态">
          <li :class="{ active: publishState === 'awaiting_login', done: ['uploading', 'ready_to_confirm', 'submitted'].includes(publishState) }"><span>1</span>扫码登录</li>
          <li :class="{ active: publishState === 'uploading', done: ['ready_to_confirm', 'submitted'].includes(publishState) }"><span>2</span>上传与填写</li>
          <li :class="{ active: publishState === 'ready_to_confirm', done: publishState === 'submitted' }"><span>3</span>确认发布</li>
        </ol>
        <div v-if="['uploading', 'submitting'].includes(publishState) || action" class="publish-progress" role="status"><progress aria-label="投稿任务进度"></progress><span>{{ action || statusLabel }}…</span></div>
        <p v-if="visibleError" class="publish-error" role="alert">{{ visibleError }}</p>
        <p v-if="currentTask?.package_id && currentTask.package_id !== store.pkg?.id" class="publish-error" role="alert">当前交付包已变化，此任务不能直接发布。请核对旧任务后重新准备。</p>
        <label v-if="publishState === 'ready_to_confirm'" class="ack-label">
          <input v-model="publishAcknowledged" type="checkbox" :disabled="!!action" />
          <span>我已在专用浏览器核对发布账号、视频、文案、封面和 AI 内容声明，确认发布这条作品。</span>
        </label>
        <div class="btn-row publish-actions">
          <button v-if="['draft', 'failed', 'cancelled'].includes(publishState)" class="primary" :disabled="!canStart" @click="beginPrepare">开始准备（不发布）</button>
          <button v-if="publishState === 'awaiting_login'" class="primary" :disabled="!canStart" @click="beginPrepare">登录后继续准备</button>
          <button :class="{ primary: publishState === 'ready_to_confirm' }" :disabled="!canConfirm" @click="confirmAndPublish">确认并发布</button>
          <button v-if="canCancel" class="ghost" @click="cancelPrepare">取消</button>
          <a v-if="currentTask?.provider_url" class="action-link" :href="currentTask.provider_url" target="_blank" rel="noopener noreferrer">查看投稿结果</a>
        </div>
        <p class="section-footnote">扫码和验证码由你在专用浏览器完成。遇到结果不明确时先核对投稿列表，系统不会自动重复发布。</p>
        <p v-if="currentTask?.id" class="section-footnote">任务编号：{{ currentTask.id }}</p>
      </div>
    </section>
  </div>
</template>

<style scoped>
.publish-package-head { display: flex; justify-content: space-between; align-items: center; gap: 16px; padding: 15px 0; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); margin-bottom: 18px; }
.publish-package-head p { margin: 4px 0 0; }
.publish-blocker { border-left: 3px solid var(--amber); }
.publish-blocker p { margin: 5px 0 14px; }
.publish-preview-grid { display: grid; grid-template-columns: minmax(0, 2fr) minmax(200px, 1fr); gap: 14px; margin-bottom: 18px; }
.publish-preview { min-width: 0; overflow: hidden; }
.preview-label { padding: 10px 14px; border-bottom: 1px solid var(--line); font-size: 12px; color: var(--fg-2); }
.publish-preview video { display: block; width: 100%; max-height: 440px; background: #202024; }
.publish-preview img { display: block; width: 100%; max-height: 440px; object-fit: contain; background: var(--panel-2); }
.preview-empty { min-height: 120px; display: grid; place-items: center; color: var(--fg-3); padding: 20px; }
.publish-meta { margin-bottom: 18px; }
.publish-meta-body { padding: 4px 22px 10px; }
.publish-meta-row { display: grid; grid-template-columns: 54px minmax(0, 1fr) auto; align-items: center; gap: 12px; padding: 12px 0; border-bottom: 1px solid var(--line); }
.publish-meta-row:last-child { border-bottom: 0; }
.publish-meta-row small { color: var(--fg-3); display: block; margin-top: 4px; font-size: 12px; }
.publish-meta-row small.form-error { color: var(--red); }
.publish-meta-row [readonly] { background: var(--panel-2); color: var(--fg-2); }
.meta-label { color: var(--fg-3); font-size: 12px; }
.meta-value { overflow-wrap: anywhere; white-space: pre-wrap; }
.platform-table td:last-child, .platform-table th:last-child { text-align: right; }
.platform-table tr.selected td { background: var(--blue-wash); }
.publish-action-section { margin-top: 12px; }
.publish-status-detail { margin-bottom: 18px; color: var(--fg-2); }
.publish-steps { display: flex; gap: 10px; list-style: none; padding: 0; margin: 0 0 20px; }
.publish-steps li { flex: 1; display: flex; align-items: center; gap: 7px; min-width: 0; color: var(--fg-3); font-size: 12px; }
.publish-steps span { width: 23px; height: 23px; display: grid; place-items: center; border-radius: 50%; border: 1px solid var(--line-2); flex: none; font-size: 11px; }
.publish-steps li.active { color: var(--blue-d); }
.publish-steps li.active span { border-color: var(--blue); color: var(--blue); }
.publish-steps li.done { color: var(--green); }
.publish-steps li.done span { border-color: var(--green); color: var(--green); }
.publish-actions { margin-top: 6px; }
.publish-progress { display: flex; gap: 10px; align-items: center; color: var(--fg-2); font-size: 13px; margin-bottom: 16px; }
.publish-progress progress { width: 110px; height: 6px; }
.publish-error { color: var(--red); background: #fff7f6; border-left: 3px solid var(--red); padding: 10px 12px; margin-bottom: 14px; }
@media (max-width: 700px) {
  .publish-preview-grid { grid-template-columns: 1fr; }
  .publish-package-head { align-items: flex-start; flex-direction: column; }
  .publish-meta-row { grid-template-columns: 1fr auto; }
  .publish-meta-row .meta-value { grid-column: 1 / -1; grid-row: 2; }
  .publish-steps { flex-direction: column; }
}
</style>
