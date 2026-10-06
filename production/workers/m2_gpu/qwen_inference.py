"""Run one offline GPU inference task and preserve its evidence in a new directory."""

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path


MODEL_NAMES = {"asr": "Qwen3-ASR-1.7B", "align": "Qwen3-ForcedAligner-0.6B"}


def clean_text(text):
    """Remove the known MiniMax pause controls, preserving spoken text and punctuation."""
    return re.sub(r"<#\d+(?:\.\d+)?#>", "", text).strip()


def validate_duration(frames, sample_rate):
    if frames <= 0 or sample_rate <= 0:
        raise ValueError("audio must decode to nonempty samples with a positive sample rate")
    duration = frames / sample_rate
    if not math.isfinite(duration):
        raise ValueError("audio duration must be positive and finite")
    return duration


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def save_result(directory, result):
    temporary = directory / "结果.json.tmp"
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(directory / "结果.json")


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True, choices=tuple(MODEL_NAMES))
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--text", required=True, type=Path,
                        help="UTF-8 transcript; only align passes it to the model")
    parser.add_argument("--models-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path,
                        help="Must not exist, including an existing empty directory")
    parser.add_argument("--asr-result", type=Path, help="Verified ASR result for long-audio reference partitioning")
    return parser.parse_args(argv)


