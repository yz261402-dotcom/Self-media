"""Cloud input guards for native audio ASR; no GPU inference is performed."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import wave

import numpy as np
import pytest


WORKER = Path(__file__).resolve().parents[3] / "workers/m2_gpu"


@pytest.fixture()
def inputs(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("qwen_inference_inputs", WORKER / "qwen_inference.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    audio = tmp_path / "audio.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\x00\x00" * 160)
    text = tmp_path / "text.txt"
    models = tmp_path / "models"
    for name in runner.MODEL_NAMES.values():
        (models / name).mkdir(parents=True)

    # Soundfile lives in the remote GPU environment. Only its decode boundary
    # is replaced here; the production file/text/model guards remain real.
    decoded = np.zeros((160, 1), dtype=np.float32)
    monkeypatch.setitem(sys.modules, "soundfile", SimpleNamespace(
        info=lambda path: SimpleNamespace(frames=160, samplerate=16000),
        read=lambda path, **kwargs: (decoded, 16000),
    ))
    args = SimpleNamespace(audio=audio, text=text, models_root=models, task="asr")
    result = {"inputs": {"audio": {}, "text": {}}}
    return runner, args, result


@pytest.mark.parametrize("script", ["", " \n<#0.30#>\t"])
def test_asr_accepts_no_reference_script(inputs, script):
    runner, args, result = inputs
    args.text.write_text(script, encoding="utf-8")

    audio, text, model = runner.validate_inputs(args, result)

    assert text == ""
    assert model.name == "Qwen3-ASR-1.7B"
    assert audio[1] == 16000
    assert result["audio"]["duration_seconds"] == .01
    assert "alignment_text" not in result


@pytest.mark.parametrize("script", ["", " \n\t", "<#0.30#>"])
def test_aligner_rejects_empty_or_pause_only_reference_script(inputs, script):
    runner, args, result = inputs
    args.task = "align"
    args.text.write_text(script, encoding="utf-8")

    with pytest.raises(ValueError, match="text is empty"):
        runner.validate_inputs(args, result)

    assert "audio" not in result
