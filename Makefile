PYTHON ?= python3

.PHONY: check compile privacy-check

check: compile privacy-check

compile:
	$(PYTHON) -m py_compile scripts/*.py

privacy-check:
	$(PYTHON) scripts/privacy_check.py
