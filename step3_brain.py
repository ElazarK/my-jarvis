"""
STEP 3 - Give Jarvis a brain.

Run:   python step3_brain.py
Type a question and get an answer from an AI model running 100% on your computer.

Before you run this:
  1. Ollama must be installed and running (look for the llama icon in the system tray, bottom right).
  2. The model must be downloaded once:   ollama pull llama3.2
"""
from openai import OpenAI, APIConnectionError, NotFoundError

MODEL = "llama3.2"      # the brain. On a strong PC try "llama3.1:8b" or "qwen2.5:7b" (pull them first)

# Ollama understands the same API as OpenAI, so we point the OpenAI library at Ollama.
# The api_key is required by the library but Ollama ignores it.
client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")

history = [
    {"role": "system",
     "content": "You are Jarvis, a friendly, witty assistant. Keep every answer short: one to three sentences."}
]

print("Chat with Jarvis. Type 'quit' to stop.")
while True:
    user_text = input("\nYou: ").strip()
    if user_text.lower() in ("quit", "exit", "bye", "goodbye"):
        print("Jarvis: Goodbye!")
        break
    if not user_text:
        continue

    history.append({"role": "user", "content": user_text})
    try:
        response = client.chat.completions.create(model=MODEL, messages=history)
    except APIConnectionError:
        print("Jarvis: I can't reach Ollama. Is the Ollama app running? Look for the llama icon in the system tray.")
        history.pop()
        continue
    except NotFoundError:
        print(f"Jarvis: The model '{MODEL}' is not installed yet. Run:  ollama pull {MODEL}")
        history.pop()
        continue

    answer = response.choices[0].message.content.strip()
    history.append({"role": "assistant", "content": answer})
    print("Jarvis:", answer)
