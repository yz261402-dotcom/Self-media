"""Existing Wan masters gain a separately reviewed subtitle version."""
import copy
import hashlib
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import delivery, jobs, storage as st
from app.main import app
from tests.short_video.test_delivery import stitched, _confirm
from tests.short_video.test_storyboard import prepared


def submit(service, graph, **changes):
    return service.queue_subtitles(graph['id'], actor_id='user_local', compute_ack=True,
        expected_video_id=graph['video_id'], output_digest=graph['output']['file_hash'],
        request_id=changes.pop('request_id', 'subtitle-request'), **changes)


def renderer(root, calls):
    def render(graph, *, job_id):
        calls.append(job_id)
        folder = root / 'production/backend/runtime/wan_storyboards' / graph['id'] / 'subtitles' / job_id
        folder.mkdir(parents=True)
        path = folder / 'captioned.mp4'
        source = root / graph['output']['storage_uri']
        path.write_bytes(source.read_bytes())
        files=[]
        for kind, name, data in [('subtitles_ass','captions.ass', '[Script Info]\n[Events]\n'),
                                  ('subtitles_srt','captions.srt', '1\n00:00:00,000 --> 00:00:01,000\n实际对白\n')]:
            subtitle=folder/name; subtitle.write_text(data, encoding='utf-8')
            files.append(dict(kind=kind,storage_uri=subtitle.relative_to(root).as_posix(),
                file_hash='sha256:'+hashlib.sha256(subtitle.read_bytes()).hexdigest(),bytes=subtitle.stat().st_size))
        return {**graph['output'], 'storage_uri':path.relative_to(root).as_posix(),
                'file_hash':'sha256:'+hashlib.sha256(path.read_bytes()).hexdigest(),
                'subtitles_burned':True, 'subtitle_source':{'video_id':graph['video_id'],'file_hash':graph['output']['file_hash']},
                'subtitle_files':files, 'subtitle_checks':{'outcome':'pass','alignment_source':'forced_alignment',
                    'transcript':'实际对白','cue_count':1,'source_audio_hash':'sha256:audio',
                    'source_audio_packet_hash':'sha256:packets','output_audio_packet_hash':'sha256:packets'}}
    return render


@pytest.mark.parametrize('field,value',[('actor_id','agent'),('compute_ack',False),('request_id',''),
    ('expected_video_id','old'),('output_digest','old')])
def test_subtitle_submission_requires_current_user_and_output(stitched, field, value):
    service,graph,_=stitched
    kwargs=dict(actor_id='user_local',compute_ack=True,expected_video_id=graph['video_id'],
                output_digest=graph['output']['file_hash'],request_id='r')
    kwargs[field]=value
    with pytest.raises(service.StoryboardError): service.queue_subtitles(graph['id'],**kwargs)
    assert not [j for j in st.all_rows('provider_jobs','work_id=?',[graph['work_id']]) if j['capability']=='storyboard_subtitles']


def test_caption_queue_is_durable_idempotent_and_preserves_confirmed_original(stitched):
    service,graph,root=stitched
    original=_confirm(service,graph); persisted=service.get_graph(graph['id'])
    queued=submit(service,graph)
    assert queued['subtitles']['state']=='queued'
    assert queued['video_id']==graph['video_id'] and queued['output']==graph['output']
    assert queued['package_id']==original['package']['id']
    assert submit(service,graph)['subtitles']['job_id']==queued['subtitles']['job_id']
    job=jobs.get_job(queued['subtitles']['job_id'])
    assert job['state']=='queued' and job['billable'] is False
    assert service.get_graph(graph['id'])['shots']==persisted['shots']
    with pytest.raises(service.StoryboardError): submit(service,graph,request_id='other')


def test_queue_http_and_cross_work_are_bound(stitched):
    _,graph,_=stitched
    client=TestClient(app)
    body=dict(actor_id='user_local',compute_ack=True,expected_video_id=graph['video_id'],
              output_digest=graph['output']['file_hash'],request_id='http')
    path=f"/api/v1/works/{graph['work_id']}/wan/storyboards/{graph['id']}/subtitles"
    r=client.post(path,json=body)
    assert r.status_code==200 and r.json()['subtitles']['state']=='queued'
    wrong=client.post(path.replace(graph['work_id'],'missing'),json=body)
    assert wrong.status_code==404


