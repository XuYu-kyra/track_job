PYTHON ?= python3

.PHONY: check compile config-check test privacy-check candidate-evidence resume-bases resume-base-benchmarks sync-robot-bases source-audit public-web-query-plan discovered-companies discovery-coverage

check: compile config-check test privacy-check

compile:
	$(PYTHON) -m py_compile scripts/*.py

config-check:
	$(PYTHON) scripts/validate_config.py --targets config/targets.example.yaml --source-registry config/company_registry.example.yaml --profile config/profile.example.yaml

test:
	$(PYTHON) -m unittest discover -s tests -v

privacy-check:
	$(PYTHON) scripts/privacy_check.py

candidate-evidence:
	$(PYTHON) scripts/build_candidate_evidence.py

resume-bases:
	$(PYTHON) scripts/import_resume_bases.py

resume-base-benchmarks:
	$(PYTHON) scripts/benchmark_resume_bases.py

sync-robot-bases:
	$(PYTHON) scripts/sync_resume_base_sources.py --role robot

source-audit:
	$(PYTHON) scripts/audit_company_sources.py

public-web-query-plan:
	$(PYTHON) scripts/build_public_web_query_plan.py

discovered-companies:
	$(PYTHON) scripts/discovered_companies.py

discovery-coverage:
	$(PYTHON) scripts/build_discovery_coverage.py
