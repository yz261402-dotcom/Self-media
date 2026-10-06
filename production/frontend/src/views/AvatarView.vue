<script setup lang="ts">
import { computed, ref, watch, onMounted, onScopeDispose, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { useStudioStore } from '@/stores/studio'
import { api } from '@/api'
import type { AvatarIdentity, AvatarAssetView, AvatarPolicyOptions, IdentityReadiness, QualifyProgress, GeneratedIdentityCandidate, ReferenceImageSource, ReferenceImageGeneration } from '@/api/types'
import QualifySteps from '@/components/QualifySteps.vue'
import MotionReferencePicker from '@/components/MotionReferencePicker.vue'
import FoldCard from '@/components/FoldCard.vue'
import HeroAction from '@/components/HeroAction.vue'
import DeleteConfirmation from '@/components/DeleteConfirmation.vue'

const store = useStudioStore()
const router = useRouter()

/** HeroAction：当前该做什么。用户一进页面就能看到。 */
const pageHero = computed(() => {
  if (store.identityReady) {
    return {
      status: 'done' as const,
      title: `本期人物：${store.currentIdentity?.name ?? '已选'}`,
      desc: '人物已确认，可以开始写稿。',
      ctaText: '下一步：生成三稿',
    }
  }
  return {
    status: 'todo' as const,
    title: '选这期的人物',
    desc: store.identities.length
      ? `从下方 ${store.identities.length} 张卡片里选一位，或者点右侧新建。`
      : '还没有人物，点右侧新建一位主播。',
    ctaText: '新建主播',
  }
})

function onHeroCta() {
  if (store.identityReady) router.push('/script')
  else openCreation()
}

const OUTFITS = [
  { v: 'news_suit', t: '新闻套装' },
  { v: 'knitwear', t: '针织衫' },
  { v: 'shirt', t: '衬衫' },
  { v: 'blazer', t: '西装外套' },
]
const outfit = ref('news_suit')
const background = ref('')
const avatarImages = ref<Record<string, string>>({})
const showCreation = ref(false)
const newIdentity = ref({ gender: '女', age: '22 岁左右', appearance: '', free_text: '' })
const candidates = ref<any[]>([])
const creationMode = ref<'text' | 'reference'>('text')
const referenceImage = ref<ReferenceImageSource | null>(null)
const referencePaidAck = ref(false)
const referenceUploading = ref(false)
const referenceSubmitting = ref(false)
const referenceQuerying = ref(false)
const referenceRequestId = ref('')
const referenceState = ref<ReferenceImageGeneration['state'] | ''>('')
const referenceError = ref('')
let referenceVersion = 0
const referenceStorageKey = 'avatar-studio:avatar-reference-generation'
const referenceUnresolved = computed(() => !!referenceRequestId.value && ['running', 'unknown'].includes(referenceState.value))
const referenceLocked = computed(() => referenceUploading.value || referenceSubmitting.value || referenceQuerying.value || referenceUnresolved.value)
const canGenerateReference = computed(() => !!referenceImage.value && referencePaidAck.value && !referenceLocked.value && !store.busy)

function persistReferenceGeneration() {
  try {
    sessionStorage.setItem(referenceStorageKey, JSON.stringify({ request_id: referenceRequestId.value, state: referenceState.value, source: referenceImage.value, appearance: newIdentity.value.appearance, free_text: newIdentity.value.free_text }))
  } catch { /* The visible request id remains available if browser storage is disabled. */ }
}

try {
  const saved = JSON.parse(sessionStorage.getItem(referenceStorageKey) ?? 'null')
  if (saved?.request_id && ['running', 'unknown'].includes(saved.state)) {
    referenceRequestId.value = saved.request_id
    referenceState.value = saved.state
    referenceImage.value = saved.source ?? null
    newIdentity.value.appearance = saved.appearance ?? ''
    newIdentity.value.free_text = saved.free_text ?? ''
    creationMode.value = 'reference'
    showCreation.value = true
  }
} catch { /* A missing local receipt does not imply a successful generation. */ }
onScopeDispose(() => { referenceVersion++ })

async function chooseReferenceImage(event: Event) {
  if (referenceLocked.value) return
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  referenceError.value = ''
  if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) {
    referenceError.value = '请选择 PNG、JPEG 或 WebP 图片。'
    return
  }
  if (!file.size || file.size > 10 * 1024 * 1024) {
    referenceError.value = '图片须非空且不超过 10 MiB。'
    return
  }
  const version = referenceVersion
  referenceUploading.value = true
  try {
    const image = await api.uploadAvatarReferenceImage(file)
    if (version !== referenceVersion) return
    referenceImage.value = image
    referencePaidAck.value = false
  } catch (cause: any) {
    if (version === referenceVersion) referenceError.value = cause?.message ?? '参考图上传失败。'
  } finally {
    if (version === referenceVersion) referenceUploading.value = false
  }
}

function removeReferenceImage() {
  if (referenceLocked.value) return
  referenceImage.value = null
  referencePaidAck.value = false
  referenceError.value = ''
}

async function acceptReferenceCandidate(result: GeneratedIdentityCandidate) {
  if (!result?.identity?.id || !result.image_url) throw new Error('生成结果缺少人物图片，请查询原作业。')
  referenceState.value = 'succeeded'
  referenceError.value = ''
  referencePaidAck.value = false
  if (!candidates.value.some(item => item.identity?.id === result.identity.id)) candidates.value.unshift(result)
  avatarImages.value[result.identity.id] = result.image_url
  persistReferenceGeneration()
  try { await store.reloadIdentities() } catch { referenceError.value = '人物库刷新失败，请刷新页面。' }
}

