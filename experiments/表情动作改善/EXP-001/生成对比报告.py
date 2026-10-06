#!/usr/bin/env python
"""Generate an offline comparison HTML from measured EXP-001 results.

Uses only Python's standard library. Media paths in the JSON are relative to
the JSON directory. Never creates result JSON, media, metrics, or rankings.
"""
from __future__ import annotations

import argparse
from html import escape
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sys
from urllib.parse import quote


ROOT = Path(__file__).resolve().parent
STATUSES = {
    "SUCCEEDED": "已生成",
    "FAILED": "执行失败",
    "RUNNING": "运行中",
    "NOT_RUN": "未运行",
}
METRICS = ("width", "height", "fps", "video_seconds", "audio_seconds",
           "wall_seconds", "gpu_peak_mib", "estimated_gpu_cny")


def text(value: object) -> str:
    return escape(str(value), quote=True)


def local_path(value: object, root: Path, field: str) -> Path | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是实验目录内的相对路径或 null")
    normalized = value.replace("\\", "/")
    windows = PureWindowsPath(value)
    if (windows.drive or windows.is_absolute() or PurePosixPath(normalized).is_absolute()
            or ":" in normalized or ".." in PurePosixPath(normalized).parts):
        raise ValueError(f"{field} 必须是实验目录内的相对路径：{value}")
    resolved = (root / normalized).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"{field} 必须是实验目录内的相对路径：{value}")
    return resolved


def file_url(path: Path, output_dir: Path) -> str:
    try:
        relative = Path(os.path.relpath(path, output_dir)).as_posix()
    except ValueError as exc:
        raise ValueError("HTML 输出与数据文件必须位于同一磁盘") from exc
    return quote(relative, safe="/")


def number(value: object, unit: str = "", decimals: int = 3) -> str:
    if value is None:
        return "未测"
    rendered = f"{value:,.{decimals}f}".rstrip("0").rstrip(".") if decimals else f"{value:,.0f}"
    return text(rendered + (" " + unit if unit else ""))


def list_of_strings(value: object, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field} 必须为字符串列表")
    return value


def validate(data: object, root: Path) -> dict:
    if not isinstance(data, dict):
        raise ValueError("结果数据必须为 JSON 对象")
    if not isinstance(data.get("inputs"), dict):
        raise ValueError("结果数据缺少 inputs 对象")
    clips = data.get("clips")
    if not isinstance(clips, list) or len(clips) != 3:
        raise ValueError("clips 必须包含 M1、M2、M3 三条路线")
    if any(not isinstance(clip, dict) for clip in clips):
        raise ValueError("clips 的每条路线必须为 JSON 对象")
    ids = [clip.get("id") for clip in clips]
    if sorted(str(item) for item in ids) != ["M1", "M2", "M3"]:
        raise ValueError("路线 id 必须为不重复的 M1、M2、M3")
    for clip in clips:
        clip_id = clip["id"]
        if clip.get("status") not in STATUSES:
            raise ValueError(f"{clip_id} status 必须为 SUCCEEDED、FAILED、RUNNING 或 NOT_RUN")
        for field in ("label", "model"):
            if not isinstance(clip.get(field), str) or not clip[field].strip():
                raise ValueError(f"{clip_id} 缺少 {field}")
        for field in METRICS:
            value = clip.get(field)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value) or value < 0):
                raise ValueError(f"{clip_id}.{field} 必须为非负有限数值或 null")
        for field in ("processing_steps", "processing_parameters", "observations", "limitations"):
            list_of_strings(clip.get(field), f"{clip_id}.{field}")
        if clip["status"] == "SUCCEEDED":
            video = local_path(clip.get("video_path"), root, f"{clip_id}.video_path")
            if video is None or not video.is_file():
                raise ValueError(f"成功路线的视频不存在：{clip_id} {clip.get('video_path')}")
        for field in ("poster_path", "evidence_path"):
            local_path(clip.get(field), root, f"{clip_id}.{field}")
    for field in ("avatar_path", "audio_path"):
        local_path(data["inputs"].get(field), root, f"inputs.{field}")
    list_of_strings(data.get("notes"), "notes")
    for field in ("summary", "updated_at", "cost_note"):
        if data.get(field) is not None and not isinstance(data[field], str):
            raise ValueError(f"{field} 必须为字符串或 null")
    data["clips"] = sorted(clips, key=lambda clip: clip["id"])
    return data


