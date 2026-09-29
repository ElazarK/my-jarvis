"""
STEP 1 - Give Jarvis a voice.

Run:   python step1_speak.py
You should hear your PC say two sentences out loud.

It uses the voices that are already built into Windows (usually "David" and "Zira").
"""
import pyttsx3

VOICE = 0       # 0 = the first Windows voice (usually David), 1 = the second (usually Zira)
RATE = 175      # speaking speed in words per minute. 150 is slower, 200 is faster.


def speak(text: str) -> None:
    """Say the text out loud, and also print it so you can read it."""
    print("Jarvis:", text)
    engine = pyttsx3.init()
    voices = engine.getProperty("voices")
    if voices and VOICE < len(voices):
        engine.setProperty("voice", voices[VOICE].id)
    engine.setProperty("rate", RATE)
    engine.say(text)
    engine.runAndWait()
    engine.stop()


if __name__ == "__main__":
    available = pyttsx3.init().getProperty("voices")
    print("Voices installed on this PC:", ", ".join(v.name for v in available) or "(none found)")
    speak("Hello. I am Jarvis. Your computer can talk now.")
    speak("Step one is done. Let's give me some ears next.")