async function generateReferenceCandidate() {
  if (!canGenerateReference.value || !referenceImage.value) return
  const version = referenceVersion
  referenceRequestId.value = crypto.randomUUID()
  referenceState.value = 'running'
  referenceSubmitting.value = true
  referenceError.value = ''
  persistReferenceGeneration()
  try {
    const result = await api.createIdentityCandidateFromImage({ reference_image_id: referenceImage.value.id, appearance: newIdentity.value.appearance.trim(), free_text: newIdentity.value.free_text.trim(), paid_ack: true, request_id: referenceRequestId.value })
    if (version === referenceVersion) await acceptReferenceCandidate(result)
  } catch (cause: any) {
    if (version !== referenceVersion) return
    referenceState.value = 'unknown'
    referenceError.value = cause?.message ?? '生成结果尚未确认，请查询原作业。'
    persistReferenceGeneration()
  } finally {
    if (version === referenceVersion) referenceSubmitting.value = false
  }
}

async function queryReferenceGeneration() {
  if (!referenceRequestId.value || referenceSubmitting.value || referenceQuerying.value) return
  const version = referenceVersion
  const requestId = referenceRequestId.value
  referenceQuerying.value = true
  referenceError.value = ''
  try {
    const job = await api.getAvatarImageGeneration(requestId)
    if (version !== referenceVersion) return
    if (job.request_id !== requestId) throw new Error('返回的作业编号不一致，请查询原作业。')
    if (job.state === 'succeeded') {
      if (!job.result) throw new Error('作业成功但图片缺失，请查询原作业。')
      await acceptReferenceCandidate(job.result)
    } else {
      referenceState.value = job.state
      referenceError.value = job.error?.message ?? ''
      if (job.state === 'failed') referencePaidAck.value = false
      persistReferenceGeneration()
    }
  } catch (cause: any) {
    if (version === referenceVersion) referenceError.value = cause?.message ?? '原作业查询失败。'
  } finally {
    if (version === referenceVersion) referenceQuerying.value = false
  }
}

const current = computed(() => store.currentIdentity)
const work = computed(() => store.currentWork)
const hasConfirmedMask = computed(() => store.masks.some(mask => mask.state === 'confirmed'))
onMounted(() => {
  watch(() => store.identities.map(i => i.id).join(','), async () => {
    await Promise.all(store.identities.map(async identity => {
      if (avatarImages.value[identity.id]) return
      const assets = await api.listIdentityAssets(identity.id, 'hero').catch(() => [])
      const image = assets.find(asset => !asset.is_mock && asset.storage_uri)
      if (image && store.identities.some(person => person.id === identity.id)) avatarImages.value[identity.id] = `/api/v1/media?uri=${encodeURIComponent(image.storage_uri)}`
    }))
  }, { immediate: true })
})
watch(() => store.currentWorkId, () => { background.value = ''; inspectId.value = ''; inspectVersion++ })
function openCreation() {
  showCreation.value = true
  void nextTick(() => document.getElementById('create-person')?.scrollIntoView({ behavior: 'smooth', block: 'start' }))
}

async function genCandidates() {
  if (creationMode.value === 'reference') {
    await generateReferenceCandidate()
    return
  }
  // 真实出图：调用文生图服务，返回的候选仍是"待审"——出图不等于合格。
  // 失败时不 swallow：guard 会把后端的 409/422 原因显示出来（含是否可重试）。
  const r = await store.guard('生成候选并出图', () =>
    api.createIdentityCandidateWithImage({
      gender: newIdentity.value.gender,
      age: newIdentity.value.age,
      appearance: newIdentity.value.appearance,
      free_text: newIdentity.value.free_text,
    }),
  )
  if (r) {
    candidates.value = [r, ...candidates.value]
    // 重新拉列表 + 就绪度：新候选要立刻出现在分组里，并带上"还缺什么"。
    await store.reloadIdentities()
  }
}

// ---------------- 定版链：审核 → 参考依据 → 权属确认 → 冻结 ----------------
// 这四步都是"人来做判定"，系统只负责留痕。因此界面上的每个按钮都要求
// 先显式勾选，不做"点一下就等于看见了"的默认同意。

const inspectId = ref('')
let inspectVersion = 0
const readiness = ref<IdentityReadiness | null>(null)
const heroAssets = ref<AvatarAssetView[]>([])
const policyOptions = ref<AvatarPolicyOptions | null>(null)

const reviewAck = ref(false)
const reviewMethod = ref('人工目检：与期望人设一致')
const policyMode = ref<'multi_view' | 'hero_only'>('hero_only')
const policyReason = ref('')
const policyAck = ref<string[]>([])
const attestBasis = ref('')
const attestAck = ref(false)

const inspecting = computed(
  () =>
    store.identities.find((i) => i.id === inspectId.value) ??
    store.discardedIdentities.find((i) => i.id === inspectId.value) ??
    null,
)

// ---------------- 草稿候选：丢弃 / 恢复 ----------------
// 丢弃**不是删除**：后端只把它移出工作列表并留痕，可恢复。理由必填、点两下确认，
// 不做"点一下就等于同意丢弃"——丢错人的代价比多点一次高得多。
const discardId = ref('')
const discardReason = ref('')
const identityToDelete = ref<Pick<AvatarIdentity, 'id' | 'name'> | null>(null)
const deletingIdentity = ref(false)
const deleteIdentityError = ref('')

function startIdentityDelete(person: Pick<AvatarIdentity, 'id' | 'name'>) {
  if (store.busy || store.deletionPending) return
  identityToDelete.value = { id: person.id, name: person.name }
  deleteIdentityError.value = ''
}

