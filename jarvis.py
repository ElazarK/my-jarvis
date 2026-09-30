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
import datetime
import json
import os
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
    "when no tool is needed, simply answer in words and never write JSON. "
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
def speak_windows(text: str) -> None:
    """The voices built into Windows. Works offline."""
    engine = pyttsx3.init()
    voices = engine.getProperty("voices")
    if voices and VOICE < len(voices):
        engine.setProperty("voice", voices[VOICE].id)
    engine.setProperty("rate", RATE)
    engine.say(text)
    engine.runAndWait()
    engine.stop()


def speak_edge(text: str) -> None:
    """Microsoft's neural voices through the edge-tts package. Needs internet."""
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
    """Say the text out loud (and print it) with whichever voice engine is selected."""
    print(f"{NAME}: {text}")
    notify("jarvis", text)
    notify("state", "speaking")
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


def chime() -> None:
    """A short two-note sound that means 'I heard my name, go ahead'. Made from numbers, no file needed."""
    try:
        t = np.linspace(0, 0.09, int(SAMPLE_RATE * 0.09), endpoint=False)
        fade = np.linspace(1, 0, t.size)
        tone = np.concatenate([np.sin(2 * np.pi * f * t) * fade for f in (880, 1320)]) * 0.25
        sd.play(tone.astype("float32"), SAMPLE_RATE)
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


def record(threshold: float, max_seconds: float = 15, silence_seconds: float = 1.2,
           wait_seconds: float = 6):
    """Record from the microphone until you stop talking. Returns None if nobody spoke."""
    chunk_seconds = 0.1
    chunk = int(SAMPLE_RATE * chunk_seconds)
    frames, heard_speech, quiet_time, elapsed = [], False, 0.0, 0.0

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=chunk) as stream:
        while elapsed < max_seconds:
            data, _ = stream.read(chunk)
            data = data[:, 0]
            frames.append(data)
            elapsed += chunk_seconds

            level = loudness(data)
            notify("level", level / threshold if threshold else 0.0)
            if level > threshold:
                heard_speech, quiet_time = True, 0.0
            else:
                quiet_time += chunk_seconds

            if heard_speech and quiet_time >= silence_seconds:
                break                                   # you finished your sentence
            if not heard_speech and elapsed >= wait_seconds:
                break                                   # nobody spoke

    return np.concatenate(frames) if heard_speech else None


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


