# My Jarvis (Windows)

A voice assistant that runs 100% on your own Windows PC. Free, private, no API keys, no monthly fees.

| Part  | What it does                                        | Made with                                   |
|-------|-----------------------------------------------------|---------------------------------------------|
| Ears  | turns your speech into text                         | faster-whisper (runs locally)               |
| Brain | understands you and decides what to do              | Ollama + llama3.2 (runs locally)            |
| Hands | does things: time, weather, web, notes, smart home  | small Python functions ("tools")            |
| Voice | reads the answer out loud                           | the voices built into Windows               |

## Files

| File              | Purpose                                              |
|-------------------|------------------------------------------------------|
| requirements.txt  | the Python packages to install                       |
| step1_speak.py    | test: your PC talks                                  |
| step2_listen.py   | test: your PC understands what you say               |
| step3_brain.py    | test: chat with the local AI model by typing         |
| jarvis.py         | the real thing: ears + brain + hands + voice         |

## Quick start (Windows 10 or 11)

1. Install Python 3.11 (tick "Add python.exe to PATH"), then Ollama, then run `ollama pull llama3.2`.
2. Open this folder in Visual Studio Code, open a terminal (Terminal > New Terminal) and run:

   ```
   py -3.11 -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```

   If PowerShell says "running scripts is disabled", run this once and try the activate line again:
   `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

3. Run the steps in order: `python step1_speak.py`, `python step2_listen.py`, `python step3_brain.py`, then `python jarvis.py`.

If Jarvis never hears you: Settings > Privacy & security > Microphone > allow desktop apps to access your microphone.

## Settings

All settings are at the top of `jarvis.py`: the model, the voice and speed, the language, the microphone sensitivity, and the optional Home Assistant connection.

## Add your own tool

Three moves, all inside `jarvis.py`. Example: a tool that reads back your saved notes.

1. Write a small Python function in the HANDS section:

   ```python
   def read_notes() -> str:
       try:
           with open("notes.txt", encoding="utf-8") as file:
               return file.read()[-1500:] or "There are no notes yet."
       except FileNotFoundError:
           return "There are no notes yet."
   ```

2. Add it to the `TOOL_FUNCTIONS` dictionary:

   ```python
   "read_notes": read_notes,
   ```

3. Add one line to the `TOOLS` list describing it in plain English:

   ```python
   tool("read_notes", "Read back the notes the user saved earlier."),
   ```

Restart Jarvis and say "What are my notes?". The brain reads the description and starts using your tool when it makes sense.
Tools with inputs work the same way: give the function a parameter and describe it in the `tool(...)` line, exactly like `get_weather` does with `city`.