def test_success_preserves_original_and_requires_new_review_and_subtitle_package(stitched):
    from app.short_video import subtitle_jobs
    service,graph,root=stitched
    original=_confirm(service,graph); old_bytes=(root/graph['output']['storage_uri']).read_bytes()
    queued=submit(service,graph); calls=[]
    updated=subtitle_jobs.run(graph['id'],render=renderer(root,calls))
    assert len(calls)==1 and updated['subtitles']['state']=='succeeded'
    assert updated['output']['subtitles_burned'] is True and updated['video_id']!=graph['video_id']
    assert updated.get('package_id') is None
    assert st.get('videos',updated['video_id'])['approval_state']=='pending'
    assert (root/graph['output']['storage_uri']).read_bytes()==old_bytes
    assert st.get('packages',original['package']['id']) is not None
    assert delivery.current_package(graph['work_id']) is None
    assert subtitle_jobs.run(graph['id'],render=renderer(root,calls))['video_id']==updated['video_id']
    assert len(calls)==1
    with pytest.raises(service.StoryboardError): _confirm(service,graph)
    result=_confirm(service,updated)
    assert result['package']['id']!=original['package']['id']
    with zipfile.ZipFile(delivery.download_path(result['package']['id'])) as bundle:
        assert {'字幕.ass','字幕.srt','成片.mp4'}<=set(bundle.namelist())
    assert jobs.get_job(queued['subtitles']['job_id'])['state']=='succeeded'


def test_failure_preserves_previous_package_and_exposes_real_error(stitched):
    from app.short_video import subtitle_jobs
    service,graph,root=stitched
    original=_confirm(service,graph); submit(service,graph)
    def fail(*args,**kwargs): raise service.StoryboardError('ASR_NOT_CONFIGURED','字幕服务未连接')
    updated=subtitle_jobs.run(graph['id'],render=fail)
    assert updated['subtitles']['state']=='failed' and updated['subtitles']['error']['code']=='ASR_NOT_CONFIGURED'
    assert updated['video_id']==graph['video_id'] and updated['output']==graph['output']
    assert updated['package_id']==original['package']['id']
    assert updated['subtitle_generation_available']
    next_=submit(service,graph,request_id='retry')
    assert next_['subtitles']['job_id']!=updated['subtitles']['job_id']


def test_stale_worker_cannot_replace_changed_current_script(stitched):
    from app.short_video import subtitle_jobs
    service,graph,root=stitched
    submit(service,graph)
    render=renderer(root,[])
    def change(g,*,job_id):
        output=render(g,job_id=job_id)
        work=st.get('works',graph['work_id']);work['content_revision']+=1;st.put('works',work['id'],work)
        return output
    subtitle_jobs.run(graph['id'],render=change)
    saved=service.get_graph(graph['id'])
    assert saved['video_id']==graph['video_id'] and saved['output']==graph['output']
    assert saved['subtitles']['state']=='failed'


def test_worker_claim_executes_only_once(stitched):
    from app.short_video import subtitle_jobs
    service,graph,root=stitched
    submit(service,graph);started=threading.Event();release=threading.Event();calls=[]
    render=renderer(root,calls)
    def paused(g,*,job_id): started.set();release.wait(10);return render(g,job_id=job_id)
    with ThreadPoolExecutor(2) as pool:
        running=pool.submit(subtitle_jobs.run,graph['id'],render=paused)
        assert started.wait(5)
        try:
            second=subtitle_jobs.run(graph['id'],render=paused)
            assert second['subtitles']['state']=='running'
        finally: release.set()
        assert running.result(timeout=10)['subtitles']['state']=='succeeded'
    assert len(calls)==1


def test_subtitle_file_change_blocks_new_video_confirmation(stitched):
    from app.short_video import subtitle_jobs
    service,graph,root=stitched
    submit(service,graph)
    updated=subtitle_jobs.run(graph['id'],render=renderer(root,[]))
    (root/updated['output']['subtitle_files'][0]['storage_uri']).write_text('changed',encoding='utf-8')
    with pytest.raises(service.StoryboardError): _confirm(service,updated)


def test_active_publishing_input_cannot_be_replaced(stitched):
    service,graph,_=stitched
    st.put('publish_tasks','pub_active',{'id':'pub_active','work_id':graph['work_id'],'state':'submitting'}, {'work_id':graph['work_id']})
    with pytest.raises(service.StoryboardError): submit(service,graph)


def test_subtitle_files_download_as_current_verified_assets(stitched):
    from app.short_video import subtitle_jobs
    service,graph,root=stitched;submit(service,graph)
    updated=subtitle_jobs.run(graph['id'],render=renderer(root,[]))
    for item in updated['output']['subtitle_files']:
        response=TestClient(app).get(item['preview_url'])
        assert response.status_code==200
        assert response.content==(root/item['storage_uri']).read_bytes()
    (root/updated['output']['subtitle_files'][0]['storage_uri']).write_text('tampered',encoding='utf-8')
    assert TestClient(app).get(updated['output']['subtitle_files'][0]['preview_url']).status_code==409


