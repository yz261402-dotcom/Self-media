"""将讲解片段组装为可离线阅读的单文件 HTML。只写本目录上级的讲解版文件。"""
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent

DEMOS = {
    "<!--QPS_DEMO-->": '''
<section class="lab" aria-labelledby="qpsTitle">
  <header><div><h4 id="qpsTitle">动手算一算：多少次查询，会产生多少 QPS？</h4><p class="lab-caption">假设每个客户端以固定频率查询进度；这是请求量演示，不测量当前系统。</p></div><span class="badge example-tag">假设示例</span></header>
  <div class="lab-grid"><div class="interactive-controls">
    <label class="field" for="qpsClients"><span>同时查询的客户端：<strong id="clientsValue">10</strong> 个</span><input type="range" id="qpsClients" min="1" max="100" step="1" value="10"><span class="range-label"><span>1 个</span><span>100 个</span></span></label>
    <label class="field" for="qpsFrequency"><span>每个客户端每秒查询：<strong id="frequencyValue">2</strong> 次</span><input type="range" id="qpsFrequency" min="1" max="5" step="1" value="2"><span class="range-label"><span>1 次/秒</span><span>5 次/秒</span></span></label>
  </div><div class="demo-result"><output class="demo-number" id="qpsValue" aria-live="polite">20</output><div class="demo-unit">次请求 / 秒 · QPS</div><p>这 20 次请求可以全部是查询同一个视频的进度。它不代表每秒制作 20 条视频。</p></div></div>
  <p class="formula" id="qpsFormula">10 个客户端 × 每秒 2 次查询 = 20 次请求/秒</p>
</section>''',
    "<!--PERCENTILE_DEMO-->": '''
<section class="lab" aria-labelledby="percentileTitle">
  <header><div><h4 id="percentileTitle">看懂 P95 / P99：少数请求变慢，会发生什么？</h4><p class="lab-caption">100 个固定构造的假设请求，按耗时从短到长排列；数据仅用于说明百分位。</p></div><span class="badge example-tag">假设示例</span></header>
  <label class="field interactive-controls" for="tailFactor"><span>只把最慢的 5 个请求放慢：<strong id="tailValue">1 倍</strong></span><input type="range" id="tailFactor" min="1" max="4" step="1" value="1"><span class="range-label"><span>保持原样</span><span>放大到 4 倍</span></span></label>
  <div class="metric-pair"><div><small>P95 · 排序后的第 95 个</small><strong id="p95Value">1.6 秒</strong></div><div><small>P99 · 排序后的第 99 个</small><strong id="p99Value">3.5 秒</strong></div></div>
  <p class="chart-title">请求耗时（秒）</p><div class="chart-frame" role="img" aria-labelledby="percentileTitle" aria-describedby="percentileSummary"><div class="y-axis" aria-hidden="true"><span id="yMax">4.2</span><span id="yMid">2.1</span><span>0</span></div><div class="percentile-bars" id="percentileBars" aria-hidden="true"></div></div><div class="x-axis" aria-hidden="true"><span>第 1 个</span><span>按耗时排序的请求编号</span><span>第 100 个</span></div>
  <p class="chart-note">灰色：前 95 个请求；蓝色：最慢的 5 个；黑色：P95 / P99 对应位置。纵轴上限随示例变化，数值以标注为准。</p><p class="chart-note" id="percentileScenario">示例设置：仅把排序后最慢的 5 个请求耗时放大 1 倍。</p>
  <p id="percentileSummary" aria-live="polite">100 个假设请求按耗时排序，P95 取第 95 个，P99 取第 99 个。当前 P95 为 1.6 秒，P99 为 3.5 秒。</p>
  <p class="lab-caption">保持前 95 个请求不变时，P95 可以不变，而 P99 显著上升。这就是为什么只报平均值或 P95，可能看不到少数用户的长时间等待。100 个构造样本仅用于讲解，不足以证明稳定的生产 P99。</p>
</section>''',
    "<!--GPU_DEMO-->": '''
<section class="lab" aria-labelledby="gpuTitle">
  <header><div><h4 id="gpuTitle">GPU 容量粗估：给任务留出足够的计算时间</h4><p class="lab-caption">假设任务可分配到同类 GPU，每项任务的全部 GPU 阶段合计占用指定分钟数。</p></div><span class="badge example-tag">假设示例</span></header>
  <div class="form-grid interactive-controls"><label class="field" for="gpuJobs"><span>窗口内任务数（个）</span><input id="gpuJobs" type="number" min="1" max="100000" step="1" value="30"></label><label class="field" for="gpuMinutes"><span>每任务 GPU 时间（分钟）</span><input id="gpuMinutes" type="number" min="0.1" max="10000" step="0.1" value="5"></label><label class="field" for="gpuWindow"><span>可用时间窗口（分钟）</span><input id="gpuWindow" type="number" min="1" max="100000" step="1" value="60"></label><label class="field" for="gpuUtil"><span>目标利用率（%）</span><input id="gpuUtil" type="number" min="1" max="100" step="1" value="70"></label></div>
  <div class="gpu-answer"><output class="demo-number" id="gpuValue" aria-live="polite">4</output><span class="demo-unit">张 GPU · 向上取整的粗估</span></div><p class="formula" id="gpuFormula">30 × 5 ÷ 60 ÷ 0.7 = 3.57，向上取整为 4 张</p><p id="gpuError" class="input-error" hidden role="status">请输入范围内的有效数字；任务数与窗口须为正整数，利用率为 1–100%。</p>
  <p class="lab-caption">这是工作量估算，不是采购建议或按时完成保证。还需验证显存、模型加载、任务突发、传输、失败重做和人工等待；OmniHuman 外部服务的吞吐不能按本机 GPU 数直接推算。</p>
</section>''',
}