def linked_note(value: str, root: Path, output_dir: Path) -> str:
    """Allow simple [label](relative/file) links while escaping all other text."""
    rendered = []
    cursor = 0
    for match in re.finditer(r"\[([^\]\n]+)\]\(([^)\n]+)\)", value):
        rendered.append(text(value[cursor:match.start()]))
        target = local_path(match.group(2), root, "notes 链接")
        if target and target.is_file():
            rendered.append(f'<a href="{file_url(target, output_dir)}" target="_blank" rel="noopener">{text(match.group(1))}</a>')
        else:
            rendered.append(text(match.group(1)) + "（文件不可用）")
        cursor = match.end()
    rendered.append(text(value[cursor:]))
    return "".join(rendered)


def list_html(items: list[str], root: Path, output_dir: Path) -> str:
    return "<ul>" + "".join(f"<li>{linked_note(item, root, output_dir)}</li>" for item in items) + "</ul>"


def render(data: dict, root: Path, output_dir: Path) -> str:
    cards = []
    for clip in data["clips"]:
        status = clip["status"]
        if status == "SUCCEEDED":
            video = local_path(clip["video_path"], root, "video_path")
            url = file_url(video, output_dir)
            poster = local_path(clip.get("poster_path"), root, "poster_path")
            poster_attribute = f' poster="{file_url(poster, output_dir)}"' if poster and poster.is_file() else ""
            player = (
                f'<div class="video-frame"><video controls playsinline preload="metadata"{poster_attribute} '
                f'aria-label="{text(clip["label"])} 样片" src="{url}">浏览器不支持视频播放，请打开样片。</video></div>'
                f'<div class="media-link"><a href="{url}" target="_blank" rel="noopener">打开播放文件</a></div>'
            )
        else:
            player = f'<div class="no-video"><strong>{STATUSES[status]}</strong><p>暂无可播放的成功结果</p></div>'
        sections = []
        steps = clip.get("processing_steps") or []
        if steps:
            steps_html = "<ol>" + "".join(f"<li>{linked_note(item, root, output_dir)}</li>" for item in steps) + "</ol>"
            sections.append(f'<section class="clip-notes processing"><h3>处理方式 · 输入到输出</h3>{steps_html}</section>')
        parameters = clip.get("processing_parameters") or []
        if parameters:
            sections.append(f'<details class="clip-notes parameters"><summary>关键参数与处理证据</summary>{list_html(parameters, root, output_dir)}</details>')
        for field, label in (("observations", "观察记录"), ("limitations", "限制与问题")):
            items = clip.get(field) or []
            if items:
                sections.append(f'<section class="clip-notes"><h3>{label}</h3>{list_html(items, root, output_dir)}</section>')
        evidence = local_path(clip.get("evidence_path"), root, "evidence_path")
        if evidence and evidence.is_file():
            sections.append(f'<p class="evidence"><a href="{file_url(evidence, output_dir)}" target="_blank" rel="noopener">查看运行证据</a></p>')
        cards.append(
            f'<article class="clip" data-status="{status}"><header class="clip-header">'
            f'<span class="route-id">{text(clip["id"])}</span><span class="status">{STATUSES[status]}</span>'
            f'<h2>{text(clip["label"])}</h2><p>{text(clip["model"])}</p></header>'
            f'{player}{"".join(sections)}</article>'
        )

    def dimensions(clip: dict) -> str:
        if clip.get("width") is None or clip.get("height") is None:
            return "未测"
        return f'{number(clip["width"], decimals=0)} × {number(clip["height"], decimals=0)}'

    metric_rows = [
        ("状态", lambda c: STATUSES[c["status"]]),
        ("处理流程", lambda c: text(c.get("processing_summary") or "未记录")),
        ("表情与头动来源", lambda c: text(c.get("motion_source") or "未记录")),
        ("输出后处理", lambda c: text(c.get("postprocessing_summary") or "未记录")),
        ("动作观察（采样）", lambda c: text(c.get("motion_summary") or "尚未观察")),
        ("画面尺寸（像素）", dimensions),
        ("原始视频编码", lambda c: text(c.get("video_codec") or "未测")),
        ("帧率", lambda c: number(c.get("fps"), "fps")),
        ("视频时长", lambda c: number(c.get("video_seconds"), "秒")),
        ("音频时长", lambda c: number(c.get("audio_seconds"), "秒", 6)),
        ("原输出音频规格", lambda c: text(c.get("audio_format") or "未测")),
        ("运行耗时", lambda c: number(c.get("wall_seconds"), "秒", 2)),
        ("GPU 峰值显存", lambda c: number(c.get("gpu_peak_mib"), "MiB", 1)),
        ("活跃推理 GPU 估算", lambda c: number(c.get("estimated_gpu_cny"), "元", 4)),
    ]
    rows = "".join(
        f'<tr><th scope="row">{label}</th>' + "".join(f"<td>{formatter(clip)}</td>" for clip in data["clips"]) + "</tr>"
        for label, formatter in metric_rows
    )
    headers = "".join(f'<th scope="col">{text(clip["id"])} · {text(clip["label"])}</th>' for clip in data["clips"])
    inputs = data["inputs"]
    input_links = []
    for field, label in (("avatar_path", "人物实验输入图"), ("audio_path", "原始音频")):
        target = local_path(inputs.get(field), root, f"inputs.{field}")
        if target and target.is_file():
            input_links.append(f'<a href="{file_url(target, output_dir)}" target="_blank" rel="noopener">{label}</a>')
    input_description = " · ".join(text(inputs.get(field) or "未提供") for field in ("identity", "voice"))
    notes = list_html(data.get("notes") or [], root, output_dir) if data.get("notes") else '<p class="muted">暂无补充记录</p>'
    summary = text(data.get("summary") or "尚未记录结论")
    cost_note = f'<p>{text(data["cost_note"])}</p>' if data.get("cost_note") else ""
    document = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:,">
