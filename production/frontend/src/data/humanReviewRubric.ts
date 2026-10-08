import type { HumanReviewRubric } from '@/api/types'

export interface HumanReviewCriterion {
  id: string
  title: string
  focus: string
  optional?: boolean
  levels: [string, string, string, string, string]
}

export const humanReviewRubric: HumanReviewCriterion[] = [
  {
    id: 'hook', title: '前三秒吸引力', focus: '只评开头：目标受众能否迅速识别与自己有关的问题、收益或悬念。', levels: [
      '以问候、自我介绍或空泛铺垫开场，前三秒看不出主题，也没有继续看的理由。',
      '提到了主题，但只说“很重要”“很有用”，问题或收益含糊，进入正文太慢。',
      '能识别具体问题或收益，但引子偏长、常见，吸引点需要前三秒之后才明确。',
      '前三秒用简洁台词或明确画面提出具体问题、收益或悬念，与目标受众直接相关。',
      '满足 4 分，并用准确具体的反差、场景或可兑现的细节形成鲜明记忆点，台词与画面配合紧凑。',
    ],
  },
  {
    id: 'momentum', title: '剧情张力 / 内容推进力', focus: '评中段的持续期待。故事看目标、障碍、后果；科普与教程看问题、认知反差、证据或方法的递进，不强求戏剧冲突。', levels: [
      '中段只是零散陈述或重复观点，缺乏问题推进，看不出下一段为什么值得看。',
      '有问题或矛盾，但过早给完答案或长时间不推进，主要靠夸张措辞制造刺激。',
      '有基本推进和一个值得追问的节点，但部分段落重复、转折生硬或后果说明不足。',
      '各关键段落带来新信息；问题、障碍或认知反差自然递进，让人期待下一步。',
      '满足 4 分，并有合理、非生硬的转折或逐层揭示；推进建立在内容本身，删去夸张措辞仍成立。',
    ],
  },
  {
    id: 'interaction', title: '互动潜力', focus: '评观众有没有与主题相关、具体且容易回应的表达空间，不以“点赞关注评论”的口号计分。', levels: [
      '没有可回应的讨论点，或仅有与内容无关的点赞、关注口号。',
      '只有“你怎么看”之类空泛提问，回应门槛高，或用无依据争议强行引导评论。',
      '有相关问题，但范围过宽、答案过于单一，观众不容易提供自己的经历或理由。',
      '有具体、低门槛的选择、经历或观点问题，观众能自然说出自己的答案与理由。',
      '满足 4 分，并能自然容纳不同经验与合理观点，回应内容对其他观众也有参考价值。',
    ],
  },
  {
    id: 'empathy', title: '代入感', focus: '评目标受众的真实处境、感受与选择是否被准确写出，而非单看情绪强弱。', levels: [
      '对象不明确，场景和诉求与目标受众无关，或呈现明显失真的刻板印象。',
      '只用“大家都很焦虑”等笼统标签，没有具体处境、行为或困难。',
      '有相关场景，观众能理解，但细节泛化，角色选择或感受缺少依据。',
      '以具体、可信的场景和行为呈现受众困扰，表达贴近其经验，容易产生“我也遇到过”的感受。',
      '满足 4 分，并写出受众容易忽略但真实的顾虑或两难选择，以准确细节形成共鸣而不强行煽情。',
    ],
  },
  {
    id: 'structure', title: '文案结构 / 完播意愿', focus: '评整篇的信息顺序、段落承接与结尾兑现。开头是否有钩子归第 1 项，中段的新信息推进归第 2 项。', levels: [
      '缺少完整主线，信息顺序混乱，结尾缺失或与开头承诺无关。',
      '有开头、中段、结尾，但篇幅失衡、跳跃明显，关键答案或结论缺失。',
      '主线完整、基本能兑现承诺，但有可整段删除的重复或旁枝，需要调整段落顺序或压缩。',
      '开头设定的问题、中段展开和结尾回收连贯；信息量与时长匹配，只有局部冗余或小修。',
      '满足 4 分，各段都有明确作用，结尾清晰回收开头承诺并留下有价值的总结或行动，没有明显可删的空转段落。',
    ],
  },
  {
    id: 'value', title: '内容价值', optional: true, focus: '评观众看完获得的新理解、判断依据或可执行方法；事实是否成立另列核对。', levels: [
      '主要是口号或常识堆叠，看完没有明确获得。',
      '提出观点或建议，但缺少原因、适用场景或具体做法，难以利用。',
      '有一个明确收获，但例子、步骤或边界不够具体，还需补充才能用于实际判断。',
      '提供清楚的新解释、判断依据或具体方法，说明适用场景，目标受众能够复述或尝试。',
      '满足 4 分，并用有针对性的例子、对照或限制条件帮助观众迁移应用，避免只记住一个口号。',
    ],
  },
  {
    id: 'delivery', title: '口播表达', optional: true, focus: '评台词是否自然、易懂、好念，句长、术语、停顿和估读时长是否适合数字人口播。', levels: [
      '长句、歧义或错误表达大量存在，朗读难以听懂，台词无法直接用于口播。',
      '有明显书面腔、术语堆积或绕口句，需要重写多处才能自然表达。',
      '大体能顺畅朗读，但仍有多处长句、未解释术语或节奏问题，需要逐句调整。',
      '用词口语化、句子清晰、停顿自然；必要术语有解释，估读时长与目标时长大致匹配。',
      '满足 4 分，重点与语气有清楚落点，朗读顺畅且表达有辨识度，无需实质改写。',
    ],
  },
  {
    id: 'execution', title: '分镜可执行性 / 声画配合', optional: true, focus: '评画面、台词与镜头时长是否可实现。人物、服装、道具和场景的连续性要在剧本中明确。', levels: [
      '关键镜头没有可呈现的画面，或台词与画面严重矛盾，现有制作能力无法实现。',
      '多处镜头描述含糊、声画脱节或时长不可行；人物、服装、道具连续性频繁冲突。',
      '整体可制作，但部分镜头需要补充画面、拆分动作或明确连续性约束后才能执行。',
      '各镜头画面与台词相互支持；镜头时长合理，人物、服装、道具、场景约束清楚，仅需局部调整。',
      '满足 4 分，并用适当的示例画面、演示或对照降低理解负担，镜头安排精简，关键生成限制已有可行替代方案。',
    ],
  },
]

export function activeHumanReviewCriteria(mode: HumanReviewRubric) {
  return humanReviewRubric.filter(item => !item.optional || mode === 'extended8')
}

export function calculateHumanReview(mode: HumanReviewRubric, scores: Record<string, number>) {
  const criteria = activeHumanReviewCriteria(mode)
  const values = criteria.map(item => scores[item.id] ?? 0)
  const filled = values.filter(value => Number.isInteger(value) && value >= 1 && value <= 5).length
  const complete = filled === criteria.length
  const total = values.reduce((sum, value) => sum + value, 0)
  return { count: criteria.length, filled, complete, total, average: complete ? total / criteria.length : null,
    qualified: complete && total > 4 * criteria.length }
}
