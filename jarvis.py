"""
JARVIS - a voice assistant that runs on your own Windows PC.

  Ears  : your microphone + Whisper turn your speech into text
  Brain : a local AI model (served by Ollama) decides what to answer or which tool to use
  Hands : small Python functions ("tools") that the brain can call
  Voice : the text-to-speech voices built into Windows read the answer aloud

Run:   python jarvis.py
Press Enter to talk, or type a message instead. Say "goodbye" to stop.

Hands-free: set WAKE_WORD = True below and say "Hey Jarvis" instead of pressing Enter (Step 9 of the guide).
A face:     run  python jarvis_hud.py  for a window that shows what Jarvis hears and says (Step 10).
Manners:    ADDRESS_AS = "sir" makes it answer the way a butler would (Step 11).
Learning:   LEARNING = True lets it write, test and keep new tools ("skills") of its own when it lacks one (Step 12).
"""
import ast
import collections
import datetime
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser

import numpy as np
import pyttsx3
import requests
import sounddevice as sd
from faster_whisper import WhisperModel
from openai import OpenAI, APIConnectionError, NotFoundError

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")       # so every language prints cleanly

# =====================================================================
#  SETTINGS - the only part most people ever need to change
# =====================================================================
NAME = "Jarvis"
MODEL = "llama3.2"          # the brain. On a strong PC try "llama3.1:8b" or "qwen2.5:7b"
WHISPER_SIZE = "base"       # the ears. "tiny" = fastest, "small" = more accurate
VOICE_ENGINE = "windows"    # "windows" = the voices built into Windows (works offline)
                            # "edge"    = Microsoft's natural neural voices, the closest free thing to a movie Jarvis
                            #             (needs internet; falls back to the Windows voice if it cannot connect)
VOICE = 0                   # windows engine: 0 = first Windows voice (usually David), 1 = second (usually Zira)
RATE = 175                  # windows engine: speaking speed in words per minute
EDGE_VOICE = "en-GB-RyanNeural"   # edge engine: calm British male. Also try "en-GB-ThomasNeural".
EDGE_RATE = "-8%"                 # edge engine: a touch slower than normal
EDGE_PITCH = "-4Hz"               # edge engine: a touch deeper than normal
PERSONALITY = "calm, precise and impeccably polite, like a British butler with a dry wit"
ADDRESS_AS = "sir"          # how Jarvis addresses you: "sir", "ma'am", your first name, or "" for no title at all
LANGUAGE = "en"             # "he" = Hebrew, "es" = Spanish, "fr" = French ... or None = auto-detect
THRESHOLD = None            # microphone sensitivity. None = measure automatically at start-up
SAMPLE_RATE = 16000

# Hands-free (Step 9). Needs:  pip install openwakeword
WAKE_WORD = False           # True = no Enter key: say "Hey Jarvis" and then talk, like a smart speaker
WAKE_SENSITIVITY = 0.5      # how sure Jarvis must be that it heard its name: 0.3 = eager, 0.7 = strict
FOLLOW_UP_SECONDS = 8       # after an answer Jarvis keeps listening this long, so you can reply without the wake word
INTERRUPTIBLE = True        # True = say "Hey Jarvis" (or press Space in the window, or Enter in the terminal) while Jarvis
                            #        is talking and it stops mid-sentence and listens to you
PAUSE_SECONDS = 1.0         # how long you must be quiet before Jarvis decides your sentence is over (1.2 = patient, 0.7 = snappy)
SHOW_TIMINGS = True         # True = after each answer, print where the time went: ears, brain, voice
LEARNING = True             # True = when Jarvis has no tool for what you ask, it may write one itself (a "skill"), test it and keep it
ASK_BEFORE_LEARNING = True  # True = Jarvis asks "Shall I write one?" and waits for your yes. False = it just goes ahead
SKILLS_FOLDER = "skills"    # learned skills live here, next to this file, one small Python file each. Delete a file to forget a skill

# Optional: smart home control through Home Assistant. Leave empty to skip.
HA_URL = ""                 # for example "http://homeassistant.local:8123"
HA_TOKEN = ""               # a Long-Lived Access Token from your Home Assistant profile page
SMART_HOME_DEVICES = {      # "what you call it": "Home Assistant entity id"
    # "living room light": "light.living_room",
    # "desk lamp": "switch.desk_lamp",
}

MANNERS = (
    f"Address the user as '{ADDRESS_AS}' the way a trusted butler would, naturally and not in every sentence: "
    f"for example 'Yes, {ADDRESS_AS}.', 'Right away, {ADDRESS_AS}.', 'Very good, {ADDRESS_AS}.' "
    "Be formal, understated and brief, with the occasional dry remark; never gush, never use exclamation marks, "
    "and never mention that you are an AI model. "
) if ADDRESS_AS else ""

SYSTEM_PROMPT = (
    f"You are {NAME}, a voice assistant running on the user's Windows PC. You are {PERSONALITY}. "
    + MANNERS +
    "Your answers are read aloud, so keep them short: one to three sentences, plain text only, "
    "no lists, no markdown, no emojis. Use a tool only when the request clearly needs it; "
    "when no tool is needed, simply answer in words and never write JSON. Do each action once: "
    "if the user comments on what you just did, or thanks you, reply briefly in words instead of doing it again; "
    "repeat an action only when the user clearly asks for it again. "
    f"Today is {datetime.date.today():%A, %d %B %Y}."
)

# =====================================================================
#  EVENTS - how other windows (the HUD in jarvis_hud.py) follow what Jarvis is doing
# =====================================================================
listeners = []                      # functions that want to be told what is happening: listener(event, value)
talk_now = threading.Event()        # another window can set this to say "listen to me now" (the HUD's Space key)
STOP = False                        # another window sets this to True to make Jarvis shut down cleanly


def addr() -> str:
    """The form of address, ready to drop into a sentence: 'Goodbye' + addr() + '.' -> 'Goodbye, sir.'"""
    return f", {ADDRESS_AS}" if ADDRESS_AS else ""


def greeting() -> str:
    """'Good morning, sir. Jarvis at your service.' (the time of day decides the first word)."""
    hour = datetime.datetime.now().hour
    part = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
    return f"{part}{addr()}. {NAME} at your service."


stopwatch = {}                      # how long the ears, the brain and the voice took on the last answer (SHOW_TIMINGS)


def notify(event: str, value="") -> None:
    """Tell every listener what is happening. Events: state (standby, listening, thinking, speaking, off),
    you (what you said), jarvis (what Jarvis says), tool (which tool runs), level (how loud you are), info."""
    for listener in list(listeners):
        try:
            listener(event, value)
        except Exception:
            pass                                    # a display problem must never stop Jarvis


# =====================================================================
#  1. VOICE
# =====================================================================
interrupted = threading.Event()     # set by interrupt(): Jarvis stops talking and listens
speaking = threading.Event()        # set while Jarvis is talking (the wake-word watcher and the HUD read it)


