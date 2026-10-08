// 离线模拟实现：不调用任何付费服务、不访问 GPU、不伪造真实媒体产物。
// 仅用于在后端尚未接通时验证交互与状态流转，界面必须显示 MOCK 标记。
import type {
  AdapterStatusView,
  AlignmentRunResult,
  AttestResult,
  AudioAssetInfo,
  AudioGateSummary,
  AvatarAssetView,
  AvatarIdentity,
  AvatarPolicyOptions,
  AvatarVariant,
  Capabilities,
  CostConfirmation,
  CostEstimate,
  DeletionReceipt,
  CostNotice,
  CostScenario,
  FeatureEntry,
  FullAudioResult,
  GenerationRoute,
  GeneratedIdentityCandidate,
  ReferenceImageSource,
  ReferenceImageGenerationInput,
  ReferenceImageGeneration,
  IdentityReadiness,
  IdentityReadinessBrief,
  MaskEstimateResult,
  MaskSpec,
  MotionReferenceView,
  MotionUploadView,
  PlateStatusView,
  PreflightBlocker,
  PublishPackage,
  PublishTaskView,
  PublishSessionView,
  QualifyProgress,
  QualifyStep,
  QueueView,
  RoutePriceView,
  ContentJobReceipt,
  ContentState,
  MediaAssetView,
  ReferenceVisual,
  VisualQuote,
  MediaFetchQuote,
  ReferenceSearchItem,
  ReferenceSnapshot,
  ScriptBatch,
  ScriptVersion,
  WritingPrompt,
  SegmentPlanView,
  SubmissionListView,
  SubmissionResult,
  TalkingHeadCostView,
  TalkingHeadCallList,
  TalkingHeadDriverView,
  TalkingHeadGateSummary,
  TalkingHeadPreflight,
  Task,
  TaskEvent,
  TtsControls,
  VariantComposeResult,
  VideoAsset,
  VoiceAsset,
  VoiceBaselineResult,
  VoiceCatalogStatus,
  VoiceCloneInput,
  VoiceCloneJob,
  VoiceCloneResult,
  VoicePreviewResult,
  Work,
} from './types'
import { ApiError } from './http'
import { controlsError, controlsKey, effectiveTtsControls, normalizeTtsControls } from './tts-controls'

const mockCloneJobs = new Map<string, VoiceCloneJob>()
const deletedWorks = new Map<string, DeletionReceipt>()
const deletedIdentities = new Map<string, DeletionReceipt>()

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms))
const nowIso = () => new Date().toISOString()

let seq = 100
const nextId = (p: string) => `${p}_${++seq}`

function mockCallList(workId: string, route: GenerationRoute, prompt: string, resolution: string): TalkingHeadCallList {
  const work = db.works.find(w => w.id === workId)
  const audio = (db.audios[workId] ?? []).find(a => a.kind === 'full' && a.confirmed)
  const listing = { route, audio_id: audio?.id ?? '', audio_hash: audio?.file_hash ?? '',
    variant_id: work?.variant_id ?? null, motion_ref_id: work?.motion_intent?.motion_ref_id ?? null,
    plate_id: null, prompt: route === 'M2' ? '' : prompt,
    resolution: route === 'M2' ? 'native' : resolution, segment_count: 1 }
  return { ...listing, digest: `mock:${JSON.stringify(listing)}` }
}

/** 用户确认的参照点：单条 60 秒按 1 元/秒 = 6000 分；取整风险含在上限里。 */
function mockScenario(name: string, perDay: number): CostScenario {
  const est = 6000
  return {
    scenario: name,
    estimated_fen: est,
    ceiling_fen: 7200,
    unit_fen: 100,
    per_day_fen: est * perDay,
    monthly_fen: est * perDay * 30,
    excluded: ['TTS', '文本/图片', '存储与后期', '返工重做', '失败可能仍计费'],
    note: 'MOCK 参照点：取整/最低消费/失败扣费待账单核验，实际费用可能高于估价',
  }
}

/**
 * 哪些作品已经有过一次人工路线确认。
 *
 * 作品的 `route` 字段带默认值 `M2`，**不能**拿它判断"选过没有"——
 * 默认值不是授权。离线模式同样要能区分"已选"与"还没选、只是默认指向"，
 * 否则界面在离线演示里就会把没选过显示成选过了。
 */
const routeConfirmedWorks = new Set<string>()

/**
 * 离线模式下的费用提示。金额一律按"没有计费时长就不给数"处理：
 * 离线模式没有真实音频时长，给一个具体金额就是在编。
 */
function mockCostNotice(route: GenerationRoute): CostNotice {
  if (route === 'WAN_3_0') return {
    route, cost_tier: 'high', cost_tier_label: '按生成时长计费', notice_version: 'mock-wan3',
    requires_acknowledgement: true, unit_text: '官方北京原价：480p 0.30、720p 0.60、1080p 1.20 元/生成秒',
    body_motion: { supported: true, scope: '参考图片与完整音频生成创意画面，口型须预览验收', verified: false, verified_note: 'MOCK：无实测结论' },
    estimated_fen: null, ceiling_fen: null, amount_known: false,
    amount_pending_reason: '离线模式没有真实最终音频，不能给出本次金额。',
    risks: ['参考图生成非严格首帧', '人物保持与口型须预览验收', '失败与重做可能产生费用'],
    excluded: ['TTS 合成', '存储与后期', '返工重做'], note: 'MOCK 只展示官方声明，未调用 Wan。',
  }
  const high = route === 'OMNIHUMAN_1_5'
  return {
    route,
    cost_tier: high ? 'high' : 'low',
    cost_tier_label: high ? '高成本 · 含肢体动作' : '低成本 · 头肩口型',
    notice_version: 'mock',
    requires_acknowledgement: high,
    unit_text: high
      ? '按生成秒计费：¥1.00/生成秒（报价，取整/最低消费/失败扣费均未核验）'
      : '无云单价：占用本机 GPU 卡时（实验观测约 ¥0.10/15.2 秒，非生产单价）',
    body_motion: {
      supported: high,
      scope: high
        ? '画面含头肩以外的肢体动作，由模型自由生成，不是逐帧指定'
        : '沿用已审动态底片的动作，路线本身不新增肢体动作与手势',
      verified: false,
      verified_note: 'MOCK：离线模式没有实测结论',
    },
    estimated_fen: null,
    ceiling_fen: null,
    amount_known: false,
    amount_pending_reason: high
      ? '离线模式没有最终音频时长，给不出金额'
      : 'M2 不按生成秒计价：成本以本机 GPU 卡时计入，没有云单价可给',
    risks: high
      ? ['效果不满意仍可能收费', '重生成/返工会再次收费',
         '取整粒度、最低消费、失败是否扣费均未核验']
      : ['不产生云费用，但占用本机 GPU 卡时，单卡串行排队',
         '失败不产生云费用，已占用的 GPU 时间仍计入成本'],
    excluded: high ? ['TTS 合成', '文本与图片', '存储与后期', '返工重做']
                   : ['GPU 卡时', '存储', '后期', '返工'],
    note: 'MOCK 提示：真实金额按音频时长 × 费率计算',
  }
}

interface Db {
  works: Work[]
  currentWorkId: string | null
  identities: AvatarIdentity[]
  variants: AvatarVariant[]
  // 人物图资产（hero/reference/variant）。Mock 里的资产一律 is_mock，
  // 且不带真实哈希——不伪造可核对的产出。
  assets: AvatarAssetView[]
  voices: VoiceAsset[]
  scriptBatches: Record<string, ScriptBatch>
  selected: Record<string, string>
  // 每个作品绑定的试听短句（同一作品必须用同一句，跨音色才可比）
  audition: Record<string, string>
  audios: Record<string, AudioAssetInfo[]>
  tasks: Record<string, Task>
  events: Record<string, TaskEvent[]>
  packages: Record<string, PublishPackage>
  timers: Record<string, number>
  // T05：费用确认按路线分别记录（同一作品可分别确认两条路线的清单）
  costConfirmations: Record<string, Record<string, CostConfirmation>>
}

// 官方预置音色目录（与后端 app/tts/catalog.py 同一份数据，仅用于 mock 演示模式）
const MOCK_CATALOG: [string, string, string, string][] = [
  ['Chinese (Mandarin)_Reliable_Executive', '沉稳高管', '一位沉稳可靠的中年男性高管声音，标准普通话，传递出值得信赖的感觉。', '普通话预置'],
  ['Chinese (Mandarin)_News_Anchor', '新闻女声', '一位专业、播音腔的中年女性新闻主播，标准普通话。', '普通话预置'],
  ['Chinese (Mandarin)_Mature_Woman', '傲娇御姐', '一位妩媚成熟的青年御姐声音，标准普通话。', '普通话预置'],
  ['Chinese (Mandarin)_Unrestrained_Young_Man', '不羁青年', '一位潇洒不羁的青年男性声音，标准普通话。', '普通话预置'],
  ['Chinese (Mandarin)_Kind-hearted_Antie', '热心大婶', '一位温和善良的中年大婶声音，标准普通话，温暖而体贴。', '普通话预置'],
  ['Chinese (Mandarin)_HK_Flight_Attendant', '港普空姐', '一位礼貌的中年女性空乘员声音，带有港式普通话口音，清晰而有礼。', '普通话预置'],
  ['Chinese (Mandarin)_Humorous_Elder', '搞笑大爷', '一位爽朗幽默的老年男性大爷声音，带有北方口音的中文，充满个性。', '普通话预置'],
  ['Chinese (Mandarin)_Gentleman', '温润男声', '一位温润磁性的青年男性声音，标准普通话。', '普通话预置'],
  ['Chinese (Mandarin)_Warm_Bestie', '温暖闺蜜', '一位温暖清脆的青年女性闺蜜声音，标准普通话，友好而清晰。', '普通话预置'],
  ['Chinese (Mandarin)_Male_Announcer', '播报男声', '一位富有磁性的中年男性播报员声音，标准普通话，清晰而权威。', '普通话预置'],
  ['Chinese (Mandarin)_Sweet_Lady', '甜美女声', '一位温柔甜美的青年女性声音，标准普通话。', '普通话预置'],
  ['Chinese (Mandarin)_Southern_Young_Man', '南方小哥', '一位质朴的青年男性声音，带有南方口音的中文。', '普通话预置'],
  ['Chinese (Mandarin)_Wise_Women', '阅历姐姐', '一位富有阅历、声音抒情的中年姐姐声音，标准普通话。', '普通话预置'],
  ['Chinese (Mandarin)_Gentle_Youth', '温润青年', '一位温柔的青年男性声音，标准普通话。', '普通话预置'],
  ['Chinese (Mandarin)_Warm_Girl', '温暖少女', '一位温柔温暖的少年女声，标准普通话。', '普通话预置'],
  ['Chinese (Mandarin)_Kind-hearted_Elder', '花甲奶奶', '一位慈祥和蔼的老年女性奶奶声音，标准普通话。', '普通话预置'],
  ['Chinese (Mandarin)_Cute_Spirit', '憨憨萌兽', '一位呆萌可爱的少年男声，适合憨厚的萌兽角色。', '普通话预置'],
  ['Chinese (Mandarin)_Radio_Host', '电台男主播', '一位富有诗意的青年男性电台主播声音，标准普通话，声音流畅引人入胜。', '普通话预置'],
  ['Chinese (Mandarin)_Lyrical_Voice', '抒情男声', '一位磁性抒情的青年男性声音，标准普通话，流畅而富有表现力。', '普通话预置'],
  ['Chinese (Mandarin)_Straightforward_Boy', '率真弟弟', '一位认真率真的少年弟弟声音，标准普通话。', '普通话预置'],
  ['Chinese (Mandarin)_Sincere_Adult', '真诚青年', '一位真诚、富有鼓励性的青年男性声音，标准普通话。', '普通话预置'],
  ['Chinese (Mandarin)_Gentle_Senior', '温柔学姐', '一位温暖温柔的青年学姐声音，标准普通话。', '普通话预置'],
  ['Chinese (Mandarin)_Stubborn_Friend', '嘴硬竹马', '一位嘴硬心软、不羁的青年竹马声音，标准普通话。', '普通话预置'],
  ['Chinese (Mandarin)_Crisp_Girl', '清脆少女', '一位温暖清脆的少女声音，标准普通话。', '普通话预置'],
  ['Chinese (Mandarin)_Pure-hearted_Boy', '清澈邻家弟弟', '一位认真清澈的邻家少年弟弟声音，标准普通话。', '普通话预置'],
  ['Chinese (Mandarin)_Soft_Girl', '软软女孩', '一位温暖柔软的青年女性声音，带有南方口音的中文。', '普通话预置'],
  ['danya_xuejie', '淡雅学姐', '经典中文音色；本项目 AV-001 首轮实测过同稿试听（音色 2）。', '经典中文'],
  ['female-yujie', '御姐音色', '经典中文女声，成熟御姐气质。', '经典中文'],
  ['female-chengshu', '成熟女性音色', '经典中文女声，成熟稳重。', '经典中文'],
  ['female-tianmei', '甜美女性音色', '经典中文女声，甜美亲和。', '经典中文'],
  ['female-shaonv', '少女音色', '经典中文女声，清亮少女感。', '经典中文'],
  ['wumei_yujie', '妩媚御姐', '经典中文女声，妩媚成熟。', '经典中文'],
  ['qiaopi_mengmei', '俏皮萌妹', '经典中文女声，俏皮活泼。', '经典中文'],
  ['tianxin_xiaoling', '甜心小玲', '经典中文女声，甜美俏皮。', '经典中文'],
  ['diadia_xuemei', '嗲嗲学妹', '经典中文女声，软糯亲和。', '经典中文'],
  ['Arrogant_Miss', '嚣张小姐', '一位嚣张自信的青年女性声音，标准普通话，展现出优越感。', '经典中文'],
  ['male-qn-jingying', '精英青年音色', '经典中文男声，干练精英感。', '经典中文'],
  ['male-qn-qingse', '青涩青年音色', '经典中文男声，青涩年轻。', '经典中文'],
  ['male-qn-daxuesheng', '青年大学生音色', '经典中文男声，大学生气质。', '经典中文'],
  ['male-qn-badao', '霸道青年音色', '经典中文男声，低沉有力度。', '经典中文'],
  ['junlang_nanyou', '俊朗男友', '经典中文男声，温柔俊朗。', '经典中文'],
  ['chunzhen_xuedi', '纯真学弟', '经典中文男声，干净纯真。', '经典中文'],
  ['lengdan_xiongzhang', '冷淡学长', '经典中文男声，冷静克制。', '经典中文'],
  ['badao_shaoye', '霸道少爷', '经典中文男声，强势有气场。', '经典中文'],
  ['bingjiao_didi', '病娇弟弟', '经典中文男声，黏人少年感。', '经典中文'],
]