function cancelIdentityDelete() {
  if (deletingIdentity.value) return
  identityToDelete.value = null
  deleteIdentityError.value = ''
}

async function confirmIdentityDelete() {
  const target = identityToDelete.value
  if (!target || deletingIdentity.value) return
  deletingIdentity.value = true
  deleteIdentityError.value = ''
  try {
    if (await store.deleteIdentity(target.id)) {
      candidates.value = candidates.value.filter(candidate => (candidate.identity ?? candidate).id !== target.id)
      delete avatarImages.value[target.id]
      if (inspectId.value === target.id) {
        closeInspect()
        heroAssets.value = []
        readiness.value = null
      }
      if (discardId.value === target.id) cancelDiscard()
      identityToDelete.value = null
    } else deleteIdentityError.value = store.error ?? '删除未完成，请稍后重试'
  } catch (cause: any) {
    deleteIdentityError.value = cause?.message ?? '删除未完成，请稍后重试'
  } finally {
    deletingIdentity.value = false
  }
}

/** 该人物"还缺什么"：直接取后端就绪清单的 reasons，界面不自己另编一套说法。 */
function reasonsFor(id: string): string[] {
  return store.readinessMap[id]?.reasons ?? []
}

/**
 * 定版四步进度。同样只取后端结论：界面若自行推断"下一步点哪个"，
 * 就会出现"界面让人点、后端照样拒"的落差——这正是用户此前觉得根本不能用的原因。
 */
function qualifyFor(id: string): QualifyProgress | null {
  return store.readinessMap[id]?.qualify ?? null
}

/** 草稿行的主按钮文案：直接写明走到第几步，用户不必展开面板才知道。 */
function draftActionLabel(id: string): string {
  const q = qualifyFor(id)
  if (!q) return '确认并使用'
  return `完善人物（${q.done_count}/${q.total}）`
}

/**
 * 「已定版」分组只保证**可以被选用**（后端选择端点的门槛是 approval_state）。
 * 它不等于"进得了生产"——生产另有就绪清单。两者混为一谈，会让人以为点了选用就能出片。
 */
function readyFor(id: string): boolean {
  return store.readinessMap[id]?.ready ?? false
}

function startDiscard(id: string) {
  discardId.value = id
  discardReason.value = '试跑出来的草稿，不要了'
}

function cancelDiscard() {
  discardId.value = ''
  discardReason.value = ''
}

async function confirmDiscard(id: string) {
  await store.discardCandidate(id, discardReason.value)
  cancelDiscard()
  if (inspectId.value === id) inspectId.value = ''
}
const hero = computed(() => heroAssets.value.find((a) => a.kind === 'hero') ?? null)
const heroImageUrl = computed(() =>
  hero.value?.storage_uri ? `/api/v1/media?uri=${encodeURIComponent(hero.value.storage_uri)}` : '',
)
// 身份基准摘要就用原图哈希：同一张图里"是同一个人"的唯一可核对依据。
const derivedDigest = computed(() => hero.value?.file_hash ?? '')
const heroReviewed = computed(() => hero.value?.approval_state === 'approved')
const policyDeclared = computed(() => !!inspecting.value?.reference_policy)
/** 已冻结即链路闭合：四步不可再改，界面必须停手，不能给出点了必被拒的按钮。 */
const isFrozen = computed(() => !!inspecting.value?.frozen)
const canFreeze = computed(
  () =>
    !isFrozen.value &&
    heroReviewed.value &&
    policyDeclared.value &&
    !!derivedDigest.value &&
    !!inspecting.value?.license_id,
)

