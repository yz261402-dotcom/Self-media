"""由当前权威文档和已验证图形生成离线汇报页，不调用模型或云服务。"""
from pathlib import Path
from hashlib import sha256
from html import escape
from urllib.parse import quote
from datetime import datetime
import json
import re

from bs4 import BeautifulSoup
from markdown_it import MarkdownIt

HERE = Path(__file__).resolve().parent
SPECS = HERE.parent
DOCS = SPECS / "0001-douyin-ai-studio"
REFERENCE = Path("C:/Users/22346/Downloads/project-blueprint-v2.html")
SOURCES = [DOCS / name for name in (
    "00-文档导航.md", "03-总体技术方案.md", "04-接口契约与数据字典.md",
    "05-开发实施计划.md", "06-验收标准与测试方案.md",
    "08-端到端技术流程与模型选型地图.md", "09-模型选型决策与接入方案.md",
    "10-Agent编排选型与整体架构.md",
)]


def source_dom(name):
    return BeautifulSoup(MarkdownIt("commonmark").enable("table").render(
        (DOCS / name).read_text(encoding="utf-8")), "html.parser")


evaluation = source_dom("06-验收标准与测试方案.md")
agent_doc = source_dom("10-Agent编排选型与整体架构.md")
flow_doc = source_dom("08-端到端技术流程与模型选型地图.md")
core_table = next(t for t in evaluation.find_all("table") if "计算方法（简版）" in t.get_text())
full_table = next(t for t in evaluation.find_all("table") if "指标（★核心）" in t.get_text())
core_ids = [re.search(r"\bE\d{2}\b", r.get_text()).group() for r in core_table.select("tbody tr")]
assert len(core_ids) == len(set(core_ids)) == 11
assert len(full_table.select("tbody tr")) == 31
skills_table = next(t for t in agent_doc.find_all("table") if "S01 选题与新意" in t.get_text())
mcp_table = next(t for t in agent_doc.find_all("table") if "M01" in t.get_text() and "search_content_history" in t.get_text())
assert len(skills_table.select("tbody tr")) == 7
assert len(mcp_table.select("tbody tr")) == 4
assert "S07" not in skills_table.get_text()
data_table = next(t for t in flow_doc.find_all("table") if "D01 需求进入作品" in t.get_text())


def table_html(table, ident):
    result = BeautifulSoup(str(table), "html.parser").table
    result["id"] = ident
    for th in result.select("thead th"):
        th["scope"] = "col"
    return f'<div class="table-scroll" tabindex="0" aria-label="可横向滚动的表格">{result}</div>'


def link(path, label, css=""):
    return f'<a class="{css}" href="{quote(path, safe="/.-#")}">{escape(label)}</a>'


def doc_link(name, label):
    return link("../0001-douyin-ai-studio/" + name + ".html", label)


GRAPH_CSS = """
svg{font-family:'Microsoft YaHei','PingFang SC',Arial,sans-serif;background:#fff}
.c-grid{fill:none;stroke:#f1f3f6}.c-mask{fill:#fff;stroke:none}
.c-frontend,.c-backend,.c-database,.c-cloud,.c-security,.c-messagebus,.c-external{fill:#f3f6ff;stroke:#657eb8}
.c-security{fill:#eef2ff;stroke:#002fa7}.c-backend{fill:#fff;stroke:#8093bf}
.c-region,.c-lane,.c-security-group{fill:#fbfcff;stroke:#d9e1f0;stroke-dasharray:6 5}
.t-primary{fill:#172134}.t-muted{fill:#58657a}.t-dim{fill:#68748a}
.t-frontend,.t-backend,.t-database,.t-cloud,.t-security,.t-messagebus,.t-external{fill:#315398}
.m-default,.m-dashed{fill:#8290aa}.m-emphasis,.m-security{fill:#002fa7}
.a-default,.a-dashed{fill:none;stroke:#8290aa}.a-emphasis,.a-security{fill:none;stroke:#002fa7}
.a-security,.a-dashed{stroke-dasharray:5 4}
.s-frontend,.s-backend,.s-database,.s-cloud,.s-security,.s-messagebus,.s-external{fill:none;stroke:#526fa9}
.sigil-fill{fill:#526fa9;stroke:none}
[data-node-id]{cursor:pointer}[data-node-id]:hover>rect:not(.c-mask),[data-node-id][aria-pressed=true]>rect:not(.c-mask){stroke:#002fa7;stroke-width:2.5}
[data-node-id]:focus{outline:none}[data-node-id]:focus-visible>rect:not(.c-mask){stroke:#002fa7;stroke-width:3}
"""


def chart(source, key, title, caption):
    """复用经验证的图形几何，嵌入蓝色配色及本页节点交互。"""
    dom = BeautifulSoup(source.read_text(encoding="utf-8"), "html.parser")
    svg = max(dom.find_all("svg"), key=lambda s: len(str(s)))
    old_ids = [tag["id"] for tag in svg.select("[id]")]
    markup = str(svg)
    for old in sorted(old_ids, key=len, reverse=True):
        markup = markup.replace(f'id="{old}"', f'id="{key}-{old}"')
        markup = markup.replace(f'url(#{old})', f'url(#{key}-{old})')
    svg = BeautifulSoup(markup, "html.parser").svg
    svg["id"] = "svg-" + key
    svg["class"] = "diagram"
    svg["xmlns"] = "http://www.w3.org/2000/svg"
    svg["role"] = "group"
    svg["aria-label"] = title + "；点击节点查看说明"
    svg.attrs.pop("aria-labelledby", None)
    for element in svg.select("[data-node-id]"):
        element["data-chart"] = key
        element["aria-label"] = "查看：" + element.get("data-node-label", "模块")
        if element.get("data-node-context") == "Architecture component":
            element["data-node-context"] = "架构模块"
        box = element.find("rect", class_="c-mask")
        if box:
            top = float(box["y"])
            layout = {
                "system": [(21, 25), (16, 48), (14, 68)],
                "agents": [(17, 24), (14, 45), (12, float(box["height"]) - 10)],
                "flow": [(11, 21), (9, 36), (8, 50)],
            }[key]
            for text, (size, offset) in zip(element.find_all("text", recursive=False), layout):
                text["font-size"] = str(size)
                text["y"] = str(top + offset)
    for text in svg.find_all("text"):
        if key == "agents" and not text.find_parent(attrs={"data-node-id": True}):
            if text.get("font-size") == "9":
                text["font-size"] = "12"
                if text.get_text().startswith("4 类专业角色"):
                    # 把分组说明放在角色卡下方，避免覆盖从总控进入角色的连线。
                    text["y"] = "371"
        if text.get_text(strip=True) == "Legend":
            text.string = "图例"
    style = dom.new_tag("style")
    style.string = GRAPH_CSS
    svg.insert(0, style)
    SOURCES.append(source)
    if source.with_suffix(".json").exists():
        SOURCES.append(source.with_suffix(".json"))
    return f'''<div class="chart-card"><div class="chart-head"><strong>{title}</strong><div class="chart-actions"><button data-zoom="{key}" aria-pressed="false">放大</button><button data-export="{key}">下载 SVG</button></div></div>
    <div class="diagram-scroll" id="scroll-{key}" tabindex="0" aria-label="{title}，可横向滚动">{svg}</div>
    <div class="chart-caption">{caption}</div><div class="node-detail" id="detail-{key}" aria-live="polite"><span class="detail-label">节点说明</span><strong>点击图中模块</strong><p>查看该模块的职责和交接要求；支持 Tab、Enter 和空格操作。</p></div></div>'''


