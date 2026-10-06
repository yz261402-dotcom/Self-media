"""ASR/对齐适配器接口（AC-MEDIA-04 的"ASR/对齐"能力位）。

红线（specs T07）：**未配置或未验的 ASR/对齐能力不得被假定可用。**
- 已登记 ASR-001 验证的 Qwen3 云端适配器；默认不启用，真实 Worker 到字幕步
  必须 blocked（ASR_NOT_CONFIGURED），而不是用"按字数均分时间"之类的假时间轴；
- 对齐来源白名单沿用 T04 结论：provider / forced_alignment / asr_alignment；
  **均分时长不算对齐**——任何实现不得以均分充当时间戳来源；
- 测试可用注入 fixture adapter（显式标记 simulated），只证明处理逻辑，
  不得在生产路径出现。
"""
from __future__ import annotations

import json
import math
import os
import unicodedata
from typing import Any, Dict, List, Optional, Protocol

from .errors import VideoBlocked

ALLOWED_ALIGNMENT_SOURCES = ("provider", "forced_alignment", "asr_alignment")

_ENV_PROVIDER = "AVATAR_STUDIO_ASR_PROVIDER"

# 提供方在文件末尾登记模型、验证状态与证据。
KNOWN_PROVIDERS: Dict[str, Dict[str, Any]] = {}


class AsrAdapter(Protocol):
    """ASR/对齐适配器协议：输入音频路径，输出**源时间轴** cue 列表。"""

    provider_id: str
    alignment_source: str  # 必须在 ALLOWED_ALIGNMENT_SOURCES 内
    simulated: bool        # 测试夹具为 True；生产实现必须 False

    def transcribe(self, audio_path: str, script_text: str) -> List[Dict[str, Any]]:
        """返回 [{"start_ms", "end_ms", "text"}]，文本拼接须与 script_text 一致。"""
        ...


def configured() -> bool:
    return bool(os.environ.get(_ENV_PROVIDER))


def provider_id() -> Optional[str]:
    return os.environ.get(_ENV_PROVIDER)


def get_adapter() -> Optional[AsrAdapter]:
    """按环境取真实适配器。未配置返回 None；配置了未知 provider 明确报错。"""
    pid = provider_id()
    if not pid:
        return None
    impl = KNOWN_PROVIDERS.get(pid)
    if impl is None:
        raise VideoBlocked(
            "ASR_PROVIDER_UNKNOWN",
            f"ASR provider '{pid}' 未实现或未通过选型验证；已登记的 provider: "
            f"{sorted(KNOWN_PROVIDERS) or '无'}")
    return impl["adapter"]()


ASR_WORKER_URL_ENV = "AVATAR_STUDIO_ASR_WORKER_URL"
ASR_WORKER_TOKEN_ENV = "AVATAR_STUDIO_ASR_WORKER_TOKEN"

_QWEN_STAGES = ("asr", "align")
_SENTENCE_END = "。？！；!?"
_CLAUSE_END = ("，", "、", "；", "：", ",", ";", ":")
_TAIL_KEEP = "，。？！、；：,.?!;:"


