.PHONY: help setup test run deploy clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

setup:  ## Create a venv (py3.10+) and install deps
	python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
	cp -n .env.example .env || true

test:  ## Run the unit tests (tools + score, no AWS needed)
	python3 tests/test_tools.py && python3 tests/test_score.py

run:  ## Onboard a repo from the CLI (REPO=owner/name). Offline tools, no model call.
	python3 -m repopilot.cli $(REPO) --offline

run-ai:  ## Onboard with the Bedrock Nova Pro agent (needs AWS creds + Nova Pro access)
	python3 -m repopilot.cli $(REPO) --region $(AWS_REGION)

deploy:  ## Build the Lambda zip and update the function (needs AWS creds)
	bash scripts/deploy.sh

clean:  ## Remove build artifacts
	rm -rf build *.zip .venv **/__pycache__