def validate_inputs(args, result):
    for name, path in (("audio", args.audio), ("text", args.text)):
        if not path.is_file():
            raise FileNotFoundError(f"{name} input file does not exist: {path}")
        result["inputs"][name]["sha256"] = sha256(path)
        result["inputs"][name]["bytes"] = path.stat().st_size
    text = clean_text(args.text.read_text(encoding="utf-8-sig"))
    if args.task == "align" and not text:
        raise ValueError("text is empty after removing TTS pause controls")
    model_path = args.models_root / MODEL_NAMES[args.task]
    if not model_path.is_dir():
        raise FileNotFoundError(f"model directory does not exist: {model_path}")

    # Decode and check the audio before importing torch or loading any model.
    import soundfile as sf
    import numpy as np

    info = sf.info(str(args.audio))
    validate_duration(info.frames, info.samplerate)
    samples, sample_rate = sf.read(str(args.audio), dtype="float32", always_2d=True)
    duration = validate_duration(len(samples), sample_rate)
    if not np.isfinite(samples).all():
        raise ValueError("audio contains non-finite decoded samples")
    result["audio"] = {"duration_seconds": duration, "sample_rate": sample_rate,
                       "channels": samples.shape[1], "decoded_frames": len(samples)}
    if args.task == "align":
        result["alignment_text"] = text
        result["alignment_text_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return (samples.mean(axis=1), sample_rate), text, model_path


def cuda_peaks(torch):
    return {
        "max_memory_allocated_bytes": int(torch.cuda.max_memory_allocated(0)),
        "max_memory_reserved_bytes": int(torch.cuda.max_memory_reserved(0)),
    }


def infer(model, task, audio, text, asr_evidence=None):
    samples, rate = audio
    if len(samples) / rate <= 180:
        if task == 'asr':
            prediction = model.transcribe(audio=audio, language='Chinese', return_time_stamps=False)[0]
            return {'text': prediction.text, 'language': prediction.language}
        prediction = model.align(audio=audio, text=text, language='Chinese')[0]
        return {'alignment': [{'text': x.text, 'start': float(x.start_time), 'end': float(x.end_time)}
                              for x in prediction], 'time_unit': 'seconds'}
    from qwen_audio_chunks import partition_reference, offset_alignment
    if task == 'asr':
        from qwen_asr.inference.utils import split_audio_into_chunks
        parts = split_audio_into_chunks(samples, rate, max_chunk_sec=120)
        starts = [round(offset * rate) for _, offset in parts] + [len(samples)]
        chunks = []
        for start, end in zip(starts, starts[1:]):
            prediction = model.transcribe(audio=(samples[start:end], rate), language='Chinese', return_time_stamps=False)[0]
            chunks.append({'start_sample': start, 'end_sample': end, 'sample_rate': rate,
                           'text': prediction.text, 'language': prediction.language})
        return {'text': ''.join(x['text'] for x in chunks), 'language': 'Chinese',
                'chunks': chunks, 'chunking': 'low_energy_120_seconds_no_total_limit'}
    chunks = (asr_evidence or {}).get('chunks') or []
    if not chunks:
        raise ValueError('长音频需要同一音频的 ASR 分段结果，不能按字数猜测时间轴')
    cursor = 0
    for chunk in chunks:
        start, end = chunk['start_sample'], chunk['end_sample']
        if (type(start) is not int or type(end) is not int or start != cursor or
                not start < end <= len(samples) or chunk['sample_rate'] != rate or (end - start) / rate > 180):
            raise ValueError('ASR 分段未按采样点完整覆盖输入，或单段超过模型处理窗口')
        cursor = end
    if cursor != len(samples):
        raise ValueError('ASR 分段漏掉音频尾部')
    references = partition_reference(text, [chunk['text'] for chunk in chunks])
    alignment, evidence = [], []
    for chunk, reference in zip(chunks, references):
        start, end = chunk['start_sample'], chunk['end_sample']
        prediction = model.align(audio=(samples[start:end], rate), text=reference, language='Chinese')[0]
        local = [{'text': x.text, 'start': float(x.start_time), 'end': float(x.end_time)} for x in prediction]
        alignment.extend(offset_alignment(local, start, rate, end - start))
        evidence.append({'start_sample': start, 'end_sample': end, 'reference': reference,
                         'alignment': local})
    return {'alignment': alignment, 'time_unit': 'seconds', 'chunks': evidence,
            'chunking': 'asr_reference_matching_sample_offsets'}


def main(argv=None):
    args = parse_args(argv)
    args.audio = args.audio.resolve()
    args.text = args.text.resolve()
    args.models_root = args.models_root.resolve()
    # Do not resolve the final output component: even a dangling symlink is occupied.
    output_dir = args.output_dir.absolute()
    try:
        output_dir.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        print(f"Refusing output directory: {exc}", file=sys.stderr)
        return 2

    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    result = {
        "schema_version": 1,
        "task": args.task,
        "status": "running",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "inputs": {"audio": {"path": str(args.audio)}, "text": {"path": str(args.text)}},
        "model_path": str(args.models_root / MODEL_NAMES[args.task]),
        "parameters": {"device": "cuda:0", "dtype": "bfloat16", "attention": "sdpa",
                       "batch_size": 1, "max_new_tokens": 2048 if args.task == "asr" else None,
                       "language": "Chinese", "asr_reference_context_used": False},
        "offline": {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"},
        "versions": {"python": platform.python_version(), "platform": platform.platform()},
        "timings_seconds": {"model_load": None, "inference": None},
        "cuda_memory": None,
        "validation": "Execution record only; subtitle quality and timeline checks are separate.",
    }
    torch = None
    start = time.perf_counter()
    operation_start = None
    stage = "input_validation"
    try:
        save_result(output_dir, result)
        audio, text, model_path = validate_inputs(args, result)
        asr_evidence = None
        if args.task == 'align' and result['audio']['duration_seconds'] > 180:
            if not args.asr_result or not args.asr_result.is_file():
                raise ValueError('长音频缺少已完成的 ASR 分段证据')
            asr_evidence = json.loads(args.asr_result.read_text(encoding='utf-8'))
            if (asr_evidence.get('status') != 'succeeded' or asr_evidence.get('task') != 'asr' or
                    asr_evidence.get('inputs', {}).get('audio', {}).get('sha256') != result['inputs']['audio']['sha256'] or
                    asr_evidence.get('inputs', {}).get('text', {}).get('sha256') != result['inputs']['text']['sha256']):
                raise ValueError('ASR 证据不是同一音频和正文，拒绝复用')
            result['asr_evidence_sha256'] = sha256(args.asr_result)
        save_result(output_dir, result)
        stage = "runtime_import"
        for package in ("qwen-asr", "torch", "transformers", "accelerate", "soundfile", "numpy"):
            try:
                result["versions"][package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                result["versions"][package] = None

        import torch as torch_module
        torch = torch_module
        from qwen_asr import Qwen3ASRModel, Qwen3ForcedAligner

        if not torch.cuda.is_available():
            raise RuntimeError("CUDA is unavailable; refusing to substitute CPU inference")
        if not torch.cuda.is_bf16_supported():
            raise RuntimeError("GPU does not support requested bfloat16 inference")
        torch.cuda.set_device(0)
        result["gpu"] = {"name": torch.cuda.get_device_name(0), "cuda_version": torch.version.cuda,
                         "total_memory_bytes": int(torch.cuda.get_device_properties(0).total_memory)}
        torch.cuda.synchronize(0)
        torch.cuda.reset_peak_memory_stats(0)
        stage = "model_load"
        operation_start = time.perf_counter()
        kwargs = {"dtype": torch.bfloat16, "device_map": "cuda:0",
                  "attn_implementation": "sdpa", "local_files_only": True}
        if args.task == "asr":
            model = Qwen3ASRModel.from_pretrained(
                str(model_path), max_inference_batch_size=1, max_new_tokens=2048, **kwargs)
        else:
            model = Qwen3ForcedAligner.from_pretrained(str(model_path), **kwargs)
        torch.cuda.synchronize(0)
        result["timings_seconds"]["model_load"] = time.perf_counter() - operation_start
        save_result(output_dir, result)

        stage = "inference"
        operation_start = time.perf_counter()
        with torch.inference_mode():
            raw = infer(model, args.task, audio, text, asr_evidence)
        # Preserve the model output before any metrics collection or downstream validation.
        with (output_dir / "原始输出.json").open("x", encoding="utf-8") as stream:
            json.dump(raw, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        result.update(raw)
        save_result(output_dir, result)
        torch.cuda.synchronize(0)
        result["timings_seconds"]["inference"] = time.perf_counter() - operation_start
        result["cuda_memory"] = cuda_peaks(torch)
        result["status"] = "succeeded"
        result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        result["timings_seconds"]["total"] = time.perf_counter() - start
        save_result(output_dir, result)
        print(f"Saved {args.task} inference evidence: {output_dir / '结果.json'}")
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        result["status"] = "failed"
        result["error"] = {"type": type(exc).__name__, "message": str(exc),
                           "stage": stage, "traceback": traceback.format_exc()}
        if operation_start is not None and stage in ("model_load", "inference"):
            result["timings_seconds"][stage] = time.perf_counter() - operation_start
        result["timings_seconds"]["total"] = time.perf_counter() - start
        result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        if torch is not None:
            try:
                if torch.cuda.is_initialized():
                    result["cuda_memory"] = cuda_peaks(torch)
            except Exception as metric_error:
                result["cuda_metrics_error"] = str(metric_error)
        try:
            save_result(output_dir, result)
        except Exception:
            traceback.print_exc()
        print(result["error"]["traceback"], file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