def _restore_punctuation(items: List[Dict[str, Any]], alignment_text: str) -> List[Dict[str, Any]]:
    """恢复原稿标点；点时间词仅并入端点相接的词，不分配虚构时长。"""
    units: List[Dict[str, Any]] = []
    timings: list[tuple[float, float]] = []
    text = "".join(alignment_text.split())
    cursor = 0
    prefix = ""

    def consume_punctuation() -> str:
        nonlocal cursor
        start = cursor
        while cursor < len(text) and unicodedata.category(text[cursor]).startswith("P"):
            cursor += 1
        return text[start:cursor]

    for item in items:
        punctuation = consume_punctuation()
        if units:
            units[-1]["tail"] += punctuation
        else:
            prefix += punctuation
        token = "".join(str(item.get("text") or "").split())
        if not token or not text.startswith(token, cursor):
            raise VideoBlocked("ALIGN_COVERAGE_INCOMPLETE", "字词时间轴与冻结正文不一致")
        try:
            start, end = float(item["start"]), float(item["end"])
            valid = math.isfinite(start) and math.isfinite(end) and 0 <= start <= end
            start_ms, end_ms = round(start * 1000), round(end * 1000)
            valid = valid and (start == end or start_ms < end_ms)
            if timings:
                valid = valid and start >= timings[-1][1]
        except (TypeError, ValueError, KeyError, OverflowError):
            valid = False
        if not valid:
            raise VideoBlocked("ALIGN_TIMING_INVALID", "对齐时间必须有限、正向且不得重叠")
        units.append({"start_ms": start_ms, "end_ms": end_ms,
                      "text": prefix + token, "tail": ""})
        timings.append((start, end))
        prefix = ""
        cursor += len(token)
    punctuation = consume_punctuation()
    if units:
        units[-1]["tail"] += punctuation
    if cursor != len(text) or not units:
        raise VideoBlocked("ALIGN_COVERAGE_INCOMPLETE", "字词时间轴未覆盖全文")

    # Qwen 可能返回“得”等词的点时间。句级字幕保留其文字，与相邻有效词
    # 共用已存在的区间；原始模型结果不修改。先比较秒端点，避免毫秒舍入
    # 把实际有间隔的点误认为相接。全部为点、悬空点仍然阻断。
    merged: List[Dict[str, Any]] = []
    pending_text = ""
    pending_at = None
    last_end = None
    for unit, (start, end) in zip(units, timings):
        if start == end:
            if merged and not pending_text and start == last_end:
                previous = merged[-1]
                previous["text"] += previous["tail"] + unit["text"]
                previous["tail"] = unit["tail"]
            else:
                if pending_at is not None and start != pending_at:
                    raise VideoBlocked("ALIGN_TIMING_INVALID", "点时间词没有端点相接的有效区间")
                pending_text += unit["text"] + unit["tail"]
                pending_at = start
            continue
        if pending_text and start != pending_at:
            raise VideoBlocked("ALIGN_TIMING_INVALID", "点时间词没有端点相接的有效区间")
        unit["text"] = pending_text + unit["text"]
        pending_text, pending_at = "", None
        merged.append(unit)
        last_end = end
    if pending_text or not merged:
        raise VideoBlocked("ALIGN_TIMING_INVALID", "点时间词没有端点相接的有效区间")
    return merged


def _group_units(units: List[Dict[str, Any]], *, max_chars: int = 18,
                 max_ms: int = 5000, gap_ms: int = 320,
                 min_chars: int = 8) -> List[Dict[str, Any]]:
    """字级时间轴 → 句级字幕行。

    断句只用四类可核对的依据：句末标点、句中逗号且已够长、字间停顿、字数/时长上限。
    没有第五种来源——均分时长不算对齐，这里也不做"看起来差不多"的切分。
    """
    cues: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for u in units:
        tail = u["tail"]
        if cur is None:
            cur = {"start_ms": u["start_ms"], "end_ms": u["end_ms"],
                   "text": u["text"] + tail}
            continue
        gap = u["start_ms"] - cur["end_ms"]
        # 先把这个字（连同它后面的标点）并进来，再判断要不要收行——
        # 标点是"能断行"的信号，它必须先属于当前行，才谈得上"在它后面断"。
        cand = cur["text"] + u["text"] + tail
        body = "".join(c for c in cand if c not in _TAIL_KEEP)
        ended = cand[-1] in _SENTENCE_END
        clause = cand[-1] in _CLAUSE_END and len(body) >= min_chars
        too_long = len(body) >= max_chars
        too_wide = (u["end_ms"] - cur["start_ms"]) > max_ms
        if gap >= gap_ms:
            # 明显停顿：旧行到此为止，新行从这个字起
            cues.append(cur)
            cur = {"start_ms": u["start_ms"], "end_ms": u["end_ms"],
                   "text": u["text"] + tail}
        elif ended or clause or too_long or too_wide:
            cur["text"] = cand
            cur["end_ms"] = u["end_ms"]
            cues.append(cur)
            cur = None
        else:
            cur["text"] = cand
            cur["end_ms"] = u["end_ms"]
    if cur:
        cues.append(cur)
    return cues


