.PHONY: check test lint prompt-report

check: lint test

lint:
	ruff check tgbot tests bot.py

test:
	python -m pytest

prompt-report:
	python -m tgbot.report
