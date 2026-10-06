.PHONY: check test lint prompt-report

check: lint test

lint:
	ruff check lib tests bot.py

test:
	python -m pytest

prompt-report:
	python -m lib.report