def units_to_words(units: List[Dict[str, Any]], alignment_text: str,
                   script_text: str) -> List[Dict[str, Any]]:
    """字级 unit → 可登记的 `words`（带**冻结正文**的字符区间）。

    OmniHuman 分段是按正文字符位置取切点的，所以时间轴必须能映射回冻结正文。
    对齐文本与冻结正文在**去掉空白后必须逐字一致**：不一致就说明对的是另一份文本，
    映射出来的字符区间会是错的——宁可阻断，也不登记一份位置错误的"对齐"。
    """
    norm_script = "".join(script_text.split())
    norm_align = "".join(alignment_text.split())
    if norm_align != norm_script:
        raise VideoBlocked(
            "ALIGN_TEXT_MISMATCH",
            "对齐所用文本与冻结正文（去空白后）不一致：字符位置无法可信映射，拒绝登记")

    # 归一化位置 → 冻结正文字符位置（跳过空白字符）
    norm_to_orig = [i for i, ch in enumerate(script_text) if not ch.isspace()]
    words: List[Dict[str, Any]] = []
    cursor = 0
    for unit in units:
        token = str(unit.get("text") or "")
        tail = str(unit.get("tail") or "")
        start_norm = cursor
        end_norm = cursor + len(token)
        cursor = end_norm + len(tail)
        if not token:
            continue
        if end_norm > len(norm_to_orig):
            raise VideoBlocked("ALIGN_COVERAGE_INCOMPLETE", "对齐文本超出冻结正文长度")
        words.append({
            "segment_id": "seg_1",
            "start_char": norm_to_orig[start_norm],
            "end_char": norm_to_orig[end_norm - 1] + 1,
            "start_ms": int(unit["start_ms"]),
            "end_ms": int(unit["end_ms"]),
        })
    if cursor != len(norm_to_orig):
        raise VideoBlocked("ALIGN_COVERAGE_INCOMPLETE", "对齐文本未覆盖冻结正文全文")
    return words