async function openInspect(id: string) {
  const version = ++inspectVersion
  inspectId.value = id
  readiness.value = null
  heroAssets.value = []
  const row = store.identities.find((x) => x.id === id)
  policyMode.value = (row?.reference_policy?.mode as 'multi_view' | 'hero_only') ?? 'hero_only'
  policyReason.value = row?.reference_policy?.reason ?? ''
  policyAck.value = [...(row?.reference_policy?.acknowledged ?? [])]
  reviewAck.value = false
  attestAck.value = false
  attestBasis.value = ''
  if (!policyOptions.value) policyOptions.value = await store.guard('读取审核选项', () => api.avatarPolicyOptions())
  if (inspectVersion !== version || inspectId.value !== id) return
  await refreshInspect()
  if (inspectVersion !== version || inspectId.value !== id) return
  await nextTick()
  if (inspectVersion !== version || inspectId.value !== id) return
  document.getElementById('identity-review')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

function closeInspect() {
  inspectId.value = ''
  inspectVersion++
}

function stepDone(key: string): boolean {
  return readiness.value?.qualify?.steps.some(step => step.key === key && step.done) ?? false
}

function stepOpen(key: string): boolean {
  return (readiness.value?.qualify?.current ?? 'review') === key
}

async function refreshInspect() {
  const id = inspectId.value
  const version = inspectVersion
  const result = await store.guard('读取人物资料', () => Promise.all([
    api.identityReadiness(id),
    api.listIdentityAssets(id),
  ]))
  if (!result || id !== inspectId.value || version !== inspectVersion) return
  const [r, assets] = result
  readiness.value = r
  heroAssets.value = assets
  // 走完任一步都要重取分组与就绪度：刚定版的身份要立刻移到"可用"分组，
  // 而"还缺什么"必须跟着变，否则界面显示的还是上一步的旧清单。
  await store.reloadIdentities()
}

async function copyDraft(identityId: string) {
  const workId = store.currentWorkId
  const workVersion = store.workLoadVersion
  const version = inspectVersion
  const draft = await store.guard('复制为新草稿', () => api.copyIdentityDraft(identityId))
  if (!draft) return
  await store.reloadIdentities()
  if (!store.workContextMatches(workId, workVersion) || inspectVersion !== version) return
  await openInspect(draft.id)
  store.setNotice('ok', '已复用原图创建新草稿，请依次审核并定版。完成后再为当前作品选用。')
}

function togglePolicyAck(text: string) {
  policyAck.value = policyAck.value.includes(text)
    ? policyAck.value.filter((x) => x !== text)
    : [...policyAck.value, text]
}

async function submitReview() {
  if (!hero.value) return
  const r = await store.guard('登记身份审核', () =>
    api.reviewIdentityAsset(inspectId.value, hero.value!.id, {
      outcome: 'pass',
      identity_digest: derivedDigest.value,
      method: reviewMethod.value,
    }),
  )
  if (r) await refreshInspect()
}

async function submitPolicy() {
  const r = await store.guard('声明参考依据', () =>
    api.setReferencePolicy(inspectId.value, {
      mode: policyMode.value,
      reason: policyReason.value,
      acknowledged: policyMode.value === 'hero_only' ? policyAck.value : [],
    }),
  )
  if (r) await refreshInspect()
}

async function submitAttest() {
  const lic = inspecting.value?.license_id
  if (!lic || !policyOptions.value) return
  const r = await store.guard('确认商用范围', () =>
    api.attestLicense(lic, attestBasis.value, policyOptions.value!.attest_token),
  )
  if (r) await refreshInspect()
}

async function submitFreeze() {
  if (!hero.value || !inspecting.value) return
  const r = await store.guard('冻结定版', () =>
    api.freezeIdentity(inspectId.value, {
      hero_asset_id: hero.value!.id,
      identity_digest: derivedDigest.value,
      license_id: inspecting.value!.license_id ?? '',
    }),
  )
  if (r) await refreshInspect()
}
</script>

<template>
  <div class="person-page">
    <HeroAction
      :status="pageHero.status"
      :title="pageHero.title"
      :desc="pageHero.desc"
      :cta-text="pageHero.ctaText"
      :cta-loading="store.busy === '生成候选并出图'"
      @cta="onHeroCta"
    />

    <!-- 新建主播：提到页面顶部，不再沉在最下方 -->
    <FoldCard
      id="create-person"
      title="新建主播"
      hint="根据描述或参考图片生成新人物"
      tone="accent"
      :open="showCreation"
      @toggle="showCreation = $event"
    >
      <div class="creation-layout">
        <form class="creation-form" @submit.prevent="genCandidates">
          <div class="creation-modes" role="group" aria-label="主播生成方式">
            <button type="button" :class="{ active: creationMode === 'text' }" :aria-pressed="creationMode === 'text'" :disabled="referenceLocked" @click="creationMode = 'text'">文字生成</button>
            <button type="button" :class="{ active: creationMode === 'reference' }" :aria-pressed="creationMode === 'reference'" @click="creationMode = 'reference'">参考图片</button>
          </div>
          <div v-if="creationMode === 'text'" class="grid-2">
            <label class="field"><span>性别</span><input v-model="newIdentity.gender" type="text" /></label>
            <label class="field"><span>年龄</span><input v-model="newIdentity.age" type="text" /></label>
          </div>
          <div v-else class="reference-upload">
            <label v-if="!referenceImage" class="field"><span>人物参考图</span><input type="file" accept="image/png,image/jpeg,image/webp" :disabled="referenceLocked" @change="chooseReferenceImage" /></label>
            <div v-if="referenceImage" class="reference-preview">
              <img :src="referenceImage.preview_url" alt="上传的人物参考图" />
              <div class="btn-row">
                <label class="sm reference-replace">替换<input type="file" accept="image/png,image/jpeg,image/webp" :disabled="referenceLocked" @change="chooseReferenceImage" /></label>
                <button type="button" class="sm" :disabled="referenceLocked" @click="removeReferenceImage">移除</button>
              </div>
            </div>
            <p v-if="referenceUploading" class="inline-help" role="status">正在上传参考图…</p>
          </div>
          <label class="field"><span>外貌、发型与气质</span><input v-model="newIdentity.appearance" placeholder="例如：黑色长发、自然妆容、温柔专业" /></label>
          <label class="field"><span>其他要求</span><textarea v-model="newIdentity.free_text" placeholder="例如：正面半身，面部清晰，演播室柔和光" /></label>
          <label v-if="creationMode === 'reference'" class="check-line"><input v-model="referencePaidAck" type="checkbox" :disabled="referenceLocked" /><span>我确认本次图片生成费用</span></label>
          <div class="generation-action">
            <button v-if="creationMode === 'reference'" type="submit" class="primary" :disabled="!canGenerateReference">{{ referenceSubmitting ? '正在生成图片…' : '根据图片生成主播' }}</button>
            <button v-else type="submit" class="primary" :disabled="!!store.busy">{{ store.busy === '生成候选并出图' ? '正在生成图片…' : '生成一张人物图' }}</button>
            <button v-if="creationMode === 'reference' && referenceUnresolved" type="button" class="sm" :disabled="referenceSubmitting || referenceQuerying" @click="queryReferenceGeneration">{{ referenceQuerying ? '正在查询…' : '查询原作业' }}</button>
            <span v-if="creationMode === 'reference' && referenceRequestId && referenceUnresolved">作业编号：{{ referenceRequestId }}</span>
            <span v-if="creationMode === 'reference' && referenceState === 'failed'">原作业失败。可重新确认费用后生成。</span>
            <span v-if="creationMode === 'reference'">生成结果需人工确认；参考图重绘不保证与原人物完全相同。</span>
            <span v-else>调用生图服务会产生费用。生成后由你确认是否使用。</span>
            <span v-if="creationMode === 'reference' && referenceError" class="creation-error" role="alert">{{ referenceError }}</span>
          </div>
        </form>
        <div class="creation-result">
          <template v-if="candidates.length">
            <article v-for="c in candidates" :key="c.identity?.id ?? c.id" class="new-candidate">
              <img v-if="c.image_url" :src="c.image_url" :alt="(c.identity ?? c).name" />
              <div v-else class="empty">{{ c.asset?.is_mock ? '模拟模式，没有真实图片' : '未取得图片' }}</div>
              <h3>{{ (c.identity ?? c).name }}</h3>
              <p>{{ (c.identity ?? c).persona }}</p>
              <button class="sm" :disabled="!!store.busy" @click="openInspect((c.identity ?? c).id)">查看图片并确认人物</button>
              <ul v-if="c.warnings?.length" class="inline-help"><li v-for="warning in c.warnings" :key="warning">{{ warning }}</li></ul>
            </article>
          </template>
          <div v-else class="creation-placeholder"><strong>人物图片将显示在这里</strong><p>填写人物描述，生成后检查图片，再完成四步确认。</p></div>
        </div>
      </div>
    </FoldCard>

    <section class="person-library" aria-labelledby="person-library-title">
      <div class="section-head">
        <h2 id="person-library-title" class="section-title">选择已有主播</h2>
        <span class="section-note">{{ store.identities.length }} 位人物</span>
      </div>
      <p v-if="!work" class="inline-help">先在顶部创建或选择作品，再选用人物。</p>
      <div class="avatar-cards">
        <article v-for="person in store.identities" :key="person.id" class="avatar-card" :class="{ selected: work?.identity_id === person.id }">
          <div class="avatar-photo">
            <img v-if="avatarImages[person.id]" :src="avatarImages[person.id]" :alt="person.name" @error="delete avatarImages[person.id]" />
            <div v-else class="avatar-no-image"><span>暂无图片</span><small>打开人物资料查看</small></div>
            <span v-if="work?.identity_id === person.id" class="photo-caption">本期人物</span>
          </div>
          <div class="avatar-card-body">
            <h3>{{ person.name }}</h3>
            <p class="person-persona">{{ person.persona }}</p>
            <p class="person-state">{{ readyFor(person.id) ? '已确认，可以制作' : person.frozen ? '已定版，使用资料待完善' : '待确认' }}</p>
            <p v-if="!readyFor(person.id)" class="person-reason">{{ reasonsFor(person.id)[0] || '先确认原图和使用范围。' }}</p>
            <div class="person-actions">
              <button v-if="person.approval_state === 'approved' && readyFor(person.id)" class="primary sm" :disabled="work?.identity_id === person.id || !work || !!store.busy" @click="store.selectIdentity(person.id)">{{ work?.identity_id === person.id ? '本期已选用' : '使用这位主播' }}</button>
              <button :class="['sm', { primary: !readyFor(person.id) }]" :disabled="!!store.busy" @click="openInspect(person.id)">{{ readyFor(person.id) ? '人物资料' : person.frozen ? '查看待完善资料' : draftActionLabel(person.id) }}</button>
              <button class="sm danger" :disabled="!!store.busy || store.deletionPending" @click="startIdentityDelete(person)">删除人物</button>
            </div>
            <details v-if="!person.frozen && person.approval_state !== 'approved'" class="person-manage">
              <summary>管理草稿</summary>
              <template v-if="discardId === person.id">
                <label class="field"><span>丢弃原因</span><input v-model="discardReason" placeholder="填写原因，候选稍后可以恢复" /></label>
                <div class="btn-row"><button class="sm" @click="cancelDiscard">取消</button><button class="sm danger" :disabled="!discardReason.trim() || !!store.busy" @click="confirmDiscard(person.id)">确认丢弃</button></div>
              </template>
              <button v-else class="sm" @click="startDiscard(person.id)">丢弃候选</button>
            </details>
          </div>
        </article>
      </div>
    </section>

    <section v-if="inspecting" id="identity-review" class="review-section" aria-labelledby="identity-review-title">
      <div class="section-head">
        <h2 id="identity-review-title" class="section-title">{{ inspecting.name }} · 人物确认</h2>
        <button class="sm" @click="closeInspect">关闭</button>
      </div>
      <QualifySteps :qualify="readiness?.qualify ?? null" />

      <div class="review-layout">
        <aside class="review-picture">
          <a v-if="heroImageUrl" :href="heroImageUrl" target="_blank" rel="noopener" aria-label="打开人物原图">
            <img :src="heroImageUrl" :alt="inspecting.name" />
          </a>
          <div v-else class="empty">尚无原图</div>
          <p v-if="heroImageUrl" class="inline-help">点击图片，查看原图。</p>
          <p v-if="hero?.is_mock" class="inline-help">当前是模拟资产，不能作为人物确认依据。</p>
          <details class="record-details">
            <summary>图片与人物记录</summary>
            <dl class="record-list">
              <dt>人物编号</dt><dd>{{ inspecting.id }}</dd>
              <template v-if="hero">
                <dt>图片尺寸</dt><dd>{{ hero.width }} × {{ hero.height }}</dd>
                <dt>图片指纹</dt><dd class="mono">{{ hero.file_hash || '无记录' }}</dd>
              </template>
              <dt>使用范围记录</dt><dd>{{ inspecting.license_id || '尚未登记' }}</dd>
            </dl>
          </details>
        </aside>

        <div class="review-form">
          <div v-if="readiness?.ready" class="review-outcome">
            <strong>人物已确认，可以用于制作。</strong>
            <button class="primary" :disabled="!work || work.identity_id === inspecting.id || !!store.busy" @click="store.selectIdentity(inspecting.id)">{{ work?.identity_id === inspecting.id ? '本期已选用' : '使用这位主播' }}</button>
          </div>
          <div v-else-if="isFrozen" class="review-outcome">
            <strong>这版人物已锁定，需要在新草稿中完善资料。</strong>
            <p>复制会复用原图，不产生生图费用。确认新草稿后，再为作品选用。</p>
            <button class="primary" :disabled="!!store.busy || !hero || hero.is_mock" @click="copyDraft(inspecting.id)">复制为新草稿</button>
          </div>

          <template v-if="!isFrozen">
            <details class="review-step" :open="stepOpen('review')">
              <summary><span class="step-number">01</span><span>确认人物原图</span><span class="step-state">{{ stepDone('review') ? '已完成' : '待确认' }}</span></summary>
              <div class="review-step-body">
                <p>查看原图，确认人脸清晰、外貌符合预期，再勾选并保存。</p>
                <template v-if="hero">
                  <label class="check-line"><input v-model="reviewAck" type="checkbox" :disabled="hero.is_mock" /><span>我已查看原图，确认符合预期人设</span></label>
                  <label class="field"><span>核对说明</span><input v-model="reviewMethod" placeholder="例如：人工检查人脸、发型与人设一致" /></label>
                  <button class="primary sm" :disabled="!reviewAck || hero.is_mock || !!store.busy" @click="submitReview">保存原图确认</button>
                  <details class="record-details"><summary>确认内容如何保存</summary><p>目前由你人工确认身份。记录绑定原图指纹，图片变化后需要重新审核。</p></details>
                </template>
                <p v-else class="inline-help">当前没有原图。请先生成一位新主播。</p>
              </div>
            </details>

            <details class="review-step" :open="stepOpen('reference_policy')">
              <summary><span class="step-number">02</span><span>选择人物参考依据</span><span class="step-state">{{ stepDone('reference_policy') ? '已完成' : '待确认' }}</span></summary>
              <div class="review-step-body">
                <p>选择用于保持人物外貌一致的参考资料。只有一张生成图时，选择单张原图并确认其限制。</p>
                <template v-if="policyOptions">
                  <label v-for="p in policyOptions.reference_policies" :key="p.mode" class="check-line">
                    <input v-model="policyMode" type="radio" :value="p.mode" :disabled="policyDeclared" />
                    <span><strong>{{ p.label }}</strong><small>{{ p.summary }}</small></span>
                  </label>
                  <template v-if="policyMode === 'hero_only'">
                    <label class="field"><span>使用单张原图的原因</span><textarea v-model="policyReason" :disabled="policyDeclared" placeholder="例如：人物由 AI 生成，目前只有这张原图。" /></label>
                    <div class="ack-list">
                      <p class="field-heading">请逐项确认</p>
                      <label v-for="lim in policyOptions.hero_only_limitations" :key="lim" class="check-line">
                        <input type="checkbox" :checked="policyAck.includes(lim)" :disabled="policyDeclared" @change="togglePolicyAck(lim)" />
                        <span>{{ lim }}</span>
                      </label>
                    </div>
                  </template>
                  <button class="primary sm" :disabled="policyDeclared || !!store.busy" @click="submitPolicy">{{ policyDeclared ? '参考依据已保存' : '保存参考依据' }}</button>
                </template>
              </div>
            </details>

            <details class="review-step" :open="stepOpen('license_attest')">
              <summary><span class="step-number">03</span><span>确认使用范围</span><span class="step-state">{{ stepDone('license_attest') ? '已完成' : '待确认' }}</span></summary>
              <div class="review-step-body">
                <template v-if="inspecting.license_id">
                  <p>根据图片来源或生成服务条款，填写允许使用这张图片的依据。</p>
                  <label class="field"><span>商用依据</span><input v-model="attestBasis" placeholder="例如：生成服务条款允许本项目商业使用" /></label>
                  <label class="check-line"><input v-model="attestAck" type="checkbox" /><span>我已确认使用范围，并理解这是我作出的判断</span></label>
                  <details class="record-details"><summary>关于使用范围确认</summary><p>{{ policyOptions?.attest_note }}</p><p>系统保存确认人、时间与依据，不能代替法律意见。</p></details>
                  <button class="primary sm" :disabled="!attestAck || !!store.busy" @click="submitAttest">保存使用范围确认</button>
                </template>
                <p v-else class="inline-help">尚无来源记录。新生成的原图会自动登记来源，请先生成图片。</p>
              </div>
            </details>

            <details class="review-step" :open="stepOpen('freeze')">
              <summary><span class="step-number">04</span><span>保存为固定人物</span><span class="step-state">{{ stepDone('freeze') ? '已完成' : '待完成' }}</span></summary>
              <div class="review-step-body">
                <p>完成后，这张原图将作为固定身份，后续作品可直接复用。需要更换原图时，请创建新版本。</p>
                <button class="primary sm" :disabled="!canFreeze || !!store.busy" @click="submitFreeze">确认并定版</button>
                <p v-if="!canFreeze" class="inline-help">请先完成原图、参考依据和使用范围确认。</p>
              </div>
            </details>
          </template>

          <details v-if="readiness?.reasons.length" class="record-details">
            <summary>查看完整待完善清单（{{ readiness.reasons.length }} 项）</summary>
            <ul><li v-for="reason in readiness.reasons" :key="reason">{{ reason }}</li></ul>
          </details>
        </div>
      </div>
    </section>

    <FoldCard title="服装与背景" hint="为当前人物管理出镜画面" tone="purple">
      <div class="section-content">
        <p class="inline-help">当前人物：{{ current?.name ?? '尚未选用' }}。现阶段支持使用蒙版更换背景；服装选项只登记备注。</p>
        <div class="grid-2">
          <label class="field"><span>服装备注</span><select v-model="outfit"><option v-for="o in OUTFITS" :key="o.v" :value="o.v">{{ o.t }}</option></select></label>
          <label class="field"><span>背景图片</span><select v-model="background"><option value="">{{ store.backgroundAssets.length ? '请选择背景图片' : '暂无已登记背景图片' }}</option><option v-for="b in store.backgroundAssets" :key="b.id" :value="b.id">{{ b.note || b.id }}</option></select></label>
        </div>
        <button class="primary sm" :disabled="!current || !background || !hasConfirmedMask || !!store.busy" @click="store.createVariant(outfit, background, store.backgroundAssets.find(b => b.id === background)?.note || '')">生成背景变体</button>
        <p v-if="!hasConfirmedMask || !store.backgroundAssets.length" class="inline-help">需要先登记背景图片，并确认当前人物的蒙版。也可以继续使用已有形象。</p>
        <div v-if="store.variants.length" class="variant-list">
          <div v-for="v in store.variants" :key="v.id" class="variant-row">
            <div><strong>{{ OUTFITS.find(o => o.v === v.outfit)?.t || v.outfit }}</strong><p>{{ v.background }}</p></div>
            <span>{{ v.approval_state === 'approved' ? '已审核' : v.approval_state === 'rejected' ? '未通过' : '待审核' }}</span>
            <details class="record-details"><summary>记录</summary><span class="mono">{{ v.id }}</span></details>
          </div>
        </div>
        <p v-else class="inline-help">当前人物还没有背景变体。</p>
      </div>
    </FoldCard>

    <MotionReferencePicker />

    <FoldCard
      v-if="store.discardedIdentities.length"
      title="已丢弃的候选"
      :hint="`${store.discardedIdentities.length} 位，可以恢复`"
      tone="danger"
    >
      <div class="section-content">
        <div v-for="person in store.discardedIdentities" :key="person.id" class="discarded-row">
          <div><strong>{{ person.name }}</strong><p>{{ person.discard_reason || '未记录原因' }}</p></div>
          <div class="btn-row">
            <button class="sm" :disabled="!!store.busy" @click="store.restoreCandidate(person.id)">恢复候选</button>
            <button class="sm danger" :disabled="!!store.busy || store.deletionPending" @click="startIdentityDelete(person)">删除人物</button>
          </div>
        </div>
      </div>
    </FoldCard>
    <DeleteConfirmation
      v-if="identityToDelete"
      title="删除人物"
      :name="identityToDelete.name"
      :pending="deletingIdentity"
      :error="deleteIdentityError"
      impact="删除后人物将从素材库中移除，页面没有恢复入口。已有图片、授权与审核历史会保留。如果仍被作品引用，请先删除对应作品。"
      @cancel="cancelIdentityDelete"
      @confirm="confirmIdentityDelete"
    />
  </div>
</template>

<style scoped>
.person-library { margin-bottom: 28px; }
.avatar-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(218px, 1fr)); gap: 16px; }
.avatar-card { min-width: 0; background: var(--panel); border: 1px solid var(--line); display: flex; flex-direction: column; }
.avatar-card.selected { border-color: var(--fg); }
.avatar-photo { position: relative; height: 260px; background: var(--panel-2); border-bottom: 1px solid var(--line); }
.avatar-photo img { width: 100%; height: 100%; object-fit: contain; display: block; }
.avatar-no-image { height: 100%; display: grid; place-content: center; gap: 6px; text-align: center; color: var(--fg-3); }
.avatar-no-image small { font-size: 12px; }
.photo-caption { position: absolute; bottom: 10px; left: 10px; color: var(--panel); background: var(--fg); padding: 3px 9px; font-size: 11px; }
.avatar-card-body { display: flex; flex-direction: column; flex: 1; padding: 16px; }
.avatar-card-body h3 { font-size: 16px; margin-bottom: 7px; }
.person-persona { color: var(--fg-2); font-size: 12px; line-height: 1.6; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.person-state { font-size: 12px; color: var(--fg); margin: 4px 0 6px; }
.person-reason { font-size: 12px; color: var(--fg-3); }
.person-actions { display: flex; gap: 6px; flex-wrap: wrap; margin-top: auto; padding-top: 12px; }
.person-manage { margin-top: 12px; font-size: 12px; color: var(--fg-3); }
.person-manage summary { cursor: pointer; }
.person-manage[open] summary { margin-bottom: 10px; }
.review-section { scroll-margin-top: 24px; border: 1px solid var(--line); padding: 22px; margin: 26px 0; }
.review-layout { display: grid; grid-template-columns: 220px minmax(0, 1fr); gap: 28px; margin-top: 22px; align-items: start; }
.review-picture img { width: 100%; display: block; border: 1px solid var(--line); }
.review-picture .inline-help { margin-top: 8px; }
.review-outcome { padding: 4px 0 20px; display: flex; flex-direction: column; align-items: flex-start; gap: 12px; font-size: 13px; }
.review-outcome p { color: var(--fg-2); margin: 0; }
.review-step { border-top: 1px solid var(--line); }
.review-step > summary { cursor: pointer; list-style: none; display: flex; align-items: center; gap: 12px; padding: 17px 0; font-size: 14px; font-weight: 600; }
.review-step > summary::-webkit-details-marker { display: none; }
.step-number { color: var(--fg-3); font-size: 12px; font-variant-numeric: tabular-nums; font-weight: 400; }
.step-state { margin-left: auto; color: var(--fg-3); font-size: 11px; font-weight: 400; }
.review-step-body { padding: 0 0 22px 28px; font-size: 13px; }
.review-step-body > p { color: var(--fg-2); }
.review-step-body .field { margin-top: 14px; }
.check-line { display: flex; gap: 9px; align-items: flex-start; margin: 12px 0; color: var(--fg-2); font-size: 12px; line-height: 1.65; }
.check-line input { margin: 4px 0 0; flex: none; }
.check-line small { display: block; color: var(--fg-3); font-size: 12px; margin-top: 3px; }
.ack-list { margin: 15px 0; padding: 14px; border: 1px solid var(--line); background: var(--panel-2); }
.field-heading { font-size: 12px; font-weight: 600; margin: 0; }
.record-details { color: var(--fg-3); font-size: 12px; margin: 16px 0; overflow-wrap: anywhere; }
.record-details > summary { cursor: pointer; padding: 5px 0; }
.record-details[open] > summary { margin-bottom: 8px; }
.record-details p { margin: 8px 0; }
.record-details ul { padding-left: 18px; margin-bottom: 0; }
.record-list { margin: 0; }
.record-list dt { margin-top: 12px; color: var(--fg-3); }
.record-list dd { margin: 2px 0 0; color: var(--fg-2); }
.person-section { border-top: 1px solid var(--line); scroll-margin-top: 24px; }
.person-section:last-child { border-bottom: 1px solid var(--line); }
.person-section > summary { cursor: pointer; padding: 20px 0; font-size: 15px; }
.person-section > summary small { font-size: 12px; font-weight: 400; color: var(--fg-3); margin-left: 16px; }
.section-content { padding: 2px 0 24px; }
.creation-layout { display: grid; grid-template-columns: minmax(0, 1fr) minmax(240px, .8fr); gap: 32px; padding: 4px 0 28px; }
.creation-form .field { margin-bottom: 18px; }
.creation-form textarea { min-height: 100px; }
.creation-modes { display: inline-flex; border: 1px solid var(--line); margin-bottom: 20px; min-height: 38px; }
.creation-modes button { min-width: 96px; padding: 7px 12px; border: 0; background: var(--panel); color: var(--fg-2); font-size: 12px; }
.creation-modes button + button { border-left: 1px solid var(--line); }
.creation-modes button.active { background: var(--fg); color: var(--panel); }
.reference-upload { margin-bottom: 18px; }
.reference-upload input[type="file"] { max-width: 100%; }
.reference-preview { display: flex; align-items: flex-start; gap: 12px; margin-top: 10px; }
.reference-preview img { width: 104px; height: 130px; object-fit: contain; background: var(--panel-2); border: 1px solid var(--line); }
.reference-preview .btn-row { display: flex; flex-wrap: wrap; gap: 6px; }
.reference-replace { position: relative; display: inline-flex; align-items: center; cursor: pointer; border: 1px solid var(--line); padding: 6px 10px; }
.reference-replace input { position: absolute; inset: 0; opacity: 0; width: 100%; cursor: pointer; }
.creation-error { color: #a32d2d !important; overflow-wrap: anywhere; }
.generation-action { display: flex; align-items: flex-start; flex-direction: column; gap: 10px; }
.generation-action span { color: var(--fg-3); font-size: 12px; }
.creation-result { border-left: 1px solid var(--line); padding-left: 28px; }
.creation-placeholder { min-height: 210px; display: flex; flex-direction: column; justify-content: center; font-size: 13px; }
.creation-placeholder p { margin-top: 9px; color: var(--fg-3); max-width: 260px; }
.new-candidate { padding-bottom: 20px; margin-bottom: 18px; border-bottom: 1px solid var(--line); }
.new-candidate img { width: 100%; max-height: 300px; object-fit: contain; object-position: left; margin-bottom: 12px; }
.new-candidate h3 { font-size: 15px; }
.new-candidate p { margin: 6px 0 12px; color: var(--fg-2); font-size: 12px; }
.inline-help { color: var(--fg-3); font-size: 12px; margin: 12px 0; line-height: 1.7; }
.variant-list { margin-top: 20px; }
.variant-row, .discarded-row { display: flex; gap: 18px; align-items: center; justify-content: space-between; padding: 16px 0; border-top: 1px solid var(--line); font-size: 12px; overflow-wrap: anywhere; }
.variant-row p, .discarded-row p { margin: 4px 0 0; color: var(--fg-3); }
.variant-row .record-details { margin: 0; }
.motion-current { margin-top: 8px; color: var(--fg-2); }
@media (max-width: 900px) {
  .review-layout { grid-template-columns: 160px minmax(0, 1fr); gap: 20px; }
  .creation-layout { gap: 22px; }
}
@media (max-width: 650px) {
  .avatar-cards { grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); }
  .avatar-photo { height: 300px; }
  .review-section { padding: 16px; }
  .review-layout, .creation-layout { grid-template-columns: 1fr; }
  .review-picture { max-width: 230px; }
  .review-step-body { padding-left: 0; }
  .creation-result { border-left: 0; border-top: 1px solid var(--line); padding: 18px 0 0; }
  .person-section > summary small { display: block; margin: 5px 0 0 16px; }
  .person-actions button { flex: 1; }
}
</style>