def interrupt() -> None:
    """Cut Jarvis off mid-sentence. Called when you say the wake word or press Space while it talks."""
    if speaking.is_set():
        interrupted.set()


def play_file(path: str) -> None:
    """Play a sound file through pygame and wait, stopping at once if interrupt() is called."""
    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    import pygame
    if not pygame.mixer.get_init():
        pygame.mixer.init()
    if path.lower().endswith(".wav"):                   # the Windows voice: a sound object converts the format for us
        channel = pygame.mixer.Sound(path).play()
        while channel is not None and channel.get_busy():
            if interrupted.is_set():
                channel.stop()
                break
            time.sleep(0.03)
        return
    pygame.mixer.music.load(path)                       # the neural voice: an mp3 stream
    pygame.mixer.music.play()
    while pygame.mixer.music.get_busy():
        if interrupted.is_set():
            pygame.mixer.music.stop()
            break
        time.sleep(0.03)
    pygame.mixer.music.unload()


def speak_windows(text: str) -> None:
    """The voices built into Windows. Works offline. The speech is written to a small sound file first and
    played from there, so that it can be cut off when you interrupt (a live Windows voice cannot be)."""
    engine = pyttsx3.init()
    voices = engine.getProperty("voices")
    if voices and VOICE < len(voices):
        engine.setProperty("voice", voices[VOICE].id)
    engine.setProperty("rate", RATE)
    path = os.path.join(tempfile.gettempdir(), "jarvis_voice.wav")
    try:
        engine.save_to_file(text, path)
        engine.runAndWait()
        engine.stop()
        play_file(path)
    except Exception:                               # no pygame, or the file route failed: speak live, uninterruptible
        engine.say(text)
        engine.runAndWait()
        engine.stop()


def sentences_of(text: str) -> list:
    """Splits an answer into sentences, keeping very short ones attached to the next, so the voice can
    start on the first sentence while the rest is still being prepared."""
    parts, current = [], ""
    for piece in re.split(r"(?<=[.!?])\s+", text.strip()):
        current = f"{current} {piece}".strip()
        if len(current) >= 40:
            parts.append(current)
            current = ""
    if current:
        parts.append(current)
    return parts or [text]


def speak_edge(text: str) -> None:
    """Microsoft's neural voices through the edge-tts package. Needs internet. Each sentence is fetched while
    the previous one plays, so you hear the first words about as soon as a one-sentence answer would start."""
    import edge_tts

    def fetch(index: int, sentence: str) -> str:
        path = os.path.join(tempfile.gettempdir(), f"jarvis_voice_{index % 2}.mp3")
        edge_tts.Communicate(sentence, EDGE_VOICE, rate=EDGE_RATE, pitch=EDGE_PITCH).save_sync(path)
        return path

    parts = sentences_of(text)
    path = fetch(0, parts[0])
    for index, _ in enumerate(parts):
        outcome = {}
        worker = None
        if index + 1 < len(parts):                      # prepare the next sentence in the background

            def prepare(i=index + 1):
                try:
                    outcome["path"] = fetch(i, parts[i])
                except Exception as error:
                    outcome["error"] = error

            worker = threading.Thread(target=prepare, daemon=True)
            worker.start()
        play_file(path)
        if interrupted.is_set() or worker is None:
            break
        worker.join()
        if "error" in outcome:
            raise outcome["error"]
        path = outcome["path"]


def speak(text: str) -> None:
    """Say the text out loud (and print it) with whichever voice engine is selected.
    Sets `interrupted` if you cut it off; the caller decides what to do about that."""
    print(f"{NAME}: {text}")
    notify("jarvis", text)
    notify("state", "speaking")
    interrupted.clear()
    speaking.set()
    stopwatch["voice_started"] = time.time()
    try:
        if VOICE_ENGINE == "edge":
            try:
                speak_edge(text)
                return
            except Exception as error:          # no internet, or the packages are missing
                print(f"(neural voice unavailable: {error}. Using the Windows voice instead.)")
        speak_windows(text)
    except Exception as error:                  # a voice problem should never crash Jarvis
        print(f"(voice unavailable: {error})")
    finally:
        speaking.clear()
        if interrupted.is_set():
            print("(interrupted)")


def chime(wait: bool = True) -> None:
    """A short two-note sound that means 'I heard my name, go ahead'. Made from numbers, no file needed.
    wait=False plays it in the background so Jarvis can keep listening while it sounds."""
    try:
        t = np.linspace(0, 0.09, int(SAMPLE_RATE * 0.09), endpoint=False)
        fade = np.linspace(1, 0, t.size)
        tone = np.concatenate([np.sin(2 * np.pi * f * t) * fade for f in (880, 1320)]) * 0.18
        sd.play(tone.astype("float32"), SAMPLE_RATE)
        if wait:
            sd.wait()
    except Exception:
        pass


# =====================================================================
#  2. EARS
# =====================================================================
OLLAMA_URL = "http://localhost:11434/v1"       # where the brain (Ollama) listens


def keep_brain_warm() -> None:
    """Loads the model into memory while the ears are still loading, and nudges it every few minutes so Ollama
    does not unload it (it normally does after five idle minutes, which makes the next answer slow)."""
    while not STOP:
        try:
            requests.post(OLLAMA_URL.replace("/v1", "") + "/api/generate", json={"model": MODEL, "keep_alive": "15m"}, timeout=120)
        except Exception:
            pass                                        # Ollama is not running yet; safe_think() will say so
        for _ in range(240):                            # four minutes, in small steps so shutdown is quick
            if STOP:
                return
            time.sleep(1)


threading.Thread(target=keep_brain_warm, daemon=True).start()     # the brain loads while the ears load

print("Loading the speech model (the first run downloads about 150 MB)...")
whisper = WhisperModel(WHISPER_SIZE, device="cpu", compute_type="int8")


def loudness(audio) -> float:
    """How loud a piece of audio is (0 = silence)."""
    return float(np.sqrt(np.mean(audio ** 2)))


def measure_background_noise(seconds: float = 1.0) -> float:
    """Listen to the room for a moment so we know what 'quiet' sounds like."""
    audio = sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype="float32")
    sd.wait()
    return loudness(audio)


def collect_speech(read_chunk, chunk_seconds: float, threshold: float, max_seconds: float = 15,
                   silence_seconds: float = None, wait_seconds: float = 6, start_with=(), ignore_seconds: float = 0.0):
    """The listening rule shared by every way of talking to Jarvis: keep taking small chunks of sound
    until you have spoken and then gone quiet. read_chunk() returns one chunk as 1-D float32 audio.
    start_with = sound to keep in front of the recording (what was said just before the wake word was
    recognised). ignore_seconds = how long at the start to ignore loudness (while the chime plays).
    Returns the audio, or None if nobody spoke."""
    if silence_seconds is None:
        silence_seconds = PAUSE_SECONDS
    frames, heard_speech, quiet_time, elapsed = list(start_with), False, 0.0, 0.0
    while elapsed < max_seconds:
        data = read_chunk()
        frames.append(data)
        elapsed += chunk_seconds
        if elapsed <= ignore_seconds:
            continue

        level = loudness(data)
        notify("level", level / threshold if threshold else 0.0)
        if level > threshold:
            heard_speech, quiet_time = True, 0.0
        else:
            quiet_time += chunk_seconds

        if heard_speech and quiet_time >= silence_seconds:
            break                                       # you finished your sentence
        if not heard_speech and elapsed >= wait_seconds + ignore_seconds:
            break                                       # nobody spoke
    return np.concatenate(frames) if heard_speech else None


