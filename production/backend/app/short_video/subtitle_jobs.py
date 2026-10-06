"""Durable, explicitly requested subtitle versions of existing native masters."""
from __future__ import annotations

import argparse
import copy
import os
import re
import time
import uuid
from urllib.parse import quote

from .. import config, jobs, storage as st
from ..contracts import now_iso
from . import media, service

CAPABILITY = 'storyboard_subtitles'
LEASE_SECONDS = 7200
ACTIVE = ('queued', 'running', 'unknown')


def available(graph: dict) -> bool:
    return bool(graph.get('state') == 'succeeded' and graph.get('video_id') and graph.get('output')
        and not graph['output'].get('subtitles_burned') and not graph.get('is_mock')
        and (graph.get('subtitles') or {}).get('state') not in ACTIVE)


def _assert_not_publishing(work_id: str) -> None:
    for task in st.all_rows('publish_tasks', 'work_id=?', [work_id]):
        if task.get('state') in ('uploading', 'ready_to_confirm', 'submitting', 'needs_verification'):
            raise service.StoryboardError('STORYBOARD_PUBLISH_IN_PROGRESS', '当前作品正在准备或提交发布，请先完成或取消发布后补字幕')


def queue(graph_id: str, *, actor_id: str, compute_ack: bool, expected_video_id: str,
          output_digest: str, request_id: str) -> dict:
    from .approval import require_video
    if (not isinstance(actor_id, str) or not actor_id.startswith('user') or compute_ack is not True
        or not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', request_id)
        or not isinstance(expected_video_id, str) or not expected_video_id or not isinstance(output_digest, str)):
        raise service.StoryboardError('STORYBOARD_SUBTITLE_APPROVAL_REQUIRED', '请确认当前成片并授权使用云端 GPU 生成字幕')
    approval = dict(actor_id=actor_id, compute_ack=True, expected_video_id=expected_video_id,
                    output_digest=output_digest, request_id=request_id)
    key = graph_id + ':subtitles:' + request_id
    with service._paid_claim(graph_id):
        graph = service.get_graph(graph_id)
        service.assert_current(graph, paid=True)
        row = st.conn().execute('SELECT job_id FROM provider_jobs_idem WHERE idem_key=?', [key]).fetchone()
        previous = jobs.get_job(row['job_id']) if row else None
        if previous:
            if previous.get('subtitle_approval') != approval:
                raise service.StoryboardError('STORYBOARD_SUBTITLE_REQUEST_CONFLICT', '本次请求号已绑定另一份成片或授权，请刷新后重试')
            return service.project(graph)
        if not available(graph):
            raise service.StoryboardError('STORYBOARD_SUBTITLE_NOT_READY', '字幕仍在处理中、已生成，或原成片尚未完成，请刷新状态')
        if graph.get('video_id') != expected_video_id or graph['output'].get('file_hash') != output_digest:
            raise service.StoryboardError('STORYBOARD_SUBTITLE_SOURCE_CHANGED', '成片版本已变化，请观看当前视频后重新确认')
        require_video(st.get('videos', expected_video_id) or {}, st.get('works', graph['work_id']) or {})
        _assert_not_publishing(graph['work_id'])
        args = dict(graph_id=graph_id, source_video_id=expected_video_id, source_hash=output_digest,
                    clip_digest=service._clip_digest(graph), script_digest=graph['script_digest'])
        with st.transaction() as conn:
            current = service.get_graph(graph_id)
            service.assert_current(current, paid=True)
            if not available(current) or current.get('video_id') != expected_video_id or current['output']['file_hash'] != output_digest:
                raise service.StoryboardError('STORYBOARD_SUBTITLE_SOURCE_CHANGED', '提交期间成片或字幕作业已变化')
            _assert_not_publishing(current['work_id'])
            jobs._reject_deleted_work(current['work_id'])
            # Local nonbillable intent and its graph binding commit together.
            # jobs.submit owns a separate transaction and cannot provide this boundary.
            job = {'id':st.new_id('job'), 'work_id':graph['work_id'], 'task_id':graph['task_id'],
                'capability':CAPABILITY, 'provider':'qwen3_gpu_ffmpeg', 'model':'Qwen3-ASR-1.7B/Qwen3-ForcedAligner-0.6B',
                'args_digest':jobs.args_digest(args), 'idempotency_key':key, 'namespace':'wan-storyboard',
                'billable':False, 'amount_fen':0, 'heavy':False, 'state':'queued', 'settlement_required':False,
                'stale_writers':[], 'attempts':0, 'note':'Existing cloud GPU; no Wan video generation',
                'created_at':now_iso(), 'updated_at':now_iso(), 'subtitle_approval':approval, 'subtitle_args':args}
            current.update(subtitles={'state':'queued', 'job_id':job['id'], 'requested_at':now_iso()}, updated_at=now_iso())
            st.insert_row(conn, 'provider_jobs', job['id'], job, {'work_id':graph['work_id']})
            conn.execute('INSERT INTO provider_jobs_idem (idem_key,job_id) VALUES (?,?)', [key,job['id']])
            st.insert_row(conn, 'media_jobs', graph_id, current, {'work_id':graph['work_id']})
        return service.project(current)


