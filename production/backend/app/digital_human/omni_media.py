"""OmniHuman 媒体桥接：已登记本地输入 → 哈希校验 → 分段切片 → TOS → 匿名读取校验。

这里有一条**极易踩中、且症状极具误导性**的云端约束：

    OmniHuman 的 `audio_url` 是"整段音频"，官方契约写明
    **音频时长必须小于 60 秒**；超长时"提交任务正常，查询任务会报错"
    `code 50215 Input invalid for this service.`

也就是说，只要把**整条音轨**发给云端，提交会拿到一个看起来正常的 `task_id`，
但之后无论查多少次都是 50215。只按分段算钱、却把整轨发出去，
就会得到"提交成功、查询永远失败"这种最难归因的形态（2026-09-28 真实踩到）。

所以：**每一段都必须携带它自己那一段的音频**。切片只裁剪时间轴——
不加速、不截字；切点沿用分段计划里来自句子边界的真实毫秒位置。
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import publish, worker_client
from .errors import TalkingHeadBlocked

SCRATCH_ENV = "AVATAR_STUDIO_OMNIHUMAN_SCRATCH"


def _scratch_dir() -> Path:
    """切片临时目录。放在后端 runtime 下，避免依赖系统临时目录的可写性。"""
    configured = (os.environ.get(SCRATCH_ENV) or "").strip()
    base = (Path(configured) if configured
            else Path(__file__).resolve().parents[2] / "runtime" / "omnihuman_segments")
    base.mkdir(parents=True, exist_ok=True)
    return base


def _local_media_path(field: str, uri: Any, declared: Any) -> Path:
    """已登记、真实且哈希匹配的本地媒体路径。上传与切片共用同一套校验。"""
    try:
        path = worker_client._registered_input_path(field, str(uri or ''), str(declared or ''))
    except TalkingHeadBlocked as error:
        raise TalkingHeadBlocked('OMNIHUMAN_MEDIA_PATH_INVALID',
                                 '人物图或配音文件缺失、未登记或路径不合法；尚未提交生成。',
                                 {'input': field, 'reason': error.code}) from error
    try:
        with path.open('rb') as stream:
            actual = 'sha256:' + hashlib.file_digest(stream, 'sha256').hexdigest()
    except OSError as error:
        raise TalkingHeadBlocked('OMNIHUMAN_MEDIA_PATH_INVALID',
                                 '人物图或配音文件暂时无法读取；尚未提交生成。', {'input': field}) from error
    if actual != declared:
        raise TalkingHeadBlocked('OMNIHUMAN_MEDIA_HASH_MISMATCH',
                                 '人物图或配音已变化，与审核时文件不一致；请重新检查素材。', {'input': field})
    return path


def _publisher(ak: str, sk: str) -> "publish.TosPublisher":
    try:
        return publish.TosPublisher(ak, sk, publish.resolved_bucket())
    except Exception as error:  # noqa: BLE001
        raise TalkingHeadBlocked('OMNIHUMAN_MEDIA_UPLOAD_FAILED',
                                 '无法初始化素材存储，请检查火山 TOS 配置；尚未提交生成。') from error


def prepare_media(source: Dict[str, Any], audio: Dict[str, Any], *, ak: str, sk: str) -> Dict[str, Any]:
    """只返回传输地址；不改写原图/音频路径、审核记录或确认状态。

    source 必须来自 resolve_plate_source，audio 必须是该作品已确认配音。
    两份本地文件都验证完才上传，避免第二份缺失时已把第一份发出去。

    额外返回 `audio_path`：分段切片要按同一份本地音频切，不能让调用方
    再走一遍"猜路径"的逻辑，否则切片用的文件与上传校验过的文件可能不是同一个。
    """
    urls: Dict[str, Any] = {}
    pending = []
    for key, field, uri, declared in (
        ('image_url', 'source_image_url', source.get('storage_uri'), source.get('asset_hash')),
        ('audio_url', 'audio_url', audio.get('storage_uri'), audio.get('file_hash')),
    ):
        if isinstance(uri, str) and uri.startswith(('https://', 'http://')):
            urls[key] = uri
            continue
        path = _local_media_path(field, uri, declared)
        if key == 'audio_url':
            urls['audio_path'] = path
        kind = 'image' if key == 'image_url' else 'audio'
        ext = path.suffix.lower().lstrip('.')
        pending.append((key, path, f'omnihuman/{kind}/{str(declared)[7:]}.{ext}', declared,
                        worker_client._ASSET_MIME[ext]))
    if pending:
        publisher = _publisher(ak, sk)
        for key, path, object_key, declared, mime in pending:
            try:
                if not publisher.exists(object_key):
                    publisher.ensure_bucket()
                    publisher.publish_file(str(path), key=object_key, content_type=mime)
                url = publisher.url_for(object_key)
            except Exception as error:  # noqa: BLE001
                raise TalkingHeadBlocked('OMNIHUMAN_MEDIA_UPLOAD_FAILED',
                                         '人物图或配音上传失败，请检查 TOS 网络和对象写入权限；尚未提交生成。',
                                         {'input': key, 'error_type': type(error).__name__}) from error
            try:
                publisher.verify_public_file(url, declared)
            except Exception as error:  # noqa: BLE001
                raise TalkingHeadBlocked('OMNIHUMAN_MEDIA_NOT_READABLE',
                                         '素材已存储，但外部读取或文件校验未通过；请检查 TOS 对象读取权限和网络。尚未提交生成。',
                                         {'input': key, 'error_type': type(error).__name__}) from error
            urls[key] = url
    return urls


def _segment_window(seg: Dict[str, Any]) -> tuple:
    """分段的 [start_ms, end_ms)。优先用句子边界的真实位置，缺失才退回偏移+时长。"""
    start_ms = int(seg.get('start_ms', seg.get('global_offset_ms', 0)) or 0)
    end_ms = seg.get('end_ms')
    if end_ms is None:
        end_ms = start_ms + int(seg.get('duration_ms') or 0)
    return start_ms, int(end_ms)


def _slice_audio(src: Path, start_ms: int, end_ms: int, dest: Path) -> None:
    """按时间窗切出一段音频。

    只裁剪时间轴：`-ss/-t` 只挪窗口，不碰速度（不用 atempo）、不碰内容
    （不按字数截断）。切点来自分段计划的句子边界，因此段首段尾都在句读处。
    """
    from ..video import ffmpeg as vff

    start_s = max(0.0, start_ms / 1000.0)
    dur_s = max(0.001, (end_ms - start_ms) / 1000.0)
    try:
        cp = vff.run_ffmpeg(["-v", "error", "-ss", f"{start_s:.3f}", "-i", str(src),
                             "-t", f"{dur_s:.3f}", "-acodec", "pcm_s16le", str(dest)],
                            timeout=180)
    except Exception as error:  # noqa: BLE001
        raise TalkingHeadBlocked('OMNIHUMAN_SEGMENT_AUDIO_SLICE_FAILED',
                                 '分段音频切分失败：不得退回整轨音频提交（整轨超限会被云端拒绝）。',
                                 {'error_type': type(error).__name__}) from error
    if cp.returncode != 0:
        detail = (cp.stderr or b'').decode('utf-8', 'replace')[:200]
        raise TalkingHeadBlocked('OMNIHUMAN_SEGMENT_AUDIO_SLICE_FAILED',
                                 '分段音频切分失败：不得退回整轨音频提交（整轨超限会被云端拒绝）。',
                                 {'ffmpeg': detail})
    if not dest.is_file() or dest.stat().st_size == 0:
        raise TalkingHeadBlocked('OMNIHUMAN_SEGMENT_AUDIO_SLICE_FAILED',
                                 '分段音频切分未产出文件：不得退回整轨音频提交。')


def prepare_segment_audio(audio: Dict[str, Any], segments: List[Dict[str, Any]], *,
                          audio_url: str, audio_path: Optional[Path],
                          ak: str, sk: str) -> Dict[str, str]:
    """为**要提交的那几段**各准备一份音频 URL，返回 `{segment_id: url}`。

    调用方只传"这一次真的要提交的段"，不要把整条计划的 7 段都传进来：
    推进器每轮都可能走到这里，重复切片纯属浪费。

    整轨恰好就是这一段时（音频未超限的单次调用）直接用整轨 URL，不做无谓重编码。
    对象按切片内容的 sha256 命名，因此重试、重做、换段顺序都能自然复用。
    """
    total_ms = int(audio.get('duration_ms') or 0)
    out: Dict[str, str] = {}
    to_slice: List[tuple] = []
    for seg in segments:
        segment_id = seg.get('segment_id')
        if not segment_id:
            raise TalkingHeadBlocked('OMNIHUMAN_SEGMENT_ID_MISSING',
                                     '分段缺少 segment_id，无法为它准备独立音频；不提交。')
        start_ms, end_ms = _segment_window(seg)
        if start_ms <= 0 and (not total_ms or end_ms >= total_ms):
            # 整轨即该段：超限根本不会发生，用原对象最省事也最不容易出错。
            out[segment_id] = audio_url
            continue
        to_slice.append((segment_id, start_ms, end_ms))

    if not to_slice:
        return out
    if audio_path is None:
        # 音频已是公网 URL：拿不到本地字节就切不了。这时**绝不能**拿整轨兜底——
        # 整轨超限必然在查询阶段变成 50215，那种失败看起来像"云端坏了"。
        raise TalkingHeadBlocked(
            'OMNIHUMAN_SEGMENT_AUDIO_UNSLICEABLE',
            '配音只有公网地址、没有本地文件，无法按句子边界切片；'
            '整轨音频超限会被云端拒绝（50215），因此不提交。',
            {'segments': [s[0] for s in to_slice]})

    publisher = _publisher(ak, sk)
    scratch = _scratch_dir()
    for segment_id, start_ms, end_ms in to_slice:
        dest = scratch / f'{segment_id}.wav'
        try:
            _slice_audio(audio_path, start_ms, end_ms, dest)
            with dest.open('rb') as stream:
                digest = 'sha256:' + hashlib.file_digest(stream, 'sha256').hexdigest()
            object_key = f'omnihuman/audio_seg/{digest[7:]}.wav'
            try:
                if not publisher.exists(object_key):
                    publisher.ensure_bucket()
                    publisher.publish_file(str(dest), key=object_key,
                                           content_type=worker_client._ASSET_MIME['wav'])
                url = publisher.url_for(object_key)
            except Exception as error:  # noqa: BLE001
                raise TalkingHeadBlocked('OMNIHUMAN_MEDIA_UPLOAD_FAILED',
                                         '分段音频上传失败，请检查 TOS 网络和对象写入权限；尚未提交生成。',
                                         {'segment_id': segment_id,
                                          'error_type': type(error).__name__}) from error
            try:
                publisher.verify_public_file(url, digest)
            except Exception as error:  # noqa: BLE001
                raise TalkingHeadBlocked('OMNIHUMAN_MEDIA_NOT_READABLE',
                                         '分段音频已存储，但外部读取或字节校验未通过；尚未提交生成。',
                                         {'segment_id': segment_id,
                                          'error_type': type(error).__name__}) from error
            out[segment_id] = url
        finally:
            try:
                dest.unlink()
            except OSError:
                pass
    return out
