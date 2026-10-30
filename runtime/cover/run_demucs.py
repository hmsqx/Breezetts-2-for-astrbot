"""Run Demucs using soundfile for WAV output with recent torchaudio."""

import soundfile as sf
import torchaudio
from demucs.separate import main


def save_wav(
    path, waveform, sample_rate, *, encoding=None, bits_per_sample=16, **kwargs
):
    if not str(path).lower().endswith(".wav"):
        raise ValueError("This Demucs runner only supports WAV output")
    subtype = "FLOAT" if encoding == "PCM_F" else f"PCM_{bits_per_sample}"
    sf.write(
        path,
        waveform.detach().cpu().transpose(0, 1).numpy(),
        sample_rate,
        subtype=subtype,
    )


torchaudio.save = save_wav
main()