def verify_output(output: dict, source: dict) -> None:
    checks = output.get('subtitle_checks') or {}
    if (output.get('subtitles_burned') is not True or checks.get('outcome') != 'pass'
        or checks.get('alignment_source') != 'forced_alignment' or not str(checks.get('transcript') or '').strip()
        or not checks.get('source_audio_hash') or not checks.get('source_audio_packet_hash')
        or checks['source_audio_packet_hash'] != checks.get('output_audio_packet_hash')
        or output.get('native_audio') is not True
        or output.get('subtitle_source') != {'video_id':source['video_id'], 'file_hash':source['output']['file_hash']}
        or output.get('input_clip_hashes') != source['output'].get('input_clip_hashes')
        or abs(output.get('duration_ms',0)-source['output']['duration_ms']) > 100):
        raise service.StoryboardError('STORYBOARD_SUBTITLE_PROOF_INVALID', '字幕结果缺少原片、实际对白时间轴或原声音轨验证')
    files = output.get('subtitle_files') or []
    if len(files) != 2 or {f.get('kind') for f in files} != {'subtitles_ass', 'subtitles_srt'}:
        raise service.StoryboardError('STORYBOARD_SUBTITLE_FILES_MISSING', 'ASS/SRT 字幕文件尚未全部生成')
    for item in files:
        path = media.local_clip(item)
        if not path.stat().st_size or path.stat().st_size != item.get('bytes'):
            raise service.StoryboardError('STORYBOARD_SUBTITLE_FILE_CHANGED', '字幕文件已变化，请重新生成并审核')
    media.local_clip(output)


def _source_current(graph: dict, job: dict) -> None:
    service.assert_current(graph, paid=True)
    args = job.get('subtitle_args') or {}
    if ((graph.get('subtitles') or {}).get('job_id') != job['id'] or graph.get('video_id') != args.get('source_video_id')
        or (graph.get('output') or {}).get('file_hash') != args.get('source_hash')
        or service._clip_digest(graph) != args.get('clip_digest') or graph['script_digest'] != args.get('script_digest')):
        raise service.StoryboardError('STORYBOARD_SUBTITLE_SOURCE_CHANGED', '原成片或字幕任务已变化，保留结果但不替换当前视频')
    _assert_not_publishing(graph['work_id'])


