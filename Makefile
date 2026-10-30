.PHONY: check test lint prompt-report

check: lint test

lint:
	ruff check lib tools tests bot.py

test:
	python -m pytest -n auto

prompt-report:
	python -m tools.report
