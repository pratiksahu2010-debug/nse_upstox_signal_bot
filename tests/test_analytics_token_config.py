import os
import subprocess
import sys


def _config_token_source(analytics_token, legacy_token):
    env = os.environ.copy()
    env["UPSTOX_ANALYTICS_TOKEN"] = analytics_token
    env["UPSTOX_ACCESS_TOKEN"] = legacy_token
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.config import UPSTOX_TOKEN, UPSTOX_TOKEN_SOURCE; "
            "print(UPSTOX_TOKEN + '|' + UPSTOX_TOKEN_SOURCE)",
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_analytics_token_takes_precedence_over_legacy_access_token():
    assert _config_token_source("analytics-secret", "legacy-secret") == (
        "analytics-secret|analytics_token"
    )


def test_legacy_access_token_remains_a_compatibility_fallback():
    assert _config_token_source("", "legacy-secret") == (
        "legacy-secret|legacy_access_token"
    )


def test_missing_tokens_are_reported_without_a_credential_value():
    assert _config_token_source("", "") == "|missing"
