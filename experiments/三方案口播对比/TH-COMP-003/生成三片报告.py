"""从真实媒体与执行证据生成三方案报告；不提交模型任务，不修改历史实验。"""
from __future__ import annotations

from datetime import datetime, timezone
from fractions import Fraction
import html
import importlib.util
import json
import math
from pathlib import Path
import re
import shutil
import sys

from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parents[2]
PREVIOUS = ROOT.parent / 'TH-COMP-002'
ALI = PROJECT / 'experiments/数字人口播选型'
MT = PROJECT / 'experiments/MuseTalk部署/MT-001'


def load_previous():
    """仅导入可复用定义，避免在 002 下写入字节码或执行旧 main。"""
    spec = importlib.util.spec_from_file_location('comparison_002_readonly', PREVIOUS / '生成对比报告.py')
    module = importlib.util.module_from_spec(spec)
    previous_flag = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous_flag
    return module


def copy_verified(source, name, sha):
    target = ROOT / '素材' / name
    target.parent.mkdir(exist_ok=True)
    if target.exists():
        assert sha(target) == sha(source), f'拒绝覆盖不同素材：{name}'
    else:
        with source.open('rb') as reader, target.open('xb') as writer:
            shutil.copyfileobj(reader, writer)
    assert sha(target) == sha(source), f'复制摘要不一致：{name}'
    return target


def content(tag, markup):
    tag.clear()
    fragment = BeautifulSoup(f'<div>{markup}</div>', 'html.parser').div
    for node in list(fragment.contents):
        tag.append(node)


def number(value, places=3):
    return f'{float(value):.{places}f}'.rstrip('0').rstrip('.')


