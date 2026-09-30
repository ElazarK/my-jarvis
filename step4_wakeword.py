"""
STEP 4 - Teach Jarvis its name (the wake word).

Run:   python step4_wakeword.py
Say "Hey Jarvis" and watch the score bar jump. Press Ctrl+C to quit.

Needs one extra package:   pip install openwakeword
The first run downloads a few small model files (about 10 MB) and then works offline.

How it works: openWakeWord listens to the microphone in 80 millisecond slices and gives each
slice a score from 0 to 1 that says "how much did that sound like Hey Jarvis". Above the
SENSITIVITY line, Jarvis reacts. Nothing is transcribed and nothing leaves your PC.
"""
import numpy as np
import sounddevice as sd
import openwakeword
import openwakeword.utils
from openwakeword.model import Model

SENSITIVITY = 0.5       # 0.3 = reacts more easily (more false alarms), 0.7 = stricter
SAMPLE_RATE = 16000
FRAME = 1280            # 80 ms of audio, the slice size openWakeWord expects

print("Getting the wake-word model (downloaded once, then kept)...")
openwakeword.utils.download_models(model_names=["hey_jarvis"])
model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")

print('\nListening. Say "Hey Jarvis"...   (Ctrl+C to quit)\n')
try:
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=FRAME) as stream:
        while True:
            data, _ = stream.read(FRAME)
            score = max(model.predict(data[:, 0]).values())
            bar = "#" * int(score * 40)
            print(f"\r score {score:.2f} |{bar:<40}|", end="", flush=True)
            if score >= SENSITIVITY:
                print(f"\n>>> Heard you! (score {score:.2f})  Say it again, or Ctrl+C to quit.\n")
                model.reset()                           # start fresh so one shout is not counted twice
except KeyboardInterrupt:
    print("\nBye!")
