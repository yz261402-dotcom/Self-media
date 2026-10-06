"""从本轮实际执行证据生成离线候选选择页；不调用模型，不修改正式资产。"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime
import hashlib
import html
import json
from pathlib import Path
import re
from urllib.parse import quote


ROOT = Path(__file__).resolve().parent
TITLE = "人物与音色选择"
STAGES = ("image_A", "image_B", "image_C", "voice_1", "voice_2", "voice_3")
IMAGE_DESCRIPTIONS = {
    "A": "齐肩发 · 温柔知性",
    "B": "长直发 · 温暖自然",
    "C": "低马尾 · 清爽利落",
}


def escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def local_href(path: Path) -> str:
    return quote(path.relative_to(ROOT).as_posix(), safe="/")


def constants() -> tuple[str, dict]:
    """仅解析文本常量，不导入或运行候选执行器。"""
    values: dict = {}
    tree = ast.parse((ROOT / "候选执行器.py").read_text(encoding="utf-8-sig"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in {"SPEECH", "VOICES"}:
                values[target.id] = ast.literal_eval(node.value)
    return str(values.get("SPEECH", "")), values.get("VOICES", {})


def load_candidate(stage: str, voices: dict) -> dict:
    directory = ROOT / "执行证据" / stage
    evidence = directory / "执行结果.json"
    started_path = directory / "attempt.started.json"
    record = read_json(evidence)
    request = read_json(started_path).get("request", {})
    request = request if isinstance(request, dict) else {}
    result = record.get("result", {})
    result = result if isinstance(result, dict) else {}
    is_image = stage.startswith("image_")
    code = stage.rsplit("_", 1)[-1]
    label = f"人物{code}" if is_image else f"音色{code}"
    state = record.get("state") or ("STARTED_WITHOUT_RESULT" if started_path.is_file() else "NOT_SUBMITTED")
    available = False
    asset_href = ""
    issue = ""
    if state == "SUCCEEDED":
        raw_path = result.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            issue = "执行结果没有可用文件路径"
        else:
            asset = (ROOT / raw_path.replace("\\", "/")).resolve()
            try:
                asset.relative_to(ROOT.resolve())
            except ValueError:
                issue = "文件路径超出本轮目录，未加载"
            else:
                if not asset.is_file():
                    issue = "执行已成功，但本地文件缺失"
                else:
                    expected = result.get("sha256")
                    actual = hashlib.sha256(asset.read_bytes()).hexdigest()
                    if not expected or actual != expected:
                        issue = "本地文件与执行记录的 SHA-256 不一致，暂停选择"
                    else:
                        available = True
                        asset_href = local_href(asset)
    elif state == "UNKNOWN_OR_FAILED":
        issue = "调用失败或结果未知；没有可审阅成品，未自动重试"
    elif state == "STARTED_WITHOUT_RESULT":
        issue = "已有提交标记，尚无执行结果；不推断生成成功"
    else:
        issue = "尚无提交记录"

    prompt_path = ROOT / "提示词" / f"人物{code}.txt"
    prompt = prompt_path.read_text(encoding="utf-8-sig") if is_image and prompt_path.is_file() else ""
    observation = (read_json(ROOT / "候选审查.json").get(stage) or {}).get("observations", "") if is_image else ""
    name = result.get("name") or (voices.get(stage, ("", ""))[0] if not is_image else "")
    voice_id = result.get("voice_id") or (voices.get(stage, ("", ""))[1] if not is_image else "")
    width, height = result.get("width"), result.get("height")
    requested_size = request.get("size")
    measured_size = f"{width}×{height}" if width and height else "未测量"
    mismatch = bool(requested_size and width and height and str(requested_size).lower() != f"{width}x{height}")
    return {
        "stage": stage, "kind": "image" if is_image else "voice", "code": code,
        "label": label, "name": str(name), "voice_id": str(voice_id),
        "state": str(state), "available": available, "issue": issue,
        "href": asset_href, "sha256": result.get("sha256", ""),
        "evidence_href": local_href(evidence) if evidence.is_file() else "",
        "elapsed_seconds": record.get("elapsed_seconds"), "seconds": result.get("seconds"),
        "estimated_cny": result.get("estimated_cny"), "actual_cost_cny": result.get("actual_cost_cny"),
        "usage_characters": result.get("usage_characters"), "width": width, "height": height,
        "measured_size": measured_size, "requested_size": str(requested_size or "未记录"),
        "size_mismatch": mismatch, "prompt": prompt,
        "prompt_href": local_href(prompt_path) if prompt else "", "error_type": record.get("error_type", ""),
        "description": IMAGE_DESCRIPTIONS.get(code, "") if is_image else "",
        "observation": str(observation),
        "model": str(request.get("model") or result.get("model") or "未记录"),
        "voice_setting": request.get("voice_setting", {}),
    }


def number(value: object, precision: int = 2, suffix: str = "") -> str:
    if isinstance(value, (int, float)):
        return f"{value:,.{precision}f}{suffix}"
    return "未记录"


def load_selection(candidates: list[dict]) -> dict:
    """正式选择仅来自定版记录，并与已核验的本地候选文件绑定。"""
    record = read_json(ROOT / "人物与音色定版.json")
    if (record.get("schema_version") != 1
            or record.get("selection_status") != "SELECTED_BY_USER"
            or record.get("approval_scope") != "identity_and_voice_selection_only"
            or record.get("production_acceptance") != "PENDING_ACCEPTANCE"):
        raise ValueError("定版记录缺失或状态不符合人物与音色选择范围，停止生成页面")
    selection = record.get("selection")
    if not isinstance(selection, dict):
        raise ValueError("定版记录缺少人物与音色编号")
    for kind, field in (("image", "avatar"), ("voice", "voice")):
        candidate = next((c for c in candidates if c["kind"] == kind and c["code"] == selection.get(kind)), None)
        asset = record.get(field)
        if (not candidate or not candidate["available"] or not isinstance(asset, dict)
                or asset.get("sha256") != candidate["sha256"]):
            raise ValueError(f"定版记录的 {field} 与实际候选文件 SHA-256 不一致，停止生成页面")
        if kind == "voice" and asset.get("voice_id") != candidate["voice_id"]:
            raise ValueError("定版音色 voice_id 与候选记录不一致，停止生成页面")
    return {"image": selection["image"], "voice": selection["voice"]}


def evidence_link(candidate: dict) -> str:
    href = candidate["evidence_href"]
    return f'<a href="{escape(href)}" target="_blank" rel="noopener">执行记录</a>' if href else "暂无执行记录"


def image_card(candidate: dict) -> str:
    c = candidate
    mismatch = (
        '<p class="size-note"><strong>尺寸不一致，待验证</strong><br>'
        f'请求 {escape(c["requested_size"].replace("x", "×"))}；实际 {escape(c["measured_size"])}。'
        '此处展示原图，未放大冒充请求尺寸。</p>'
        if c["size_mismatch"] else ""
    )
    return f'''<article class="candidate image-card{' selected' if c['official'] else ''}" data-kind="image" data-code="{c['code']}">
      <div class="candidate-top"><h3>{c['label']}</h3><span class="tag">{'正式已选' if c['official'] else '可对比'}</span></div>
      <button class="portrait-button" type="button" data-enlarge="{c['code']}" aria-label="放大查看{c['label']}">
        <img src="{escape(c['href'])}" alt="{c['label']}，本轮实际生成的年轻成年女性主播候选" width="{c['width']}" height="{c['height']}" loading="eager">
        <span class="enlarge-hint">放大查看</span>
      </button>
      <div class="card-content"><p class="candidate-description">{escape(c['description'])}<span>提示词中的设计方向</span></p>
      {mismatch}<dl class="metrics"><div><dt>实际图片</dt><dd>{escape(c['measured_size'])}</dd></div><div><dt>生成耗时</dt><dd>{number(c['elapsed_seconds'], 2, ' 秒')}</dd></div><div><dt>图片费用</dt><dd>中转价格未知，待对账</dd></div></dl>
      <button class="choice-button" type="button" data-choose-image="{c['code']}" aria-pressed="{str(c['official']).lower()}">{'正在查看' if c['official'] else '对比'}{c['label']}</button>
      <div class="file-links"><a href="{escape(c['href'])}" target="_blank" rel="noopener">打开全图</a><a href="{escape(c['href'])}" download>下载原图</a>{evidence_link(c)}</div>
      <details><summary>查看静态观察、提示词与资产信息</summary><p>{escape(c['observation']) or '尚无静态观察记录。'}</p><p class="small">以上仅为静态图片观察，数字人口播效果还未验证。</p><pre>{escape(c['prompt'])}</pre><p><a href="{escape(c['prompt_href'])}" target="_blank" rel="noopener">提示词文件</a></p><p class="small">请求模型：{escape(c['model'])}，经专用中转；上游模型身份尚未独立核验。</p><p class="hash">SHA-256：{escape(c['sha256'])}</p></details></div>
    </article>'''


def voice_card(candidate: dict) -> str:
    c = candidate
    estimate = number(c["estimated_cny"], 4, " 元") if c["estimated_cny"] is not None else "尚未估算"
    settings = c["voice_setting"] if isinstance(c["voice_setting"], dict) else {}
    return f'''<article class="candidate voice-card{' selected' if c['official'] else ''}" data-kind="voice" data-code="{c['code']}">
      <div class="candidate-top"><h3>{c['label']} · {escape(c['name'])}</h3><span class="tag">{'正式已选' if c['official'] else '可对比'}</span></div>
      <p class="small">MiniMax Speech 2.8 HD · 原始 WAV</p>
      <audio controls preload="metadata" src="{escape(c['href'])}" aria-label="试听{c['label']}{escape(c['name'])}">当前浏览器不支持音频播放，请下载原始 WAV 试听。</audio>
      <dl class="metrics"><div><dt>音频时长</dt><dd>{number(c['seconds'], 2, ' 秒')}</dd></div><div><dt>生成耗时</dt><dd>{number(c['elapsed_seconds'], 2, ' 秒')}</dd></div><div><dt>TTS 估算</dt><dd>{estimate}<small>实际费用待对账</small></dd></div></dl>
      <button class="choice-button" type="button" data-choose-voice="{c['code']}" aria-pressed="{str(c['official']).lower()}">{'正在查看' if c['official'] else '对比'}{c['label']}</button>
      <div class="file-links"><a href="{escape(c['href'])}" download>下载原始 WAV</a>{evidence_link(c)}</div>
      <details><summary>查看声音参数</summary><dl class="detail-list"><div><dt>voice_id</dt><dd>{escape(c['voice_id'])}</dd></div><div><dt>语速 / 音调 / 情绪</dt><dd>{escape(settings.get('speed', '未记录'))} / {escape(settings.get('pitch', '未记录'))} / {escape(settings.get('emotion', '未记录'))}</dd></div><div><dt>返回计费字符数</dt><dd>{escape(c['usage_characters'] if c['usage_characters'] is not None else '未记录')}</dd></div><div><dt>文件规格</dt><dd>32 kHz · 单声道 · PCM WAV</dd></div></dl><p class="hash">SHA-256：{escape(c['sha256'])}</p></details>
    </article>'''


CSS = r'''
:root{color-scheme:light;--paper:#fff;--surface:#f7f7f8;--ink:#17191e;--muted:#656a73;--line:#dedfe3;--accent:#002fa7;--sans:"Helvetica Neue",Helvetica,Arial,"PingFang SC","Microsoft YaHei",sans-serif}
*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:28px}body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);line-height:1.65}a{color:var(--accent);text-underline-offset:4px}button,input{font:inherit}button{cursor:pointer}button:focus-visible,a:focus-visible,input:focus-visible,summary:focus-visible{outline:3px solid var(--accent);outline-offset:4px}button:disabled{cursor:not-allowed;opacity:.45}button{border:1px solid var(--line);border-radius:0}main{max-width:1360px;margin:auto;padding:0 44px 60px}.topline{padding:22px 0 18px;display:flex;justify-content:space-between;border-bottom:1px solid var(--ink);font-size:13px;gap:12px}.topline a{text-decoration:none}.hero{padding:46px 0 36px;display:grid;grid-template-columns:1fr 280px;gap:48px}.eyebrow{color:var(--accent);font-size:14px;margin:0 0 14px}.hero h1{font-size:clamp(30px,4.4vw,56px);font-weight:600;line-height:1.15;letter-spacing:-.045em;margin:0 0 20px}.intro{max-width:750px;margin:0;color:var(--muted);font-size:16px}.availability{border-left:1px solid var(--line);padding:4px 0 0 28px;display:grid;grid-template-columns:1fr 1fr;align-content:start;gap:20px}.count{font-size:49px;line-height:1;font-weight:500;color:var(--accent);display:block;font-variant-numeric:tabular-nums}.count-label{display:block;font-size:13px;margin-top:11px;color:var(--muted)}.availability p{grid-column:1/-1;margin:0;font-size:12px;color:var(--muted)}.jump{display:flex;gap:0;border-top:1px solid var(--line);border-bottom:1px solid var(--line);margin-bottom:48px}.jump a{padding:15px 22px 15px 0;margin-right:26px;color:var(--ink);text-decoration:none;font-size:14px}.jump a span{color:var(--accent);margin-right:8px}section{margin-bottom:48px}.section-heading{display:flex;align-items:start;gap:18px;margin-bottom:22px}.section-number{font-size:34px;line-height:1.2;color:var(--accent);font-variant-numeric:tabular-nums}.section-heading h2{font-size:23px;line-height:1.35;margin:0 0 8px;font-weight:600;letter-spacing:-.03em}.section-heading p{margin:0;color:var(--muted);font-size:14px}.candidate-grid{display:grid;grid-template-columns:repeat(var(--columns,3),minmax(0,1fr));gap:24px}.candidate{border:1px solid var(--line);min-width:0;background:#fff;position:relative}.candidate.selected{outline:2px solid var(--accent);outline-offset:-2px}.candidate-top{padding:15px 18px;display:flex;align-items:baseline;justify-content:space-between;gap:12px}.candidate-top h3{margin:0;font-size:19px;line-height:1.4;font-weight:600}.tag{font-size:12px;color:var(--muted);white-space:nowrap}.selected .tag{color:var(--accent);font-weight:600}.portrait-button{display:block;width:100%;border:0;border-top:1px solid var(--line);border-bottom:1px solid var(--line);padding:0;background:var(--surface);position:relative;text-align:left;overflow:hidden}.portrait-button img{display:block;width:100%;height:min(54vw,610px);object-fit:contain;background:var(--surface)}.enlarge-hint{position:absolute;right:14px;bottom:14px;background:#fff;color:var(--ink);padding:6px 12px;border:1px solid var(--line);font-size:12px}.card-content{padding:18px}.candidate-description{margin:0 0 16px;font-size:18px}.candidate-description span{display:block;color:var(--muted);font-size:12px;margin-top:2px}.size-note{padding:12px 14px;background:var(--surface);border-left:3px solid var(--accent);font-size:13px;margin:0 0 17px}.size-note strong{color:var(--accent)}.metrics{margin:0 0 20px;padding:0;display:grid;grid-template-columns:1fr 1fr;gap:14px 12px}.metrics>div:last-child{grid-column:1/-1}.metrics dt{color:var(--muted);font-size:12px}.metrics dd{margin:3px 0 0;font-size:16px;font-variant-numeric:tabular-nums}.metrics dd small{display:block;font-size:12px;color:var(--muted)}.choice-button{width:100%;padding:11px 14px;background:#fff;color:var(--accent);border-color:var(--accent);font-weight:600}.choice-button:hover{background:var(--surface)}.choice-button[aria-pressed=true]{background:var(--accent);color:white}.file-links{display:flex;flex-wrap:wrap;gap:8px 18px;font-size:12px;margin-top:15px}.candidate details{margin-top:19px;padding-top:12px;border-top:1px solid var(--line);font-size:12px}summary{cursor:pointer}.candidate pre{white-space:pre-wrap;font:inherit;font-size:12px;line-height:1.8;word-break:break-word;color:var(--muted);margin:12px 0}.hash{font-size:11px;overflow-wrap:anywhere;color:var(--muted);margin-bottom:0}.small{font-size:12px;color:var(--muted)}.script{padding:21px 25px;background:var(--surface);margin-bottom:24px;border-left:3px solid var(--accent)}.script h3{font-size:13px;margin:0 0 10px}.script p{margin:0;font-size:17px;line-height:1.9}.script .small{font-size:12px;margin-top:10px}.voice-card{padding:19px}.voice-card .candidate-top{padding:0}.voice-card audio{width:100%;height:46px;margin:12px 0 22px}.voice-card>.small{margin:9px 0 0}.voice-card .metrics{grid-template-columns:1fr 1fr}.detail-list{margin-bottom:0}.detail-list>div{margin-top:9px}.detail-list dt{color:var(--muted)}.detail-list dd{margin:0;overflow-wrap:anywhere}.quality-guide{padding:16px 0 0;font-size:13px;color:var(--muted);margin:0}.combination{border:1px solid var(--line);display:grid;grid-template-columns:220px 1fr;min-height:290px}.combination-visual{border-right:1px solid var(--line);background:var(--surface);padding:20px;display:flex;align-items:flex-start}.combination-visual img{display:block;width:100%;height:270px;object-fit:contain}.combination-visual p{font-size:14px;color:var(--muted)}[hidden]{display:none!important}.combination-copy{padding:28px}.combination-copy h3{font-size:25px;font-weight:500;margin:0 0 10px}.combination-copy>p{margin:0 0 15px;color:var(--muted);font-size:14px}.combination-copy audio{display:block;width:min(440px,100%);margin:20px 0 14px;height:44px}.selection-actions{display:flex;flex-wrap:wrap;gap:12px;margin-top:22px}.action-primary{background:var(--accent);color:#fff;border-color:var(--accent);padding:10px 18px}.action-secondary{background:#fff;color:var(--ink);padding:10px 18px}.selection-message{margin-top:14px;color:var(--accent);font-size:13px;min-height:1.65em}.share-field{display:block;width:100%;max-width:480px;padding:10px 12px;border:1px solid var(--line);background:var(--surface);margin-top:15px;font-size:15px}.audit{border-top:1px solid var(--line);padding-top:24px}.audit>summary{font-size:15px}.audit-list{list-style:none;padding:0;margin:18px 0}.audit-list li{display:grid;grid-template-columns:160px 1fr auto;gap:15px;padding:18px 0;border-bottom:1px solid var(--line);font-size:13px}.audit-list strong{font-weight:600}.audit-list p{margin:0;color:var(--muted)}.audit-code{display:block;font-size:11px;margin-top:4px;overflow-wrap:anywhere}.empty-state{padding:26px;border:1px solid var(--line);background:var(--surface);color:var(--muted)}footer{border-top:1px solid var(--ink);padding-top:23px;font-size:12px;color:var(--muted);display:flex;justify-content:space-between;gap:20px;margin-top:34px}footer p{margin:0}.modal{border:1px solid var(--line);padding:0;max-width:96vw;width:min(980px,96vw);max-height:94vh;background:#fff}.modal::backdrop{background:rgba(0,0,0,.66)}.modal-header{padding:12px 16px;display:flex;justify-content:space-between;align-items:center;gap:16px;border-bottom:1px solid var(--line)}.modal-header h2{font-size:18px;margin:0}.modal-header button{padding:5px 12px;background:#fff}.modal img{display:block;max-width:100%;max-height:calc(94vh - 72px);width:auto;height:auto;margin:auto}.modal-header a{font-size:12px;margin-left:auto}.noscript{border:1px solid var(--line);padding:18px;margin:20px 0}.time-note{font-size:12px;color:var(--muted);margin-top:20px}
@media(min-width:1150px){.candidate-grid[data-count="1"]{max-width:650px}.candidate-grid[data-kind="image"][data-count="2"] .portrait-button img{height:590px}}
@media(max-width:900px){main{padding:0 25px 45px}.hero{grid-template-columns:1fr;gap:24px;padding:34px 0}.availability{border-left:0;border-top:1px solid var(--line);padding:20px 0 0;grid-template-columns:100px 100px 1fr;align-items:center}.availability p{grid-column:auto}.count{font-size:35px}.count-label{margin-top:6px}.candidate-grid{gap:17px}.candidate-grid[data-count="3"]{grid-template-columns:repeat(2,minmax(0,1fr))}.combination{grid-template-columns:190px 1fr}.combination-copy{padding:22px}.candidate-top{padding:13px}.card-content{padding:14px}.voice-card{padding:15px}}
@media(max-width:620px){main{padding:0 18px 36px}.topline{font-size:11px;padding:16px 0}.hero{padding:29px 0 27px}.hero h1{font-size:33px;line-height:1.22}.intro{font-size:14px}.availability{grid-template-columns:80px 80px 1fr;gap:12px}.availability p{font-size:11px}.count{font-size:31px}.count-label{font-size:11px}.jump{margin-bottom:31px;justify-content:space-between}.jump a{margin:0;padding:12px 0;font-size:12px}.jump a span{margin-right:4px}.section-number{font-size:29px}.section-heading{gap:12px}.section-heading h2{font-size:21px}.section-heading p{font-size:12px}.candidate-grid,.candidate-grid[data-count="3"]{grid-template-columns:1fr}.portrait-button img{height:auto;max-height:650px}.candidate-top{padding:15px}.candidate-top h3{font-size:19px}.card-content{padding:17px}.candidate-description{font-size:17px}.voice-card{padding:18px}.script{padding:17px 18px}.script p{font-size:15px}.combination{grid-template-columns:1fr}.combination-visual{border-right:0;border-bottom:1px solid var(--line);padding:16px}.combination-visual img{height:240px}.combination-copy{padding:20px}.combination-copy h3{font-size:22px}.selection-actions{gap:10px}.selection-actions button{flex:1 1 140px;padding:10px;font-size:13px}.audit-list li{grid-template-columns:1fr;gap:7px}.audit-list li>a{justify-self:start}footer{flex-direction:column;gap:12px}.modal{max-width:94vw}.modal-header{flex-wrap:wrap}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
'''


JS = r'''
const data=JSON.parse(document.getElementById('asset-data').textContent);
const available=(kind,code)=>data.candidates.find(c=>c.available&&c.kind===kind&&c.code===code);
const official=Object.freeze(data.authoritative_selection);
const selection={...official};
const key='AV-001-candidate-selection-v1';
const image=document.getElementById('combination-image');
const audio=document.getElementById('combination-audio');
const message=document.getElementById('selection-message');
const copyButton=document.getElementById('copy-selection');
const saveButton=document.getElementById('save-selection');
const field=document.getElementById('selection-text');
function phrase(){return `对比组合：人物${selection.image}＋音色${selection.voice}`}
function persist(){try{localStorage.setItem(key,JSON.stringify(selection))}catch(e){}}
function update(){
  const avatar=available('image',selection.image),voice=available('voice',selection.voice);
  document.querySelectorAll('.candidate').forEach(card=>{
    const active=selection[card.dataset.kind]===card.dataset.code;
    const confirmed=official[card.dataset.kind]===card.dataset.code;
    card.classList.toggle('selected',active);
    card.querySelector('.tag').textContent=confirmed?'正式已选':active?'本机对比中':'可对比';
    const button=card.querySelector('.choice-button');
    button.setAttribute('aria-pressed',String(active));
    button.textContent=(active?'正在查看':'对比')+(card.dataset.kind==='image'?'人物':'音色')+card.dataset.code;
  });
  image.hidden=!avatar;document.getElementById('combination-empty').hidden=!!avatar;
  if(avatar){image.src=avatar.href;image.alt=`当前查看：${avatar.label}`;}
  else image.removeAttribute('src');
  document.getElementById('combination-title').textContent=(avatar?avatar.label:'请选择人物')+' ＋ '+(voice?voice.label+' · '+voice.name:'请选择音色');
  if(voice){if(audio.getAttribute('src')!==voice.href){audio.pause();audio.src=voice.href;}audio.hidden=false;}
  else{audio.pause();audio.removeAttribute('src');audio.hidden=true;}
  const complete=!!(avatar&&voice);copyButton.disabled=!complete;saveButton.disabled=!complete;
  field.hidden=!complete;field.value=complete?phrase():'';
  const isOfficial=selection.image===official.image&&selection.voice===official.voice;
  document.getElementById('selection-status').textContent=isOfficial?'当前展示正式已选组合；本次仅确认人物与音色，生产验收待完成。':`当前仅为本机临时对比；正式选择仍是人物${official.image}＋音色${official.voice}，刷新后恢复正式组合。`;
}
// 每次打开均以定版记录为准，旧 localStorage 不得覆盖正式选择。
document.querySelectorAll('[data-choose-image]').forEach(button=>button.addEventListener('click',()=>{selection.image=button.dataset.chooseImage;message.textContent='已切换本机对比人物，正式选择不变。';persist();update();}));
document.querySelectorAll('[data-choose-voice]').forEach(button=>button.addEventListener('click',()=>{selection.voice=button.dataset.chooseVoice;message.textContent='已切换本机对比音色，正式选择不变。';persist();update();}));
document.querySelectorAll('audio').forEach(player=>player.addEventListener('play',()=>{document.querySelectorAll('audio').forEach(other=>{if(other!==player)other.pause();});}));
copyButton.addEventListener('click',async()=>{
  try{if(!navigator.clipboard)throw new Error('clipboard unavailable');await navigator.clipboard.writeText(phrase());message.textContent='已复制当前对比组合。需要改选时，请明确确认新的组合；此操作不改变正式选择。';}
  catch(e){field.hidden=false;field.focus();field.select();let ok=false;try{ok=document.execCommand('copy')}catch(e){}message.textContent=ok?'已复制对比文字，正式选择不变。':'对比文字已选中，请按 Ctrl+C 或长按复制。';}
});
saveButton.addEventListener('click',()=>{
  const avatar=available('image',selection.image),voice=available('voice',selection.voice);if(!avatar||!voice)return;
  const documentData={experiment:'AV-001',status:'PENDING_USER_CONFIRMATION',created_at:new Date().toISOString(),selection_text:phrase(),avatar:{candidate:avatar.label,path:decodeURIComponent(avatar.href),sha256:avatar.sha256},voice:{candidate:voice.label,name:voice.name,voice_id:voice.voice_id,path:decodeURIComponent(voice.href),sha256:voice.sha256},note:'待确认改选草稿；不改变已有定版记录，不构成正式资产批准或生产验收。'};
  const url=URL.createObjectURL(new Blob([JSON.stringify(documentData,null,2)],{type:'application/json;charset=utf-8'}));
  const link=document.createElement('a');link.href=url;link.download='人物与音色待确认改选草稿.json';document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);message.textContent='已下载待确认改选草稿。正式选择不变，下载文件不构成批准。';
});
document.getElementById('clear-selection').addEventListener('click',()=>{Object.assign(selection,official);try{localStorage.removeItem(key)}catch(e){}update();message.textContent='已恢复正式已选组合。';});
const dialog=document.getElementById('portrait-modal');
document.querySelectorAll('[data-enlarge]').forEach(button=>button.addEventListener('click',()=>{
  const candidate=available('image',button.dataset.enlarge);if(!candidate)return;
  if(typeof dialog.showModal!=='function'){window.open(candidate.href,'_blank','noopener');return;}
  document.getElementById('modal-image').src=candidate.href;document.getElementById('modal-image').alt=candidate.label+'原图';
  document.getElementById('modal-title').textContent=candidate.label+' · '+candidate.measured_size;
  document.getElementById('modal-file').href=candidate.href;dialog.showModal();
}));
document.getElementById('close-modal').addEventListener('click',()=>dialog.close());
dialog.addEventListener('click',e=>{if(e.target===dialog){const rect=dialog.getBoundingClientRect();if(e.clientX<rect.left||e.clientX>rect.right||e.clientY<rect.top||e.clientY>rect.bottom)dialog.close();}});
update();
'''


def build() -> str:
    speech, voices = constants()
    candidates = [load_candidate(stage, voices) for stage in STAGES]
    selection = load_selection(candidates)
    for candidate in candidates:
        candidate["official"] = candidate["code"] == selection[candidate["kind"]]
    images = [c for c in candidates if c["available"] and c["kind"] == "image"]
    sounds = [c for c in candidates if c["available"] and c["kind"] == "voice"]
    selected_image = next(c for c in images if c["official"])
    selected_voice = next(c for c in sounds if c["official"])
    selected_label = f'{selected_image["label"]}＋{selected_voice["label"]}（{selected_voice["name"]}）'
    unavailable = [c for c in candidates if not c["available"]]
    plain_speech = re.sub(r"<#\d+(?:\.\d+)?#>", "〔短暂停顿〕", speech)
    image_html = "".join(image_card(c) for c in images) or '<p class="empty-state">当前没有可审阅的人物成品。执行状态在页尾保留；未放入替代头像。</p>'
    voice_html = "".join(voice_card(c) for c in sounds) or '<p class="empty-state">当前没有可试听的音色成品。生成成功并核验文件后，才会出现播放器。</p>'
    known_estimates = [c['estimated_cny'] for c in sounds if isinstance(c['estimated_cny'], (int, float))]
    total_cost_note = f"已成功音频的已知估算合计 {sum(known_estimates):.4f} 元。" if known_estimates else "暂无可汇总的 TTS 费用估算。"
    failures = "".join(
        f'<li><strong>{escape(c["label"])}{(" · " + escape(c["name"])) if c["name"] else ""}<span class="audit-code">{escape(c["state"])}</span></strong><div><p>{escape(c["issue"])}</p><p class="small">{escape(c["error_type"])} · 已记录耗时 {number(c["elapsed_seconds"], 2, " 秒")} · 费用未核实</p></div>{evidence_link(c)}</li>'
        for c in unavailable
    )
    audit_html = f'<details class="audit"><summary>查看未提供成品的候选与执行记录（{len(unavailable)} 项）</summary><ul class="audit-list">{failures}</ul><p class="small">失败或结果未知不等于零扣费；本页不重新提交任何任务，也不将这些候选纳入可选素材。</p></details>' if unavailable else ""
    # 仅将选择交互所需的公开字段放进页面，不包含原始 API 响应。
    public_fields = ("kind", "code", "label", "name", "voice_id", "available", "href", "sha256", "measured_size")
    asset_data = json.dumps({"authoritative_selection": selection, "candidates": [{key: c[key] for key in public_fields} for c in candidates]}, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    generated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %z")
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light"><link rel="icon" href="data:,"><title>{TITLE} · AV-001</title><style>{CSS}</style></head>
<body><main>
<div class="topline"><span>AI 科普主播 · 人物与音色选型</span><a href="候选方案.md" target="_blank" rel="noopener">AV-001 · 查看候选方案</a></div>
<header class="hero"><div><p class="eyebrow">已完成用户选择 · 22 岁左右 · 明确成年 · 专业温柔</p><h1>{escape(selected_image['label'])}，<br>{escape(selected_voice['label'])} · {escape(selected_voice['name'])}。</h1><p class="intro"><strong>已正式选择 {escape(selected_label)}。</strong>本次仅确认人物与音色，生产验收待完成。其他候选可继续临时对比，刷新后恢复正式组合。<br><a href="../../../specs/selection/03-人物与音色/02-人物B与音色3定版记录.html">查看人物与音色定版记录</a></p></div><div class="availability"><div><span class="count">{len(images):02d}</span><span class="count-label">可查看人物</span></div><div><span class="count">{len(sounds):02d}</span><span class="count-label">可试听音色</span></div><p>原图与原始 WAV<br>临时对比不改变正式选择</p></div></header>
<nav class="jump" aria-label="页面章节"><a href="#portraits"><span>01</span> 看人物</a><a href="#voices"><span>02</span> 听声音</a><a href="#combination"><span>03</span> 看组合</a></nav>
<noscript><p class="noscript">正式已选组合为 {escape(selected_label)}，图片和音频可以直接查看、播放。启用 JavaScript 后可临时搭配对比与导出待确认改选草稿。</p></noscript>
<section id="portraits"><div class="section-heading"><span class="section-number">01</span><div><h2>先看人物</h2><p>同样的米白外套、浅蓝上衣和浅灰背景，重点比较面部、发型与整体气质。可点图放大。</p></div></div><div class="candidate-grid" data-kind="image" data-count="{len(images)}" style="--columns:{max(1, min(3, len(images)))}">{image_html}</div><p class="quality-guide">看图时关注：年龄感是否合适、真人感是否自然、面部是否清楚、长期出镜是否耐看。人物身份已确认，MuseTalk 嘴型与人物一致性仍待验证。</p></section>
<section id="voices"><div class="section-heading"><span class="section-number">02</span><div><h2>再听声音</h2><p>同一段 AI 科普稿，使用相同语速、音调与情绪参数。播放一条声音时，其他播放器会暂停。</p></div></div><div class="script"><h3>统一试听稿</h3><p>{escape(plain_speech)}</p><p class="small">〔短暂停顿〕对应 TTS 停顿标记，不朗读这几个字。相同语速参数下，各音色的实际时长仍可能不同。</p></div><div class="candidate-grid" data-kind="voice" data-count="{len(sounds)}" style="--columns:{max(1, min(3, len(sounds)))}">{voice_html}</div><p class="quality-guide">听声音时关注：专业但不生硬、温柔但不幼态，AI / RAG 的读法、停顿是否自然，以及连续听一分钟是否舒服。完整主观试听评价由你决定。</p><p class="time-note">耗时是本轮单次请求的端到端记录，包含网络与等待，不代表稳定吞吐。TTS 按 3.5 元／万计费字符估算，实际账单待核对；{total_cost_note}图片中转价格未知，未套用官方价。</p></section>
<section id="combination"><div class="section-heading"><span class="section-number">03</span><div><h2>试听已选组合与临时对比</h2><p>这里是图片与声音的搭配预览，还没有生成数字人口播视频。</p></div></div><div class="combination"><div class="combination-visual"><p id="combination-empty" hidden>选择人物后，在这里查看搭配。</p><img id="combination-image" src="{escape(selected_image['href'])}" alt="正式已选：{escape(selected_image['label'])}"></div><div class="combination-copy"><h3 id="combination-title">{escape(selected_label)}</h3><p id="selection-status">当前展示正式已选组合；本次仅确认人物与音色，生产验收待完成。</p><audio id="combination-audio" controls preload="metadata" src="{escape(selected_voice['href'])}" aria-label="当前搭配音色试听"></audio><input id="selection-text" class="share-field" aria-label="可复制的对比文字" readonly hidden><div class="selection-actions"><button id="copy-selection" class="action-primary" type="button" disabled>复制当前组合</button><button id="save-selection" class="action-secondary" type="button" disabled>下载改选草稿</button><button id="clear-selection" class="action-secondary" type="button">恢复正式组合</button></div><div id="selection-message" class="selection-message" role="status" aria-live="polite"></div><p class="small">临时对比和下载均不修改正式选择。如需改选，请明确确认新的组合。下载文件始终是待确认改选草稿，不构成批准或生产验收，也不会立即生成视频。</p></div></div></section>
{audit_html}
<footer><p>生图：GPT Image 2 专用中转 · TTS：MiniMax Speech 2.8 HD 官方 API<br>人物与音色已定版：{escape(selected_label)}；正式名字待你确定，生产验收待完成。</p><p>页面生成：{generated_at}<br>保留本 HTML 与人物、音色、执行证据目录，可离线阅读。</p></footer>
</main><dialog id="portrait-modal" class="modal"><div class="modal-header"><h2 id="modal-title">人物原图</h2><a id="modal-file" target="_blank" rel="noopener">打开文件</a><button id="close-modal" type="button">关闭</button></div><img id="modal-image" alt=""></dialog>
<script id="asset-data" type="application/json">{asset_data}</script><script>{JS}</script></body></html>'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只构建与报告候选状态，不写 HTML")
    args = parser.parse_args()
    document = build()
    _, voices = constants()
    status = [{"stage": stage, "available": (c := load_candidate(stage, voices))["available"], "state": c["state"], "issue": c["issue"]} for stage in STAGES]
    if not args.check:
        output = ROOT / f"{TITLE}.html"
        output.write_text(document, encoding="utf-8")
    print(json.dumps({"html_written": not args.check, "characters": len(document), "authoritative_selection": read_json(ROOT / "人物与音色定版.json")["selection"], "candidates": status}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
