.PHONY: train-notebooks cloud-auth-smoke github-env-credentials deploy-cloud-targets

train-notebooks:
	python scripts/execute_notebooks.py

cloud-auth-smoke:
	python scripts/cloud_auth_smoke.py

github-env-credentials:
	python scripts/sync_github_environment_credentials.py

deploy-cloud-targets:
	python scripts/deploy_cloud_targets.py --apply
