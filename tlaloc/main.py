import os
from dotenv import load_dotenv
import anthropic

load_dotenv()


def main():
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    print("Tlaloc — weather update tool")
    print(f"Anthropic client ready: {client.__class__.__name__}")


if __name__ == "__main__":
    main()