def record(threshold: float, max_seconds: float = 15, silence_seconds: float = None,
           wait_seconds: float = 6):
    """Record from the microphone until you stop talking. Returns None if nobody spoke."""
    chunk_seconds = 0.1
    chunk = int(SAMPLE_RATE * chunk_seconds)
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=chunk) as stream:
        return collect_speech(lambda: stream.read(chunk)[0][:, 0], chunk_seconds, threshold,
                              max_seconds, silence_seconds, wait_seconds)


def transcribe(audio) -> str:
    """Turn recorded audio into text."""
    started = time.time()
    segments, _ = whisper.transcribe(audio, language=LANGUAGE, beam_size=1, vad_filter=True)
    text = " ".join(segment.text.strip() for segment in segments).strip()
    stopwatch["ears"] = time.time() - started
    return text


def listen(threshold: float, wait_seconds: float = 6, prompt: str = "Listening... (speak now)") -> str:
    """Record one sentence and return it as text ("" if nothing was heard)."""
    print(prompt)
    notify("state", "listening")
    audio = record(threshold, wait_seconds=wait_seconds)
    if audio is None:
        return ""
    return transcribe(audio)


# =====================================================================
#  2b. WAKE WORD - "Hey Jarvis" instead of the Enter key (Step 9). Needs the openwakeword package.
# =====================================================================
def load_wake_word_model():
    """Loads the ready-made 'hey jarvis' model from openWakeWord. Returns None if that is not possible."""
    try:
        import openwakeword
        import openwakeword.utils
        from openwakeword.model import Model
    except ImportError:
        print("(the openwakeword package is not installed. Run:  pip install openwakeword  and start me again.)")
        return None
    try:
        openwakeword.utils.download_models(model_names=["hey_jarvis"])   # first run only: a few small files
        return Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
    except Exception as error:
        print(f"(the wake word is not available: {error})")
        return None


WAKE_FRAME = 1280           # 80 ms at 16 kHz, the slice openWakeWord expects
PRE_ROLL_SECONDS = 0.4      # sound kept from just before the name was recognised, so a fast first word is not lost
CHIME_SECONDS = 0.3         # how long the chime is ignored by the "did somebody speak" rule


def wait_for_wake_word(model, stream, pre_roll) -> bool:
    """Read 80 ms slices from the open microphone stream until you say 'Hey Jarvis' (or talk_now is set).
    The last slices are kept in pre_roll. Returns False when STOP was requested."""
    model.reset()                                       # forget older sound, including Jarvis's own voice
    while not STOP:
        if talk_now.is_set():                           # the HUD's Space key works in this mode too
            talk_now.clear()
            pre_roll.clear()                            # nothing useful was said before a key press
            return True
        data, _ = stream.read(WAKE_FRAME)
        pre_roll.append(data[:, 0])
        score = max(model.predict(data[:, 0]).values())
        if score >= WAKE_SENSITIVITY:
            print(f"(heard my name, score {score:.2f})")
            return True
    return False


def watch_for_interruption(model):
    """Runs on its own thread while Jarvis talks: if you say the wake word (or press Space), it cuts the voice."""
    try:
        pre_roll = collections.deque(maxlen=1)
        model.reset()
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=WAKE_FRAME) as stream:
            while speaking.is_set() and not interrupted.is_set() and not STOP:
                if talk_now.is_set():
                    talk_now.clear()
                    interrupt()
                    break
                data, _ = stream.read(WAKE_FRAME)
                # stricter than usual: the microphone also hears Jarvis's own voice through the speakers
                if max(model.predict(data[:, 0]).values()) >= min(0.95, WAKE_SENSITIVITY + 0.25):
                    print("(you called me while I was talking)")
                    interrupt()
                    break
    except Exception:
        pass                                            # the microphone is busy or missing: no interruption, no crash


def say(text: str, wake_model=None) -> bool:
    """speak(), listening for an interruption meanwhile. Returns True if you cut Jarvis off."""
    watcher = None
    if INTERRUPTIBLE and wake_model is not None:
        speaking.set()                                  # so the watcher does not give up before the voice starts
        watcher = threading.Thread(target=watch_for_interruption, args=(wake_model,), daemon=True)
        watcher.start()
    speak(text)
    if watcher is not None:
        watcher.join(timeout=1)
    if talk_now.is_set() and INTERRUPTIBLE:            # Space was pressed during a non-wake-word run
        talk_now.clear()
        return True
    return interrupted.is_set()


def strip_name(text: str) -> str:
    """'Hey Jarvis, what time is it?' -> 'what time is it?' (the name often ends up in the recording)."""
    return re.sub(rf"^\W*(hey|hi|ok|okay)?\W*{re.escape(NAME)}\W*", "", text, count=1, flags=re.IGNORECASE).strip()


def hands_free_listen(model, threshold: float):
    """Wait for 'Hey Jarvis', then record your request on the same microphone stream, so nothing is lost
    between the name and the question. Works in one breath ('Hey Jarvis, what time is it?') or with a
    pause for the chime. Returns the text ("" if nothing was heard), or None when STOP was requested."""
    pre_roll = collections.deque(maxlen=max(1, int(PRE_ROLL_SECONDS * SAMPLE_RATE / WAKE_FRAME)))
    to_float = lambda samples: samples.astype("float32") / 32768.0
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=WAKE_FRAME) as stream:
        if not wait_for_wake_word(model, stream, pre_roll):
            return None
        chime(wait=False)                               # sounds while we keep listening
        print("Listening... (speak now)")
        notify("state", "listening")
        audio = collect_speech(lambda: to_float(stream.read(WAKE_FRAME)[0][:, 0]), WAKE_FRAME / SAMPLE_RATE,
                               threshold, start_with=[to_float(f) for f in pre_roll], ignore_seconds=CHIME_SECONDS)
    if audio is None:
        return ""
    heard = transcribe(audio)
    text = strip_name(heard)
    if heard and not text:                              # only the name was said: answer, and listen once more
        speak(f"Yes{addr()}?")
        return listen(threshold)
    return text


def wait_for_enter():
    """The classic way: press Enter to talk, or type a message. Returns the typed text ("" = listen)."""
    try:
        return input("\n> ").strip()
    except (KeyboardInterrupt, EOFError):
        return None                                     # None = shut down


def wait_for_signal():
    """Wait until another window (the HUD) sets talk_now, for example when you press Space there."""
    while not STOP:
        if talk_now.wait(0.1):
            talk_now.clear()
            return ""
    return None


