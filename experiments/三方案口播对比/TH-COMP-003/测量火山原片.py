"""离线测量原片与参考音频，不调用模型、不修改输入或原片。"""
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import subprocess

import numpy as np
from scipy.signal import correlate, correlation_lags
from PIL import Image, ImageOps, ImageDraw

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('old_report', ROOT.parent / 'TH-COMP-002/生成对比报告.py')
old = importlib.util.module_from_spec(spec)
spec.loader.exec_module(old)


def decode_audio(path):
    result = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-vn', '-ac', '1', '-ar', '24000', '-f', 'f32le', '-'], check=True, capture_output=True)
    return np.frombuffer(result.stdout, dtype='<f4').astype(np.float64)


def rms(array):
    return float(20 * np.log10(max(float(np.sqrt(np.mean(array ** 2))), 1e-12))) if len(array) else None


def main():
    video = ROOT / '火山口播原片.mp4'
    data = old.measure(video)
    src = decode_audio(old.ALI / 'inputs/测试口播.wav')
    dst = decode_audio(video)
    corr = correlate(dst, src, method='fft')
    lags = correlation_lags(len(dst), len(src))
    lag = int(lags[np.argmax(corr)])
    ref_start, out_start = max(0, -lag), max(0, lag)
    n = min(len(src) - ref_start, len(dst) - out_start)
    assert n > 0
    pearson = float(np.corrcoef(src[ref_start:ref_start+n], dst[out_start:out_start+n])[0, 1])
    data['audio_waveform'] = {
        'method': 'ffmpeg mono24k float PCM; scipy full correlation(output,reference), Pearson on overlap; NOT lip sync',
        'reference_samples': len(src), 'output_samples': len(dst),
        'best_lag_samples': lag, 'best_lag_ms': lag / 24,
        'overlap_correlation': pearson,
        'reference_start_uncovered_ms': ref_start / 24,
        'reference_end_uncovered_ms': (len(src)-ref_start-n)/24,
        'reference_start_uncovered_rms_dbfs': rms(src[:ref_start]),
        'reference_end_uncovered_rms_dbfs': rms(src[ref_start+n:]),
        'subjective_audio': 'NOT_REVIEWED',
    }
    timestamps = json.loads(subprocess.run(['ffprobe','-v','error','-select_streams','v:0','-show_frames','-show_entries','frame=best_effort_timestamp_time','-of','json',str(video)],check=True,capture_output=True).stdout)
    pts = [float(x['best_effort_timestamp_time']) for x in timestamps['frames']]
    deltas = np.diff(pts)
    data['frame_timestamps'] = {'count':len(pts),'first':pts[0],'last':pts[-1],'min_interval_seconds':float(deltas.min()),'max_interval_seconds':float(deltas.max())}
    (ROOT/'帧时间戳.json').write_text(json.dumps(timestamps,ensure_ascii=False,indent=2),encoding='utf-8')
    frames=[]
    for i,fraction in enumerate([.1,.3,.5,.7,.9],1):
        at = data['video_seconds'] * fraction
        path = ROOT/f'火山关键帧-{i}.jpg'
        if not path.exists():
            subprocess.run(['ffmpeg','-v','error','-ss',str(at),'-i',str(video),'-frames:v','1','-q:v','2',str(path)],check=True)
        frames.append({'at_seconds':at,'file':path.name})
    data['keyframes']=frames
    reference=old.ALI/'inputs/测试人物.png'
    sheet=Image.new('RGB',(6*240,470),'white')
    draw=ImageDraw.Draw(sheet)
    for i,path in enumerate([reference]+[ROOT/x['file'] for x in frames]):
        with Image.open(path) as im:
            thumbnail=ImageOps.contain(im.convert('RGB'),(232,420))
            sheet.paste(thumbnail,(i*240+(240-thumbnail.width)//2,35))
        draw.text((i*240+12,8),'Reference' if i==0 else f"{frames[i-1]['at_seconds']:.3f}s",fill='black')
    sheet.save(ROOT/'火山关键帧对照.jpg',quality=93)
    old.shutil.copyfile(ROOT/'火山关键帧-3.jpg',ROOT/'火山预览.jpg')
    data['measured_at']=datetime.now(timezone.utc).isoformat()
    data['manual_review']='NOT_REVIEWED'
    (ROOT/'火山媒体测量.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in data.items() if k not in ['ffprobe']},ensure_ascii=False))


if __name__=='__main__':
    main()
