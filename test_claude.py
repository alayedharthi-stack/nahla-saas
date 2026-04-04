import os
from anthropic import Anthropic

client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=100,
    messages=[
        {"role": "user", "content": "Say hello in Arabic"}
    ],
)

print(response.content)

print(response.content)