GLOSSARY = [
    ("API / 接口", "网页与后台之间约定好的办事窗口，例如提交任务、查询进度。"),
    ("前端 / 后端", "前端展示页面和接收操作；后端检查业务条件、调用能力并保存正式记录。"),
    ("Agent / 智能体角色", "承担特定职责的执行角色，可按规则或模型决策调用受限工具；角色数不等于服务器数。"),
    ("DeepAgents / LangGraph", "组织角色、工具调用和流程状态的框架；具体生成能力由模型或工具提供。"),
    ("Skill / 业务技能", "某项工作的操作规范，例如口播写作或固定人物的审核要求。"),
    ("MCP / 工具通道", "按统一约定把工具提供给智能体使用；实际权限由白名单和业务校验控制。"),
    ("Provider / 能力供应商", "通过接口提供模型服务的平台，例如 DeepSeek、MiniMax、OmniHuman。"),
    ("Worker / 执行程序", "接收具体任务、调用模型或媒体工具、记录并返回结果的程序。它不等于 GPU 本身。"),
    ("GPU / 显卡计算", "适合大量并行数值计算的硬件资源；本项目的自管模型当前运行在租用的云 GPU 上。"),
    ("推理", "给已经训练好的模型输入资料，让它生成或识别结果；并不是重新训练模型。"),
    ("TTS / 文字转语音", "把批准的文字转换为配音；项目主要使用 MiniMax。"),
    ("ASR / 语音识别", "听一段声音，识别它说了什么。项目接入 Qwen3-ASR。"),
    ("Forced Alignment / 强制对齐", "在已知正文的情况下，确定字词在音频中何时出现，用于字幕时间轴。"),
    ("M2 / 自管 GPU 路线", "先用 LivePortrait 制作动作底片，再用 MuseTalk 按最终配音对口型。当前 GPU 位于云端。"),
    ("OmniHuman / 云视频路线", "外部音频驱动人物视频服务；当前提交入口要求音频严格小于 60 秒，超限阻断。"),
    ("Work / Task / Job", "Work 是作品档案；Task 是制作任务；Job 通常是交给某个执行器或供应商的具体作业。"),
    ("状态机", "规定任务可以处于哪些状态，以及怎样从排队、执行或等待确认走向成功、失败等结果。"),
    ("门禁 / Gate", "动作执行前必须满足的条件，例如最终音频已确认、人物已审核、预算已授权。"),
    ("幂等 / Idempotency", "识别同一次操作的重复请求，避免反复执行；可靠范围取决于具体实现与持久化。"),
    ("unknown / 结果未知", "目前证据不足以确定外部任务的最终结果，需要查询原作业和费用记录，不能盲目重提。"),
    ("版本 / 父资产 / 血缘", "记录成片使用了哪版正文、哪张人物图和哪段配音，使结果可追溯并能识别旧产物失效。"),
    ("hash / SHA-256", "文件内容的指纹，用于检查是否还是审核时的同一份文件；不评价内容质量。"),
    ("manifest / 交付清单", "描述交付包有哪些文件及其大小、指纹和相关编号的清单。"),
    ("SQLite / PostgreSQL", "保存业务记录的数据库。当前使用本机 SQLite；独立部署的 PostgreSQL 属于后续目标。"),
    ("Redis / 队列", "Redis 可参与共享任务队列、缓存和锁；队列决定任务如何排队和被领取，不能替代全部业务账本。"),
    ("JobStore / 作业账本", "保存 Worker 作业状态和索引的地方。当前云 Worker 版本保存在进程内存中，重启可能丢失。"),
    ("持久化 / checkpoint", "把状态保存到可靠存储，而非只放在运行进程内；checkpoint 记录流程执行到哪一步。"),
    ("租约 / 锁", "控制任务或 GPU 在一段时间内由谁使用，减少多个执行者重复领取或争抢资源。"),
    ("死信队列", "保存多次执行失败、需要人工分析和处理的任务，避免无限重试。"),
    ("对象存储 / S3 / MinIO / TOS", "保存媒体文件的独立仓库。项目已有部分 TOS 素材桥接，统一媒体存储改造仍是后续目标。"),
    ("签名 URL", "带授权和有效期限的文件访问地址，常用于受控下载或向服务提供输入。"),
    ("SSH 隧道", "连接本机后端与云端 Worker 的加密转发通道。通道连通与 Worker 运行需要分别检查。"),
    ("127.0.0.1 / 端口", "127.0.0.1 指当前机器自己；端口用于区分这台机器上不同服务的入口。"),
    ("ensure / guard / systemd", "ensure 检查并在安全条件下拉起 Worker；guard 是当前自有守护程序；systemd 是另一种进程管理方案。"),
    ("QPS / RPS", "每秒请求数量。本汇报用于接口受理或查询吞吐，不能换算成每秒完成多少条视频。"),
    ("P95 / P99", "将耗时从小到大排序，取 95% / 99% 位置的值。它们不是平均值，也不是成功率。"),
    ("SSE / 进度推送", "服务端持续向页面推送进度的连接；长连接数量不等于视频生成并发数。"),
    ("jobs/min / 端到端耗时", "前者是每分钟完成的作业数，须定义作业范围；后者统计从受理到合格交付的全过程。"),
    ("CER / 字错误率", "相对批准朗读文本的替换、漏字、添字总数除以字符数；人名、术语、数字还需单独严格检查。"),
    ("fps / CFR / H.264 / AAC", "fps 表示每秒帧数，CFR 表示帧率恒定，H.264 和 AAC 分别是视频与音频编码。"),
    ("LUFS / 响度", "衡量音频整体听感响度的单位，用于控制成片声音水平，不是模型生成速度。"),
    ("L0 / L1 / L2", "依次验证程序与接口规则、真实生产交付、平台发布事实；不同层级的证据不能互相替代。"),
    ("PASS / FAIL / BLOCKED / NOT_RUN", "分别表示验收通过、实测不合格、缺前提受阻、尚未执行。未测不能当通过。"),
    ("SSO / RBAC", "SSO 是统一登录；RBAC 是按用户角色分配权限，例如制作、审核、管理。"),
    ("备份 / PITR", "备份保存可恢复副本；PITR 指恢复到某个时间点。需要实际恢复演练证明可用。"),
    ("Prometheus / Grafana / Loki", "分别用于指标采集、监控图表、日志汇集与查询；本项目完整监控平台属于后续目标。"),
    ("OpenTelemetry / trace_id", "串起一次任务经过多个服务的记录；trace_id 是用于追踪和定位问题的关联编号。"),
    ("Secrets Manager / 凭据轮换", "集中管理服务密钥并在需要时更换，避免凭据进入网页、日志或智能体上下文。"),
    ("灰度 / 金丝雀发布", "先让少量使用范围运行新版，观察正常后逐步扩大，降低更新影响范围。"),
]


def main():
    template = (ROOT / "template.html").read_text(encoding="utf-8")
    chapters = "\n".join((ROOT / name).read_text(encoding="utf-8") for name in (
        "sections-01-04.html", "sections-05-08.html"))
    for marker, demo in DEMOS.items():
        if chapters.count(marker) != 1:
            raise ValueError(f"演示插入标记必须出现一次：{marker}")
        chapters = chapters.replace(marker, demo)
    glossary = "\n".join(f'<div><dt>{escape(term)}</dt><dd>{escape(definition)}</dd></div>'
                         for term, definition in GLOSSARY)
    result = template.replace("<!--CHAPTERS-->", chapters).replace("<!--GLOSSARY-->", glossary)
    if any(marker in result for marker in ("<!--CHAPTERS-->", "<!--GLOSSARY-->", *DEMOS)):
        raise ValueError("存在未替换内容")
    output = ROOT.parent / "项目白话讲解.html"
    output.write_text(result, encoding="utf-8")
    print(f"已生成 {output.name}；{len(result):,} 字符；{len(GLOSSARY)} 个术语")


if __name__ == "__main__":
    main()