class Qwen3CloudAdapter:
    """Qwen3-ASR-1.7B（识别）＋ Qwen3-ForcedAligner-0.6B（时间轴）。

    两个模型都跑在算力云 ASR-001 部署上，经 worker（HTTP + Bearer）提交，
    与 M2 共用同一套"提交 → 轮询 → 取产物"语义，只是阶段不同。

    音频要先发到火山 TOS 公开读：runner 在远端，拿不到本机磁盘路径，
    而公开 URL 也是 M2 一直在用的同一条素材通路（哈希声明 + 下载校验）。

    `alignment_source = forced_alignment`（白名单内）。识别文本另存
    `last_transcript`，用来核对"音频里到底说了什么"，不参与时间轴。
    """

    provider_id = "qwen3_cloud"
    alignment_source = "forced_alignment"
    simulated = False

    def __init__(self) -> None:
        self.last_transcript: str = ""
        self.last_asr_job: Dict[str, Any] = {}
        self.last_align_job: Dict[str, Any] = {}
        self.last_audio_duration_seconds = 0.0

    # ---- 素材发布 ----

    def _publish_audio(self, audio_path: str) -> tuple:
        import hashlib

        from ..digital_human import publish as pub

        h = hashlib.sha256()
        with open(audio_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        digest = h.hexdigest()
        # 地域/桶名一律走 publish 的解析器，避免这里出现第二份真相：
        # 两者都是账号相关的，写死会在别人的账号上直接失败。
        bucket = pub.resolved_bucket()
        publisher = pub.TosPublisher(
            os.environ.get("AVATAR_STUDIO_VOLC_ACCESS_KEY", ""),
            os.environ.get("AVATAR_STUDIO_VOLC_SECRET_KEY", ""),
            bucket)
        key = f"audio/{digest}.wav"
        if not publisher.exists(key):
            publisher.ensure_bucket()
            publisher.publish_file(audio_path, key=key)
        return pub.public_url(bucket, publisher.region, key), f"sha256:{digest}"

    # ---- 远端作业 ----

    def _run_stage(self, stage: str, audio_url: str, declared: str,
                   script_text: str, *, attempt_id: Optional[str] = None) -> Dict[str, Any]:
        from ..digital_human import worker_client as wc

        base = (os.environ.get(ASR_WORKER_URL_ENV) or "").strip().rstrip("/")
        token = (os.environ.get(ASR_WORKER_TOKEN_ENV)
                 or os.environ.get("AVATAR_STUDIO_M2_WORKER_TOKEN") or "")
        if not base:
            raise VideoBlocked(
                "ASR_NOT_CONFIGURED",
                f"未配置 {ASR_WORKER_URL_ENV}：Qwen3 ASR/对齐在算力云上执行，"
                "未接通时不得用任何替代时间轴（均分不算对齐）")
        request = {"stage": stage,
                   "model": "Qwen3-ASR-1.7B" if stage == "asr" else "Qwen3-ForcedAligner-0.6B",
                   "args": {"audio_url": audio_url, "audio_hash": declared,
                            "text": script_text}}
        if attempt_id is not None:
            request["args"]["subtitle_attempt_id"] = attempt_id
        if stage == 'align' and self.last_audio_duration_seconds > 180 and self.last_asr_job.get('job_id'):
            request['args']['asr_job_id'] = self.last_asr_job['job_id']
        resp = wc.transport(request, base_url=base, token=token,
                            allowed_stages=_QWEN_STAGES, poll_interval=5.0, max_wait=1800.0)
        # 轮询终态带 json；幂等命中已完成作业会直接返回 HTTP body。
        # 两条路径使用同一解析器，否则第二次提交会把成功误读为 unknown。
        doc = wc._doc_of(resp) or {}
        status = resp.get("status_code")
        state = str(doc.get("state") or "").strip()
        detail = {"job_id": doc.get("job_id"), "stage": stage,
                  "status_code": status, "code": doc.get("code"),
                  "error": doc.get("error") or resp.get("error"),
                  "message": doc.get("message") or resp.get("message"),
                  "tail": (doc.get("runner_output_tail") or "")[-500:]}
        if "status_code" in resp and status is None:
            raise VideoBlocked(
                "ASR_WORKER_UNREACHABLE",
                f"{stage} Worker 通信失败：{detail['message'] or detail['error'] or '无 HTTP 响应'}；"
                "作业结果尚未确认，请先查询原作业",
                detail=detail)
        if status is not None and not 200 <= status < 300:
            raise VideoBlocked(
                "ASR_WORKER_AUTH_FAILED" if status in (401, 403) else "ASR_WORKER_HTTP_ERROR",
                f"{stage} Worker 请求被拒（HTTP {status}）："
                f"{detail['code'] or ''} {detail['error'] or detail['message'] or '响应未提供原因'}；"
                "尚不能确认作业结果",
                detail=detail)
        if state != "succeeded":
            if state != "failed":
                raise VideoBlocked(
                    "ASR_STAGE_PENDING" if state in ("queued", "running") else "ASR_STAGE_UNKNOWN",
                    f"{stage} 作业尚未确认完成（state={state or 'unknown'}，"
                    f"job_id={doc.get('job_id')}）；请查询原作业，不重复提交",
                    detail=detail)
            raise VideoBlocked(
                "ASR_STAGE_FAILED",
                f"{stage} 作业未成功（state={state or 'unknown'}，"
                f"code={doc.get('code')}，error={doc.get('error')}）；"
                "字幕时间轴只能来自成功的对齐，不得降级",
                detail=detail)

        out_url = str(doc.get("output_url") or "")
        if not out_url:
            raise VideoBlocked("ASR_OUTPUT_MISSING",
                               f"{stage} 作业没给出产物地址；无产物即无时间轴")
        # worker 的 public_base 是它自己那侧的 8081；本机经隧道访问的是另一个端口，
        # 这里换成本机 base，否则下载会被 401/连接拒绝挡住。
        if "/v1/outputs/" in out_url:
            out_url = base + "/v1/outputs/" + out_url.split("/v1/outputs/")[-1]
        raw = wc.transport({"method": "GET", "url": out_url}, base_url=base, token=token)
        output_status = raw.get("status_code")
        if output_status is None or not 200 <= output_status < 300:
            raise VideoBlocked(
                "ASR_OUTPUT_UNREADABLE",
                f"{stage} 产物下载失败（HTTP {output_status}）："
                f"{raw.get('message') or raw.get('error') or '无法读取产物'}",
                detail={"job_id": doc.get("job_id"), "status_code": output_status})
        body = raw.get("body")
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        if not body:
            raise VideoBlocked("ASR_OUTPUT_UNREADABLE",
                               f"{stage} 产物下载为空（HTTP {raw.get('status_code')}）")
        try:
            parsed = json.loads(body)
        except Exception as e:  # noqa: BLE001
            raise VideoBlocked("ASR_OUTPUT_NOT_JSON",
                               f"{stage} 产物不是 JSON：{type(e).__name__}: {e}")
        return {"doc": parsed, "job": doc}

    def align_units(self, audio_path: str, script_text: str,
                    audio_ref: Optional[tuple] = None) -> Dict[str, Any]:
        """跑 ASR + 强制对齐，返回**字级 unit 列表**与它所对齐的那份文本。

        与 `transcribe` 是同一次模型调用，区别只在暴露的粒度：
        `transcribe` 再把 unit 归并成字幕行；而 OmniHuman 分段需要的是字级时间轴
        本身（要按正文字符位置取切点），所以这里把 unit 层单独暴露出来，
        而不是让两条消费方各自推断一遍时间轴——同一份对齐只推断一次。

        `audio_ref=(storage_uri, file_hash)` 时走 worker 的资产直传：音频由 worker
        按哈希收下并落成作业私有文件，不需要公开对象存储。这与 M2 底片同一条通路，
        少一套存储凭据依赖；不传则维持"发到公开存储再给 URL"的旧路径。
        """
        # 先把"没接通"说清楚，再去上传素材：否则失败形态会变成 TOS 报错，
        # 真正的原因（没配 worker）被埋掉，还白白把素材发出去了。
        if not (os.environ.get(ASR_WORKER_URL_ENV) or "").strip():
            raise VideoBlocked(
                "ASR_NOT_CONFIGURED",
                f"未配置 {ASR_WORKER_URL_ENV}：Qwen3 ASR/对齐在算力云上执行，"
                "未接通时不得用任何替代时间轴（均分不算对齐）")
        audio_url, declared = tuple(audio_ref) if audio_ref else self._publish_audio(audio_path)

        # 先识别：回答"音频里说了什么"，与稿件对照（只有一处读法差异也是差异）。
        asr = self._run_stage("asr", audio_url, declared, script_text)
        self.last_transcript = str(asr["doc"].get("text") or "")
        self.last_asr_job = asr["job"]
        self.last_audio_duration_seconds = float((asr['doc'].get('audio') or {}).get('duration_seconds') or 0)

        align = self._run_stage("align", audio_url, declared, script_text)
        doc = align["doc"]
        self.last_align_job = align["job"]
        if str(doc.get("time_unit") or "seconds") != "seconds":
            raise VideoBlocked("ASR_ALIGN_UNIT_UNKNOWN",
                               f"对齐时间单位未知：{doc.get('time_unit')!r}")
        items = doc.get("alignment") or []
        if not items:
            raise VideoBlocked("ASR_ALIGN_EMPTY", "对齐结果为空；空时间轴不得进入字幕")
        alignment_text = doc.get("alignment_text") or script_text
        return {"units": _restore_punctuation(items, alignment_text),
                "alignment_text": alignment_text}

    def transcribe(self, audio_path: str, script_text: str) -> List[Dict[str, Any]]:
        return _group_units(self.align_units(audio_path, script_text)["units"])

    def transcribe_native(self, audio_path: str, *, cache_path=None,
                          attempt_id: Optional[str] = None) -> Dict[str, Any]:
        """Recognize native sound, then align exactly that inferred text.

        This separate entry point leaves frozen-script narration unchanged. A
        successful recognition is saved before alignment, so a failed align or
        local render can resume without recognizing the same audio again.
        An explicit new local job supplies a new attempt_id: remote failed
        stage results are immutable, so this also makes a new remote request.
        """
        import hashlib
        from pathlib import Path
        import uuid

        base = (os.environ.get(ASR_WORKER_URL_ENV) or "").strip().rstrip("/")
        if not base:
            raise VideoBlocked("ASR_NOT_CONFIGURED", "未配置字幕识别与对齐 Worker；未上传音频")
        cache = Path(cache_path) if cache_path is not None else None
        identity = None
        saved = {}
        if cache is not None:
            with open(audio_path, "rb") as audio:
                audio_hash = "sha256:" + hashlib.file_digest(audio, "sha256").hexdigest()
            identity = {"audio_hash": audio_hash, "provider": self.provider_id,
                        "worker_url": base, "version": "native-asr-align-v1",
                        "attempt_id": attempt_id}
            if cache.is_symlink():
                raise VideoBlocked("ASR_CACHE_INVALID", "字幕识别缓存路径不是普通文件")
            if cache.exists():
                try:
                    old = json.loads(cache.read_text(encoding="utf-8"))
                    if isinstance(old, dict) and old.get("identity") == identity:
                        saved = old
                except (OSError, ValueError) as error:
                    raise VideoBlocked("ASR_CACHE_INVALID", "字幕识别缓存损坏，已保留原证据") from error

        def persist() -> None:
            if cache is None:
                return
            cache.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache.with_name(cache.name + "." + uuid.uuid4().hex[:12] + ".tmp")
            temporary.write_text(json.dumps(saved, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            temporary.replace(cache)

        def valid_stage(value: Any) -> Dict[str, Any]:
            if (not isinstance(value, dict) or not isinstance(value.get("doc"), dict)
                    or not isinstance(value.get("job"), dict)):
                raise VideoBlocked("ASR_OUTPUT_INVALID", "字幕识别或对齐产物结构无效，未生成字幕")
            return value

        saved["identity"] = identity
        recognized = saved.get("recognition")
        audio_ref = None
        attempt_options = {"attempt_id": attempt_id} if attempt_id is not None else {}
        if recognized is None:
            audio_ref = self._publish_audio(audio_path)
            recognized = valid_stage(self._run_stage("asr", *audio_ref, "", **attempt_options))
            text = recognized["doc"].get("text")
            if not isinstance(text, str) or not text.strip():
                raise VideoBlocked("ASR_NATIVE_TRANSCRIPT_EMPTY", "原生音轨未识别到有效对白，未生成字幕")
            saved["recognition"] = recognized
            persist()
        recognized = valid_stage(recognized)
        transcript = recognized["doc"].get("text")
        if not isinstance(transcript, str) or not transcript.strip():
            raise VideoBlocked("ASR_NATIVE_TRANSCRIPT_EMPTY", "原生音轨识别文本为空或损坏")
        self.last_transcript = transcript
        self.last_asr_job = recognized["job"]
        try:
            self.last_audio_duration_seconds = float((recognized["doc"].get("audio") or {}).get("duration_seconds") or 0)
            if not math.isfinite(self.last_audio_duration_seconds) or self.last_audio_duration_seconds < 0:
                raise ValueError("invalid duration")
        except (TypeError, ValueError, AttributeError) as error:
            raise VideoBlocked("ASR_OUTPUT_INVALID", "字幕识别音轨信息无效，未生成字幕") from error
        aligned = saved.get("alignment")
        if aligned is None:
            audio_ref = audio_ref or self._publish_audio(audio_path)
            aligned = self._run_stage("align", *audio_ref, transcript, **attempt_options)
        aligned = valid_stage(aligned)
        doc = aligned["doc"]
        if str(doc.get("time_unit") or "seconds") != "seconds":
            raise VideoBlocked("ASR_ALIGN_UNIT_UNKNOWN", "对齐时间单位未知，未生成字幕")
        alignment_text = doc.get("alignment_text") or transcript
        if not isinstance(alignment_text, str) or "".join(alignment_text.split()) != "".join(transcript.split()):
            raise VideoBlocked("ALIGN_TEXT_MISMATCH", "对齐文本与实际音轨识别文本不一致，未生成字幕")
        items = doc.get("alignment") or []
        if not isinstance(items, list) or not items:
            raise VideoBlocked("ASR_ALIGN_EMPTY", "实际对白的对齐时间轴为空，未生成字幕")
        if not all(isinstance(item, dict) for item in items):
            raise VideoBlocked("ASR_OUTPUT_INVALID", "字幕对齐时间轴结构无效，未生成字幕")
        cues = _group_units(_restore_punctuation(items, alignment_text))
        self.last_align_job = aligned["job"]
        saved["alignment"] = aligned
        persist()
        return {"transcript": transcript, "cues": cues,
                "asr_job": self.last_asr_job, "align_job": self.last_align_job,
                "alignment_source": self.alignment_source}


KNOWN_PROVIDERS["qwen3_cloud"] = {
    "adapter": Qwen3CloudAdapter,
    "models": ["Qwen3-ASR-1.7B", "Qwen3-ForcedAligner-0.6B"],
    "validation_state": "deployed_inference_verified",
    "evidence": "ASR-001 部署（算力云 RTX 4080）+ 本项目真实音频作业；"
                "时间轴来源 forced_alignment，字级时间戳经标点还原后成句",
}


def require_adapter() -> AsrAdapter:
    """真实路径取适配器；取不到即 blocked（字幕必须有真实 ASR 时间轴）。"""
    adapter = get_adapter()
    if adapter is None:
        raise VideoBlocked(
            "ASR_NOT_CONFIGURED",
            f"未配置 ASR/对齐 provider（{_ENV_PROVIDER} 为空）；"
            "字幕时间轴只能来自真实 ASR/对齐，不得均分或臆造，故阻断")
    if adapter.alignment_source not in ALLOWED_ALIGNMENT_SOURCES:
        raise VideoBlocked(
            "ASR_ALIGNMENT_SOURCE_INVALID",
            f"对齐来源 '{adapter.alignment_source}' 不在白名单 "
            f"{ALLOWED_ALIGNMENT_SOURCES}（均分时长不算对齐）")
    if adapter.simulated:
        raise VideoBlocked("ASR_SIMULATED", "模拟字幕时间轴不得进入真实后期")
    return adapter
