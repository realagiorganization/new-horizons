#!/usr/bin/env python3
import argparse
import base64
import configparser
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

EXPECTED_ENV_KEYS = [
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AZURE_CLIENT_ID",
    "AZURE_TENANT_ID",
    "AZURE_CLIENT_SECRET",
    "AZURE_SUBSCRIPTION_ID",
    "AZURE_ACCESS_TOKEN",
    "AZURE_ACCESS_TOKEN_EXPIRES_ON",
    "GOOGLE_CLOUD_PROJECT",
    "CLOUDSDK_CORE_PROJECT",
]

AWS_CRED_KEYS = [
    ("aws_access_key_id", "ACCESS_KEY_ID"),
    ("aws_secret_access_key", "SECRET_ACCESS_KEY"),
    ("aws_session_token", "SESSION_TOKEN"),
]


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=check, text=True, capture_output=True)


def render_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        rows = [["(none)"] + ["--"] * (len(headers) - 1)]
    widths = [len(header) for header in headers]
    normalized = [[str(cell) for cell in row] for row in rows]
    for row in normalized:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))
    sep = "+-" + "-+-".join("-" * width for width in widths) + "-+"
    out = [sep]
    out.append("| " + " | ".join(header.ljust(widths[idx]) for idx, header in enumerate(headers)) + " |")
    out.append(sep)
    for row in normalized:
        out.append("| " + " | ".join(row[idx].ljust(widths[idx]) for idx in range(len(headers))) + " |")
    out.append(sep)
    return "\n".join(out)


def print_table(title: str, headers: list[str], rows: list[list[str]]) -> None:
    print(title)
    print(render_table(headers, rows))
    print("")