const db: Db = {
  works: [],
  currentWorkId: null,
  identities: [
    {
      id: 'av_base_b_v1',
      name: '人物 B',
      persona: '约 22 岁成年女性，专业、亲和、自然；正面半身构图',
      hero_asset_id: 'img_hero_b_v1',
      identity_digest: 'sha256:mock-identity-b-v1',
      default_voice_asset_id: 'voice_minimax_warm_bestie_v1',
      approval_state: 'approved',
      is_preset: true,
      note: '本项目已选初始预设；原图 941×1672，参考集与变体仍待补齐验收',
    },
  ],
  variants: [],
  assets: [
    {
      id: 'img_hero_b_v1',
      identity_id: 'av_base_b_v1',
      kind: 'hero',
      version: 1,
      storage_uri: 'assets/固定主播/主播B/版本1/host_b.png',
      file_hash: '',
      width: 941,
      height: 1672,
      license_id: null,
      is_mock: true,
      approval_state: 'pending',
      note: 'MOCK 行：真实哈希与审核结论只在真实后端读得到',
    },
  ],
  voices: [
    {
      id: 'voice_minimax_warm_bestie_v1',
      name: '音色 3 · Warm Bestie',
      provider: 'minimax',
      model: 'speech-2.8-hd',
      remote_voice_id: 'Chinese (Mandarin)_Warm_Bestie',
      approval_state: 'approved',
      is_preset: true,
      sample_text: '大家好，今天我们用三分钟讲清楚一个容易被忽略的细节。',
      description: '一位温暖清脆的青年女性闺蜜声音，标准普通话，友好而清晰。',
      catalog_group: '普通话预置',
      supported_emotions: ['calm'],
      note: '已选初始预设；其他情绪、多稿件与长稿仍待验收',
    },
    {
      id: 'voice_minimax_calm_v1',
      name: '音色 2 · Calm（候选）',
      provider: 'minimax',
      model: 'speech-2.8-hd',
      remote_voice_id: 'Chinese (Mandarin)_Calm_Woman',
      approval_state: 'pending',
      is_preset: false,
      sample_text: '大家好，今天我们用三分钟讲清楚一个容易被忽略的细节。',
      description: '历史候选；remote_voice_id 不在官方 303 条目录内，来源待复核。',
      catalog_group: '其他',
      supported_emotions: ['calm'],
      note: '候选音色，尚未完成基准试听与审批',
    },
    ...MOCK_CATALOG.map(([remote, name, tone, group]) => ({
      id: `voice_minimax_${remote.replace(/^Chinese \(Mandarin\)_/, '')
        .replace(/[^0-9a-zA-Z]+/g, '_').replace(/^_+|_+$/g, '').toLowerCase()}_v1`,
      name,
      provider: 'minimax',
      model: 'speech-2.8-hd',
      remote_voice_id: remote,
      approval_state: 'pending' as const,
      is_preset: true,
      sample_text: '大家好，今天我们用三分钟讲清楚一个容易被忽略的细节。',
      description: tone,
      catalog_group: group,
      supported_emotions: ['calm'],
      note: '官方预置音色候选；可试听，选定后由用户显式批准',
    })).filter((v) => v.id !== 'voice_minimax_warm_bestie_v1'),
  ],
  scriptBatches: {},
  selected: {},
  audition: {},
  audios: {},
  tasks: {},
  events: {},
  packages: {},
  timers: {},
  costConfirmations: {},
}

const STAGE_ORDER = [
  'topic',
  'script',
  'content_review',
  'avatar_variant',
  'tts',
  'talking_head',
  'postprocess',
  'video_review',
  'publish_package',
] as const

const STAGE_LABEL: Record<string, string> = {
  topic: '确定选题与来源',
  script: '三角度写稿与表演说明',
  content_review: '内容门禁与查重',
  avatar_variant: '人物与表演准备',
  tts: '试听选择与全文合成',
  talking_head: '口播生成',
  postprocess: '字幕、封面与编码',
  video_review: '成片终审',
  publish_package: '冻结发布包',
}

function stageRows(current: string, state: string) {
  const idx = STAGE_ORDER.indexOf(current as any)
  return STAGE_ORDER.map((s, i) => {
    let st: 'pending' | 'active' | 'done' | 'blocked' | 'failed' = 'pending'
    if (state === 'failed' && i === idx) st = 'failed'
    else if (state === 'blocked' && i === idx) st = 'blocked'
    else if (i < idx) st = 'done'
    else if (i === idx) st = state === 'succeeded' ? 'done' : 'active'
    return { stage: s, label: STAGE_LABEL[s], state: st, detail: '' }
  })
}

function pushEvent(taskId: string, type: string, message: string, extra: Partial<TaskEvent> = {}) {
  const list = (db.events[taskId] ||= [])
  const e: TaskEvent = {
    seq: list.length + 1,
    type,
    occurred_at: nowIso(),
    stage: null,
    state: null,
    message,
    ...extra,
  }
  list.push(e)
  return e
}

function makeTask(workId: string, route: GenerationRoute): Task {
  const t: Task = {
    id: nextId('task'),
    work_id: workId,
    mode: 'mock',
    stage: 'topic',
    state: 'queued',
    route,
    route_confirmed: false,
    percent: null,
    progress_source: 'indeterminate',
    pending_selection_ids: [],
    block_reason: null,
    last_error: null,
    stage_rows: stageRows('topic', 'queued'),
    created_at: nowIso(),
  }
  db.tasks[t.id] = t
  db.events[t.id] = []
  pushEvent(t.id, 'task.created', '任务已创建，等待编排', { state: 'queued' })
  const w = db.works.find((x) => x.id === workId)
  if (w) w.current_task_id = t.id
  return t
}

// 模拟编排推进：遇到必须由用户确认的门禁即停住等待。
function startEngine(taskId: string) {
  const step = () => {
    const t = db.tasks[taskId]
    if (!t) return
    if (t.state === 'cancelled' || t.state === 'failed' || t.state === 'blocked') return

    if (t.stage === 'topic') {
      t.stage = 'script'
      t.state = 'running'
      pushEvent(taskId, 'stage.started', '开始资料检索与三角度写稿', { stage: 'script', state: 'running' })
      schedule(taskId, 1600)
      return
    }
    if (t.stage === 'script') {
      t.stage = 'content_review'
      pushEvent(taskId, 'stage.started', '逐稿执行事实、查重与口语检查', {
        stage: 'content_review',
        state: 'running',
      })
      schedule(taskId, 1800)
      return
    }
    if (t.stage === 'content_review') {
      t.state = 'waiting_selection'
      t.pending_selection_ids = ['sel_script']
      pushEvent(taskId, 'selection.requested', '三稿已就绪，等待用户选稿', {
        stage: 'content_review',
        state: 'waiting_selection',
      })
      return
    }
    if (t.stage === 'avatar_variant') {
      t.stage = 'tts'
      pushEvent(taskId, 'stage.started', '声音、人物与剪辑准备并行推进', { stage: 'tts', state: 'running' })
      schedule(taskId, 1600)
      return
    }
    if (t.stage === 'tts') {
      t.state = 'waiting_selection'
      t.pending_selection_ids = ['sel_audio']
      pushEvent(taskId, 'selection.requested', '全文音频已合成，等待用户试听确认', {
        stage: 'tts',
        state: 'waiting_selection',
      })
      return
    }
    if (t.stage === 'talking_head') {
      t.state = 'waiting_provider'
      pushEvent(taskId, 'provider.submitted', `${t.route} 路线已提交，等待外部作业`, {
        stage: 'talking_head',
        state: 'waiting_provider',
      })
      schedule(taskId, 2600)
      return
    }
    if (t.stage === 'postprocess') {
      t.stage = 'video_review'
      pushEvent(taskId, 'stage.started', '成片技术检测与人工观感复核', {
        stage: 'video_review',
        state: 'running',
      })
      schedule(taskId, 1500)
      return
    }
    if (t.stage === 'video_review') {
      t.stage = 'publish_package'
      pushEvent(taskId, 'stage.started', '冻结交付版本并生成 manifest', {
        stage: 'publish_package',
        state: 'running',
      })
      schedule(taskId, 1500)
      return
    }
    if (t.stage === 'publish_package') {
      t.state = 'succeeded'
      t.percent = 100
      pushEvent(taskId, 'package.ready', '发布就绪包已生成，可预览与下载', {
        stage: 'publish_package',
        state: 'succeeded',
      })
      buildPackage(t)
      return
    }
  }
  const schedule = (id: string, ms: number) => {
    clearTimeout(db.timers[id])
    db.timers[id] = window.setTimeout(step, ms)
  }
  schedule(taskId, 900)
}

function buildPackage(t: Task) {
  const w = db.works.find((x) => x.id === t.work_id)
  const batch = db.scriptBatches[t.work_id]
  const chosen = batch?.candidates.find((c) => c.id === db.selected['sel_script'])
  db.packages[t.work_id] = {
    id: nextId('pkg'),
    work_id: t.work_id,
    task_id: t.id,
    mode: 'mock',
    state: 'ready',
    title: chosen?.title ?? (w?.name ?? '未命名作品'),
    description: chosen?.description ?? '',
    tags: chosen?.tags ?? [],
    ai_label: '本片人物与声音由 AI 生成（AI 标识规范版本待投产前核对）',
    files: [
      { path: 'video.mp4', bytes: 0, sha256: 'mock' },
      { path: 'cover.jpg', bytes: 0, sha256: 'mock' },
      { path: 'subtitles.srt', bytes: 0, sha256: 'mock' },
      { path: 'title.txt', bytes: 0, sha256: 'mock' },
      { path: 'description.txt', bytes: 0, sha256: 'mock' },
      { path: 'tags.txt', bytes: 0, sha256: 'mock' },
      { path: 'ai_label.txt', bytes: 0, sha256: 'mock' },
      { path: 'manifest.json', bytes: 0, sha256: 'mock' },
    ],
    video_url: null,
    cover_url: null,
    download_url: `/api/v1/publish-packages/${nextId('pkg')}/download`,
    manifest_digest: 'sha256:mock-package-digest',
  }
}

function makeCandidates(topic: string): ScriptVersion[] {
  const base = topic || 'RAG 如何补充模型回答所需的信息'
  const mk = (
    angle: string,
    title: string,
    text: string,
    failCode?: string,
  ): ScriptVersion => ({
    id: nextId('script'),
    angle,
    title,
    description: `${title}｜围绕“${base}”的一个独立切入角度`,
    tags: ['AI科普', 'RAG', '实用技巧'],
    text,
    duration_estimate_ms: 58000,
    quality_report: {
      checks: [
        { code: 'FACT_SUPPORT', outcome: 'pass', hard_gate: true, message: '关键断言均可定位到已存档来源' },
        { code: 'DEDUP_TOPIC', outcome: 'pass', hard_gate: true, message: '与历史主题不重复' },
        { code: 'DEDUP_VIEWPOINT', outcome: 'pass', hard_gate: true, message: '观点层未命中重复' },
        { code: 'DEDUP_TEXT', outcome: 'pass', hard_gate: true, message: '文本层相似度低于阈值' },
        { code: 'TYPO', outcome: 'pass', hard_gate: true, message: '未发现错别字与术语误写' },
        { code: 'ORAL_FLUENCY', outcome: 'pass', hard_gate: false, message: '口语流畅度良好' },
        {
          code: failCode ?? 'HOOK_STRENGTH',
          outcome: failCode ? 'fail' : 'pass',
          hard_gate: !!failCode,
          message: failCode ? '存在硬门禁失败，需要重写或人工复核' : '开头 3 秒提出了具体问题',
        },
      ],
      overall: failCode ? 'fail' : 'pass',
      review_required: !!failCode,
    },
    claims: [
      { id: nextId('claim'), text: 'RAG 先检索外部资料再生成答案', verdict: 'supported' },
      { id: nextId('claim'), text: '检索质量决定最终回答质量', verdict: 'supported' },
    ],
  })

  return [
    mk(
      '生活类比',
      '为什么 AI 读了你的文件，还是答错了',
      '开头：你有没有遇到过，把资料丢给 AI，它却答非所问？\n\n解释：问题往往不在模型，而在检索。RAG 先去找资料，再让模型读资料作答。找错了资料，后面再聪明也没用。\n\n例子：同一份文档，切片太大，答案就被无关段落稀释；切片太小，又丢了上下文。\n\n边界：RAG 不能凭空补全文档里没有的信息。\n\n收尾：下次遇到答错，先问一句：它到底检索到了什么。',
    ),
    mk(
      '常见误区',
      '三个最常见的 RAG 误区',
      '开头：很多人以为接了向量库就是 RAG，其实只完成了一半。\n\n解释：误区一是只看相似度分数；误区二是切片一刀切；误区三是忽略时效性。\n\n例子：把一年前的价格当成今天的报价，就是时效没管好。\n\n边界：本文只讲检索环节，不讨论模型微调。\n\n收尾：把这三件事做对，效果往往比换模型更明显。',
    ),
    mk(
      '实战案例',
      '一次把回答准确率救回来的排查过程',
      '开头：一条问答准确率只有四成，我们花了两小时定位到根因。\n\n解释：先查召回，再查切片，最后查提示词，顺序不能反。\n\n例子：召回阶段命中率只有 55%，把切片从 1200 字改成 400 字后提升到 88%。\n\n边界：这个数字只属于当时的语料，不能外推。\n\n收尾：先量再改，别凭感觉调参数。',
      'HOOK_STRENGTH',
    ),
  ]
}