system_chart = chart(HERE / "整体分层架构.html", "system", "整体技术架构 · 六层职责", "六层展示系统组成与职责，不表示逐层流水线。Agent 经 MCP 和业务校验调用模型；后期 Worker 由业务服务直接调度，Skill 和记忆按需访问。当前为目标设计。")
agent_chart = chart(HERE / "Agent协作框架.html", "agents", "Agent 协作框架 · 一总控、四类专家", "展开整体架构中的 DeepAgents 协作层及其支撑：总控负责委派与汇合，四类子 Agent 按角色使用 Skill、MCP 和记忆；后期 Worker 属于代码执行层，成片返回 quality 终审。")
flow_chart = chart(SPECS / "架构图/作品生产数据流.html", "flow", "从用户需求到发布就绪包", "按提示直接生成三稿，quality 内部查重并核对已有材料；最终音频确认后字幕可与口播并行；原片通过＋字幕就绪后代码合成，再由 quality 终审，最后内部打包。")

features = [
    ("01", "人物形象", "V1", "用关键词与自由描述创建写实人物；固定身份后换衣、换景，表演要求可编辑。", "已选人物 B 是初始预设；新角色须审核。"),
    ("02", "口播稿生成", "V1", "输入类型、主题与受众，按用户提示直接生成三个不同角度的候选稿；不联网抓取。", "quality 内部查重和材料核对 → 用户选一篇。"),
    ("03", "TTS 语音", "V1", "同一句话试听选声，再合成全文；检查发音、情绪和实际时长。", "用户确认最终音频，才能进入口型生成。"),
    ("04", "视频合成", "V1", "选择路线与预算生成口播；音频确认后字幕可并行，原片通过后代码合成、清晰度处理和封面制作，终审后内部打包。", "预览、下载成片与完整发布就绪包。"),
    ("05", "发布", "后续", "抖音、B站、快手、小红书、视频号分别接入官方或获准发布方式。", "V1 显示开发状态；不假装已投稿。"),
    ("06", "指标监控", "后续", "取得权限后读取真实播放、完播、点赞和收藏，记录来源与统计窗口。", "不可获取不填 0；不展示虚构运营成绩。"),
    ("07", "领域模型选择", "后续", "提供已适配供应商的 URL、密钥和模型名设置，检查能力与错误原因。", "基础服务端配置与脱敏报错仍属于 V1。"),
]
features_html = "".join(f'<article class="feature"><div class="feature-top"><span class="number">{i}</span><span class="badge {"accent" if scope=="V1" else ""}">{scope}</span></div><h3>{title}</h3><p>{desc}</p><div class="feature-foot">{foot}</div></article>' for i,title,scope,desc,foot in features)

roles = [
    ("总控", "supervisor", "分解任务、并行委派、等待用户、汇总结果", "不代选人物、稿件或收费路线。"),
    ("策划文案", "content", "按用户提示提出新角度，直接生成三稿", "不联网；自带材料保留引用，无依据不伪称已核实。"),
    ("质量审核", "quality", "内部历史查重、已有材料核对及媒体终审", "事实缺依据转人工；原片通过后触发代码后期。"),
    ("数字人导演", "avatar", "管理人物和衣景，匹配表情动作，执行口播", "保持身份；不能承诺任意动作。"),
    ("声音导演", "audio", "编译停顿、重音、语速与情绪，生成 TTS", "锁定所选音色；产物交用户试听确认。"),
]
roles_html = "".join(f'<article class="role"><div class="role-head"><h3>{name}</h3><code>{ident}</code></div><p>{job}</p><small>{boundary}</small></article>' for name,ident,job,boundary in roles)
skills_html = ""
for row in skills_table.select("tbody tr"):
    cells = row.find_all("td")
    skills_html += f'<details class="skill"><summary>{cells[0].get_text(" ",strip=True)}</summary><p><b>负责角色</b> {cells[1].get_text(" ",strip=True)}</p><p>{cells[2].get_text(" ",strip=True)}</p><p class="muted">检查：{cells[3].get_text(" ",strip=True)}</p></details>'

models = [
    ("文本与决策", "DeepSeek V4.1 Flash", "deepseek-flash", "DeepSeek 官方 API", "selected", "已选 · 未实测", "策划、写稿与 Agent 决策", "工具调用、结构输出、多轮协作和 DeepAgents 兼容。"),
    ("人物图片", "GPT Image 2", "gpt-image-2", "用户指定 GPT 中转", "evidence", "已有生图证据", "新建人物；参考图编辑衣景", "已有 B/C 候选并选 B；图生图身份保持待验，上游型号未独立核验。"),
    ("TTS", "MiniMax Speech 2.8 HD", "speech-2.8-hd", "MiniMax 官方 API", "evidence", "已有样音证据", "固定音色、发音和情绪", "音色 3 已选；长稿、其他情绪及时间信息待验。"),
    ("本地可选", "M2 · LivePortrait → MuseTalk 1.5", "LivePortrait / MuseTalk 1.5", "算力云 GPU 自部署", "evidence", "已有短片证据", "动作底片 → 最终音频驱动嘴型", "人物 B 的 15.20 秒样片；正式动作母版、60 秒和日产五条待验。"),
    ("MVP 数字人", "OmniHuman 1.5", "jimeng_realman_avatar_picture_omni_v15", "火山视觉 API", "evidence", "历史短片证据", "人物图＋最终音频＋可选动作描述", "人物 B、身体动作、输入音轨保持和长片待验；用户确认 1 元/秒，MVP 必须验收。"),
    ("字幕与对齐", "FunASR / faster-whisper 等", "待同输入对比", "待选型", "pending", "候选待测试", "按最终音轨确定字幕边界", "不能把 MuseTalk 内部音频特征模型当作字幕模块。"),
    ("内部历史查重", "BGE-M3 / text-embedding-v4 等", "待同语料对比", "待选型", "pending", "候选待测试", "仅 quality 查询内部主题、观点与正文", "用人工金标分别测准确率、精确率和召回率。"),
    ("感知质量审核", "身份 / 声音 / 视听审核模型", "尚未定版", "待选型与标定", "pending", "候选待测试", "身份漂移、音色、口型和画面", "检测器未标定时转人工复核；纯文本模型不能替代看画面。"),
]
model_rows = "".join(f'<tr data-status="{status}"><td>{kind}</td><td><strong>{name}</strong><code>{ident}</code></td><td>{channel}</td><td><span class="badge {"accent" if status=="evidence" else ""}">{label}</span></td><td>{use}</td><td>{limit}</td></tr>' for kind,name,ident,channel,status,label,use,limit in models)

memories = [
    ("01", "任务状态", "检查点与业务任务表", "恢复工作图、等待位置和输入版本；外部任务仍须单独对账。"),
    ("02", "人设与声音", "不可变资产＋版本配置", "人物和音色只读引用；用户显式换人、换声后更新版本。"),
    ("03", "内容历史", "稿件与来源＋检索索引", "已审未发布稿也参与查重；主题、观点、案例可追溯。"),
    ("04", "长期经验", "持久 Store＋审核记录", "术语读法、偏好和纠错先提候选，审核后进入长期记忆。"),
]
memory_html = "".join(f'<article class="memory"><span class="number">{num}</span><div><h3>{name}</h3><strong>{mechanism}</strong><p>{meaning}</p></div></article>' for num,name,mechanism,meaning in memories)