# =====================================================================
#  3. HANDS - the tools Jarvis can use.  To add your own:
#     a) write a function,  b) add it to TOOL_FUNCTIONS,  c) describe it in TOOLS.
# =====================================================================
def get_current_time() -> str:
    return datetime.datetime.now().strftime("%A %d %B %Y, %H:%M")


def get_weather(city: str) -> str:
    """Current weather from wttr.in - free, no account needed."""
    try:
        url = f"https://wttr.in/{urllib.parse.quote(city)}?format=%l:+%C,+%t,+wind+%w"
        return requests.get(url, timeout=10).text.strip()
    except Exception:
        return "The weather service did not answer."


def open_website(url: str) -> str:
    if not url.startswith("http"):
        url = "https://" + url
    webbrowser.open(url)
    return f"Opened {url}"


def search_web(query: str) -> str:
    webbrowser.open("https://www.bing.com/search?q=" + urllib.parse.quote(query))
    return f"Searching the web for: {query}"


def save_note(text: str) -> str:
    """Adds a line to notes.txt in this folder."""
    with open("notes.txt", "a", encoding="utf-8") as file:
        file.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M}  {text}\n")
    return "Note saved."


def calculate(expression: str) -> str:
    """Arithmetic the safe way: the expression is read as a formula (numbers, + - * / % ** and brackets, plus
    sqrt, round and abs), never run as code. Spoken forms such as '15 percent of 80' are translated first."""
    import math
    import operator
    text = " " + expression.lower().strip() + " "
    for words, symbol in (("to the power of", "**"), ("multiplied by", "*"), ("divided by", "/"), ("percent of", "/100*"),
                          ("squared", "**2"), ("cubed", "**3"), ("square root of", "sqrt"), ("plus", "+"), ("minus", "-"),
                          ("times", "*"), ("over", "/"), (" x ", " * "), ("^", "**"), ("×", "*"), ("÷", "/"), (",", "")):
        text = text.replace(words, symbol)
    text = re.sub(r"sqrt\s+([0-9.]+)", r"sqrt(\1)", text)          # "square root of 144" -> sqrt(144)
    ops = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
           ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos}
    functions = {"sqrt": math.sqrt, "round": round, "abs": abs}
    constants = {"pi": math.pi, "e": math.e}

    def value_of(node):
        if isinstance(node, ast.Expression):
            return value_of(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in ops:
            left, right = value_of(node.left), value_of(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 1000:
                raise ValueError("too big")
            return ops[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in ops:
            return ops[type(node.op)](value_of(node.operand))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in functions:
            return functions[node.func.id](*[value_of(a) for a in node.args])
        if isinstance(node, ast.Name) and node.id in constants:
            return constants[node.id]
        raise ValueError("not arithmetic")

    try:
        result = value_of(ast.parse(text.strip(), mode="eval"))
    except ZeroDivisionError:
        return "That would be a division by zero."
    except Exception:
        return f"I couldn't work out '{expression}'. I can do plain arithmetic: plus, minus, times, divided by, powers and square roots."
    result = round(result, 6)
    if result == int(result):
        result = int(result)
    return f"{expression} equals {result:,}"


def smart_home(device: str, action: str) -> str:
    """Turn a Home Assistant device on or off."""
    entity = SMART_HOME_DEVICES.get(device.lower().strip())
    if not entity:
        return f"I don't know a device called '{device}'. I know: {', '.join(SMART_HOME_DEVICES)}."
    action = action.lower().strip().replace(" ", "_")
    if action in ("on", "off"):
        action = "turn_" + action
    if action not in ("turn_on", "turn_off", "toggle"):
        return "I can only turn devices on or off."
    domain = entity.split(".")[0]                       # "light.kitchen" -> "light"
    try:
        answer = requests.post(f"{HA_URL}/api/services/{domain}/{action}",
                               headers={"Authorization": f"Bearer {HA_TOKEN}"},
                               json={"entity_id": entity}, timeout=10)
        return "Done." if answer.ok else f"Home Assistant answered with error {answer.status_code}."
    except Exception:
        return "I couldn't reach Home Assistant."


TOOL_FUNCTIONS = {
    "get_current_time": get_current_time,
    "get_weather": get_weather,
    "open_website": open_website,
    "search_web": search_web,
    "save_note": save_note,
    "calculate": calculate,
    "smart_home": smart_home,
}
BUILT_IN_TOOLS = set(TOOL_FUNCTIONS)        # the names a learned skill may not take


def tool(name: str, description: str, **params: str) -> dict:
    """Writes the description card the brain reads to understand a tool."""
    return {"type": "function", "function": {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {p: {"type": "string", "description": d} for p, d in params.items()},
            "required": list(params),
        },
    }}


TOOLS = [
    tool("get_current_time", "Get the current date and time. Use when the user asks what time or what day it is."),
    tool("get_weather", "Get the current weather for a city. Use whenever the user asks about weather, temperature or rain.", city="City name, for example Tel Aviv"),
    tool("open_website", "Open a website in the user's browser. Use when the user says open, go to or show me a site.", url="Full address, for example https://www.youtube.com"),
    tool("search_web", "Search the internet in the user's browser. Use when the user says search, look up or google something.", query="What to search for"),
    tool("save_note", "Save a short note for the user. Use when the user says save a note, remember this or write this down.", text="The note to save"),
    tool("calculate", "Work out a calculation. Use whenever the user asks what a sum, product, percentage, power or square root comes to, for example 17 times 23 or 15 percent of 80.",
         expression="The calculation in symbols, for example 17 * 23 or 15 / 100 * 80"),
]
if HA_URL and HA_TOKEN and SMART_HOME_DEVICES:
    TOOLS.append(tool("smart_home", "Turn a smart home device on or off.",
                      device="One of: " + ", ".join(SMART_HOME_DEVICES),
                      action="turn_on or turn_off"))


# =====================================================================
#  3b. SKILLS - tools Jarvis writes for itself when it lacks one (Step 12 of the guide)
#
#  A skill is one small Python file in the skills folder. The brain writes it, Jarvis reads it without
#  running it (only harmless modules, no files, no network, no eval), runs the skill's own self-test in a
#  separate process with a time limit, and only then saves it and hands it to the brain as a new tool.
#  Every skill is plain text you can open and read. Delete the file and the skill is forgotten.
# =====================================================================
ALLOWED_MODULES = {"math", "datetime", "random", "re", "json", "statistics", "fractions", "decimal", "calendar",
                   "string", "itertools", "collections", "time", "textwrap", "unicodedata", "zoneinfo", "functools", "operator"}
FORBIDDEN_NAMES = {"eval", "exec", "compile", "open", "input", "__import__", "getattr", "setattr", "delattr",
                   "globals", "locals", "vars", "breakpoint", "exit", "quit", "help", "memoryview"}
SKILL_ATTEMPTS = 3          # how many tries the brain gets to write a skill that passes the checks and its own test
SKILL_TEST_SECONDS = 10     # a skill's self-test must finish within this time
YES_WORDS = ("yes", "yeah", "yep", "sure", "please", "go ahead", "do it", "ok", "okay", "why not", "of course",
             "absolutely", "certainly", "yes please", "go on", "do")

SKILL_EXAMPLE = '''NAME = "convert_temperature"
DESCRIPTION = "Convert a temperature between Celsius and Fahrenheit. Use when the user asks what a temperature is in the other scale."
PARAMETERS = {"degrees": "The temperature, for example 20", "scale": "The scale it is in: C or F"}
TEST = {"degrees": "100", "scale": "C"}
EXPECT = "212"

def run(degrees, scale):
    value = float(degrees)
    if scale.strip().upper().startswith("C"):
        return f"{degrees} degrees Celsius is {value * 9 / 5 + 32:.0f} degrees Fahrenheit."
    return f"{degrees} degrees Fahrenheit is {(value - 32) * 5 / 9:.0f} degrees Celsius."
'''

SKILL_WRITER_PROMPT = (
    "You write small Python skills for a voice assistant. A skill is one Python file with exactly these parts: "
    "NAME (a short snake_case name), DESCRIPTION (one sentence saying what it does and when to use it, starting with a verb), "
    "PARAMETERS (a dict of parameter name to a short description; every parameter arrives as a string), "
    "TEST (a dict of example arguments for a self-test, with the same keys as PARAMETERS), "
    "EXPECT (text the self-test answer must contain, or \"\" if unsure), and a function run(...) whose parameters are exactly "
    "the keys of PARAMETERS and which returns one short spoken sentence. "
    f"Rules: use only these modules: {', '.join(sorted(ALLOWED_MODULES))}. No files, no network, no input(), no eval() or exec(). "
    "Convert string parameters to numbers yourself with float(). Reply with the Python code only: no explanations and no markdown.\n\n"
    "Example:\n" + SKILL_EXAMPLE
)

SKILL_RUNNER = """import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("skill", sys.argv[1])
skill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(skill)
print(json.dumps(str(skill.run(**skill.TEST))))
"""


class SkillWanted:
    """What the brain hands back when it reached for a tool that does not exist: the request to learn from."""

    def __init__(self, request: str, hint: str = ""):
        self.request, self.hint = request, hint


def skills_folder() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), SKILLS_FOLDER)