/** 演示模式下"还缺什么"的判定。与后端 `production_ready` 的对外口径保持一致，
 *  免得演示模式说"可以了"、真实模式却拦住。 */
function mockIdentityReasons(i: AvatarIdentity | undefined): string[] {
  const reasons: string[] = []
  if (!i || i.approval_state !== 'approved') reasons.push('人物未定版')
  if (i && !i.frozen) reasons.push('身份未冻结')
  if (!i?.reference_policy) reasons.push('参考集政策未声明：须先由人声明走多角度还是受限路径')
  if (!i?.license_id) reasons.push('身份缺少权属记录，商业使用范围不可追溯')
  return reasons
}

/**
 * 演示模式下复刻后端的定版四步进度。步骤文案与真实后端逐字一致——
 * 演示数据说一套、真后端说另一套，会让人按演示去点真实界面，然后被拒。
 */
function mockQualifyProgress(i: AvatarIdentity | undefined): QualifyProgress {
  const hero = db.assets.find((a) => a.identity_id === i?.id && a.kind === 'hero')
  const reviewed = !!hero && !hero.is_mock && hero.approval_state === 'approved'
  const policy = i?.reference_policy
  const policyOk = !!policy && policy.mode !== 'hero_only'
  const attested = !!i?.license_id
  const frozen = !!i?.frozen && i?.approval_state === 'approved'
  const steps: QualifyStep[] = [
    {
      key: 'review',
      label: '原片身份审核',
      done: reviewed,
      why: '系统不做人脸比对，只能由人目检原图并留下核对方法；结论绑定这张图的哈希，换图即失效。',
    },
    {
      key: 'reference_policy',
      label: '声明参考依据口径',
      done: policyOk,
      why: '定版前必须定下「拿什么当是同一个人」。定版后不许再改，所以只能现在声明。',
    },
    {
      key: 'license_attest',
      label: '商用范围确认',
      done: attested,
      why: '由人确认这一版权属记录可用于商用并写明依据；系统不做法律判断。',
    },
    {
      key: 'freeze',
      label: '冻结定版',
      done: frozen,
      why: '把这一版焊死：原图不可替换，此后任何作品都能直接选它；要换人只能新建版本。',
    },
  ]
  const doneCount = steps.filter((s) => s.done).length
  return {
    steps,
    done_count: doneCount,
    total: steps.length,
    current: frozen ? null : steps.find((s) => !s.done)?.key ?? null,
    frozen,
    closed_incomplete: frozen && doneCount !== steps.length,
  }
}

// ---- 口播稿升级的本地演示库：全部是模拟数据，绝不冒充真实供应商结果，也不可用于生产 ----
const mockContent: Record<string, {
  revision: number
  references: ReferenceSnapshot[]
  referenceId: string | null
  prompt: WritingPrompt | null
  jobs: Record<string, ContentJobReceipt>
  byRequest: Record<string, string>
  media: Record<string, MediaAssetView>
}> = {}

function mc(workId: string) {
  return (mockContent[workId] ??= { revision: 0, references: [], referenceId: null, prompt: null, jobs: {}, byRequest: {}, media: {} })
}

function mockJob(workId: string, kind: string, requestId: string, result: Record<string, any>, error: ContentJobReceipt['error'] = null): ContentJobReceipt {
  const c = mc(workId)
  const key = `${kind}:${requestId}`
  const prior = c.byRequest[key]
  if (prior) return { ...c.jobs[prior], replayed: true }
  const id = nextId('cjob')
  const job: ContentJobReceipt = {
    job_id: id, kind, state: error ? 'failed' : 'succeeded', stage: 'done', replayed: false,
    poll_url: `/api/v1/works/${workId}/content/jobs/${id}`, result_refs: result, error, content_revision: c.revision,
  }
  c.jobs[id] = job
  c.byRequest[key] = id
  return job
}

const MOCK_ITEMS = (query: string): ReferenceSearchItem[] => [1, 2, 3].map((n) => ({
  platform_work_id: `mock_${n}`, title: '', caption: `【演示数据】关于「${query}」的示例参考 ${n}`, author: '演示作者',
  cover_url: null, source_url: `https://www.douyin.com/video/mock_${n}`,
  metrics: { likes: n === 3 ? null : 1000 * (4 - n), comments: 100 * n, shares: null, collects: 50 * n },
  published_at: '2026-09-2' + n + ' 10:00:00', duration_ms: 30000, media_kind: 'video' as const, transcribable: true,
  fetched_at: new Date().toISOString(),
}))

