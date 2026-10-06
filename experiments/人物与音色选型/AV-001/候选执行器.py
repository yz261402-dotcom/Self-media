"""一次性候选生成：原版 imagegen CLI + MiniMax 官方 TTS，六个固定提交槽。"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent
KEYS=Path(r'C:\Users\22346\Desktop\密钥\api_keys.md')
CLI=Path(r'C:\Users\22346\.codex\skills\.system\imagegen\scripts\image_gen.py')
ALLOWED={'image_A','image_B','image_C','voice_1','voice_2','voice_3'}
VOICES={
 'voice_1':('温柔学姐','Chinese (Mandarin)_Gentle_Senior'),
 'voice_2':('淡雅学姐','danya_xuejie'),
 'voice_3':('温暖闺蜜','Chinese (Mandarin)_Warm_Bestie'),
}
SPEECH='你有没有发现，AI回答得很流畅，却不一定说得对。<#0.30#>RAG，也就是检索增强生成，会先找资料，再组织答案。今天用三个步骤，带你看懂它。资料是否可靠，仍然需要检查。'
PROMPT_BASE='''用途：为长期中文 AI 教学和科普短视频创建基础虚拟主播候选，真实摄影感的人物资产。
画面中只有一位完全虚构、明确成年的中国年轻女性，约22岁，专业、温柔、有可信赖的知识分享者气质。年龄感年轻但不是儿童或未成年人，不模仿任何现实公众人物。
真实摄影棚人像，真实而细腻的皮肤微纹理，正常五官结构和人体比例，自然黑色瞳孔，清晰对称的眼睛，淡妆，无重滤镜、无美颜磨皮、无网红夸张尖下巴。
统一穿搭：剪裁简洁的米白色轻薄西装外套，内搭浅雾蓝色圆领上衣，领口端庄；不戴眼镜，不戴显眼耳饰。统一浅灰色纯净摄影棚背景，无设备或杂物，柔和均匀的正面主光与很轻的自然阴影。
构图：9:16竖幅，单人正面半身，胸部至腰部上方构图，身体正对镜头，头部端正不歪斜，眼睛睁开直视镜头，亲切轻微微笑但嘴唇自然完全闭合、无露齿。完整头顶、头发和双肩入镜，头顶留适量空间，人脸占比足够用于数字人口播。手和手臂下部在画面外。
禁止卡通、插画、动漫、3D塑料皮肤、抽象人物、拼贴分镜、多个人、文字、标志、字幕。输出一张完整真实外观的人物照片。
本候选的固定人物特征：'''
PROMPTS={
 'image_A':PROMPT_BASE+'柔和鹅蛋脸，颧骨和下颌线自然柔和，眉形平直略有弧度，眼型清澈自然；黑色齐肩顺直短中发，三七侧分，发梢轻微内收，两侧头发不遮眼睛、脸颊和嘴唇。气质温柔知性、干净亲和。',
 'image_B':PROMPT_BASE+'自然圆椭圆脸，脸颊有适度年轻饱满感，比例正常、眼睛不过大；黑色长直发到锁骨下，中偏侧分，头发整洁放于肩后，脸颊和嘴唇无遮挡。气质清晰自然、温暖有活力，同时保持专业克制。',
 'image_C':PROMPT_BASE+'线条清晰但柔和的椭圆脸，自然眉骨与鼻梁，友善明亮的眼神；黑色头发松紧适中的低马尾，六四侧分，无碎发遮挡面部，两侧鬓发整洁。气质专注温柔、清爽利落，适合耐心讲解技术知识。',
}


def now():return datetime.now(timezone.utc).isoformat()
def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def error_diagnostic(error):
    chain=[];seen=set();current=error
    while current is not None and id(current) not in seen and len(chain)<8:
        seen.add(id(current))
        message=re.sub(r'\bsk-[A-Za-z0-9_-]+','[REDACTED]',str(current))
        message=re.sub(r'(?i)Bearer\s+[^\s\"\x27]+','Bearer [REDACTED]',message)
        message=re.sub(r'(https?://[^\s?]+)\?[^\s]+',r'\1?[REDACTED]',message)
        chain.append({'type':type(current).__name__,'message':message[:800]})
        current=current.__cause__ or current.__context__
    return chain


def once(root,stage,request,call):
    if stage not in ALLOWED:raise ValueError('stage outside fixed candidate scope')
    directory=Path(root)/'执行证据'/stage;directory.mkdir(parents=True,exist_ok=True)
    record={'stage':stage,'started_at':now(),'request':request,'request_sha256':hashlib.sha256(json.dumps(request,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),'automatic_retry':False}
    with (directory/'attempt.started.json').open('x',encoding='utf-8') as file:
        json.dump(record,file,ensure_ascii=False,indent=2)
    started=time.monotonic()
    try:
        result=call()
    except BaseException as error:
        write(directory/'执行结果.json',{'stage':stage,'state':'UNKNOWN_OR_FAILED','error_type':type(error).__name__,'error_chain':error_diagnostic(error),'elapsed_seconds':time.monotonic()-started,'finished_at':now(),'automatic_retry':False})
        raise
    record={'stage':stage,'state':'SUCCEEDED','elapsed_seconds':time.monotonic()-started,'finished_at':now(),'result':result,'automatic_retry':False}
    write(directory/'执行结果.json',record)
    return record


def key(label,env):
    if os.environ.get(env):return os.environ[env]
    lines=KEYS.read_text(encoding='utf-8-sig').splitlines()
    matches=[x for x in lines if re.search(label,x,re.I)]
    if len(matches)!=1:raise ValueError('credential label ambiguous or absent')
    values=re.findall(r'\bsk-[A-Za-z0-9_-]+',matches[0])
    if len(values)!=1:raise ValueError('credential not configured')
    return values[0]


def generate_image(stage):
    import openai,httpx
    from PIL import Image
    directory=ROOT/'执行证据'/stage
    output=ROOT/'人物'/f'人物{stage[-1]}.png'
    if output.exists():raise FileExistsError('candidate output already exists')
    prompt=ROOT/'提示词'/f'人物{stage[-1]}.txt';prompt.parent.mkdir(parents=True,exist_ok=True);prompt.write_text(PROMPTS[stage],encoding='utf-8')
    payload={'model':'gpt-image-2','prompt':PROMPTS[stage],'size':'1152x2048','quality':'medium','n':1,'output_format':'png'}
    api_key=key('gpt-image','AVATAR_STUDIO_IMAGE_API_KEY')
    def call():
        def request_hook(request):
            def trace(event,info):
                with (directory/'连接事件.jsonl').open('a',encoding='utf-8') as f:
                    f.write(json.dumps({'at':now(),'event':event},ensure_ascii=False)+'\n')
            request.extensions['trace']=trace
        def response_hook(response):
            response.read()
            (directory/'响应.private.json').write_bytes(response.content)
            meta={'http_status':response.status_code,'headers':{k:v for k,v in response.headers.items() if k.lower() in ['x-request-id','request-id','content-type']}}
            try:
                data=response.json()
                meta.update({'model':data.get('model'),'created':data.get('created'),'usage':data.get('usage'),'data_count':len(data.get('data',[]))})
            except ValueError:meta['json_response']=False
            write(directory/'响应摘要.json',meta)
        sdk_class=openai.OpenAI
        # 原技能文件不修改。依赖注入的客户端只关闭SDK自动重试并保留原始响应。
        with httpx.Client(event_hooks={'request':[request_hook],'response':[response_hook]},timeout=httpx.Timeout(600,connect=20)) as transport:
            with sdk_class(api_key=api_key,base_url='https://airelay.dog.airelay.buzz/v1',max_retries=0,http_client=transport) as client:
                spec=importlib.util.spec_from_file_location('unmodified_imagegen_cli',CLI)
                cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
                arguments=[str(CLI),'generate','--model','gpt-image-2','--prompt-file',str(prompt),'--size','1152x2048','--quality','medium','--n','1','--output-format','png','--out',str(output),'--no-augment']
                buffer=io.StringIO()
                with patch.object(openai,'OpenAI',return_value=client),patch.object(sys,'argv',arguments),patch.dict(os.environ,{'OPENAI_API_KEY':api_key}),redirect_stdout(buffer),redirect_stderr(buffer):
                    cli.main()
                (directory/'工具日志.txt').write_text(buffer.getvalue(),encoding='utf-8')
        with Image.open(output) as im:
            width,height=im.size;im.verify()
        return {'path':str(output.relative_to(ROOT)),'sha256':sha(output),'width':width,'height':height,'model':'gpt-image-2','upstream_identity':'relay_reported_not_independently_verified','quality':'CANDIDATE_NOT_APPROVED'}
    return once(ROOT,stage,payload,call)


def tts_audio(status,data):
    if status!=200 or (data.get('base_resp') or {}).get('status_code')!=0:raise ValueError('TTS not successful')
    audio=(data.get('data') or {}).get('audio')
    if not isinstance(audio,str) or not audio or not re.fullmatch('[0-9A-Fa-f]+',audio):raise ValueError('missing hex audio')
    return bytes.fromhex(audio)


def generate_voice(stage):
    import requests
    name,voice_id=VOICES[stage]
    catalogue=json.loads((ROOT/'官方音色目录.json').read_text(encoding='utf-8'))['system_voice']
    if not any(x['voice_id']==voice_id for x in catalogue):raise ValueError('voice absent from verified catalogue')
    output=ROOT/'音色'/f'{stage[-1]}-{name}.wav'
    if output.exists():raise FileExistsError('voice output already exists')
    payload={'model':'speech-2.8-hd','text':SPEECH,'stream':False,'output_format':'hex','language_boost':'Chinese','voice_setting':{'voice_id':voice_id,'speed':1.0,'vol':1.0,'pitch':0,'emotion':'calm'},'audio_setting':{'sample_rate':32000,'format':'wav','channel':1},'subtitle_enable':False}
    api_key=key('minmax','AVATAR_STUDIO_MINIMAX_API_KEY')
    def call():
        r=requests.post('https://api.minimax.cn/v1/t2a_v2',headers={'Authorization':'Bearer '+api_key,'Content-Type':'application/json'},json=payload,timeout=(20,180),allow_redirects=False)
        directory=ROOT/'执行证据'/stage
        (directory/'响应.private.json').write_bytes(r.content)
        data=r.json()
        summary={'http_status':r.status_code,'base_resp':data.get('base_resp'),'trace_id':data.get('trace_id'),'extra_info':data.get('extra_info'),'model':'speech-2.8-hd'}
        write(directory/'响应摘要.json',summary)
        audio=tts_audio(r.status_code,data)
        output.parent.mkdir(parents=True,exist_ok=True)
        with output.open('xb') as file:file.write(audio)
        probe=json.loads(subprocess.run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(output)],check=True,capture_output=True).stdout)
        decoded=subprocess.run(['ffmpeg','-v','error','-xerror','-i',str(output),'-f','null','-'],capture_output=True,timeout=45)
        if decoded.returncode or decoded.stderr:raise ValueError('audio decode failed')
        usage=(data.get('extra_info') or {}).get('usage_characters')
        result={'path':str(output.relative_to(ROOT)),'sha256':sha(output),'name':name,'voice_id':voice_id,'seconds':float(probe['format']['duration']),'full_decode':'PASS','ffprobe':probe,'usage_characters':usage,'estimated_cny':usage*3.5/10000 if isinstance(usage,(int,float)) else None,'actual_cost_cny':None,'quality':'CANDIDATE_NOT_APPROVED'}
        write(directory/'音频测量.json',result)
        return result
    return once(ROOT,stage,payload,call)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('stage',choices=sorted(ALLOWED));args=parser.parse_args()
    try:
        result=generate_image(args.stage) if args.stage.startswith('image') else generate_voice(args.stage)
        summary={k:v for k,v in result.items() if k!='result'}
        summary['asset']={k:v for k,v in result['result'].items() if k!='ffprobe'}
        print(json.dumps(summary,ensure_ascii=False))
    except BaseException as error:
        if isinstance(error,KeyboardInterrupt):raise
        print(json.dumps({'stage':args.stage,'state':'STOPPED','error_type':type(error).__name__,'automatic_retry':False},ensure_ascii=False))
        raise SystemExit(2)