<title>人物 B 表情动作对比 · EXP-001</title>
<style>
:root { color-scheme: light; --paper: #FFFFFF; --panel: #F7F7F8; --ink: #17191e; --muted: #616771; --line: #d9dce2; --accent: #002FA7; }
* { box-sizing: border-box; }
body { margin: 0; color: var(--ink); background: var(--paper); font-family: "Helvetica Neue", Helvetica, "Microsoft YaHei", "PingFang SC", Arial, sans-serif; line-height: 1.7; }
main { max-width: 1600px; margin: 0 auto; padding: 42px 36px 64px; }
a { color: var(--accent); text-underline-offset: 4px; overflow-wrap: anywhere; }
a:focus-visible, video:focus-visible { outline: 3px solid var(--accent); outline-offset: 4px; }
h1, h2, h3, p { margin: 0; }
h1 { font-size: clamp(28px, 3.1vw, 48px); line-height: 1.2; font-weight: 650; letter-spacing: -.035em; }
.page-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 24px; border-top: 5px solid var(--accent); padding: 28px 0 24px; border-bottom: 1px solid var(--line); }
.page-header .updated { color: var(--muted); font-size: 13px; max-width: 300px; text-align: left; overflow-wrap: anywhere; }
.inputs { margin-top: 14px; color: var(--muted); }
.input-links { display: flex; gap: 18px; flex-wrap: wrap; margin-top: 9px; font-size: 14px; }
.summary { padding: 20px 0; font-size: 17px; max-width: 1060px; white-space: pre-wrap; overflow-wrap: anywhere; }
.play-hint { padding: 0 0 20px; font-size: 13px; color: var(--muted); }
.clips { display: grid; grid-template-columns: repeat(3,minmax(0,1fr)); border-top: 1px solid var(--line); border-left: 1px solid var(--line); }
.clip { min-width: 0; padding: 22px; border-right: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.clip-header { min-height: 136px; }
.route-id { display: inline-block; color: var(--accent); font-size: 28px; font-weight: 700; line-height: 1; letter-spacing: -.06em; }
.status { float: right; padding: 2px 8px; border: 1px solid var(--line); font-size: 12px; }
.clip[data-status="SUCCEEDED"] .status { border-color: var(--accent); color: var(--accent); }
.clip h2 { font-size: 20px; line-height: 1.4; margin-top: 17px; overflow-wrap: anywhere; }
.clip-header p { font-size: 12px; color: var(--muted); margin-top: 5px; overflow-wrap: anywhere; }
.video-frame { background: #000; height: clamp(290px,35vw,520px); display: flex; align-items: center; }
video { display: block; width: 100%; height: 100%; object-fit: contain; }
.media-link { padding: 10px 0 3px; font-size: 13px; }
.no-video { min-height: clamp(290px,35vw,520px); display: flex; flex-direction: column; justify-content: center; align-items: flex-start; gap: 8px; padding: 25px; background: var(--panel); border: 1px solid var(--line); }
.no-video p { font-size: 13px; color: var(--muted); }
.clip-notes { margin-top: 22px; }
h3 { font-size: 14px; font-weight: 650; }
ul, ol { padding-left: 19px; margin: 8px 0 0; }
li { margin: 6px 0; overflow-wrap: anywhere; }
.clip-notes li, .evidence { font-size: 13px; }
.processing { padding-top: 16px; border-top: 2px solid var(--accent); }
.processing li::marker { color: var(--accent); font-weight: 650; }
.parameters summary { cursor: pointer; font-size: 13px; color: var(--accent); }
.parameters summary:focus-visible { outline: 3px solid var(--accent); outline-offset: 4px; }
.evidence { margin-top: 16px; }
.section { margin-top: 36px; }
.section > h2 { font-size: 21px; margin-bottom: 15px; }
.table-wrap { overflow-x: auto; border: 1px solid var(--line); }
table { width: 100%; border-collapse: collapse; min-width: 700px; font-size: 14px; font-variant-numeric: tabular-nums; }
th, td { padding: 13px 16px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
thead th { background: var(--panel); font-weight: 600; }
tbody th { color: var(--muted); font-weight: 400; width: 25%; }
tbody td { width: 25%; }
tbody tr:last-child > * { border-bottom: 0; }
.cost-note { margin-top: 12px; color: var(--muted); font-size: 13px; }
.cost-note p + p { margin-top: 4px; }
.muted, footer { color: var(--muted); }
footer { margin-top: 34px; padding-top: 16px; border-top: 1px solid var(--line); font-size: 12px; }
@media (max-width: 850px) { main { padding: 20px 16px 40px; } .page-header { display: block; } .updated { margin-top: 14px; } .clips { grid-template-columns: 1fr; } .clip-header { min-height: auto; margin-bottom: 20px; } .video-frame, .no-video { height: 480px; min-height: 0; } }
@media print { main { padding: 0; } .video-frame { height: 260px; } .clips { grid-template-columns: repeat(3,minmax(0,1fr)); } .clip { padding: 12px; } .clip-notes { break-inside: avoid; } }
</style>
</head>
<body>
<main>
<header class="page-header"><div><h1>人物 B 表情动作对比</h1>
<p class="inputs">__INPUT_DESCRIPTION__</p><div class="input-links">__INPUT_LINKS__</div></div>
<p class="updated">EXP-001<br>数据更新：__UPDATED__</p></header>
<p class="summary">__SUMMARY__</p>
<p class="play-hint">播放一条视频时会自动暂停其他视频。每条样片下方列出实际处理步骤；M3 播放器使用 H.264 兼容版，原始文件保留在处理记录中。</p>
<section class="clips" aria-label="三条路线的样片、处理方式与观察记录">__CARDS__</section>
<section class="section" aria-labelledby="metrics-title"><h2 id="metrics-title">实测信息</h2>
<div class="table-wrap"><table><thead><tr><th scope="col">指标</th>__HEADERS__</tr></thead><tbody>__ROWS__</tbody></table></div>
<div class="cost-note"><p>“未测”表示没有记录该指标。GPU 费用仅为活跃推理估算，安装、下载、空闲及其他云端计费未计入，不能视为云账单总额。</p>__COST_NOTE__</div></section>
<section class="section" aria-labelledby="notes-title"><h2 id="notes-title">补充记录</h2>__NOTES__</section>
<footer>本页按结果文件中的状态与记录展示，不提供自动排名或推测评分。</footer>
</main>
<script>
(() => {
  const videos = Array.from(document.querySelectorAll('video'));
  videos.forEach(video => {
    video.addEventListener('play', () => {
      videos.forEach(other => { if (other !== video) other.pause(); });
    });
  });
})();
</script>
</body></html>
"""
    parts = {
        "INPUT_DESCRIPTION": input_description,
        "INPUT_LINKS": "".join(input_links),
        "UPDATED": text(data.get("updated_at") or "未记录"),
        "SUMMARY": summary,
        "CARDS": "".join(cards),
        "HEADERS": headers,
        "ROWS": rows,
        "COST_NOTE": cost_note,
        "NOTES": notes,
    }
    # One pass prevents result text that resembles a template marker being expanded.
    return re.sub(r"__([A-Z_]+)__", lambda match: parts[match.group(1)], document)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从真实对比结果 JSON 生成离线 HTML；缺少数据时直接报错，不生成样例。")
    parser.add_argument("--data", type=Path, default=ROOT / "对比结果.json", help="结果 JSON，默认读取脚本同目录的 对比结果.json")
    parser.add_argument("--output", type=Path, default=None, help="输出 HTML，默认与数据文件同目录：人物B表情动作对比.html")
    args = parser.parse_args(argv)
    data_path = args.data.resolve()
    if not data_path.is_file():
        print(f"缺少结果数据：{data_path}；请先写入真实 对比结果.json，未生成报告。", file=sys.stderr)
        return 2
    output = (args.output or data_path.parent / "人物B表情动作对比.html").resolve()
    if output == data_path or output.suffix.lower() != ".html":
        print("生成失败：输出路径必须是独立的 .html 文件。", file=sys.stderr)
        return 2
    try:
        data = validate(json.loads(data_path.read_text(encoding="utf-8-sig")), data_path.parent)
        html = render(data, data_path.parent, output.parent)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(html, encoding="utf-8", newline="\n")
    except (OSError, ValueError, TypeError) as exc:
        print(f"生成失败：{exc}；本次未写入报告。", file=sys.stderr)
        return 2
    print(f"已生成：{output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
