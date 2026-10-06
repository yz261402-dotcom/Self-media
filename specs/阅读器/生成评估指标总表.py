"""从06验收文档导出首版速览与完整表，支持切换/筛选/打印，不生成测试成绩。"""
from pathlib import Path
from hashlib import sha256
from html import escape
from urllib.parse import quote
from bs4 import BeautifulSoup
from markdown_it import MarkdownIt
import re

folder = Path(__file__).resolve().parent
source = folder.parent / "0001-douyin-ai-studio" / "06-验收标准与测试方案.md"
raw = source.read_text(encoding="utf-8")
body = BeautifulSoup(MarkdownIt("commonmark").enable("table").render(raw), "html.parser")
table = next(t for t in body.find_all("table") if "指标（★核心）" in t.get_text())
rows = table.select("tbody tr")
ac_count = sum(1 for h in body.find_all("h4") if h.get_text(strip=True).startswith("AC-"))
core = 0
production = 0
future = 0
stages = []
metric_ids = []
last_stage = None
core_heading = next(h for h in body.find_all("h4") if h.get_text(strip=True).startswith("首版核心指标速览"))
report_core_table = core_heading.find_next("table")
report_core_ids = []
for row in report_core_table.select("tbody tr"):
    cells = row.find_all("td", recursive=False)
    if len(cells) != 5:
        raise ValueError("核心速览必须包含环节、指标、含义、计算方法和标准基线")
    report_core_ids.append(re.search(r"\bE\d{2}\b", cells[1].get_text()).group())
if len(report_core_ids) != 11 or len(set(report_core_ids)) != 11:
    raise ValueError("首版速览应包含11个独立指标")
report_core_table["id"] = "report-core-metrics"
report_core_table["aria-label"] = "首版11项核心指标：含义、计算方法与标准基线"
for header in report_core_table.select("thead th"):
    header["scope"] = "col"
for row in rows:
    cells = row.find_all("td", recursive=False)
    if len(cells) != 5:
        raise ValueError("指标表必须包含环节、指标、含义、方法、基线五列")
    stage = cells[0].get_text(strip=True)
    metric = cells[1].get_text(" ", strip=True)
    match = re.search(r"\b([EF]\d{2})\b", metric)
    if not match:
        raise ValueError(f"指标缺少唯一编号：{metric}")
    metric_id = match.group(1)
    metric_ids.append(metric_id)
    is_future = metric_id.startswith("F")
    future += int(is_future)
    production += int(not is_future)
    if stage not in stages:
        stages.append(stage)
    row["id"] = metric_id
    row["data-stage"] = stage
    row["data-scope"] = "future" if is_future else "production"
    row["data-core"] = "true" if "★" in metric else "false"
    row["data-search"] = row.get_text(" ", strip=True).lower()
    classes = []
    if "★" in metric:
        classes.append("core")
        core += 1
    if is_future:
        classes.append("future")
    if stage != last_stage:
        classes.append("stage-start")
    row["class"] = classes
    last_stage = stage
if len(set(metric_ids)) != len(metric_ids):
    raise ValueError("指标编号重复")
if [m for m in metric_ids if m.startswith("E")] != [f"E{i:02}" for i in range(1, 29)]:
    raise ValueError("必须保留 E01～E28 及其顺序")
if ac_count != 69:
    raise ValueError(f"验收用例数量发生变化：{ac_count}，预期69")
if any(metric_id not in metric_ids for metric_id in report_core_ids):
    raise ValueError("首版核心指标引用了不存在的指标")
for header in table.select("thead th"):
    header["scope"] = "col"
table["aria-label"] = "各生产环节的评估指标、含义、计算方法和标准基线"
table["id"] = "metrics"


def section_nodes(prefix):
    heading = next(h for h in body.find_all("h3") if h.get_text(strip=True).startswith(prefix))
    nodes = []
    for sibling in heading.next_siblings:
        if getattr(sibling, "name", None) in {"h2", "h3"}:
            break
        if getattr(sibling, "name", None):
            nodes.append(sibling)
    return nodes