def strip_code_fences(text: str) -> str:
    """Small models wrap code in ``` fences even when told not to."""
    match = re.search(r"```[a-zA-Z]*\s*\n(.*?)```", text, re.S)
    return (match.group(1) if match else text).strip() + "\n"


def inspect_skill(code: str) -> str:
    """Reads a skill WITHOUT running it. Returns "" when it looks safe and complete, otherwise what is wrong."""
    try:
        tree = ast.parse(code)
    except SyntaxError as error:
        return f"syntax error on line {error.lineno}: {error.msg}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in ALLOWED_MODULES:
                    return f"the module {alias.name} is not allowed"
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] not in ALLOWED_MODULES:
                return f"the module {node.module} is not allowed"
        elif isinstance(node, ast.Name) and (node.id in FORBIDDEN_NAMES or node.id.startswith("__")):
            return f"{node.id} is not allowed"
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            return f"{node.attr} is not allowed"
    facts = skill_facts(code)
    missing = [key for key in ("NAME", "DESCRIPTION", "PARAMETERS", "TEST") if key not in facts]
    if missing:
        return "missing " + ", ".join(missing) + " (they must be plain values, not expressions)"
    if not any(isinstance(node, ast.FunctionDef) and node.name == "run" for node in tree.body):
        return "missing the run function"
    name = facts["NAME"]
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{2,40}", name):
        return "NAME must be a short snake_case word such as convert_volume"
    if name in BUILT_IN_TOOLS:
        return f"a tool called {name} already exists; choose another name"
    if not isinstance(facts["DESCRIPTION"], str) or not facts["DESCRIPTION"].strip():
        return "DESCRIPTION must be a sentence"
    parameters, test = facts["PARAMETERS"], facts["TEST"]
    if not isinstance(parameters, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in parameters.items()):
        return "PARAMETERS must be a dict of name to description, both text"
    if not isinstance(test, dict) or set(test) != set(parameters):
        return "TEST must be a dict with exactly the same keys as PARAMETERS"
    return ""