export const mockApi = {
  async listAudios(workId: string) { return structuredClone(db.audios[workId] ?? []) },
  async getPostprocess() { return null },
  async getCapabilities(): Promise<Capabilities> {
    await wait(120)
    return {
      mode: 'mock',
      routes: [
        { route: 'M2', label: 'M2 · LivePortrait → MuseTalk 1.5', state: 'developing', note: '默认路线；长片与日产 5 条待验收' },
        { route: 'OMNIHUMAN_1_5', label: 'OmniHuman 1.5 · 自定义表演', state: 'planned', note: '需显式选择并确认预算；当前人物与动作待验收' },
      ],
      providers: [
        { provider: 'deepseek', purpose: '文本决策', model: 'deepseek-flash', validation_state: 'unverified', detail: '已选未调用；工具调用兼容性未实测' },
        { provider: 'gpt-image-2', purpose: '人像生成与编辑', model: 'gpt-image-2.5-sunburst', validation_state: 'unverified', detail: '已生成并选定人物 B；参考编辑未验' },
        { provider: 'minimax', purpose: 'TTS', model: 'speech-2.8-hd', validation_state: 'unverified', detail: '音色 3 已定版；多稿件与情绪待验' },
        { provider: 'm2_worker', purpose: '口播推理', model: 'LivePortrait+MuseTalk1.5', validation_state: 'unverified', detail: '仅有 15.20 秒实验样片' },
      ],
      budget: { per_task_limit_fen: 0, daily_limit_fen: 0, max_billable_submissions: 0 },
    }
  },

  async getFeatures(): Promise<FeatureEntry[]> {
    await wait(80)
    return [
      { feature_id: 'avatar', title: '人物形象', feature_state: 'available', reason: '可浏览人物库、生成候选与衣景变体，收集表演要求', available_actions: ['browse', 'generate_candidate', 'create_variant'] },
      { feature_id: 'script', title: '口播稿生成', feature_state: 'available', reason: '三角度候选、逐稿审核与用户选稿', available_actions: ['generate', 'select', 'revise'] },
      { feature_id: 'tts', title: 'TTS 语音', feature_state: 'available', reason: '短句试听、全文合成与用户确认', available_actions: ['preview', 'synthesize', 'confirm'] },
      { feature_id: 'video', title: '视频合成', feature_state: 'available', reason: '路线选择、估价确认、口播生成与下载', available_actions: ['select_route', 'confirm_cost', 'submit', 'download'] },
      { feature_id: 'publish', title: '发布', feature_state: 'planned', reason: 'V1 只交付可下载就绪包；平台发布需逐平台取得权限后独立验收', available_actions: [] },
      { feature_id: 'metrics', title: '指标监控', feature_state: 'planned', reason: '未接入平台数据权限，不展示任何估算或补零数据', available_actions: [] },
      { feature_id: 'models', title: '领域模型选择', feature_state: 'developing', reason: 'V1 仅提供服务端基础配置状态与脱敏报错；可编辑配置页后续开放', available_actions: ['view_status'] },
    ]
  },

  async listWorks(): Promise<Work[]> {
    await wait(80)
    return db.works.filter(work => !deletedWorks.has(work.id)).map((w) => ({ ...w }))
  },

  async deleteWork(workId: string): Promise<DeletionReceipt> {
    await wait(150)
    const previous = deletedWorks.get(workId)
    if (previous) return { ...previous }
    const work = db.works.find(item => item.id === workId)
    if (!work) throw new ApiError('WORK_NOT_FOUND', '作品不存在', false, null, 404)
    if (Object.values(db.tasks).some(task => task.work_id === workId && ['queued', 'running', 'waiting_provider', 'unknown'].includes(task.state))) {
      throw new ApiError('WORK_DELETE_BLOCKED', '作品仍有运行中或结果未知的任务，请先处理任务', false, null, 409)
    }
    const receipt = { id: workId, deleted_at: nowIso() }
    deletedWorks.set(workId, receipt)
    if (db.currentWorkId === workId) db.currentWorkId = null
    return { ...receipt }
  },

  async deleteIdentity(identityId: string): Promise<DeletionReceipt> {
    await wait(150)
    const previous = deletedIdentities.get(identityId)
    if (previous) return { ...previous }
    if (!db.identities.some(item => item.id === identityId)) throw new ApiError('IDENTITY_NOT_FOUND', '人物不存在', false, null, 404)
    const variants = new Set(db.variants.filter(variant => variant.identity_id === identityId).map(variant => variant.id))
    const references = db.works.filter(work => !deletedWorks.has(work.id) && (work.identity_id === identityId || !!work.variant_id && variants.has(work.variant_id)))
    if (references.length) throw new ApiError('IDENTITY_DELETE_BLOCKED', `人物仍被作品引用：${references.map(work => `「${work.name}」`).join('、')}，请先删除对应作品`, false, null, 409)
    const receipt = { id: identityId, deleted_at: nowIso() }
    deletedIdentities.set(identityId, receipt)
    return { ...receipt }
  },

  async createWork(input: {
    name: string
    topic: string
    content_type: string
    audience: string
    keywords: string[]
    target_duration_ms: number
  }): Promise<Work> {
    await wait(200)
    const w: Work = {
      id: nextId('work'),
      name: input.name || '未命名作品',
      script_version_id: null,
      brief: {
        topic: input.topic,
        content_type: input.content_type,
        audience: input.audience,
        keywords: input.keywords,
        target_duration_ms: input.target_duration_ms,
      },
      identity_id: deletedIdentities.has('av_base_b_v1') ? null : 'av_base_b_v1',
      voice_asset_id: 'voice_minimax_warm_bestie_v1',
      route: 'M2',
      current_task_id: null,
      version: 1,
      created_at: nowIso(),
    }
    db.works.push(w)
    db.currentWorkId = w.id
    return { ...w }
  },

  async listIdentities(includeDiscarded = false): Promise<AvatarIdentity[]> {
    await wait(80)
    const rows = db.identities.filter(identity => !deletedIdentities.has(identity.id) && (includeDiscarded || !identity.discarded_at))
    return rows.map((x) => ({ ...x }))
  },

  async copyIdentityDraft(_identityId: string): Promise<AvatarIdentity> {
    throw new ApiError('REAL_HERO_REQUIRED', '模拟模式没有可验证的真实原图，请连接后端后复制草稿')
  },

  async identitiesReadiness(includeDiscarded = false): Promise<Record<string, IdentityReadinessBrief>> {
    await wait(80)
    const out: Record<string, IdentityReadinessBrief> = {}
    for (const i of db.identities) {
      if (deletedIdentities.has(i.id)) continue
      if (i.discarded_at && !includeDiscarded) continue
      const reasons = mockIdentityReasons(i)
      out[i.id] = {
        ready: reasons.length === 0,
        reasons,
        limited_reference: i.reference_policy?.mode === 'hero_only',
        qualify: mockQualifyProgress(i),
      }
    }
    return out
  },

  /** 丢弃 ≠ 删除：与后端同规则——已定版、或被作品/变体引用的一律拒绝。 */
  async discardIdentity(identityId: string, reason: string): Promise<AvatarIdentity> {
    await wait(150)
    if (deletedIdentities.has(identityId)) throw new ApiError('IDENTITY_DELETED', '人物已删除')
    const i = db.identities.find((x) => x.id === identityId)
    if (!i) throw new ApiError('IDENTITY_NOT_FOUND', `人物不存在：${identityId}`)
    if (!reason.trim()) throw new ApiError('REASON_REQUIRED', '丢弃须写明理由，便于日后回溯')
    if (i.discarded_at) return { ...i }
    if (i.approval_state === 'approved' || i.frozen) {
      throw new ApiError('IDENTITY_NOT_DISCARDABLE', '身份已定版：换人须新建版本，不能丢弃')
    }
    const users = db.works.filter((w) => w.identity_id === identityId && !deletedWorks.has(w.id))
    if (users.length) {
      throw new ApiError(
        'IDENTITY_NOT_DISCARDABLE',
        `已被 ${users.length} 个作品引用：${users.map((w) => w.id).join('、')}`,
      )
    }
    const vars = db.variants.filter((v) => v.identity_id === identityId)
    if (vars.length) {
      throw new ApiError(
        'IDENTITY_NOT_DISCARDABLE',
        `已有 ${vars.length} 条变体建立在它之上，丢弃会让变体失去归属`,
      )
    }
    const at = nowIso()
    i.discarded_at = at
    i.discarded_by = 'user_local'
    i.discard_reason = reason.trim()
    i.discard_history = [...(i.discard_history ?? []), { action: 'discard', actor: 'user_local', at, reason: reason.trim() }]
    return { ...i }
  },

  async restoreIdentity(identityId: string): Promise<AvatarIdentity> {
    await wait(150)
    if (deletedIdentities.has(identityId)) throw new ApiError('IDENTITY_DELETED', '人物已删除')
    const i = db.identities.find((x) => x.id === identityId)
    if (!i) throw new ApiError('IDENTITY_NOT_FOUND', `人物不存在：${identityId}`)
    if (!i.discarded_at) return { ...i }
    const at = nowIso()
    i.discard_history = [
      ...(i.discard_history ?? []),
      { action: 'restore', actor: 'user_local', at, reason: `恢复（原丢弃理由：${i.discard_reason || '未记录'}）` },
    ]
    i.discarded_at = null
    i.discarded_by = ''
    i.discard_reason = ''
    return { ...i }
  },

  async uploadAvatarReferenceImage(_file: File): Promise<ReferenceImageSource> {
    throw new ApiError('AVATAR_REFERENCE_UNAVAILABLE', '离线模式不支持参考图上传，请连接后端。')
  },
  async createIdentityCandidateFromImage(_input: ReferenceImageGenerationInput): Promise<GeneratedIdentityCandidate> {
    throw new ApiError('AVATAR_REFERENCE_UNAVAILABLE', '离线模式不支持参考图生成，请连接后端。')
  },
  async getAvatarImageGeneration(_requestId: string): Promise<ReferenceImageGeneration> {
    throw new ApiError('AVATAR_REFERENCE_UNAVAILABLE', '离线模式无法查询图片生成作业，请连接后端。')
  },

  async createIdentityCandidates(input: {
    gender: string
    age: string
    appearance: string
    free_text: string
  }): Promise<AvatarIdentity[]> {
    await wait(900)
    const id: AvatarIdentity = {
      id: nextId('av'),
      name: `候选 ${input.gender || '未指定'}·${input.age || '成年'}`,
      persona: [input.appearance, input.free_text].filter(Boolean).join('；') || '未填写描述',
      hero_asset_id: nextId('img'),
      identity_digest: 'sha256:mock-candidate',
      default_voice_asset_id: null,
      approval_state: 'pending',
      is_preset: false,
      note: 'MOCK 候选：未调用生图服务，需真实生成并审核后才能定版',
    }
    db.identities.push(id)
    return [id]
  },

  /**
   * Mock 版"真实出图"。
   *
   * 红线：Mock 不得伪造媒体。这里**不给图片**，也声明钱包混杂：
   * image_url 为空、asset.is_mock 为 true、storage_uri 为空，
   * 界面上一眼能看出"这不是真的图"，不会把占位当成已生成的产物。
   */
  async createIdentityCandidateWithImage(input: {
    gender: string
    age: string
    appearance: string
    free_text: string
  }): Promise<GeneratedIdentityCandidate> {
    await wait(1200)
    const identityId = nextId('av')
    const assetId = nextId('img')
    const identity: AvatarIdentity = {
      id: identityId,
      name: `候选 ${input.gender || '未指定'}·${input.age || '成年'}`,
      persona: [input.appearance, input.free_text].filter(Boolean).join('；') || '未填写描述',
      hero_asset_id: '',
      identity_digest: '',
      default_voice_asset_id: null,
      approval_state: 'pending',
      is_preset: false,
      note: 'MOCK 候选：未调用生图服务，没有真实图片',
    }
    db.identities.push(identity)
    const mockAsset: AvatarAssetView = {
      id: assetId,
      identity_id: identityId,
      kind: 'hero',
      version: 1,
      storage_uri: '',
      file_hash: '',
      width: null,
      height: null,
      license_id: null,
      is_mock: true,
      approval_state: 'pending',
      note: 'MOCK 资产：没有真实图片',
    }
    db.assets.push(mockAsset)
    return {
      identity,
      asset: mockAsset,
      prompt: '',
      request_digest: 'mock-request-digest（未发起真实请求）',
      usage: {},
      image_url: '',
      warnings: [
        'MOCK 模式未调用任何生图服务，这里没有图片',
        '真实出图需要配置图像生成凭据并切换到真实后端',
      ],
    }
  },

  async selectIdentity(workId: string, identityId: string): Promise<Work> {
    await wait(150)
    if (deletedWorks.has(workId)) throw new ApiError('WORK_DELETED', '作品已删除')
    if (deletedIdentities.has(identityId)) throw new ApiError('IDENTITY_DELETED', '人物已删除')
    const w = db.works.find((x) => x.id === workId)!
    w.identity_id = identityId
    db.selected['sel_avatar'] = identityId
    return { ...w }
  },

  // MOCK 下的定版链：只演示状态流转，不伪造任何审核结论或权属判定。
  // 判定的责任在人，Mock 层不替人做结论，也不允许把 Mock 结果当成已核验。
  async avatarPolicyOptions(): Promise<AvatarPolicyOptions> {
    await wait(60)
    return {
      reference_policies: [
        {
          mode: 'multi_view',
          label: '多角度参考集',
          summary: '有多张经审核的参考图可交叉验证；AC-AV-01 要求至少 4 张。',
          requires_acknowledgement: false,
        },
        {
          mode: 'hero_only',
          label: '受限路径（仅原图为锚）',
          summary: 'MOCK 文案：真实措辞以后端返回为准，前端不得另写一份。',
          requires_acknowledgement: true,
        },
      ],
      hero_only_limitations: ['MOCK：限制项原文由后端给出'],
      attest_token: 'COMMERCIAL_ATTEST_V1',
      attest_note: 'MOCK：由人确认商用范围，非法律意见',
    }
  },

  async identityReadiness(identityId: string): Promise<IdentityReadiness> {
    await wait(120)
    const i = db.identities.find((x) => x.id === identityId)
    const reasons = mockIdentityReasons(i)
    return {
      identity_id: identityId,
      ready: reasons.length === 0,
      reasons,
      reference_policy: i?.reference_policy ?? null,
      limited_reference: i?.reference_policy?.mode === 'hero_only',
      qualify: mockQualifyProgress(i),
      gate: 'AC-AV-01',
      checked_at: new Date().toISOString(),
    }
  },

  async listIdentityAssets(identityId: string, kind?: string): Promise<AvatarAssetView[]> {
    await wait(100)
    const rows = db.assets.filter((a) => a.identity_id === identityId)
    return (kind ? rows.filter((a) => a.kind === kind) : rows).map((a) => ({ ...a }))
  },

  async reviewIdentityAsset(
    identityId: string,
    assetId: string,
    input: { outcome: 'pass' | 'reject' | 'review'; identity_digest: string; method: string; note?: string },
  ): Promise<Record<string, unknown>> {
    await wait(200)
    const a = db.assets.find((x) => x.id === assetId)
    if (a) a.approval_state = input.outcome === 'pass' ? 'approved' : 'pending'
    return {
      asset_id: assetId,
      identity_id: identityId,
      outcome: input.outcome,
      reviewer: 'user_local',
      method: input.method,
      note: 'MOCK：此结论未被任何真实检测支撑',
    }
  },

  async setReferencePolicy(
    identityId: string,
    input: { mode: 'multi_view' | 'hero_only'; reason: string; acknowledged: string[] },
  ): Promise<AvatarIdentity> {
    await wait(150)
    const i = db.identities.find((x) => x.id === identityId)!
    i.reference_policy = {
      mode: input.mode,
      declared_by: 'user_local',
      declared_at: new Date().toISOString(),
      reason: input.reason,
      acknowledged: [...input.acknowledged],
    }
    return { ...i }
  },

  async freezeIdentity(
    identityId: string,
    input: { hero_asset_id: string; identity_digest: string; license_id: string },
  ): Promise<AvatarIdentity> {
    await wait(200)
    const i = db.identities.find((x) => x.id === identityId)!
    i.hero_asset_id = input.hero_asset_id
    i.identity_digest = input.identity_digest
    i.license_id = input.license_id
    i.approval_state = 'approved'
    return { ...i }
  },

  async attestLicense(licenseId: string, basis: string, ackToken: string): Promise<AttestResult> {
    await wait(200)
    return {
      license: {
        id: licenseId,
        subject: 'MOCK 对象',
        kind: 'image_source',
        scope: 'commercial_ok',
        evidence: `MOCK：未改写任何原始证据；${basis}`,
        covers_weights: false,
        verified_at: new Date().toISOString(),
        verified_by: 'user_local',
        note: ackToken,
      },
      attest_token: ackToken,
      attest_note: '由人确认商用范围，非法律意见；不代表已取得书面法律意见（MOCK 演示）',
    }
  },

  async listVariants(workId: string): Promise<AvatarVariant[]> {
    await wait(120)
    if (db.variants.length === 0) {
      db.variants.push(
        {
          id: nextId('var'),
          identity_id: 'av_base_b_v1',
          outfit: 'news_suit',
          background: 'studio',
          approval_state: 'approved',
          identity_report: { outcome: 'pass', message: '身份一致（MOCK 结论，真实检测待标定）' },
          preview_label: '演播室 · 西装',
        },
        {
          id: nextId('var'),
          identity_id: 'av_base_b_v1',
          outfit: 'knitwear',
          background: 'ai_lab',
          approval_state: 'pending',
          identity_report: { outcome: 'review', message: '待人工复核：换装后需对照冻结参考（MOCK）' },
          preview_label: 'AI 实验室 · 针织衫',
        },
      )
    }
    return db.variants.map((v) => ({ ...v }))
  },

  async createVariant(
    workId: string,
    input: { asset_id?: string; outfit: string; background: string; identity_digest?: string },
  ): Promise<AvatarVariant> {
    await wait(1000)
    const v: AvatarVariant = {
      id: nextId('var'),
      identity_id: db.works.find((x) => x.id === workId)?.identity_id ?? 'av_base_b_v1',
      outfit: input.outfit,
      background: input.background,
      approval_state: 'pending',
      identity_report: { outcome: 'review', message: 'MOCK：未调用参考图编辑，需真实生成并身份审核' },
      preview_label: `${input.background} · ${input.outfit}`,
    }
    db.variants.push(v)
    return v
  },

  // ---- 换景变体：Mock 一律不伪造 ----
  // 这三条都要真实素材、真实分割与逐像素比对。Mock 里编一张"看起来通过了"的 mask
  // 或合成图，会让后面的"人工目视确认"对着假图做判断——那比缺少功能更危险。
  async listMasks(): Promise<{ identity_id: string; masks: MaskSpec[]; note: string }> {
    await wait(80)
    return { identity_id: '', masks: [], note: 'MOCK：演示模式不产出 mask' }
  },

  async createMask(): Promise<MaskEstimateResult> {
    throw new ApiError(
      'MOCK_UNSUPPORTED',
      '演示模式不产出 mask 草稿：它必须由真实素材分割得到，伪造一张会让人工目视确认失去对象',
    )
  },

  async confirmMask(): Promise<MaskSpec> {
    throw new ApiError('MOCK_UNSUPPORTED', '演示模式没有可确认的 mask')
  },

  async composeVariant(): Promise<VariantComposeResult> {
    throw new ApiError(
      'MOCK_UNSUPPORTED',
      '演示模式不做确定性合成：产物必须逐像素来自真实人物图，Mock 不得伪造媒体',
    )
  },

  async listVoices(): Promise<VoiceAsset[]> {
    await wait(80)
    return db.voices.map((v) => ({ ...v }))
  },

  async getTtsControls(workId: string): Promise<{ controls: TtsControls; digest: string }> {
    const work = db.works.find(item => item.id === workId)
    if (!work) throw new ApiError('WORK_NOT_FOUND', '作品不存在')
    const voice = db.voices.find(item => item.id === work.voice_asset_id)
    const controls = effectiveTtsControls(voice?.synthesis_profile, work.tts_controls)
    return { controls, digest: `mock:${controlsKey(controls)}` }
  },

  async updateTtsControls(workId: string, controls: TtsControls): Promise<Work> {
    const work = db.works.find(item => item.id === workId)
    if (!work) throw new ApiError('WORK_NOT_FOUND', '作品不存在')
    const normalized = normalizeTtsControls(controls)
    const invalid = controlsError(normalized)
    if (invalid) throw new ApiError('TTS_CONTROLS_INVALID', invalid)
    const old = await mockApi.getTtsControls(workId)
    if (controlsKey(old.controls) !== controlsKey(normalized)) {
      work.tts_controls_revision = (work.tts_controls_revision ?? 0) + 1
      for (const audio of db.audios[workId] ?? []) { audio.confirmed = false; audio.invalidated = true }
      work.final_audio_id = null
      work.final_audio_duration_ms = null
      work.video_asset_id = null
      delete db.packages[workId]
      delete db.costConfirmations[workId]
    }
    work.tts_controls = normalized
    return structuredClone(work)
  },

  async createVoiceClone(input: VoiceCloneInput): Promise<VoiceCloneResult> {
    const job: VoiceCloneJob = mockCloneJobs.get(input.request_id) ?? {
      request_id: input.request_id, state: 'blocked', stage: 'validating',
      error: { code: 'MOCK_UNSUPPORTED', message: '演示模式不上传音频、不创建真实克隆音色，也不会产生费用。' },
    }
    mockCloneJobs.set(input.request_id, job)
    return { voice: null, clone_job: job, note: '请连接真实后端后使用克隆功能。' }
  },

  async getVoiceClone(requestId: string): Promise<VoiceCloneJob> {
    const job = mockCloneJobs.get(requestId)
    if (!job) throw new ApiError('CLONE_JOB_NOT_FOUND', '未找到这条演示克隆作业。', false, null, 404)
    return job
  },

  async createVoicePreview(workId: string, voiceId: string, sampleText: string, _forceNew = false): Promise<VoicePreviewResult> {
    await wait(1100)
    // 同一作品绑定同一试听短句：Mock 不能比真实后端更宽松，
    // 否则"换句比较音色"这类错误只会在接通后端后才暴露。
    const pinned = (db.audition[workId] ||= '')
    if (pinned && sampleText && pinned !== sampleText) {
      throw new ApiError(
        'PREVIEW_SAMPLE_TEXT_MISMATCH',
        `同一作品必须用同一短句试听：该作品已使用「${pinned}」`,
      )
    }
    if (!pinned && sampleText) db.audition[workId] = sampleText

    const list = (db.audios[workId] ||= [])
    const a: AudioAssetInfo = {
      id: nextId('audio'),
      kind: 'preview',
      voice_asset_id: voiceId,
      duration_ms: 4200,
      approved: true,
      confirmed: false,
      url: null,
      work_id: workId,
      is_mock: true,
      file_hash: '',
      timing_source: null,
      synthesis_profile: (await mockApi.getTtsControls(workId)).controls,
      tts_controls_revision: db.works.find(item => item.id === workId)?.tts_controls_revision ?? 0,
      checks: [
        { code: 'VOICE_SCOPE', outcome: 'pass', hard_gate: true, message: '音色作用域与所选 VoiceAsset 一致' },
        {
          code: 'AUDIO_DECODE',
          outcome: 'not_run',
          hard_gate: false,
          message: 'MOCK：未产生真实音频字节，不得据此声称已解码校验',
        },
        { code: 'PREVIEW_NOT_FINAL', outcome: 'pass', hard_gate: true, message: '试听短句不是正文，不能作为最终音频' },
      ],
    }
    list.push(a)
    db.selected['sel_voice'] = voiceId
    return {
      cached: false,
      message: 'MOCK 试听：未提交真实 Provider，不产生费用',
      note: '试听短句仅用于比较音色，不是完整稿件，不能作为正文音频',
      sample_sentence: db.audition[workId] || null,
      audio: a,
    }
  },

  async getVoiceCatalogStatus(): Promise<VoiceCatalogStatus> {
    await wait(60)
    return {
      catalog_size: MOCK_CATALOG.length,
      registered: db.voices.length,
      missing: [],
      approved_ids: db.voices.filter((v) => v.approval_state === 'approved').map((v) => v.id),
      total_voices_in_store: db.voices.length,
      excluded_note: 'MOCK：目录筛选口径与后端一致',
      attest_token: 'COMMERCIAL_ATTEST_V1',
      attest_note: '由人确认商用范围，非法律意见；不代表已取得书面法律意见',
    }
  },

  async registerVoiceBaseline(voiceId: string, workId: string): Promise<VoiceBaselineResult> {
    await wait(140)
    const v = db.voices.find((x) => x.id === voiceId)
    if (!v) throw new ApiError('VOICE_NOT_FOUND', `音色不存在：${voiceId}`)
    // Mock 没有真实音频：按后端口径拒收，不伪造一份哈希填进去
    throw new ApiError(
      'VOICE_BASELINE_NO_AUDITION',
      `作品 ${workId} 没有真实试听：基准证据必须来自真实合成，Mock 不得伪造哈希`,
    )
  },

  async approveVoice(voiceId: string): Promise<VoiceAsset> {
    await wait(120)
    const v = db.voices.find((x) => x.id === voiceId)
    if (!v) throw new ApiError('VOICE_NOT_FOUND', `音色不存在：${voiceId}`)
    // 批准的是"用户确认了这组音色"，不是生产质量结论：这里不动 production_ready
    v.approval_state = 'approved'
    return { ...v }
  },

  async selectVoice(workId: string, voiceId: string): Promise<Work> {
    await wait(120)
    const w = db.works.find((x) => x.id === workId)!
    w.voice_asset_id = voiceId
    return { ...w }
  },

  async generateScripts(workId: string, topic: string, materials: string[] = []): Promise<ScriptBatch> {
    await wait(1500)
    const batch: ScriptBatch = {
      id: nextId('batch'),
      work_id: workId,
      candidates: makeCandidates(topic),
      content_route: db.works.find((x) => x.id === workId)?.brief.content_route ?? 'talking_script',
      shortfall_note: null,
      // 本地模拟库里的稿子就是模拟稿，如实标注，不冒充模型产出
      source_kind: 'mock',
      materials_count: materials.length,
    }
    db.scriptBatches[workId] = batch
    const w = db.works.find((x) => x.id === workId)
    if (w && !w.current_task_id) makeTask(workId, w.route)
    const t = db.tasks[w?.current_task_id ?? '']
    if (t && t.stage === 'topic') startEngine(t.id)
    return batch
  },

  async getScriptBatch(workId: string): Promise<ScriptBatch | null> {
    await wait(80)
    return db.scriptBatches[workId] ?? null
  },

  async selectScript(workId: string, scriptId: string): Promise<{ ok: boolean; message: string }> {
    await wait(200)
    const batch = db.scriptBatches[workId]
    const c = batch?.candidates.find((x) => x.id === scriptId)
    if (!c) return { ok: false, message: '稿件不存在或已被新版本取代' }
    if (batch.candidates.length !== 3 || batch.candidates.some(x => x.quality_report.overall === 'fail'))
      return { ok: false, message: '必须三篇全部通过审核后才能采用' }
    if (c.quality_report.overall === 'fail')
      return { ok: false, message: '该稿未通过硬门禁，不能被采用（可编辑后重新审核）' }
    db.selected['sel_script'] = scriptId
    const w = db.works.find((x) => x.id === workId)
    const t = db.tasks[w?.current_task_id ?? '']
    if (t) {
      t.pending_selection_ids = t.pending_selection_ids.filter((x) => x !== 'sel_script')
      if (t.stage === 'content_review') {
        const storyboard = w?.brief.content_route === 'short_video_storyboard'
        t.stage = storyboard ? 'content_review' : 'avatar_variant'
        t.state = storyboard ? 'waiting_review' : 'running'
        pushEvent(t.id, 'selection.decided', `已采用稿件：${c.title}`, { state: t.state })
        if (!storyboard) startEngine(t.id)
      }
    }
    return { ok: true, message: `已采用：${c.title}` }
  },

  async getContentState(workId: string): Promise<ContentState> {
    await wait(60)
    const c = mc(workId)
    const w = db.works.find((x) => x.id === workId)
    const batch = db.scriptBatches[workId] ?? null
    const ref = c.references.find((r) => r.id === c.referenceId) ?? null
    return {
      work_id: workId, content_revision: c.revision, brief: (w?.brief ?? { topic: '', content_type: '', audience: '', keywords: [], target_duration_ms: 0 }),
      reference: ref, style: null, prompt: c.prompt,
      batch: batch ? { ...batch, requested: 3, produced: batch.candidates.length,
        passed: batch.candidates.filter((x) => x.quality_report.overall !== 'fail').length,
        ready_to_present: batch.candidates.length === 3 && batch.candidates.every(x => x.quality_report.overall !== 'fail'),
        retry_targets: batch.candidates.filter(x => x.quality_report.overall === 'fail').map((x, index) =>
          ({ candidate_id: x.id, slot: index + 1, review_state: 'failed', reason: '硬门禁未过（演示数据）' })),
        candidates: batch.candidates.map((x) => ({ ...x, pickable: x.quality_report.overall !== 'fail',
          review_state: x.quality_report.overall !== 'fail' ? 'passed' as const : 'failed' as const,
          unpickable_reasons: x.quality_report.overall !== 'fail' ? [] : ['硬门禁未过（演示数据）'] }))
          .filter(() => batch.candidates.length === 3 && batch.candidates.every(x => x.quality_report.overall !== 'fail')) } : null,
      selected_script_id: w?.script_version_id ?? null, active_jobs: [], recent_jobs: Object.values(c.jobs).slice(-10),
      references: c.references.map((r) => ({ id: r.id, platform_work_id: r.platform_work_id, caption: r.caption, author: r.author,
        transcript_state: r.transcript.state, parent_id: r.parent_id ?? null,
        visual_state: (r.visual as ReferenceVisual | undefined)?.state ?? 'none', media_asset_id: r.media_asset_id ?? null,
        has_video_url: !!r.video_url })),
      media_assets: Object.values(c.media),
      limits: { per_flow_fen: null, daily_fen: null, max_text_calls: null, concurrency: 2, max_repair_rounds: 2 },
    }
  },
  async getContentJob(workId: string, jobId: string): Promise<ContentJobReceipt> {
    await wait(30)
    const j = mc(workId).jobs[jobId]
    if (!j) throw new ApiError('JOB_NOT_FOUND', '内容任务不存在', false, null, 404)
    return { ...j }
  },
  async getReferenceSnapshot(workId: string, referenceId: string): Promise<ReferenceSnapshot> {
    const ref = mc(workId).references.find((r) => r.id === referenceId)
    if (!ref) throw new ApiError('REFERENCE_NOT_FOUND', '参考不存在或不属于该作品', false, null, 404)
    return structuredClone(ref)
  },
  async cancelContentJob(workId: string, jobId: string): Promise<ContentJobReceipt> { return this.getContentJob(workId, jobId) },
  async refreshContentJob(_workId: string, jobId: string) {
    return { job_id: jobId, state: 'succeeded', status: 'nothing_to_refresh', message: '演示模式没有远端任务' }
  },
  async searchReferences(workId: string, input: { request_id: string; query: string }): Promise<ContentJobReceipt> {
    await wait(300)
    return mockJob(workId, 'search', input.request_id, {
      items: MOCK_ITEMS(input.query), next_cursor: null, fetched_at: new Date().toISOString(),
      sorted_scope: 'page', contract: 'mock（演示数据，非真实搜索结果）',
    })
  },
  async importReference(workId: string, input: { request_id: string; platform_work_id?: string }): Promise<ContentJobReceipt> {
    await wait(400)
    const c = mc(workId)
    const item = MOCK_ITEMS('演示').find((i) => i.platform_work_id === input.platform_work_id) ?? MOCK_ITEMS('演示')[0]
    const ref: ReferenceSnapshot = {
      id: nextId('ref'), work_id: workId, platform_work_id: item.platform_work_id, source_url: item.source_url, title: '',
      caption: item.caption, author: item.author, cover_url: null, metrics: item.metrics, published_at: item.published_at,
      fetched_at: item.fetched_at, evidence_kind: 'inspiration', content_digest: 'mock',
      transcript: { state: 'ready', text: '【演示转写】这是一段用于界面演示的示例转写，不是真实视频内容。', source: 'mock', task_id: null, error: null },
    }
    if (item.platform_work_id === 'mock_2') {
      item.caption = '【演示数据】BGM 舞蹈（无口播）'
      ref.caption = item.caption
      ref.transcript = { state: 'failed', text: '', source: 'redfox', task_id: null, error: '转写成功但正文为空（可能是纯音乐/无口播）' }
    }
    c.references.push(ref)
    return mockJob(workId, 'import', input.request_id, { reference_id: ref.id, transcript_state: ref.transcript.state, visual_state: 'none' })
  },
  async selectReference(workId: string, referenceId: string, expected: number) {
    const c = mc(workId)
    if (expected !== c.revision) throw new ApiError('CONTENT_REVISION_STALE', '内容已变化', false, null, 409)
    const target = c.references.find((r) => r.id === referenceId)
    if (!target) throw new ApiError('REFERENCE_NOT_FOUND', '参考不存在', false, null, 404)
    const storyboard = db.works.find((x) => x.id === workId)?.brief?.content_route === 'short_video_storyboard'
    if (target.transcript.state !== 'ready') {
      const visualReady = (target.visual as ReferenceVisual | undefined)?.state === 'ready'
      if (!storyboard) throw new ApiError('REFERENCE_TRANSCRIPT_REQUIRED', '口播稿需要有效转写：该参考没有有效口播', false, null, 422)
      if (!visualReady) throw new ApiError('REFERENCE_VISUAL_REQUIRED', '该参考既没有有效转写，也没有完成的画面分析', false, null, 422)
    }
    c.referenceId = referenceId
    c.prompt = null
    c.revision += 1
    return { ok: true, content_revision: c.revision, reference_snapshot_id: referenceId }
  },
  async uploadReferenceMedia(workId: string, referenceId: string, file: File): Promise<{ asset: MediaAssetView }> {
    await wait(300)
    const c = mc(workId)
    if (!c.references.some((r) => r.id === referenceId)) throw new ApiError('REFERENCE_NOT_FOUND', '参考不存在', false, null, 404)
    const ok = /\.(mp4|mov|webm)$/i.test(file.name)
    if (!ok) throw new ApiError('MEDIA_TYPE_UNSUPPORTED', '只支持 MP4、MOV、WebM 视频文件', false, null, 422)
    if (file.size <= 0) throw new ApiError('MEDIA_INVALID', '不能上传空文件', false, null, 422)
    if (file.size > 200 * 1024 * 1024) throw new ApiError('MEDIA_TOO_LARGE', '视频不能超过 200 MiB', false, null, 413)
    const asset: MediaAssetView = { id: nextId('media'), duration_ms: 18500, width: 1080, height: 1920,
      mime_type: 'video/mp4', sha256: 'sha256:' + 'ab'.repeat(32), byte_size: file.size, source_kind: 'user_upload' }
    c.media[asset.id] = asset
    return { asset: structuredClone(asset) }
  },
  async quoteVisualAnalysis(workId: string, input: { reference_snapshot_id: string; media_asset_id: string }): Promise<VisualQuote> {
    if (!input.media_asset_id || !mc(workId).media[input.media_asset_id]) {
      throw new ApiError('REFERENCE_MEDIA_UNAVAILABLE', '该参考还没有可分析的视频文件：请上传原视频', false, null, 422)
    }
    return { request_digest: 'mock', reserve_fen: 80, estimated_fen: null, price_verified: false, max_ceiling_fen: null,
      note: '演示模式：不会真实调用，也不会产生费用' }
  },
  async startVisualAnalysis(workId: string, input: { request_id: string; reference_snapshot_id: string; media_asset_id: string; expected_content_revision: number; paid_ack: boolean; total_ceiling_fen: number }): Promise<ContentJobReceipt> {
    await wait(500)
    const c = mc(workId)
    if (input.paid_ack !== true) throw new ApiError('VISUAL_COST_CONFIRMATION_REQUIRED', '请先确认视觉分析费用', false, null, 422)
    if (input.total_ceiling_fen < 80) throw new ApiError('COST_AMOUNT_INVALID', '费用上限无效', false, null, 422)
    const base = c.references.find((r) => r.id === input.reference_snapshot_id)
    const asset = c.media[input.media_asset_id]
    if (!base) throw new ApiError('REFERENCE_NOT_FOUND', '参考不存在', false, null, 404)
    if (!asset) throw new ApiError('MEDIA_NOT_FOUND', '媒体资产不存在或不属于该作品', false, null, 404)
    const visual: ReferenceVisual = {
      state: 'ready', provider: 'mock', model: 'mock-vision（演示）', analysis_version: 'visual-v1', media_asset_id: asset.id,
      duration_ms: asset.duration_ms, summary: '【演示数据】竖屏单人舞蹈，室内镜面场景，动作切换快',
      speech_presence: 'none', music_presence: 'music', audio: { tempo_bpm: 128.4, beat_times_ms: [468, 937, 1406], confidence: 0.74 },
      scenes: [
        { index: 1, start_ms: 0, end_ms: 6200, shot_type: 'full', camera_motion: 'static', description: '人物在室内正面面对镜头',
          actions: ['抬手', '转身'], on_screen_text: [], beat_cues_ms: [468, 1406], confidence: 0.81 },
        { index: 2, start_ms: 6200, end_ms: 18500, shot_type: 'medium', camera_motion: 'handheld', description: '侧向移动并做节奏点动作',
          actions: ['侧向移动', '点头'], on_screen_text: ['跟我学'], beat_cues_ms: [6900], confidence: 0.7 },
      ], warnings: ['演示数据，不是真实模型观察'],
    }
    const ref: ReferenceSnapshot = { ...base, id: nextId('ref'), parent_id: base.id, media_asset_id: asset.id, visual }
    c.references.push(ref)
    return mockJob(workId, 'visual_analysis', input.request_id, {
      reference_id: ref.id, visual_state: 'ready', media_asset_id: asset.id, visual_digest: 'mock' })
  },
  async quoteMediaFetch(workId: string, input: { reference_snapshot_id: string }): Promise<MediaFetchQuote> {
    if (!mc(workId).references.some((r) => r.id === input.reference_snapshot_id)) {
      throw new ApiError('REFERENCE_NOT_FOUND', '参考不存在', false, null, 404)
    }
    return { request_digest: 'mock', reserve_fen: 50, estimated_fen: null, price_verified: false, max_ceiling_fen: null,
      note: '演示模式：不会真实调用，也不会产生费用' }
  },
  async startMediaFetch(workId: string, input: { request_id: string; reference_snapshot_id: string; expected_content_revision: number; paid_ack: boolean; total_ceiling_fen: number }): Promise<ContentJobReceipt> {
    await wait(500)
    const c = mc(workId)
    if (input.paid_ack !== true) throw new ApiError('MEDIA_FETCH_CONFIRMATION_REQUIRED', '请先确认获取视频的费用', false, null, 422)
    if (input.total_ceiling_fen < 50) throw new ApiError('COST_AMOUNT_INVALID', '费用上限无效', false, null, 422)
    if (!c.references.some((r) => r.id === input.reference_snapshot_id)) throw new ApiError('REFERENCE_NOT_FOUND', '参考不存在', false, null, 404)
    const asset: MediaAssetView = { id: nextId('media'), duration_ms: 18500, width: 1080, height: 1920,
      mime_type: 'video/mp4', sha256: 'sha256:' + 'cd'.repeat(32), byte_size: 4553327, source_kind: 'provider_url' }
    c.media[asset.id] = asset
    return mockJob(workId, 'media_fetch', input.request_id, {
      reference_id: input.reference_snapshot_id, media_asset_id: asset.id, duration_ms: asset.duration_ms, byte_size: asset.byte_size })
  },
  async reviseReferenceTranscript(workId: string, input: { request_id: string; reference_snapshot_id: string; transcript_text: string }): Promise<ContentJobReceipt> {
    const c = mc(workId)
    const base = c.references.find((r) => r.id === input.reference_snapshot_id)
    if (!base) throw new ApiError('REFERENCE_NOT_FOUND', '参考不存在', false, null, 404)
    const ref: ReferenceSnapshot = { ...base, id: nextId('ref'), parent_id: base.id,
      original_transcript: base.original_transcript ?? base.transcript,
      transcript: { state: 'ready', text: input.transcript_text, source: 'user_edit', task_id: null, error: null } }
    c.references.push(ref)
    c.referenceId = ref.id
    c.prompt = null
    c.revision += 1
    return mockJob(workId, 'reference_revision', input.request_id, { reference_id: ref.id })
  },
  async saveContentSettings(workId: string, input: { expected_content_revision: number; brief?: Record<string, unknown> }) {
    const c = mc(workId)
    if (input.expected_content_revision !== c.revision) throw new ApiError('CONTENT_REVISION_STALE', '内容已变化', false, null, 409)
    const work = db.works.find(x => x.id === workId)
    if (work && input.brief) work.brief = { ...work.brief, ...input.brief }
    c.prompt = null
    c.revision += 1
    return { ok: true, content_revision: c.revision }
  },
  async preparePrompt(workId: string, input: { request_id: string }): Promise<ContentJobReceipt> {
    await wait(500)
    const c = mc(workId)
    const ref = c.references.find((r) => r.id === c.referenceId)
    if (!ref) throw new ApiError('REFERENCE_REQUIRED', '请先选择一个可用的主参考', false, null, 422)
    const storyboard = db.works.find((x) => x.id === workId)?.brief.content_route === 'short_video_storyboard'
    if (ref.transcript.state !== 'ready') {
      if (!storyboard) throw new ApiError('REFERENCE_TRANSCRIPT_REQUIRED', '口播稿需要有效转写：该参考没有有效口播', false, null, 422)
      if ((ref.visual as ReferenceVisual | undefined)?.state !== 'ready')
        throw new ApiError('REFERENCE_VISUAL_REQUIRED', '该参考既没有有效转写，也没有完成的画面分析', false, null, 422)
    }
    const prompt: WritingPrompt = { id: nextId('prm'), version: (c.prompt?.version ?? 0) + 1, source: 'generated', reference_snapshot_id: c.referenceId,
      reference_analysis: { hook_function: '（演示）', do_not_reuse: [] },
      angle_plan: [1, 2, 3].map((n) => ({ slot: n, angle: `演示角度 ${n}`, new_viewpoint: '（演示）' })),
      prompt_text: storyboard ? '【演示提示】请围绕主题写三篇角度不同的分镜剧本，包含画面、对白和声音。'
        : '【演示提示】请围绕主题写三篇角度不同的原创口播，口语化、有自己的观点。' }
    c.prompt = prompt
    c.revision += 1
    return mockJob(workId, 'prompt', input.request_id, { prompt_id: prompt.id, version: prompt.version })
  },
  async revisePrompt(workId: string, input: { request_id: string; prompt_id: string; prompt_text: string }): Promise<ContentJobReceipt> {
    const c = mc(workId)
    const base = c.prompt
    if (!base || base.id !== input.prompt_id) throw new ApiError('PROMPT_NOT_FOUND', '提示不存在', false, null, 404)
    c.prompt = { ...base, id: nextId('prm'), parent_id: base.id, version: base.version + 1, source: 'edited', prompt_text: input.prompt_text }
    c.revision += 1
    return mockJob(workId, 'prompt_revision', input.request_id, { prompt_id: c.prompt.id, version: c.prompt.version })
  },
  async startGeneration(workId: string, input: { request_id: string; content_route?: string }): Promise<ContentJobReceipt> {
    const work = db.works.find(x => x.id === workId)
    if (input.content_route && work?.brief.content_route !== input.content_route)
      throw new ApiError('CONTENT_ROUTE_STALE', '内容路线已变化', false, null, 409)
    const b = await this.generateScripts(workId, db.works.find((x) => x.id === workId)?.brief.topic ?? '', [])
    return mockJob(workId, 'generation', input.request_id, { batch_id: b.id, produced: b.candidates.length })
  },
  async startRepair(workId: string, input: { request_id: string; candidate_ids: string[] }): Promise<ContentJobReceipt> {
    await this.repairScript(workId, input.candidate_ids[0])
    return mockJob(workId, 'repair', input.request_id, {})
  },
  async startReview(workId: string, input: { request_id: string }): Promise<ContentJobReceipt> {
    return mockJob(workId, 'review', input.request_id, {})
  },

  async reviseScript(workId: string, scriptId: string, input: string | { text?: string; title?: string; description?: string; storyboard?: ScriptVersion['storyboard'] }): Promise<ScriptVersion> {
    await wait(900)
    const batch = db.scriptBatches[workId]!
    const src = batch.candidates.find((x) => x.id === scriptId)!
    const revised: ScriptVersion = {
      ...src,
      id: nextId('script'),
      ...(typeof input === 'string' ? { text: input } : {
        text: input.text ?? src.text,
        title: input.title ?? src.title,
        description: input.description ?? src.description,
        storyboard: input.storyboard ?? src.storyboard,
      }),
      quality_report: {
        checks: src.quality_report.checks.map((c) =>
          c.code === 'HOOK_STRENGTH' && c.outcome === 'fail'
            ? { ...c, outcome: 'pass', message: '修订后重新审核通过（MOCK 审核）' }
            : c,
        ),
        overall: 'pass',
        review_required: false,
      },
    }
    batch.candidates = batch.candidates.map((c) => (c.id === scriptId ? revised : c))
    return revised
  },

  /** 让模型重写这一稿。MOCK 下不产生真实模型调用，措辞必须如实写明是模拟重写。 */
  async repairScript(workId: string, scriptId: string): Promise<ScriptVersion> {
    await wait(1100)
    const batch = db.scriptBatches[workId]!
    const src = batch.candidates.find((x) => x.id === scriptId)!
    const fixed: ScriptVersion = {
      ...src,
      // 自修复沿用同一个候选位（后端也是这样），界面上是"这稿修好了"
      quality_report: {
        checks: src.quality_report.checks.map((c) =>
          c.hard_gate && c.outcome !== 'pass'
            ? { ...c, outcome: 'pass', message: '模型重写后通过（MOCK 重写，不代表已核验）' }
            : c,
        ),
        overall: 'pass',
        review_required: false,
      },
    }
    batch.candidates = batch.candidates.map((c) => (c.id === scriptId ? fixed : c))
    batch.repair_note = `模型重写「${fixed.angle}」：消掉无依据断言（MOCK 重写，未调用真实模型）`
    batch.repair_history = [
      ...(batch.repair_history ?? []),
      {
        round: (batch.repair_history?.length ?? 0) + 1,
        candidate_id: fixed.id,
        angle: fixed.angle,
        outcome: 'repaired',
        before: src.quality_report.checks.filter((c) => c.hard_gate && c.outcome !== 'pass').map((c) => c.code),
        after: [],
        note: 'MOCK 重写',
      },
    ]
    return fixed
  },

  async synthesizeFullAudio(workId: string): Promise<FullAudioResult> {
    await wait(1800)
    const list = (db.audios[workId] ||= [])
    const a: AudioAssetInfo = {
      id: nextId('audio'),
      kind: 'full',
      voice_asset_id: db.works.find((x) => x.id === workId)?.voice_asset_id ?? '',
      duration_ms: 58400,
      approved: true,
      confirmed: false,
      url: null,
      work_id: workId,
      is_mock: true,
      file_hash: '',
      timing_source: null,
      script_version_id: db.selected['sel_script'] ?? null,
      performance_plan_id: null,
      synthesis_profile: (await mockApi.getTtsControls(workId)).controls,
      tts_controls_revision: db.works.find(item => item.id === workId)?.tts_controls_revision ?? 0,
      checks: [
        { code: 'VOICE_SCOPE', outcome: 'pass', hard_gate: true, message: '使用本作品确认的 VoiceAsset' },
        {
          code: 'PRONUNCIATION',
          outcome: 'not_run',
          hard_gate: false,
          message: '术语读法需人工听检：MOCK 未产生真实音频，也不产生自动结论',
        },
        { code: 'DURATION', outcome: 'not_run', hard_gate: true, message: 'MOCK：时长 58.4s 为示例值，不是真实音频时长' },
      ],
    }
    list.push(a)
    return {
      audio: a,
      is_mock: true,
      duration_ms: 58400,
      timing_source: null,
      hard_failures: [
        { code: 'AUDIO_IS_MOCK', outcome: 'fail', hard_gate: true, message: 'Mock 音频不得被确认为最终音频' },
      ],
      control_summary: { supported: 0, approximate: 0, unsupported: 0 },
      unsupported_controls: [],
      note: '占位产物：不得确认为最终音频，也不继承任何质量通过结论',
    }
  },

  async confirmAudio(
    workId: string,
    audioId: string,
    _auditionAck = true,
  ): Promise<{ ok: boolean; message: string; duration_ms: number; revoked_previous: string[]; note: string }> {
    await wait(200)
    const list = db.audios[workId] ?? []
    const a = list.find((x) => x.id === audioId)
    if (!a) return { ok: false, message: '音频不存在', duration_ms: 0, revoked_previous: [], note: '' }
    // Mock 也要守住"试听片/Mock 音频不得被确认为最终音频"这条线，
    // 否则离线演示会给人一种"这条门禁不存在"的错觉。
    if (a.kind !== 'full')
      return { ok: false, message: '试听片不能被当作最终音频', duration_ms: 0, revoked_previous: [], note: '' }
    if (a.is_mock)
      return {
        ok: false,
        message: 'Mock 音频不得被确认为最终音频，也不得进入下游口播',
        duration_ms: 0,
        revoked_previous: [],
        note: '',
      }
    const revoked: string[] = []
    list.forEach((x) => {
      if (x.confirmed && x.id !== audioId) {
        x.confirmed = false
        revoked.push(x.id)
      }
    })
    a.confirmed = true
    db.selected['sel_audio'] = audioId
    const w = db.works.find((x) => x.id === workId)
    const t = db.tasks[w?.current_task_id ?? '']
    if (t) {
      t.pending_selection_ids = t.pending_selection_ids.filter((x) => x !== 'sel_audio')
      if (t.stage === 'tts') {
        t.stage = 'talking_head'
        t.state = 'running'
        pushEvent(t.id, 'selection.decided', '最终音频已确认，等待用户选择路线与预算', {
          stage: 'talking_head',
          state: 'running',
        })
      }
    }
    return {
      ok: true,
      message: `最终音频已冻结，确定时长 ${Math.round(a.duration_ms / 100) / 10}s`,
      duration_ms: a.duration_ms,
      revoked_previous: revoked,
      note: '最终音频时长以此为准；字幕对齐可并行启动',
    }
  },

  async getAudioGate(workId: string): Promise<AudioGateSummary> {
    await wait(60)
    const list = db.audios[workId] ?? []
    const final = list.find((x) => x.kind === 'full' && x.confirmed) ?? null
    return {
      work_id: workId,
      voice_asset_id: db.works.find((x) => x.id === workId)?.voice_asset_id ?? null,
      full_audio_count: list.filter((x) => x.kind === 'full').length,
      preview_count: list.filter((x) => x.kind === 'preview').length,
      final_audio_id: final?.id ?? null,
      final_audio_duration_ms: final?.duration_ms ?? null,
      audio_confirmed: !!final,
      alignment: {
        aligned: false,
        word_count: 0,
        claimable: false,
        reason: final
          ? '无可用字词时间戳：不得宣称字词对齐已完成；由后期对齐后另行登记对齐产物版本'
          : '尚无已确认的最终音频，字幕对齐不启动',
      },
    }
  },

  async runAlignment(workId: string): Promise<AlignmentRunResult> {
    await wait(150)
    const audio = (db.audios[workId] ?? []).find((a) => a.kind === 'full' && a.confirmed)
    if (!audio) throw new ApiError('FINAL_AUDIO_REQUIRED', '尚无已确认最终音频，字词对齐不启动')
    // 离线模式没有 ASR/强制对齐，不能凭空造一份字词时间戳：
    // 伪造的时间轴会让分段看着"成功"，而每段边界其实与语音无关。
    throw new ApiError(
      'ALIGNMENT_UNAVAILABLE_IN_MOCK',
      'MOCK 模式不产生字词级对齐：真实字词时间轴只能来自对同一音轨的 ASR + 强制对齐，离线模式不做，也不编造',
    )
  },

  async estimateCost(workId: string, route: GenerationRoute): Promise<CostEstimate> {
    await wait(400)
    // 金额必须由费率 × 实际计费时长算出来。写死常数会让界面显示的"估价"与输入无关。
    const audio = (db.audios[workId] ?? []).find((a) => a.kind === 'full' && a.confirmed) ?? null
    const seconds = Math.ceil((audio?.duration_ms ?? 0) / 1000)
    if (route === 'WAN_3_0') throw new ApiError('WAN_MOCK_UNAVAILABLE', '离线模式不能计算真实 Wan 费用')
    if (route === 'OMNIHUMAN_1_5') {
      if (!seconds)
        return {
          route,
          estimated_fen: 0,
          ceiling_fen: 0,
          currency: 'CNY',
          price_basis: 'MOCK：1 元/生成秒（报价，非账单）',
          billable: true,
          note: '最终音频未就绪：这里的 0 表示「未计算」，不是「免费」',
        }
      const est = seconds * 100
      return {
        route,
        estimated_fen: est,
        ceiling_fen: Math.ceil(est * 1.2),
        currency: 'CNY',
        price_basis: 'MOCK：1 元/生成秒（报价，非账单）',
        billable: true,
        note: '取整/最低消费/失败扣费均未核验，实际费用可能高于估价',
      }
    }
    return {
      route,
      estimated_fen: 0,
      ceiling_fen: 0,
      currency: 'CNY',
      price_basis: 'MOCK：本地 GPU 作业不按生成秒计价，按卡时计入',
      billable: false,
      note: '无云单价；默认额度为 0，真实计费需先在预算策略中授权',
    }
  },

  async confirmCost(
    workId: string,
    route: GenerationRoute,
    costNoticeAck = false,
  ): Promise<{ ok: boolean; cost_notice_acknowledged: boolean; message: string }> {
    await wait(200)
    const w = db.works.find((x) => x.id === workId)!
    // 高成本路线在离线模式下同样要求费用提示确认：门禁口径前后端一致。
    if (route !== 'M2' && !costNoticeAck) {
      throw new Error('COST_NOTICE_REQUIRED：高成本路线必须先确认费用提示')
    }
    w.route = route
    routeConfirmedWorks.add(workId)
    const t = db.tasks[w.current_task_id ?? '']
    if (t) {
      t.route = route
      t.route_confirmed = true
      pushEvent(t.id, 'route.selected', `路线 ${route} 已选择`, { stage: 'talking_head' })
    }
    return {
      ok: true,
      cost_notice_acknowledged: route !== 'M2' && costNoticeAck,
      message: `已确认路线 ${route}（MOCK：不产生真实费用）`,
    }
  },

  async submitTask(workId: string): Promise<Task> {
    await wait(300)
    const w = db.works.find((x) => x.id === workId)!
    let t = db.tasks[w.current_task_id ?? '']
    if (!t) t = makeTask(workId, w.route)
    if (!t.route_confirmed) {
      t.state = 'blocked'
      t.block_reason = 'COST_CONFIRMATION_REQUIRED'
      t.last_error = {
        code: 'COST_CONFIRMATION_REQUIRED',
        message: '提交前必须选择口播路线并确认适用预算',
        action: 'confirm_cost',
      }
      return { ...t, stage_rows: stageRows(t.stage, t.state) }
    }
    t.stage = 'talking_head'
    t.state = 'running'
    pushEvent(t.id, 'stage.started', '开始口播生成', { stage: 'talking_head', state: 'running' })
    startEngine(t.id)
    return { ...t, stage_rows: stageRows(t.stage, t.state) }
  },

  async getTask(taskId: string): Promise<Task> {
    await wait(80)
    const t = db.tasks[taskId]
    if (!t) throw new Error('任务不存在')
    return { ...t, stage_rows: stageRows(t.stage, t.state) }
  },

  async getTaskEvents(taskId: string, afterSeq = 0): Promise<TaskEvent[]> {
    await wait(60)
    return (db.events[taskId] ?? []).filter((e) => e.seq > afterSeq)
  },

  async cancelTask(taskId: string): Promise<Task> {
    await wait(150)
    const t = db.tasks[taskId]
    clearTimeout(db.timers[taskId])
    t.state = 'cancelled'
    pushEvent(taskId, 'task.cancelled', '已停止推进；已受理的外部作业仍需对账', { state: 'cancelled' })
    return { ...t, stage_rows: stageRows(t.stage, t.state) }
  },

  async getPackage(workId: string): Promise<PublishPackage | null> {
    await wait(100)
    return db.packages[workId] ?? null
  },

  async getPublishStatus(workId: string): Promise<PublishTaskView> {
    return { id: null, work_id: workId, state: 'draft', is_mock: true }
  },
  async openPublishLogin(): Promise<PublishSessionView> {
    return { state: 'unavailable', adapter: 'mock', message: 'MOCK 模式不会打开真实浏览器或发布作品。' }
  },
  async getPublishSession(): Promise<PublishSessionView> {
    return { state: 'unavailable', adapter: 'mock', message: 'MOCK 模式不读取真实抖音登录状态。' }
  },
  async startPublish(): Promise<PublishTaskView> {
    throw new ApiError('PUBLISH_MOCK_ONLY', 'MOCK 模式不执行真实发布，请切换到真实后端。')
  },
  async confirmPublish(): Promise<PublishTaskView> {
    throw new ApiError('PUBLISH_MOCK_ONLY', 'MOCK 模式不执行真实发布。')
  },
  async cancelPublish(): Promise<PublishTaskView> {
    throw new ApiError('PUBLISH_MOCK_ONLY', 'MOCK 模式没有真实发布任务。')
  },
  async resumePublish(): Promise<PublishTaskView> {
    throw new ApiError('PUBLISH_MOCK_ONLY', 'MOCK 模式没有真实发布任务。')
  },

  // ---------- T05 数字人口播（离线模拟） ----------
  //
  // 离线模式刻意**不**伪造这三样东西：
  //   · 视频产物（不登记任何原片/底片）——占位文件会让人以为已经生成过；
  //   · 门禁结论（一律 not_run）——"没审"不能被模拟成"审过"；
  //   · 实测能力（verified 一律为空）——没测过就没测过。
  // 但它**会**如实模拟"被拦"的路径，因为那正是这一版要演示的部分。

  async getTalkingHeadRoutes(_workId?: string): Promise<RoutePriceView> {
    await wait(120)
    return {
      routes: [
        {
          route: 'WAN_3_0', label: 'Wan 3.0 · 创意短视频', provider: 'dashscope_wan', model: 'wan3.0-video',
          capability: 'short_video_cloud', consumes: ['参考图片', '画面与声音提示词', '生成时长'],
          requires_motion_plate: false, stage_count: 1, heavy: false, cost_tier: 'high',
          cost_tier_label: '按生成时长计费', cost_notice_required: true,
          cost_summary: '官方北京原价：480p 0.30、720p 0.60、1080p 1.20 元/生成秒',
          body_motion: { supported: true, scope: '参考图片生成，非严格首帧；口型须预览验收', verified: false, verified_note: '离线模式无实测结论' },
          cost_notice: mockCostNotice('WAN_3_0'),
          declared: { duration_min_seconds: 2, duration_max_seconds: 30, default_duration_seconds: 5, prompt_max_chars: 20000,
            resolutions: ['480p', '720p', '1080p'], generation_mode: 'image_prompt_native_audio', sound_mode: 'native_audio' },
          verified: {}, evidence: [], open_questions: ['账号邀测权限', '当前素材口型与人物保持效果'], verified_state: 'unverified',
        },
        {
          route: 'M2',
          label: 'M2 · LivePortrait → MuseTalk 1.5',
          provider: 'm2_worker',
          model: 'LivePortrait+MuseTalk1.5',
          capability: 'avatar_video',
          consumes: ['已审变体图', '已获准动作参考', '已确认最终 TTS'],
          requires_motion_plate: true,
          stage_count: 2,
          heavy: true,
          cost_tier: 'low',
          cost_tier_label: '低成本 · 头肩口型',
          cost_notice_required: false,
          cost_summary: '无云单价：占用本机 GPU 卡时（实验观测约 ¥0.10/15.2 秒，非生产单价）',
          body_motion: {
            supported: false,
            scope: '沿用已审动态底片的动作，路线本身不新增肢体动作与手势',
            verified: false,
            verified_note: '动作来自已获准动作参考与底片；不承诺任意手势',
          },
          cost_notice: mockCostNotice('M2'),
          declared: {
            stage_1: 'LivePortrait 动作迁移（人物图 + 动作参考 → 动态底片）',
            stage_2: 'MuseTalk 1.5 口型（底片 + 最终音频 → 原片）',
            async: false,
          },
          verified: {
            exp_001: {
              output_seconds: 15.2,
              gpu_cost_yuan_estimate: 0.103217,
              scope: 'EXP-001 隔离环境；非账单、非生产单价、非约 60 秒能力证据',
            },
          },
          evidence: ['EXP-001：15.20 秒 M2 原片，两阶段共 235.177 秒 GPU 时间。'],
          open_questions: ['约 60 秒循环接缝', '每天 5 条产能', '动作母版定版'],
          verified_state: 'partially_verified',
        },
        {
          route: 'OMNIHUMAN_1_5',
          label: 'OmniHuman 1.5 · 图＋音频驱动',
          provider: 'volcengine',
          model: 'omnihuman-1.5',
          capability: 'avatar_video_cloud',
          consumes: ['已审固定人物/变体图', '已确认最终 TTS', '可编辑表演 prompt（可选）'],
          requires_motion_plate: false,
          stage_count: 1,
          heavy: false,
          cost_tier: 'high',
          cost_tier_label: '高成本 · 含肢体动作',
          cost_notice_required: true,
          cost_summary: '按生成秒计费：¥1.00/生成秒（报价，取整/最低消费/失败扣费均未核验）',
          body_motion: {
            supported: true,
            scope: '画面含头肩以外的肢体动作，由模型自由生成，不是逐帧指定',
            verified: false,
            verified_note: '声明口径：服务商文档如此描述；本项目真实任务尚未逐项验收动作质量',
          },
          cost_notice: mockCostNotice('OMNIHUMAN_1_5'),
          declared: {
            audio_max_seconds_exclusive: 60,
            audio_recommended_seconds: 15,
            prompt_max_chars: 300,
            resolutions: ['720p', '1080p'],
            async: true,
            idempotent_submit: false,
          },
          verified: {},
          evidence: ['MOCK：离线模式没有实测记录。'],
          open_questions: [
            '当前账户的音频时长上限与分辨率权限',
            '计费取整/最低粒度/失败扣费',
            '身体动作与手势的指令遵循程度',
          ],
          verified_state: 'unverified',
        },
      ],
      price_snapshots: [
        {
          id: 'price_mock_omni',
          route: 'OMNIHUMAN_1_5',
          provider: 'volcengine',
          unit: 'fen_per_generated_second',
          unit_fen: 100,
          price_source: 'user_confirmed_rate',
          billing_verified: false,
          rounding_note: '计费取整粒度未核验（按秒向上取整只是保守估计，不是账单结论）',
          minimum_note: '最低消费未核验',
          failure_billing_note: '失败扣费规则未核验：失败可能仍计费，因此不得盲目重提',
          evidence: '用户确认 1 元/生成秒；仅含视频生成费，TTS/文本图片/存储后期/返工另计',
          created_at: nowIso(),
        },
        {
          id: 'price_mock_m2',
          route: 'M2',
          provider: 'm2_worker',
          unit: 'no_unit_price',
          unit_fen: 0,
          price_source: 'unverified',
          billing_verified: false,
          rounding_note: '本地 GPU 无单价：按实际使用时长/卡时计入',
          minimum_note: '不适用于本地作业',
          failure_billing_note: '失败作业不产生云费用，但已占用的 GPU 时间仍计入成本',
          evidence: 'GPU 卡时是实验结果，不是生产单价',
          created_at: nowIso(),
        },
      ],
      scenarios: [
        mockScenario('单条 60 秒', 1),
        mockScenario('每天 5 条 60 秒', 5),
        mockScenario('30 天每天 5 条', 150),
      ],
      cost_notice_version: 'mock',
      selected_route: _workId ? (db.works.find((x) => x.id === _workId)?.route ?? null) : null,
      route_confirmed: _workId ? routeConfirmedWorks.has(_workId) : false,
      note: 'MOCK：声明口径与实测口径必须分列；估价不等于付费授权。',
    }
  },

  async getTalkingHeadQueue(workId?: string): Promise<QueueView> {
    await wait(80)
    return {
      work_id: workId ?? null,
      gpu_holder: null,
      counts: {
        intent: 0, blocked: 0, queued: 0, leased: 0,
        succeeded: 0, failed: 0, unknown: 0, cancelled: 0,
      },
      heavy_jobs: [],
      serialized: true,
      note: '单 GPU 一次只允许一个有效重型作业；云路线（OmniHuman）不占本地 GPU',
    }
  },

  async getTalkingHeadAdapters(): Promise<AdapterStatusView> {
    await wait(80)
    return {
      dashscope_wan: { provider: 'dashscope_wan', model: 'wan3.0-video', configured: false,
        state: 'not_configured', verified: {}, detail: 'MOCK：未调用官方接口，账号权限与口型效果未核验' },
      omnihuman: {
        provider: 'volcengine',
        model: 'omnihuman-1.5',
        configured: false,
        state: 'not_configured',
        declared: { audio_max_seconds_exclusive: 60, prompt_max_chars: 300 },
        verified: {},
        detail: '未配置凭据（MOCK）：端点与模型名齐全不等于可用',
      },
      m2_worker: {
        provider: 'm2_worker',
        model: 'LivePortrait+MuseTalk1.5',
        configured: false,
        state: 'not_configured',
        verified: {},
        detail: '未配置 M2 推理通道（MOCK）：不在本机伪造结果',
      },
    }
  },

  async getTalkingHeadPreflight(workId: string): Promise<TalkingHeadPreflight> {
    await wait(120)
    const w = db.works.find((x) => x.id === workId)!
    const audio = (db.audios[workId] ?? []).find((a) => a.kind === 'full' && a.confirmed) ?? null
    const blockers: PreflightBlocker[] = []
    if (!w.identity_id)
      blockers.push({ code: 'IDENTITY_REQUIRED', message: '尚未绑定人物：口播必须基于已审人物', kind: 'rejected' })
    if (!audio)
      blockers.push({
        code: 'FINAL_AUDIO_REQUIRED',
        message: '尚无已确认的最终音频：口型不能基于试听片或未确认音频',
        kind: 'blocked',
      })
    return {
      work_id: workId,
      route: w.route,
      ok: blockers.length === 0,
      blockers,
      rejected: blockers.filter((b) => b.kind === 'rejected'),
      identity_id: w.identity_id,
      variant_id: null,
      script_version_id: w.script_version_id,
      performance_plan_id: null,
      audio_asset_id: audio?.id ?? null,
      audio_duration_ms: audio?.duration_ms ?? null,
      plate_id: null,
      motion_intent: {
        state: 'unspecified',
        free_text: '',
        reason: '表演描述交由云路线执行；不承诺任意动作，需实测指令遵循程度',
      },
      note: '前置不通过不得提交；rejected 类需要改输入，blocked 类需要等外部条件',
    }
  },

  async getWanImage() { return { source: null } },
  async uploadWanImage() { throw new ApiError('WAN_MOCK_UNAVAILABLE', '离线模式不保存 Wan 参考图片，请连接真实后端。') },
  async confirmWanNativeVideo() { throw new ApiError('WAN_MOCK_UNAVAILABLE', '离线模式没有可确认的真实 Wan 视频。') },

  async getTalkingHeadCost(workId: string, route?: GenerationRoute, prompt = '', resolution = '1080p'): Promise<TalkingHeadCostView> {
    await wait(120)
    const w = db.works.find((x) => x.id === workId)!
    const r = route ?? w.route
    if (r === 'WAN_3_0') return {
      work_id: workId, route: r, confirmation: null, current_call_list: null, confirmation_valid: false,
      changed_fields: [], estimate: null, billable_allowed: false, price_snapshots: {},
      budget: { per_task_limit_fen: 0, daily_limit_fen: 0, max_billable_submissions: 0, spent_today_fen: 0, submissions_today: 0 },
      blocker: { code: 'WAN_MOCK_UNAVAILABLE', message: '离线模式不能确认真实 Wan 报价。' },
      note: 'MOCK 不产生真实视频、合成费用或质量通过结论。',
    }
    const confirmation = db.costConfirmations[workId]?.[r] ?? null
    const listing = mockCallList(workId, r, prompt, resolution)
    const valid = !!confirmation && confirmation.request_digest === listing.digest
    return {
      work_id: workId,
      route: r,
      confirmation,
      current_call_list: listing,
      confirmation_valid: valid,
      changed_fields: confirmation && !valid ? ['调用清单'] : [],
      estimate: r === 'OMNIHUMAN_1_5'
        ? {
            route: r, estimated_fen: 6000, ceiling_fen: 7200, currency: 'CNY',
            price_basis: '用户已确认 1 元/生成秒（100 分/秒）；仅视频生成费',
            billable: true, billing_verified: false, segment_count: 1, billing_seconds: 60,
            note: 'MOCK 估价：不等于真实账单，也不等于付费授权',
          }
        : {
            route: r, estimated_fen: 0, ceiling_fen: 0, currency: 'CNY',
            price_basis: '本地 GPU 作业：不按生成秒计价，按实际卡时计入成本',
            billable: false, segment_count: 1, billing_seconds: null,
            note: 'MOCK：估价 0 元不等于零成本，卡时仍计入',
          },
      budget: {
        per_task_limit_fen: 0,
        daily_limit_fen: 0,
        max_billable_submissions: 0,
        spent_today_fen: 0,
        submissions_today: 0,
      },
      price_snapshots: {},
      billable_allowed: false,
      note: '估价、预留与实际费用分开记录；用户报价不是本轮付费授权',
    }
  },

  async confirmTalkingHeadCost(
    workId: string,
    input: { route: GenerationRoute; prompt: string; resolution: string },
  ): Promise<CostConfirmation> {
    await wait(200)
    const { route, prompt, resolution } = input
    if (route === 'WAN_3_0') throw new ApiError('WAN_MOCK_UNAVAILABLE', '离线模式不能确认真实 Wan 报价')
    const segCount = 1
    const listing = mockCallList(workId, route, prompt, resolution)
    const est = route === 'OMNIHUMAN_1_5' ? 6000 : 0
    const rec: CostConfirmation = {
      id: nextId('cost'),
      work_id: workId,
      route,
      provider: route === 'OMNIHUMAN_1_5' ? 'volcengine' : 'm2_worker',
      model: route === 'OMNIHUMAN_1_5' ? 'omnihuman-1.5' : 'LivePortrait+MuseTalk1.5',
      request_digest: listing.digest,
      call_list: listing,
      estimated_fen: est,
      ceiling_fen: Math.round(est * 1.2),
      segment_count: segCount,
      generated_seconds: 60,
      resolution: listing.resolution,
      accepted_risks: ['效果不满意仍可能收费', '重生成可能再次收费', '取整/最低消费/失败扣费尚未核验'],
      actor_id: 'user_local',
      confirmed_at: nowIso(),
      valid_until: null,
      expiry_policy: 'invalidate_on_change',
      expiry_note: '清单摘要、段数、分辨率或预算策略变化即失效；不按时间自动过期',
      authorizes_payment: false,
      note: 'MOCK：确认了清单与金额，但报价不是付费授权',
    }
    db.costConfirmations[workId] = { ...(db.costConfirmations[workId] ?? {}), [route]: rec }
    return rec
  },

  async createSegmentPlan(workId: string, limitMs?: number): Promise<SegmentPlanView> {
    await wait(200)
    const audio = (db.audios[workId] ?? []).find((a) => a.kind === 'full' && a.confirmed)
    if (!audio) throw new ApiError('FINAL_AUDIO_REQUIRED', '尚无已确认最终音频，分段计划不成立')
    // 离线模式没有字词级对齐产物，因此拒绝建立计划，而不是退化成"字符平均切"：
    // 平均切会在句子中间断开，而这类错误在界面上完全看不出来（每段时长都很整齐）。
    const limit = limitMs ?? 59000
    throw new ApiError(
      'ALIGNMENT_REQUIRED',
      `MOCK 模式没有字词级对齐产物（单段上限 ${limit} ms）：分段只能来自同一音轨的句边界时间戳，不做字符平均切`,
    )
  },

  async getSegmentPlan(workId: string): Promise<SegmentPlanView> {
    await wait(80)
    return {
      work_id: workId,
      planned: false,
      reason: '尚未建立分段计划（需要已确认音频与可用对齐）',
    }
  },

  async getPlateStatus(workId: string): Promise<PlateStatusView> {
    await wait(80)
    return { work_id: workId, plate_count: 0, approved_plate_id: null, approved: false, plates: [] }
  },

  // 动作意图：MOCK 只回一份目录与空 approved，绝不自造"已获准参考"——
  // 那会让界面显示一个真实后端根本不会接受的选项。
  async listMotions() {
    await wait(120)
    return {
      catalog: {
        nod: { label: '轻微点头', params: { amplitude: [0.1, 0.6], speed: [0.5, 1.5] } },
        head_turn_small: { label: '小幅转头', params: { amplitude: [0.1, 0.5], speed: [0.5, 1.5] } },
        gesture_open: { label: '开放手势', params: { amplitude: [0.2, 0.8], speed: [0.5, 1.5] } },
        smile: { label: '微笑', params: { intensity: [0.1, 0.7] } },
        serious: { label: '严肃陈述', params: { intensity: [0.1, 0.6] } },
      },
      approved: [],
    }
  },
  async listMotionTemplates() {
    await wait(120)
    return { templates: [], note: '模拟模式没有真实动作视频，请连接正式后端读取已有动作。' }
  },
  async listMotionUploads(_identityId: string): Promise<{ uploads: MotionUploadView[] }> {
    await wait(80)
    return { uploads: [] }
  },
  async uploadMotion(): Promise<MotionUploadView> {
    throw new ApiError('MOCK_UPLOAD_UNAVAILABLE', '模拟模式不接收动作视频；请连接正式后端上传真实参考。')
  },
  async confirmMotionUpload(): Promise<MotionReferenceView> {
    throw new ApiError('MOCK_UPLOAD_UNAVAILABLE', '模拟模式不登记动作参考；请连接正式后端确认。')
  },
  async importMotionTemplate() {
    throw new ApiError('MOTION_TEMPLATE_UNAVAILABLE', '模拟模式没有真实动作视频，不能添加可用动作。')
  },
  async setMotionIntent(_workId: string, input: { motion_key: string; motion_ref_id?: string }) {
    await wait(150)
    throw new ApiError(
      'MOTION_NOT_APPROVED',
      `MOCK 模式没有已获准的动作参考，动作「${input.motion_key}」无法登记；请用真实后端`,
    )
  },
  async admitVariant() {
    await wait(120)
    return { ok: false, outcome: 'review', asset_id: '', identity_id: '', reasons: ['MOCK 模式不做真实准入判定'] }
  },

  async createPlate(workId: string, outputSeconds: number, _variantId?: string) {
    await wait(300)
    return {
      registered: false,
      is_mock: true,
      duration_ms: Math.round(outputSeconds * 1000),
      note: '占位底片：MOCK 下不产生真实字节，不登记为可用资产，不继承任何通过结论',
    }
  },

  async reviewPlate() {
    await wait(200)
    throw new ApiError('PLATE_NOT_FOUND', 'MOCK 模式没有登记底片，无可审对象')
  },

  async submitTalkingHead(
    workId: string,
    input: { route: GenerationRoute; prompt: string; resolution: string; dry_run: boolean },
  ): Promise<SubmissionResult> {
    await wait(400)
    const confirmation = db.costConfirmations[workId]?.[input.route]
    if (!confirmation)
      throw new ApiError(
        'COST_CONFIRMATION_REQUIRED',
        input.route === 'M2'
          ? 'M2 没有云单价，但会占用本机 GPU 卡时：提交前须先确认调用清单与预算边界'
          : `${input.route} 是计费路线：必须先展示调用清单与估价并经用户确认`,
        true,
      )
    if (confirmation.request_digest !== mockCallList(workId, input.route, input.prompt, input.resolution).digest) {
      throw new ApiError('COST_LIST_CHANGED', '调用清单已变化，请重新确认本次调用清单与费用')
    }
    // 默认 0 预算下不发生任何副作用：报价确认不等于付费授权。
    throw new ApiError(
      'BUDGET_NOT_AUTHORIZED',
      '预算未授权（per_task=0、max_submissions=0）：报价确认不等于付费授权',
      true,
    )
  },

  async getSubmissions(workId: string): Promise<SubmissionListView> {
    await wait(80)
    return {
      work_id: workId,
      runs: [],
      auto_fallback: false,
      note: '失败与未知一律保留原记录；跨路线回退需要用户重新选择，不自动发生',
    }
  },

  async pollRun() {
    await wait(150)
    throw new ApiError('RUN_NOT_FOUND', 'MOCK 模式没有外部作业可查')
  },

  async settleRun() {
    await wait(150)
    throw new ApiError('RUN_NOT_FOUND', 'MOCK 模式没有需要核对的未知结果')
  },

  async getTalkingHeadDriver(workId: string): Promise<TalkingHeadDriverView> {
    await wait(60)
    return {
      work_id: workId, state: 'IDLE', total: 0, succeeded: 0, in_flight: 0,
      pending: 0, failed: 0, unknown: 0, resumable: false, stop_reason: null,
      blocked_runs: [], attempt: null,
      note: 'MOCK 模式没有真实分段计划：不伪造可续发的进度',
    }
  },

  async resumeTalkingHeadDriver(workId: string): Promise<TalkingHeadDriverView> {
    await wait(120)
    return {
      work_id: workId, state: 'IDLE', total: 0, succeeded: 0, in_flight: 0,
      pending: 0, failed: 0, unknown: 0, resumable: false, stop_reason: null,
      blocked_runs: [], attempt: null, started: false,
      note: 'MOCK 模式没有真实分段计划：不伪造可续发的进度',
    }
  },

  async getVideos(): Promise<VideoAsset[]> {
    await wait(80)
    // 不伪造视频产物：空列表是离线模式唯一诚实的答案。
    return []
  },

  async getVideoGate() {
    await wait(80)
    throw new ApiError('VIDEO_NOT_FOUND', 'MOCK 模式没有登记视频，无法给出门禁结论')
  },

  async getTalkingHeadGate(workId: string): Promise<TalkingHeadGateSummary> {
    await wait(80)
    const w = db.works.find((x) => x.id === workId)!
    return {
      work_id: workId,
      route: w.route,
      route_selection_id: null,
      plate_count: 0,
      original_count: 0,
      final_video_id: null,
      original_confirmed: false,
      gate: null,
      note: '无已确认原片时 gate 为 null：不把「尚未审核」写成通过',
    }
  },

  async reviewVideo() {
    await wait(200)
    throw new ApiError('VIDEO_NOT_FOUND', 'MOCK 模式没有登记视频，无可审对象')
  },

  async recordAudioTrackCheck() {
    await wait(200)
    throw new ApiError('VIDEO_NOT_FOUND', 'MOCK 模式没有登记视频，无法核对音轨')
  },

  async confirmFinalVideo() {
    await wait(200)
    throw new ApiError('VIDEO_NOT_FOUND', 'MOCK 模式没有登记原片，无法确认')
  },
}
