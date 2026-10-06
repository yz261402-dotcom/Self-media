"""按真实识别分段映射冻结稿，保留原始采样位置与模型时间。"""
import difflib
import math
import unicodedata


def _normalized(text):
    chars, indices = [], []
    for index, char in enumerate(text):
        if char.isspace() or unicodedata.category(char).startswith('P'):
            continue
        for lowered in char.casefold():
            chars.append(lowered)
            indices.append(index)
    return ''.join(chars), indices


def partition_reference(reference, transcripts):
    """只用识别文字匹配边界；无法匹配即报错，不按字数均分原稿。"""
    target, positions = _normalized(reference)
    recognized = [_normalized(text)[0] for text in transcripts]
    source = ''.join(recognized)
    if not target or not recognized or any(not chunk for chunk in recognized):
        raise ValueError('ASR 分段或冻结稿为空，无法确定长音频分段边界')
    match = difflib.SequenceMatcher(a=source, b=target, autojunk=False)
    if match.ratio() < 0.7:
        raise ValueError('ASR 与冻结稿差异过大，无法可靠分段，请先核对音频和文稿')
    cuts = [0]
    source_cursor = 0
    for chunk in recognized[:-1]:
        source_cursor += len(chunk)
        mapped = None
        for tag, a0, a1, b0, b1 in match.get_opcodes():
            if a0 <= source_cursor <= a1:
                if tag == 'equal':
                    mapped = b0 + source_cursor - a0
                elif source_cursor == a1:
                    mapped = b1
                elif source_cursor == a0:
                    mapped = b0
                break
        if mapped is None or not cuts[-1] < mapped < len(target):
            raise ValueError('ASR 分段边界落在无法匹配的文字内，拒绝估算时间或删减正文')
        cuts.append(mapped)
    cuts.append(len(target))
    for chunk, begin, end in zip(recognized, cuts, cuts[1:]):
        if difflib.SequenceMatcher(a=chunk, b=target[begin:end], autojunk=False).ratio() < 0.6:
            raise ValueError('ASR 局部分段与正文不符，请核对该段音频')
    offsets = [0] + [positions[cut] for cut in cuts[1:-1]] + [len(reference)]
    return [reference[start:end] for start, end in zip(offsets, offsets[1:])]


def offset_alignment(items, start_sample, sample_rate, frame_count):
    if start_sample < 0 or sample_rate <= 0 or frame_count <= 0 or not items:
        raise ValueError('音频分段采样位置或对齐结果非法')
    duration = frame_count / sample_rate
    offset = start_sample / sample_rate
    output, previous_end = [], 0.0
    for item in items:
        start, end = float(item['start']), float(item['end'])
        if not (math.isfinite(start) and math.isfinite(end) and
                0 <= start <= end <= duration and start >= previous_end):
            raise ValueError('分段对齐结果越界、倒序或重叠，不能拼接为全局字幕')
        output.append({'text': item['text'], 'start': offset + start, 'end': offset + end})
        previous_end = end
    return output