def skill_facts(code: str) -> dict:
    """NAME, DESCRIPTION, PARAMETERS, TEST and EXPECT, read from the text without running anything."""
    facts = {"EXPECT": ""}
    try:
        body = ast.parse(code).body
    except SyntaxError:
        return facts
    for node in body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                facts[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                pass
    return facts


def test_skill(path: str):
    """Runs the skill's own self-test in a separate process with a time limit. Returns (ok, answer or problem)."""
    try:
        result = subprocess.run([sys.executable, "-I", "-c", SKILL_RUNNER, path], capture_output=True, text=True,
                                timeout=SKILL_TEST_SECONDS, cwd=tempfile.gettempdir())
    except subprocess.TimeoutExpired:
        return False, f"the self-test took longer than {SKILL_TEST_SECONDS} seconds"
    if result.returncode != 0:
        lines = result.stderr.strip().splitlines()
        return False, (lines[-1] if lines else "the self-test crashed")
    try:
        return True, json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False, "the self-test printed nothing usable"


def install_skill(path: str) -> str:
    """Loads one skill file and hands it to the brain as a tool. Returns the skill's name."""
    with open(path, encoding="utf-8") as file:
        code = file.read()
    problem = inspect_skill(code)
    if problem:
        raise ValueError(problem)
    facts = skill_facts(code)
    spec = importlib.util.spec_from_file_location(f"skill_{facts['NAME']}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    TOOL_FUNCTIONS[facts["NAME"]] = module.run
    TOOLS[:] = [t for t in TOOLS if t["function"]["name"] != facts["NAME"]] + [tool(facts["NAME"], facts["DESCRIPTION"], **facts["PARAMETERS"])]
    return facts["NAME"]


def load_skills() -> list:
    """At start-up: switch on every skill in the skills folder. Returns their names."""
    names = []
    folder = skills_folder()
    if os.path.isdir(folder):
        for file in sorted(os.listdir(folder)):
            if file.endswith(".py") and not file.startswith("_"):
                try:
                    names.append(install_skill(os.path.join(folder, file)))
                except Exception as error:
                    print(f"(the skill {file} could not be loaded: {error})")
    return names


def write_skill(request: str, hint: str = ""):
    """Asks the brain to write a skill for the request, checks it, tests it, saves it and switches it on.
    Returns (name, description) on success, or (None, what went wrong)."""
    notify("state", "thinking")
    notify("tool", "writing a new skill")
    print(f"   [learning] writing a skill for: {request}")
    messages = [{"role": "system", "content": SKILL_WRITER_PROMPT},
                {"role": "user", "content": f"Write a skill so that you can answer requests like: {request}"
                                            + (f" (a tool called something like {hint} would do)" if hint else "")}]
    problem = "the brain did not answer"
    for attempt in range(1, SKILL_ATTEMPTS + 1):
        response = client.chat.completions.create(model=MODEL, messages=messages)
        code = strip_code_fences(response.choices[0].message.content or "")
        problem = inspect_skill(code)
        if not problem:
            facts = skill_facts(code)
            os.makedirs(skills_folder(), exist_ok=True)
            trial = os.path.join(skills_folder(), f"_trial_{facts['NAME']}.py")
            with open(trial, "w", encoding="utf-8") as file:
                file.write(code)
            ok, answer = test_skill(trial)
            if ok and facts["EXPECT"] and str(facts["EXPECT"]).lower() not in str(answer).lower():
                ok, answer = False, f"the self-test answered '{answer}', which does not contain '{facts['EXPECT']}'"
            if ok:
                final = os.path.join(skills_folder(), f"{facts['NAME']}.py")
                os.replace(trial, final)
                install_skill(final)
                print(f"   [learning] new skill saved: {os.path.join(SKILLS_FOLDER, facts['NAME'] + '.py')} (self-test said: {answer})")
                notify("tool", f"learned {facts['NAME']}")
                return facts["NAME"], facts["DESCRIPTION"]
            try:
                os.remove(trial)
            except OSError:
                pass
            problem = answer
        print(f"   [learning] attempt {attempt} rejected: {problem}")
        messages.append({"role": "assistant", "content": code})
        messages.append({"role": "user", "content": f"That was rejected: {problem}. Rewrite the whole file, following every rule."})
    notify("tool", "could not learn that")
    return None, problem


def learning_request(text: str):
    """'Learn how to convert gallons to litres' -> 'convert gallons to litres'. None if this is not a request to learn."""
    cleaned = re.sub(r"[^a-z0-9' ]+", " ", strip_name(text).lower()).strip()
    match = re.match(r"^(?:please |can you |could you )?(?:learn|teach yourself)\s+(?:how to |to |a new skill(?: to| for)? )?(.+)$", cleaned)
    return match.group(1).strip() if match and len(match.group(1).split()) >= 2 else None


def said_yes(text: str) -> bool:
    phrase = " ".join(w for w in re.sub(r"[^a-z']+", " ", text.lower()).split() if w != NAME.lower())
    if not phrase or any(re.search(rf"\b{n}\b", phrase) for n in NEGATIONS + ("no", "nope", "nah")):
        return False
    return any(re.search(rf"\b{re.escape(y)}\b", phrase) for y in YES_WORDS)


# =====================================================================
#  4. BRAIN
# =====================================================================
# Ollama understands the OpenAI API, so we point the OpenAI library at it (the key is ignored).
client = OpenAI(base_url=OLLAMA_URL, api_key="ollama")
history = [{"role": "system", "content": SYSTEM_PROMPT}]


def run_tool(name: str, arguments) -> str:
    function = TOOL_FUNCTIONS.get(name)
    if function is None:
        return f"Unknown tool: {name}"
    try:
        if isinstance(arguments, str):
            arguments = json.loads(arguments or "{}")
        return str(function(**(arguments or {})))
    except Exception as error:
        return f"The tool failed: {error}"


def forget_old_messages(keep: int = 20) -> None:
    """Drop old conversation so the brain stays fast (the system prompt always stays)."""
    if len(history) <= keep + 1:
        return
    for i in range(len(history) - keep, len(history)):
        if history[i]["role"] == "user":
            del history[1:i]
            return


def fake_tool_call(text: str):
    """
    Small models sometimes write a tool call as plain text instead of using the real
    tool-calling channel, for example {"name": "None", "parameters": {}} or the same
    thing wrapped in a code fence.
    Returns (tool_name, arguments) if the text holds a call like that, otherwise None.
    """
    text = text.strip().strip("`").strip()
    if text.lower().startswith("json"):
        text = text[4:].strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    if isinstance(data, dict) and "name" in data:
        return str(data.get("name")), data.get("parameters") or data.get("arguments") or {}
    return None


def named_tool(text: str):
    """The tool name inside a tool call written out as text, even a broken one. None if there is none."""
    match = re.search(r'"name"\s*:\s*"([^"]+)"', text)
    return match.group(1) if match else None


def looks_like_code(text: str) -> bool:
    """
    True when a reply is JSON or code rather than a sentence, even a half-finished one:
    it opens with a brace, a bracket or a code fence, or it carries the tell-tale
    "name": / "parameters": of a tool call written out as text. Such a reply must
    never be read aloud.
    """
    text = text.strip()
    return (text.startswith(("{", "[", "```", "<|"))
            or re.search(r'"(name|parameters|arguments|function|tool_calls?)"\s*:', text) is not None
            or "<function" in text)


last_action = {"key": None, "result": ""}     # the most recent action in this conversation, so a comment does not repeat it


def action_key(name: str, arguments) -> str:
    """One string that identifies a tool call: its name plus its arguments, in a fixed order."""
    try:
        arguments = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
    except ValueError:
        pass
    return f"{name} {json.dumps(arguments, sort_keys=True, default=str)}"


def forget_recent_actions() -> None:
    """A new conversation starts: anything may be done afresh."""
    last_action["key"], last_action["result"] = None, ""


def wants_repeat(text: str) -> bool:
    return re.search(r"\b(again|repeat|once more|one more time|re-?open|re-?do)\b", text.lower()) is not None


TOOL_TRIGGERS = {           # a tool that changes something runs only when your words actually ask for it
    "save_note": ("save", "note", "remember", "write down", "write this", "write that", "jot", "remind"),
    "open_website": ("open", "go to", "show", "launch", "bring up", "visit", "website", "site", "browser", "page",
                     ".com", ".org", ".net", "www"),
    "search_web": ("search", "look up", "look for", "google", "bing", "find", "browse"),
}


def asked_for(name: str, user_text: str) -> bool:
    """Did the user actually ask for this tool? Read-only tools (time, weather) are always allowed."""
    triggers = TOOL_TRIGGERS.get(name)
    if not triggers:
        return True
    text = " " + re.sub(r"[^a-z0-9.' ]+", " ", user_text.lower()) + " "
    return any(t in text for t in triggers)


def perform(name: str, arguments, user_text: str, done_this_turn: set):
    """Run a tool, unless it is an accidental repeat of what was just done, or something the user never asked
    for. Small models tend to call the same tool again when your next sentence merely mentions it ("stop opening
    YouTube" opens YouTube), and to "save a note" out of a stray remark. Returns (result, was_blocked)."""
    key = action_key(name, arguments)
    if key in done_this_turn or (key == last_action["key"] and not wants_repeat(user_text)):
        print(f"   [tool] {name} skipped: already done a moment ago")
        notify("tool", f"{name} (already done)")
        return (f"(Skipped: you already did exactly this a moment ago and the result was: {last_action['result'] or 'done'}. "
                "The user is probably commenting, not asking again. Reply in plain words now, without any tool.)"), True
    if not asked_for(name, user_text):
        print(f"   [tool] {name} skipped: the user did not ask for it")
        notify("tool", f"{name} (not asked for)")
        return (f'(Not done: the user did not ask for {name}. Their exact words were: "{user_text}". '
                "Reply to those words yourself, from what you know, in one or two plain sentences. "
                "Do not mention tools, searching, or this note.)"), True
    shown = arguments if isinstance(arguments, str) else json.dumps(arguments)
    print(f"   [tool] {name} {shown}")
    notify("tool", f"{name} {shown}")
    result = run_tool(name, arguments)
    done_this_turn.add(key)
    last_action["key"], last_action["result"] = key, result
    return result, False


def think(user_text: str, may_learn: bool = True, hint: str = ""):
    """Send the user's words to the brain, run any tools it asks for, return the reply.
    When the brain reaches for a tool that does not exist and learning is on, returns a SkillWanted instead,
    so that converse() can offer to write that tool. may_learn=False switches that off for the retry."""
    notify("state", "thinking")
    forget_old_messages()
    history.append({"role": "user", "content": user_text + (f" {hint}" if hint else "")})
    asked_at = len(history) - 1                         # so a learning detour can take the question back out
    nudge_at = None                                     # where the nudge messages sit, so we can remove them later
    done_this_turn = set()                              # tools already run for this request

    def wants_skill(name: str) -> bool:
        return LEARNING and may_learn and bool(name) and name != "None" and name not in TOOL_FUNCTIONS

    def finish(reply: str) -> str:
        if nudge_at is not None:                        # drop the nudge chatter, keep everything real
            del history[nudge_at:nudge_at + 2]
        history.append({"role": "assistant", "content": reply})
        return reply

    def answer_in_words(fallback: str) -> str:
        """Ask the brain once more, with no tools on offer, so the answer has to be a sentence."""
        response = client.chat.completions.create(model=MODEL, messages=history)
        reply = (response.choices[0].message.content or "").strip()
        return finish(fallback if not reply or looks_like_code(reply) else reply)

    for _ in range(6):                                  # allow a few tool calls in a row
        response = client.chat.completions.create(model=MODEL, messages=history, tools=TOOLS)
        message = response.choices[0].message

        if not message.tool_calls:
            reply = (message.content or "").strip()
            fake = fake_tool_call(reply)
            if fake and fake[0] in TOOL_FUNCTIONS:      # a real tool, asked for in the wrong way: run it anyway
                name, arguments = fake
                result, repeated = perform(name, arguments, user_text, done_this_turn)
                history.append({"role": "assistant", "content": reply})
                history.append({"role": "user", "content": f"(Result of {name}: {result}) Now answer me in one or two plain sentences."})
                if repeated:
                    return answer_in_words(f"That is already done{addr()}.")
                continue
            if fake or not reply or looks_like_code(reply):   # not an answer at all
                name = fake[0] if fake else named_tool(reply)
                if wants_skill(name):                   # the brain wished for a tool it does not have: offer to write it
                    del history[asked_at:]
                    return SkillWanted(user_text, name)
                if nudge_at is None:                    # first: give the brain a second chance, tools included
                    nudge_at = len(history)
                    history.append({"role": "assistant", "content": reply or "..."})
                    if name in TOOL_FUNCTIONS:
                        no_such_tool = f"Call the {name} tool properly, through the tool channel, not as text. "
                    elif name and name != "None":
                        no_such_tool = f"There is no tool called {name}. "
                    else:
                        no_such_tool = ""
                    history.append({"role": "user", "content": f"(That was not an answer. {no_such_tool}If one of your tools fits my request, call it properly now. Otherwise answer me in plain words, never JSON.)"})
                    continue
                return answer_in_words(f"I'm not sure what to say to that{addr()}.")   # last resort: plain answer, no tools
            return finish(reply)

        for call in message.tool_calls:                 # a proper call, but for a tool that does not exist
            if wants_skill(call.function.name):
                del history[asked_at:]
                return SkillWanted(user_text, call.function.name)
        history.append({                                # remember that the brain asked for tools
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": [{"id": call.id, "type": "function",
                            "function": {"name": call.function.name,
                                         "arguments": call.function.arguments}}
                           for call in message.tool_calls],
        })
        repeated_any = False
        for call in message.tool_calls:                 # run each tool and report back
            result, repeated = perform(call.function.name, call.function.arguments, user_text, done_this_turn)
            repeated_any = repeated_any or repeated
            history.append({"role": "tool", "tool_call_id": call.id, "content": result})
        if repeated_any:                                # the brain is going in circles: make it answer in words
            return answer_in_words(f"That is already done{addr()}.")

    return finish(f"I'm afraid I lost the thread there{addr()}. Could you put that another way?")


