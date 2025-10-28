from huggingface_hub import InferenceClient, HfApi
from dotenv import load_dotenv
import os

load_dotenv()
HF_TOKEN = os.getenv("HUGGINGFACE_API_TOKEN")
HF_MODEL = os.getenv("HF_MODEL", "deepseek-ai/DeepSeek-V3")

client = InferenceClient(token=HF_TOKEN)
api = HfApi()

print("HF token present:", bool(HF_TOKEN))
print("HF model:", HF_MODEL)

# Optional: check model metadata
try:
    info = api.model_info(HF_MODEL)
    print("pipeline_tag:", getattr(info, "pipeline_tag", None))
    tags = getattr(info, "tags", None)
    print("tags (sample):", tags[:10] if tags else None)
except Exception as e:
    print("Couldn't fetch model metadata:", e)

prompt = "Summarize AI in one sentence."

def generate_with_fallback(prompt, max_new_tokens=50):
    # 1) Try text_generation *with explicit model*
    try:
        # pass model explicitly to avoid hitting a default backend model
        gen = client.text_generation(model=HF_MODEL, inputs=prompt, parameters={"max_new_tokens": max_new_tokens})
        # client may return different shapes — normalize to string:
        if isinstance(gen, (list, tuple)) and len(gen) > 0:
            # some clients return a list of dicts
            first = gen[0]
            if isinstance(first, dict) and "generated_text" in first:
                return first["generated_text"]
            return str(first)
        if isinstance(gen, dict) and "generated_text" in gen:
            return gen["generated_text"]
        return str(gen)
    except Exception as e:
        print("text_generation failed:", e)

    # 2) Try chat completions (explicit model)
    try:
        chat_resp = client.chat.completions.create(
            model=HF_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_new_tokens
        )
        return chat_resp['choices'][0]['message']['content']
    except Exception as e2:
        print("chat completion also failed:", e2)
        raise RuntimeError("Both text_generation and chat completion failed") from e2

out = generate_with_fallback(prompt)
print("Output:", out)
print("Output length:", len(out))