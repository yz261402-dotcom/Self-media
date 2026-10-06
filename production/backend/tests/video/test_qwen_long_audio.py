"""长音频按真实 ASR 边界匹配原稿，时间戳只能来自各段模型对齐。"""
import importlib.util
from pathlib import Path
import sys

import pytest

WORKER = Path(__file__).resolve().parents[3] / 'workers/m2_gpu'


def load(name):
    spec = importlib.util.spec_from_file_location(name, WORKER / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_duration_has_no_total_ceiling():
    runner = load('qwen_inference')
    assert runner.validate_duration(32000 * 3601, 32000) == 3601
    assert runner.validate_duration(16000, 32000) == 0.5
    with pytest.raises(ValueError):
        runner.validate_duration(0, 32000)


def test_reference_chunks_cover_original_and_preserve_punctuation():
    module = load('qwen_audio_chunks')
    text = '你问它，它回答。\n\n它讲得清楚吗？最后聊聊 AI。'
    transcripts = ['你问他他回答', '他讲的清楚吗', '最后聊聊AI']
    pieces = module.partition_reference(text, transcripts)
    assert ''.join(pieces) == text
    assert pieces == ['你问它，它回答。\n\n', '它讲得清楚吗？', '最后聊聊 AI。']


def test_repeated_content_keeps_all_reference_occurrences():
    module = load('qwen_audio_chunks')
    text = '你好，今天聊人工智能。' * 6
    pieces = module.partition_reference(text, ['你好今天聊人工智能' * 2] * 3)
    assert len(pieces) == 3 and ''.join(pieces) == text


def test_unrelated_recognition_cannot_allocate_reference_by_length():
    module = load('qwen_audio_chunks')
    with pytest.raises(ValueError, match='ASR'):
        module.partition_reference('我们讨论检索增强生成，而不是天气。', ['今天风和日丽', '明天适合出游'])


def test_offset_uses_sample_positions_and_refuses_out_of_chunk_timestamps():
    module = load('qwen_audio_chunks')
    actual = module.offset_alignment([{'text': '你', 'start': 0.2, 'end': 0.5}], 3840000, 32000, 64000)
    assert actual == [{'text': '你', 'start': 120.2, 'end': 120.5}]
    with pytest.raises(ValueError):
        module.offset_alignment([{'text': '你', 'start': 0.2, 'end': 2.1}], 3840000, 32000, 64000)


def test_long_alignment_uses_real_sample_offsets_without_truncating_script(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.syspath_prepend(str(WORKER))
    runner = load('qwen_inference')
    calls = []

    class Model:
        def align(self, *, audio, text, language):
            calls.append((len(audio[0]), text))
            return [[SimpleNamespace(text=text.strip('。'), start_time=0.5, end_time=2.0)]]

    evidence = {'chunks': [
        {'start_sample': 0, 'end_sample': 120000, 'sample_rate': 1000, 'text': '我们讨论检索'},
        {'start_sample': 120000, 'end_sample': 240000, 'sample_rate': 1000, 'text': '接着解释生成'},
    ]}
    result = runner.infer(Model(), 'align', (range(240000), 1000), '我们讨论检索。接着解释生成。', evidence)
    assert calls == [(120000, '我们讨论检索。'), (120000, '接着解释生成。')]
    assert result['alignment'][1]['start'] == 120.5
    assert ''.join(x['reference'] for x in result['chunks']) == '我们讨论检索。接着解释生成。'
    evidence['chunks'][-1]['end_sample'] -= 1
    with pytest.raises(ValueError, match='尾部'):
        runner.infer(Model(), 'align', (range(240000), 1000), '我们讨论检索。接着解释生成。', evidence)