def main():
    old = load_previous()
    read, sha = old.read, old.sha
    result = read(ROOT / '生成结果.json')
    measured = read(ROOT / '火山媒体测量.json')
    quality = read(ROOT / '火山质量观察.json')
    assert result['task_id'] and result['request_id'], '需要真实生成任务与请求编号'
    assert result['actual_cost_cny'] is None, '报告当前仅处理未对账结果'
    assert quality['full_audiovisual_review'] == 'NOT_REVIEWED', '人工验收状态改变时请同步报告逻辑'
    wave = measured['audio_waveform']
    for key in ('best_lag_ms', 'overlap_correlation', 'reference_start_uncovered_ms', 'reference_end_uncovered_ms'):
        assert math.isfinite(float(wave[key])), f'缺少有效音轨测量：{key}'
    sources = {
        '阿里万相原片.mp4': ALI / 'runs/TH-AB-001/B/原片.mp4',
        'MuseTalk单图原片.mp4': MT / '单图口播原片.mp4',
        '火山OmniHuman原片.mp4': ROOT / '火山口播原片.mp4',
        '共用人物.png': ALI / 'inputs/测试人物.png',
        '共用口播.wav': ALI / 'inputs/测试口播.wav',
        '阿里预览.jpg': PREVIOUS / '素材/阿里预览.jpg',
        'MuseTalk预览.jpg': MT / '单图关键帧-2.jpg',
        '火山预览.jpg': ROOT / '火山预览.jpg',
        '阿里关键帧对照.jpg': ALI / 'runs/TH-AB-001/B/参考图与关键帧.jpg',
        'MuseTalk关键帧对照.jpg': MT / '单图关键帧对照.jpg',
        '火山关键帧对照.jpg': ROOT / '火山关键帧对照.jpg',
    }
    for path in sources.values():
        assert path.is_file(), f'缺少真实素材：{path.name}'
    assert sha(sources['共用人物.png']) == '6531d41f561927836d496c169e8c53751c93243aad9fb0d8e624ad4c73e4f135'
    assert sha(sources['共用口播.wav']) == '3aa5637692cfb333e0594d949bae2522cd44f698fe40c87718c96163408411ba'
    copies = {name: copy_verified(path, name, sha) for name, path in sources.items()}
    a, b, c = (old.measure(copies[name]) for name in ('阿里万相原片.mp4', 'MuseTalk单图原片.mp4', '火山OmniHuman原片.mp4'))
    assert a['sha256'] == '29e8af401d8f4701a01cf50c35d526b7ff13a159715d4d9e6627dfc05c0c2fd0'
    assert b['sha256'] == read(MT / '单图媒体测量.json')['sha256']
    for key in ('sha256', 'width', 'height', 'fps', 'frames', 'video_seconds', 'audio_seconds', 'full_decode'):
        assert c[key] == measured[key], f'火山媒体与提供的测量不一致：{key}'
    response = read(ALI / 'runs/TH-AB-001/aliyun_video-query-20260921T084430324285-response.json')
    deployment = read(MT / '部署记录.json')
    parse = lambda value: datetime.strptime(value, '%Y-%m-%d %H:%M:%S.%f')
    ali_seconds = (parse(response['output']['end_time']) - parse(response['output']['submit_time'])).total_seconds()
    mt_seconds = deployment['m1']['wall_seconds']
    ali_cost = response['usage']['duration'] * 0.9
    mt_cost = mt_seconds / 3600 * deployment['hourly_cny_user_reported']
    volc_seconds = float(result['observed_submit_to_success_seconds'])
    uncertainty = float(result['poll_uncertainty_seconds'])
    volc_cost = c['video_seconds'] * 1.0
    assert volc_seconds > 0 and 0 <= uncertainty <= volc_seconds
    assert math.isclose(float(result['estimated_video_cny']), volc_cost, abs_tol=0.01), '估算须使用输出视频时长 × 1 元/s'
    data = {
        'measured_at': datetime.now(timezone.utc).isoformat(),
        'experiment': 'TH-COMP-003',
        'scope': 'same original image and 9.36 second TTS; reuse Ali and MuseTalk baselines; one new successful Volc generation',
        'eligible_videos': 3, 'compared_providers': 3, 'human_full_reviews': 0, 'selection': 'UNSELECTED',
        'aliyun': {**a, 'generation_seconds': ali_seconds, 'timing_scope': 'provider_submit_to_end', 'estimated_video_cny': ali_cost},
        'musetalk': {**b, 'generation_seconds': mt_seconds, 'timing_scope': 'whole_inference_process', 'estimated_active_gpu_cny': mt_cost},
        'volcengine': {
            **c, 'task_id': result['task_id'], 'request_id': result['request_id'],
            'observed_submit_to_success_seconds': volc_seconds,
            'timing_scope': 'client_submission_to_first_successful_poll_excludes_download',
            'poll_uncertainty_seconds': uncertainty, 'estimated_video_cny': volc_cost,
            'pricing_basis': result['pricing_basis'],
            'estimate_formula': 'measured output video seconds * public reference rate 1 CNY/second; no billed usage field',
            'actual_cost_cny': None, 'audio_waveform': wave, 'quality_observation': quality,
        },
        'actual_bill_verified': False,
        'shared_asset_estimated_cny': 0.2 + 79 * 0.8 / 10000,
        'new_calls': {'image': 0, 'tts': 0, 'aliyun_video': 0, 'musetalk': 0, 'volc_video': 1},
        'historical_failure_evidence': '../TH-COMP-002/补测结果.json',
    }
    raw = old.TEMPLATE.replace('@@ALI_SECONDS@@', f'{ali_seconds:.2f}').replace('@@MT_SECONDS@@', str(mt_seconds))
    raw = raw.replace('@@ALI_COST@@', f'{ali_cost:.3f}').replace('@@MT_COST@@', f'{mt_cost:.5f}')
    soup = BeautifulSoup(raw, 'html.parser')
    soup.select_one('nav span').string = f'{datetime.now().astimezone():%Y.%m.%d} · TH-COMP-003 · 同输入对照'
    content(soup.select_one('.status'), '<strong>3 条真实原片已取得，可直接并排比较。</strong> 开通 OmniHuman1.5 后，本轮单次提交成功；阿里、MuseTalk 沿用前次同图同音频原片。生产选型未定，完整人类视听评审尚未进行。')
    esc = lambda value: html.escape(str(value))
    identity, motion, detail = (esc(quality[key]) for key in ('identity_observation', 'motion_observation', 'detail_observation'))
    third = soup.select('.sample')[2]
    content(third, f'''<div class="serial">03</div><h3>火山 / 字节数字人</h3><p class="sub">OmniHuman1.5 · 720P 请求 · 本轮真实输出</p>
<video class="screen" controls playsinline preload="metadata" poster="素材/火山预览.jpg" src="素材/火山OmniHuman原片.mp4"></video>
<div class="metrics"><div><strong>{volc_seconds:.2f} s</strong><span>客户端提交至首个成功查询</span></div><div><strong>¥{volc_cost:.3f}</strong><span>输出时长估算 / 非账单</span></div></div>
<p class="sample-note">{motion} <a href="素材/火山OmniHuman原片.mp4">打开原片</a></p>''')
    video_stream = next(stream for stream in c['ffprobe']['streams'] if stream['codec_type'] == 'video')
    audio_stream = next(stream for stream in c['ffprobe']['streams'] if stream['codec_type'] == 'audio')
    specs = f"{c['width']}×{c['height']} · {number(float(Fraction(c['fps'])))} fps<br>{esc(video_stream['codec_name'].upper())} / {esc(audio_stream['codec_name'].upper())} · {c['frames']} 帧"
    values = {
        '视频生成结果': '原片已取得 / 可完整解码<small>本轮生成成功，旧鉴权失败证据单独保留。</small>',
        '输出规格': specs,
        '视频轨 / 音频轨时长': f"{number(c['video_seconds'], 6)} s / {number(c['audio_seconds'], 6)} s",
        '生成耗时': f'{volc_seconds:.2f} s<small>客户端提交→首个成功查询；不含下载。轮询观测不确定区间约 {number(uncertainty)} s，不等于服务端精确生成时长。</small>',
        '视频阶段费用估算': f'¥{volc_cost:.3f}<small>输出视频轨 {number(c["video_seconds"], 6)} s × 公开参考价 1 元/s。响应无计费用量字段，实际计费规则、优惠及账单待核。</small>',
        '本轮新增动作': '开通后一次生成提交，成功出片<small>无自动重提；查询同一任务属于取结果，未再次提交生成。</small>',
        '部署 / 常驻成本': '接口侧按用量计费；实际价格、额度、存储/流量及账单待核对',
        '实际账单': '尚未对账；输出时长估算不等于实际扣费',
        '显存 / 人脸细节': f'云服务内部显存未知；{detail}',
        '人物身份观察': identity,
        '动作自然度': motion,
        '音轨保持': f'最佳偏移 {number(wave["best_lag_ms"])} ms；重叠区相关 {float(wave["overlap_correlation"]):.6f}<small>参考音频首部未覆盖 {number(wave["reference_start_uncovered_ms"])} ms，尾部未覆盖 {number(wave["reference_end_uncovered_ms"])} ms。仅音轨测量，不是口型同步误差。</small>',
        '精确口型 / 牙齿伪影': f'完整逐音素视听未评<small>{esc(quality["review_scope"])}</small>',
        '长期生产验收': '未通过：缺长稿、多次、服装/背景变体与完整视听验收',
    }
    for row in soup.select('tbody tr'):
        name = row.find('th').get_text(strip=True)
        if name in values:
            content(row.find_all('td')[-1], values.pop(name))
    assert not values, f'表格行名发生变化：{list(values)}'
    frames = soup.select_one('.frames')
    content_frame = BeautifulSoup('<figure><img src="素材/火山关键帧对照.jpg" alt="火山 OmniHuman 原始人物图与真实输出关键帧"><figcaption class="note">火山：真实输出关键帧，与原始人物图同屏观察；静帧不能替代完整视听。</figcaption></figure>', 'html.parser')
    frames.append(content_frame.figure)
    soup.style.append('\n.frames{grid-template-columns:repeat(3,minmax(0,1fr))}@media(max-width:850px){.frames{grid-template-columns:1fr}}\n')
    content(soup.select('.decision > div')[1], f'''<h3>三方案出片，生产选型继续保留</h3><p>阿里需确认首字和完整画质，MuseTalk 需补自然动作与长片，火山已解除本次调用阻塞，仍需完整视听、长稿及身份变体测试。三条短片足以开始效果对照，尚不足以确定长期生产模型。</p><p class="note">本轮火山任务：<code>{esc(result['task_id'])}</code>；请求：<code>{esc(result['request_id'])}</code>。<a href="../TH-COMP-002/三方案数字人口播对比报告.html">查看开通前的失败报告</a>。</p>''')
    content(soup.select_one('.evidence'), '''<li><a href="对比数据.json">本报告机器可读数据与三条媒体测量</a></li>
<li><a href="生成结果.json">本轮火山生成结果与计时</a> · <a href="提交请求.json">提交请求（脱敏）</a> · <a href="提交响应.json">提交响应</a></li>
<li><a href="火山媒体测量.json">火山输出规格、摘要和音轨测量</a> · <a href="火山质量观察.json">火山关键帧质量观察</a></li>
<li><a href="轮询时间线.json">火山轮询时间线</a> · <a href="ledger.json">独立预留台账（非账单）</a></li>
<li><a href="../TH-COMP-002/补测结果.json">上轮鉴权失败记录（保留原始证据）</a></li>
<li><a href="../../数字人口播选型/runs/TH-AB-001/aliyun_video-query-20260921T084430324285-response.json">阿里服务时间与计费用量回执</a></li>
<li><a href="../../数字人口播选型/runs/TH-AB-001/B/补充测量.json">阿里音轨与帧时间戳</a></li>
<li><a href="../../MuseTalk部署/MT-001/部署记录.json">MuseTalk 计时、显存与费用来源</a></li>
<li><a href="../../MuseTalk部署/MT-001/单图音轨核验.json">MuseTalk 音轨保持检查</a></li>
<li><a href="../../../specs/selection/01-数字人口播/04-MuseTalk部署与首测结果.html">MuseTalk 完整部署报告</a></li>
<li><a href="../../../specs/selection/01-数字人口播/火山候选核验.html">OmniHuman1.5 接口与参考价格核验</a></li>
<li><a href="../../../specs/0001-douyin-ai-studio/08-端到端技术流程与模型选型地图.html">完整流程与模型选型清单</a></li>''')
    content(soup.footer, '本页无需联网脚本或字体。保留本 HTML 与同级“素材”文件夹即可离线播放；原始证据链接需要保留工作区目录。本轮未新建人物、未重新生成语音、未向抖音发布，未启动 MuseTalk GPU 推理。阿里、MuseTalk 基线复用，火山为开通服务后的新生成任务。')
    script = soup.find('script')
    script.string = script.string.replace('两条原片从片头同时静音播放', '三条原片从片头同时静音播放')
    output = str(soup)
    assert not re.search(r'@@[A-Z_]+@@', output), '存在未替换的模板变量'
    assert len(soup.select('video')) == 3, '必须提供三条真实播放器'
    assert '火山方案仍被权限阻塞' not in output and '权限校验失败，未生成视频' not in output
    for node in soup.select('[href], [src]'):
        link = node.get('href') or node.get('src')
        if not link or link.startswith(('http:', 'https:', 'data:', '#')):
            continue
        if link in ('对比数据.json', '三方案数字人口播对比报告.html'):
            continue
        assert (ROOT / link.split('#', 1)[0]).is_file(), f'本地链接失效：{link}'
    (ROOT / '对比数据.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    (ROOT / '三方案数字人口播对比报告.html').write_text(output, encoding='utf-8')
    print(json.dumps({'html': '三方案数字人口播对比报告.html', 'eligible_videos': 3, 'full_decode': [a['full_decode'], b['full_decode'], c['full_decode']], 'selection': 'UNSELECTED'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