sections = {}
sections['system'] = f'''
<div class="hero"><div><p class="eyebrow">01 / 总览与分层架构</p><h1>让同一位主播，<br><em>持续产出好内容。</em></h1><p class="intro">面向 AI 科普与教学的单用户数字人口播工作台。把选题、文案、声音、人物和后期串成可审核、可返工、可追溯的生产流程。</p></div><div class="hero-note"><span>首版目标</span><strong>制作闭环</strong><p>从需求到成片预览、下载<br>平台发布与运营能力后续开放</p></div></div>
<div class="stats"><div><b>07</b><strong>产品入口</strong><span>四项制作＋三项后续</span></div><div><b>04</b><strong>首版核心功能</strong><span>人物／文案／声音／合成</span></div><div><b>05</b><strong>Agent 角色</strong><span>一总控＋四类专家</span></div><div><b>07</b><strong>拟建业务 Skill</strong><span>规范方法，受控执行</span></div></div>
<div class="note"><strong>本轮 MVP：固定人物＋MiniMax 音频 → OmniHuman 1.5</strong><p>目标是让同一个主播按既定声音和口播内容产生自然身体表演。Grok 退出当前路线；M2 保留为低成本本地选项。专门的数字人接口也须验证身体动作、音轨保持与身份稳定。</p></div>
<div class="scope-line"><strong>输入</strong><span>“生成一期关于 RAG 的 AI 科普视频”</span><span class="flow-arrow" aria-hidden="true">→</span><strong>输出</strong><span>竖屏 MP4＋字幕＋封面＋投稿文案＋质量与来源清单</span></div>
<div class="section-head"><h2>整体架构：系统由哪些层组成</h2><p>工作台与 API、Agent 协作、业务执行、能力支撑、模型与 Worker、数据与媒体。各层分工如下；执行先后顺序见“生产与数据流”。</p></div>
{system_chart}
<p class="source-link">继续查看：<a href="#agents">Agent 协作框架</a> · <a href="#flow">作品生产数据流</a> · {doc_link('03-总体技术方案','总体技术方案')}</p>
<div class="section-head"><h2>七个入口，共用一条作品记录。</h2><p>Work 连接候选、选择、媒体版本、任务与费用；页面数量不等于 Agent 或服务数量。</p></div>
<div class="feature-grid">{features_html}</div>
<div class="note"><strong>当前状态</strong><p>方案与独立资产/短片实验已有证据。七入口应用、DeepAgents 编排、MCP 和记忆服务尚未完成开发；这是一份方案汇报页。</p></div>
'''
sections['flow'] = f'''
<div class="section-head"><p class="eyebrow">02 / 生产与数据流</p><h1>每一步都知道，<br><em>接收什么，交出什么。</em></h1><p>固定人物先准备，三篇稿件分别审核；用户选稿、选声并确认最终音频后，再进入视频合成。</p></div>
<div class="decision-strip"><span>人物确认</span><span>三稿选一</span><span>全文音频确认</span><span>路线与预算确认</span></div>
{flow_chart}
<div class="routes"><article><span class="badge">本地可选 · M2</span><h2>把动作和嘴型分开驱动</h2><p class="route-chain">人物图＋动作参考 → LivePortrait 底片<br>底片＋最终 TTS → MuseTalk 1.5 原片</p><p>底片按实际音频检查时长、覆盖与接缝。依赖动作参考，不能仅凭提示词生成任意肢体动作。</p></article><article><span class="badge accent">MVP 必交付 · OmniHuman 1.5</span><h2>图片与声音直接生成表演</h2><p class="route-chain">人物图＋最终 TTS＋表演描述<br>→ 云端口播原片 → 共同后期</p><p>保持已选人物与 MiniMax 最终音频，改善身体表演；无需 LivePortrait 或 MuseTalk。身体动作、人物 B 与长片仍待验收。</p></article></div>
<div class="note"><strong>对口型在第 4 步“视频合成”执行</strong><p>TTS 质检通过并经用户试听确认后才提交。人物页只准备身份、衣景和表演要求；Agent 可并行，单 GPU 的重型推理按资源租约排队。</p></div>
<div class="cost-evidence"><div><span>OmniHuman · 用户确认费率</span><strong>¥1 / 生成秒</strong><small>60 秒约 ¥60 · 5 条约 ¥300 / 天</small></div><p>30 天、每天 5 条一分钟视频，视频生成费约 ¥9,000。另计 TTS、图像、存储、后期和重做；分段取整与实际账单待核，不使用 Grok 的 ¥0.38 / 秒报价。</p></div>
<div class="section-head"><h2>数据交接与返工</h2><p>传递 ID、版本和摘要；媒体文件留在资产库，用户选择与质量审批分别保存。</p></div>
<div class="change-grid"><article><h3>改稿／换声</h3><p>音频、口型、字幕及成片重新生成或审核；旧结果和费用保留。</p></article><article><h3>换衣／换景</h3><p>重新检查身份和画面；未变化且仍获准的音频可复用。</p></article><article><h3>只改字幕样式</h3><p>复用原片与已对齐时间轴，只重渲染并重审成片。</p></article></div>
<details class="wide-details"><summary>展开 12 个数据交接点：输入、产物、存储和放行条件</summary>{table_html(data_table,'handoff-table')}</details>
'''
sections['agents'] = f'''
<div class="section-head"><p class="eyebrow">03 / Agent 协作框架</p><h1>一个总控，<br><em>四类专业分工。</em></h1><p>这张图展开整体架构的协作层。DeepAgents 是主框架，LangGraph 是底层运行机制；总控按依赖委派并汇合结果，专业角色共享规范、工具与记忆。</p></div>
{agent_chart}
<div class="principle"><strong>可并行的是独立工作，有依赖的任务必须等待</strong><p>人物准备、文案与声音方案可按条件并行；最终音频审核确认后才提交口播生成。字幕与口播可并行，原片通过且字幕就绪后由代码合成；quality 终审后内部打包。单 GPU 重型推理排队。</p></div>
<div class="role-grid">{roles_html}</div>
<div class="note"><strong>确定性后期 Worker：代码执行，不是子智能体</strong><p>最终音频确认后，字幕代码可与视频生成并行；原片审核通过且字幕就绪后，业务服务自动调度 FFmpeg 合成、清晰度处理和封面制作，再交 quality 终审，通过后才由内部代码打包。后期不设独立 Skill，也不通过 MCP 开放渲染或打包工具。</p></div>
<div class="principle"><strong>统一表演说明 PerformancePlan</strong><p>同一稿件的分段、情绪、语速、重音与动作意图共享给声音、数字人及后期代码。真实时间轴来自最终音频，不能按目标时长提前编造。</p></div>
<div class="section-head"><h2>7 个 Skill：怎样把任务做好</h2><p>以下是项目拟建规范；权限、预算和硬质量门禁仍由代码执行。</p></div><div class="skills">{skills_html}</div>
<div class="section-head"><h2>4 组 MCP 能力，建议先落两个服务</h2><p><code>studio-core</code> 管历史、角色记忆与质量登记；<code>studio-media</code> 管媒体生产与检测。M01 内部历史查重只授权 quality；quality 同时调用 M04 媒体检测与审核登记。联网抓取、后期渲染和打包不暴露为 MCP 工具。</p></div>
{table_html(mcp_table,'mcp-table')}
<div class="section-head"><h2>四层记忆，各有权威来源</h2><p>任务隔离写入，共享经验审核合并；检查点不能代替媒体、审批和计费账本。</p></div><div class="memory-grid">{memory_html}</div>
<div class="note"><strong>这部分仍是待实现能力</strong><p>先用无付费模拟工具验证委派、前置依赖、重启恢复与幂等，再按授权接入真实模型。文件存在或 Mock 跑通不代表真实生产验收通过。</p></div>
'''
sections['stack'] = f'''
<div class="section-head"><p class="eyebrow">04 / 技术与模型</p><h1>先明确模型，<br><em>再验证每个接入。</em></h1><p>已选型号、已有实验和生产验收分开记录。供应商通过 Provider Interface 接入，替换服务时保持业务契约稳定。</p></div>
<div class="table-tools"><label for="model-search">查找模型<input id="model-search" type="search" placeholder="模型、渠道或用途"></label><div class="filters" aria-label="模型状态筛选"><button data-filter="all" aria-pressed="true">全部</button><button data-filter="evidence" aria-pressed="false">已有实验</button><button data-filter="selected" aria-pressed="false">已选待测</button><button data-filter="pending" aria-pressed="false">待选型</button></div></div><p class="result-count" id="model-count" aria-live="polite">共 {len(models)} 类能力</p>
<div class="table-scroll" tabindex="0" aria-label="模型及验证状态"><table id="model-table"><thead><tr><th>能力</th><th>模型与请求 ID</th><th>调用渠道</th><th>证据状态</th><th>输出用途</th><th>尚需验证</th></tr></thead><tbody>{model_rows}</tbody></table></div><p id="model-empty" hidden>没有匹配结果，请调整关键词或筛选条件。</p>
<div class="section-head"><h2>本机管理作品，按路线调用云端能力。</h2></div><div class="deploy"><article><span class="number">本机控制面</span><h3>工作台、业务服务与资产管理</h3><dl><dt>前端</dt><dd>Vue 3 / React，待同流程选型</dd><dt>API</dt><dd>Python＋FastAPI＋Pydantic，建议首测</dd><dt>存储</dt><dd>PostgreSQL＋SQLAlchemy，按需 pgvector；待验证</dd><dt>作业</dt><dd>持久任务表＋独立 Worker，建议首测</dd></dl></article><article><span class="number">M2 可选 GPU 执行面</span><h3>LivePortrait 与 MuseTalk 独立环境</h3><dl><dt>调度</dt><dd>受控提交、查询、登记；重型任务互斥</dd><dt>媒体</dt><dd>FFmpeg／FFprobe／libass／Pillow</dd><dt>恢复</dt><dd>检查租约、输入摘要与原任务，不盲目重复提交</dd><dt>密钥</dt><dd>服务端保管，前端与 Agent 只持引用</dd></dl></article></div>
<div class="note"><strong>费用控制贯穿所有 Provider</strong><p>OmniHuman 按用户确认的 1 元/生成秒估价；提交前展示路线、模型、时长、分辨率与预算。生成成功但效果不满意，重做仍可能收费；两路线均不自动切换，超时未知先核对原任务。</p></div>
'''
sections['quality'] = f'''
<div class="section-head"><p class="eyebrow">05 / 质量评估</p><h1>汇报只看重点，<br><em>标准仍然可追溯。</em></h1><p>11 项覆盖内容、声音、人物、成片、交付率与成本。公式和标准来自同一份验收文档；所有值均为待执行基线，当前没有完整系统评测成绩。</p></div>
<div class="quality-legend"><span><b>★</b> 首版汇报重点</span><span><b>硬</b> 失败不得放行</span><span><b>建议</b> 测试前冻结</span><span><b>观察</b> 尚未设通过上限</span></div>
{table_html(core_table,'core-metrics')}
<div class="note"><strong>不做加权总分，不用平均分掩盖硬问题</strong><p>本轮身体动作是重点：E12 检查身体、肩臂、手势与构图，云原片另核输入音轨保持。未列入速览的格式、响度、权限和预算仍须执行。文案至少 3 人盲评，媒体至少 2 人逐项核验；无样本、缺测不能算通过。</p></div>
<div class="review-steps"><div><b>01</b><strong>固定输入</strong><p>同稿、同人物、同音频；对照试验一次只改一项。</p></div><div><b>02</b><strong>分阶段测量</strong><p>工具检测＋人工盲评，记录版本与原始分子分母。</p></div><div><b>03</b><strong>失败定位返工</strong><p>保留原片和费用，只重做受影响的环节。</p></div><div><b>04</b><strong>带证据签收</strong><p>区分 Mock、真实 Provider、生产与后续发布。</p></div></div>
<details class="wide-details" id="all-metrics"><summary>完整技术验收：31 项（28 项生产与运行＋3 项后续扩展）</summary><p>以下 ★ 是完整表的技术核心标记。E13、Agent/MCP/记忆指标用于工程验收，F01～F03 在后续阶段执行。</p>{table_html(full_table,'full-metrics')}</details>
<p class="source-link">{link('../阅读器/评估指标总表.html','打开带搜索和筛选的独立指标页')} · {doc_link('06-验收标准与测试方案','查看 69 个验收用例')}</p>
'''
sections['progress'] = f'''
<div class="section-head"><p class="eyebrow">06 / 进度与验收</p><h1>已经验证到哪里，<br><em>下一步就从哪里开始。</em></h1><p>已有工作台骨架、基础资产和独立短片实验。本轮同步四角色、7 个 Skill、4 组 MCP 与代码后期入口；离线回归通过不代表真实媒体链路已实现。</p></div>
<div class="evidence-grid"><article><span class="badge accent">资产证据</span><h3>人物 B＋音色 3</h3><p>固定人物已选，MiniMax 预设音色已试听。原图 941×1672；其他人物与音色可经独立审批成为新预设。</p>{doc_link('09-模型选型决策与接入方案','查看模型与资产记录')}</article><article><span class="badge accent">M2 短片证据</span><h3>15.20 秒真人口播</h3><p>LivePortrait 与 MuseTalk 两阶段共 235.177 秒；眼睑、轻微表情及头姿改善。正式动作母版与长片待验。</p>{link('../../experiments/表情动作改善/EXP-001/人物B表情动作对比.html','查看三条样片与处理方式')}</article><article><span class="badge">历史云端证据</span><h3>OmniHuman 1.5</h3><p>已有 9.48 秒历史原片及输入音轨保持证据。当前人物 B、身体动作、口型和约 60 秒衔接尚未验收；纳入本轮 MVP。</p>{link('../selection/01-数字人口播/05-OmniHuman开通后实测结果.html','查看历史实测结果')}</article></div>
<div class="cost-evidence"><div><span>历史 M2 作业估算</span><strong>¥0.1032</strong><small>按 1.58 元 / GPU 小时折算</small></div><p>仅对应上述 <b>15.20 秒</b>实验的两阶段 GPU 作业，不是实际账单，也不是一分钟视频的完整生产成本。安装、空闲、素材、其他 API、存储及返工须在生产批次重新统计。</p></div>
<div class="section-head"><h2>按可验证的切片推进</h2><p>以下按完整目标列出后续验收；已有代码、离线验证和媒体实验分别记录，不混报生产完成。</p></div>
<ol class="roadmap"><li><span>T00—T02</span><div><h3>契约与离线编排</h3><p>已有离线骨架；本轮四角色、工具边界及后期阻断回归通过，真实模型兼容待验。</p></div><em>部分验证</em></li><li><span>T03—T06</span><div><h3>人物、声音、口播与三稿</h3><p>登记已选资产；实现参考编辑、试听确认、逐稿审核；OmniHuman 身体表演与音轨保持必须在 MVP 验收，M2 保留本地选项。</p></div><em>待实施</em></li><li><span>T07—T11</span><div><h3>字幕后期与七入口闭环</h3><p>确定性后期 Worker、quality 终审、预览下载、真实进度及局部返工；完成空环境部署和备份恢复。</p></div><em>待实施</em></li><li><span>T12</span><div><h3>真实 RAG 与生产验收</h3><p>按获准预算测试约 60 秒成片、每天 5 条和 20 次真实任务，记录质量、产能与完整成本。</p></div><em>待实施</em></li><li><span>T13</span><div><h3>发布、运营与完整模型设置</h3><p>取得各平台适用权限后逐一接入；提醒、回填、指标读取和配置页分别验收。</p></div><em>后续</em></li></ol>
<div class="milestones"><div><b>G0</b><strong>文档基线</strong><p>方案、契约与验收对齐</p></div><div><b>G1</b><strong>真实核心能力</strong><p>身份、声音及口播单项验证</p></div><div><b>G2</b><strong>完整生产闭环</strong><p>合格成片可预览、下载</p></div><div><b>G3</b><strong>官方发布</strong><p>权限、回执和实际效果独立验收</p></div></div>
<details class="wide-details"><summary>汇报依据与文档入口</summary><ul><li>{doc_link('00-文档导航','开发文档导航')}</li><li>{doc_link('03-总体技术方案','总体技术方案')}</li><li>{doc_link('05-开发实施计划','T00～T13 实施计划')}</li><li>{doc_link('08-端到端技术流程与模型选型地图','数据流与返工依赖')}</li><li>{doc_link('09-模型选型决策与接入方案','模型、渠道与实验边界')}</li><li>{doc_link('10-Agent编排选型与整体架构','Agent、Skill、MCP 和记忆规划')}</li></ul><p>本页所有图形、表格和交互均内嵌，可离线展示；上述文档和样片链接需保留项目目录。</p></details>
'''

