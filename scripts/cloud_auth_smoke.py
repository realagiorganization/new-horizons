#!/usr/bin/env python3
import base64
import json
import os
import tempfile
from pathlib import Path


def print_result(provider: str, status: str, details: str) -> None:
    print(f"[{provider}] {status}: {details}")


def aws_smoke() -> bool:
    access_key = os.environ.get("AWS_ACCESS_KEY_ID")
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
    if not access_key or not secret_key:
        print_result("aws", "skipped", "missing AWS access key material")
        return True

    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - dependency failure
        print_result("aws", "failed", f"missing dependency: {exc}")
        return False

    try:
        client = boto3.client("sts", region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))
        identity = client.get_caller_identity()
        arn = identity.get("Arn", "unknown")
        account = identity.get("Account", "unknown")
        print_result("aws", "ok", f"account={account} arn={arn}")
        return True
    except Exception as exc:  # pragma: no cover - network/auth surface
        print_result("aws", "failed", str(exc))
        return False


def load_google_credentials_file() -> Path | None:
    raw_json = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    raw_b64 = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON_B64")
    if not raw_json and not raw_b64:
        return None

    payload = raw_json
    if payload is None and raw_b64:
        payload = base64.b64decode(raw_b64).decode("utf-8")

    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    tmp.write(payload)
    tmp.flush()
    tmp.close()
    return Path(tmp.name)


def gcp_smoke() -> bool:
    creds_path = load_google_credentials_file()
    if creds_path is None:
        print_result("gcp", "skipped", "missing ADC JSON secret")
        return True

    try:
        import google.auth
        from google.auth.transport.requests import Request
    except ImportError as exc:  # pragma: no cover - dependency failure
        print_result("gcp", "failed", f"missing dependency: {exc}")
        return False

    try:
        credentials, project_id = google.auth.load_credentials_from_file(
            str(creds_path),
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        credentials.refresh(Request())
        project = (
            project_id
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or os.environ.get("CLOUDSDK_CORE_PROJECT")
            or "unknown"
        )
        print_result("gcp", "ok", f"project={project} token_refreshed=yes")
        return True
    except Exception as exc:  # pragma: no cover - network/auth surface
        print_result("gcp", "failed", str(exc))
        return False
    finally:
        try:
            creds_path.unlink()
        except FileNotFoundError:
            pass


def azure_smoke() -> bool:
    token = os.environ.get("AZURE_ACCESS_TOKEN")
    subscription_id = os.environ.get("AZURE_SUBSCRIPTION_ID")
    if not token or not subscription_id:
        print_result("azure", "skipped", "missing Azure access token or subscription id")
        return True

    try:
        import requests
    except ImportError as exc:  # pragma: no cover - dependency failure
        print_result("azure", "failed", f"missing dependency: {exc}")
        return False

    try:
        response = requests.get(
            f"https://management.azure.com/subscriptions/{subscription_id}",
            params={"api-version": "2020-01-01"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        state = payload.get("state", "unknown")
        display_name = payload.get("displayName", "unknown")
        print_result("azure", "ok", f"subscription={display_name} state={state}")
        return True
    except Exception as exc:  # pragma: no cover - network/auth surface
        print_result("azure", "failed", str(exc))
        return False


def main() -> int:
    checks = [aws_smoke(), gcp_smoke(), azure_smoke()]
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