def safe_think(user_text: str, may_learn: bool = True, hint: str = ""):
    """think(), but every problem becomes a spoken sentence instead of a crash."""
    try:
        started = time.time()
        reply = think(user_text, may_learn, hint)
        stopwatch["brain"] = time.time() - started
        stopwatch["brain_done"] = time.time()
        return reply
    except APIConnectionError:
        return f"I'm afraid I can't reach my brain{addr()}. Please make sure the Ollama app is running. Look for the llama icon in the system tray."
    except NotFoundError:
        return f"The model {MODEL} is not installed{addr()}. Run: ollama pull {MODEL}"
    except Exception as error:
        return f"I'm afraid something went wrong{addr()}: {error}"


# =====================================================================
#  5. THE LOOP - ears -> brain -> hands -> voice, again and again
# =====================================================================
QUIT_WORDS = ("exit", "quit", "shut down", "shutdown", "power off", "power down", "go to sleep")   # close the program
GOODBYE_WORDS = ("goodbye", "good bye", "bye", "bye bye", "see you", "see you later", "stop",       # end the conversation
                 "that's all", "that is all", "never mind", "nevermind")                           # (hands-free: back to standby)
THANKS_WORDS = ("thank you", "thanks", "thank you very much", "thanks a lot", "many thanks", "cheers", "no thanks")
FILLER_WORDS = ("hey", "ok", "okay", "please", "now", "then", "well", "alright", "right", "so", "um", "uh")
NEGATIONS = ("don't", "dont", "do not", "not", "never", "no need")
NOISE_PHRASES = {"you", "so", "the end", "subscribe", "thanks for watching", "thank you for watching",   # what the ears
                 "please subscribe", "like and subscribe", "bye bye bye"}                                # "hear" in noise


def command(text: str):
    """Is this a goodbye, a shut-down or a thank-you? Ignores the name, punctuation and filler, so
    'Jarvis, shut down now!' and 'Bye bye.' both count. A command at the END of a short sentence counts too
    ('Service. Shut down.' is what the ears made of 'Jarvis, shut down'), unless it is negated
    ('don't shut down'). Returns "quit", "goodbye", "thanks" or None."""
    words = [w for w in re.sub(r"[^a-z']+", " ", text.lower()).split() if w not in FILLER_WORDS and w != NAME.lower()]
    phrase = " ".join(words)
    for wish, phrases in (("quit", QUIT_WORDS), ("goodbye", GOODBYE_WORDS), ("thanks", THANKS_WORDS)):
        if phrase in phrases:
            return wish
    if any(re.search(rf"\b{n}\b", phrase) for n in NEGATIONS):
        return None
    for wish, phrases in (("quit", QUIT_WORDS), ("goodbye", GOODBYE_WORDS), ("thanks", THANKS_WORDS)):
        for p in phrases:
            if phrase.endswith(" " + p) and len(words) - len(p.split()) <= 3:
                return wish
    return None