CSS = """
.chart-head{flex-wrap:wrap}.chart-actions{flex-shrink:0}.chart-actions button{white-space:nowrap}
:root{--bg:#f7f7f8;--paper:#fff;--ink:#171c27;--muted:#5b6575;--line:#dfe3e9;--accent:#002fa7;--soft:#eef2fc;--font:Arial,'Microsoft YaHei','PingFang SC',sans-serif}
*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:20px}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.75 var(--font)}button,input{font:inherit}button{cursor:pointer}a{color:var(--accent);text-underline-offset:4px}button,a,input,summary,[tabindex]{outline-offset:4px}:focus-visible{outline:3px solid #577cdb}[hidden]{display:none!important}p{margin:0 0 14px}h1,h2,h3{line-height:1.25;margin:0 0 15px}h1{font-size:clamp(34px,3.5vw,56px);letter-spacing:-.04em;font-weight:700}h1 em{font-style:normal;color:var(--accent)}h2{font-size:26px;letter-spacing:-.02em}h3{font-size:18px}code{font-size:12px;background:#eef0f4;padding:2px 5px;overflow-wrap:anywhere}small,.muted{color:var(--muted)}button{color:var(--ink);border:1px solid #cbd2df;background:#fff;padding:8px 14px;font-size:13px}button:hover{border-color:var(--accent);color:var(--accent)}button[aria-pressed=true]{background:var(--accent);color:#fff;border-color:var(--accent)}
.skip{position:fixed;top:-80px;left:16px;background:white;padding:12px;z-index:30}.skip:focus{top:10px}.sidebar{position:fixed;inset:0 auto 0 0;width:236px;background:#fff;border-right:1px solid var(--line);padding:32px 22px;display:flex;flex-direction:column;z-index:10}.brand{margin:0 6px 42px;border-top:5px solid var(--accent);padding-top:15px}.brand strong{display:block;font-size:20px;line-height:1.35}.brand span{font-size:11px;color:var(--muted);letter-spacing:.06em}.nav-caption{margin:0 8px 12px;color:var(--muted);font-size:11px}.nav-button{display:flex;width:100%;gap:14px;text-align:left;align-items:center;padding:13px 10px;margin:3px 0;border:0;border-left:3px solid transparent;background:transparent;font-size:14px}.nav-button[aria-selected=true]{border-left-color:var(--accent);background:var(--soft);color:var(--accent);font-weight:700}.nav-num{font-size:12px;color:#6e7890;min-width:20px}.side-bottom{margin-top:auto;padding-top:24px;font-size:12px;color:var(--muted)}.side-bottom strong{color:var(--ink)}.side-bottom p{padding-top:8px}.side-bottom a{font-size:12px}.main{margin-left:236px;padding:0 44px 35px;max-width:1870px}.topbar{min-height:82px;display:flex;justify-content:space-between;align-items:center;gap:18px;border-bottom:1px solid var(--line);font-size:12px;color:var(--muted)}.topbar strong{color:var(--ink)}.top-actions{display:flex;gap:8px;align-items:center}.top-actions button{padding:7px 10px;font-size:12px}.status-line{margin-right:6px;color:var(--muted)}.view{padding-top:37px}.eyebrow{font-size:12px;letter-spacing:.08em;color:var(--accent);margin-bottom:16px;font-weight:700}.hero{display:grid;grid-template-columns:1fr 240px;align-items:end;gap:32px}.intro{font-size:16px;color:var(--muted);max-width:750px;margin-bottom:0}.hero-note{border-left:2px solid var(--accent);padding:8px 0 4px 22px}.hero-note span{font-size:12px;color:var(--muted)}.hero-note strong{display:block;font-size:24px;margin:6px 0}.hero-note p{font-size:12px;color:var(--muted);margin:0}.stats{display:grid;grid-template-columns:repeat(4,1fr);border-top:1px solid var(--line);border-bottom:1px solid var(--line);margin:32px 0 24px;background:#fff}.stats>div{padding:20px 22px;border-right:1px solid var(--line)}.stats>div:last-child{border:0}.stats b{float:left;font-size:44px;line-height:1.1;color:var(--accent);letter-spacing:-.06em;margin-right:18px;font-variant-numeric:tabular-nums}.stats strong{display:block;font-size:14px;margin-top:2px}.stats span{display:block;font-size:11px;color:var(--muted)}.scope-line{display:flex;gap:15px;align-items:center;flex-wrap:wrap;margin:22px 0;font-size:13px}.scope-line strong{color:var(--accent)}.flow-arrow{color:#8b95a7;font-size:24px}.section-head{margin:0 0 22px}.section-head:not(:first-child){margin-top:40px}.section-head p{color:var(--muted);max-width:1000px}.section-head .eyebrow{color:var(--accent)}.feature-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:1px;background:var(--line);border:1px solid var(--line)}.feature{padding:22px;background:#fff;display:flex;flex-direction:column}.feature p{font-size:13px;color:var(--muted)}.feature-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px}.number{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}.badge{display:inline-block;white-space:nowrap;font-size:11px;padding:3px 8px;border:1px solid #d6dce6;color:#5d677a;background:#fff}.badge.accent{color:var(--accent);background:var(--soft);border-color:#c7d3f1}.feature-foot{border-top:1px solid var(--line);padding-top:12px;margin-top:auto;font-size:12px;color:#596980}.note,.principle{border-left:3px solid var(--accent);background:var(--soft);padding:18px 22px;margin:24px 0}.note strong,.principle strong{font-size:15px;color:var(--accent)}.note p,.principle p{font-size:13px;margin:6px 0 0;color:#465573}.chart-card{border:1px solid var(--line);background:#fff;margin:24px 0}.chart-head{display:flex;justify-content:space-between;align-items:center;padding:15px 20px;border-bottom:1px solid var(--line);gap:12px;font-size:15px}.chart-actions{display:flex;gap:8px}.chart-actions button{font-size:12px;padding:5px 11px}.diagram-scroll{overflow:auto;padding:12px;scrollbar-width:thin}.diagram{width:100%;min-width:930px;height:auto;display:block}.diagram-scroll.enlarged .diagram{width:145%;min-width:1300px}.chart-caption{padding:13px 20px;border-top:1px solid var(--line);font-size:12px;color:var(--muted)}.node-detail{border-top:1px solid var(--line);padding:14px 20px;background:#fafbff;font-size:13px}.node-detail strong{display:block;color:var(--accent)}.detail-label{float:right;font-size:11px;color:var(--muted)}.node-detail p{margin:4px 0 0;color:var(--muted)}.decision-strip{display:flex;border:1px solid var(--line);background:#fff;margin:24px 0}.decision-strip span{flex:1;padding:14px 16px;font-size:14px;border-right:1px solid var(--line);color:var(--accent);text-align:center}.decision-strip span:last-child{border:0}.routes,.deploy{display:grid;grid-template-columns:1fr 1fr;gap:22px;margin:28px 0}.routes article,.deploy article{border:1px solid var(--line);background:#fff;padding:26px}.routes h2{font-size:22px;margin-top:18px}.routes p{font-size:13px;color:var(--muted)}.routes .route-chain{font-size:16px;color:var(--ink);line-height:2;padding:12px 0}.routes p:last-child{margin:0}.change-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:24px;border-top:1px solid var(--line);padding-top:22px}.change-grid h3{font-size:17px}.change-grid p{font-size:13px;color:var(--muted)}.wide-details{border:1px solid var(--line);padding:18px 20px;margin:25px 0;background:#fff}.wide-details>summary{font-size:15px;font-weight:700;cursor:pointer;color:var(--accent)}.wide-details[open]>summary{margin-bottom:18px}.wide-details p,.wide-details li{font-size:13px;color:var(--muted)}.wide-details .table-scroll{margin-bottom:0}.role-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));border:1px solid var(--line);background:var(--line);gap:1px;margin:28px 0}.role{padding:24px;background:#fff}.role-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:16px}.role-head h3{margin:0}.role p{font-size:14px;margin-bottom:10px}.role small{font-size:12px}.skills{display:grid;grid-template-columns:1fr 1fr;gap:12px}.skill{background:#fff;border:1px solid var(--line);padding:0 18px}.skill summary{cursor:pointer;padding:15px 0;font-weight:600;font-size:14px}.skill p{font-size:13px}.skill p b{font-size:12px;color:var(--accent);display:block}.memory-grid{display:grid;grid-template-columns:1fr 1fr;gap:26px}.memory{display:flex;gap:18px;border-top:1px solid var(--line);padding-top:20px}.memory .number{font-size:26px;color:var(--accent)}.memory h3{margin-bottom:8px}.memory strong{font-size:13px;font-weight:500}.memory p{font-size:13px;color:var(--muted);margin-top:8px}.table-scroll{overflow:auto;border:1px solid var(--line);background:#fff;margin:18px 0;scrollbar-width:thin}table{width:100%;min-width:950px;border-collapse:collapse;text-align:left;font-size:13px;line-height:1.7}th{background:#eff2f7;font-weight:600;font-size:12px;color:#536079;border-bottom:1px solid var(--line);padding:14px 16px;white-space:nowrap}td{padding:15px 16px;border-bottom:1px solid var(--line);vertical-align:top;color:#445169}tbody tr:last-child td{border-bottom:0}td:first-child{color:var(--ink);font-weight:600}td strong{color:var(--ink)}td code{display:block;margin-top:6px;font-size:11px;background:transparent;padding:0;color:#687487}#model-table{min-width:1120px}#model-table th:nth-child(2){width:25%}#model-table th:last-child{width:27%}.table-tools{display:flex;justify-content:space-between;align-items:end;gap:20px;margin-top:28px}.table-tools label{display:flex;flex-direction:column;gap:6px;font-size:12px;color:var(--muted)}input[type=search]{border:1px solid #cbd2df;padding:10px 14px;width:310px;background:#fff;font-size:14px}.filters{display:flex;gap:7px;flex-wrap:wrap}.filters button{font-size:12px}.result-count{font-size:12px;color:var(--muted);margin:9px 0}dl{margin:18px 0 0}dt{float:left;width:58px;font-size:12px;color:var(--muted)}dd{margin:0 0 10px 64px;font-size:13px}.deploy .number{display:block;margin-bottom:12px;color:var(--accent)}.quality-legend{display:flex;flex-wrap:wrap;gap:24px;font-size:13px;margin:24px 0}.quality-legend b{color:var(--accent);margin-right:6px}#core-metrics{table-layout:fixed;min-width:1000px}#core-metrics th:nth-child(1){width:9%}#core-metrics th:nth-child(2){width:19%}#core-metrics th:nth-child(3){width:17%}#core-metrics th:nth-child(4){width:25%}#core-metrics th:nth-child(5){width:30%}#core-metrics td:nth-child(2) strong{color:var(--accent)}.review-steps{display:grid;grid-template-columns:repeat(4,1fr);gap:24px;margin:30px 0}.review-steps b{display:block;font-size:28px;color:#9aa7bf}.review-steps strong{display:block;font-size:16px;margin:8px 0}.review-steps p{font-size:13px;color:var(--muted)}.source-link{font-size:13px}.evidence-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px;margin:30px 0}.evidence-grid article{background:#fff;border:1px solid var(--line);padding:25px}.evidence-grid h3{margin-top:18px}.evidence-grid p{font-size:13px;color:var(--muted)}.evidence-grid a{font-size:13px}.cost-evidence{background:#172134;color:#fff;padding:24px 28px;display:grid;grid-template-columns:260px 1fr;gap:25px}.cost-evidence span,.cost-evidence small{font-size:12px;color:#c8d0e0;display:block}.cost-evidence strong{font-size:38px;font-weight:600;display:block;letter-spacing:-.02em}.cost-evidence p{font-size:14px;margin:0;align-self:center;line-height:1.9;color:#e6eaf3}.roadmap{list-style:none;margin:24px 0;padding:0;border-top:1px solid var(--line)}.roadmap li{display:grid;grid-template-columns:120px 1fr 70px;gap:24px;padding:25px 0;border-bottom:1px solid var(--line)}.roadmap li>span{font-size:13px;font-variant-numeric:tabular-nums;color:var(--accent)}.roadmap h3{font-size:19px;margin-bottom:10px}.roadmap p{font-size:13px;color:var(--muted);margin:0}.roadmap em{font-size:12px;font-style:normal;color:var(--muted)}.milestones{display:grid;grid-template-columns:repeat(4,1fr);border:1px solid var(--line);background:#fff;margin:25px 0}.milestones>div{padding:18px;border-right:1px solid var(--line)}.milestones>div:last-child{border:0}.milestones b{font-size:23px;color:var(--accent);display:block}.milestones strong{font-size:14px}.milestones p{font-size:12px;margin:5px 0 0;color:var(--muted)}.footer{border-top:1px solid var(--line);margin-top:40px;padding-top:18px;display:flex;justify-content:space-between;gap:20px;font-size:11px;color:var(--muted)}.toast{position:fixed;right:25px;bottom:25px;padding:12px 18px;background:#172134;color:white;font-size:13px;z-index:25}.presenting .sidebar{display:none}.presenting .main{margin:0 auto;max-width:1680px}.presenting .topbar{position:sticky;top:0;z-index:11;background:var(--bg)}.presenting .present-nav{display:flex}.present-nav{display:none;gap:7px}.present-nav button{font-size:12px}
@media(min-width:1800px){.main{padding-left:60px;padding-right:60px}}
@media(max-width:1250px){.main{padding-left:28px;padding-right:28px}.hero{grid-template-columns:1fr}.hero-note{display:none}.stats>div{padding:17px 12px}.stats b{font-size:34px;margin-right:12px}.stats span{font-size:10px}.feature-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.status-line{display:none}.role-head{display:block}.role-head code{display:inline-block;margin-top:8px}.evidence-grid article{padding:18px}.table-tools{align-items:start;flex-direction:column}}
@media(max-width:760px){.sidebar{position:sticky;inset:auto;top:0;width:100%;padding:8px;border-right:0;border-bottom:1px solid var(--line)}.brand,.nav-caption,.side-bottom{display:none}.sidebar nav{display:flex;overflow:auto;gap:3px}.nav-button{width:auto;flex-shrink:0;padding:10px 12px;gap:7px;font-size:12px;white-space:nowrap;margin:0;border:0;border-bottom:2px solid transparent}.nav-button[aria-selected=true]{border-bottom-color:var(--accent)}.main{margin:0;padding:0 18px 28px}.topbar{min-height:65px}.topbar .crumb-prefix{display:none}.top-actions{gap:5px}.top-actions button{font-size:10px;padding:6px 8px}.view{padding-top:27px}h1{font-size:36px}.intro{font-size:14px}.stats{grid-template-columns:1fr 1fr;margin-top:24px}.stats>div{padding:17px 14px;border-bottom:1px solid var(--line)}.stats>div:nth-child(2){border-right:0}.stats>div:nth-child(3){border-bottom:0}.stats b{font-size:38px}.stats span{font-size:11px}.scope-line{gap:9px}.scope-line .flow-arrow{display:none}.feature-grid,.role-grid,.skills,.memory-grid,.routes,.deploy,.evidence-grid{grid-template-columns:1fr}.section-head h2{font-size:23px}.section-head p{font-size:13px}.chart-head{padding:12px;font-size:13px}.chart-actions button{font-size:11px;padding:5px 8px}.diagram-scroll{padding:6px}.chart-caption,.node-detail{padding:12px}.decision-strip{display:grid;grid-template-columns:1fr 1fr}.decision-strip span{font-size:12px;border-bottom:1px solid var(--line)}.change-grid,.review-steps,.milestones{grid-template-columns:1fr 1fr;gap:18px}.milestones{gap:0}.milestones>div{border-bottom:1px solid var(--line)}.quality-legend{gap:12px;font-size:12px}.cost-evidence{grid-template-columns:1fr;padding:22px;gap:14px}.roadmap li{grid-template-columns:80px 1fr;gap:12px}.roadmap em{grid-column:2}.wide-details{padding:14px}.wide-details>summary{font-size:13px}.footer{flex-direction:column;gap:8px}.present-nav button{font-size:10px;padding:6px}.table-tools,input[type=search]{width:100%}.filters{gap:5px}.filters button{padding:7px 10px}.routes article,.deploy article{padding:20px}.note,.principle{padding:16px}.role-head{display:flex}.role-head code{margin:0}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
@media print{@page{size:A3 landscape;margin:13mm}body{background:white;font-size:11px}.sidebar,.top-actions,.present-nav,.chart-actions,.table-tools,.toast,.skip,.node-detail{display:none!important}.main,.presenting .main{margin:0;padding:0;max-width:none}.topbar{min-height:35px}.view{padding:20px 0;break-before:page}.view:first-of-type{break-before:auto}body.print-all .view[hidden]{display:block!important}.hero{display:block}.hero-note{display:none}h1{font-size:32px}h2{font-size:23px}.chart-card,.feature,.role,.routes article,.deploy article,.evidence-grid article,.cost-evidence,.roadmap li{break-inside:avoid}.diagram-scroll,.table-scroll{overflow:visible}.diagram{min-width:0!important;width:100%!important}.chart-caption{font-size:10px}table,#core-metrics,#model-table{min-width:0;font-size:10px}th,td{padding:8px;font-size:10px}thead{display:table-header-group}tr{break-inside:avoid}#model-table tr[hidden]{display:table-row!important}.stats b{font-size:30px}.feature-grid{grid-template-columns:repeat(4,1fr)}.role-grid{grid-template-columns:repeat(3,1fr)}.evidence-grid{grid-template-columns:repeat(3,1fr)}.footer{font-size:9px}.source-link{display:none}.wide-details,.skill{break-inside:auto}.cost-evidence{background:#f0f2f7;color:#171c27}.cost-evidence p,.cost-evidence span,.cost-evidence small{color:#445169}a{color:#171c27}}
"""

