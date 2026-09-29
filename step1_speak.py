"""
STEP 1 - Give Jarvis a voice.

Run:   python step1_speak.py
You should hear your PC say two sentences out loud.

VOICE_ENGINE = "windows" uses the voices built into Windows (usually "David" and "Zira"). Works offline.
VOICE_ENGINE = "edge"    uses Microsoft's natural neural voices (needs internet). Try it: it is the
                         closest free thing to a movie-style Jarvis.
"""
import os
import tempfile
import time

VOICE_ENGINE = "windows"           # "windows" or "edge"
VOICE = 0                          # windows: 0 = first voice (usually David), 1 = second (usually Zira)
RATE = 175                         # windows: speaking speed in words per minute
EDGE_VOICE = "en-GB-RyanNeural"    # edge: calm British male. Also try "en-GB-ThomasNeural".
EDGE_RATE = "-8%"                  # edge: a touch slower
EDGE_PITCH = "-4Hz"                # edge: a touch deeper


def speak_windows(text: str) -> None:
    import pyttsx3
    engine = pyttsx3.init()
    voices = engine.getProperty("voices")
    if voices and VOICE < len(voices):
        engine.setProperty("voice", voices[VOICE].id)
    engine.setProperty("rate", RATE)
    engine.say(text)
    engine.runAndWait()
    engine.stop()


def speak_edge(text: str) -> None:
    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    import edge_tts
    import pygame
    path = os.path.join(tempfile.gettempdir(), "jarvis_voice.mp3")
    edge_tts.Communicate(text, EDGE_VOICE, rate=EDGE_RATE, pitch=EDGE_PITCH).save_sync(path)
    if not pygame.mixer.get_init():
        pygame.mixer.init()
    pygame.mixer.music.load(path)
    pygame.mixer.music.play()
    while pygame.mixer.music.get_busy():
        time.sleep(0.05)
    pygame.mixer.music.unload()


def speak(text: str) -> None:
    """Say the text out loud, and also print it so you can read it."""
    print("Jarvis:", text)
    if VOICE_ENGINE == "edge":
        try:
            speak_edge(text)
            return
        except Exception as error:
            print(f"(neural voice unavailable: {error}. Using the Windows voice instead.)")
    speak_windows(text)


if __name__ == "__main__":
    if VOICE_ENGINE == "windows":
        import pyttsx3
        available = pyttsx3.init().getProperty("voices")
        print("Voices installed on this PC:", ", ".join(v.name for v in available) or "(none found)")
    else:
        print("Using the neural voice", EDGE_VOICE)
    speak("Good evening. I am Jarvis. Your computer can talk now.")
    speak("Step one is done. Shall we give me some ears next?")