def run(graph_id: str, *, worker_id: str | None = None, render=None) -> dict:
    from .approval import require_video
    graph = service.get_graph(graph_id)
    job = jobs.get_job((graph.get('subtitles') or {}).get('job_id') or '')
    if not job or job['state'] != 'queued':
        return service.project(graph)
    worker_id = worker_id or 'storyboard-subtitles-' + uuid.uuid4().hex
    try:
        jobs.acquire_lease(job['id'], worker_id, ttl_seconds=LEASE_SECONDS, require_queued=True)
    except jobs.JobRejected:
        return service.project(service.get_graph(graph_id))
    try:
        with st.transaction() as conn:
            current = service.get_graph(graph_id)
            _source_current(current, job)
            current['subtitles'].update(state='running', started_at=now_iso())
            current['updated_at'] = now_iso()
            st.insert_row(conn, 'media_jobs', graph_id, current, {'work_id':graph['work_id']})
        require_video(st.get('videos', graph['video_id']) or {}, st.get('works', graph['work_id']) or {})
        if render is None:
            from .subtitle_media import render
        output = render(copy.deepcopy(graph), job_id=job['id'])
        verify_output(output, graph)
        output['preview_url'] = '/api/v1/media?uri=' + quote(output['storage_uri'], safe='/')
        with st.transaction() as conn:
            current = service.get_graph(graph_id)
            saved = jobs.get_job(job['id'])
            jobs._check_writer(saved, worker_id)
            _source_current(current, job)
            require_video(st.get('videos', graph['video_id']) or {}, st.get('works', graph['work_id']) or {})
            work = st.get('works', graph['work_id'])
            video = {**st.get('videos',graph['video_id']), **output, 'id':st.new_id('storyboard_video'),
                     'subtitle_job_id':job['id'], 'confirmed':False, 'approval_state':'pending', 'created_at':now_iso()}
            for field in ('confirmed_at', 'confirmed_by', 'revoked_at', 'revoked_reason'):
                video.pop(field, None)
            result = {'output':output, 'video_id':video['id'], 'source_video_id':graph['video_id'], 'source_output':graph['output']}
            saved.update(state='succeeded', result=result, result_digest=jobs.args_digest(result), lease=None, updated_at=now_iso())
            current.setdefault('output_history', []).append({'video_id':graph['video_id'], 'output':graph['output'], 'superseded_at':now_iso()})
            current.update(output=output, video_id=video['id'], updated_at=now_iso(), subtitles={'state':'succeeded', 'job_id':job['id'], 'completed_at':now_iso()})
            current.pop('package_id', None)
            current.pop('package_error', None)
            previous = st.get('videos',graph['video_id'])
            if previous.get('confirmed'):
                previous.update(confirmed=False, approval_state='revoked', revoked_at=now_iso(), revoked_reason='已生成带字幕新版本，等待重新审核')
                st.insert_row(conn,'videos',previous['id'],previous,{'work_id':graph['work_id']})
            work['video_asset_id'] = video['id']
            task = st.get('tasks',graph['task_id'])
            task.update(stage='video_review', state='waiting_selection', stage_rows=st.stage_rows('video_review','waiting_selection'))
            st.insert_row(conn,'provider_jobs',saved['id'],saved,{'work_id':graph['work_id']})
            st.insert_row(conn,'videos',video['id'],video,{'work_id':graph['work_id']})
            st.insert_row(conn,'works',work['id'],work)
            st.insert_row(conn,'tasks',task['id'],task,{'work_id':graph['work_id']})
            st.insert_row(conn,'media_jobs',graph_id,current,{'work_id':graph['work_id']})
        return service.project(current)
    except Exception as error:
        message = getattr(error, 'message', '字幕处理失败，原成片已保留，请检查字幕 Worker 日志')
        failure = {'code':getattr(error,'code','STORYBOARD_SUBTITLE_FAILED'), 'message':message}
        uncertain = failure['code'] in ('ASR_STAGE_PENDING', 'ASR_STAGE_UNKNOWN', 'ASR_WORKER_UNREACHABLE',
            'ASR_WORKER_HTTP_ERROR', 'ASR_WORKER_AUTH_FAILED', 'ASR_OUTPUT_MISSING', 'ASR_OUTPUT_UNREADABLE')
        detail = getattr(error, 'detail', {}) or {}
        remote = {k:detail[k] for k in ('job_id','stage') if isinstance(detail.get(k),str)}
        with st.transaction() as conn:
            saved = jobs.get_job(job['id'])
            try:
                jobs._check_writer(saved, worker_id)
            except jobs.JobRejected:
                return service.project(service.get_graph(graph_id))
            saved.update(state='unknown' if uncertain else 'failed',error=failure,lease=None,updated_at=now_iso(),remote_evidence=remote)
            current = service.get_graph(graph_id)
            if (current.get('subtitles') or {}).get('job_id') == job['id']:
                current['subtitles'].update(state=saved['state'], error=failure, remote_evidence=remote)
                current['updated_at'] = now_iso()
                st.insert_row(conn,'media_jobs',graph_id,current,{'work_id':graph['work_id']})
            st.insert_row(conn,'provider_jobs',saved['id'],saved,{'work_id':graph['work_id']})
        return service.project(current)


def run_queued(*, worker_id: str | None = None) -> list[dict]:
    results=[]
    for graph in st.all_rows('media_jobs'):
        if graph.get('kind') != service.KIND:
            continue
        subtitles=graph.get('subtitles') or {}
        if subtitles.get('state') in ('queued','running'):
            job=jobs.get_job(subtitles.get('job_id') or '') or {}
            if subtitles.get('state') == 'queued' and job.get('state') == 'queued':
                results.append(run(graph['id'],worker_id=worker_id))
                continue
            if job.get('state') == 'leased' and (job.get('lease') or {}).get('expires_at',0) <= jobs._now():
                with st.transaction() as conn:
                    current=service.get_graph(graph['id']);saved=jobs.get_job(job['id'])
                    if (current.get('subtitles') or {}).get('job_id') != job['id'] or saved['state'] != 'leased' or (saved.get('lease') or {}).get('expires_at',0) > jobs._now():
                        continue
                    failure={'code':'STORYBOARD_SUBTITLE_INTERRUPTED','message':'字幕处理已超过执行时限；原成片保留，请核对云端作业后重试'}
                    saved.update(state='failed',lease=None,error=failure,updated_at=now_iso())
                    current['subtitles'].update(state='failed',error=failure)
                    st.insert_row(conn,'provider_jobs',saved['id'],saved,{'work_id':graph['work_id']})
                    st.insert_row(conn,'media_jobs',graph['id'],current,{'work_id':graph['work_id']})
    return results


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--loop',action='store_true')
    parser.add_argument('--interval-seconds',type=float,default=3)
    args=parser.parse_args()
    if not 0 < args.interval_seconds <= 60:
        parser.error('interval must be within 0..60 seconds')
    config.bootstrap_secrets()
    st.init_db()
    worker_id='storyboard-subtitles-' + str(os.getpid()) + '-' + uuid.uuid4().hex[:8]
    while True:
        for graph in run_queued(worker_id=worker_id):
            print(graph['id'] + ': ' + str((graph.get('subtitles') or {}).get('state')),flush=True)
        if not args.loop:
            break
        time.sleep(args.interval_seconds)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
