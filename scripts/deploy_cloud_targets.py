#!/usr/bin/env python3
import argparse
import base64
import json
import os
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests


def git_head() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_bucket_name(value: str, max_length: int = 63) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9-]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value[:max_length].strip("-")


def ensure_build_dir() -> Path:
    path = Path("build")
    path.mkdir(exist_ok=True)
    return path


def infer_targets() -> list[str]:
    raw = os.environ.get("TARGET_CLOUDS", "").strip()
    if raw:
        return [item.strip().lower() for item in raw.split(",") if item.strip()]

    inferred = []
    if os.environ.get("AWS_DEPLOY_REGION") or os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION"):
        inferred.append("aws")
    if os.environ.get("AZURE_SUBSCRIPTION_ID") or os.environ.get("AZURE_RESOURCE_GROUP"):
        inferred.append("azure")
    if os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("CLOUDSDK_CORE_PROJECT"):
        inferred.append("gcp")
    return inferred


def build_manifest(targets: list[str]) -> dict[str, Any]:
    notebooks = sorted(path.name for path in Path("notebooks").glob("*.ipynb"))
    return {
        "repo": os.environ.get("GITHUB_REPOSITORY", "realagiorganization/new-horizons"),
        "commit": os.environ.get("GITHUB_SHA") or git_head(),
        "generated_at": utc_now(),
        "namespace": os.environ.get("DEPLOY_NAMESPACE", "new-horizons"),
        "targets": targets,
        "notebooks": notebooks,
        "workflow": os.environ.get("GITHUB_WORKFLOW"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
    }


@dataclass
class DeployResult:
    provider: str
    status: str
    detail: str
    resource: str = ""


def aws_deploy(manifest: dict[str, Any], apply: bool) -> DeployResult:
    region = os.environ.get("AWS_DEPLOY_REGION") or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        return DeployResult("aws", "blocked", "missing AWS region")

    if not os.environ.get("AWS_ACCESS_KEY_ID") or not os.environ.get("AWS_SECRET_ACCESS_KEY"):
        return DeployResult("aws", "blocked", "missing AWS credentials")

    try:
        import boto3
        from botocore.exceptions import ClientError
    except Exception as exc:  # pragma: no cover
        return DeployResult("aws", "failed", f"missing boto3: {exc}")

    try:
        sts = boto3.client("sts", region_name=region)
        account = sts.get_caller_identity()["Account"]
        bucket_prefix = os.environ.get("AWS_S3_BUCKET_PREFIX", f"{manifest['namespace']}-training")
        bucket = sanitize_bucket_name(f"{bucket_prefix}-{account}-{region}")
        key = f"deployments/{manifest['commit']}.json"
        if not apply:
            return DeployResult("aws", "planned", f"would upload deployment manifest to s3://{bucket}/{key}", f"s3://{bucket}/{key}")

        s3 = boto3.client("s3", region_name=region)
        try:
            s3.head_bucket(Bucket=bucket)
        except ClientError:
            params: dict[str, Any] = {"Bucket": bucket}
            if region != "us-east-1":
                params["CreateBucketConfiguration"] = {"LocationConstraint": region}
            s3.create_bucket(**params)
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(manifest, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        return DeployResult("aws", "ok", "uploaded deployment manifest", f"s3://{bucket}/{key}")
    except Exception as exc:  # pragma: no cover
        return DeployResult("aws", "failed", str(exc))


def azure_deploy(manifest: dict[str, Any], apply: bool) -> DeployResult:
    subscription_id = os.environ.get("AZURE_SUBSCRIPTION_ID")
    token = os.environ.get("AZURE_ACCESS_TOKEN")
    resource_group = os.environ.get("AZURE_RESOURCE_GROUP", f"{manifest['namespace']}-training-rg")
    location = os.environ.get("AZURE_LOCATION", "eastus2")

    if not subscription_id:
        return DeployResult("azure", "blocked", "missing Azure subscription id")
    if not token:
        return DeployResult("azure", "blocked", "missing Azure ARM access token", resource_group)

    url = (
        f"https://management.azure.com/subscriptions/{subscription_id}/resourcegroups/"
        f"{resource_group}?api-version=2021-04-01"
    )
    payload = {
        "location": location,
        "tags": {
            "repo": manifest["repo"],
            "commit": manifest["commit"],
            "namespace": manifest["namespace"],
        },
    }
    if not apply:
        return DeployResult("azure", "planned", f"would create or update resource group in {location}", resource_group)

    try:
        response = requests.put(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        return DeployResult("azure", "ok", f"resource group ready in {location}", resource_group)
    except Exception as exc:  # pragma: no cover
        return DeployResult("azure", "failed", str(exc), resource_group)


def load_google_credentials() -> tuple[Any | None, str | None, str | None]:
    raw_json = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    raw_b64 = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON_B64")
    if not raw_json and not raw_b64:
        return None, None, "missing ADC JSON secret"

    payload = raw_json
    if payload is None and raw_b64:
        payload = base64.b64decode(raw_b64).decode("utf-8")

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        handle.write(payload)
        tmp_path = handle.name

    try:
        import google.auth
        from google.auth.transport.requests import Request

        credentials, project_id = google.auth.load_credentials_from_file(
            tmp_path,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        credentials.refresh(Request())
        return credentials, project_id, None
    except Exception as exc:  # pragma: no cover
        return None, None, str(exc)
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def gcp_deploy(manifest: dict[str, Any], apply: bool) -> DeployResult:
    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("CLOUDSDK_CORE_PROJECT")
    if not project:
        return DeployResult("gcp", "blocked", "missing GCP project")

    credentials, inferred_project, error = load_google_credentials()
    if error:
        return DeployResult("gcp", "blocked", error)
    project = project or inferred_project
    bucket_prefix = os.environ.get("GCP_GCS_BUCKET_PREFIX", f"{manifest['namespace']}-training")
    bucket = sanitize_bucket_name(f"{bucket_prefix}-{project}")
    object_name = f"deployments/{manifest['commit']}.json"

    if not apply:
        return DeployResult("gcp", "planned", f"would upload deployment manifest to gs://{bucket}/{object_name}", f"gs://{bucket}/{object_name}")

    try:
        headers = {
            "Authorization": f"Bearer {credentials.token}",
            "Content-Type": "application/json",
        }
        create_response = requests.post(
            f"https://storage.googleapis.com/storage/v1/b?project={quote(project, safe='')}",
            headers=headers,
            json={
                "name": bucket,
                "location": os.environ.get("GCP_LOCATION", "US"),
                "labels": {"repo": "new-horizons"},
            },
            timeout=30,
        )
        if create_response.status_code not in (200, 201, 409):
            create_response.raise_for_status()

        upload_response = requests.post(
            f"https://storage.googleapis.com/upload/storage/v1/b/{quote(bucket, safe='')}/o",
            params={"uploadType": "media", "name": object_name},
            headers={
                "Authorization": f"Bearer {credentials.token}",
                "Content-Type": "application/json",
            },
            data=json.dumps(manifest, indent=2).encode("utf-8"),
            timeout=30,
        )
        upload_response.raise_for_status()
        return DeployResult("gcp", "ok", "uploaded deployment manifest", f"gs://{bucket}/{object_name}")
    except Exception as exc:  # pragma: no cover
        return DeployResult("gcp", "failed", str(exc))


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy minimal cloud landing zones for New Horizons.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Apply remote changes.")
    mode.add_argument("--plan", action="store_true", help="Show what would be deployed.")
    args = parser.parse_args()

    apply = args.apply or not args.plan
    targets = infer_targets()
    manifest = build_manifest(targets)
    build_dir = ensure_build_dir()
    (build_dir / "deploy-manifest.json").write_text(json.dumps(manifest, indent=2))

    if not targets:
        print("No deployment targets configured via TARGET_CLOUDS or provider environment variables.")
        return 1

    handlers = {
        "aws": aws_deploy,
        "azure": azure_deploy,
        "gcp": gcp_deploy,
    }

    results = []
    for target in targets:
        handler = handlers.get(target)
        if handler is None:
            results.append(DeployResult(target, "blocked", "unsupported target"))
            continue
        results.append(handler(manifest, apply))

    output = {
        "mode": "apply" if apply else "plan",
        "manifest": manifest,
        "results": [asdict(result) for result in results],
    }
    (build_dir / "deploy-results.json").write_text(json.dumps(output, indent=2))
    print(json.dumps(output, indent=2))

    if not apply:
        return 0
    return 1 if any(result.status in {"blocked", "failed"} for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