SCRIPT = """
(() => {
 'use strict';
 const nav=Array.from(document.querySelectorAll('[data-view]'));
 const names={system:'总览与分层架构',flow:'生产与数据流',agents:'Agent 协作框架',stack:'技术与模型',quality:'质量评估',progress:'进度与验收'};
 let active='system';
 function activate(id,hash=true,focus=false){
  if(!Object.hasOwn(names,id))id='system';active=id;
  nav.forEach(b=>{const on=b.dataset.view===id;b.setAttribute('aria-selected',String(on));b.tabIndex=on?0:-1;});
  document.querySelectorAll('.view').forEach(p=>p.hidden=p.id!=='view-'+id);
  document.querySelector('#breadcrumb').textContent=names[id];
  document.title=names[id]+' · AI 数字人工作台项目汇报';
  if(hash&&location.hash!=='#'+id)location.hash=id;
  if(focus)document.querySelector('#tab-'+id).focus();
  window.scrollTo({top:0,behavior:'instant'});
 }
 nav.forEach((b,i)=>{b.addEventListener('click',()=>activate(b.dataset.view));b.addEventListener('keydown',e=>{
  let j=i;if(['ArrowDown','ArrowRight'].includes(e.key))j=(i+1)%nav.length;else if(['ArrowUp','ArrowLeft'].includes(e.key))j=(i+nav.length-1)%nav.length;else if(e.key==='Home')j=0;else if(e.key==='End')j=nav.length-1;else return;
  e.preventDefault();activate(nav[j].dataset.view,true,true);
 });});
 window.addEventListener('hashchange',()=>activate(location.hash.slice(1),false));activate(location.hash.slice(1),false);
 function presentation(enabled){document.body.classList.toggle('presenting',enabled);const b=document.querySelector('#present');b.setAttribute('aria-pressed',String(enabled));b.textContent=enabled?'退出汇报模式':'汇报模式';}
 document.querySelector('#present').addEventListener('click',()=>presentation(!document.body.classList.contains('presenting')));
 document.addEventListener('keydown',e=>{if(e.key==='Escape')presentation(false);});
 document.querySelector('#previous').addEventListener('click',()=>{const ids=Object.keys(names),i=ids.indexOf(active);activate(ids[(i+ids.length-1)%ids.length]);});
 document.querySelector('#next').addEventListener('click',()=>{const ids=Object.keys(names),i=ids.indexOf(active);activate(ids[(i+1)%ids.length]);});
 document.querySelectorAll('[data-node-id]').forEach(node=>{
  function show(){const key=node.dataset.chart;document.querySelectorAll('[data-chart="'+key+'"]').forEach(n=>n.setAttribute('aria-pressed',String(n===node)));const target=document.querySelector('#detail-'+key);target.replaceChildren();
   const title=document.createElement('strong');title.textContent=node.dataset.nodeLabel;target.append(title);
   for(const text of [node.dataset.nodeSublabel,node.dataset.nodeTag,node.dataset.nodeContext])if(text){const p=document.createElement('p');p.textContent=text;target.append(p);}
   target.scrollIntoView({block:'nearest',behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});
  }node.addEventListener('click',show);node.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();show();}});
 });
 document.querySelectorAll('[data-zoom]').forEach(b=>b.addEventListener('click',()=>{const enabled=b.getAttribute('aria-pressed')!=='true';b.setAttribute('aria-pressed',String(enabled));b.textContent=enabled?'适应宽度':'放大';document.querySelector('#scroll-'+b.dataset.zoom).classList.toggle('enlarged',enabled);}));
 let toastTimer;
 function toast(text){const el=document.querySelector('#toast');el.textContent=text;el.hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.hidden=true,2800);}
 document.querySelectorAll('[data-export]').forEach(b=>b.addEventListener('click',()=>{const svg=document.querySelector('#svg-'+b.dataset.export).cloneNode(true);svg.removeAttribute('class');const box=svg.viewBox.baseVal;svg.setAttribute('width',box.width);svg.setAttribute('height',box.height);svg.querySelectorAll('[tabindex]').forEach(e=>{e.removeAttribute('tabindex');e.removeAttribute('role');e.removeAttribute('aria-pressed');});const blob=new Blob(['<?xml version="1.0" encoding="UTF-8"?>\\n'+new XMLSerializer().serializeToString(svg)],{type:'image/svg+xml;charset=utf-8'});const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=({system:'整体分层架构',agents:'Agent协作框架',flow:'作品生产数据流'}[b.dataset.export])+'.svg';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),3000);toast('已请求下载 SVG');}));
 const opened=[];
 function restorePrint(){document.body.classList.remove('print-all');opened.splice(0).forEach(d=>d.open=false);}
 document.querySelector('#print-current').addEventListener('click',()=>{restorePrint();window.print();});
 document.querySelector('#print-all').addEventListener('click',()=>{restorePrint();document.body.classList.add('print-all');document.querySelectorAll('.skill:not([open])').forEach(d=>{opened.push(d);d.open=true;});window.print();});
 window.addEventListener('afterprint',restorePrint);
 let filter='all';const rows=Array.from(document.querySelectorAll('#model-table tbody tr'));
 function filterModels(){const q=document.querySelector('#model-search').value.trim().toLowerCase();let n=0;rows.forEach(r=>{r.hidden=!(filter==='all'||r.dataset.status===filter)||!r.textContent.toLowerCase().includes(q);if(!r.hidden)n++;});document.querySelector('#model-count').textContent='显示 '+n+' / '+rows.length+' 类能力';document.querySelector('#model-empty').hidden=n!==0;}
 document.querySelector('#model-search').addEventListener('input',filterModels);
 document.querySelectorAll('[data-filter]').forEach(b=>b.addEventListener('click',()=>{filter=b.dataset.filter;document.querySelectorAll('[data-filter]').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));filterModels();}));filterModels();
})();
"""