def sanitize_profile(name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()
    return clean or "PROFILE"


def detect_repo_from_git() -> str | None:
    try:
        result = run(["git", "remote", "get-url", "origin"])
    except Exception:
        return None
    match = re.search(r"github.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$", result.stdout.strip())
    if not match:
        return None
    return f"{match.group('owner')}/{match.group('repo')}"


def format_env_value(value: str) -> str:
    if any(ch in value for ch in [" ", "\t", "\n", "\r", "#", '"']):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def add_secret(
    secrets: dict[str, str],
    sources: dict[str, str],
    key: str,
    value: str | None,
    source: str,
    logs: list[list[str]],
    note: str = "",
) -> None:
    if value is None or str(value).strip() == "":
        logs.append([key, source, "missing", note or "empty value"])
        return
    if key in secrets:
        logs.append([key, source, "skipped", f"kept {sources[key]}"])
        return
    secrets[key] = str(value)
    sources[key] = source
    detail = note or f"len={len(str(value))}"
    logs.append([key, source, "collected", detail])


def try_json(cmd: list[str]) -> tuple[dict | None, str | None]:
    try:
        result = run(cmd, check=False)
    except FileNotFoundError as exc:
        return None, str(exc)
    if result.returncode != 0:
        return None, result.stderr.strip() or result.stdout.strip() or "command failed"
    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError as exc:
        return None, str(exc)


def ensure_environment(repo: str, environment: str) -> None:
    run(
        [
            "gh",
            "api",
            "--method",
            "PUT",
            f"repos/{repo}/environments/{quote(environment, safe='')}",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect local cloud credentials, write .env, and push GitHub environment secrets."
    )
    parser.add_argument("--env", dest="env_name", default=os.environ.get("GH_ENVIRONMENT"))
    parser.add_argument("--repo", default=os.environ.get("GH_REPO"))
    parser.add_argument("--env-file", default=os.environ.get("CREDENTIALS_ENV_FILE", ".env"))
    parser.add_argument("--dry-run", action="store_true", help="Skip GitHub pushes.")
    args = parser.parse_args()

    env_logs: list[list[str]] = []
    aws_logs: list[list[str]] = []
    gcp_logs: list[list[str]] = []
    azure_logs: list[list[str]] = []
    write_logs: list[list[str]] = []
    push_logs: list[list[str]] = []

    secrets: dict[str, str] = {}
    sources: dict[str, str] = {}

    for key in EXPECTED_ENV_KEYS:
        add_secret(secrets, sources, key, os.environ.get(key), "env", env_logs)

    aws_creds_path = Path.home() / ".aws" / "credentials"
    if aws_creds_path.exists():
        parser_cfg = configparser.RawConfigParser()
        parser_cfg.read(aws_creds_path)
        for section in parser_cfg.sections():
            prefix = "AWS" if section == "default" else f"AWS_{sanitize_profile(section)}"
            for key_name, suffix in AWS_CRED_KEYS:
                env_key = f"AWS_{suffix}" if section == "default" else f"{prefix}_{suffix}"
                add_secret(
                    secrets,
                    sources,
                    env_key,
                    parser_cfg.get(section, key_name, fallback=None),
                    f"aws_credentials:{section}",
                    aws_logs,
                )
    else:
        aws_logs.append([str(aws_creds_path), "aws_credentials", "missing", "file not found"])

    aws_config_path = Path.home() / ".aws" / "config"
    if aws_config_path.exists():
        cfg = configparser.RawConfigParser()
        cfg.read(aws_config_path)
        for section in cfg.sections():
            profile = section.split("profile ", 1)[1].strip() if section.startswith("profile ") else section
            prefix = "AWS" if profile == "default" else f"AWS_{sanitize_profile(profile)}"
            env_key = "AWS_DEFAULT_REGION" if profile == "default" else f"{prefix}_REGION"
            add_secret(
                secrets,
                sources,
                env_key,
                cfg.get(section, "region", fallback=None),
                f"aws_config:{profile}",
                aws_logs,
            )
    else:
        aws_logs.append([str(aws_config_path), "aws_config", "missing", "file not found"])

    adc_path = None
    env_adc = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if env_adc:
        candidate = Path(env_adc).expanduser()
        if candidate.exists():
            adc_path = candidate
            gcp_logs.append([str(candidate), "gcp_adc", "found", "from GOOGLE_APPLICATION_CREDENTIALS"])
        else:
            gcp_logs.append([str(candidate), "gcp_adc", "missing", "env path not found"])

    default_adc = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
    if adc_path is None and default_adc.exists():
        adc_path = default_adc
        gcp_logs.append([str(default_adc), "gcp_adc", "found", "from default gcloud config"])
    elif adc_path is None:
        gcp_logs.append([str(default_adc), "gcp_adc", "missing", "file not found"])

    if adc_path is not None:
        adc_text = adc_path.read_text()
        add_secret(secrets, sources, "GOOGLE_APPLICATION_CREDENTIALS_JSON", adc_text, "gcp_adc", gcp_logs, note=f"chars={len(adc_text)}")
        adc_b64 = base64.b64encode(adc_text.encode("utf-8")).decode("ascii")
        add_secret(secrets, sources, "GOOGLE_APPLICATION_CREDENTIALS_JSON_B64", adc_b64, "gcp_adc", gcp_logs)

    try:
        gcloud_project = run(["gcloud", "config", "get-value", "project"], check=False)
    except FileNotFoundError as exc:
        gcp_logs.append(["gcloud project", "gcloud_config", "missing", str(exc)])
    else:
        if gcloud_project.returncode == 0:
            project = gcloud_project.stdout.strip()
            add_secret(secrets, sources, "GOOGLE_CLOUD_PROJECT", project, "gcloud_config", gcp_logs)
            add_secret(secrets, sources, "CLOUDSDK_CORE_PROJECT", project, "gcloud_config", gcp_logs)
        else:
            gcp_logs.append(["gcloud project", "gcloud_config", "missing", gcloud_project.stderr.strip() or "not available"])

    azure_account, azure_error = try_json(["az", "account", "show", "--output", "json"])
    if azure_account is not None:
        add_secret(secrets, sources, "AZURE_SUBSCRIPTION_ID", azure_account.get("id"), "az_account_show", azure_logs)
        add_secret(secrets, sources, "AZURE_TENANT_ID", azure_account.get("tenantId"), "az_account_show", azure_logs)
        add_secret(
            secrets,
            sources,
            "AZURE_ENVIRONMENT_NAME",
            azure_account.get("environmentName"),
            "az_account_show",
            azure_logs,
        )
    else:
        azure_logs.append(["az account show", "az_account_show", "missing", azure_error or "not logged in"])

    azure_token, azure_token_error = try_json(
        [
            "az",
            "account",
            "get-access-token",
            "--resource",
            "https://management.azure.com/",
            "--output",
            "json",
        ]
    )
    if azure_token is not None:
        add_secret(
            secrets,
            sources,
            "AZURE_ACCESS_TOKEN",
            azure_token.get("accessToken"),
            "az_account_get_access_token",
            azure_logs,
            note="ephemeral token",
        )
        add_secret(
            secrets,
            sources,
            "AZURE_ACCESS_TOKEN_EXPIRES_ON",
            azure_token.get("expiresOn"),
            "az_account_get_access_token",
            azure_logs,
        )
    else:
        azure_logs.append(["az account get-access-token", "az_account_get_access_token", "missing", azure_token_error or "token unavailable"])

    print_table("== Environment variable scan", ["Key", "Source", "Status", "Notes"], env_logs)
    print_table("== AWS scan", ["Key/Profile", "Source", "Status", "Notes"], aws_logs)
    print_table("== GCP scan", ["Path/Key", "Source", "Status", "Notes"], gcp_logs)
    print_table("== Azure scan", ["Path/Key", "Source", "Status", "Notes"], azure_logs)

    env_path = Path(args.env_file).expanduser()
    lines = [
        f"# Generated by {Path(__file__).name} at {datetime.now().isoformat(timespec='seconds')}",
        "# DO NOT COMMIT THIS FILE",
    ]
    for key in sorted(secrets):
        lines.append(f"{key}={format_env_value(secrets[key])}")
    lines.append("")
    env_path.write_text("\n".join(lines))
    write_logs.append([".env path", str(env_path), "written", "ok"])
    write_logs.append(["secrets", str(len(secrets)), "written", "keys collected"])
    print_table("== .env write", ["Item", "Value", "Status", "Notes"], write_logs)

    repo = args.repo or detect_repo_from_git()
    env_name = args.env_name
    if not env_name:
        print("ERROR: missing GitHub environment name. Set GH_ENVIRONMENT or use --env.", file=sys.stderr)
        return 2
    if not repo:
        print("ERROR: unable to determine repo. Set GH_REPO or use --repo.", file=sys.stderr)
        return 2

    print_table(
        "== GitHub target",
        ["Item", "Value"],
        [["repo", repo], ["environment", env_name], ["dry-run", str(args.dry_run)]],
    )

    try:
        run(["gh", "auth", "status", "--hostname", "github.com"])
    except Exception as exc:
        print(f"ERROR: gh auth status failed: {exc}", file=sys.stderr)
        return 2

    if not args.dry_run:
        ensure_environment(repo, env_name)

    for key, value in secrets.items():
        if args.dry_run:
            push_logs.append([key, "skipped", "dry-run"])
            continue
        try:
            run(["gh", "secret", "set", key, "-R", repo, "-e", env_name, "-b", value])
            push_logs.append([key, "ok", "secret set"])
        except subprocess.CalledProcessError as exc:
            push_logs.append([key, "error", exc.stderr.strip() or "gh secret set failed"])

    print_table("== GitHub environment push", ["Secret", "Status", "Notes"], push_logs)
    return 1 if any(row[1] == "error" for row in push_logs) else 0


if __name__ == "__main__":
    raise SystemExit(main())
