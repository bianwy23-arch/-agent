import argparse
import asyncio
import json
from pathlib import Path

from .config import Settings
from .runtime import ShoppingRuntime


async def chat(args):
    runtime = ShoppingRuntime(Settings.load(args.root), runtime_dir=args.runtime_dir)
    try:
        conversation_id = args.conversation or runtime.create_conversation()
        print(f"conversation_id: {conversation_id}")
        while True:
            message = args.message or input("你：").strip()
            if message in {"/exit", "/quit"}:
                return
            if not message:
                continue
            result = await runtime.run(conversation_id, message)
            print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else result["message"])
            if not args.json:
                print(f"[{result['kind']}] {result.get('price_notice', '')}")
            if args.message:
                return
    finally:
        await runtime.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--conversation")
    parser.add_argument("--message")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(chat(args))
    except (ValueError, KeyboardInterrupt) as exc:
        parser.exit(2, "Configuration/input error. Check the local environment and arguments.\n")


if __name__ == "__main__":
    main()