method_nodes = section_nodes("4.1 ")
workflow = str(method_nodes[0])
methods = "\n".join(str(node) for node in method_nodes[1:])
rules = "\n".join(str(node) for node in section_nodes("4.3 "))
stage_options = "".join(f'<option value="{escape(stage, quote=True)}">{escape(stage)}</option>' for stage in stages)
digest = sha256(source.read_bytes()).hexdigest()
main_href = quote("../0001-douyin-ai-studio/06-验收标准与测试方案.html", safe="/.")
flow_href = quote("../0001-douyin-ai-studio/08-端到端技术流程与模型选型地图.html", safe="/.")
report_href = quote("../汇报/项目方案汇报.html", safe="/.")
css = """
:root{font-family:system-ui,'Microsoft YaHei',sans-serif;color:#202126;background:#f7f8fa}
*{box-sizing:border-box}body{margin:0;padding:28px 32px}main{max-width:1640px;margin:auto}
header{margin-bottom:20px}h1{font-size:27px;line-height:1.4;margin:8px 0 12px}
h2{font-size:20px;line-height:1.5;margin:16px 0 8px}.view-nav{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0}.view-nav a{padding:9px 14px;border:1px solid #bac3d2;border-radius:5px;background:white;text-decoration:none;font-size:14px}.view-nav a[aria-current=page]{background:#002fa7;border-color:#002fa7;color:white}.core-actions{display:flex;gap:16px;align-items:center;margin:12px 0}
p{font-size:14px;line-height:1.8;margin:6px 0}a{color:#002fa7}small{color:#666}
.eyebrow{font-size:12px;color:#002fa7;letter-spacing:.08em}.note{color:#555;max-width:1200px}
.workflow{padding:14px 18px;background:#eef3ff;border-left:3px solid #002fa7;margin:18px 0 10px}
details{background:#fff;border:1px solid #d9dde5;margin:10px 0;padding:12px 16px;font-size:14px;line-height:1.85}
summary{cursor:pointer;font-weight:650;color:#263654}details li{margin:8px 0}details ol{padding-left:24px}
.controls{display:flex;align-items:end;gap:12px;flex-wrap:wrap;margin:20px 0 10px}
.control{display:flex;flex-direction:column;gap:5px;font-size:12px;font-weight:600;color:#555}
input[type=search],select,button{font:inherit;font-size:14px;min-height:40px;border:1px solid #bac3d2;border-radius:5px;background:white;padding:8px 10px;color:#202126}
input[type=search]{width:260px;max-width:100%}select{max-width:260px}button{cursor:pointer}button:hover{background:#eef3ff}
.core-toggle{font-size:14px;display:flex;gap:7px;align-items:center;min-height:40px;color:#002fa7}
input[type=checkbox]{width:17px;height:17px;accent-color:#002fa7}.count{font-size:13px;color:#555;margin:10px 0}
:focus-visible{outline:3px solid #356ad8;outline-offset:3px}[hidden]{display:none!important}
.table-wrap{overflow-x:auto;background:white;border:1px solid #d9dde5}
table{width:100%;min-width:1060px;border-collapse:separate;border-spacing:0;font-size:14px;line-height:1.65;table-layout:fixed}
thead th{background:#eef1f5;text-align:left;font-size:13px;position:sticky;top:0;z-index:1}
th,td{padding:13px 15px;border-right:1px solid #e0e4eb;border-bottom:1px solid #e0e4eb;vertical-align:top;overflow-wrap:anywhere}
th:nth-child(1){width:8%}th:nth-child(2){width:17%}th:nth-child(3){width:19%}th:nth-child(4){width:31%}th:nth-child(5){width:25%}
th:last-child,td:last-child{border-right:0}tbody tr:last-child td{border-bottom:0}
tr.core td:nth-child(2){color:#002fa7;background:#eef3ff}tr.stage-start td{border-top:1px solid #c7ceda}
tr.future td:first-child{color:#865518;background:#fcf8ef}.empty{padding:20px;background:white;border:1px solid #d9dde5}
#report-core-metrics{min-width:1060px;margin:0;font-size:14px}#report-core-metrics th:nth-child(1){width:9%}#report-core-metrics th:nth-child(2){width:19%}#report-core-metrics th:nth-child(3){width:17%}#report-core-metrics th:nth-child(4){width:27%}#report-core-metrics th:nth-child(5){width:28%}#report-core-metrics thead th{position:static;background:#e8eefb}#report-core-metrics tbody td:nth-child(2){font-weight:650;color:#002fa7}
td:first-child{font-weight:600;color:#555;font-size:13px}code{font-size:.95em}
footer{padding:18px 0;font-size:12px;line-height:1.8;color:#666}
@media(max-width:700px){body{padding:18px 14px}h1{font-size:23px}.table-wrap{border-radius:0}.controls{gap:10px}.search-control{width:100%}input[type=search]{width:100%}.workflow{padding:10px 12px}details{padding:10px 12px}}
@media print{@page{size:A3 landscape;margin:10mm}body{padding:0;background:white}main{max-width:none}table,#report-core-metrics{min-width:0;font-size:10px;line-height:1.5}th,td{padding:7px}thead{display:table-header-group}thead th{position:static}.table-wrap{overflow:visible}tr{break-inside:avoid}a{color:inherit}.controls,details,nav,.no-print{display:none}.workflow{padding:6px 10px;margin:8px 0}h1{font-size:21px}p{font-size:11px}.count{font-size:11px}}
"""
script = """
const allRows = Array.from(document.querySelectorAll('#metrics tbody tr'));
const search = document.querySelector('#search');
const stage = document.querySelector('#stage');
const scope = document.querySelector('#scope');
const coreOnly = document.querySelector('#core-only');
const count = document.querySelector('#result-count');
const empty = document.querySelector('#empty');
function filterRows() {
  const terms = search.value.trim().toLocaleLowerCase().split(/\\s+/).filter(Boolean);
  let visible = 0;
  for (const row of allRows) {
    const matches = (!coreOnly.checked || row.dataset.core === 'true') &&
      (!stage.value || row.dataset.stage === stage.value) &&
      (!scope.value || row.dataset.scope === scope.value) &&
      terms.every(term => row.dataset.search.includes(term));
    row.hidden = !matches;
    visible += Number(matches);
  }
  count.textContent = `当前显示 ${visible} / ${allRows.length} 项；所有项目均为待执行计划。`;
  empty.hidden = visible !== 0;
}
search.addEventListener('input', filterRows);
for (const control of [stage, scope, coreOnly]) control.addEventListener('change', filterRows);
document.querySelector('#reset').addEventListener('click', () => {
  search.value = ''; stage.value = ''; scope.value = ''; coreOnly.checked = false; filterRows();
});
document.querySelector('#print').addEventListener('click', () => window.print());
document.querySelector('#print-core').addEventListener('click', () => window.print());
function selectView() {
  const full = location.hash === '#full-metrics';
  document.querySelector('#core-summary').hidden = full;
  document.querySelector('#full-panel').hidden = !full;
  document.querySelectorAll('.view-nav a').forEach(link => {
    if ((link.hash === '#full-metrics') === full) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
}
window.addEventListener('hashchange', selectView);
selectView();
filterRows();
"""
document = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="source-sha256" content="{digest}"><link rel="icon" href="data:,"><title>各环节评估指标总表</title><style>{css}</style></head>
<body><main><header><div class="eyebrow">AI 自媒体生产 · 评估标准</div>
<h1>各环节评估指标总表</h1>
<p><strong>日常汇报看 {len(report_core_ids)} 项 · 完整验收保留 {len(rows)} 项</strong></p>
<nav><p><a href="{report_href}">项目方案汇报</a> · <a href="{main_href}">完整验收方案与 {ac_count} 个用例</a> · <a href="{flow_href}">数据流与工作流</a></p></nav>
<p class="note">★ 标明重点。“硬”必须满足，“建议”在测试前冻结，“观察”先记录。当前均为待执行标准，不是实测成绩；不能用综合分抵消硬项失败。</p>
<nav class="view-nav" aria-label="选择评估视图"><a href="#core-summary" aria-current="page">首版核心 · {len(report_core_ids)} 项</a><a href="#full-metrics">完整技术验收 · {len(rows)} 项</a></nav>
</header><section id="core-summary" aria-labelledby="core-summary-title"><h2 id="core-summary-title">首版核心指标速览</h2><p class="note">文案 3 人盲评，媒体 2 人逐项核验；无样本或缺测不能判通过。未列出的动作自然度、情绪、格式、响度等仍执行完整验收要求。</p><div class="core-actions no-print"><button id="print-core" type="button">打印核心表</button><a href="#full-metrics">查看完整测法与工程指标</a></div><div class="table-wrap" tabindex="0" aria-label="首版核心指标，可横向滚动">{report_core_table}</div></section>
<section id="full-panel" aria-labelledby="full-metrics"><h2 id="full-metrics">完整技术验收指标（{len(rows)}项）</h2><p class="note">{production} 项生产与运行指标 ＋ {future} 项后续扩展，★ {core} 项技术核心。M2 与 OmniHuman 分别报告；F01～F03 后续独立验收，V1 基础配置已在 E01。</p>
<section class="workflow" aria-label="评估流程">{workflow}</section>
<details><summary>展开样本、测量者与统计规则</summary>{methods}</details>
<details><summary>展开结果记录与失败判定</summary>{rules}</details>
<div class="controls" role="search" aria-label="筛选评估指标">
<label class="control search-control" for="search">搜索编号或内容<input id="search" type="search" placeholder="如：E08、情绪、最终音频" autocomplete="off"></label>
<label class="control" for="stage">环节<select id="stage"><option value="">全部环节</option>{stage_options}</select></label>
<label class="control" for="scope">范围<select id="scope"><option value="">全部范围</option><option value="production">生产与运行 E01～E28</option><option value="future">后续扩展 F01～F03</option></select></label>
<label class="core-toggle" for="core-only"><input id="core-only" type="checkbox">只看 ★ 技术核心</label>
<button id="reset" type="button">重置</button><button id="print" type="button">打印当前筛选</button>
</div><p id="result-count" class="count" aria-live="polite">当前显示 {len(rows)} / {len(rows)} 项；所有项目均为待执行计划。</p>
<p class="no-print note">窄屏可在表格内横向滚动；打印保留当前筛选结果。展开测法可查看样本与失败处置。</p>
<div class="table-wrap" tabindex="0" aria-label="指标总表，可横向滚动">{table}</div>
<p id="empty" class="empty" hidden>没有匹配的指标，请调整筛选或点击“重置”。</p>
<p class="note">TP/FP/FN/TN 分别为正确检出、误报、漏报、正确排除重复。</p></section>
<footer>字幕核对已审稿件、朗读映射和最终音轨；画面全片检查。失败与未完成任务保留在交付率分母中。
<br>来源：{escape(source.name)} 第 4 章；测法与总表由同一文档自动导出。保留全部 {ac_count} 个验收用例；本页不替代实际执行与证据签收。页面不依赖网络、外部脚本或字体。</footer></main><script>{script}</script></body></html>'''
(folder / "评估指标总表.html").write_text(document, encoding="utf-8")
print(f"Generated summary: {len(report_core_ids)} rows; full: {len(rows)} rows, {core} starred; source SHA-256 {digest}")