def looks_like_noise(text: str) -> bool:
    """True for the phrases the speech model invents when it hears typing or rustling instead of words."""
    words = re.sub(r"[^a-z' ]+", " ", text.lower()).split()
    return not words or " ".join(words) in NOISE_PHRASES


def learn(wanted: SkillWanted, threshold: float, asked: bool = False) -> str:
    """The learning detour: (ask permission,) write the skill, then answer the original request with it.
    Returns what Jarvis should say."""
    if ASK_BEFORE_LEARNING and not asked:
        speak(f"I don't have a skill for that yet{addr()}. Shall I write one?")
        try:
            answer = listen(threshold, wait_seconds=8, prompt="(yes or no?)")
        except Exception:
            answer = ""
        print(f"You: {answer}" if answer else "You: (nothing)")
        if not said_yes(answer):
            reply = safe_think(wanted.request, may_learn=False)   # answer as best it can, in words
            return f"Very well{addr()}. " + (reply if isinstance(reply, str) else "")
    speak(f"Right away{addr()}. Give me a moment.")
    name, description = write_skill(wanted.request, wanted.hint)
    if name is None:
        print(f"   [learning] gave up: {description}")
        return f"I'm afraid that one is beyond me for now{addr()}."
    if asked:                                           # "learn how to ...": report, and wait for a real question
        return f"Done{addr()}. {description.split('. ')[0].rstrip('.')}. Do try me."
    reply = safe_think(wanted.request, may_learn=False, hint=f"(you now have a tool called {name} for exactly this; use it)")
    return reply if isinstance(reply, str) else f"I have learned it{addr()}, but I'm not sure what to say."


def converse(user_text: str, threshold: float, follow_up_seconds: float, hands_free: bool, wake_model=None) -> str:
    """Answer one request, then (hands-free) keep listening for follow-ups.
    Returns "quit" when Jarvis should close, otherwise "done"."""
    forget_recent_actions()                             # a new conversation
    while True:
        print(f"You: {user_text}")
        notify("you", user_text)
        wish = command(user_text)

        if wish == "quit" or (wish == "goodbye" and not hands_free):
            speak(f"Goodbye{addr()}.")
            return "quit"
        if wish == "goodbye":                           # hands-free: back to waiting for the wake word
            speak(f"Very good{addr()}. I'll be here if you need me.")
            return "done"
        if wish == "thanks":                            # no need to trouble the brain, and nothing should be re-done
            speak(f"You're very welcome{addr()}.")
            return "done"

        stopwatch.clear()
        to_learn = learning_request(user_text) if LEARNING else None
        if to_learn:                                    # "learn how to ...": you asked, so no need to ask back
            reply = learn(SkillWanted(to_learn), threshold, asked=True)
        else:
            reply = safe_think(user_text)
            if isinstance(reply, SkillWanted):          # the brain wanted a tool it does not have
                reply = learn(reply, threshold)
        cut_off = say(reply, wake_model)
        if SHOW_TIMINGS and "brain" in stopwatch:
            voice = max(0.0, stopwatch.get("voice_started", 0) - stopwatch.get("brain_done", stopwatch["voice_started"]))
            print(f"   (ears {stopwatch.get('ears', 0):.1f} s, brain {stopwatch['brain']:.1f} s, voice ready in {voice:.1f} s)")

        if cut_off:                                     # you interrupted: listen right now, for a normal turn
            follow_up_seconds, prompt = max(follow_up_seconds, 6), "Yes? (listening)"
        elif follow_up_seconds <= 0:
            return "done"
        else:
            prompt = f"(still listening for {follow_up_seconds:g} seconds, no need to say Hey {NAME})"
        try:                                            # a follow-up needs no wake word
            heard = listen(threshold, wait_seconds=follow_up_seconds, prompt=prompt)
            user_text = strip_name(heard)
            if heard and not user_text:                 # only the name: answer, and listen once more
                speak(f"Yes{addr()}?")
                user_text = strip_name(listen(threshold))
        except Exception as error:
            print(f"(microphone problem: {error})")
            return "done"
        if not user_text or looks_like_noise(user_text):
            return "done"


def main(wait_for_user=None) -> None:
    """wait_for_user decides how a conversation starts. It blocks until it is your turn and returns
    typed text, "" to listen through the microphone, or None to shut down. Default: the Enter key.
    With WAKE_WORD = True the wake word takes over. The HUD passes wait_for_signal (its Space key)."""
    print(f"\n{NAME} is starting up...")
    skills = load_skills()
    if skills:
        print(f"Skills I have learned: {', '.join(skills)}")
    threshold = THRESHOLD
    if threshold is None:
        try:
            print("Calibrating the microphone - please stay quiet for a second...")
            threshold = max(0.005, measure_background_noise() * 3)
        except Exception as error:
            print(f"(microphone problem: {error} - you can still type to me)")
            threshold = 0.01

    wake_model = load_wake_word_model() if WAKE_WORD else None
    if wake_model is not None:
        hands_free = True
        how_to_talk = f'Say "Hey {NAME}" and ask your question (say it again to interrupt me). Say "goodbye" to end a chat, "shut down" to close me.'
    elif wait_for_user is None:
        hands_free = False
        wait_for_user = wait_for_enter
        how_to_talk = "Press Enter to talk, or type a message and press Enter. Say 'goodbye' to stop."
    else:
        hands_free = True                               # another window (the HUD) tells us when to listen
        how_to_talk = "Press Space in the window to talk. Say 'goodbye' to end a chat, 'shut down' to close me."

    speak(greeting())
    print("\n" + how_to_talk)
    notify("info", how_to_talk)

    while not STOP:
        notify("state", "standby")
        try:
            if wake_model is not None:
                user_text = hands_free_listen(wake_model, threshold)     # None = shut down
            else:
                user_text = wait_for_user()                             # None = shut down, "" = use the microphone
                if user_text == "":
                    user_text = listen(threshold)
        except KeyboardInterrupt:
            user_text = None
        except Exception as error:
            print(f"(microphone problem: {error} - try typing instead)")
            time.sleep(1)
            continue
        if user_text is None:
            break

        if not user_text or looks_like_noise(user_text):
            print("I didn't catch that. Try again, a little louder.")
            continue

        if converse(user_text, threshold, FOLLOW_UP_SECONDS if hands_free else 0, hands_free, wake_model) == "quit":
            break

    notify("state", "off")


if __name__ == "__main__":
    main()
