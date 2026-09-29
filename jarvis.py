"""
JARVIS - a voice assistant that runs on your own Windows PC.

  Ears  : your microphone + Whisper turn your speech into text
  Brain : a local AI model (served by Ollama) decides what to answer or which tool to use
  Hands : small Python functions ("tools") that the brain can call
  Voice : the text-to-speech voices built into Windows read the answer aloud

Run:   python jarvis.py
Press Enter to talk, or type a message instead. Say "goodbye" to stop.
"""
import datetime
import json
import sys
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
VOICE = 0                   # the voice. 0 = first Windows voice (usually David), 1 = second (usually Zira)
RATE = 175                  # speaking speed in words per minute
LANGUAGE = "en"             # "he" = Hebrew, "es" = Spanish, "fr" = French ... or None = auto-detect
THRESHOLD = None            # microphone sensitivity. None = measure automatically at start-up
SAMPLE_RATE = 16000

# Optional: smart home control through Home Assistant. Leave empty to skip.
HA_URL = ""                 # for example "http://homeassistant.local:8123"
HA_TOKEN = ""               # a Long-Lived Access Token from your Home Assistant profile page
SMART_HOME_DEVICES = {      # "what you call it": "Home Assistant entity id"
    # "living room light": "light.living_room",
    # "desk lamp": "switch.desk_lamp",
}

SYSTEM_PROMPT = (
    f"You are {NAME}, a friendly and witty voice assistant running on the user's Windows PC. "
    "Your answers are read aloud, so keep them short: one to three sentences, plain text only, "
    "no lists, no markdown, no emojis. Use a tool only when the request clearly needs it. "
    f"Today is {datetime.date.today():%A, %d %B %Y}."
)

# =====================================================================
#  1. VOICE
# =====================================================================
def speak(text: str) -> None:
    """Say the text out loud (and print it) using the voices built into Windows."""
    print(f"{NAME}: {text}")
    try:
        engine = pyttsx3.init()
        voices = engine.getProperty("voices")
        if voices and VOICE < len(voices):
            engine.setProperty("voice", voices[VOICE].id)
        engine.setProperty("rate", RATE)
        engine.say(text)
        engine.runAndWait()
        engine.stop()
    except Exception as error:                     # a voice problem should never crash Jarvis
        print(f"(voice unavailable: {error})")


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

            if loudness(data) > threshold:
                heard_speech, quiet_time = True, 0.0
            else:
                quiet_time += chunk_seconds

            if heard_speech and quiet_time >= silence_seconds:
                break                                   # you finished your sentence
            if not heard_speech and elapsed >= wait_seconds:
                break                                   # nobody spoke

    return np.concatenate(frames) if heard_speech else None


def listen(threshold: float) -> str:
    """Record one sentence and return it as text ("" if nothing was heard)."""
    print("Listening... (speak now)")
    audio = record(threshold)
    if audio is None:
        return ""
    segments, _ = whisper.transcribe(audio, language=LANGUAGE, beam_size=1, vad_filter=True)
    return " ".join(segment.text.strip() for segment in segments).strip()


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
    tool("get_current_time", "Get the current date and time."),
    tool("get_weather", "Get the current weather for a city.", city="City name, for example Tel Aviv"),
    tool("open_website", "Open a website in the user's browser.", url="Full address, for example https://www.youtube.com"),
    tool("search_web", "Search the internet in the user's browser.", query="What to search for"),
    tool("save_note", "Save a short note for the user.", text="The note to save"),
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


def think(user_text: str) -> str:
    """Send the user's words to the brain, run any tools it asks for, return the reply."""
    forget_old_messages()
    history.append({"role": "user", "content": user_text})

    for _ in range(5):                                  # allow a few tool calls in a row
        response = client.chat.completions.create(model=MODEL, messages=history, tools=TOOLS)
        message = response.choices[0].message

        if not message.tool_calls:                      # a normal answer - we're done
            reply = (message.content or "").strip() or "I'm not sure what to say to that."
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
            result = run_tool(call.function.name, call.function.arguments)
            history.append({"role": "tool", "tool_call_id": call.id, "content": result})

    return "I got a bit lost there. Could you ask that in a different way?"


# =====================================================================
#  5. THE LOOP - ears -> brain -> hands -> voice, again and again
# =====================================================================
def main() -> None:
    print(f"\n{NAME} is starting up...")
    threshold = THRESHOLD
    if threshold is None:
        try:
            print("Calibrating the microphone - please stay quiet for a second...")
            threshold = max(0.005, measure_background_noise() * 3)
        except Exception as error:
            print(f"(microphone problem: {error} - you can still type to me)")
            threshold = 0.01

    speak(f"Hello, I am {NAME}. How can I help?")
    print("\nPress Enter to talk, or type a message and press Enter. Say 'goodbye' to stop.")

    while True:
        try:
            typed = input("\n> ").strip()
        except (KeyboardInterrupt, EOFError):
            typed = "goodbye"

        if typed:
            user_text = typed
        else:
            try:
                user_text = listen(threshold)
            except Exception as error:
                print(f"(microphone problem: {error} - try typing instead)")
                continue

        if not user_text:
            print("I didn't catch that. Try again, a little louder.")
            continue
        print(f"You: {user_text}")

        if user_text.lower().strip(" .!?") in ("goodbye", "bye", "exit", "quit", "stop"):
            speak("Goodbye!")
            break

        try:
            reply = think(user_text)
        except APIConnectionError:
            reply = "I can't reach my brain. Please make sure the Ollama app is running. Look for the llama icon in the system tray."
        except NotFoundError:
            reply = f"The model {MODEL} is not installed. Run: ollama pull {MODEL}"
        except Exception as error:
            reply = f"Something went wrong: {error}"
        speak(reply)


if __name__ == "__main__":
    main()