def test_intent_and_graph_binding_roll_back_together(stitched, monkeypatch):
    service,graph,_=stitched
    original_insert=st.insert_row
    def fail_graph(conn,table,key,data,extra=None):
        if table=='media_jobs' and key==graph['id'] and data.get('subtitles'):
            raise RuntimeError('interrupted graph binding')
        return original_insert(conn,table,key,data,extra)
    monkeypatch.setattr(st,'insert_row',fail_graph)
    with pytest.raises(RuntimeError): submit(service,graph)
    assert not [j for j in st.all_rows('provider_jobs','work_id=?',[graph['work_id']]) if j['capability']=='storyboard_subtitles']
    monkeypatch.setattr(st,'insert_row',original_insert)
    assert submit(service,graph)['subtitles']['state']=='queued'


def test_lease_before_running_crash_expires_without_automatic_asr_retry(stitched,monkeypatch):
    from app.short_video import subtitle_jobs
    service,graph,_=stitched
    queued=submit(service,graph);job_id=queued['subtitles']['job_id']
    jobs.acquire_lease(job_id,'crashed',ttl_seconds=7200,require_queued=True)
    job=jobs.get_job(job_id);job['lease']['expires_at']=jobs._now()-1;st.put('provider_jobs',job_id,job,{'work_id':graph['work_id']})
    all_rows=st.all_rows
    monkeypatch.setattr(st,'all_rows',lambda table,*args: [service.get_graph(graph['id'])] if table=='media_jobs' and not args else all_rows(table,*args))
    subtitle_jobs.run_queued()
    recovered=service.project(service.get_graph(graph['id']))
    assert recovered['subtitles']['state']=='failed'
    assert recovered['subtitles']['error']['code']=='STORYBOARD_SUBTITLE_INTERRUPTED'
    assert recovered['video_id']==graph['video_id']
    assert len([j for j in st.all_rows('provider_jobs','work_id=?',[graph['work_id']]) if j['capability']=='storyboard_subtitles'])==1


def test_real_ffmpeg_caption_pipeline_creates_and_packages_new_version(stitched, monkeypatch):
    from app.short_video import subtitle_jobs
    from app.video import asr
    service,graph,root=stitched
    class NativeAdapter:
        alignment_source='forced_alignment';simulated=False
        def transcribe_native(self,audio_path,*,cache_path,attempt_id=None):
            return {'transcript':'实际对白。','cues':[{'start_ms':200,'end_ms':1200,'text':'实际对白。'}]}
    monkeypatch.setattr(asr,'require_adapter',lambda:NativeAdapter())
    submit(service,graph)
    updated=subtitle_jobs.run(graph['id'])
    assert updated['subtitles']['state']=='succeeded',updated['subtitles']
    assert updated['output']['file_hash']!=graph['output']['file_hash']
    checks=updated['output']['subtitle_checks']
    assert checks['source_audio_packet_hash']==checks['output_audio_packet_hash']
    result=_confirm(service,updated)
    assert result['package']['state']=='ready'


@pytest.mark.parametrize('code',['ASR_STAGE_PENDING','ASR_STAGE_UNKNOWN','ASR_WORKER_UNREACHABLE','ASR_OUTPUT_UNREADABLE','ASR_WORKER_AUTH_FAILED'])
def test_uncertain_remote_subtitle_job_retains_receipt_and_cannot_retry(stitched,code):
    from app.short_video import subtitle_jobs
    from app.video.errors import VideoBlocked
    service,graph,_=stitched
    submit(service,graph)
    def pending(*args,**kwargs):
        raise VideoBlocked(code,'原字幕作业尚未确认完成',detail={'job_id':'remote-original','stage':'align','token':'do-not-copy'})
    updated=subtitle_jobs.run(graph['id'],render=pending)
    assert updated['subtitles']['state']=='unknown'
    assert updated['subtitles']['remote_evidence']=={'job_id':'remote-original','stage':'align'}
    assert not updated['subtitle_generation_available']
    assert jobs.get_job(updated['subtitles']['job_id'])['state']=='unknown'
    with pytest.raises(service.StoryboardError): submit(service,graph,request_id='other')
    with pytest.raises(service.StoryboardError): _confirm(service,graph)
