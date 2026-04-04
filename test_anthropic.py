import os
import sys

import anthropic


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY environment variable is not set.")
        print("Set it with: set ANTHROPIC_API_KEY=your-key-here")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    try:
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=256,
            messages=[{"role": "user", "content": "Hello Claude"}],
        )
        print("Response:", response.content[0].text)
    except anthropic.AuthenticationError:
        print("ERROR: Invalid API key. Check your ANTHROPIC_API_KEY.")
        sys.exit(1)
    except anthropic.APIConnectionError:
        print("ERROR: Could not connect to the Anthropic API. Check your internet connection.")
        sys.exit(1)
    except anthropic.APIStatusError as e:
        print(f"ERROR: API returned status {e.status_code}: {e.message}")
        sys.exit(1)


if __name__ == "__main__":
    main()