labels = {"system":"总览与分层架构","flow":"生产与数据流","agents":"Agent 协作框架","stack":"技术与模型","quality":"质量评估","progress":"进度与验收"}
nav = "".join(f'<button id="tab-{key}" class="nav-button" data-view="{key}" role="tab" aria-controls="view-{key}" aria-selected="{str(i==1).lower()}" tabindex="{0 if i==1 else -1}"><span class="nav-num">{i:02}</span>{label}</button>' for i,(key,label) in enumerate(labels.items(),1))
views = "".join(f'<section class="view" id="view-{key}" role="tabpanel" aria-labelledby="tab-{key}" tabindex="0" {"hidden" if key!="system" else ""}>{body}</section>' for key,body in sections.items())
provenance = {"purpose":"项目方案汇报，非运行中的生产应用","style_reference":str(REFERENCE),"style_reference_sha256":sha256(REFERENCE.read_bytes()).hexdigest(),"visual_direction":"原创白底、深色文字、蓝色强调；参考文件仅用于信息组织与交互参考","generated_at":datetime.now().isoformat(timespec="seconds"),"sources":[{"path":str(p.relative_to(SPECS)),"sha256":sha256(p.read_bytes()).hexdigest()} for p in SOURCES],"metric_ids":core_ids,"full_metric_count":31,"views":list(labels)}
page = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="description" content="AI 数字人工作台项目汇报：七入口、DeepAgents、数据流、模型、核心评估与进度。"><link rel="icon" href="data:,"><title>AI 数字人工作台 · 项目方案汇报</title><style>{CSS}</style></head><body>
<a class="skip" href="#content">跳转到汇报内容</a><aside class="sidebar"><div class="brand"><strong>AI 数字人<br>内容工作台</strong><span>项目方案汇报 · 2026.09</span></div><div class="nav-caption">六个视图，讲清一条生产线</div><nav role="tablist" aria-label="汇报章节" aria-orientation="vertical">{nav}</nav><div class="side-bottom"><strong>每天 5 条 × 约 60 秒</strong><p>生产验收目标，尚未达标验证。<br>首版做到预览与下载。</p>{doc_link('00-文档导航','打开开发文档')}</div></aside>
<main class="main" id="content"><header class="topbar"><div><span class="crumb-prefix">项目方案 / </span><strong id="breadcrumb">总览与分层架构</strong></div><div class="present-nav"><button id="previous">上一节</button><button id="next">下一节</button></div><div class="top-actions"><span class="status-line">方案与实验阶段</span><button id="present" aria-pressed="false">汇报模式</button><button id="print-current">打印本页</button><button id="print-all">全部 / PDF</button></div></header>{views}<footer class="footer"><span>2026-09-23 · v2.4 · 按当前权威文档整理，模型已选与实测通过分别记录。</span><span>离线 HTML · 无外部脚本与字体</span></footer></main><div class="toast" id="toast" role="status" hidden></div><script>{SCRIPT}</script></body></html>'''
output = HERE / "项目方案汇报.html"
output.write_text(page, encoding="utf-8")
provenance["output_sha256"] = sha256(output.read_bytes()).hexdigest()
(HERE / "项目汇报来源.json").write_text(json.dumps(provenance,ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps({"output":str(output),"views":len(labels),"core_metrics":len(core_ids),"full_metrics":31,"bytes":output.stat().st_size},ensure_ascii=False))