def wait_for_wake_word(model) -> bool:
    """Listen to the room in 80 ms slices until you say 'Hey Jarvis'.
    Returns True when the name was heard (or talk_now was set), False when STOP was requested."""
    frame = 1280                                        # 80 ms at 16 kHz, the slice openWakeWord expects
    model.reset()                                       # forget older sound, including Jarvis's own voice
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=frame) as stream:
        while not STOP:
            if talk_now.is_set():                       # the HUD's Space key works in this mode too
                talk_now.clear()
                return True
            data, _ = stream.read(frame)
            score = max(model.predict(data[:, 0]).values())
            if score >= WAKE_SENSITIVITY:
                print(f"(heard my name, score {score:.2f})")
                return True
    return False


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
    tool-calling channel, for example {"name": "None", "parameters": {}}.
    Returns (tool_name, arguments) if the text looks like that, otherwise None.
    """
    text = text.strip()
    if not (text.startswith("{") and text.endswith("}")):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, dict) and "name" in data:
        return str(data.get("name")), data.get("parameters") or data.get("arguments") or {}
    return None


def think(user_text: str) -> str:
    """Send the user's words to the brain, run any tools it asks for, return the reply."""
    notify("state", "thinking")
    forget_old_messages()
    history.append({"role": "user", "content": user_text})
    nudge_at = None                                     # where the nudge messages sit, so we can remove them later

    for _ in range(6):                                  # allow a few tool calls in a row
        response = client.chat.completions.create(model=MODEL, messages=history, tools=TOOLS)
        message = response.choices[0].message

        if not message.tool_calls:
            reply = (message.content or "").strip()
            fake = fake_tool_call(reply)
            if fake and fake[0] in TOOL_FUNCTIONS:      # a real tool, asked for in the wrong way: run it anyway
                name, arguments = fake
                print(f"   [tool] {name} {json.dumps(arguments)}")
                notify("tool", f"{name} {json.dumps(arguments)}")
                result = run_tool(name, arguments)
                history.append({"role": "assistant", "content": reply})
                history.append({"role": "user", "content": f"(Result of {name}: {result}) Now answer me in one or two plain sentences."})
                continue
            if fake or not reply:                       # not an answer at all
                if nudge_at is None:                    # first: give the brain a second chance, tools included
                    nudge_at = len(history)
                    history.append({"role": "assistant", "content": reply or "..."})
                    history.append({"role": "user", "content": "(That was not an answer. If one of your tools fits my request, call it properly now. Otherwise answer me in plain words, never JSON.)"})
                    continue
                response = client.chat.completions.create(model=MODEL, messages=history)   # last resort: plain answer, no tools
                reply = (response.choices[0].message.content or "").strip() or "I'm not sure what to say to that."
            if nudge_at is not None:                    # drop the nudge chatter, keep everything real
                del history[nudge_at:nudge_at + 2]
            history.append({"role": "assistant", "content": reply})
            return reply

        history.append({                                # remember that the brain asked for tools
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": [{"id": call.id, "type": "function",
                            "function": {"name": call.function.name,
                                         "arguments": call.function.arguments}}
                           for call in message.tool_calls],
        })
        for call in message.tool_calls:                 # run each tool and report back
            print(f"   [tool] {call.function.name} {call.function.arguments}")
            notify("tool", f"{call.function.name} {call.function.arguments}")
            result = run_tool(call.function.name, call.function.arguments)
            history.append({"role": "tool", "tool_call_id": call.id, "content": result})

    return "I got a bit lost there. Could you ask that in a different way?"


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
QUIT_WORDS = ("exit", "quit", "shut down", "shutdown", "power off")           # close the program
GOODBYE_WORDS = ("goodbye", "bye", "stop", "that's all", "that is all")   # end the conversation (hands-free: back to standby)


def converse(user_text: str, threshold: float, follow_up_seconds: float, hands_free: bool) -> str:
    """Answer one request, then (hands-free) keep listening for follow-ups.
    Returns "quit" when Jarvis should close, otherwise "done"."""
    while True:
        print(f"You: {user_text}")
        notify("you", user_text)
        words = user_text.lower().strip(" .!?,")

        if words in QUIT_WORDS or (words in GOODBYE_WORDS and not hands_free):
            speak("Goodbye!")
            return "quit"
        if words in GOODBYE_WORDS:                      # hands-free: back to waiting for the wake word
            speak("Very good. I'll be here if you need me.")
            return "done"

        speak(safe_think(user_text))

        if follow_up_seconds <= 0:
            return "done"
        try:                                            # a follow-up needs no wake word
            user_text = listen(threshold, wait_seconds=follow_up_seconds,
                               prompt=f"(still listening for {follow_up_seconds:g} seconds, no need to say Hey {NAME})")
        except Exception as error:
            print(f"(microphone problem: {error})")
            return "done"
        if not user_text:
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
        wait_for_user = lambda: ("" if wait_for_wake_word(wake_model) else None)
        how_to_talk = f'Say "Hey {NAME}", wait for the chime, then talk. Say "goodbye" to end a chat, "shut down" to close me.'
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
            typed = wait_for_user()
        except KeyboardInterrupt:
            typed = None
        except Exception as error:
            print(f"(microphone problem: {error})")
            time.sleep(1)
            continue
        if typed is None:
            break

        if typed:
            user_text = typed
        else:
            if hands_free and wake_model is not None:
                chime()
            try:
                user_text = listen(threshold)
            except Exception as error:
                print(f"(microphone problem: {error} - try typing instead)")
                continue

        if not user_text:
            print("I didn't catch that. Try again, a little louder.")
            continue

        if converse(user_text, threshold, FOLLOW_UP_SECONDS if hands_free else 0, hands_free) == "quit":
            break

    notify("state", "off")


if __name__ == "__main__":
    main()
