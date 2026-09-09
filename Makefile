.PHONY: install kafka-up kafka-down topics test test-all lint fmt

install:          ## create the venv and install the package with dev extras
	python3 -m venv .venv && .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -e '.[dev]'

kafka-up:         ## start the local single-broker KRaft cluster
	docker compose up -d

kafka-down:       ## stop it (add `-v` to drop the log dir)
	docker compose down

topics:           ## provision topics (auto-creation is disabled on purpose)
	.venv/bin/python -m olv.kafka_admin provision --ticker QQQ

test:             ## unit tests only; no broker needed
	.venv/bin/python -m pytest -m "not integration"

test-all:         ## everything, including tests against a running broker
	.venv/bin/python -m pytest

lint:
	.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests

fmt:
	.venv/bin/ruff format src tests && .venv/bin/ruff check --fix src tests
