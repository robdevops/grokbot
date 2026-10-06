#!/usr/bin/env python3
"""Telegram group bot that answers @mentions with an LLM (xAI or OpenRouter).

Usage: python bot.py [label]     (the label is ignored; it only shows in ps/top)
See README.md for configuration; the code lives in the lib/ package.
"""

from lib.app import main

if __name__ == "__main__":
    main()
