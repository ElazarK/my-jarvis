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
"""
import collections
import datetime
import json
import os
import re
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
PERSONALITY = "calm, precise and well-mannered, like a British butler with a dry sense of humour"
LANGUAGE = "en"             # "he" = Hebrew, "es" = Spanish, "fr" = French ... or None = auto-detect
THRESHOLD = None            # microphone sensitivity. None = measure automatically at start-up
SAMPLE_RATE = 16000

# Hands-free (Step 9). Needs:  pip install openwakeword
WAKE_WORD = False           # True = no Enter key: say "Hey Jarvis" and then talk, like a smart speaker
WAKE_SENSITIVITY = 0.5      # how sure Jarvis must be that it heard its name: 0.3 = eager, 0.7 = strict
FOLLOW_UP_SECONDS = 8       # after an answer Jarvis keeps listening this long, so you can reply without the wake word
INTERRUPTIBLE = True        # True = say "Hey Jarvis" (or press Space in the window, or Enter in the terminal) while Jarvis
                            #        is talking and it stops mid-sentence and listens to you

# Optional: smart home control through Home Assistant. Leave empty to skip.
HA_URL = ""                 # for example "http://homeassistant.local:8123"
HA_TOKEN = ""               # a Long-Lived Access Token from your Home Assistant profile page
SMART_HOME_DEVICES = {      # "what you call it": "Home Assistant entity id"
    # "living room light": "light.living_room",
    # "desk lamp": "switch.desk_lamp",
}

SYSTEM_PROMPT = (
    f"You are {NAME}, a voice assistant running on the user's Windows PC. You are {PERSONALITY}. "
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


def speak_edge(text: str) -> None:
    """Microsoft's neural voices through the edge-tts package. Needs internet."""
    import edge_tts
    path = os.path.join(tempfile.gettempdir(), "jarvis_voice.mp3")
    edge_tts.Communicate(text, EDGE_VOICE, rate=EDGE_RATE, pitch=EDGE_PITCH).save_sync(path)
    play_file(path)


def speak(text: str) -> None:
    """Say the text out loud (and print it) with whichever voice engine is selected.
    Sets `interrupted` if you cut it off; the caller decides what to do about that."""
    print(f"{NAME}: {text}")
    notify("jarvis", text)
    notify("state", "speaking")
    interrupted.clear()
    speaking.set()
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
                   silence_seconds: float = 1.2, wait_seconds: float = 6, start_with=(), ignore_seconds: float = 0.0):
    """The listening rule shared by every way of talking to Jarvis: keep taking small chunks of sound
    until you have spoken and then gone quiet. read_chunk() returns one chunk as 1-D float32 audio.
    start_with = sound to keep in front of the recording (what was said just before the wake word was
    recognised). ignore_seconds = how long at the start to ignore loudness (while the chime plays).
    Returns the audio, or None if nobody spoke."""
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


def record(threshold: float, max_seconds: float = 15, silence_seconds: float = 1.2,
           wait_seconds: float = 6):
    """Record from the microphone until you stop talking. Returns None if nobody spoke."""
    chunk_seconds = 0.1
    chunk = int(SAMPLE_RATE * chunk_seconds)
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=chunk) as stream:
        return collect_speech(lambda: stream.read(chunk)[0][:, 0], chunk_seconds, threshold,
                              max_seconds, silence_seconds, wait_seconds)


def transcribe(audio) -> str:
    """Turn recorded audio into text."""
    segments, _ = whisper.transcribe(audio, language=LANGUAGE, beam_size=1, vad_filter=True)
    return " ".join(segment.text.strip() for segment in segments).strip()


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
        speak("Yes?")
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
    "smart_home": smart_home,
}


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
]
if HA_URL and HA_TOKEN and SMART_HOME_DEVICES:
    TOOLS.append(tool("smart_home", "Turn a smart home device on or off.",
                      device="One of: " + ", ".join(SMART_HOME_DEVICES),
                      action="turn_on or turn_off"))


# =====================================================================
#  4. BRAIN
# =====================================================================
# Ollama understands the OpenAI API, so we point the OpenAI library at it (the key is ignored).
client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
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


def think(user_text: str) -> str:
    """Send the user's words to the brain, run any tools it asks for, return the reply."""
    notify("state", "thinking")
    forget_old_messages()
    history.append({"role": "user", "content": user_text})
    nudge_at = None                                     # where the nudge messages sit, so we can remove them later
    done_this_turn = set()                              # tools already run for this request

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
                    return answer_in_words("That is already done.")
                continue
            if fake or not reply or looks_like_code(reply):   # not an answer at all
                if nudge_at is None:                    # first: give the brain a second chance, tools included
                    nudge_at = len(history)
                    history.append({"role": "assistant", "content": reply or "..."})
                    name = fake[0] if fake else named_tool(reply)
                    if name in TOOL_FUNCTIONS:
                        no_such_tool = f"Call the {name} tool properly, through the tool channel, not as text. "
                    elif name and name != "None":
                        no_such_tool = f"There is no tool called {name}. "
                    else:
                        no_such_tool = ""
                    history.append({"role": "user", "content": f"(That was not an answer. {no_such_tool}If one of your tools fits my request, call it properly now. Otherwise answer me in plain words, never JSON.)"})
                    continue
                return answer_in_words("I'm not sure what to say to that.")   # last resort: plain answer, no tools
            return finish(reply)

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
            return answer_in_words("That is already done.")

    return finish("I got a bit lost there. Could you ask that in a different way?")


def safe_think(user_text: str) -> str:
    """think(), but every problem becomes a spoken sentence instead of a crash."""
    try:
        return think(user_text)
    except APIConnectionError:
        return "I can't reach my brain. Please make sure the Ollama app is running. Look for the llama icon in the system tray."
    except NotFoundError:
        return f"The model {MODEL} is not installed. Run: ollama pull {MODEL}"
    except Exception as error:
        return f"Something went wrong: {error}"


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


def converse(user_text: str, threshold: float, follow_up_seconds: float, hands_free: bool, wake_model=None) -> str:
    """Answer one request, then (hands-free) keep listening for follow-ups.
    Returns "quit" when Jarvis should close, otherwise "done"."""
    forget_recent_actions()                             # a new conversation
    while True:
        print(f"You: {user_text}")
        notify("you", user_text)
        wish = command(user_text)

        if wish == "quit" or (wish == "goodbye" and not hands_free):
            speak("Goodbye!")
            return "quit"
        if wish == "goodbye":                           # hands-free: back to waiting for the wake word
            speak("Very good. I'll be here if you need me.")
            return "done"
        if wish == "thanks":                            # no need to trouble the brain, and nothing should be re-done
            speak("You're very welcome.")
            return "done"

        cut_off = say(safe_think(user_text), wake_model)

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
                speak("Yes?")
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

    speak(f"Hello, I am {NAME}. How can I help?")
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
