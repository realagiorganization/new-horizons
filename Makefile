.PHONY: train-notebooks cloud-auth-smoke github-env-credentials

train-notebooks:
	python scripts/execute_notebooks.py

cloud-auth-smoke:
	python scripts/cloud_auth_smoke.py

github-env-credentials:
	python scripts/sync_github_environment_credentials.py
