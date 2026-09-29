"""
STEP 2 - Give Jarvis ears.

Run:   python step2_listen.py
Press Enter, say a sentence, and watch your words appear as text.

The first run downloads the speech model (about 150 MB). Be patient, it only happens once.
Everything runs on your own PC - nothing is sent to the cloud.

If Windows never hears you: Settings > Privacy & security > Microphone,
and make sure desktop apps are allowed to use the microphone.
"""
import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel

SAMPLE_RATE = 16000     # Whisper likes 16,000 samples per second
LANGUAGE = "en"         # "he" = Hebrew, "es" = Spanish, "fr" = French ... or None = auto-detect
MODEL_SIZE = "base"     # "tiny" = fastest, "base" = good balance, "small" = more accurate but slower

print("Loading the speech model...")
model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")


def loudness(audio) -> float:
    """How loud a piece of audio is (0 = silence)."""
    return float(np.sqrt(np.mean(audio ** 2)))


def measure_background_noise(seconds: float = 1.0) -> float:
    """Listen to the room for a moment so we know what 'quiet' sounds like."""
    audio = sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype="float32")
    sd.wait()
    return loudness(audio)


def record(threshold: float, max_seconds: float = 15, silence_seconds: float = 1.2,
           wait_seconds: float = 6):
    """
    Record from the microphone until you stop talking.
    Returns the audio, or None if nobody said anything.
    """
    chunk_seconds = 0.1
    chunk = int(SAMPLE_RATE * chunk_seconds)
    frames = []
    heard_speech = False
    quiet_time = 0.0
    elapsed = 0.0
    peak = 0.0

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=chunk) as stream:
        while elapsed < max_seconds:
            data, _ = stream.read(chunk)
            data = data[:, 0]
            frames.append(data)
            elapsed += chunk_seconds

            level = loudness(data)
            peak = max(peak, level)
            if level > threshold:
                heard_speech = True
                quiet_time = 0.0
            else:
                quiet_time += chunk_seconds

            if heard_speech and quiet_time >= silence_seconds:
                break                                   # you finished your sentence
            if not heard_speech and elapsed >= wait_seconds:
                break                                   # nobody spoke

    print(f"(loudest moment: {peak:.4f}, threshold: {threshold:.4f})")
    if not heard_speech:
        return None
    return np.concatenate(frames)


def transcribe(audio) -> str:
    """Turn recorded audio into text."""
    segments, _ = model.transcribe(audio, language=LANGUAGE, beam_size=1, vad_filter=True)
    return " ".join(segment.text.strip() for segment in segments).strip()


if __name__ == "__main__":
    print("Calibrating - please stay quiet for a second...")
    noise = measure_background_noise()
    threshold = max(0.005, noise * 3)
    print(f"Background noise: {noise:.4f}  ->  I will treat anything above {threshold:.4f} as speech.")

    try:
        while True:
            input("\nPress Enter, then say something (Ctrl+C to quit)... ")
            print("Listening...")
            audio = record(threshold)
            if audio is None:
                print("I didn't hear anything. Speak a little louder, or closer to the microphone.")
                continue
            print("Got it, transcribing...")
            text = transcribe(audio)
            print("You said:", text if text else "(nothing I could understand)")
    except KeyboardInterrupt:
        print("\nBye!")
