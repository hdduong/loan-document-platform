from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMMON_MODULE = ROOT / "scripts" / "common.psm1"


def classify_stack_lookup_error(error_text: str) -> bool:
    module_path = str(COMMON_MODULE).replace("'", "''")
    script = (
        f"Import-Module -Force '{module_path}'; "
        "$errorText = [Environment]::GetEnvironmentVariable('TEST_STACK_ERROR'); "
        "$result = Test-AwsCloudFormationStackNotFound -ErrorText $errorText; "
        "[Console]::Out.Write($result.ToString().ToLowerInvariant())"
    )
    environment = os.environ.copy()
    environment["TEST_STACK_ERROR"] = error_text
    completed = subprocess.run(
        ["pwsh", "-NoLogo", "-NoProfile", "-Command", script],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return completed.stdout.strip() == "true"


def stack_policy_is_valid(policy: dict[str, object]) -> bool:
    module_path = str(COMMON_MODULE).replace("'", "''")
    script = (
        f"Import-Module -Force '{module_path}'; "
        "$policy = [Environment]::GetEnvironmentVariable('TEST_STACK_POLICY') | "
        "ConvertFrom-Json -Depth 30; "
        "try { Assert-AwsStatefulStackPolicy -Policy $policy; "
        "[Console]::Out.Write('true') } catch { [Console]::Out.Write('false') }"
    )
    environment = os.environ.copy()
    environment["TEST_STACK_POLICY"] = json.dumps(policy)
    completed = subprocess.run(
        ["pwsh", "-NoLogo", "-NoProfile", "-Command", script],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return completed.stdout.strip() == "true"


@pytest.mark.parametrize(
    "stack_id",
    [
        "loan-idp-prod",
        "arn:aws:cloudformation:us-west-2:123456789012:stack/loan-idp-prod/abc123",
    ],
)
def test_cloudformation_classifier_accepts_only_stack_not_found(stack_id: str) -> None:
    message = (
        "An error occurred (ValidationError) when calling the DescribeStacks operation: "
        f"Stack with id {stack_id} does not exist"
    )

    assert classify_stack_lookup_error(message)


@pytest.mark.parametrize(
    "message",
    [
        (
            "An error occurred (AccessDenied) when calling the DescribeStacks operation: "
            "User is not authorized"
        ),
        (
            "An error occurred (ThrottlingException) when calling the DescribeStacks operation: "
            "Rate exceeded"
        ),
        (
            "An error occurred (ValidationError) when calling the DescribeStacks operation: "
            "Stack name is invalid"
        ),
        (
            "WARNING: credentials are near expiry\n"
            "An error occurred (ValidationError) when calling the DescribeStacks operation: "
            "Stack with id loan-idp-prod does not exist"
        ),
        "",
    ],
)
def test_cloudformation_classifier_fails_closed_for_other_errors(message: str) -> None:
    assert not classify_stack_lookup_error(message)


def test_stateful_stack_policy_requires_default_allow_and_all_protected_types() -> None:
    policy = json.loads(
        (ROOT / "infra" / "stack-policies" / "protect-stateful-resources.json").read_text(
            encoding="utf-8"
        )
    )

    assert stack_policy_is_valid(policy)

    policy["Statement"][1]["Condition"]["StringEquals"]["ResourceType"].remove("AWS::KMS::Key")
    assert not stack_policy_is_valid(policy)

    policy["Statement"][1]["Condition"]["StringEquals"]["ResourceType"].append("AWS::KMS::Key")
    policy["Statement"][1]["Action"].remove("Update:Replace")
    assert not stack_policy_is_valid(policy)
